"""Durable ChannelManager 队列、并发和崩溃恢复测试。"""

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.agent.turn import TurnResult
from miniclaw.channels.base import InboundMessage
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
            if self.fail:
                self.turns.fail(turn.id, "provider_server_error", "safe failure")
                raise RuntimeError("secret-provider-detail")
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
