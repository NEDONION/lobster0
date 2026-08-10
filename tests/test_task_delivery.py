"""Automation terminal response 到 durable Channel Outbox 的投影测试。"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lobster0.automation.delivery import TaskDeliveryService
from lobster0.automation.models import (
    DeliveryTarget,
    RunStatus,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskResponse,
    TaskRun,
)
from lobster0.automation.repository import ScheduledTaskRepository, TaskRunRepository
from lobster0.channels.approvals import ApprovalEnvelope, parse_approval_delivery_payload
from lobster0.channels.base import ChannelTransportError, SendReceipt
from lobster0.channels.delivery import DeliveryWorker
from lobster0.policy.engine import PolicyAction, PolicyDecision
from lobster0.providers.base import ToolCall
from lobster0.storage.channels import ChannelStateError, DeliveryRepository
from lobster0.storage.conversations import SessionRepository, TurnRepository
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations
from lobster0.storage.repositories import OwnerRepository
from lobster0.storage.tooling import ApprovalRepository
from lobster0.tools.base import ToolContext
from tests.fakes.fake_channel import FakeChannelTransport


class TaskDeliveryServiceTest(unittest.IsolatedAsyncioTestCase):
    """验证主动投递只从已完成 Run 投影，崩溃重试不改正文或目的地。"""

    def setUp(self) -> None:
        """创建真实 v5 TaskRun、Outbox 和固定 Channel 上限。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "lobster0.db")
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.now = datetime(2026, 8, 9, 12, tzinfo=UTC)
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.deliveries = DeliveryRepository(self.database, clock=lambda: self.now)
        self.approvals = ApprovalRepository(self.database, clock=lambda: self.now)
        self.session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "task-delivery-fixture",
        )
        turns = TurnRepository(self.database)
        self.turn = turns.create_with_user_message(
            self.session.id,
            "task-delivery-event",
            "test-model",
            "write status",
        )
        turns.mark_running(self.turn.id)
        self.wakes = 0
        self.service = TaskDeliveryService(
            self.deliveries,
            self.runs,
            approvals=self.approvals,
            channel_max_chars={"feishu": 18, "telegram": 16, "discord": 14},
            wake=self._wake,
        )

    def test_same_terminal_run_projects_one_delivery_set(self) -> None:
        """同一终态 Run 重投只能返回同一组 row 与同一 UUID。"""
        response = TaskResponse(True, "done")
        run = self._succeeded_run(response=response)

        first = self.service.project(run, response)
        second = self.service.project(run, response)

        self.assertEqual([item.id for item in first], [item.id for item in second])
        self.assertEqual(
            [item.idempotency_key for item in first],
            [item.idempotency_key for item in second],
        )
        self.assertEqual(first[0].task_run_id, run.id)
        self.assertEqual(self.wakes, 2)
        restarted = TaskDeliveryService(
            DeliveryRepository(self.database, clock=lambda: self.now),
            TaskRunRepository(self.database, clock=lambda: self.now),
            approvals=ApprovalRepository(self.database, clock=lambda: self.now),
            channel_max_chars={"feishu": 18, "telegram": 16, "discord": 14},
        )
        self.assertEqual(restarted.recover(), 1)
        self.assertEqual(
            [item.id for item in restarted.project(run, response)],
            [item.id for item in first],
        )

    def test_notify_false_or_none_route_creates_no_outbox_row(self) -> None:
        """静默 terminal response 和 none 目的地都不能制造空 Delivery。"""
        silent = TaskResponse(False, "")
        silent_run = self._succeeded_run(response=silent)
        no_route = TaskResponse(True, "not delivered")
        no_route_run = self._succeeded_run(
            response=no_route,
            delivery=DeliveryTarget("none", "none"),
            slot=self.now + timedelta(minutes=1),
        )

        self.assertEqual(self.service.project(silent_run, silent), ())
        self.assertEqual(self.service.project(no_route_run, no_route), ())
        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.wakes, 0)

    def test_long_unicode_text_is_lossless_and_destination_is_immutable(self) -> None:
        """多分片去掉序号后必须无损；同 Run 不能改正文或外部目的地。"""
        response = TaskResponse(True, "第一段🙂\n第二段很长很长\n第三段结束")
        run = self._succeeded_run(response=response)

        parts = self.service.project(run, response)

        payload = "".join(
            part.content.removeprefix(f"[{index}/{len(parts)}] ")
            for index, part in enumerate(parts, 1)
        )
        self.assertEqual(payload, response.text)
        self.assertTrue(all(part.channel == "feishu" for part in parts))
        self.assertTrue(all(part.external_conversation_id == "oc_owner" for part in parts))
        with self.assertRaisesRegex(ChannelStateError, "delivery_content_conflict"):
            self.deliveries.create_task_parts(
                task_run_id=run.id,
                channel="feishu",
                account_id="default",
                external_conversation_id="oc_changed",
                reply_to_message_id="",
                kind="message",
                contents=tuple(part.content for part in parts),
            )

    def test_projection_requires_succeeded_matching_persisted_response(self) -> None:
        """调用方不能用旧 Run 或改写 terminal response 注入 Outbox。"""
        response = TaskResponse(True, "bound")
        run = self._succeeded_run(response=response)

        with self.assertRaisesRegex(ValueError, "task delivery response mismatch"):
            self.service.project(run, TaskResponse(True, "changed"))
        queued = self.runs.enqueue(
            self.tasks.get(run.task_id, owner_id=self.owner.id),
            scheduled_for=self.now + timedelta(hours=1),
            idempotency_key="queued-delivery",
        )
        with self.assertRaisesRegex(ValueError, "succeeded"):
            self.service.project(queued, TaskResponse(True, "queued"))

    def test_waiting_approval_projects_durable_card_and_recovers_idempotently(self) -> None:
        """等待审批必须持久化唯一 Approval Delivery，重启不能重复发卡。"""
        run, approval_id = self._waiting_run()

        first = self.service.project_approval(run, approval_id)
        second = self.service.project_approval(run, approval_id)
        parsed = parse_approval_delivery_payload(first[0].content)

        self.assertIsInstance(parsed, ApprovalEnvelope)
        assert isinstance(parsed, ApprovalEnvelope)
        self.assertEqual(parsed.approval_id, approval_id)
        self.assertEqual(first[0].delivery_kind, "approval")
        self.assertEqual(first[0].task_run_id, run.id)
        self.assertEqual([item.id for item in first], [item.id for item in second])
        restarted = TaskDeliveryService(
            DeliveryRepository(self.database, clock=lambda: self.now),
            TaskRunRepository(self.database, clock=lambda: self.now),
            approvals=ApprovalRepository(self.database, clock=lambda: self.now),
            channel_max_chars={"feishu": 18, "telegram": 16, "discord": 14},
        )
        self.assertEqual(restarted.recover(), 1)

    def test_approval_and_terminal_message_use_distinct_idempotency_keys(self) -> None:
        """同一 Run 批准后可投递最终消息，不能与审批卡 UUID 冲突。"""
        waiting, approval_id = self._waiting_run()
        approval_delivery = self.service.project_approval(waiting, approval_id)[0]
        running = self.runs.resume_waiting(
            waiting.id,
            approval_id,
            worker_id="approval-continuation",
            now=self.now,
            lease_seconds=30,
        )
        response = TaskResponse(True, "finished")
        completed = self.runs.finish(
            running.id,
            status=RunStatus.SUCCEEDED,
            now=self.now,
            worker_id="approval-continuation",
            response=response,
        )

        message_delivery = self.service.project(completed, response)[0]

        self.assertNotEqual(
            approval_delivery.idempotency_key,
            message_delivery.idempotency_key,
        )

    async def test_unknown_crash_window_reuses_uuid_and_preserves_part_order(self) -> None:
        """远端结果不确定时重启只能复用首片 UUID，后续分片仍按顺序发送。"""
        response = TaskResponse(True, "abcdefghijklmnopqrstuvwx")
        run = self._succeeded_run(response=response)
        parts = self.service.project(run, response)
        outcomes = [
            ChannelTransportError("feishu_send_timeout", unknown=True),
            SendReceipt("om_recovered_first"),
            *(SendReceipt(f"om_part_{index}") for index in range(1, len(parts))),
        ]
        transport = FakeChannelTransport(tuple(outcomes))
        worker = DeliveryWorker(
            transport=transport,
            repository=self.deliveries,
            channel="feishu",
            account_id="default",
            poll_interval=0.01,
        )

        self.assertTrue(await worker.run_once())
        self.assertEqual(self.deliveries.get(parts[0].id).status, "unknown")
        self.assertEqual(worker.recover(), 1)
        while await worker.run_once():
            pass

        self.assertEqual(transport.sent[0][1], transport.sent[1][1])
        self.assertEqual(
            [message.content for message, _ in transport.sent[1:]],
            [part.content for part in parts],
        )
        self.assertTrue(all(self.deliveries.get(part.id).status == "sent" for part in parts))

    def _succeeded_run(
        self,
        *,
        response: TaskResponse,
        delivery: DeliveryTarget | None = None,
        slot: datetime | None = None,
    ):
        """创建、claim、running 并结算一条真实 TaskRun。"""
        scheduled_for = slot or self.now
        task = self.tasks.create(
            owner_id=self.owner.id,
            name=f"delivery-{scheduled_for.isoformat()}",
            schedule=ScheduleSpec(
                ScheduleKind.INTERVAL,
                "3600",
                "UTC",
                scheduled_for + timedelta(hours=1),
            ),
            prompt="summarize status",
            skill_names=(),
            delivery=delivery
            or DeliveryTarget("owner", "feishu", "default", "oc_owner"),
            policy_profile="automation-default",
            budget=TaskBudget(),
        )
        self.runs.enqueue(task, scheduled_for=scheduled_for)
        claimed = self.runs.claim_next("delivery-worker", now=scheduled_for, lease_seconds=30)
        assert claimed is not None
        self.runs.mark_running(claimed.id, "delivery-worker", now=scheduled_for)
        return self.runs.finish(
            claimed.id,
            status=RunStatus.SUCCEEDED,
            now=scheduled_for,
            worker_id="delivery-worker",
            response=response,
        )

    def _waiting_run(self) -> tuple[TaskRun, int]:
        """创建绑定真实 pending Approval 的 waiting TaskRun。"""
        task = self.tasks.create(
            owner_id=self.owner.id,
            name="approval delivery",
            schedule=ScheduleSpec(
                ScheduleKind.INTERVAL,
                "3600",
                "UTC",
                self.now + timedelta(hours=1),
            ),
            prompt="write status",
            skill_names=(),
            delivery=DeliveryTarget("owner", "feishu", "default", "oc_owner"),
            policy_profile="automation-default",
            budget=TaskBudget(),
        )
        self.runs.enqueue(task, scheduled_for=self.now)
        claimed = self.runs.claim_next("delivery-worker", now=self.now, lease_seconds=30)
        assert claimed is not None
        self.runs.mark_running(claimed.id, "delivery-worker", now=self.now)
        context = ToolContext(
            user_id=self.owner.id,
            session_id=self.session.id,
            turn_id=self.turn.id,
            state_home=Path(self.temporary_directory.name),
            workspace=Path(self.temporary_directory.name),
            read_only_roots=(),
        )
        approval = self.approvals.create_waiting(
            context,
            ToolCall(
                "call_write",
                "write_file",
                {"path": "status.txt", "content": "done"},
            ),
            {"path": "status.txt", "content": "done"},
            PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required"),
            ttl_seconds=600,
            summary="write_file status.txt",
        )
        waiting = self.runs.mark_waiting(
            claimed.id,
            "delivery-worker",
            session_id=self.session.id,
            turn_id=self.turn.id,
            approval_id=approval.id,
        )
        return waiting, approval.id

    def _wake(self) -> None:
        """记录投影服务通知 DeliveryWorker 的次数。"""
        self.wakes += 1


if __name__ == "__main__":
    unittest.main()
