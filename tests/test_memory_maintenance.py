"""Memory TTL、周审候选与过期 lease 维护测试。"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.maintenance import MemoryMaintenance
from miniclaw.memory.markdown_store import MarkdownUnitDocument, MemoryMarkdownStore
from miniclaw.memory.models import DisclosureContext, SourceRef
from miniclaw.memory.repository import (
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryRunRepository,
    MemoryUnitRepository,
)
from miniclaw.memory.review import MemoryReviewService
from miniclaw.memory.store import MemoryStore
from miniclaw.paths import build_state_paths
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


class MemoryMaintenanceTest(unittest.TestCase):
    """验证维护动作幂等、Markdown-first 且只生成 Review。"""

    def setUp(self) -> None:
        """创建可核验 source、Repository 和 Maintenance。"""
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
        self.maintenance = MemoryMaintenance(
            self.database,
            self.markdown,
            self.units,
            self.reviews,
        )
        self.session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "maintenance",
        )
        self.turns = TurnRepository(self.database)

    def create_source(self, event_id: str, text: str) -> SourceRef:
        """创建真实 Owner User Message SourceRef。"""
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

    def create_unit(
        self,
        unit_id: str,
        text: str,
        *,
        status: str,
        valid_until: datetime | None,
    ) -> None:
        """以 Markdown-first 顺序创建维护测试 Unit。"""
        source = self.create_source(f"source-{unit_id}", text)
        document = MarkdownUnitDocument(
            unit_id,
            self.owner.id,
            f"fact.{unit_id}",
            text,
            "fact",
            "private",
            status,
            0.9,
            "low",
            NOW - timedelta(days=30),
            valid_until,
            (source,),
        )
        write = self.markdown.append(document)
        self.units.create(
            unit_id=unit_id,
            owner_id=self.owner.id,
            key=document.key,
            text=text,
            kind="fact",
            scope="private",
            status=status,
            confidence=0.9,
            sensitivity="low",
            valid_from=document.valid_from,
            valid_until=valid_until,
            sources=(source,),
            markdown_hash=write.block_hash,
            now=NOW - timedelta(days=30),
        )

    def test_expiry_updates_markdown_projection_and_audit(self) -> None:
        """到期 short-term Unit 转 expired，来源保留且不再可召回。"""
        self.create_unit(
            "mem-expiring",
            "用户临时在上海出差",
            status="short_term",
            valid_until=NOW - timedelta(seconds=1),
        )

        result = self.maintenance.run_due(self.owner.id, now=NOW)

        self.assertEqual(result.expired_unit_ids, ("mem-expiring",))
        expired = self.units.get(self.owner.id, "mem-expiring")
        self.assertEqual(expired.status, "expired")
        self.assertTrue(expired.sources)
        self.assertIn(
            '"status":"expired"',
            self.markdown.path_for_owner(self.owner.id).read_text(encoding="utf-8"),
        )

    def test_weekly_review_and_stale_lease_recovery_are_idempotent(self) -> None:
        """每周只创建一个 Review，并把过期 running lease 送回 retry。"""
        self.create_unit(
            "mem-active",
            "用户偏好中文回答",
            status="active",
            valid_until=None,
        )
        source = self.create_source("run-source", "普通对话")
        runs = MemoryRunRepository(self.database)
        run = runs.enqueue(
            owner_id=self.owner.id,
            first_message_id=source.message_id,
            last_message_id=source.message_id,
            extractor="test-extractor",
            prompt_hash="a" * 64,
            now=NOW - timedelta(minutes=5),
        )
        runs.claim_next(
            "old-worker",
            now=NOW - timedelta(minutes=5),
            lease_seconds=60,
        )

        first = self.maintenance.run_due(self.owner.id, now=NOW)
        second = self.maintenance.run_due(self.owner.id, now=NOW + timedelta(hours=1))

        self.assertIsNotNone(first.weekly_review_id)
        self.assertEqual(first.weekly_review_id, second.weekly_review_id)
        self.assertEqual(len(self.reviews.list_pending(self.owner.id)), 1)
        self.assertEqual(first.reclaimed_leases, 1)
        self.assertEqual(runs.get(run.id).status, "retry")
        assert first.weekly_review_id is not None
        disclosure = DisclosureContext(
            self.owner.id,
            self.owner.id,
            "cli",
            "local",
            True,
        )
        governance = MemoryReviewService(
            self.database,
            self.markdown,
            self.units,
            self.reviews,
            MemoryStore(self.paths),
        )
        preview = governance.get(disclosure, first.weekly_review_id)
        governance.decide(
            disclosure,
            first.weekly_review_id,
            preview.preview_hash,
            approve=False,
            now=NOW + timedelta(hours=2),
        )
        self.assertEqual(self.units.get(self.owner.id, "mem-active").status, "active")


if __name__ == "__main__":
    unittest.main()
