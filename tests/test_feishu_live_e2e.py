"""Feishu Live E2E 的只读取证、进程与报告安全契约。"""

import importlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths
from miniclaw.storage.database import Database


class FeishuDatabaseProbeTest(unittest.TestCase):
    """保证 Live 自动证据只看 checkpoint 后的真实 Feishu SQLite 状态。"""

    def setUp(self) -> None:
        """创建真实 schema v2 与最小 Owner。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialize_state(self.paths)
        self.database = Database(self.paths.database)
        self.now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC).isoformat()

    def test_checkpoint_ignores_old_rows_and_detects_new_completed_chain(self) -> None:
        """旧数据或其他 Channel 不能满足本轮 Inbox/Turn/Tool/Delivery/Audit 证据。"""
        api = self._api()
        self._insert_chain(channel="feishu", suffix="old", tool_name="system_info")
        checkpoint = api.capture_checkpoint(self.paths.database)
        self._insert_chain(channel="telegram", suffix="peer", tool_name="system_info")

        before = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            (
                "inbox_completed",
                "turn_completed",
                "delivery_sent",
                "system_info_succeeded",
                "gateway_ready",
                "transport_reconnected",
            ),
        )
        self.assertEqual(before.passed, ())
        self.assertEqual(
            before.failed,
            (
                "inbox_completed",
                "turn_completed",
                "delivery_sent",
                "system_info_succeeded",
                "gateway_ready",
                "transport_reconnected",
            ),
        )

        self._insert_chain(channel="feishu", suffix="new", tool_name="system_info")
        self._insert_audit("channel.supervisor.ready")
        self._insert_audit("channel.transport.reconnecting")
        self._insert_audit("channel.transport.connected")
        after = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            before.failed,
        )

        self.assertEqual(after.passed, before.failed)
        self.assertEqual(after.failed, ())

    def test_multi_turn_approval_and_delivery_evidence_require_complete_shapes(self) -> None:
        """上下文、审批与分片必须满足完整关联，不能用单行或失败状态凑证据。"""
        api = self._api()
        checkpoint = api.capture_checkpoint(self.paths.database)
        session_id = self._insert_session("feishu", "context")
        for index in range(3):
            self._insert_turn(session_id, f"om_context_{index}", status="completed")
        approval_turn = self._insert_turn(session_id, "om_approval", status="completed")
        tool_run_id = self._insert_tool_run(
            approval_turn,
            "write_file",
            status="succeeded",
        )
        self._insert_tool_run(approval_turn, "read_file", status="succeeded")
        self._insert_approval(approval_turn, tool_run_id, status="consumed")
        pending_turn = self._insert_turn(session_id, "om_pending", status="waiting_approval")
        pending_tool = self._insert_tool_run(
            pending_turn,
            "write_file",
            status="waiting_approval",
        )
        self._insert_approval(pending_turn, pending_tool, status="pending")
        denied_turn = self._insert_turn(session_id, "om_denied", status="waiting_approval")
        denied_tool = self._insert_tool_run(
            denied_turn,
            "write_file",
            status="denied",
        )
        self._insert_approval(denied_turn, denied_tool, status="denied")
        message_id = self._insert_message(approval_turn, session_id)
        self._insert_delivery(message_id, 0, status="sent")
        self._insert_delivery(message_id, 1, status="sent")
        self._insert_audit("channel.supervisor.ready")
        self._insert_audit("channel.supervisor.ready")

        result = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            (
                "one_session_three_turns",
                "read_file_succeeded",
                "approval_pending",
                "approval_consumed_once",
                "approval_denied",
                "multiple_parts_sent",
                "memory_survived_restart",
            ),
        )

        self.assertEqual(
            result.passed,
            (
                "one_session_three_turns",
                "read_file_succeeded",
                "approval_pending",
                "approval_consumed_once",
                "approval_denied",
                "multiple_parts_sent",
                "memory_survived_restart",
            ),
        )
        self.assertEqual(result.failed, ())

    def test_no_new_turn_is_feishu_scoped_and_flips_after_a_real_turn(self) -> None:
        """静默证据可以忽略 peer Channel，但任何新 Feishu Turn 都必须使其失败。"""
        api = self._api()
        checkpoint = api.capture_checkpoint(self.paths.database)
        peer_session = self._insert_session("discord", "peer")
        self._insert_turn(peer_session, "discord-message", status="completed")

        silent = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            ("no_new_turn",),
        )
        self.assertEqual(silent.passed, ("no_new_turn",))

        session_id = self._insert_session("feishu", "unexpected")
        self._insert_turn(session_id, "om_unexpected", status="completed")
        noisy = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            ("no_new_turn",),
        )
        self.assertEqual(noisy.failed, ("no_new_turn",))

    def test_unknown_key_and_database_failure_return_only_stable_codes(self) -> None:
        """错误不能回显绝对数据库路径、SQL、正文或外部平台标识。"""
        api = self._api()
        checkpoint = api.capture_checkpoint(self.paths.database)

        with self.assertRaises(api.FeishuLiveError) as unknown:
            api.evaluate_local_evidence(
                self.paths.database,
                checkpoint,
                ("not_a_live_fact",),
            )
        self.assertEqual(unknown.exception.code, "unknown_local_evidence")
        self.assertEqual(str(unknown.exception), "unknown_local_evidence")

        missing = Path(self.temporary_directory.name) / "private-owner" / "missing.db"
        with self.assertRaises(api.FeishuLiveError) as unavailable:
            api.capture_checkpoint(missing)
        self.assertEqual(unavailable.exception.code, "evidence_database_unavailable")
        rendered = str(unavailable.exception)
        self.assertNotIn("private-owner", rendered)
        self.assertNotIn("missing.db", rendered)

    def _api(self) -> ModuleType:
        """导入真实生产模块；缺失功能时转成清晰的 RED failure。"""
        try:
            return importlib.import_module("miniclaw.evals.feishu_live")
        except ModuleNotFoundError as error:
            self.fail(f"Feishu Live evidence module is missing: {error.name}")

    def _insert_chain(self, *, channel: str, suffix: str, tool_name: str) -> None:
        """插入一条手工推导的真实 schema 关联链。"""
        session_id = self._insert_session(channel, suffix)
        external_message_id = f"om_{suffix}" if channel == "feishu" else f"msg_{suffix}"
        turn_id = self._insert_turn(session_id, external_message_id, status="completed")
        self._insert_event(channel, suffix, session_id, external_message_id)
        self._insert_tool_run(turn_id, tool_name, status="succeeded")
        message_id = self._insert_message(turn_id, session_id)
        self._insert_delivery(message_id, 0, status="sent", channel=channel)

    def _insert_session(self, channel: str, suffix: str) -> int:
        """插入测试会话并返回内部 ID。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (1, ?, 'default', ?, 'active', ?, ?)
                """,
                (channel, f"conversation_{suffix}", self.now, self.now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_turn(self, session_id: int, inbound: str, *, status: str) -> int:
        """插入指定状态 Turn。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO turns (
                    session_id, inbound_event_id, status, model,
                    started_at, completed_at, runtime_snapshot_json
                ) VALUES (?, ?, ?, 'test-model', ?, ?, '{}')
                """,
                (session_id, inbound, status, self.now, self.now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_event(
        self,
        channel: str,
        suffix: str,
        session_id: int,
        external_message_id: str,
    ) -> None:
        """插入 completed Channel Inbox。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO processed_events (
                    channel, account_id, event_id, external_message_id,
                    session_id, received_at, external_user_id,
                    external_conversation_id, chat_type, message_type, content,
                    reply_to_message_id, status, attempts, updated_at
                ) VALUES (?, 'default', ?, ?, ?, ?, ?, ?, 'p2p', 'text', ?, ?,
                          'completed', 1, ?)
                """,
                (
                    channel,
                    f"event_{suffix}",
                    external_message_id,
                    session_id,
                    self.now,
                    f"user_{suffix}",
                    f"conversation_{suffix}",
                    f"synthetic-content-{suffix}",
                    external_message_id,
                    self.now,
                ),
            )

    def _insert_tool_run(self, turn_id: int, name: str, *, status: str) -> int:
        """插入 ToolRun 并返回内部 ID。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_runs (
                    turn_id, tool_call_id, tool_name, arguments_json,
                    arguments_hash, policy_action, status, created_at, completed_at
                ) VALUES (?, ?, ?, '{}', ?, 'allow', ?, ?, ?)
                """,
                (turn_id, f"call_{turn_id}_{name}", name, "a" * 64, status, self.now, self.now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_approval(self, turn_id: int, tool_run_id: int, *, status: str) -> None:
        """插入与 ToolRun 一对一绑定的审批事实。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    user_id, turn_id, tool_run_id, tool_name, arguments_hash,
                    summary, status, expires_at, decided_at, created_at
                ) VALUES (1, ?, ?, 'write_file', ?, 'safe summary', ?, ?, ?, ?)
                """,
                (turn_id, tool_run_id, "a" * 64, status, self.now, self.now, self.now),
            )

    def _insert_message(self, turn_id: int, session_id: int) -> int:
        """插入内部 Assistant Message。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (session_id, turn_id, role, content, metadata_json, created_at)
                VALUES (?, ?, 'assistant', 'synthetic answer', '{}', ?)
                """,
                (session_id, turn_id, self.now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_delivery(
        self,
        message_id: int,
        part_index: int,
        *,
        status: str,
        channel: str = "feishu",
    ) -> None:
        """插入真实 v2 Outbox 分片。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO deliveries (
                    message_id, channel, account_id, external_conversation_id,
                    reply_to_message_id, delivery_kind, part_index, content,
                    content_hash, idempotency_key, status, attempts,
                    created_at, updated_at, sent_at
                ) VALUES (?, ?, 'default', 'conversation', 'reply', 'message', ?,
                          'synthetic delivery', ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    message_id,
                    channel,
                    part_index,
                    f"hash-{message_id}-{part_index}",
                    f"key-{channel}-{message_id}-{part_index}",
                    status,
                    self.now,
                    self.now,
                    self.now,
                ),
            )

    def _insert_audit(self, event_type: str) -> None:
        """插入不含外部 ID 的 Feishu Audit。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_type, summary, metadata_json, created_at
                ) VALUES (?, ?, '{"channel":"feishu"}', ?)
                """,
                (event_type, event_type.replace(".", " "), self.now),
            )


if __name__ == "__main__":
    unittest.main()
