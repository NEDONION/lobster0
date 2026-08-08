"""自动提取 Pipeline 的 Markdown-first、去重和重复晋升纵切测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.buffer import MemoryBufferRepository
from miniclaw.memory.extractor import ExtractedCandidate
from miniclaw.memory.flush import FlushCoordinator, FlushSourceMessage, MemoryCapture
from miniclaw.memory.markdown_store import MemoryMarkdownStore
from miniclaw.memory.models import DisclosureContext
from miniclaw.memory.pipeline import MemoryPipelineHandler
from miniclaw.memory.repository import (
    MemoryCandidateRepository,
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryRunRepository,
    MemoryUnitRepository,
)
from miniclaw.paths import build_state_paths
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database


class PreferenceExtractor:
    """始终引用当前批次首条 User Message 的 deterministic fake。"""

    async def extract(
        self,
        messages: tuple[FlushSourceMessage, ...],
    ) -> tuple[ExtractedCandidate, ...]:
        """返回一条相同低风险偏好，模拟两个独立 Turn 重复确认。"""
        source = next(message.id for message in messages if message.role == "user")
        return (
            ExtractedCandidate(
                "用户偏好使用中文回复",
                "preference",
                0.95,
                "low",
                (source,),
            ),
        )


class UnsafeExtractor:
    """返回一条 Secret 和一条 fabricated source 的 adversarial fake。"""

    async def extract(
        self,
        messages: tuple[FlushSourceMessage, ...],
    ) -> tuple[ExtractedCandidate, ...]:
        """构造必须在 Candidate Repository 前拒绝的两条候选。"""
        source = next(message.id for message in messages if message.role == "user")
        return (
            ExtractedCandidate(
                "API key: sk-abcdefghijklmnop1234",
                "fact",
                0.99,
                "low",
                (source,),
            ),
            ExtractedCandidate("用户偏好英文回复", "preference", 0.9, "low", (999_999,)),
        )


class MemoryPipelineTest(unittest.IsolatedAsyncioTestCase):
    """验证首见 short-term、重复 active 和单 Markdown Unit。"""

    def setUp(self) -> None:
        """创建真实数据库、Markdown、Repository 和 Pipeline Handler。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        self.buffers = MemoryBufferRepository(self.database)
        self.runs = MemoryRunRepository(self.database)
        self.units = MemoryUnitRepository(self.database)
        self.session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "pipeline",
        )
        self.turns = TurnRepository(self.database)
        self.markdown = MemoryMarkdownStore(
            self.paths,
            MemoryManifestRepository(self.database),
        )
        self.handler = MemoryPipelineHandler(
            self.database,
            PreferenceExtractor(),
            self.markdown,
            MemoryCandidateRepository(self.database),
            self.units,
            MemoryReviewRepository(self.database),
        )
        self.coordinator = FlushCoordinator(
            self.database,
            self.buffers,
            self.runs,
            self.handler,
            extractor="test-extractor-v1",
            prompt_hash="d" * 64,
            batch_size=1,
        )
        self.disclosure = DisclosureContext(
            self.owner.id,
            self.owner.id,
            "cli",
            "local",
            True,
        )

    def complete_and_capture(self, event_id: str) -> None:
        """完成一个真实 Turn，并创建不复制正文的 durable buffer。"""
        turn = self.turns.create_with_user_message(
            self.session.id,
            event_id,
            "test-model",
            "我偏好使用中文回复",
        )
        self.turns.mark_running(turn.id)
        self.turns.complete_with_assistant_message(
            turn.id,
            self.session.id,
            "好的",
            input_tokens=1,
            output_tokens=1,
            provider_request_id=event_id,
            iterations=1,
            finish_reason="stop",
        )
        MemoryCapture(self.buffers).capture_completed(
            owner_id=self.owner.id,
            session_id=self.session.id,
            turn_id=turn.id,
            disclosure=self.disclosure,
        )

    async def test_independent_repeat_promotes_one_unit_without_duplicate_markdown(self) -> None:
        """两个独立 User sources 合并到同一 Unit，并从 short_term 晋升 active。"""
        self.complete_and_capture("pipeline-1")
        first = await self.coordinator.run_once(
            "worker-a",
            now=datetime(2026, 8, 9, 8, 0, tzinfo=UTC),
        )
        with self.database.connect_read_only() as connection:
            unit_id = str(connection.execute("SELECT id FROM memory_units").fetchone()[0])
        self.assertEqual(first.status, "completed")
        self.assertEqual(self.units.get(self.owner.id, unit_id).status, "short_term")

        self.complete_and_capture("pipeline-2")
        second = await self.coordinator.run_once(
            "worker-b",
            now=datetime(2026, 8, 9, 8, 1, tzinfo=UTC),
        )
        unit = self.units.get(self.owner.id, unit_id)
        markdown = self.markdown.path_for_owner(self.owner.id).read_text(encoding="utf-8")

        self.assertEqual(second.status, "completed")
        self.assertEqual(unit.status, "active")
        self.assertEqual(len(unit.sources), 2)
        self.assertEqual(markdown.count(f"<!-- miniclaw:unit {unit_id} -->"), 1)

    async def test_secret_and_fabricated_source_never_enter_candidate_or_markdown(self) -> None:
        """拒绝内容只结算 source range，不保存 Candidate、Unit 或 Markdown。"""
        self.complete_and_capture("pipeline-unsafe")
        unsafe_handler = MemoryPipelineHandler(
            self.database,
            UnsafeExtractor(),
            self.markdown,
            MemoryCandidateRepository(self.database),
            self.units,
            MemoryReviewRepository(self.database),
        )
        coordinator = FlushCoordinator(
            self.database,
            self.buffers,
            self.runs,
            unsafe_handler,
            extractor="unsafe-test-v1",
            prompt_hash="e" * 64,
            batch_size=1,
        )

        outcome = await coordinator.run_once(
            "worker-unsafe",
            now=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
        )

        with self.database.connect_read_only() as connection:
            candidates = int(
                connection.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
            )
            units = int(connection.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0])
        self.assertEqual(outcome.status, "completed")
        self.assertEqual((candidates, units), (0, 0))
        self.assertFalse(self.markdown.path_for_owner(self.owner.id).exists())


if __name__ == "__main__":
    unittest.main()
