"""Markdown 长期/每日记忆的持久化与安全边界测试。"""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.store import MemoryError, MemoryStore
from miniclaw.paths import build_state_paths


class MemoryStoreTest(unittest.TestCase):
    """验证 Memory 可重启恢复且不把凭据或逃逸路径写入磁盘。"""

    def setUp(self) -> None:
        """创建独立状态目录和固定日期的 MemoryStore。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialize_state(self.paths)
        self.today = date(2026, 8, 8)
        self.store = MemoryStore(self.paths, today=lambda: self.today)

    def test_snapshot_loads_long_term_today_and_yesterday_only(self) -> None:
        """上下文只自动加载长期、今日和昨日，不把更早 daily 全塞进 Prompt。"""
        self.paths.memory_file.write_text("- prefers Python\n", encoding="utf-8")
        (self.paths.memory_dir / "2026-08-08.md").write_text(
            "- today fact\n", encoding="utf-8"
        )
        (self.paths.memory_dir / "2026-08-07.md").write_text(
            "- yesterday fact\n", encoding="utf-8"
        )
        (self.paths.memory_dir / "2026-08-06.md").write_text(
            "- old fact\n", encoding="utf-8"
        )

        snapshot = self.store.snapshot()

        self.assertEqual(
            [document.scope for document in snapshot.documents],
            ["long_term", "2026-08-07", "2026-08-08"],
        )
        self.assertIn("prefers Python", snapshot.text)
        self.assertIn("yesterday fact", snapshot.text)
        self.assertIn("today fact", snapshot.text)
        self.assertNotIn("old fact", snapshot.text)
        self.assertEqual(len(snapshot.content_hash), 64)

    def test_append_daily_is_private_deduplicated_and_survives_restart(self) -> None:
        """审批后的事实只追加一次，并由新 Store 从同一 Markdown 恢复。"""
        first = self.store.append_daily(
            "  I prefer   Python 3.12. ",
            source="explicit user request",
            session_id=42,
        )
        duplicate = self.store.append_daily(
            "I prefer Python 3.12.",
            source="explicit user request",
            session_id=42,
        )

        daily = self.paths.memory_dir / "2026-08-08.md"
        text = daily.read_text(encoding="utf-8")
        restored = MemoryStore(self.paths, today=lambda: self.today).read("today")

        self.assertEqual(first.status, "recorded")
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(text.count("I prefer Python 3.12."), 1)
        self.assertIn("source_session: 42", text)
        self.assertIn("confidence: confirmed", text)
        self.assertEqual(restored, text)
        self.assertEqual(daily.stat().st_mode & 0o777, 0o600)

    def test_credentials_are_rejected_without_echoing_or_writing_secret(self) -> None:
        """常见凭据形态必须 fail closed，异常和磁盘都不能回显原值。"""
        secrets = (
            "api_key = super-secret-value-123456",
            "Authorization: Bearer abcdefghijklmnop",
            "password: hunter-two-secret",
            "-----BEGIN PRIVATE KEY-----",
            "验证码: 918273",
        )
        for index, value in enumerate(secrets):
            with self.subTest(index=index), self.assertRaises(MemoryError) as caught:
                self.store.append_daily(value, source="user", session_id=1)
            self.assertEqual(caught.exception.code, "sensitive_memory")
            self.assertNotIn(value, str(caught.exception))

        self.assertFalse((self.paths.memory_dir / "2026-08-08.md").exists())

    def test_symlink_and_invalid_utf8_memory_fail_closed(self) -> None:
        """Memory 文件不能借 symlink 逃逸，损坏文本也不能静默替换。"""
        outside = self.paths.home / "outside.md"
        outside.write_text("outside secret", encoding="utf-8")
        self.paths.memory_file.unlink()
        self.paths.memory_file.symlink_to(outside)

        with self.assertRaises(MemoryError) as symlink_error:
            self.store.read("long_term")
        self.assertEqual(symlink_error.exception.code, "unsafe_memory_path")
        self.assertNotIn("outside secret", str(symlink_error.exception))

        self.paths.memory_file.unlink()
        self.paths.memory_file.write_bytes(b"valid\xffinvalid")
        with self.assertRaises(MemoryError) as encoding_error:
            self.store.read("long_term")
        self.assertEqual(encoding_error.exception.code, "invalid_memory_text")

    def test_read_truncates_context_copy_and_daily_write_respects_64_kib(self) -> None:
        """大文件保留磁盘原文，但上下文副本和 daily 追加都受 64 KiB 限制。"""
        original = "记" * 30_000
        self.paths.memory_file.write_text(original, encoding="utf-8")

        long_term = self.store.read("long_term")

        self.assertLessEqual(len(long_term.encode("utf-8")), 64 * 1024)
        self.assertEqual(self.paths.memory_file.read_text(encoding="utf-8"), original)

        daily = self.paths.memory_dir / "2026-08-08.md"
        descriptor = os.open(daily, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, b"x" * (64 * 1024 - 8))
        finally:
            os.close(descriptor)
        with self.assertRaises(MemoryError) as caught:
            self.store.append_daily("another fact", source="user", session_id=1)
        self.assertEqual(caught.exception.code, "memory_full")

    def test_read_scope_is_a_closed_enum(self) -> None:
        """模型不能把 scope 变成任意文件名或路径。"""
        with self.assertRaises(MemoryError) as caught:
            self.store.read("../../.env")

        self.assertEqual(caught.exception.code, "invalid_memory_scope")


if __name__ == "__main__":
    unittest.main()
