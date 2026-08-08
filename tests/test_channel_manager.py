"""Durable ChannelManager 队列、并发和崩溃恢复测试。"""

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from miniclaw.agent.events import RunEvent
from miniclaw.agent.turn import TurnResult
from miniclaw.channels.approvals import (
    ApprovalCommandOutcome,
    ApprovalEnvelope,
    approval_delivery_payload,
)
from miniclaw.channels.base import InboundMessage, SendReceipt
from miniclaw.channels.capabilities import ChannelCapabilities
from miniclaw.channels.manager import ChannelManager
from miniclaw.channels.observability import ChannelObserver
from miniclaw.channels.progress import ProgressProjector, progress_to_metadata
from miniclaw.policy.approvals import ApprovalDecision
from miniclaw.policy.modes import PermissionMode, PermissionState
from miniclaw.storage.channels import (
    ChannelIdentityRepository,
    DeliveryRepository,
    InboundEventRepository,
)
from miniclaw.storage.conversations import (
    MessageRepository,
    SessionRepository,
    TurnRepository,
)
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import OwnerRepository


@dataclass(slots=True)
class TrackingTurnService:
    """保存真实 Turn/Message、记录并发并可注入失败的测试 Service。"""

    sessions: SessionRepository
    messages: MessageRepository
    turns: TurnRepository
    fail: bool = False
    gate: asyncio.Event | None = None
    expected_concurrency: int = 0
    approval_id: int | None = None
    emit_event: bool = True
    calls: list[tuple[str, str]] = field(init=False, default_factory=list)
    trusted_calls: list[bool] = field(init=False, default_factory=list)
    active: int = field(init=False, default=0)
    max_active: int = field(init=False, default=0)
    reached_concurrency: asyncio.Event = field(init=False)

    def __post_init__(self) -> None:
        self.reached_concurrency = asyncio.Event()

    async def handle_inbound(
        self,
        *,
        user_id: int,
        channel: str,
        account_id: str,
        external_conversation_id: str,
        inbound_event_id: str,
        text: str,
        on_text=None,
        on_event=None,
        trusted_owner: bool = False,
    ) -> TurnResult:
        """模拟共享 TurnService，保留真实持久化边界。"""
        self.calls.append((external_conversation_id, inbound_event_id))
        self.trusted_calls.append(trusted_owner)
        session = self.sessions.get_or_create(
            user_id,
            channel,
            account_id,
            external_conversation_id,
        )
        turn = self.turns.create_with_user_message(
            session.id,
            inbound_event_id,
            "fake-model",
            text,
        )
        self.turns.mark_running(turn.id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active >= self.expected_concurrency > 0:
            self.reached_concurrency.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if on_event is not None and self.emit_event:
                await on_event(
                    RunEvent(
                        "model_text_delta",
                        turn.id,
                        {"text": "partial" if self.fail else f"reply:{text}"},
                    )
                )
            if self.fail:
                self.turns.fail(turn.id, "provider_server_error", "safe failure")
                raise RuntimeError("secret-provider-detail")
            if self.approval_id is not None:
                self.turns.wait_for_approval(
                    turn.id,
                    session.id,
                    self.approval_id,
                    input_tokens=1,
                    output_tokens=1,
                    provider_request_id="req_approval",
                    iterations=1,
                )
                return TurnResult(
                    turn_id=turn.id,
                    session_id=session.id,
                    content="approval required",
                    input_tokens=1,
                    output_tokens=1,
                    provider_request_id="req_approval",
                    message_id=None,
                    approval_id=self.approval_id,
                )
            assistant = self.turns.complete_with_assistant_message(
                turn.id,
                session.id,
                f"reply:{text}",
                input_tokens=1,
                output_tokens=1,
                provider_request_id="req_fake",
                iterations=1,
                finish_reason="stop",
            )
            return TurnResult(
                turn_id=turn.id,
                session_id=session.id,
                content=assistant.content,
                input_tokens=1,
                output_tokens=1,
                provider_request_id="req_fake",
                message_id=assistant.id,
                approval_id=None,
            )
        finally:
            self.active -= 1


@dataclass(slots=True)
class ManagerCapabilityTransport:
    """验证 Manager 是否把能力生命周期包在 Turn 外层。"""

    fail_card: bool = False
    typing_added: list[str] = field(default_factory=list)
    typing_removed: list[tuple[str, str | None]] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    card_ids_by_key: dict[str, str] = field(default_factory=dict)

    async def add_typing(self, message_id: str) -> str:
        """记录 Typing 开始。"""
        self.typing_added.append(message_id)
        return "reaction_manager"

    async def remove_typing(self, message_id: str, reaction_id: str | None) -> bool:
        """记录 Typing 结束。"""
        self.typing_removed.append((message_id, reaction_id))
        return True

    async def send_card(
        self,
        *,
        conversation_id: str,
        reply_to_message_id: str,
        card: dict[str, Any],
        idempotency_key: str,
    ) -> SendReceipt:
        """记录进度卡片或模拟 API 失败。"""
        del conversation_id, reply_to_message_id
        self.cards.append(card)
        if self.fail_card:
            raise RuntimeError("private-card-error")
        message_id = self.card_ids_by_key.setdefault(
            idempotency_key,
            "om_manager_card",
        )
        return SendReceipt(message_id)

    async def update_card(
        self,
        platform_message_id: str,
        card: dict[str, Any],
    ) -> SendReceipt:
        """记录卡片终态。"""
        self.cards.append(card)
        return SendReceipt(platform_message_id)


@dataclass(slots=True)
class ManagerApprovalController:
    """模拟已经过单元测试的 Approval Channel Controller。"""

    calls: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    async def handle_text(
        self,
        *,
        user_id: int,
        actor_external_user_id: str,
        text: str,
        on_event=None,
    ) -> ApprovalCommandOutcome:
        """只消费测试中的 /deny 命令并返回安全通知。"""
        del user_id, on_event
        self.calls.append((actor_external_user_id, text))
        if self.fail:
            raise RuntimeError("approval-controller-secret")
        if text == "/deny 7":
            return ApprovalCommandOutcome(
                True,
                notice="审批 #7 已拒绝。",
                approval_id=7,
            )
        return ApprovalCommandOutcome(False)

    def prompt(self, *, user_id: int, approval_id: int) -> ApprovalEnvelope:
        """返回固定平台中立 envelope。"""
        del user_id
        return ApprovalEnvelope(
            version=2,
            approval_id=approval_id,
            tool_name="write_file",
            summary=f"approval-{approval_id}",
            decisions=(ApprovalDecision.ONCE, ApprovalDecision.DENY),
            expires_at="2026-08-08T09:00:00+00:00",
            fallback_text=f"发送 /approve {approval_id} once 或 /deny {approval_id}",
        )


class ChannelManagerTest(unittest.IsolatedAsyncioTestCase):
    """验证内存队列只是 wake-up，SQLite 才是事实来源。"""

    def setUp(self) -> None:
        """创建独立 schema v2、Owner 和 Repository。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(
            Path(self.temporary_directory.name).resolve() / "miniclaw.db"
        )
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.sessions = SessionRepository(self.database)
        self.messages = MessageRepository(self.database)
        self.turns = TurnRepository(self.database)
        self.inbound = InboundEventRepository(self.database)
        self.deliveries = DeliveryRepository(self.database)
        self.permission_state = PermissionState(PermissionMode.SAFE)

    async def test_only_owner_private_messages_receive_trusted_automation(self) -> None:
        """Owner 私聊为 trusted；Owner 群聊和其他白名单成员均降级。"""
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        manager = self._manager(service, queue_size=4, worker_count=1)
        await manager.start()
        try:
            await manager.receive(self._message("om_owner", "one"))
            await manager.receive(
                self._message("om_group", "two", chat_id="oc_group", chat_type="group")
            )
            await manager.receive(
                self._message("om_friend", "three", external_user_id="ou_friend")
            )
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        self.assertEqual(service.trusted_calls, [True, False, False])

    async def test_permissions_command_is_owner_private_only_and_bypasses_agent(self) -> None:
        """Owner 私聊可切换/查询模式；群聊和其他成员不能切换或进入模型。"""
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        manager = self._manager(service, queue_size=6, worker_count=1)
        await manager.start()
        try:
            await manager.receive(self._message("om_set", "/permissions autopilot"))
            await manager.receive(self._message("om_get", "/permissions"))
            await manager.receive(
                self._message(
                    "om_group_mode",
                    "/permissions yolo",
                    chat_id="oc_group_mode",
                    chat_type="group",
                )
            )
            await manager.receive(
                self._message(
                    "om_friend_mode",
                    "/permissions yolo",
                    external_user_id="ou_friend",
                )
            )
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        self.assertEqual(service.calls, [])
        self.assertEqual(self.permission_state.mode, PermissionMode.AUTOPILOT)
        with self.database.connect_read_only() as connection:
            notices = {
                row["reply_to_message_id"]: row["content"]
                for row in connection.execute(
                    "SELECT reply_to_message_id, content FROM deliveries "
                    "WHERE reply_to_message_id IN "
                    "('om_set', 'om_get', 'om_group_mode', 'om_friend_mode')"
                )
            }
        self.assertIn("autopilot", notices["om_set"])
        self.assertIn("autopilot", notices["om_get"])
        self.assertIn("Owner 私聊", notices["om_group_mode"])
        self.assertIn("Owner 私聊", notices["om_friend_mode"])

    async def test_receive_persists_before_enqueue_and_duplicate_is_not_queued(self) -> None:
        """callback 成功返回前必须落库，相同 message_id 不得产生第二个 wake-up。"""
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        manager = self._manager(service, queue_size=2, worker_count=1)

        first = await manager.receive(self._message("om_1", "one"))
        duplicate = await manager.receive(
            self._message("om_1", "must-not-overwrite", event_id="evt_retry")
        )

        queued = self.inbound.list_by_status("feishu", "default", "queued")
        self.assertTrue(first.inserted)
        self.assertTrue(first.enqueued)
        self.assertFalse(duplicate.inserted)
        self.assertFalse(duplicate.enqueued)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].content, "one")

    async def test_inbound_and_turn_lifecycle_write_correlated_audit(self) -> None:
        """Manager 首次落库、重复投递和 Turn 终态必须共享安全 correlation。"""
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        observer = ChannelObserver(self.database)
        manager = self._manager(
            service,
            queue_size=2,
            worker_count=1,
            observer=observer,
        )

        await manager.receive(self._message("om_observed", "one"))
        await manager.receive(
            self._message("om_observed", "ignored duplicate", event_id="evt_retry")
        )
        await manager.start()
        await manager.wait_idle(timeout=1)
        await manager.stop()

        with self.database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT event_type, metadata_json FROM audit_events
                WHERE event_type LIKE 'channel.%'
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(
            [row["event_type"] for row in rows],
            [
                "channel.inbound.accepted",
                "channel.inbound.duplicate",
                "channel.turn.started",
                "channel.turn.completed",
            ],
        )
        metadata = [json.loads(row["metadata_json"]) for row in rows]
        self.assertEqual(len({item["correlation_id"] for item in metadata}), 1)
        self.assertGreater(metadata[0]["event_row_id"], 0)
        self.assertEqual(metadata[-1]["tool_count"], 0)
        self.assertEqual(metadata[-1]["approval_state"], "none")
        self.assertNotIn("om_observed", repr(metadata))

    async def test_full_memory_queue_recovers_second_message_from_database(self) -> None:
        """queue 满只能丢 wake-up，不能丢已经持久化的第二条消息。"""
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        manager = self._manager(service, queue_size=1, worker_count=1)
        first = await manager.receive(self._message("om_1", "one"))
        second = await manager.receive(self._message("om_2", "two"))

        self.assertTrue(first.enqueued)
        self.assertFalse(second.enqueued)
        await manager.start()
        try:
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        self.assertEqual(
            [event.external_message_id for event in self.inbound.list_by_status(
                "feishu", "default", "completed"
            )],
            ["om_1", "om_2"],
        )
        self.assertEqual(len(service.calls), 2)

    async def test_same_conversation_serializes_with_two_workers(self) -> None:
        """同一个 Chat 的第二条消息不能越过仍在运行的第一条。"""
        gate = asyncio.Event()
        service = TrackingTurnService(
            self.sessions,
            self.messages,
            self.turns,
            gate=gate,
            expected_concurrency=2,
        )
        manager = self._manager(service, queue_size=4, worker_count=2)
        await manager.start()
        try:
            await manager.receive(self._message("om_1", "one"))
            await manager.receive(self._message("om_2", "two"))
            await asyncio.sleep(0.05)
            self.assertEqual(service.max_active, 1)
            gate.set()
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        self.assertEqual(service.max_active, 1)
        self.assertEqual(service.calls, [("oc_chat", "om_1"), ("oc_chat", "om_2")])

    async def test_different_conversations_run_concurrently(self) -> None:
        """两个不同 Chat 可以使用两个 Worker，不被全局锁错误串行化。"""
        gate = asyncio.Event()
        service = TrackingTurnService(
            self.sessions,
            self.messages,
            self.turns,
            gate=gate,
            expected_concurrency=2,
        )
        manager = self._manager(service, queue_size=4, worker_count=2)
        await manager.start()
        try:
            await manager.receive(self._message("om_1", "one", chat_id="oc_a"))
            await manager.receive(self._message("om_2", "two", chat_id="oc_b"))
            await asyncio.wait_for(service.reached_concurrency.wait(), timeout=1)
            self.assertEqual(service.max_active, 2)
            gate.set()
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

    async def test_success_creates_delivery_and_failure_creates_safe_notice(self) -> None:
        """成功回复和失败提示都必须先进入 Outbox，原始异常不能写入数据库。"""
        successful = TrackingTurnService(self.sessions, self.messages, self.turns)
        manager = self._manager(successful, queue_size=2, worker_count=1)
        await manager.start()
        try:
            await manager.receive(self._message("om_ok", "hello"))
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        with self.database.connect_read_only() as connection:
            success_delivery = connection.execute(
                "SELECT * FROM deliveries ORDER BY id LIMIT 1"
            ).fetchone()
        self.assertEqual(success_delivery["content"], "reply:hello")
        self.assertEqual(success_delivery["reply_to_message_id"], "om_ok")

        failing = TrackingTurnService(
            self.sessions,
            self.messages,
            self.turns,
            fail=True,
        )
        failed_manager = self._manager(failing, queue_size=2, worker_count=1)
        await failed_manager.start()
        try:
            await failed_manager.receive(self._message("om_fail", "break"))
            await failed_manager.wait_idle(timeout=2)
        finally:
            await failed_manager.stop()

        event = self.inbound.get(
            self.inbound.list_by_status("feishu", "default", "failed")[0].key
        )
        with self.database.connect_read_only() as connection:
            failure_delivery = connection.execute(
                "SELECT * FROM deliveries WHERE reply_to_message_id = 'om_fail'"
            ).fetchone()
            dump = "\n".join(
                str(row[0])
                for row in connection.execute(
                    "SELECT content FROM messages UNION ALL "
                    "SELECT content FROM deliveries UNION ALL "
                    "SELECT COALESCE(last_error_detail, '') FROM deliveries"
                )
            )
        self.assertEqual(event.last_error_code, "channel_turn_failed")
        self.assertIn("处理失败", failure_delivery["content"])
        self.assertNotIn("secret-provider-detail", dump)

    async def test_feishu_completed_card_replaces_text_and_failure_falls_back(self) -> None:
        """飞书成功卡片是唯一回复；卡片失败时才创建文本 Outbox。"""
        for index, fail_card in enumerate((False, True), start=1):
            with self.subTest(fail_card=fail_card):
                service = TrackingTurnService(self.sessions, self.messages, self.turns)
                transport = ManagerCapabilityTransport(fail_card=fail_card)
                capabilities = ChannelCapabilities(
                    transport=transport,
                    streaming_card=True,
                    update_interval=0.01,
                )
                manager = self._manager(service, queue_size=2, worker_count=1)
                manager.attach_experience(capabilities)
                message_id = f"om_cap_{index}"
                await manager.start()
                try:
                    await manager.receive(self._message(message_id, "hello"))
                    await manager.wait_idle(timeout=2)
                finally:
                    await manager.stop()

                self.assertEqual(transport.typing_added, [message_id])
                self.assertEqual(
                    transport.typing_removed,
                    [(message_id, "reaction_manager")],
                )
                with self.database.connect_read_only() as connection:
                    delivery = connection.execute(
                        "SELECT content FROM deliveries "
                        "WHERE reply_to_message_id = ? AND delivery_kind = 'message'",
                        (message_id,),
                    ).fetchone()
                if fail_card:
                    self.assertIsNotNone(delivery)
                    self.assertEqual(delivery["content"], "reply:hello")
                else:
                    self.assertIsNone(delivery)

    async def test_feishu_turn_failure_finishes_same_card_without_text_notice(self) -> None:
        """飞书 Turn 失败时必须把安全提示写入原卡，不再创建卡片外灰色消息。"""
        service = TrackingTurnService(
            self.sessions,
            self.messages,
            self.turns,
            fail=True,
        )
        transport = ManagerCapabilityTransport()
        capabilities = ChannelCapabilities(
            transport=transport,
            streaming_card=True,
            update_interval=0.01,
        )
        manager = self._manager(service, queue_size=2, worker_count=1)
        manager.attach_experience(capabilities)

        await manager.start()
        try:
            await manager.receive(self._message("om_card_failure", "break"))
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        with self.database.connect_read_only() as connection:
            delivery = connection.execute(
                "SELECT content FROM deliveries "
                "WHERE reply_to_message_id = 'om_card_failure' "
                "AND delivery_kind = 'message'"
            ).fetchone()
        self.assertIsNone(delivery)
        self.assertGreaterEqual(len(transport.cards), 2)
        self.assertIn("MiniClaw · 执行中", repr(transport.cards[0]))
        self.assertIn("MiniClaw · 未完成", repr(transport.cards[-1]))
        self.assertIn("处理失败", repr(transport.cards[-1]))

    async def test_feishu_card_overflow_replies_only_tail_to_card(self) -> None:
        """卡片装不下时只把未展示后缀持久化，并回复机器人自己的卡片。"""
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        transport = ManagerCapabilityTransport()
        capabilities = ChannelCapabilities(
            transport=transport,
            streaming_card=True,
            update_interval=0.01,
        )
        manager = self._manager(service, queue_size=2, worker_count=1)
        manager.attach_experience(capabilities)

        await manager.start()
        try:
            await manager.receive(self._message("om_overflow", "x" * 25_000))
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        with self.database.connect_read_only() as connection:
            deliveries = connection.execute(
                "SELECT content, reply_to_message_id FROM deliveries "
                "WHERE message_id IS NOT NULL ORDER BY part_index"
            ).fetchall()
        self.assertGreater(len(deliveries), 0)
        self.assertTrue(
            all(row["reply_to_message_id"] == "om_manager_card" for row in deliveries)
        )
        card = transport.cards[-1]
        answer_element = next(
            element
            for element in card["body"]["elements"]
            if isinstance(element, dict)
            and isinstance(element.get("content"), str)
            and element["content"].startswith("**最终回答**\n")
        )
        visible = answer_element["content"].removeprefix("**最终回答**\n")
        visible = visible.removesuffix("\n- _答案过长，剩余内容将继续发送。_")
        tail = "".join(row["content"] for row in deliveries)
        self.assertTrue(visible.startswith("- "))
        self.assertEqual(visible.removeprefix("- ") + tail, "reply:" + "x" * 25_000)

    async def test_approval_commands_bypass_agent_and_waiting_turn_creates_card(self) -> None:
        """控制命令不进模型；普通 Turn waiting 时创建 durable Approval card。"""
        controller = ManagerApprovalController()
        command_service = TrackingTurnService(self.sessions, self.messages, self.turns)
        command_manager = self._manager(command_service, queue_size=2, worker_count=1)
        command_manager.attach_approvals(controller)
        await command_manager.start()
        try:
            await command_manager.receive(self._message("om_command", "/deny 7"))
            await command_manager.wait_idle(timeout=2)
        finally:
            await command_manager.stop()

        self.assertEqual(command_service.calls, [])
        self.assertEqual(controller.calls[-1], ("ou_owner", "/deny 7"))
        with self.database.connect_read_only() as connection:
            notice = connection.execute(
                "SELECT content FROM deliveries WHERE reply_to_message_id = 'om_command'"
            ).fetchone()
        self.assertEqual(notice["content"], "审批 #7 已拒绝。")

        waiting_service = TrackingTurnService(
            self.sessions,
            self.messages,
            self.turns,
            approval_id=7,
        )
        waiting_transport = ManagerCapabilityTransport()
        waiting_capabilities = ChannelCapabilities(
            transport=waiting_transport,
            streaming_card=True,
            update_interval=0.01,
        )
        waiting_manager = self._manager(waiting_service, queue_size=2, worker_count=1)
        waiting_manager.attach_approvals(controller)
        waiting_manager.attach_experience(waiting_capabilities)
        await waiting_manager.start()
        try:
            await waiting_manager.receive(self._message("om_waiting", "write file"))
            await waiting_manager.wait_idle(timeout=2)
        finally:
            await waiting_manager.stop()

        with self.database.connect_read_only() as connection:
            card_deliveries = connection.execute(
                "SELECT * FROM deliveries WHERE reply_to_message_id = 'om_waiting'"
            ).fetchall()
        self.assertEqual(len(card_deliveries), 1)
        card_delivery = card_deliveries[0]
        self.assertEqual(card_delivery["delivery_kind"], "approval")
        self.assertEqual(
            card_delivery["content"],
            approval_delivery_payload(controller.prompt(user_id=self.owner.id, approval_id=7)),
        )
        self.assertEqual(len(waiting_transport.cards), 2)
        self.assertIn("MiniClaw · 执行中", repr(waiting_transport.cards[0]))
        self.assertIn("MiniClaw · 未完成", repr(waiting_transport.cards[-1]))

    async def test_approval_controller_failure_creates_safe_durable_notice(self) -> None:
        """控制层异常不能杀死 Worker，也不能让原始异常进入 SQLite。"""
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        manager = self._manager(service, queue_size=2, worker_count=1)
        manager.attach_approvals(ManagerApprovalController(fail=True))
        await manager.start()
        try:
            await manager.receive(self._message("om_control_fail", "/deny 7"))
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        self.assertEqual(service.calls, [])
        event = self.inbound.list_by_status("feishu", "default", "failed")[-1]
        self.assertEqual(event.last_error_code, "channel_control_failed")
        with self.database.connect_read_only() as connection:
            dump = "\n".join(
                str(row[0])
                for row in connection.execute(
                    "SELECT content FROM messages UNION ALL SELECT content FROM deliveries"
                )
            )
        self.assertIn("处理失败", dump)
        self.assertNotIn("approval-controller-secret", dump)

    async def test_restart_marks_running_turn_failed_without_replay(self) -> None:
        """已有 running Turn 表明可能执行过 Tool，重启只能中断，不能再次调用 Service。"""
        message = self._message("om_running", "write something")
        event = self.inbound.record(message).event
        claimed = self.inbound.claim(event.key)
        session = self.sessions.get_or_create(
            self.owner.id,
            "feishu",
            "default",
            "oc_chat",
        )
        self.inbound.bind_session(claimed.key, session.id)
        turn = self.turns.create_with_user_message(
            session.id,
            "om_running",
            "fake-model",
            "write something",
        )
        self.turns.mark_running(turn.id)
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        manager = self._manager(service, queue_size=2, worker_count=1)

        await manager.start()
        try:
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        self.assertEqual(service.calls, [])
        self.assertEqual(self.turns.get(turn.id).status, "failed")
        recovered = self.inbound.get(event.key)
        self.assertEqual(recovered.status, "failed")
        self.assertEqual(recovered.last_error_code, "channel_turn_interrupted")

    async def test_restart_recovers_completed_turn_through_same_card(self) -> None:
        """卡片成功后结算崩溃时，重启必须复用 UUID 完成同一卡片且不补发全文。"""
        message = self._message("om_completed_restart", "hello")
        event = self.inbound.record(message).event
        claimed = self.inbound.claim(event.key)
        session = self.sessions.get_or_create(
            self.owner.id,
            "feishu",
            "default",
            "oc_chat",
        )
        self.inbound.bind_session(claimed.key, session.id)
        turn = self.turns.create_with_user_message(
            session.id,
            "om_completed_restart",
            "fake-model",
            "hello",
        )
        self.turns.mark_running(turn.id)
        assistant = self.turns.complete_with_assistant_message(
            turn.id,
            session.id,
            "reply:hello",
            input_tokens=1,
            output_tokens=1,
            provider_request_id="req_restart",
            iterations=1,
            finish_reason="stop",
        )
        projector = ProgressProjector(clock=lambda: 0.0)
        projector.apply(
            RunEvent(
                "tool_requested",
                turn.id,
                {
                    "call_id": "call_restart",
                    "tool_name": "read_file",
                    "arguments": {"path": "README.md"},
                },
            )
        )
        projector.apply(
            RunEvent(
                "tool_finished",
                turn.id,
                {
                    "call_id": "call_restart",
                    "tool_name": "read_file",
                    "status": "succeeded",
                },
            )
        )
        self.messages.save_experience_trace(
            assistant.id,
            progress_to_metadata(projector.finish("reply:hello", failed=False)),
        )
        transport = ManagerCapabilityTransport()
        capabilities = ChannelCapabilities(
            transport=transport,
            streaming_card=True,
            update_interval=0.01,
        )
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        first_manager = self._manager(service, queue_size=2, worker_count=1)
        first_manager.attach_experience(capabilities)
        recover_running = self.inbound.recover_running
        interrupted = False

        def interrupt_after_remote_card(key, status, error_code):
            """首次 completed 结算前模拟进程中断，保留 running Inbox。"""
            nonlocal interrupted
            if status == "completed" and not interrupted:
                interrupted = True
                raise RuntimeError("simulated-process-stop")
            return recover_running(key, status, error_code)

        self.inbound.recover_running = interrupt_after_remote_card
        with self.assertRaisesRegex(RuntimeError, "simulated-process-stop"):
            await first_manager.start()
        self.inbound.recover_running = recover_running

        second_manager = self._manager(service, queue_size=2, worker_count=1)
        second_manager.attach_experience(capabilities)
        await second_manager.start()
        try:
            await second_manager.wait_idle(timeout=2)
        finally:
            await second_manager.stop()

        self.assertEqual(service.calls, [])
        self.assertEqual(len(transport.card_ids_by_key), 1)
        self.assertIn("查看文件", repr(transport.cards[-1]))
        self.assertEqual(self.inbound.get(event.key).status, "completed")
        with self.database.connect_read_only() as connection:
            message_deliveries = connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE delivery_kind = 'message' "
                "AND reply_to_message_id = 'om_completed_restart'"
            ).fetchone()[0]
        self.assertEqual(message_deliveries, 0)

    async def test_restart_recovers_waiting_approval_without_failing_parent(self) -> None:
        """崩溃发生在 waiting 持久化后时应补发审批卡，不能把 Parent Turn 判失败。"""
        message = self._message("om_wait_restart", "write something")
        event = self.inbound.record(message).event
        claimed = self.inbound.claim(event.key)
        session = self.sessions.get_or_create(
            self.owner.id,
            "feishu",
            "default",
            "oc_chat",
        )
        self.inbound.bind_session(claimed.key, session.id)
        turn = self.turns.create_with_user_message(
            session.id,
            "om_wait_restart",
            "fake-model",
            "write something",
        )
        self.turns.mark_running(turn.id)
        self.turns.wait_for_approval(
            turn.id,
            session.id,
            7,
            input_tokens=1,
            output_tokens=1,
            provider_request_id="req_wait",
            iterations=1,
        )
        controller = ManagerApprovalController()
        service = TrackingTurnService(self.sessions, self.messages, self.turns)
        manager = self._manager(service, queue_size=2, worker_count=1)
        manager.attach_approvals(controller)

        await manager.start()
        try:
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()

        self.assertEqual(service.calls, [])
        self.assertEqual(self.turns.get(turn.id).status, "waiting_approval")
        self.assertEqual(self.inbound.get(event.key).status, "completed")
        with self.database.connect_read_only() as connection:
            delivery = connection.execute(
                "SELECT delivery_kind FROM deliveries "
                "WHERE reply_to_message_id = 'om_wait_restart'"
            ).fetchone()
        self.assertEqual(delivery["delivery_kind"], "approval")

    def _manager(
        self,
        service: TrackingTurnService,
        *,
        queue_size: int,
        worker_count: int,
        observer: ChannelObserver | None = None,
    ) -> ChannelManager:
        """用共享 Repository 构造 Manager。"""
        return ChannelManager(
            owner_id=self.owner.id,
            owner_external_user_id="ou_owner",
            permission_state=self.permission_state,
            service=service,
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            identities=ChannelIdentityRepository(self.database),
            inbound=self.inbound,
            deliveries=self.deliveries,
            channel="feishu",
            account_id="default",
            queue_size=queue_size,
            worker_count=worker_count,
            feeder_interval=0.01,
            observer=observer,
        )

    def _message(
        self,
        message_id: str,
        text: str,
        *,
        event_id: str | None = None,
        chat_id: str = "oc_chat",
        chat_type: str = "p2p",
        external_user_id: str = "ou_owner",
    ) -> InboundMessage:
        """创建一条标准化飞书消息。"""
        return InboundMessage(
            channel="feishu",
            account_id="default",
            event_id=event_id or f"evt_{message_id}",
            message_id=message_id,
            external_user_id=external_user_id,
            external_conversation_id=chat_id,
            chat_type=chat_type,
            message_type="text",
            text=text,
            reply_to_message_id=message_id,
            received_at=datetime(2026, 8, 8, tzinfo=UTC),
        )


if __name__ == "__main__":
    unittest.main()
