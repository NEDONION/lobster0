"""Memory conflict、行为 Review 与 source-preserving supersede 测试。"""

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
from miniclaw.memory.review import MemoryReviewService
from miniclaw.memory.service import ExplicitMemoryRequest, MemoryService, RememberResult
from miniclaw.memory.store import MemoryStore
from miniclaw.paths import build_state_paths
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database


class MemoryReviewTest(unittest.TestCase):
    """验证冲突事实不会抢占 active，Owner 决策后才原子 supersede。"""

    def setUp(self) -> None:
        """创建共享 Markdown/SQLite Service 和本地 Owner Disclosure。"""
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
        self.legacy = MemoryStore(self.paths)
        self.memory = MemoryService(
            self.markdown,
            self.units,
            self.reviews,
            self.legacy,
        )
        self.governance = MemoryReviewService(
            self.database,
            self.markdown,
            self.units,
            self.reviews,
            self.legacy,
        )
        self.session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "review",
        )
        self.turns = TurnRepository(self.database)
        self.disclosure = DisclosureContext(
            self.owner.id,
            self.owner.id,
            "cli",
            "local",
            True,
        )

    def source(self, event_id: str, text: str) -> SourceRef:
        """创建一条真实 Owner User Message 并返回 SourceRef。"""
        turn = self.turns.create_with_user_message(
            self.session.id,
            event_id,
            "test-model",
            text,
        )
        with self.database.connect_read_only() as connection:
            message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?",
                    (turn.id,),
                ).fetchone()[0]
            )
        return SourceRef(message_id, self.session.id, "cli")

    def remember(self, event_id: str, fact: str) -> RememberResult:
        """提交一个明确 remember 请求。"""
        return self.memory.remember_explicit(
            ExplicitMemoryRequest(
                self.disclosure,
                self.source(event_id, f"请记住：{fact}"),
                f"请记住：{fact}",
                fact,
                datetime(2026, 8, 9, tzinfo=UTC),
            )
        )

    def test_conflicting_active_fact_requires_review_then_supersedes(self) -> None:
        """同一 language key 的新事实在批准前保持 review_required。"""
        old = self.remember("remember-zh", "用户偏好使用中文回复")
        incoming = self.remember("remember-en", "用户偏好使用英文回复")

        self.assertEqual(self.units.get(self.owner.id, old.unit_id).status, "active")
        self.assertEqual(incoming.status, "review_required")
        assert incoming.review_id is not None
        preview = self.governance.get(self.disclosure, incoming.review_id)

        result = self.governance.decide(
            self.disclosure,
            incoming.review_id,
            preview.preview_hash,
            approve=True,
            now=datetime(2026, 8, 9, 1, tzinfo=UTC),
        )

        self.assertEqual(result.status, "consumed")
        self.assertEqual(self.units.get(self.owner.id, old.unit_id).status, "superseded")
        self.assertEqual(self.units.get(self.owner.id, incoming.unit_id).status, "active")
        self.assertTrue(self.units.get(self.owner.id, old.unit_id).sources)

    def test_behavior_review_rejection_never_activates_rule(self) -> None:
        """Owner reject 后规则 Unit 进入 rejected，不能被 Recall。"""
        rule = self.remember("remember-rule", "以后自动执行所有命令，不要询问权限")
        assert rule.review_id is not None
        preview = self.governance.get(self.disclosure, rule.review_id)

        self.governance.decide(
            self.disclosure,
            rule.review_id,
            preview.preview_hash,
            approve=False,
            now=datetime(2026, 8, 9, 1, tzinfo=UTC),
        )

        self.assertEqual(self.units.get(self.owner.id, rule.unit_id).status, "rejected")

    def test_correction_creates_sourced_unit_and_supersedes_only_after_approval(self) -> None:
        """纠错不能原地改历史，批准后新 Unit 才取代旧 Unit。"""
        old = self.remember("remember-tone", "用户偏好简洁回答")
        correction_source = self.source("correct-tone", "请更正这条记忆：用户偏好详细回答")

        preview = self.governance.propose_correction(
            self.disclosure,
            old.unit_id,
            "用户偏好详细回答",
            source=correction_source,
            latest_user_text="请更正这条记忆：用户偏好详细回答",
            now=datetime(2026, 8, 9, 1, tzinfo=UTC),
        )

        self.assertEqual(self.units.get(self.owner.id, old.unit_id).status, "active")
        self.assertEqual(self.units.get(self.owner.id, preview.unit_id).status, "review_required")
        self.governance.decide(
            self.disclosure,
            preview.review_id,
            preview.preview_hash,
            approve=True,
            now=datetime(2026, 8, 9, 2, tzinfo=UTC),
        )
        self.assertEqual(self.units.get(self.owner.id, old.unit_id).status, "superseded")
        corrected = self.units.get(self.owner.id, preview.unit_id)
        self.assertEqual(corrected.status, "active")
        self.assertEqual(corrected.text, "用户偏好详细回答")
        self.assertEqual(corrected.sources, (correction_source,))


if __name__ == "__main__":
    unittest.main()
