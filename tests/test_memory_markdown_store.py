"""Owner-scoped Markdown Memory 的原子写入与路径安全测试。"""

import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from lobster0.bootstrap import initialize_state
from lobster0.memory.markdown_store import (
    MarkdownMemoryError,
    MarkdownUnitDocument,
    MemoryMarkdownStore,
)
from lobster0.memory.models import SourceRef
from lobster0.memory.repository import MemoryManifestRepository
from lobster0.paths import build_state_paths
from lobster0.storage.database import Database


class MemoryMarkdownStoreTest(unittest.TestCase):
    """验证 Unit block 幂等、replace 原子性与 owner-only 权限。"""

    def setUp(self) -> None:
        """创建迁移完成的临时状态和固定 Unit。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.store = MemoryMarkdownStore(
            self.paths,
            MemoryManifestRepository(Database(self.paths.database)),
        )
        self.unit = MarkdownUnitDocument(
            unit_id="mem-language",
            owner_id=self.owner.id,
            key="preference.language",
            text="用户偏好中文回复",
            kind="preference",
            scope="private",
            status="active",
            confidence=1.0,
            sensitivity="low",
            valid_from=datetime(2026, 8, 9, tzinfo=UTC),
            valid_until=None,
            sources=(SourceRef(1, 1, "cli"),),
        )

    def test_append_is_idempotent_owner_only_and_manifested(self) -> None:
        """相同 Unit 只出现一次，目录/文件权限私有且 manifest 跟随新 hash。"""
        first = self.store.append(self.unit)
        second = self.store.append(self.unit)
        path = self.store.path_for_owner(self.owner.id)

        text = path.read_text(encoding="utf-8")
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(text.count("<!-- lobster0:unit mem-language -->"), 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_replace_failure_preserves_previous_truth(self) -> None:
        """temp fsync 后 replace 失败也不能破坏上一版完整 Markdown。"""
        self.store.append(self.unit)
        path = self.store.path_for_owner(self.owner.id)
        before = path.read_bytes()
        second = MarkdownUnitDocument(
            **{**self.unit.as_dict(), "unit_id": "mem-style", "text": "用户偏好简洁回答"}
        )

        with mock.patch(
            "lobster0.memory.markdown_store.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaises(MarkdownMemoryError):
                self.store.append(second)

        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(any(path.parent.glob(".memory.*.tmp")))

    def test_symlink_owner_directory_is_rejected(self) -> None:
        """Owner 路径不能借符号链接重定向到状态目录外。"""
        owners = self.paths.memory_dir / "owners"
        owners.mkdir(mode=0o700)
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        os.symlink(outside, owners / str(self.owner.id))

        with self.assertRaises(MarkdownMemoryError):
            self.store.append(self.unit)


if __name__ == "__main__":
    unittest.main()
