"""Automation Approval continuation 的 durable TaskRun 结算测试。"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.agent.runner import AgentRunBudget
from miniclaw.agent.turn import TurnExecutionProfile, TurnResult
from miniclaw.automation.continuation import TaskApprovalContinuation
from miniclaw.automation.models import (
    DeliveryTarget,
    RunStatus,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskResponse,
)
from miniclaw.automation.repository import ScheduledTaskRepository, TaskRunRepository
from miniclaw.policy.engine import PolicyAction, PolicyDecision
from miniclaw.providers.base import ToolCall
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import OwnerRepository
from miniclaw.storage.tooling import ApprovalRepository
from miniclaw.tools.base import ToolContext


class _DeliveryProbe:
    """记录成功 Run 的主动消息投影次数。"""

    def __init__(self) -> None:
        """创建空投影记录。"""
        self.projected = []

    def project(self, run, response) -> tuple[object, ...]:
        """记录终态 Run/response。"""
        self.projected.append((run, response))
        return ()


class TaskApprovalContinuationTest(unittest.TestCase):
    """验证审批后 TaskRun 不会永久停留在 waiting_approval。"""

    def setUp(self) -> None:
        """创建真实 SQLite Task、Run、Turn 与 Approval 外键。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.database = Database(root / "miniclaw.db")
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.now = datetime(2026, 8, 9, 10, tzinfo=UTC)
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.approvals = ApprovalRepository(self.database, clock=lambda: self.now)
        self.session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "approval-continuation",
        )
        self.turns = TurnRepository(self.database)
        self.turn = self.turns.create_with_user_message(
            self.session.id,
            "approval-continuation-event",
            "test-model",
            "perform a bounded write",
        )
        self.turns.mark_running(self.turn.id)
        self.task = self.tasks.create(
            owner_id=self.owner.id,
            name="approval continuation",
            schedule=ScheduleSpec(
                ScheduleKind.INTERVAL,
                "3600",
                "UTC",
                self.now + timedelta(hours=1),
            ),
            prompt="Perform the approved bounded action.",
            skill_names=(),
            delivery=DeliveryTarget(route="none", channel="none"),
            policy_profile="automation-default",
            budget=TaskBudget(timeout_seconds=60),
        )
        self.run = self.runs.enqueue(self.task, scheduled_for=self.now)
        claimed = self.runs.claim_next("worker-a", now=self.now, lease_seconds=60)
        assert claimed is not None
        self.runs.mark_running(claimed.id, "worker-a", now=self.now)
        self.approval = self._approval("call-first")
        self.runs.mark_waiting(
            claimed.id,
            "worker-a",
            session_id=self.session.id,
            turn_id=self.turn.id,
            approval_id=self.approval.id,
        )
        self.profile = TurnExecutionProfile(
            source="automation",
            task_run_id=claimed.id,
            allowed_tool_names=frozenset({"write_file", "complete_task"}),
            budget=AgentRunBudget(max_turns=3, max_tool_calls=2, timeout_seconds=60),
            automation_gate=lambda: True,
        )
        self.delivery = _DeliveryProbe()
        self.audit: list[tuple[str, dict[str, int | str]]] = []
        self.continuation = TaskApprovalContinuation(
            self.runs,
            delivery=self.delivery,
            clock=lambda: self.now,
            audit=lambda event_type, metadata: self.audit.append((event_type, metadata)),
        )

    def _approval(self, call_id: str):
        """为 fixture Turn 创建一个真实可引用 Approval。"""
        return self.approvals.create_waiting(
            ToolContext(
                user_id=self.owner.id,
                session_id=self.session.id,
                turn_id=self.turn.id,
                state_home=Path(self.temporary_directory.name),
                workspace=Path(self.temporary_directory.name),
                read_only_roots=(),
            ),
            ToolCall(call_id, "write_file", {"path": "status.txt", "content": "ok"}),
            {"path": "status.txt", "content": "ok"},
            PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required"),
            ttl_seconds=600,
            summary="write_file status.txt",
        )

    def _result(
        self,
        *,
        approval_id: int | None = None,
        response: TaskResponse | None = None,
        error_code: str | None = None,
    ) -> TurnResult:
        """构造 continuation 的最小可观察结果。"""
        return TurnResult(
            turn_id=self.turn.id,
            session_id=self.session.id,
            content="",
            input_tokens=4,
            output_tokens=2,
            provider_request_id="req-continuation",
            message_id=None,
            approval_id=approval_id,
            terminal_response=response,
            error_code=error_code,
        )

    def test_success_finishes_run_and_projects_once(self) -> None:
        """审批续跑成功必须结算 succeeded 并只投影一次 terminal response。"""
        response = TaskResponse(True, "已完成批准的操作")

        self.continuation.begin(self.profile, self.approval.id)
        self.continuation.settle(
            self.profile,
            self.approval.id,
            self._result(response=response),
        )

        stored = self.runs.get(self.run.id)
        self.assertEqual(stored.status, RunStatus.SUCCEEDED)
        self.assertEqual(stored.response, response)
        self.assertEqual(len(self.delivery.projected), 1)
        self.assertEqual([name for name, _ in self.audit], ["task_run.terminal"])

    def test_nested_approval_returns_run_to_waiting_with_new_binding(self) -> None:
        """续跑再次触发危险 Tool 时必须换绑新 Approval 且释放 lease。"""
        next_approval = self._approval("call-second")

        self.continuation.begin(self.profile, self.approval.id)
        self.continuation.settle(
            self.profile,
            self.approval.id,
            self._result(approval_id=next_approval.id),
        )

        stored = self.runs.get(self.run.id)
        self.assertEqual(stored.status, RunStatus.WAITING_APPROVAL)
        self.assertEqual(stored.approval_id, next_approval.id)
        self.assertIsNone(stored.worker_id)
        self.assertEqual([name for name, _ in self.audit], ["task_run.waiting_approval"])

    def test_failure_uses_stable_code_without_response_body(self) -> None:
        """Provider/取消异常必须让 Run 离开 running 且仅保存稳定错误码。"""
        self.continuation.begin(self.profile, self.approval.id)
        self.continuation.fail(
            self.profile,
            self.approval.id,
            error_code="provider_server",
            session_id=self.session.id,
            turn_id=self.turn.id,
        )

        stored = self.runs.get(self.run.id)
        self.assertEqual(stored.status, RunStatus.FAILED)
        self.assertEqual(stored.error_code, "provider_server")
        self.assertIsNone(stored.response)


if __name__ == "__main__":
    unittest.main()
