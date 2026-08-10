"""Memory Markdown 手工编辑对账与 fail-closed Projection 测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from lobster0.bootstrap import initialize_state
from lobster0.memory.markdown_store import MemoryMarkdownStore
from lobster0.memory.models import DisclosureContext, SourceRef
from lobster0.memory.reconcile import MemoryReconciler
from lobster0.memory.repository import (
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryUnitRepository,
)
from lobster0.memory.retrieval import MemoryRetrieval, SearchRequest
from lobster0.memory.service import ExplicitMemoryRequest, MemoryService
from lobster0.memory.store import MemoryStore
from lobster0.paths import build_state_paths
from lobster0.storage.conversations import SessionRepository, TurnRepository
from lobster0.storage.database import Database


class MemoryReconcileTest(unittest.TestCase):
    """验证合法 direct edit 更新 Projection，坏文件保留上一版结果。"""

    def setUp(self) -> None:
        """创建一条 active Unit 及其 manifest。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        manifests = MemoryManifestRepository(self.database)
        self.markdown = MemoryMarkdownStore(self.paths, manifests)
        self.units = MemoryUnitRepository(self.database)
        session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "reconcile",
        )
        turn = TurnRepository(self.database).create_with_user_message(
            session.id,
            "reconcile-source",
            "test-model",
            "请记住我偏好简洁回答",
        )
        with self.database.connect_read_only() as connection:
            message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?",
                    (turn.id,),
                ).fetchone()[0]
            )
        self.disclosure = DisclosureContext(
            self.owner.id,
            self.owner.id,
            "cli",
            "local",
            True,
        )
        self.unit_id = MemoryService(
            self.markdown,
            self.units,
            MemoryReviewRepository(self.database),
            MemoryStore(self.paths),
        ).remember_explicit(
            ExplicitMemoryRequest(
                self.disclosure,
                SourceRef(message_id, session.id, "cli"),
                "请记住我偏好简洁回答",
                "用户偏好简洁回答",
                datetime(2026, 8, 9, tzinfo=UTC),
            )
        ).unit_id
        self.reconciler = MemoryReconciler(
            self.database,
            self.markdown,
            manifests,
        )

    def test_valid_manual_edit_rebuilds_projection_with_redacted_audit(self) -> None:
        """直接修改可见正文后，Unit/FTS 更新且审计不复制正文。"""
        path = self.markdown.path_for_owner(self.owner.id)
        original = path.read_text(encoding="utf-8")
        path.write_text(
            original.replace("用户偏好简洁回答", "用户偏好详细回答"),
            encoding="utf-8",
        )

        result = self.reconciler.scan(self.owner.id)

        self.assertEqual(result.updated, (self.unit_id,))
        self.assertFalse(result.errors)
        self.assertEqual(self.units.get(self.owner.id, self.unit_id).text, "用户偏好详细回答")
        recalled = MemoryRetrieval(self.database).search(
            SearchRequest(self.disclosure, "详细回答", 5),
            now=datetime(2026, 8, 10, tzinfo=UTC),
        )
        self.assertEqual(recalled.items[0].unit.id, self.unit_id)
        with self.database.connect_read_only() as connection:
            audit = connection.execute(
                "SELECT event_type, metadata_json FROM memory_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(audit["event_type"], "manual_edit")
        self.assertNotIn("详细回答", str(audit["metadata_json"]))

    def test_malformed_manual_edit_preserves_file_and_last_valid_projection(self) -> None:
        """缺失结束 marker 时只记录安全错误，不能覆盖文件或 Unit。"""
        path = self.markdown.path_for_owner(self.owner.id)
        malformed = path.read_text(encoding="utf-8").replace(
            f"<!-- lobster0:end {self.unit_id} -->",
            "<!-- broken -->",
        )
        path.write_text(malformed, encoding="utf-8")

        result = self.reconciler.scan(self.owner.id)

        self.assertEqual(path.read_text(encoding="utf-8"), malformed)
        self.assertEqual(self.units.get(self.owner.id, self.unit_id).text, "用户偏好简洁回答")
        self.assertEqual(result.updated, ())
        self.assertEqual(result.errors[0].code, "memory_markdown_invalid")
        self.assertGreater(result.errors[0].line, 0)


if __name__ == "__main__":
    unittest.main()
