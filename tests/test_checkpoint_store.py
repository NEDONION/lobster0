"""CheckpointStore 的路径、配额、CAS 与 manifest 测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from lobster0.bootstrap import initialize_state
from lobster0.checkpoints.store import CheckpointError, CheckpointStore
from lobster0.paths import build_state_paths
from lobster0.storage.database import Database


class CheckpointStoreTest(unittest.TestCase):
    """验证 capture 只保存受限 Workspace regular file。"""

    def setUp(self) -> None:
        """初始化真实 v5 SQLite 和独立 checkpoint root。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        self.now = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
        self.store = CheckpointStore(
            self.database,
            owner_id=owner.id,
            workspace=self.paths.workspace,
            state_home=self.paths.home,
            max_entries=4,
            max_total_bytes=32,
            max_file_bytes=16,
            max_count=3,
        )

    def test_capture_records_existing_file_and_missing_tombstone(self) -> None:
        """已有文件记录 hash/blob/mode，不存在目标记录 tombstone。"""
        existing = self.paths.workspace / "note.txt"
        missing = self.paths.workspace / "new.txt"
        existing.write_text("before", encoding="utf-8")
        existing.chmod(0o640)

        manifest = self.store.capture(
            (existing, missing), reason="write_file", now=self.now
        )

        self.assertEqual([entry.path for entry in manifest.entries], ["new.txt", "note.txt"])
        tombstone, saved = manifest.entries
        self.assertFalse(tombstone.existed)
        self.assertTrue(saved.existed)
        self.assertEqual(saved.size, 6)
        self.assertEqual(saved.mode, 0o640)
        assert saved.sha256 is not None
        blob = self.paths.home / "checkpoints" / "blobs" / saved.sha256[:2] / saved.sha256
        self.assertEqual(blob.read_bytes(), b"before")
        self.assertEqual(blob.stat().st_mode & 0o777, 0o600)

    def test_same_content_deduplicates_cas_blob(self) -> None:
        """两个文件内容相同时只产生一个 hash 命名 blob。"""
        first = self.paths.workspace / "a.txt"
        second = self.paths.workspace / "b.txt"
        first.write_text("same", encoding="utf-8")
        second.write_text("same", encoding="utf-8")

        manifest = self.store.capture((first, second), reason="command", now=self.now)

        hashes = {entry.sha256 for entry in manifest.entries}
        blobs = tuple((self.paths.home / "checkpoints" / "blobs").glob("*/*"))
        self.assertEqual(len(hashes), 1)
        self.assertEqual(len(blobs), 1)

    def test_capture_rejects_symlink_secret_and_quota_overflow(self) -> None:
        """symlink、Secret path 与超限文件分别返回稳定错误码。"""
        target = self.paths.workspace / "target.txt"
        target.write_text("ok", encoding="utf-8")
        symlink = self.paths.workspace / "link.txt"
        symlink.symlink_to(target)
        env_file = self.paths.workspace / ".env.local"
        env_file.write_text("KEY=value", encoding="utf-8")
        large = self.paths.workspace / "large.txt"
        large.write_bytes(b"x" * 17)
        for path, code in (
            (symlink, "checkpoint_symlink_denied"),
            (env_file, "checkpoint_secret_path_denied"),
            (large, "checkpoint_budget_exceeded"),
        ):
            with self.subTest(path=path), self.assertRaisesRegex(CheckpointError, code):
                self.store.capture((path,), reason="tool", now=self.now)

    def test_capture_rejects_escape_git_and_too_many_entries(self) -> None:
        """Workspace escape、.git 与 entry budget 都失败关闭。"""
        outside = self.paths.home.parent / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        git_file = self.paths.workspace / ".git" / "config"
        git_file.parent.mkdir()
        git_file.write_text("config", encoding="utf-8")
        crowded = self.paths.workspace / "crowded"
        crowded.mkdir()
        for index in range(5):
            (crowded / f"{index}.txt").write_text("x", encoding="utf-8")
        for path, code in (
            (outside, "checkpoint_path_denied"),
            (git_file, "checkpoint_secret_path_denied"),
            (crowded, "checkpoint_budget_exceeded"),
        ):
            with self.subTest(path=path), self.assertRaisesRegex(CheckpointError, code):
                self.store.capture((path,), reason="tool", now=self.now)

    def test_retention_expires_oldest_manifest_without_removing_shared_blob(self) -> None:
        """超过 max_count 时只过期最旧记录，共享 CAS 仍可供新记录恢复。"""
        target = self.paths.workspace / "same.txt"
        target.write_text("same", encoding="utf-8")
        manifests = [
            self.store.capture((target,), reason=f"run-{index}", now=self.now)
            for index in range(4)
        ]

        with self.database.connect_read_only() as connection:
            statuses = connection.execute(
                "SELECT id, status FROM checkpoints ORDER BY id"
            ).fetchall()
        self.assertEqual(statuses[0]["status"], "expired")
        self.assertEqual([row["status"] for row in statuses[1:]], ["captured"] * 3)
        self.assertIsNotNone(self.store.get(manifests[-1].id))


if __name__ == "__main__":
    unittest.main()
