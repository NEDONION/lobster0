"""Phase 4 Channel 脱敏结构化日志与 durable Audit 测试。"""

import io
import json
import logging
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from lobster0.channels.observability import ChannelObserver
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations


class ChannelObservabilityTest(unittest.TestCase):
    """验证同一链路可关联、可查询，并且不会保存外部完整标识。"""

    def setUp(self) -> None:
        """创建隔离数据库与只写入内存的 JSON logger。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(
            Path(self.temporary_directory.name).resolve() / "lobster0.db"
        )
        apply_migrations(self.database)
        timestamp = datetime(2026, 8, 8, 9, 0, tzinfo=UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Owner', ?)",
                (timestamp,),
            )
            connection.execute(
                """
                INSERT INTO sessions (
                    id, user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (11, 1, 'feishu', 'default', 'oc_test', 'active', ?, ?)
                """,
                (timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO turns (
                    id, session_id, inbound_event_id, status, model
                ) VALUES (13, 11, 'om_test', 'completed', 'fake-model')
                """
            )
        self.stream = io.StringIO()
        self.logger = logging.Logger(f"lobster0.channel.test.{id(self)}")
        self.logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(self.stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)
        self.observer = ChannelObserver(
            self.database,
            logger=self.logger,
            clock=lambda: datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
        )

    def test_records_correlated_audit_and_redacted_json_logs(self) -> None:
        """Inbound、Turn、Delivery 应共享 correlation，日志/DB 只保留短哈希。"""
        message_id = "om_private_identifier_123"
        conversation_id = "oc_private_chat_456"

        self.observer.inbound(
            channel="feishu",
            account_id="default",
            external_message_id=message_id,
            external_conversation_id=conversation_id,
            status="accepted",
            event_row_id=7,
            enqueued=True,
        )
        self.observer.turn(
            channel="feishu",
            account_id="default",
            external_message_id=message_id,
            status="completed",
            event_row_id=7,
            session_id=11,
            turn_id=13,
            internal_message_id=17,
            queue_wait_ms=20,
            agent_duration_ms=125,
            tool_count=2,
            approval_state="none",
        )
        self.observer.delivery(
            channel="feishu",
            account_id="default",
            external_message_id=message_id,
            delivery_id=19,
            internal_message_id=17,
            status="retry_wait",
            delivery_duration_ms=50,
            attempts=2,
            error_code="feishu_rate_limited",
            retry_decision="retry",
        )

        with self.database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT event_type, metadata_json FROM audit_events ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [row["event_type"] for row in rows],
            [
                "channel.inbound.accepted",
                "channel.turn.completed",
                "channel.delivery.retry_wait",
            ],
        )
        metadata = [json.loads(row["metadata_json"]) for row in rows]
        correlation_ids = {item["correlation_id"] for item in metadata}
        self.assertEqual(len(correlation_ids), 1)
        self.assertEqual(len(metadata[0]["message_id_hash"]), 12)
        self.assertEqual(metadata[1]["agent_duration_ms"], 125)
        self.assertEqual(metadata[2]["retry_decision"], "retry")

        log_lines = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        self.assertEqual(len(log_lines), 3)
        self.assertTrue(all(line["source"] == "lobster0.channel" for line in log_lines))
        self.assertTrue(all("timestamp" in line for line in log_lines))
        combined = "\n".join(
            [self.stream.getvalue(), *(row["metadata_json"] for row in rows)]
        )
        self.assertNotIn(message_id, combined)
        self.assertNotIn(conversation_id, combined)

    def test_transport_state_is_observable_without_credentials(self) -> None:
        """连接状态只记录本地账号和稳定错误码，绝不需要 App 凭据。"""
        self.observer.transport_state(
            channel="feishu",
            account_id="default",
            state="connecting",
        )
        self.observer.transport_state(
            channel="feishu",
            account_id="default",
            state="disconnected",
            error_code="feishu_not_connected",
        )

        with self.database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT event_type, metadata_json FROM audit_events ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [row["event_type"] for row in rows],
            ["channel.transport.connecting", "channel.transport.disconnected"],
        )
        self.assertEqual(
            json.loads(rows[1]["metadata_json"])["error_code"],
            "feishu_not_connected",
        )

    def test_supervisor_states_are_durable_without_platform_identifiers(self) -> None:
        """ready/degraded/stopping 只记录平台、本地账号和稳定错误码。"""
        self.observer.supervisor(
            channel="telegram",
            account_id="personal",
            state="ready",
        )
        self.observer.supervisor(
            channel="telegram",
            account_id="personal",
            state="degraded",
            error_code="telegram_poll_failed",
        )
        self.observer.supervisor(
            channel="telegram",
            account_id="personal",
            state="stopping",
        )

        with self.database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT event_type, metadata_json FROM audit_events ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [row["event_type"] for row in rows],
            [
                "channel.supervisor.ready",
                "channel.supervisor.degraded",
                "channel.supervisor.stopping",
            ],
        )
        combined = "\n".join(row["metadata_json"] for row in rows)
        self.assertNotIn("chat:", combined)
        self.assertNotIn("message:", combined)

    def test_audit_database_failure_is_logged_without_breaking_channel(self) -> None:
        """Audit 表缺失/不可写时应输出安全降级标记，不能让业务回调失败。"""
        unavailable = Database(
            Path(self.temporary_directory.name).resolve() / "audit-unavailable.db"
        )
        observer = ChannelObserver(
            unavailable,
            logger=self.logger,
            clock=lambda: datetime(2026, 8, 8, 9, 1, tzinfo=UTC),
        )

        observer.transport_state(
            channel="feishu",
            account_id="default",
            state="connecting",
        )

        payload = json.loads(self.stream.getvalue().splitlines()[-1])
        self.assertFalse(payload["audit_persisted"])
        self.assertEqual(payload["event_type"], "channel.transport.connecting")
        self.assertNotIn("no such table", repr(payload))


if __name__ == "__main__":
    unittest.main()
