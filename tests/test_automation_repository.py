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


class SubagentRunTest(unittest.TestCase):
    """depth-1 子 Run 的关联与深度约束。"""

    def setUp(self) -> None:
        """准备状态、任务与 Run 仓库。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "lobster0.db")
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.now = datetime(2026, 8, 12, 9, tzinfo=UTC)
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.task = self.tasks.create(
            owner_id=self.owner.id,
            name="父任务",
            prompt="汇总",
            schedule=ScheduleSpec(
                kind=ScheduleKind.INTERVAL,
                expression="3600",
                timezone="UTC",
                next_run_at=self.now,
            ),
            skill_names=(),
            delivery=DeliveryTarget(route="none", channel="none"),
            policy_profile="automation-default",
            budget=TaskBudget(),
        )

    def parent(self) -> object:
        """建一条普通 Run 作为父。"""
        return self.runs.enqueue(
            self.task, scheduled_for=self.now, idempotency_key="parent"
        )

    def test_child_run_records_its_parent_and_subagent(self) -> None:
        """子 Run 复用同一张表，只多两列关联。"""
        parent = self.parent()

        child = self.runs.enqueue_child(
            self.task,
            parent_run_id=parent.id,
            subagent_id="researcher",
            scheduled_for=self.now,
        )

        self.assertEqual(child.parent_run_id, parent.id)
        self.assertEqual(child.subagent_id, "researcher")
        self.assertIsNone(self.runs.get(parent.id).parent_run_id)

    def test_a_child_cannot_have_children(self) -> None:
        """max depth = 1 的持久层防线：子 Run 不能再当父。

        工具层已经通过「子 Agent 拿不到 delegate_task」保证了这一点，这里是
        第二道——绕过工具层直接调仓库也不行。
        """
        parent = self.parent()
        child = self.runs.enqueue_child(
            self.task,
            parent_run_id=parent.id,
            subagent_id="researcher",
            scheduled_for=self.now,
        )

        with self.assertRaises(AutomationStateError) as raised:
            self.runs.enqueue_child(
                self.task,
                parent_run_id=child.id,
                subagent_id="researcher",
                scheduled_for=self.now,
            )

        self.assertEqual(str(raised.exception), "subagent_depth_exceeded")

    def test_children_can_be_listed_for_a_parent(self) -> None:
        """界面要按父 Run 展示参与的子任务。"""
        parent = self.parent()
        for index in range(2):
            self.runs.enqueue_child(
                self.task,
                parent_run_id=parent.id,
                subagent_id="researcher",
                scheduled_for=self.now,
                idempotency_key=f"child-{index}",
            )

        children = self.runs.list_children(parent.id)

        self.assertEqual(len(children), 2)
        self.assertTrue(all(item.parent_run_id == parent.id for item in children))


class SubagentCancellationTest(unittest.TestCase):
    """父 Run 终结时子 Run 不能被留在半路。"""

    def setUp(self) -> None:
        """准备状态、任务与 Run 仓库。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "lobster0.db")
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.now = datetime(2026, 8, 12, 9, tzinfo=UTC)
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.task = self.tasks.create(
            owner_id=self.owner.id,
            name="父任务",
            prompt="汇总",
            schedule=ScheduleSpec(
                kind=ScheduleKind.INTERVAL,
                expression="3600",
                timezone="UTC",
                next_run_at=self.now,
            ),
            skill_names=(),
            delivery=DeliveryTarget(route="none", channel="none"),
            policy_profile="automation-default",
            budget=TaskBudget(),
        )
        self.parent = self.runs.enqueue(
            self.task, scheduled_for=self.now, idempotency_key="parent"
        )

    def child(self, key: str) -> object:
        """建一条子 Run。"""
        return self.runs.enqueue_child(
            self.task,
            parent_run_id=self.parent.id,
            subagent_id="researcher",
            scheduled_for=self.now,
            idempotency_key=key,
        )

    def test_cancelling_a_parent_cancels_its_queued_children(self) -> None:
        """父被取消后，还没开跑的子 Run 不该再被 worker 捡起来。"""
        first = self.child("c-1")
        second = self.child("c-2")

        cancelled = self.runs.cancel_children(self.parent.id, now=self.now)

        self.assertEqual(cancelled, 2)
        self.assertEqual(self.runs.get(first.id).status, RunStatus.CANCELLED)
        self.assertEqual(self.runs.get(second.id).status, RunStatus.CANCELLED)

    def test_a_running_child_is_interrupted_not_silently_cancelled(self) -> None:
        """已经在跑的子 Run 可能已有副作用，只能记为 interrupted。

        直接标 cancelled 会让「它到底做过什么」这个问题永远没有答案。
        """
        child = self.child("c-1")
        # claim_next 取最早的 queued Run；父 Run 也在队列里，先把它领走。
        self.runs.claim_next("w-0", now=self.now, lease_seconds=60)
        claimed = self.runs.claim_next("w-1", now=self.now, lease_seconds=60)
        assert claimed is not None and claimed.id == child.id
        self.runs.mark_running(claimed.id, "w-1", now=self.now)

        self.runs.cancel_children(self.parent.id, now=self.now)

        stored = self.runs.get(child.id)
        self.assertEqual(stored.status, RunStatus.INTERRUPTED)
        self.assertEqual(stored.error_code, "parent_run_cancelled")

    def test_terminal_children_are_left_alone(self) -> None:
        """已经跑完的子 Run 不该被父的取消改写成别的状态。"""
        child = self.child("c-1")
        self.runs.claim_next("w-0", now=self.now, lease_seconds=60)
        claimed = self.runs.claim_next("w-1", now=self.now, lease_seconds=60)
        assert claimed is not None and claimed.id == child.id
        running = self.runs.mark_running(claimed.id, "w-1", now=self.now)
        self.runs.finish(
            running.id,
            status=RunStatus.SUCCEEDED,
            now=self.now,
            worker_id="w-1",
            result_preview="done",
        )

        self.runs.cancel_children(self.parent.id, now=self.now)

        self.assertEqual(self.runs.get(child.id).status, RunStatus.SUCCEEDED)

    def test_finishing_a_parent_cleans_up_its_children(self) -> None:
        """清理必须挂在 finish 上，而不是靠调用方记得多调一次。

        finish 是所有终态的唯一入口；把清理放在调用方，早晚会有一条路径忘记调，
        留下永远跑不完的子 Run。
        """
        child = self.child("c-1")
        self.runs.claim_next("w-0", now=self.now, lease_seconds=60)
        running = self.runs.mark_running(self.parent.id, "w-0", now=self.now)

        self.runs.finish(
            running.id,
            status=RunStatus.CANCELLED,
            now=self.now,
            worker_id="w-0",
        )

        self.assertEqual(self.runs.get(child.id).status, RunStatus.CANCELLED)

    def test_finishing_a_child_does_not_recurse(self) -> None:
        """子 Run 没有自己的子，收尾时不该多打一次无用查询。"""
        child = self.child("c-1")
        self.runs.claim_next("w-0", now=self.now, lease_seconds=60)
        claimed = self.runs.claim_next("w-1", now=self.now, lease_seconds=60)
        assert claimed is not None and claimed.id == child.id
        running = self.runs.mark_running(claimed.id, "w-1", now=self.now)

        finished = self.runs.finish(
            running.id, status=RunStatus.SUCCEEDED, now=self.now, worker_id="w-1"
        )

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)

    def test_cancelling_leaves_no_child_in_a_live_state(self) -> None:
        """核心不变量：父终结后不允许有子 Run 还处于 queued/claimed/running。"""
        self.child("c-1")
        second = self.child("c-2")
        self.runs.claim_next("w-0", now=self.now, lease_seconds=60)
        self.runs.claim_next("w-1", now=self.now, lease_seconds=60)
        claimed = self.runs.claim_next("w-2", now=self.now, lease_seconds=60)
        assert claimed is not None and claimed.id == second.id
        self.runs.mark_running(claimed.id, "w-2", now=self.now)

        self.runs.cancel_children(self.parent.id, now=self.now)

        live = {RunStatus.QUEUED, RunStatus.CLAIMED, RunStatus.RUNNING}
        self.assertFalse(
            [item for item in self.runs.list_children(self.parent.id) if item.status in live]
        )


if __name__ == "__main__":
    unittest.main()
