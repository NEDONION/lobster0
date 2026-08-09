"""Phase 6 Scheduler 的幂等入队、有界补做与 lifecycle 测试。"""

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.automation.models import (
    DeliveryTarget,
    RunStatus,
    ScheduledTask,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskStatus,
)
from miniclaw.automation.repository import (
    AutomationControlRepository,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from miniclaw.automation.scheduler import Scheduler
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import OwnerRepository


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    """验证 Scheduler 只创建 durable Run，不执行 Agent 或副作用。"""

    def setUp(self) -> None:
        """创建带一条到期 interval Task 的独立数据库。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database = Database(Path(self.temporary_directory.name) / "miniclaw.db")
        apply_migrations(database)
        owner = OwnerRepository(database).get_or_create()
        self.owner_id = owner.id
        self.now = datetime(2026, 8, 9, 8, tzinfo=UTC)
        self.current_time = self.now
        self.tasks = ScheduledTaskRepository(database, clock=lambda: self.current_time)
        self.runs = TaskRunRepository(database, clock=lambda: self.current_time)
        self.control = AutomationControlRepository(database, clock=lambda: self.current_time)
        self.task = self._create_task("hourly", next_run_at=self.now)
        self.scheduler = Scheduler(
            self.tasks,
            self.runs,
            self.control,
            max_active_tasks=50,
            misfire_grace_seconds=300,
            clock=lambda: self.current_time,
        )

    def _create_task(
        self,
        name: str,
        *,
        next_run_at: datetime,
        expression: str = "3600",
    ) -> ScheduledTask:
        """创建测试使用的静默 interval Task。"""
        return self.tasks.create(
            owner_id=self.owner_id,
            name=name,
            schedule=ScheduleSpec(
                kind=ScheduleKind.INTERVAL,
                expression=expression,
                timezone="UTC",
                next_run_at=next_run_at,
            ),
            prompt=f"private prompt for {name}",
            skill_names=(),
            delivery=DeliveryTarget(route="none", channel="none"),
            policy_profile="automation-default",
            budget=TaskBudget(),
        )

    async def test_two_ticks_enqueue_one_slot(self) -> None:
        """重复 tick 不能为同一 Task slot 创建两个 Run。"""
        first = await self.scheduler.tick(self.now)
        second = await self.scheduler.tick(self.now)

        self.assertEqual(first.enqueued, 1)
        self.assertEqual(second.enqueued, 0)
        self.assertEqual(len(self.runs.list(task_id=self.task.id)), 1)

    async def test_one_year_misfire_creates_at_most_one_catch_up(self) -> None:
        """长时间停机后只补一个 Run，并直接把 interval 推进到未来。"""
        one_year_later = self.now + timedelta(days=365)
        self.current_time = one_year_later

        result = await self.scheduler.tick(one_year_later)

        self.assertEqual(result.enqueued, 1)
        self.assertEqual(result.misfired, 1)
        self.assertEqual(len(self.runs.list(task_id=self.task.id)), 1)
        self.assertGreater(self.tasks.get(self.task.id).schedule.next_run_at, one_year_later)

    async def test_expired_once_is_recorded_failed_without_execution(self) -> None:
        """超过 grace 的 once 要留下失败事实，但不能进入 queued Worker。"""
        once = self.tasks.create(
            owner_id=self.owner_id,
            name="expired-once",
            schedule=ScheduleSpec(
                kind=ScheduleKind.ONCE,
                expression=self.now.isoformat(),
                timezone="UTC",
                next_run_at=self.now,
            ),
            prompt="must not execute",
            skill_names=(),
            delivery=DeliveryTarget(route="none", channel="none"),
            policy_profile="automation-default",
            budget=TaskBudget(),
        )
        late = self.now + timedelta(minutes=10)
        self.current_time = late

        result = await self.scheduler.tick(late)

        run = self.runs.list(task_id=once.id)[0]
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(run.error_code, "schedule_misfire")
        self.assertEqual(self.tasks.get(once.id).status, TaskStatus.COMPLETED)
        self.assertEqual(result.misfired, 2)  # hourly catch-up + expired once

    async def test_halt_blocks_scan_and_enqueue_until_unhalt(self) -> None:
        """Durable E-stop 必须让 tick 在读取 Prompt 前立即停止。"""
        self.control.halt("incident", now=self.now)

        halted = await self.scheduler.tick(self.now)

        self.assertEqual(halted.scanned, 0)
        self.assertEqual(halted.enqueued, 0)
        self.assertTrue(halted.halted)
        self.assertEqual(self.runs.list(task_id=self.task.id), ())
        self.control.unhalt(now=self.now)
        resumed = await self.scheduler.tick(self.now)
        self.assertEqual(resumed.enqueued, 1)

    async def test_two_scheduler_instances_do_not_duplicate_a_slot(self) -> None:
        """共享 SQLite 的两个 Scheduler 即使同时 tick 也只保留一个幂等 Run。"""
        other = Scheduler(
            self.tasks,
            self.runs,
            self.control,
            max_active_tasks=50,
            misfire_grace_seconds=300,
            clock=lambda: self.current_time,
        )

        results = await asyncio.gather(
            self.scheduler.tick(self.now),
            other.tick(self.now),
        )

        self.assertEqual(sum(result.enqueued for result in results), 1)
        self.assertEqual(len(self.runs.list(task_id=self.task.id)), 1)

    async def test_tick_is_bounded_and_reports_persisted_next_wake(self) -> None:
        """单次扫描遵守上限，next wake 来自持久化的最早 active Task。"""
        self._create_task("second", next_run_at=self.now)
        future = self.now + timedelta(minutes=5)
        self._create_task("future", next_run_at=future)
        bounded = Scheduler(
            self.tasks,
            self.runs,
            self.control,
            max_active_tasks=1,
            misfire_grace_seconds=300,
            clock=lambda: self.current_time,
        )

        result = await bounded.tick(self.now)

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.enqueued, 1)
        self.assertEqual(result.next_wake_at, self.now)

    async def test_paused_task_is_not_scanned_and_lifecycle_is_idempotent(self) -> None:
        """Paused Task 不会入队，Scheduler start/stop 可安全重复调用。"""
        self.tasks.pause(
            self.task.id,
            owner_id=self.owner_id,
            expected_version=self.task.version,
        )

        result = await self.scheduler.tick(self.now)

        self.assertEqual(result.scanned, 0)
        self.assertEqual(self.runs.list(task_id=self.task.id), ())
        await self.scheduler.start()
        await self.scheduler.start()
        self.assertTrue(self.scheduler.running)
        await self.scheduler.stop()
        await self.scheduler.stop()
        self.assertFalse(self.scheduler.running)


if __name__ == "__main__":
    unittest.main()
