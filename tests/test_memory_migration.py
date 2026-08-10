"""Legacy MEMORY.md/daily 文件只读、hash 幂等迁移测试。"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from lobster0.bootstrap import initialize_state
from lobster0.memory.markdown_store import MemoryMarkdownStore
from lobster0.memory.migration import LegacyMemoryImporter
from lobster0.memory.repository import MemoryManifestRepository, MemoryUnitRepository
from lobster0.memory.store import MemoryStore
from lobster0.paths import build_state_paths
from lobster0.storage.database import Database


class LegacyMemoryMigrationTest(unittest.TestCase):
    """验证 legacy 原件不变、来源 hash 可核验且重跑不重复。"""

    def setUp(self) -> None:
        """创建完成迁移的临时状态与 Importer。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        self.units = MemoryUnitRepository(self.database)
        self.importer = LegacyMemoryImporter(
            self.paths,
            self.database,
            MemoryMarkdownStore(
                self.paths,
                MemoryManifestRepository(self.database),
            ),
            self.units,
            MemoryStore(self.paths),
        )

    def test_legacy_import_is_hash_idempotent_and_never_rewrites_source(self) -> None:
        """同一原件重复扫描返回相同 Unit IDs，文件字节保持不变。"""
        original = "# Long-term Memory\n\n- 用户偏好中文回答\n- 用户项目使用 Python 3.12\n"
        self.paths.memory_file.write_text(original, encoding="utf-8")

        first = self.importer.import_all(self.owner.id)
        second = self.importer.import_all(self.owner.id)

        self.assertEqual(first.unit_ids, second.unit_ids)
        self.assertEqual(len(first.unit_ids), 2)
        self.assertEqual(self.paths.memory_file.read_text(encoding="utf-8"), original)
        unit = self.units.get(self.owner.id, first.unit_ids[0])
        self.assertEqual(unit.kind, "legacy_manual")
        self.assertTrue(unit.sources)
        source_hash = hashlib.sha256(original.encode()).hexdigest()
        with self.database.connect_read_only() as connection:
            source = connection.execute(
                "SELECT content, metadata_json FROM messages WHERE id = ?",
                (unit.sources[0].message_id,),
            ).fetchone()
        self.assertIn(source_hash, source["content"])
        self.assertNotIn("用户偏好中文回答", source["content"])

    def test_sensitive_legacy_chunk_stays_only_in_untouched_original(self) -> None:
        """疑似凭据不进入 Unit/Markdown，Importer 只报告拒绝计数。"""
        secret = "sk-abcdefghijklmnop1234"
        original = f"# Long-term Memory\n\n- api_key: {secret}\n"
        self.paths.memory_file.write_text(original, encoding="utf-8")

        result = self.importer.import_all(self.owner.id)

        self.assertEqual(result.unit_ids, ())
        self.assertEqual(result.rejected_chunks, 1)
        self.assertEqual(self.paths.memory_file.read_text(encoding="utf-8"), original)
        target = self.paths.memory_dir / "owners" / str(self.owner.id) / "memory.md"
        if target.exists():
            self.assertNotIn(secret, target.read_text(encoding="utf-8"))

    def test_repeated_initialization_imports_changed_legacy_file(self) -> None:
        """真实 bootstrap 重启会扫描 legacy 新 hash，不要求手工迁移命令。"""
        original = "# Long-term Memory\n\n- 用户正在开发 Lobster0\n"
        self.paths.memory_file.write_text(original, encoding="utf-8")

        initialize_state(self.paths)

        imported = self.units.find_by_text(self.owner.id, "用户正在开发 Lobster0")
        self.assertIsNotNone(imported)
        self.assertEqual(self.paths.memory_file.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
