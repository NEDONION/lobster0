"""Memory flush、Unit 与 Review SQLite 状态机测试。"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lobster0.bootstrap import initialize_state
from lobster0.memory.models import SourceRef
from lobster0.memory.repository import (
    MemoryDataError,
    MemoryReviewRepository,
    MemoryRunRepository,
    MemoryStateError,
    MemoryUnitRepository,
)
from lobster0.paths import build_state_paths
from lobster0.storage.conversations import SessionRepository, TurnRepository
from lobster0.storage.database import Database

NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)


class MemoryRepositoryTest(unittest.TestCase):
    """验证 lease、终态不可变和 Owner 隔离的持久契约。"""

    def setUp(self) -> None:
        """创建可被 source FK 引用的真实 Owner/Session/Message。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "memory-repository",
        )
        turn = TurnRepository(self.database).create_with_user_message(
            session.id,
            "repository-source",
            "test-model",
            "用户偏好中文回复",
        )
        with self.database.connect_read_only() as connection:
            self.message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?",
                    (turn.id,),
                ).fetchone()[0]
            )
        self.session_id = session.id
        self.runs = MemoryRunRepository(self.database)

    def enqueue(self):
        """创建当前 source range 的幂等 Flush Run。"""
        return self.runs.enqueue(
            owner_id=self.owner.id,
            first_message_id=self.message_id,
            last_message_id=self.message_id,
            extractor="extractor-v1",
            prompt_hash="b" * 64,
        )

    def test_same_source_range_has_one_flush_run(self) -> None:
        """Owner、范围、提取器与 Prompt hash 共同构成持久幂等键。"""
        first = self.enqueue()
        second = self.enqueue()

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.status, "queued")

    def test_expired_lease_is_reclaimed_once(self) -> None:
        """未过期 lease 不可抢占，过期后同一 Run 只能被下一 Worker 回收。"""
        queued = self.enqueue()

        claimed = self.runs.claim_next("worker-a", now=NOW, lease_seconds=30)
        blocked = self.runs.claim_next(
            "worker-b",
            now=NOW + timedelta(seconds=29),
            lease_seconds=30,
        )
        recovered = self.runs.claim_next(
            "worker-b",
            now=NOW + timedelta(seconds=30),
            lease_seconds=30,
        )

        self.assertEqual(claimed.id, queued.id)
        self.assertIsNone(blocked)
        assert recovered is not None
        self.assertEqual(recovered.id, queued.id)
        self.assertEqual(recovered.lease_owner, "worker-b")
        self.assertEqual(recovered.attempts, 2)

    def test_retry_due_time_and_terminal_state_are_enforced(self) -> None:
        """retry 到期前不可 claim；completed Run 的任意回退都被拒绝。"""
        run = self.enqueue()
        claimed = self.runs.claim_next("worker-a", now=NOW, lease_seconds=30)
        assert claimed is not None
        self.runs.mark_retry(
            run.id,
            "worker-a",
            error_code="provider_timeout",
            next_attempt_at=NOW + timedelta(minutes=1),
            now=NOW,
        )
        self.assertIsNone(
            self.runs.claim_next(
                "worker-b",
                now=NOW + timedelta(seconds=59),
                lease_seconds=30,
            )
        )
        reclaimed = self.runs.claim_next(
            "worker-b",
            now=NOW + timedelta(minutes=1),
            lease_seconds=30,
        )
        assert reclaimed is not None
        self.runs.mark_markdown_committed(run.id, "worker-b", now=NOW)
        self.runs.complete_projection(run.id, now=NOW)

        self.assertEqual(self.runs.get(run.id).status, "completed")
        with self.assertRaises(MemoryStateError):
            self.runs.mark_retry(
                run.id,
                "worker-b",
                error_code="late_failure",
                next_attempt_at=NOW,
                now=NOW,
            )

    def test_dead_letter_is_terminal_and_cannot_be_reclaimed(self) -> None:
        """不可恢复 Run 进入 dead_letter 后不会被 lease 扫描再次执行。"""
        run = self.enqueue()
        claimed = self.runs.claim_next("worker-a", now=NOW, lease_seconds=30)
        assert claimed is not None

        terminal = self.runs.mark_dead_letter(
            run.id,
            "worker-a",
            error_code="candidate_secret",
            now=NOW,
        )

        self.assertEqual(terminal.status, "dead_letter")
        self.assertIsNone(
            self.runs.claim_next(
                "worker-b",
                now=NOW + timedelta(days=1),
                lease_seconds=30,
            )
        )

    def test_unit_sources_and_review_remain_owner_scoped(self) -> None:
        """Unit 来源、Review 和状态转换均绑定 Owner，不能跨 Owner 查询。"""
        units = MemoryUnitRepository(self.database)
        unit = units.create(
            unit_id="mem-preference-language",
            owner_id=self.owner.id,
            key="preference.language",
            text="用户偏好使用中文回复",
            kind="preference",
            scope="private",
            status="active",
            confidence=0.95,
            sensitivity="low",
            valid_from=NOW,
            valid_until=None,
            sources=(SourceRef(self.message_id, self.session_id, "cli"),),
        )
        reviews = MemoryReviewRepository(self.database)
        review = reviews.create(
            owner_id=self.owner.id,
            review_type="correction",
            preview_hash="c" * 64,
            requested_transition="superseded",
            unit_id=unit.id,
            payload={"replacement_hash": "d" * 64},
            now=NOW,
        )

        self.assertEqual(units.get(self.owner.id, unit.id).sources[0].message_id, self.message_id)
        self.assertIsNone(units.find(self.owner.id + 1, unit.id))
        self.assertEqual(reviews.get(self.owner.id, review.id).status, "pending")
        with self.assertRaises(MemoryStateError):
            reviews.get(self.owner.id + 1, review.id)

    def test_malformed_persisted_review_json_is_rejected(self) -> None:
        """SQLite 中损坏 JSON 不能被静默当成空 payload 继续审批。"""
        reviews = MemoryReviewRepository(self.database)
        review = reviews.create(
            owner_id=self.owner.id,
            review_type="weekly",
            preview_hash="e" * 64,
            requested_transition="active",
            unit_id=None,
            payload={"count": 1},
            now=NOW,
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE memory_reviews SET payload_json = '{bad' WHERE id = ?",
                (review.id,),
            )

        with self.assertRaises(MemoryDataError):
            reviews.get(self.owner.id, review.id)


if __name__ == "__main__":
    unittest.main()
