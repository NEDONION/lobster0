"""Memory durable buffer 的幂等捕获与绑定测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.buffer import MemoryBufferRepository, MemoryBufferStateError
from miniclaw.memory.repository import MemoryRunRepository
from miniclaw.paths import build_state_paths
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database


class MemoryBufferRepositoryTest(unittest.TestCase):
    """验证普通 Turn 只持久化 source range，不复制对话正文。"""

    def setUp(self) -> None:
        """创建一个含已持久 User Message 的 Owner Session。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "memory-buffer",
        )
        turn = TurnRepository(self.database).create_with_user_message(
            session.id,
            "buffer-source",
            "test-model",
            "private source body must not be copied",
        )
        with self.database.connect_read_only() as connection:
            self.message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?",
                    (turn.id,),
                ).fetchone()[0]
            )
        self.session_id = session.id
        self.turn_id = turn.id
        self.buffers = MemoryBufferRepository(self.database)

    def test_capture_is_idempotent_and_stores_only_source_references(self) -> None:
        """同一 Turn 重复捕获复用一行，buffer schema 不提供正文列。"""
        first = self.buffers.capture(
            owner_id=self.owner.id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            first_message_id=self.message_id,
            last_message_id=self.message_id,
            capture_scope="private",
        )
        second = self.buffers.capture(
            owner_id=self.owner.id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            first_message_id=self.message_id,
            last_message_id=self.message_id,
            capture_scope="private",
        )

        with self.database.connect_read_only() as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(memory_buffers)")
            }
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.buffers.pending_count(self.owner.id), 1)
        self.assertNotIn("content", columns)
        self.assertNotIn("text", columns)

    def test_assign_and_flush_are_atomic_and_terminal(self) -> None:
        """buffer 只能 pending→assigned→flushed，完成后不能被另一 Run 重绑。"""
        captured = self.buffers.capture(
            owner_id=self.owner.id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            first_message_id=self.message_id,
            last_message_id=self.message_id,
            capture_scope="private",
        )
        runs = MemoryRunRepository(self.database)
        run = runs.enqueue(
            owner_id=self.owner.id,
            first_message_id=self.message_id,
            last_message_id=self.message_id,
            extractor="v1",
            prompt_hash="a" * 64,
        )

        self.buffers.assign(self.owner.id, (captured.id,), run.id)
        self.buffers.mark_flushed(run.id, datetime(2026, 8, 9, tzinfo=UTC))

        self.assertEqual(self.buffers.get(captured.id).status, "flushed")
        with self.assertRaises(MemoryBufferStateError):
            self.buffers.assign(self.owner.id, (captured.id,), run.id)

    def test_cross_owner_or_mismatched_source_range_fails_closed(self) -> None:
        """Owner、Session、Turn 与 Message 关联不一致时不能写入 buffer。"""
        with self.assertRaises(MemoryBufferStateError):
            self.buffers.capture(
                owner_id=self.owner.id + 1,
                session_id=self.session_id,
                turn_id=self.turn_id,
                first_message_id=self.message_id,
                last_message_id=self.message_id,
                capture_scope="private",
            )
        with self.assertRaises(MemoryBufferStateError):
            self.buffers.capture(
                owner_id=self.owner.id,
                session_id=self.session_id,
                turn_id=self.turn_id,
                first_message_id=self.message_id + 1,
                last_message_id=self.message_id,
                capture_scope="private",
            )


if __name__ == "__main__":
    unittest.main()
