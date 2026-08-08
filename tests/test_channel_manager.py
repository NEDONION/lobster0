"""Durable ChannelManager 队列、并发和崩溃恢复测试。"""

import asyncio
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
    ApprovalPrompt,
    approval_delivery_payload,
)
from miniclaw.channels.base import InboundMessage, SendReceipt
from miniclaw.channels.capabilities import ChannelCapabilities
from miniclaw.channels.manager import ChannelManager
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
    calls: list[tuple[str, str]] = field(init=False, default_factory=list)
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
    ) -> TurnResult:
        """模拟共享 TurnService，保留真实持久化边界。"""
        self.calls.append((external_conversation_id, inbound_event_id))
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
            if on_event is not None:
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
        del conversation_id, reply_to_message_id, idempotency_key
        self.cards.append(card)
        if self.fail_card:
            raise RuntimeError("private-card-error")
        return SendReceipt("om_manager_card")

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

    async def handle_text(
        self,
        *,
        user_id: int,
        actor_open_id: str,
        text: str,
        on_event=None,
    ) -> ApprovalCommandOutcome:
        """只消费测试中的 /deny 命令并返回安全通知。"""
        del user_id, on_event
        self.calls.append((actor_open_id, text))
        if text == "/deny 7":
            return ApprovalCommandOutcome(
                True,
                notice="审批 #7 已拒绝。",
                approval_id=7,
            )
        return ApprovalCommandOutcome(False)

    def prompt(self, *, user_id: int, approval_id: int) -> ApprovalPrompt:
        """返回固定卡片和文本 fallback。"""
        del user_id
        return ApprovalPrompt(
            card={"header": {"title": f"approval-{approval_id}"}},
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

    async def test_capabilities_wrap_turn_and_final_markdown_stays_durable(self) -> None:
        """Typing/Card 成败都不能取代最终 SQLite Outbox。"""
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
                manager.attach_capabilities(capabilities)
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
                self.assertIsNotNone(delivery)
                self.assertEqual(delivery["content"], "reply:hello")

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
        waiting_manager = self._manager(waiting_service, queue_size=2, worker_count=1)
        waiting_manager.attach_approvals(controller)
        await waiting_manager.start()
        try:
            await waiting_manager.receive(self._message("om_waiting", "write file"))
            await waiting_manager.wait_idle(timeout=2)
        finally:
            await waiting_manager.stop()

        with self.database.connect_read_only() as connection:
            card_delivery = connection.execute(
                "SELECT * FROM deliveries WHERE reply_to_message_id = 'om_waiting'"
            ).fetchone()
        self.assertEqual(card_delivery["delivery_kind"], "approval")
        self.assertEqual(
            card_delivery["content"],
            approval_delivery_payload(controller.prompt(user_id=self.owner.id, approval_id=7)),
        )

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
    ) -> ChannelManager:
        """用共享 Repository 构造 Manager。"""
        return ChannelManager(
            owner_id=self.owner.id,
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
        )

    def _message(
        self,
        message_id: str,
        text: str,
        *,
        event_id: str | None = None,
        chat_id: str = "oc_chat",
    ) -> InboundMessage:
        """创建一条标准化飞书消息。"""
        return InboundMessage(
            channel="feishu",
            account_id="default",
            event_id=event_id or f"evt_{message_id}",
            message_id=message_id,
            external_user_id="ou_owner",
            external_conversation_id=chat_id,
            chat_type="p2p",
            message_type="text",
            text=text,
            reply_to_message_id=message_id,
            received_at=datetime(2026, 8, 8, tzinfo=UTC),
        )


if __name__ == "__main__":
    unittest.main()
