"""Channel identity、durable inbox 与 Delivery outbox Repository 测试。"""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from miniclaw.channels.base import InboundMessage
from miniclaw.storage.channels import (
    ChannelIdentityRepository,
    ChannelStateError,
    DeliveryRepository,
    InboundEventRepository,
)
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import OwnerRepository


class ChannelStorageTest(unittest.TestCase):
    """验证 Channel 队列和交付状态完全由 SQLite 恢复。"""

    def setUp(self) -> None:
        """创建 schema v2 数据库、Owner 和固定时钟。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(
            Path(self.temporary_directory.name).resolve() / "miniclaw.db"
        )
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.now = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)

    def test_channel_identity_is_idempotent_and_cannot_change_owner(self) -> None:
        """同一平台身份必须稳定映射 Owner，不能被第二个本地用户接管。"""
        repository = ChannelIdentityRepository(
            self.database,
            clock=lambda: self.now,
        )

        first = repository.get_or_create(
            self.owner.id,
            "feishu",
            "default",
            "ou_owner",
        )
        second = repository.get_or_create(
            self.owner.id,
            "feishu",
            "default",
            "ou_owner",
        )
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (display_name, created_at) VALUES ('Other', ?)",
                (self.now.isoformat(),),
            )
            other_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

        self.assertEqual(first, second)
        with self.assertRaisesRegex(ChannelStateError, "identity_owner_conflict"):
            repository.get_or_create(
                other_id,
                "feishu",
                "default",
                "ou_owner",
            )

    def test_inbound_message_id_is_primary_idempotency_key(self) -> None:
        """相同 message_id 即使 event_id 不同也不能覆盖正文或再入队。"""
        repository = InboundEventRepository(
            self.database,
            clock=lambda: self.now,
        )
        original = self._message(
            event_id="evt_first",
            message_id="om_same",
            text="original",
        )

        first = repository.record(original)
        duplicate = repository.record(
            self._message(
                event_id="evt_retry",
                message_id="om_same",
                text="must not overwrite",
            )
        )

        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.event.event_id, "evt_first")
        self.assertEqual(duplicate.event.content, "original")
        self.assertEqual(repository.list_by_status("feishu", "default", "queued"), (first.event,))

        with self.assertRaisesRegex(ChannelStateError, "event_id_conflict"):
            repository.record(
                self._message(event_id="evt_first", message_id="om_other")
            )

    def test_inbound_claim_is_fifo_conditional_and_tracks_terminal_state(self) -> None:
        """queued 只能原子进入 running，完成后不能再次被 claim。"""
        repository = InboundEventRepository(
            self.database,
            clock=lambda: self.now,
        )
        first = repository.record(
            self._message(event_id="evt_1", message_id="om_1", text="one")
        ).event
        repository.record(
            self._message(event_id="evt_2", message_id="om_2", text="two")
        )

        claimed_first = repository.claim_next("feishu", "default")
        claimed_second = repository.claim_next("feishu", "default")

        self.assertEqual(claimed_first.external_message_id, "om_1")
        self.assertEqual(claimed_first.attempts, 1)
        self.assertEqual(claimed_second.external_message_id, "om_2")
        completed = repository.mark_completed(first.key)
        self.assertEqual(completed.status, "completed")
        with self.assertRaisesRegex(ChannelStateError, "invalid_inbound_transition"):
            repository.mark_failed(first.key, "late_failure")
        self.assertIsNone(repository.claim_next("feishu", "default"))

    def test_two_connections_claim_one_inbound_exactly_once(self) -> None:
        """并发 Worker 面对同一 queued row 时必须只有一个赢家。"""
        InboundEventRepository(self.database, clock=lambda: self.now).record(
            self._message(event_id="evt_race", message_id="om_race")
        )
        barrier = Barrier(2)

        def claim() -> str | None:
            repository = InboundEventRepository(
                self.database,
                clock=lambda: self.now,
            )
            barrier.wait()
            event = repository.claim_next("feishu", "default")
            return None if event is None else event.external_message_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: claim(), range(2)))

        self.assertEqual(results.count("om_race"), 1)
        self.assertEqual(results.count(None), 1)

    def test_delivery_parts_persist_content_order_and_stable_idempotency(self) -> None:
        """全部分片必须先落库，重试与重复创建都复用相同 UUID。"""
        message_id = self._assistant_message()
        repository = DeliveryRepository(self.database, clock=lambda: self.now)

        created = repository.create_parts(
            message_id=message_id,
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_chat",
            reply_to_message_id="om_source",
            kind="message",
            contents=("第一段", "第二段"),
        )
        repeated = repository.create_parts(
            message_id=message_id,
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_chat",
            reply_to_message_id="om_source",
            kind="message",
            contents=("第一段", "第二段"),
        )

        self.assertEqual(created, repeated)
        self.assertEqual(tuple(item.content for item in created), ("第一段", "第二段"))
        self.assertEqual(tuple(item.part_index for item in created), (0, 1))
        self.assertNotEqual(created[0].idempotency_key, created[1].idempotency_key)
        self.assertTrue(all(len(item.idempotency_key) == 32 for item in created))

        first = repository.claim_next("feishu", "default")
        self.assertEqual(first.id, created[0].id)
        self.assertIsNone(repository.claim_next("feishu", "default"))
        repository.mark_sent(first.id, "om_platform_1")
        second = repository.claim_next("feishu", "default")
        self.assertEqual(second.id, created[1].id)

    def test_delivery_retry_unknown_and_restart_recovery_keep_uuid(self) -> None:
        """retry_wait/sending 必须跨 Repository 实例恢复，幂等键不能变化。"""
        message_id = self._assistant_message()
        first_repository = DeliveryRepository(self.database, clock=lambda: self.now)
        delivery = first_repository.create_parts(
            message_id=message_id,
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_chat",
            reply_to_message_id="om_source",
            kind="message",
            contents=("reply",),
        )[0]
        claimed = first_repository.claim_next("feishu", "default")
        self.assertEqual(claimed.idempotency_key, delivery.idempotency_key)
        retry_at = self.now + timedelta(seconds=30)
        first_repository.mark_retry_wait(claimed.id, "feishu_rate_limited", retry_at)

        before_due = DeliveryRepository(self.database, clock=lambda: self.now)
        self.assertIsNone(before_due.claim_next("feishu", "default"))
        after_due = DeliveryRepository(self.database, clock=lambda: retry_at)
        retried = after_due.claim_next("feishu", "default")
        self.assertEqual(retried.idempotency_key, delivery.idempotency_key)
        self.assertEqual(retried.attempts, 2)
        self.assertEqual(after_due.recover_sending("feishu", "default"), 1)
        recovered = after_due.get(retried.id)
        self.assertEqual(recovered.status, "unknown")
        self.assertEqual(recovered.last_error_code, "channel_delivery_unknown")
        self.assertEqual(recovered.idempotency_key, delivery.idempotency_key)

    def test_sent_platform_receipt_resolves_only_the_exact_approval_card(self) -> None:
        """Card callback 只能绑定同平台、账号和 receipt 的已发送审批卡。"""
        message_id = self._assistant_message()
        repository = DeliveryRepository(self.database, clock=lambda: self.now)
        delivery = repository.create_parts(
            message_id=message_id,
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_chat",
            reply_to_message_id="om_source",
            kind="approval",
            contents=("approval-envelope",),
        )[0]
        claimed = repository.claim_next("feishu", "default")
        repository.mark_sent(claimed.id, "om_approval_card")

        resolved = repository.find_sent_by_platform_message_id(
            channel="feishu",
            account_id="default",
            platform_message_id="om_approval_card",
            kind="approval",
        )

        self.assertEqual(resolved.id, delivery.id)
        self.assertIsNone(
            repository.find_sent_by_platform_message_id(
                channel="feishu",
                account_id="other",
                platform_message_id="om_approval_card",
                kind="approval",
            )
        )
        self.assertIsNone(
            repository.find_sent_by_platform_message_id(
                channel="feishu",
                account_id="default",
                platform_message_id="om_approval_card",
                kind="message",
            )
        )

    def _message(
        self,
        *,
        event_id: str,
        message_id: str,
        text: str = "hello",
    ) -> InboundMessage:
        """创建一条稳定飞书私聊内部消息。"""
        return InboundMessage(
            channel="feishu",
            account_id="default",
            event_id=event_id,
            message_id=message_id,
            external_user_id="ou_owner",
            external_conversation_id="oc_chat",
            chat_type="p2p",
            message_type="text",
            text=text,
            reply_to_message_id=message_id,
            received_at=self.now,
        )

    def _assistant_message(self) -> int:
        """创建 Delivery 外键所需的最小 Session 与 Assistant Message。"""
        now = self.now.isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, 'feishu', 'default', 'oc_chat', 'active', ?, ?)
                """,
                (self.owner.id, now, now),
            )
            session_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.execute(
                """
                INSERT INTO messages (session_id, role, content, created_at)
                VALUES (?, 'assistant', 'full reply', ?)
                """,
                (session_id, now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
