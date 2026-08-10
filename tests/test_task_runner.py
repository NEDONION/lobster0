"""Durable TaskRunner 的 claim、隔离、预算与终态测试。"""

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from lobster0.agent.turn import TurnExecutionProfile, TurnResult
from lobster0.automation.models import (
    DeliveryTarget,
    RunStatus,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskResponse,
    TaskRun,
)
from lobster0.automation.repository import (
    AutomationControlRepository,
    AutomationStateError,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from lobster0.automation.runner import TaskDeliveryProjector, TaskRunner
from lobster0.policy.engine import PolicyAction, PolicyDecision
from lobster0.providers.base import ProviderServerError, ToolCall
from lobster0.storage.conversations import SessionRepository, TurnRepository
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations
from lobster0.storage.repositories import OwnerRepository
from lobster0.storage.tooling import ApprovalRepository
from lobster0.tools.base import ToolContext


class _FakeAutomationTurns:
    """按顺序返回结果、异常或永久等待，并记录 execution profile。"""

    def __init__(self, outcomes: list[TurnResult | BaseException | None]) -> None:
        """保存有限 outcome；None 表示等待取消。"""
        self.outcomes = outcomes
        self.calls: list[tuple[int, int, str, TurnExecutionProfile]] = []

    async def handle_automation(
        self,
        *,
        task_id: int,
        task_run_id: int,
        text: str,
        profile: TurnExecutionProfile,
    ) -> TurnResult:
        """记录不透明 Prompt 长度，并产生测试指定结果。"""
        self.calls.append((task_id, task_run_id, text, profile))
        outcome = self.outcomes.pop(0)
        if outcome is None:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeDeliveryProjector:
    """记录 terminal projection 与启动恢复调用。"""

    def __init__(self) -> None:
        """创建空投影记录。"""
        self.projected = []
        self.projected_approvals = []
        self.recoveries = 0

    def project(self, run: TaskRun, response: TaskResponse) -> tuple[object, ...]:
        """记录完整终态对象，不制造真实 Outbox。"""
        self.projected.append((run, response))
        return ()

    def project_approval(self, run: TaskRun, approval_id: int) -> tuple[object, ...]:
        """记录等待审批 Run 与绑定审批编号。"""
        self.projected_approvals.append((run, approval_id))
        return ()

    def recover(self) -> int:
        """记录一次启动补投影。"""
        self.recoveries += 1
        return 0


def _turn_result(
    *,
    session_id: int,
    turn_id: int,
    terminal: TaskResponse | None = None,
    approval_id: int | None = None,
    error_code: str | None = None,
) -> TurnResult:
    """构造 TaskRunner 不依赖 Channel 的最小 TurnResult。"""
    return TurnResult(
        turn_id=turn_id,
        session_id=session_id,
        content="" if terminal is None else terminal.text,
        input_tokens=10,
        output_tokens=3,
        provider_request_id="req_task",
        message_id=None,
        approval_id=approval_id,
        terminal_response=terminal,
        error_code=error_code,
    )


class TaskRunnerTest(unittest.IsolatedAsyncioTestCase):
    """验证后台 Worker 永远从 SQLite claim，而不是内存 Task 列表。"""

    def setUp(self) -> None:
        """创建一条已入队 TaskRun 和固定时钟。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "lobster0.db")
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.now = datetime(2026, 8, 9, 8, tzinfo=UTC)
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.control = AutomationControlRepository(self.database, clock=lambda: self.now)
        self.session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "task-runner-fixture",
        )
        self.turns = TurnRepository(self.database)
        self.turn = self.turns.create_with_user_message(
            self.session.id,
            "task-runner-event",
            "test-model",
            "run the task",
        )
        self.turns.mark_running(self.turn.id)
        self.task = self.tasks.create(
            owner_id=self.owner.id,
            name="task runner test",
            schedule=ScheduleSpec(
                ScheduleKind.INTERVAL,
                "3600",
                "UTC",
                self.now + timedelta(hours=1),
            ),
            prompt="Read reports/status.json and summarize it.",
            skill_names=(),
            delivery=DeliveryTarget(route="none", channel="none"),
            policy_profile="automation-default",
            budget=TaskBudget(timeout_seconds=1),
        )
        self.runs.enqueue(self.task, scheduled_for=self.now)

    def _result(
        self,
        *,
        terminal: TaskResponse | None = None,
        approval_id: int | None = None,
        error_code: str | None = None,
    ) -> TurnResult:
        """使用数据库中真实存在的 Session/Turn 外键构造结果。"""
        return _turn_result(
            session_id=self.session.id,
            turn_id=self.turn.id,
            terminal=terminal,
            approval_id=approval_id,
            error_code=error_code,
        )

    def _runner(
        self,
        turns: _FakeAutomationTurns,
        *,
        lease_seconds: int = 10,
        delivery: TaskDeliveryProjector | None = None,
        audit: list[tuple[str, dict[str, int | str]]] | None = None,
    ) -> TaskRunner:
        """创建只开放 read_file 与 terminal Tool 的单 Worker。"""
        return TaskRunner(
            self.runs,
            self.control,
            turns,
            allowed_tool_names=frozenset(
                {"read_file", "complete_task", "manage_task"}
            ),
            lease_seconds=lease_seconds,
            delivery=delivery,
            audit=(
                (lambda event_type, metadata: audit.append((event_type, metadata)))
                if audit is not None
                else None
            ),
        )

    async def test_success_uses_snapshot_profile_and_filters_manage_task(self) -> None:
        """成功必须依赖 Run snapshot，并把递归 control Tool 从 profile 移除。"""
        turns = _FakeAutomationTurns(
            [self._result(terminal=TaskResponse(True, "完成"))]
        )

        attempt = await self._runner(turns).run_once("worker-a", self.now)

        self.assertEqual(attempt.status, RunStatus.SUCCEEDED)
        self.assertEqual(self.runs.get(attempt.run_id).response.text, "完成")
        profile = turns.calls[0][3]
        self.assertEqual(profile.source, "automation")
        self.assertIn("complete_task", profile.allowed_tool_names)
        self.assertNotIn("manage_task", profile.allowed_tool_names)
        self.assertEqual(profile.budget.max_turns, self.task.budget.max_turns)

    async def test_lifecycle_audit_contains_only_ids_codes_and_status(self) -> None:
        """claimed/terminal audit 不能复制 Task prompt 或完整 completion。"""
        audit: list[tuple[str, dict[str, int | str]]] = []
        response = TaskResponse(True, "PRIVATE_COMPLETION_BODY")

        await self._runner(
            _FakeAutomationTurns([self._result(terminal=response)]),
            audit=audit,
        ).run_once("worker-a", self.now)

        self.assertEqual(
            [event_type for event_type, _ in audit],
            ["task_run.claimed", "task_run.terminal"],
        )
        serialized = repr(audit)
        self.assertNotIn(self.task.prompt, serialized)
        self.assertNotIn(response.text, serialized)

    async def test_success_projects_terminal_response_and_recovery_is_idempotent(self) -> None:
        """成功结算后才投影；启动恢复会要求同一 projector 补齐崩溃窗口。"""
        response = TaskResponse(True, "主动通知")
        delivery = _FakeDeliveryProjector()
        runner = self._runner(
            _FakeAutomationTurns([self._result(terminal=response)]),
            delivery=delivery,
        )

        attempt = await runner.run_once("worker-a", self.now)
        runner.recover_startup(now=self.now)

        self.assertEqual(attempt.status, RunStatus.SUCCEEDED)
        self.assertEqual(delivery.projected[0][1], response)
        self.assertEqual(delivery.projected[0][0].status, RunStatus.SUCCEEDED)
        self.assertEqual(delivery.recoveries, 1)

    async def test_each_run_uses_a_fresh_non_user_session_key(self) -> None:
        """同一 Task 的两个 Run 也不能共享临时对话历史。"""
        later = self.now + timedelta(minutes=1)
        self.runs.enqueue(
            self.task,
            scheduled_for=later,
            idempotency_key="manual:second",
        )
        turns = _FakeAutomationTurns(
            [
                self._result(terminal=TaskResponse(False, "")),
                self._result(terminal=TaskResponse(False, "")),
            ]
        )
        runner = self._runner(turns)

        first = await runner.run_once("worker-a", self.now)
        second = await runner.run_once("worker-a", later)

        self.assertNotEqual(first.session_key, second.session_key)
        self.assertTrue(first.session_key.startswith("automation/local/task:"))

    async def test_missing_terminal_and_provider_error_fail_with_stable_codes(self) -> None:
        """普通模型文本不能冒充 terminal result，Provider 正文也不能进入错误码。"""
        missing = await self._runner(
            _FakeAutomationTurns([self._result()])
        ).run_once("worker-a", self.now)
        self.runs.enqueue(
            self.task,
            scheduled_for=self.now + timedelta(minutes=1),
            idempotency_key="manual:provider",
        )
        failed = await self._runner(
            _FakeAutomationTurns([ProviderServerError("SECRET_PROVIDER_BODY")])
        ).run_once("worker-a", self.now + timedelta(minutes=1))

        self.assertEqual(missing.status, RunStatus.FAILED)
        self.assertEqual(missing.error_code, "automation_terminal_response_missing")
        self.assertEqual(failed.error_code, "provider_server")
        self.assertNotIn("SECRET_PROVIDER_BODY", failed.error_code)

    async def test_waiting_approval_releases_lease_and_keeps_turn_links(self) -> None:
        """等待 Owner 时 Run 不占 Worker lease，并保存恢复所需三个 ID。"""
        approval = ApprovalRepository(self.database, clock=lambda: self.now).create_waiting(
            ToolContext(
                user_id=self.owner.id,
                session_id=self.session.id,
                turn_id=self.turn.id,
                state_home=Path(self.temporary_directory.name),
                workspace=Path(self.temporary_directory.name),
                read_only_roots=(),
            ),
            ToolCall("call_read", "read_file", {"path": "status.txt"}),
            {"path": "status.txt"},
            PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required"),
            ttl_seconds=600,
            summary="read_file status.txt",
        )
        delivery = _FakeDeliveryProjector()
        attempt = await self._runner(
            _FakeAutomationTurns([self._result(approval_id=approval.id)]),
            delivery=delivery,
        ).run_once("worker-a", self.now)

        stored = self.runs.get(attempt.run_id)
        self.assertEqual(stored.status, RunStatus.WAITING_APPROVAL)
        self.assertEqual(
            (stored.session_id, stored.turn_id, stored.approval_id),
            (self.session.id, self.turn.id, approval.id),
        )
        self.assertIsNone(stored.worker_id)
        self.assertIsNone(stored.lease_expires_at)
        self.assertEqual(delivery.projected_approvals, [(stored, approval.id)])

    async def test_waiting_run_resumes_only_with_bound_approval_id(self) -> None:
        """审批 continuation 只能用当前 Run 绑定的 Approval 重新取得 lease。"""
        approval = ApprovalRepository(self.database, clock=lambda: self.now).create_waiting(
            ToolContext(
                user_id=self.owner.id,
                session_id=self.session.id,
                turn_id=self.turn.id,
                state_home=Path(self.temporary_directory.name),
                workspace=Path(self.temporary_directory.name),
                read_only_roots=(),
            ),
            ToolCall("call_resume", "read_file", {"path": "status.txt"}),
            {"path": "status.txt"},
            PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required"),
            ttl_seconds=600,
            summary="read_file status.txt",
        )
        waiting = await self._runner(
            _FakeAutomationTurns([self._result(approval_id=approval.id)])
        ).run_once("worker-a", self.now)

        with self.assertRaisesRegex(AutomationStateError, "task_run_transition"):
            self.runs.resume_waiting(
                waiting.run_id,
                approval.id + 1,
                worker_id="approval-continuation",
                now=self.now,
                lease_seconds=10,
            )
        resumed = self.runs.resume_waiting(
            waiting.run_id,
            approval.id,
            worker_id="approval-continuation",
            now=self.now,
            lease_seconds=10,
        )

        self.assertEqual(resumed.status, RunStatus.RUNNING)
        self.assertEqual(resumed.worker_id, "approval-continuation")
        self.assertIsNotNone(resumed.lease_expires_at)

    async def test_timeout_marks_terminal_and_leaves_no_renewal_task(self) -> None:
        """wall-clock timeout 必须结算 timed_out，不能遗留 lease coroutine。"""
        attempt = await self._runner(
            _FakeAutomationTurns([None])
        ).run_once("worker-a", self.now)

        self.assertEqual(attempt.status, RunStatus.TIMED_OUT)
        self.assertEqual(attempt.error_code, "task_timeout")
        current = asyncio.current_task()
        leaked = [
            task
            for task in asyncio.all_tasks()
            if task is not current and "lease-renew" in task.get_name()
        ]
        self.assertEqual(leaked, [])

    async def test_startup_recovery_distinguishes_claimed_and_running(self) -> None:
        """启动恢复只重排未开始 claim，running 必须 interrupted。"""
        first = self.runs.claim_next("old-a", now=self.now, lease_seconds=10)
        assert first is not None
        self.runs.enqueue(
            self.task,
            scheduled_for=self.now + timedelta(minutes=1),
            idempotency_key="manual:running",
        )
        second = self.runs.claim_next("old-b", now=self.now, lease_seconds=10)
        assert second is not None
        self.runs.mark_running(second.id, "old-b", now=self.now)
        runner = self._runner(_FakeAutomationTurns([]))

        recovered = runner.recover_startup(now=self.now + timedelta(seconds=11))

        self.assertEqual((recovered.requeued, recovered.interrupted), (1, 1))

    async def test_worker_loop_survives_unexpected_repository_failure(self) -> None:
        """单次 claim 异常只能进入退避，不能永久杀死后台 Worker。"""
        runner = self._runner(_FakeAutomationTurns([]))
        original = runner.run_once
        calls = 0

        async def flaky_run_once(worker_id: str, now: datetime):
            """第一次模拟 SQLite 故障，之后回到空队列。"""
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("repository unavailable")
            return await original(worker_id, now)

        with mock.patch.object(runner, "run_once", side_effect=flaky_run_once):
            await runner.start()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertTrue(runner.running)
            await runner.stop()


if __name__ == "__main__":
    unittest.main()
