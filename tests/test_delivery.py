"""Unicode 分片、DeliveryWorker 重试与恢复测试。"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.channels.base import ChannelTransportError, SendReceipt
from miniclaw.channels.delivery import DeliveryWorker, split_message
from miniclaw.storage.channels import DeliveryRepository
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import OwnerRepository
from tests.fakes.fake_channel import FakeChannelTransport


class MutableClock:
    """Delivery 重试测试使用的可推进 UTC 时钟。"""

    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        """返回当前测试时间。"""
        return self.current


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    """验证 Outbox 在成功、临时错误和不确定结果下保持可恢复。"""

    def setUp(self) -> None:
        """创建独立数据库、Assistant Message 和固定时钟。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(
            Path(self.temporary_directory.name).resolve() / "miniclaw.db"
        )
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.clock = MutableClock(datetime(2026, 8, 8, 8, 0, tzinfo=UTC))
        self.repository = DeliveryRepository(self.database, clock=self.clock)

    def test_split_message_preserves_unicode_and_prefix_budget(self) -> None:
        """中文、emoji、段落和换行不能损坏，每个带序号 part 都不越界。"""
        content = "第一段🙂\n\n第二段很长很长\n第三行🚀结束"

        parts = split_message(content, max_chars=14)

        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 14 for part in parts))
        payloads = []
        for index, part in enumerate(parts, start=1):
            prefix = f"[{index}/{len(parts)}] "
            self.assertTrue(part.startswith(prefix))
            payloads.append(part.removeprefix(prefix))
        self.assertEqual("".join(payloads), content)
        self.assertEqual(split_message("短回复", max_chars=14), ("短回复",))

    async def test_parts_send_in_order_and_permanent_failure_blocks_tail(self) -> None:
        """中间 part 永久失败后，后续 part 不得越过它继续发送。"""
        message_id = self._assistant_message()
        deliveries = self.repository.create_parts(
            message_id=message_id,
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_chat",
            reply_to_message_id="om_source",
            kind="message",
            contents=("one", "two", "three"),
        )
        transport = FakeChannelTransport(
            (
                SendReceipt("om_sent_1"),
                ChannelTransportError("feishu_permission_denied"),
            )
        )
        worker = self._worker(transport)

        self.assertTrue(await worker.run_once())
        self.assertTrue(await worker.run_once())
        self.assertFalse(await worker.run_once())

        self.assertEqual(self.repository.get(deliveries[0].id).status, "sent")
        self.assertEqual(self.repository.get(deliveries[1].id).status, "failed")
        self.assertEqual(self.repository.get(deliveries[2].id).status, "queued")
        self.assertEqual(
            [message.content for message, _ in transport.sent],
            ["one", "two"],
        )

    async def test_retryable_error_uses_due_time_and_same_idempotency_key(self) -> None:
        """429 临时失败到期后才重试，第二次发送必须复用原 UUID。"""
        delivery = self._delivery("retry")
        transport = FakeChannelTransport(
            (
                ChannelTransportError("feishu_rate_limited", retryable=True),
                SendReceipt("om_sent"),
            )
        )
        worker = self._worker(transport, base_delay=10)

        self.assertTrue(await worker.run_once())
        waiting = self.repository.get(delivery.id)
        self.assertEqual(waiting.status, "retry_wait")
        self.assertEqual(waiting.next_attempt_at, self.clock.current + timedelta(seconds=10))
        self.assertFalse(await worker.run_once())
        self.clock.current += timedelta(seconds=10)
        self.assertTrue(await worker.run_once())

        sent = self.repository.get(delivery.id)
        self.assertEqual(sent.status, "sent")
        self.assertEqual(sent.attempts, 2)
        self.assertEqual(transport.sent[0][1], transport.sent[1][1])
        self.assertEqual(transport.sent[0][1], delivery.idempotency_key)

    async def test_timeout_unknown_is_recovered_with_same_uuid_after_restart(self) -> None:
        """无法判断平台是否收到时先记 unknown，重启后按相同 UUID 安全恢复。"""
        delivery = self._delivery("unknown")
        first_transport = FakeChannelTransport(
            (ChannelTransportError("feishu_send_timeout", unknown=True),)
        )
        first_worker = self._worker(first_transport)

        self.assertTrue(await first_worker.run_once())
        self.assertEqual(self.repository.get(delivery.id).status, "unknown")

        second_transport = FakeChannelTransport((SendReceipt("om_recovered"),))
        second_worker = self._worker(second_transport)
        self.assertEqual(second_worker.recover(), 1)
        self.assertEqual(self.repository.get(delivery.id).status, "queued")
        self.assertTrue(await second_worker.run_once())
        self.assertEqual(first_transport.sent[0][1], second_transport.sent[0][1])

    async def test_max_attempts_and_unexpected_error_are_failed_and_redacted(self) -> None:
        """达到重试上限或未知异常必须失败，异常原文不能进入数据库。"""
        delivery = self._delivery("secret")
        transport = FakeChannelTransport(
            (
                ChannelTransportError("feishu_not_connected", retryable=True),
                RuntimeError("authorization=secret-value"),
            )
        )
        worker = self._worker(transport, max_attempts=2, base_delay=1)

        self.assertTrue(await worker.run_once())
        self.clock.current += timedelta(seconds=1)
        self.assertTrue(await worker.run_once())

        failed = self.repository.get(delivery.id)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.last_error_code, "feishu_send_failed")
        with self.database.connect_read_only() as connection:
            dump = "\n".join(
                str(value)
                for row in connection.execute(
                    "SELECT last_error_code, COALESCE(last_error_detail, '') FROM deliveries"
                )
                for value in row
            )
        self.assertNotIn("secret-value", dump)

    def _worker(
        self,
        transport: FakeChannelTransport,
        *,
        max_attempts: int = 5,
        base_delay: float = 1,
    ) -> DeliveryWorker:
        """构造不使用真实 sleep 或随机抖动的 Worker。"""
        return DeliveryWorker(
            transport=transport,
            repository=self.repository,
            channel="feishu",
            account_id="default",
            max_attempts=max_attempts,
            base_delay=base_delay,
            max_delay=60,
            jitter=lambda: 1.0,
            clock=self.clock,
            poll_interval=0.01,
        )

    def _delivery(self, content: str):
        """创建一个 queued Delivery。"""
        return self.repository.create_parts(
            message_id=self._assistant_message(),
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_chat",
            reply_to_message_id="om_source",
            kind="message",
            contents=(content,),
        )[0]

    def _assistant_message(self) -> int:
        """创建 Delivery 外键所需的 Assistant Message。"""
        now = self.clock.current.isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, 'feishu', 'default', ?, 'active', ?, ?)
                """,
                (self.owner.id, f"oc_{id(self)}_{now}", now, now),
            )
            session_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.execute(
                """
                INSERT INTO messages (session_id, role, content, created_at)
                VALUES (?, 'assistant', 'reply', ?)
                """,
                (session_id, now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
