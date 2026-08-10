"""Phase 6 Automation 不可变模型和构造边界测试。"""

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

from lobster0.automation.models import (
    DeliveryTarget,
    RunStatus,
    ScheduledTask,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskResponse,
    TaskRun,
    TaskStatus,
)


class AutomationModelTest(unittest.TestCase):
    """验证持久化前的类型、时间与状态不变量。"""

    def setUp(self) -> None:
        """固定 aware UTC 时间，避免机器时区参与断言。"""
        self.now = datetime(2026, 8, 9, 8, tzinfo=UTC)
        self.schedule = ScheduleSpec(
            kind=ScheduleKind.INTERVAL,
            expression="3600",
            timezone="Asia/Shanghai",
            next_run_at=self.now,
        )
        self.budget = TaskBudget()
        self.delivery = DeliveryTarget(route="none", channel="none")

    def test_task_budget_rejects_bool_zero_and_cost_expansion(self) -> None:
        """bool、零值和负费用不能穿过 Core 预算边界。"""
        for values in (
            {"timeout_seconds": True},
            {"max_turns": 0},
            {"max_tool_calls": -1},
            {"max_input_tokens": 0},
            {"max_output_tokens": 0},
            {"max_cost_microusd": -1},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    TaskBudget(**values)

    def test_delivery_and_response_contracts_fail_closed(self) -> None:
        """投递 route/channel 组合与静默响应必须是封闭结构。"""
        with self.assertRaisesRegex(ValueError, "delivery"):
            DeliveryTarget(route="origin", channel="none")
        with self.assertRaisesRegex(ValueError, "delivery"):
            DeliveryTarget(route="none", channel="feishu", account_id="default")
        with self.assertRaisesRegex(ValueError, "notify"):
            TaskResponse(notify=False, text="should stay silent")
        self.assertEqual(TaskResponse(notify=False, text="").text, "")

    def test_schedule_and_run_require_aware_utc_and_legal_lease_shape(self) -> None:
        """naive 时间和终态 lease 会破坏跨重启调度，必须在构造时拒绝。"""
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            ScheduleSpec(
                kind=ScheduleKind.ONCE,
                expression="2026-08-10T09:00:00+08:00",
                timezone="Asia/Shanghai",
                next_run_at=datetime(2026, 8, 10, 1),
            )
        with self.assertRaisesRegex(ValueError, "terminal"):
            TaskRun(
                id=1,
                task_id=1,
                scheduled_for=self.now,
                idempotency_key="a" * 64,
                status=RunStatus.SUCCEEDED,
                attempt=1,
                lease_expires_at=self.now,
                created_at=self.now,
            )

    def test_scheduled_task_is_immutable_and_validates_identity(self) -> None:
        """Task 必须绑定正 Owner、非空 prompt/name 和合法版本。"""
        task = ScheduledTask(
            id=1,
            owner_id=1,
            name="daily report",
            schedule=self.schedule,
            prompt="Summarize reports/status.json",
            skill_names=(),
            delivery=self.delivery,
            policy_profile="automation-default",
            budget=self.budget,
            status=TaskStatus.ACTIVE,
            version=1,
            created_at=self.now,
            updated_at=self.now,
        )
        with self.assertRaises(FrozenInstanceError):
            task.name = "changed"  # type: ignore[misc]
        for values in (
            {"owner_id": 0},
            {"name": " "},
            {"prompt": ""},
            {"version": True},
        ):
            with self.subTest(values=values):
                arguments = {
                    "id": 1,
                    "owner_id": 1,
                    "name": "task",
                    "schedule": self.schedule,
                    "prompt": "work",
                    "skill_names": (),
                    "delivery": self.delivery,
                    "policy_profile": "automation-default",
                    "budget": self.budget,
                    "status": TaskStatus.ACTIVE,
                    "version": 1,
                    "created_at": self.now,
                    "updated_at": self.now,
                }
                arguments.update(values)
                with self.assertRaises(ValueError):
                    ScheduledTask(**arguments)


if __name__ == "__main__":
    unittest.main()
