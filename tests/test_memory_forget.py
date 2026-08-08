"""Memory Forget preview binding、归档与跨重启测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.markdown_store import MemoryMarkdownStore
from miniclaw.memory.models import DisclosureContext, SourceRef
from miniclaw.memory.repository import (
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryUnitRepository,
)
from miniclaw.memory.retrieval import MemoryRetrieval
from miniclaw.memory.review import MemoryReviewService
from miniclaw.memory.service import ExplicitMemoryRequest, MemoryService
from miniclaw.memory.store import MemoryError, MemoryStore
from miniclaw.paths import build_state_paths
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database


class MemoryForgetTest(unittest.TestCase):
    """验证 Forget 绑定预览哈希、保留来源且从 Recall 中移除。"""

    def setUp(self) -> None:
        """创建一条 active Unit 和 Review Service。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        self.units = MemoryUnitRepository(self.database)
        self.reviews = MemoryReviewRepository(self.database)
        self.markdown = MemoryMarkdownStore(
            self.paths,
            MemoryManifestRepository(self.database),
        )
        legacy = MemoryStore(self.paths)
        memory = MemoryService(self.markdown, self.units, self.reviews, legacy)
        session = SessionRepository(self.database).get_or_create_cli(self.owner.id, "forget")
        turn = TurnRepository(self.database).create_with_user_message(
            session.id,
            "forget-source",
            "test-model",
            "请记住我喜欢简洁回答",
        )
        with self.database.connect_read_only() as connection:
            message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?",
                    (turn.id,),
                ).fetchone()[0]
            )
        self.disclosure = DisclosureContext(self.owner.id, self.owner.id, "cli", "local", True)
        self.unit_id = memory.remember_explicit(
            ExplicitMemoryRequest(
                self.disclosure,
                SourceRef(message_id, session.id, "cli"),
                "请记住我喜欢简洁回答",
                "用户喜欢简洁回答",
                datetime(2026, 8, 9, tzinfo=UTC),
            )
        ).unit_id
        self.governance = MemoryReviewService(
            self.database,
            self.markdown,
            self.units,
            self.reviews,
            legacy,
        )

    def test_forget_is_preview_bound_and_archives_across_restart(self) -> None:
        """批准后 archived Unit 保留 Source，但新 Retrieval 实例不再召回。"""
        preview = self.governance.preview_forget(
            self.disclosure,
            self.unit_id,
            now=datetime(2026, 8, 9, 1, tzinfo=UTC),
        )
        self.governance.decide(
            self.disclosure,
            preview.review_id,
            preview.preview_hash,
            approve=True,
            now=datetime(2026, 8, 9, 2, tzinfo=UTC),
        )

        archived = MemoryUnitRepository(Database(self.paths.database)).get(
            self.owner.id,
            self.unit_id,
        )
        self.assertEqual(archived.status, "archived")
        self.assertTrue(archived.sources)
        self.assertIsNone(
            MemoryRetrieval(Database(self.paths.database)).get(
                self.disclosure,
                self.unit_id,
                now=datetime(2026, 8, 9, 3, tzinfo=UTC),
            )
        )

    def test_forget_rejects_stale_preview_hash_after_target_changes(self) -> None:
        """预览后 Unit hash 变化时 fail closed，不能误删新内容。"""
        preview = self.governance.preview_forget(
            self.disclosure,
            self.unit_id,
            now=datetime(2026, 8, 9, 1, tzinfo=UTC),
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE memory_units SET text_hash = ? WHERE id = ?",
                ("f" * 64, self.unit_id),
            )

        with self.assertRaises(MemoryError) as caught:
            self.governance.decide(
                self.disclosure,
                preview.review_id,
                preview.preview_hash,
                approve=True,
                now=datetime(2026, 8, 9, 2, tzinfo=UTC),
            )

        self.assertEqual(caught.exception.code, "memory_review_target_changed")
        self.assertEqual(self.units.get(self.owner.id, self.unit_id).status, "active")

    def test_rejected_forget_can_be_previewed_again_without_replaying_decision(self) -> None:
        """Reject 保留 Unit 后，新请求必须获得新 Review，旧按钮保持失效。"""
        first = self.governance.preview_forget(
            self.disclosure,
            self.unit_id,
            now=datetime(2026, 8, 9, 1, tzinfo=UTC),
        )
        self.governance.decide(
            self.disclosure,
            first.review_id,
            first.preview_hash,
            approve=False,
            now=datetime(2026, 8, 9, 2, tzinfo=UTC),
        )

        second = self.governance.preview_forget(
            self.disclosure,
            self.unit_id,
            now=datetime(2026, 8, 9, 3, tzinfo=UTC),
        )

        self.assertNotEqual(second.review_id, first.review_id)
        self.assertNotEqual(second.preview_hash, first.preview_hash)
        with self.assertRaises(MemoryError):
            self.governance.decide(
                self.disclosure,
                first.review_id,
                first.preview_hash,
                approve=True,
                now=datetime(2026, 8, 9, 4, tzinfo=UTC),
            )
        self.governance.decide(
            self.disclosure,
            second.review_id,
            second.preview_hash,
            approve=True,
            now=datetime(2026, 8, 9, 4, tzinfo=UTC),
        )
        self.assertEqual(self.units.get(self.owner.id, self.unit_id).status, "archived")


if __name__ == "__main__":
    unittest.main()
