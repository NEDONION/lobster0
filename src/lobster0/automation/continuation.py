"""把 Agent Approval continuation 原子结算回 durable TaskRun。"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from lobster0.agent.runner import AgentRunBudget
from lobster0.agent.turn import TurnExecutionProfile, TurnResult
from lobster0.automation.models import RunStatus, TaskResponse, TaskRun
from lobster0.automation.repository import TaskRunRepository

_LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """返回 continuation 默认使用的 aware UTC 时间。"""
    return datetime.now(UTC)


class ContinuationDeliveryProjector(Protocol):
    """收窄审批续跑对 durable Delivery projector 的依赖。"""

    def project(self, run: TaskRun, response: TaskResponse) -> tuple[object, ...]:
        """幂等投影已经成功结算的 terminal response。"""
        ...


class TaskApprovalContinuation:
    """驱动 waiting→running→waiting/terminal 的审批续跑状态机。"""

    def __init__(
        self,
        runs: TaskRunRepository,
        *,
        delivery: ContinuationDeliveryProjector | None = None,
        clock: Callable[[], datetime] | None = None,
        audit: Callable[[str, dict[str, int | str]], None] | None = None,
    ) -> None:
        """绑定 TaskRun、可选 Delivery、时钟与脱敏 lifecycle audit。"""
        self._runs = runs
        self._delivery = delivery
        self._clock = clock or _utc_now
        self._audit_callback = audit

    def begin(self, profile: TurnExecutionProfile, approval_id: int) -> None:
        """在 Tool side effect 前用精确 Approval 取得 continuation lease。"""
        run_id, budget = _automation_binding(profile)
        self._runs.resume_waiting(
            run_id,
            approval_id,
            worker_id=_worker_id(approval_id),
            now=self._clock(),
            lease_seconds=max(10, budget.timeout_seconds + 10),
        )

    def settle(
        self,
        profile: TurnExecutionProfile,
        approval_id: int,
        result: TurnResult,
    ) -> None:
        """保存再次等待、稳定失败或 terminal success，并在提交后投影。"""
        run_id, _ = _automation_binding(profile)
        worker_id = _worker_id(approval_id)
        if result.approval_id is not None:
            waiting = self._runs.mark_waiting(
                run_id,
                worker_id,
                session_id=result.session_id,
                turn_id=result.turn_id,
                approval_id=result.approval_id,
            )
            self._audit(
                "task_run.waiting_approval",
                {
                    "approval_id": result.approval_id,
                    "run_id": waiting.id,
                    "task_id": waiting.task_id,
                },
            )
            return
        if result.error_code is not None:
            self._finish_failure(
                run_id,
                worker_id,
                result.error_code,
                session_id=result.session_id,
                turn_id=result.turn_id,
            )
            return
        if result.terminal_response is None:
            self._finish_failure(
                run_id,
                worker_id,
                "automation_terminal_response_missing",
                session_id=result.session_id,
                turn_id=result.turn_id,
            )
            return
        completed = self._runs.finish(
            run_id,
            status=RunStatus.SUCCEEDED,
            now=self._clock(),
            worker_id=worker_id,
            response=result.terminal_response,
            result_preview=_preview(result.terminal_response.text),
            usage={
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
            session_id=result.session_id,
            turn_id=result.turn_id,
        )
        if self._delivery is not None:
            try:
                self._delivery.project(completed, result.terminal_response)
            except Exception:  # noqa: BLE001 - 已落库终态由 startup recovery 补投影
                _LOGGER.warning("task_delivery_projection_failed", exc_info=False)
        self._audit(
            "task_run.terminal",
            {"run_id": completed.id, "task_id": completed.task_id, "status": "succeeded"},
        )

    def fail(
        self,
        profile: TurnExecutionProfile,
        approval_id: int,
        *,
        error_code: str,
        session_id: int,
        turn_id: int | None,
        interrupted: bool = False,
        timed_out: bool = False,
    ) -> None:
        """把已取得 lease 的异常续跑结算为 failed 或 interrupted。"""
        run_id, _ = _automation_binding(profile)
        if interrupted and timed_out:
            raise ValueError("continuation cannot be interrupted and timed out")
        self._finish_failure(
            run_id,
            _worker_id(approval_id),
            error_code,
            session_id=session_id,
            turn_id=turn_id,
            status=(
                RunStatus.INTERRUPTED
                if interrupted
                else RunStatus.TIMED_OUT
                if timed_out
                else RunStatus.FAILED
            ),
        )

    def _finish_failure(
        self,
        run_id: int,
        worker_id: str,
        error_code: str,
        *,
        session_id: int,
        turn_id: int | None,
        status: RunStatus = RunStatus.FAILED,
    ) -> None:
        """以稳定 code 结算失败且不持久化 Prompt/Provider 正文。"""
        completed = self._runs.finish(
            run_id,
            status=status,
            now=self._clock(),
            worker_id=worker_id,
            error_code=error_code,
            session_id=session_id,
            turn_id=turn_id,
        )
        self._audit(
            "task_run.terminal",
            {
                "error_code": error_code,
                "run_id": completed.id,
                "task_id": completed.task_id,
                "status": completed.status.value,
            },
        )

    def _audit(self, event_type: str, metadata: dict[str, int | str]) -> None:
        """best-effort 发出不含正文和平台 ID 的 lifecycle audit。"""
        if self._audit_callback is None:
            return
        try:
            self._audit_callback(event_type, metadata)
        except Exception:  # noqa: BLE001 - Audit 不改变已经持久化的 Run 状态
            _LOGGER.warning("automation_audit_failed", exc_info=False)


def _automation_binding(profile: TurnExecutionProfile) -> tuple[int, AgentRunBudget]:
    """返回已验证的 Run ID 与 Agent budget。"""
    if (
        profile.source != "automation"
        or type(profile.task_run_id) is not int
        or profile.task_run_id <= 0
        or profile.budget is None
    ):
        raise ValueError("automation continuation profile is invalid")
    return profile.task_run_id, profile.budget


def _worker_id(approval_id: int) -> str:
    """为一次 Approval continuation 生成稳定、非秘密的 lease owner。"""
    if type(approval_id) is not int or approval_id <= 0:
        raise ValueError("approval_id must be a positive integer")
    return f"approval-continuation-{approval_id}"


def _preview(text: str, limit: int = 500) -> str:
    """生成 bounded Task Ledger 预览，不影响完整 response_json。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"
