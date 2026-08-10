"""Phase 6 Task/Run Repository 的事务、状态机和恢复测试。"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lobster0.automation.models import (
    DeliveryTarget,
    RunStatus,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskStatus,
)
from lobster0.automation.repository import (
    AutomationControlRepository,
    AutomationDataError,
    AutomationStateError,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations
from lobster0.storage.repositories import OwnerRepository


class AutomationRepositoryTest(unittest.TestCase):
    """验证数据库是 Automation 的唯一运行事实源。"""

    def setUp(self) -> None:
        """创建带 Owner 和 v5 schema 的独立 SQLite。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "lobster0.db"
        self.database = Database(database_path)
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.now = datetime(2026, 8, 9, 8, tzinfo=UTC)
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.control = AutomationControlRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.task = self.tasks.create(
            owner_id=self.owner.id,
            name="hourly report",
            schedule=ScheduleSpec(
                kind=ScheduleKind.INTERVAL,
                expression="3600",
                timezone="Asia/Shanghai",
                next_run_at=self.now,
            ),
            prompt="Summarize reports/status.json",
            skill_names=(),
            delivery=DeliveryTarget(route="none", channel="none"),
            policy_profile="automation-default",
            budget=TaskBudget(),
        )

    def test_same_task_slot_is_enqueued_once(self) -> None:
        """重复 tick 或进程重启不能为同一 task/slot 创建第二行。"""
        first = self.runs.enqueue(self.task, scheduled_for=self.now)
        second = self.runs.enqueue(self.task, scheduled_for=self.now)

        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.runs.list(task_id=self.task.id)), 1)

    def test_two_workers_cannot_claim_the_same_run(self) -> None:
        """claim 必须原子写 worker 与 lease，第二个 Worker 看不到同一 queued Run。"""
        run = self.runs.enqueue(self.task, scheduled_for=self.now)

        first = self.runs.claim_next("worker-a", now=self.now, lease_seconds=60)
        second = TaskRunRepository(self.database).claim_next(
            "worker-b", now=self.now, lease_seconds=60
        )

        self.assertEqual(first.id, run.id)
        self.assertEqual(first.status, RunStatus.CLAIMED)
        self.assertIsNone(second)

    def test_halt_blocks_enqueue_and_claim_until_local_unhalt(self) -> None:
        """E-stop 必须持久化，重建 Repository 后仍阻断新工作。"""
        queued = self.runs.enqueue(self.task, scheduled_for=self.now)
        halted = self.control.halt("incident", now=self.now)

        with self.assertRaisesRegex(AutomationStateError, "automation_halted"):
            self.runs.enqueue(self.task, scheduled_for=self.now + timedelta(hours=1))
        with self.assertRaisesRegex(AutomationStateError, "automation_halted"):
            TaskRunRepository(self.database).claim_next(
                "worker-a", now=self.now, lease_seconds=60
            )
        self.assertTrue(halted.halted)
        resumed = self.control.unhalt(now=self.now + timedelta(minutes=1))
        claimed = self.runs.claim_next(
            "worker-a", now=self.now + timedelta(minutes=1), lease_seconds=60
        )
        self.assertFalse(resumed.halted)
        self.assertGreater(resumed.revision, halted.revision)
        self.assertEqual(claimed.id, queued.id)

    def test_task_transitions_are_optimistic_and_terminal_is_immutable(self) -> None:
        """旧 version、非法恢复和 cancelled 复活均必须失败。"""
        paused = self.tasks.pause(
            self.task.id, owner_id=self.owner.id, expected_version=self.task.version
        )
        with self.assertRaisesRegex(AutomationStateError, "task_version_conflict"):
            self.tasks.resume(
                self.task.id, owner_id=self.owner.id, expected_version=self.task.version
            )
        active = self.tasks.resume(
            paused.id, owner_id=self.owner.id, expected_version=paused.version
        )
        cancelled = self.tasks.cancel(
            active.id, owner_id=self.owner.id, expected_version=active.version
        )
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        with self.assertRaisesRegex(AutomationStateError, "task_terminal"):
            self.tasks.resume(
                cancelled.id,
                owner_id=self.owner.id,
                expected_version=cancelled.version,
            )

    def test_stale_claim_requeues_but_stale_running_is_interrupted(self) -> None:
        """未开始 claim 可重试；已 running 可能有副作用，不能盲目重放。"""
        first = self.runs.enqueue(self.task, scheduled_for=self.now)
        claimed = self.runs.claim_next("worker-a", now=self.now, lease_seconds=10)
        self.assertEqual(claimed.id, first.id)
        recovered = self.runs.recover_stale(now=self.now + timedelta(seconds=11))
        self.assertEqual(recovered.requeued, 1)

        claimed_again = self.runs.claim_next(
            "worker-b", now=self.now + timedelta(seconds=11), lease_seconds=10
        )
        running = self.runs.mark_running(
            claimed_again.id, "worker-b", now=self.now + timedelta(seconds=11)
        )
        self.assertEqual(running.status, RunStatus.RUNNING)
        recovered = self.runs.recover_stale(now=self.now + timedelta(seconds=22))

        self.assertEqual(recovered.interrupted, 1)
        self.assertEqual(self.runs.get(first.id).status, RunStatus.INTERRUPTED)
        self.assertIsNone(
            self.runs.claim_next(
                "worker-c", now=self.now + timedelta(seconds=22), lease_seconds=10
            )
        )

    def test_waiting_approval_survives_recovery_and_terminal_rows_do_not_move(self) -> None:
        """等待 Owner 的 Run 不受 lease 扫描影响，终态也不能回退。"""
        run = self.runs.enqueue(self.task, scheduled_for=self.now)
        claimed = self.runs.claim_next("worker-a", now=self.now, lease_seconds=60)
        running = self.runs.mark_running(claimed.id, "worker-a", now=self.now)
        waiting = self.runs.mark_waiting(
            running.id,
            "worker-a",
            session_id=None,
            turn_id=None,
            approval_id=None,
        )

        self.runs.recover_stale(now=self.now + timedelta(days=1))

        self.assertEqual(self.runs.get(run.id).status, RunStatus.WAITING_APPROVAL)
        with self.assertRaisesRegex(AutomationStateError, "task_run_transition"):
            self.runs.mark_running(waiting.id, "worker-a", now=self.now)

    def test_malformed_persisted_json_is_rejected_without_echo(self) -> None:
        """损坏快照不能被当成空默认值，也不能把正文拼进异常。"""
        run = self.runs.enqueue(self.task, scheduled_for=self.now)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE task_runs SET snapshot_json = ? WHERE id = ?",
                ('{"prompt":"SECRET_SENTINEL"', run.id),
            )

        with self.assertRaises(AutomationDataError) as raised:
            self.runs.get(run.id)

        self.assertNotIn("SECRET_SENTINEL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
