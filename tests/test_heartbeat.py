"""System-owned Heartbeat reconcile、active hours 与 busy delay 测试。"""

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.automation.heartbeat import HeartbeatReconciler
from miniclaw.automation.models import (
    DeliveryTarget,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskStatus,
)
from miniclaw.automation.repository import (
    AutomationStateError,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from miniclaw.config import HeartbeatConfig
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import OwnerRepository


class HeartbeatReconcilerTest(unittest.TestCase):
    """验证 Heartbeat 只是唯一 system Task，不创建第二套 Scheduler。"""

    def setUp(self) -> None:
        """创建固定 UTC 数据库和默认上海活跃时段配置。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "miniclaw.db")
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.now = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)  # 上海 10:00
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.config = HeartbeatConfig(
            enabled=True,
            interval_seconds=1800,
            timezone="Asia/Shanghai",
            active_hours_start="08:00",
            active_hours_end="23:00",
        )

    def test_enabled_config_reconciles_one_system_owned_heartbeat(self) -> None:
        """重复 reconcile 复用固定 system key，并立即入队一次到期检查。"""
        reconciler = self._reconciler()

        first = reconciler.reconcile(self.now)
        second = reconciler.reconcile(self.now)

        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(self.tasks.count_system_owned("heartbeat"), 1)
        self.assertEqual((first.enqueued, second.enqueued), (1, 0))
        task = self.tasks.get(first.task_id, owner_id=self.owner.id)
        self.assertEqual(task.system_key, "system:heartbeat:v1")
        self.assertIn("complete_task", task.prompt)
        self.assertNotIn("HEARTBEAT_OK", task.prompt)

    def test_outside_active_hours_advances_without_provider_run(self) -> None:
        """上海凌晨不创建 Run，并把 next_run_at 推进到当天 08:00。"""
        outside = datetime(2026, 8, 8, 17, 0, tzinfo=UTC)  # 上海 01:00
        reconciler = self._reconciler()

        result = reconciler.reconcile(outside)

        self.assertEqual(result.enqueued, 0)
        self.assertEqual(result.next_run_at, datetime(2026, 8, 9, 0, 0, tzinfo=UTC))
        self.assertGreater(result.next_run_at, outside)
        self.assertEqual(len(self.runs.list_succeeded()), 0)

    def test_busy_capacity_delays_due_heartbeat_without_claiming(self) -> None:
        """普通 Run 占满全局并发时 Heartbeat 延后一分钟且不排队。"""
        normal = self.tasks.create(
            owner_id=self.owner.id,
            name="busy",
            schedule=ScheduleSpec(
                ScheduleKind.INTERVAL,
                "3600",
                "UTC",
                self.now + timedelta(hours=1),
            ),
            prompt="normal",
            skill_names=(),
            delivery=DeliveryTarget("none", "none"),
            policy_profile="automation-default",
            budget=TaskBudget(),
        )
        self.runs.enqueue(normal, scheduled_for=self.now, idempotency_key="busy-run")
        claimed = self.runs.claim_next("busy-worker", now=self.now, lease_seconds=60)
        assert claimed is not None
        self.runs.mark_running(claimed.id, "busy-worker", now=self.now)

        result = self._reconciler(max_concurrent_runs=1).reconcile(self.now)

        self.assertEqual(result.enqueued, 0)
        self.assertEqual(result.next_run_at, self.now + timedelta(seconds=60))

    def test_disabled_config_pauses_history_and_user_cannot_mutate_system_task(self) -> None:
        """关闭只 pause 保留历史；通用用户 mutation API 必须拒绝 system-owned Task。"""
        enabled = self._reconciler().reconcile(self.now)
        disabled = self._reconciler(config=replace(self.config, enabled=False)).reconcile(
            self.now
        )
        task = self.tasks.get(enabled.task_id, owner_id=self.owner.id)

        self.assertEqual(disabled.task_id, enabled.task_id)
        self.assertEqual(task.status, TaskStatus.PAUSED)
        self.assertIsNone(task.schedule.next_run_at)
        with self.assertRaisesRegex(AutomationStateError, "system_task_immutable"):
            self.tasks.resume(
                task.id,
                owner_id=self.owner.id,
                expected_version=task.version,
            )

    def test_dst_gap_active_start_normalizes_to_first_valid_local_time(self) -> None:
        """纽约春季 02:30 不存在时，next_run 必须单调落在 03:30 EDT。"""
        config = HeartbeatConfig(
            enabled=True,
            interval_seconds=1800,
            timezone="America/New_York",
            active_hours_start="02:30",
            active_hours_end="04:00",
        )
        before_gap = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)  # 01:00 EST

        result = self._reconciler(config=config).reconcile(before_gap)

        self.assertEqual(result.enqueued, 0)
        self.assertEqual(result.next_run_at, datetime(2026, 3, 8, 7, 30, tzinfo=UTC))

    def _reconciler(
        self,
        *,
        config: HeartbeatConfig | None = None,
        max_concurrent_runs: int = 2,
    ) -> HeartbeatReconciler:
        """构造无主动外发目标的 Reconciler。"""
        return HeartbeatReconciler(
            config or self.config,
            owner_id=self.owner.id,
            tasks=self.tasks,
            runs=self.runs,
            max_concurrent_runs=max_concurrent_runs,
            delivery=DeliveryTarget("none", "none"),
        )


if __name__ == "__main__":
    unittest.main()
