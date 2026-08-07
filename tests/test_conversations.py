"""CLI 会话、消息与 Turn Repository 的事务行为测试。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths
from miniclaw.providers.base import ModelMessage, ToolCall
from miniclaw.storage.conversations import (
    MessageRepository,
    SessionRepository,
    TurnRepository,
)
from miniclaw.storage.database import Database


class ConversationRepositoryTest(unittest.TestCase):
    """验证 SQLite 会话记录可重复读取且状态变更保持原子。"""

    def setUp(self) -> None:
        """创建带完整 Schema 与唯一 Owner 的临时数据库。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(self.paths)
        self.owner = initialized.owner
        self.database = Database(self.paths.database)
        self.sessions = SessionRepository(self.database)
        self.messages = MessageRepository(self.database)
        self.turns = TurnRepository(self.database)

    def test_cli_session_is_idempotent_for_owner_and_conversation(self) -> None:
        """相同 Owner 与 CLI conversation ID 必须复用 Session，而不是切断历史。"""
        first = self.sessions.get_or_create_cli(self.owner.id, "default")
        second = self.sessions.get_or_create_cli(self.owner.id, "default")
        other = self.sessions.get_or_create_cli(self.owner.id, "project-b")

        self.assertEqual(first.id, second.id)
        self.assertNotEqual(first.id, other.id)
        self.assertEqual(first.channel, "cli")
        self.assertEqual(first.account_id, "local")

    def test_recent_messages_are_returned_oldest_to_newest_after_limit(self) -> None:
        """SQL limit 应选择最新记录，但 Context 必须按对话时间正序接收。"""
        session = self.sessions.get_or_create_cli(self.owner.id, "default")
        for index in range(3):
            turn = self.turns.create_with_user_message(
                session.id,
                f"event-{index}",
                "deepseek-v4-pro",
                f"user-{index}",
            )
            self.turns.mark_running(turn.id)
            self.turns.complete_with_assistant_message(
                turn.id,
                session.id,
                f"assistant-{index}",
                input_tokens=1,
                output_tokens=1,
                provider_request_id=f"req-{index}",
                iterations=1,
                finish_reason="stop",
            )

        recent = self.messages.list_recent(session.id, limit=3)

        self.assertEqual(
            [(message.role, message.content) for message in recent],
            [
                ("assistant", "assistant-1"),
                ("user", "user-2"),
                ("assistant", "assistant-2"),
            ],
        )

    def test_completion_writes_assistant_usage_and_snapshot_atomically(self) -> None:
        """Assistant Message 与 completed Turn 必须在同一事务中可见。"""
        session = self.sessions.get_or_create_cli(self.owner.id, "default")
        turn = self.turns.create_with_user_message(
            session.id,
            "event-complete",
            "deepseek-v4-pro",
            "hello",
        )
        self.turns.mark_running(turn.id)

        assistant = self.turns.complete_with_assistant_message(
            turn.id,
            session.id,
            "world",
            input_tokens=10,
            output_tokens=4,
            provider_request_id="req_1",
            iterations=2,
            finish_reason="stop",
        )

        saved = self.turns.get(turn.id)
        self.assertEqual(saved.status, "completed")
        self.assertEqual((saved.input_tokens, saved.output_tokens), (10, 4))
        self.assertEqual(saved.runtime_snapshot["provider_request_id"], "req_1")
        self.assertEqual(saved.runtime_snapshot["iterations"], 2)
        self.assertEqual(assistant.content, "world")
        self.assertEqual(assistant.provider_message_id, "req_1")

    def test_completion_constraint_error_rolls_back_message_and_status(self) -> None:
        """Assistant 插入失败时不能留下 completed Turn 或半条消息。"""
        session = self.sessions.get_or_create_cli(self.owner.id, "default")
        turn = self.turns.create_with_user_message(
            session.id,
            "event-rollback",
            "deepseek-v4-pro",
            "hello",
        )
        self.turns.mark_running(turn.id)

        with self.assertRaises(sqlite3.IntegrityError):
            self.turns.complete_with_assistant_message(
                turn.id,
                session.id,
                None,  # type: ignore[arg-type]
                input_tokens=1,
                output_tokens=1,
                provider_request_id=None,
                iterations=1,
                finish_reason="stop",
            )

        saved = self.turns.get(turn.id)
        recent = self.messages.list_recent(session.id)
        self.assertEqual(saved.status, "running")
        self.assertEqual([message.role for message in recent], ["user"])

    def test_completion_persists_tool_conversation_in_one_transaction(self) -> None:
        """Assistant Tool Call、Tool Result 和最终回答必须按顺序一起保存。"""
        session = self.sessions.get_or_create_cli(self.owner.id, "tool-history")
        turn = self.turns.create_with_user_message(
            session.id,
            "event-tool-history",
            "deepseek-v4-pro",
            "查看配置",
        )
        self.turns.mark_running(turn.id)
        intermediate = (
            ModelMessage(
                role="assistant",
                content="",
                tool_calls=(ToolCall("call_1", "system_info", {}),),
                reasoning_content="need actual data",
            ),
            ModelMessage(
                role="tool",
                content='{"ok":true,"tool":"system_info","data":{}}',
                tool_call_id="call_1",
            ),
        )

        self.turns.complete_with_assistant_message(
            turn.id,
            session.id,
            "你的电脑是……",
            intermediate_messages=intermediate,
            input_tokens=10,
            output_tokens=4,
            provider_request_id="req_1",
            iterations=2,
            finish_reason="stop",
        )

        saved = self.messages.list_recent(session.id)
        self.assertEqual(
            [message.role for message in saved],
            ["user", "assistant", "tool", "assistant"],
        )
        calls = saved[1].metadata["tool_calls"]
        self.assertIsInstance(calls, list)
        assert isinstance(calls, list)
        self.assertIsInstance(calls[0], dict)
        assert isinstance(calls[0], dict)
        self.assertEqual(calls[0]["name"], "system_info")
        self.assertEqual(saved[1].metadata["reasoning_content"], "need actual data")
        self.assertEqual(saved[2].tool_call_id, "call_1")

    def test_failure_and_cancellation_store_terminal_state(self) -> None:
        """失败与取消使用不同状态，并保存安全错误码供 CLI/回放区分。"""
        session = self.sessions.get_or_create_cli(self.owner.id, "default")
        failed = self.turns.create_with_user_message(
            session.id,
            "event-failed",
            "deepseek-v4-pro",
            "first",
        )
        cancelled = self.turns.create_with_user_message(
            session.id,
            "event-cancelled",
            "deepseek-v4-pro",
            "second",
        )
        self.turns.mark_running(failed.id)
        self.turns.mark_running(cancelled.id)

        self.turns.fail(failed.id, "provider_timeout", "model provider request timed out")
        self.turns.cancel(cancelled.id)

        self.assertEqual(self.turns.get(failed.id).status, "failed")
        self.assertEqual(self.turns.get(failed.id).error_code, "provider_timeout")
        self.assertEqual(self.turns.get(cancelled.id).status, "cancelled")


if __name__ == "__main__":
    unittest.main()
