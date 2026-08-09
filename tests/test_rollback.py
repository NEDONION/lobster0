"""RollbackService 两步预览、冲突检测与恢复测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.checkpoints.rollback import RollbackConflictError, RollbackService
from miniclaw.checkpoints.store import CheckpointStore
from miniclaw.paths import build_state_paths
from miniclaw.storage.database import Database


class RollbackServiceTest(unittest.TestCase):
    """验证 preview hash 绑定当前状态，apply 恢复精确 before image。"""

    def setUp(self) -> None:
        """创建包含 modify/create/delete 三种 before 状态的 checkpoint。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        self.store = CheckpointStore(
            self.database,
            owner_id=owner.id,
            workspace=self.paths.workspace,
            state_home=self.paths.home,
            max_entries=20,
            max_total_bytes=1024,
            max_file_bytes=512,
            max_count=10,
        )
        self.modified = self.paths.workspace / "modified.txt"
        self.deleted = self.paths.workspace / "deleted.txt"
        self.created = self.paths.workspace / "created.txt"
        self.modified.write_text("before-modified", encoding="utf-8")
        self.deleted.write_text("before-deleted", encoding="utf-8")
        self.modified.chmod(0o640)
        self.checkpoint = self.store.capture(
            (self.modified, self.deleted, self.created),
            reason="tool",
            now=datetime(2026, 8, 9, 8, 0, tzinfo=UTC),
        )
        self.modified.write_text("after", encoding="utf-8")
        self.modified.chmod(0o600)
        self.deleted.unlink()
        self.created.write_text("created-after", encoding="utf-8")
        self.rollback = RollbackService(self.store)

    def test_preview_and_apply_restore_modify_delete_create_and_mode(self) -> None:
        """apply 恢复原内容/mode、重建删除文件并删除原 tombstone 目标。"""
        preview = self.rollback.preview(self.checkpoint.id)

        self.assertEqual(
            [(operation.path, operation.action) for operation in preview.operations],
            [
                ("created.txt", "delete"),
                ("deleted.txt", "restore"),
                ("modified.txt", "restore"),
            ],
        )
        receipt = self.rollback.apply(self.checkpoint.id, preview.sha256)

        self.assertEqual(self.modified.read_text(encoding="utf-8"), "before-modified")
        self.assertEqual(self.modified.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.deleted.read_text(encoding="utf-8"), "before-deleted")
        self.assertFalse(self.created.exists())
        self.assertEqual(receipt.changed_paths, preview.changed_paths)

    def test_rollback_refuses_when_file_changed_after_preview(self) -> None:
        """preview 后的任意并发编辑必须拒绝整批且其他文件不变。"""
        preview = self.rollback.preview(self.checkpoint.id)
        self.modified.write_text("concurrent edit", encoding="utf-8")
        deleted_still_missing = not self.deleted.exists()
        created_before = self.created.read_text(encoding="utf-8")

        with self.assertRaisesRegex(RollbackConflictError, "rollback_conflict"):
            self.rollback.apply(self.checkpoint.id, preview.sha256)

        self.assertEqual(self.modified.read_text(encoding="utf-8"), "concurrent edit")
        self.assertEqual(not self.deleted.exists(), deleted_still_missing)
        self.assertEqual(self.created.read_text(encoding="utf-8"), created_before)

    def test_rollback_rejects_symlink_inserted_after_preview(self) -> None:
        """攻击者把目标换成 symlink 时不能跟随到 Workspace 外。"""
        preview = self.rollback.preview(self.checkpoint.id)
        outside = self.paths.home.parent / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        self.modified.unlink()
        self.modified.symlink_to(outside)

        with self.assertRaisesRegex(RollbackConflictError, "rollback_conflict"):
            self.rollback.apply(self.checkpoint.id, preview.sha256)

        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")


if __name__ == "__main__":
    unittest.main()
