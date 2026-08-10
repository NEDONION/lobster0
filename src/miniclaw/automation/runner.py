"""从 durable TaskRun claim 工作并在隔离 Turn 中有界执行。"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from miniclaw.agent.runner import AgentRunBudget
from miniclaw.agent.turn import TurnExecutionProfile, TurnResult
from miniclaw.automation.models import RunStatus, TaskResponse, TaskRun, TaskRunSnapshot
from miniclaw.automation.repository import (
    AutomationControlRepository,
    AutomationStateError,
    RecoveryResult,
    TaskRunRepository,
)
from miniclaw.providers.base import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)

_LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """返回 Runner 默认使用的 aware UTC 当前时间。"""
    return datetime.now(UTC)


class AutomationTurnHandler(Protocol):
    """收窄 TaskRunner 对无 Channel TurnService 的依赖。"""

    async def handle_automation(
        self,
        *,
        task_id: int,
        task_run_id: int,
        text: str,
        profile: TurnExecutionProfile,
    ) -> TurnResult:
        """执行一次 fresh automation Session 并返回终态或 Approval。"""
        ...


class TaskDeliveryProjector(Protocol):
    """收窄 Runner 对 durable Channel 投影服务的依赖。"""

    def project(self, run: TaskRun, response: TaskResponse) -> tuple[object, ...]:
        """幂等投影成功响应。"""
        ...

    def project_approval(self, run: TaskRun, approval_id: int) -> tuple[object, ...]:
        """幂等投影等待中的审批提示。"""
        ...

    def recover(self) -> int:
        """补投影崩溃窗口中的既有成功 Run。"""
        ...


@dataclass(frozen=True, slots=True)
class TaskRunAttempt:
    """保存一次 Worker 可观察但不含 Prompt/平台 ID 的执行摘要。"""

    run_id: int
    task_id: int
    status: RunStatus
    session_key: str
    session_id: int | None = None
    turn_id: int | None = None
    approval_id: int | None = None
    error_code: str | None = None


class TaskRunner:
    """以 lease、timeout 与 terminal Tool 驱动 durable TaskRun 状态机。"""

    def __init__(
        self,
        runs: TaskRunRepository,
        control: AutomationControlRepository,
        turns: AutomationTurnHandler,
        *,
        allowed_tool_names: frozenset[str],
        lease_seconds: int,
        max_concurrent_runs: int = 1,
        clock: Callable[[], datetime] | None = None,
        delivery: TaskDeliveryProjector | None = None,
        audit: Callable[[str, dict[str, int | str]], None] | None = None,
    ) -> None:
        """绑定状态仓储、Turn handler、Tool allowlist、lease 与 Worker 上限。"""
        if type(lease_seconds) is not int or lease_seconds < 10:
            raise ValueError("lease_seconds must be at least 10")
        if type(max_concurrent_runs) is not int or not 1 <= max_concurrent_runs <= 16:
            raise ValueError("max_concurrent_runs must be between 1 and 16")
        if not isinstance(allowed_tool_names, frozenset) or any(
            not isinstance(name, str) or not name for name in allowed_tool_names
        ):
            raise ValueError("allowed_tool_names must be a string frozenset")
        self._runs = runs
        self._control = control
        self._turns = turns
        self._allowed_tool_names = frozenset(
            {*allowed_tool_names, "complete_task"} - {"manage_task"}
        )
        self._lease_seconds = lease_seconds
        self._max_concurrent_runs = max_concurrent_runs
        self._clock = clock or _utc_now
        self._delivery = delivery
        self._audit_callback = audit
        self._wake_event = asyncio.Event()
        self._stopping = False
        self._workers: tuple[asyncio.Task[None], ...] = ()

    @property
    def running(self) -> bool:
        """返回至少一个 Worker loop 是否仍在运行。"""
        return any(not worker.done() for worker in self._workers)

    def recover_startup(self, *, now: datetime) -> RecoveryResult:
        """启动前恢复过期 lease，绝不自动重放可能已有副作用的 running Run。"""
        recovered = self._runs.recover_stale(now=now)
        if self._delivery is not None:
            self._delivery.recover()
        return recovered

    async def run_once(self, worker_id: str, now: datetime) -> TaskRunAttempt | None:
        """claim 并执行最多一个 queued Run，所有分支都结算或转 waiting。"""
        current = _as_utc(now)
        try:
            claimed = self._runs.claim_next(
                worker_id,
                now=current,
                lease_seconds=self._lease_seconds,
            )
        except AutomationStateError as error:
            if str(error) == "automation_halted":
                return None
            raise
        if claimed is None:
            return None
        self._audit(
            "task_run.claimed",
            {"run_id": claimed.id, "task_id": claimed.task_id, "attempt": claimed.attempt},
        )
        snapshot = claimed.snapshot
        if snapshot is None:
            return self._finish_failure(
                claimed.id,
                claimed.task_id,
                worker_id,
                "task_snapshot_missing",
            )
        session_key = _session_key(claimed.task_id, claimed.id)
        try:
            self._runs.mark_running(claimed.id, worker_id, now=current)
        except AutomationStateError as error:
            code = "automation_halted" if str(error) == "automation_halted" else "task_start_failed"
            return self._finish_failure(
                claimed.id,
                claimed.task_id,
                worker_id,
                code,
                status=RunStatus.INTERRUPTED,
            )

        renewal = asyncio.create_task(
            self._renew_lease(claimed.id, worker_id),
            name=f"miniclaw-task-{claimed.id}-lease-renew",
        )
        try:
            profile = TurnExecutionProfile(
                source="automation",
                task_run_id=claimed.id,
                allowed_tool_names=self._allowed_tool_names,
                budget=_agent_budget(snapshot),
                automation_gate=self._automation_allowed,
            )
            async with asyncio.timeout(snapshot.budget.timeout_seconds):
                result = await self._turns.handle_automation(
                    task_id=claimed.task_id,
                    task_run_id=claimed.id,
                    text=snapshot.prompt,
                    profile=profile,
                )
        except TimeoutError:
            return self._finish_failure(
                claimed.id,
                claimed.task_id,
                worker_id,
                "task_timeout",
                status=RunStatus.TIMED_OUT,
            )
        except asyncio.CancelledError:
            self._finish_failure(
                claimed.id,
                claimed.task_id,
                worker_id,
                "task_cancelled",
                status=RunStatus.INTERRUPTED,
            )
            raise
        except ProviderError as error:
            return self._finish_failure(
                claimed.id,
                claimed.task_id,
                worker_id,
                _provider_error_code(error),
            )
        except Exception:  # noqa: BLE001 - Turn 边界必须脱敏为稳定码
            return self._finish_failure(
                claimed.id,
                claimed.task_id,
                worker_id,
                "task_execution_failed",
            )
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal

        if result.approval_id is not None:
            waiting = self._runs.mark_waiting(
                claimed.id,
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
            if self._delivery is not None:
                try:
                    self._delivery.project_approval(waiting, result.approval_id)
                except Exception:  # noqa: BLE001 - Run 已持久化，Outbox 由 recovery 补投影
                    _LOGGER.warning("task_approval_projection_failed", exc_info=False)
            return TaskRunAttempt(
                waiting.id,
                waiting.task_id,
                waiting.status,
                session_key,
                session_id=result.session_id,
                turn_id=result.turn_id,
                approval_id=result.approval_id,
            )
        if result.error_code is not None:
            return self._finish_failure(
                claimed.id,
                claimed.task_id,
                worker_id,
                result.error_code,
                session_id=result.session_id,
                turn_id=result.turn_id,
            )
        if result.terminal_response is None:
            return self._finish_failure(
                claimed.id,
                claimed.task_id,
                worker_id,
                "automation_terminal_response_missing",
                session_id=result.session_id,
                turn_id=result.turn_id,
            )
        completed = self._runs.finish(
            claimed.id,
            status=RunStatus.SUCCEEDED,
            now=self._now(),
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
            except Exception:  # noqa: BLE001 - Run 已终态，Outbox 由 recovery 补投影
                _LOGGER.warning("task_delivery_projection_failed", exc_info=False)
        self._audit(
            "task_run.terminal",
            {"run_id": completed.id, "task_id": completed.task_id, "status": "succeeded"},
        )
        return TaskRunAttempt(
            completed.id,
            completed.task_id,
            completed.status,
            session_key,
            session_id=result.session_id,
            turn_id=result.turn_id,
        )

    async def start(self) -> None:
        """幂等启动固定数量 Worker loop。"""
        if self.running:
            return
        self._stopping = False
        self._workers = tuple(
            asyncio.create_task(
                self._worker_loop(f"automation-worker-{index + 1}"),
                name=f"miniclaw-automation-worker-{index + 1}",
            )
            for index in range(self._max_concurrent_runs)
        )

    async def stop(self) -> None:
        """停止 claim，取消正在执行的 Turn，并等待全部 lease 清理。"""
        if not self._workers:
            return
        self._stopping = True
        self._wake_event.set()
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            with suppress(asyncio.CancelledError):
                await worker
        self._workers = ()

    def wake(self) -> None:
        """通知空闲 Worker 立即重查 queued Run。"""
        self._wake_event.set()

    async def _worker_loop(self, worker_id: str) -> None:
        """串行 claim Run；空队列用 Event 等待且不阻塞事件循环。"""
        while not self._stopping:
            try:
                attempt = await self.run_once(worker_id, self._now())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单次 Repository 故障不能杀死常驻 Worker
                _LOGGER.warning("automation_worker_iteration_failed", exc_info=False)
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            if attempt is not None:
                continue
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
            except TimeoutError:
                continue

    async def _renew_lease(self, run_id: int, worker_id: str) -> None:
        """执行期间按半个 lease 周期续租，取消时立即退出。"""
        interval = self._lease_seconds / 2
        while True:
            await asyncio.sleep(interval)
            self._runs.renew_lease(
                run_id,
                worker_id,
                now=self._now(),
                lease_seconds=self._lease_seconds,
            )

    def _finish_failure(
        self,
        run_id: int,
        task_id: int,
        worker_id: str,
        error_code: str,
        *,
        status: RunStatus = RunStatus.FAILED,
        session_id: int | None = None,
        turn_id: int | None = None,
    ) -> TaskRunAttempt:
        """用稳定码结算 running/claimed Run，并返回脱敏 Attempt。"""
        completed = self._runs.finish(
            run_id,
            status=status,
            now=self._now(),
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
                "task_id": task_id,
                "status": completed.status.value,
            },
        )
        return TaskRunAttempt(
            completed.id,
            task_id,
            completed.status,
            _session_key(task_id, run_id),
            session_id=session_id,
            turn_id=turn_id,
            error_code=error_code,
        )

    def _audit(self, event_type: str, metadata: dict[str, int | str]) -> None:
        """best-effort 发出只含 ID/code/count 的结构化 lifecycle audit。"""
        if self._audit_callback is None:
            return
        try:
            self._audit_callback(event_type, metadata)
        except Exception:  # noqa: BLE001 - Audit 不改变已经持久化的 Run 状态
            _LOGGER.warning("automation_audit_failed", exc_info=False)

    def _automation_allowed(self) -> bool:
        """每次 Tool 执行前读取 durable E-stop。"""
        return not self._control.status().halted

    def _now(self) -> datetime:
        """读取并规范化注入时钟。"""
        return _as_utc(self._clock())


def _agent_budget(snapshot: TaskRunSnapshot) -> AgentRunBudget:
    """把冻结 TaskBudget 收窄为 AgentRunner 能执行的预算。"""
    budget = snapshot.budget
    return AgentRunBudget(
        max_turns=budget.max_turns,
        max_tool_calls=budget.max_tool_calls,
        timeout_seconds=budget.timeout_seconds,
        max_input_tokens=budget.max_input_tokens,
        max_output_tokens=budget.max_output_tokens,
        max_cost_microusd=budget.max_cost_microusd,
    )


def _provider_error_code(error: ProviderError) -> str:
    """把 Provider 类型映射为不含远端正文的稳定码。"""
    mappings = (
        (ProviderAuthenticationError, "provider_authentication"),
        (ProviderRateLimitError, "provider_rate_limit"),
        (ProviderTimeoutError, "provider_timeout"),
        (ProviderProtocolError, "provider_protocol"),
        (ProviderServerError, "provider_server"),
    )
    for error_type, code in mappings:
        if isinstance(error, error_type):
            return code
    return "provider_failure"


def _session_key(task_id: int, run_id: int) -> str:
    """返回日志安全且每 Run 唯一的 automation Session key。"""
    return f"automation/local/task:{task_id}:run:{run_id}"


def _preview(text: str) -> str:
    """生成有限结果预览，不用于投递或恢复。"""
    return text[:500]


def _as_utc(value: datetime) -> datetime:
    """要求 aware datetime 并规范化为 UTC。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("task runner time must be timezone-aware")
    return value.astimezone(UTC)
