"""Feedback、Proposal、Eval 与 ActiveRevision Repository 的行为测试。"""

import tempfile
import unittest
from pathlib import Path

from lobster0.evolution.models import (
    EvalCaseStatus,
    EvalRunStatus,
    FeedbackRating,
    ProposalStatus,
    ProposalTargetType,
)
from lobster0.evolution.repository import (
    ActiveRevisionRepository,
    EvalRepository,
    EvolutionError,
    FeedbackRepository,
    ProposalRepository,
)
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations
from lobster0.storage.repositories import OwnerRepository


class EvolutionRepositoryTestCase(unittest.TestCase):
    """为每个测试创建独立、已迁移到 v7 的 SQLite 数据库和一个 Owner。"""

    def setUp(self) -> None:
        """初始化数据库、Owner 与被测 Repository。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "lobster0.db")
        apply_migrations(self.database)
        self.owner_id = OwnerRepository(self.database).get_or_create().id
        self.feedback = FeedbackRepository(self.database)
        self.proposals = ProposalRepository(self.database)
        self.evals = EvalRepository(self.database)
        self.active = ActiveRevisionRepository(self.database)
        self._next_conversation = 0

    def _insert_message(self) -> int:
        """插入一条被评价用的最小 assistant message，返回其 ID。"""
        self._next_conversation += 1
        conversation_id = f"evo-test-{self._next_conversation}"
        with self.database.connect() as connection:
            session = connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, 'cli', 'local', ?, 'active', '2026-08-11T00:00:00+00:00',
                          '2026-08-11T00:00:00+00:00')
                """,
                (self.owner_id, conversation_id),
            ).lastrowid
            message = connection.execute(
                """
                INSERT INTO messages (session_id, role, content, created_at)
                VALUES (?, 'assistant', 'reply', '2026-08-11T00:00:00+00:00')
                """,
                (session,),
            ).lastrowid
        return int(message)


class FeedbackRepositoryTest(EvolutionRepositoryTestCase):
    """验证反馈记录、去重、遗忘与 Owner 归属。"""

    def test_record_get_list_and_duplicate_rejection(self) -> None:
        """同一 Owner 对同一 message 只能记录一次反馈。"""
        message_id = self._insert_message()

        created = self.feedback.record(
            owner_id=self.owner_id,
            message_id=message_id,
            rating=FeedbackRating.BAD,
            redacted_reason="没有真正调用工具",
            context_hash="a" * 64,
        )

        self.assertEqual(created.rating, FeedbackRating.BAD)
        self.assertEqual(self.feedback.get(self.owner_id, created.id), created)
        self.assertEqual(self.feedback.list(self.owner_id), (created,))
        self.assertEqual(self.feedback.list(self.owner_id, rating=FeedbackRating.GOOD), ())

        with self.assertRaises(EvolutionError) as raised:
            self.feedback.record(
                owner_id=self.owner_id,
                message_id=message_id,
                rating=FeedbackRating.GOOD,
                redacted_reason=None,
                context_hash="b" * 64,
            )
        self.assertEqual(raised.exception.code, "feedback_already_recorded")

    def test_forget_clears_reason_but_keeps_hash_and_is_idempotent(self) -> None:
        """forget 必须清掉 reason，同时保留不可逆 hash，可重复调用。"""
        message_id = self._insert_message()
        created = self.feedback.record(
            owner_id=self.owner_id,
            message_id=message_id,
            rating=FeedbackRating.BAD,
            redacted_reason="敏感原因",
            context_hash="c" * 64,
        )

        forgotten = self.feedback.forget(self.owner_id, created.id)
        forgotten_again = self.feedback.forget(self.owner_id, created.id)

        self.assertIsNone(forgotten.redacted_reason)
        self.assertEqual(forgotten.context_hash, "c" * 64)
        self.assertIsNotNone(forgotten.forgotten_at)
        self.assertEqual(forgotten, forgotten_again)

    def test_other_owner_cannot_read_feedback(self) -> None:
        """跨 Owner 读取必须被拒绝而不是静默返回。"""
        message_id = self._insert_message()
        created = self.feedback.record(
            owner_id=self.owner_id,
            message_id=message_id,
            rating=FeedbackRating.GOOD,
            redacted_reason=None,
            context_hash="d" * 64,
        )

        with self.assertRaises(EvolutionError) as raised:
            self.feedback.get(self.owner_id + 1, created.id)
        self.assertEqual(raised.exception.code, "not_owner")


class ProposalRepositoryTest(EvolutionRepositoryTestCase):
    """验证 Proposal 创建、append-only version 与状态机跳转边界。"""

    def _feedback_id(self) -> int:
        """创建一条最小反馈并返回其 ID，供 Proposal 测试复用。"""
        message_id = self._insert_message()
        return self.feedback.record(
            owner_id=self.owner_id,
            message_id=message_id,
            rating=FeedbackRating.BAD,
            redacted_reason="reason",
            context_hash="e" * 64,
        ).id

    def test_create_draft_binds_first_immutable_version(self) -> None:
        """创建 Proposal 时必须同时生成 ordinal=1 的 version 并绑定 current_version_id。"""
        proposal, version = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=self._feedback_id(),
            target_type=ProposalTargetType.PROMPT,
            target_name="agent-behavior",
            base_hash="f" * 64,
            candidate_hash="1" * 64,
            manifest_json="{}",
            candidate_ref="staging/1",
            rationale="修复未调用工具的问题",
        )

        self.assertEqual(proposal.status, ProposalStatus.DRAFT)
        self.assertEqual(proposal.current_version_id, version.id)
        self.assertEqual(version.ordinal, 1)
        self.assertEqual(self.proposals.get_version(self.owner_id, version.id), version)

    def test_duplicate_candidate_hash_is_rejected(self) -> None:
        """完全相同内容的候选不能被当成两个不同 version。"""
        feedback_id = self._feedback_id()
        self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=feedback_id,
            target_type=ProposalTargetType.PROMPT,
            target_name="agent-behavior",
            base_hash="f" * 64,
            candidate_hash="2" * 64,
            manifest_json="{}",
            candidate_ref="staging/1",
            rationale="r1",
        )
        other_feedback_id = self.feedback.record(
            owner_id=self.owner_id,
            message_id=self._insert_message(),
            rating=FeedbackRating.BAD,
            redacted_reason="r",
            context_hash="9" * 64,
        ).id

        with self.assertRaises(EvolutionError) as raised:
            self.proposals.create_draft(
                owner_id=self.owner_id,
                feedback_id=other_feedback_id,
                target_type=ProposalTargetType.PROMPT,
                target_name="agent-behavior",
                base_hash="f" * 64,
                candidate_hash="2" * 64,
                manifest_json="{}",
                candidate_ref="staging/2",
                rationale="r2",
            )
        self.assertEqual(raised.exception.code, "candidate_hash_duplicate")

    def test_add_version_requires_evaluating_and_appends_next_ordinal(self) -> None:
        """只有 evaluating 状态可以追加修订版本，成功后状态回到 draft。"""
        proposal, _ = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=self._feedback_id(),
            target_type=ProposalTargetType.SKILL,
            target_name="weather-skill",
            base_hash="a" * 64,
            candidate_hash="3" * 64,
            manifest_json="{}",
            candidate_ref="staging/1",
            rationale="r1",
        )

        with self.assertRaises(EvolutionError) as still_draft:
            self.proposals.add_version(
                self.owner_id,
                proposal.id,
                base_hash="a" * 64,
                candidate_hash="4" * 64,
                manifest_json="{}",
                candidate_ref="staging/2",
                rationale="r2",
            )
        self.assertEqual(still_draft.exception.code, "proposal_not_evaluating")

        self.proposals.transition(
            self.owner_id,
            proposal.id,
            expected_status=ProposalStatus.DRAFT,
            new_status=ProposalStatus.EVALUATING,
        )
        second = self.proposals.add_version(
            self.owner_id,
            proposal.id,
            base_hash="a" * 64,
            candidate_hash="4" * 64,
            manifest_json="{}",
            candidate_ref="staging/2",
            rationale="r2",
        )

        self.assertEqual(second.ordinal, 2)
        refreshed = self.proposals.get(self.owner_id, proposal.id)
        self.assertEqual(refreshed.current_version_id, second.id)
        self.assertEqual(refreshed.status, ProposalStatus.DRAFT)

    def test_forbidden_jumps_are_rejected_by_transition_table(self) -> None:
        """draft -> applied 等未在状态机中列出的跳转必须被拒绝。"""
        proposal, _ = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=self._feedback_id(),
            target_type=ProposalTargetType.MEMORY,
            target_name="memory-review-1",
            base_hash="b" * 64,
            candidate_hash="6" * 64,
            manifest_json="{}",
            candidate_ref="staging/1",
            rationale="r1",
        )

        with self.assertRaises(EvolutionError) as raised:
            self.proposals.transition(
                self.owner_id,
                proposal.id,
                expected_status=ProposalStatus.DRAFT,
                new_status=ProposalStatus.APPLIED,
            )
        self.assertEqual(raised.exception.code, "proposal_transition_denied")

    def test_transition_fails_closed_when_status_already_changed(self) -> None:
        """并发把状态改走之后，旧的 expected_status 必须失败而不是覆盖。"""
        proposal, _ = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=self._feedback_id(),
            target_type=ProposalTargetType.PROMPT,
            target_name="agent-behavior",
            base_hash="c" * 64,
            candidate_hash="7" * 64,
            manifest_json="{}",
            candidate_ref="staging/1",
            rationale="r1",
        )
        self.proposals.transition(
            self.owner_id,
            proposal.id,
            expected_status=ProposalStatus.DRAFT,
            new_status=ProposalStatus.EVALUATING,
        )

        with self.assertRaises(EvolutionError) as raised:
            self.proposals.transition(
                self.owner_id,
                proposal.id,
                expected_status=ProposalStatus.DRAFT,
                new_status=ProposalStatus.EVALUATING,
            )
        self.assertEqual(raised.exception.code, "proposal_status_changed")


class EvalRepositoryTest(EvolutionRepositoryTestCase):
    """验证 EvalRun/EvalCaseResult 的追加、去重与一次性结算。"""

    def _version_id(self) -> int:
        """创建一个 Proposal 并返回其首个 version ID，供 Eval 测试复用。"""
        message_id = self._insert_message()
        feedback_id = self.feedback.record(
            owner_id=self.owner_id,
            message_id=message_id,
            rating=FeedbackRating.BAD,
            redacted_reason="r",
            context_hash="a" * 64,
        ).id
        _, version = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=feedback_id,
            target_type=ProposalTargetType.PROMPT,
            target_name="agent-behavior",
            base_hash="a" * 64,
            candidate_hash="8" * 64,
            manifest_json="{}",
            candidate_ref="staging/1",
            rationale="r1",
        )
        return version.id

    def test_case_results_reject_duplicate_case_id_within_one_run(self) -> None:
        """同一 EvalRun 内同一 case_id 只能出现一次。"""
        run = self.evals.start_run(
            proposal_version_id=self._version_id(), suite_manifest_hash="a" * 64
        )

        self.evals.record_case_result(
            run.id,
            case_id="EVO-FAILURE-000001",
            suite_version="v1",
            status=EvalCaseStatus.PASSED,
            latency_ms=10,
            input_tokens=5,
            output_tokens=5,
            result_hash="a" * 64,
        )

        with self.assertRaises(EvolutionError) as raised:
            self.evals.record_case_result(
                run.id,
                case_id="EVO-FAILURE-000001",
                suite_version="v1",
                status=EvalCaseStatus.FAILED,
                latency_ms=10,
                input_tokens=5,
                output_tokens=5,
                result_hash="b" * 64,
            )
        self.assertEqual(raised.exception.code, "eval_case_result_duplicate")

    def test_complete_run_is_single_shot(self) -> None:
        """running -> passed/failed/error 只能结算一次。"""
        run = self.evals.start_run(
            proposal_version_id=self._version_id(), suite_manifest_hash="a" * 64
        )

        completed = self.evals.complete_run(
            run.id,
            status=EvalRunStatus.PASSED,
            receipt_hash="c" * 64,
            total_cases=10,
            passed_cases=10,
            safety_failures=0,
            duration_ms=1234,
        )

        self.assertEqual(completed.status, EvalRunStatus.PASSED)
        self.assertEqual(self.evals.get_run(run.id), completed)
        with self.assertRaises(EvolutionError) as raised:
            self.evals.complete_run(
                run.id,
                status=EvalRunStatus.FAILED,
                receipt_hash="d" * 64,
                total_cases=10,
                passed_cases=0,
                safety_failures=1,
                duration_ms=1,
            )
        self.assertEqual(raised.exception.code, "eval_run_not_running")


class ActiveRevisionRepositoryTest(EvolutionRepositoryTestCase):
    """验证 active pointer 的 CAS 激活、回滚与冲突拒绝。"""

    def _version_id(self, candidate_hash: str) -> int:
        """创建一个 Proposal 并返回其首个 version ID。"""
        message_id = self._insert_message()
        feedback_id = self.feedback.record(
            owner_id=self.owner_id,
            message_id=message_id,
            rating=FeedbackRating.BAD,
            redacted_reason="r",
            context_hash=candidate_hash,
        ).id
        _, version = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=feedback_id,
            target_type=ProposalTargetType.PROMPT,
            target_name="agent-behavior",
            base_hash="a" * 64,
            candidate_hash=candidate_hash,
            manifest_json="{}",
            candidate_ref="staging/1",
            rationale="r1",
        )
        return version.id

    def test_first_activation_requires_none_and_has_no_previous(self) -> None:
        """第一次激活某个 target 时 expected_current_version_id 必须是 None。"""
        version_id = self._version_id("1" * 64)

        activated = self.active.activate(
            self.owner_id,
            ProposalTargetType.PROMPT,
            "agent-behavior",
            proposal_version_id=version_id,
            expected_current_version_id=None,
        )

        self.assertEqual(activated.proposal_version_id, version_id)
        self.assertIsNone(activated.previous_version_id)
        self.assertEqual(
            self.active.get(self.owner_id, ProposalTargetType.PROMPT, "agent-behavior"),
            activated,
        )

    def test_stale_expected_version_fails_closed_cas(self) -> None:
        """评测之后 active base 已经变化时，apply 必须 fail closed。"""
        version_a = self._version_id("2" * 64)
        version_b = self._version_id("3" * 64)
        self.active.activate(
            self.owner_id,
            ProposalTargetType.PROMPT,
            "agent-behavior",
            proposal_version_id=version_a,
            expected_current_version_id=None,
        )

        with self.assertRaises(EvolutionError) as raised:
            self.active.activate(
                self.owner_id,
                ProposalTargetType.PROMPT,
                "agent-behavior",
                proposal_version_id=version_b,
                expected_current_version_id=None,
            )
        self.assertEqual(raised.exception.code, "active_base_changed")

    def test_rollback_switches_back_and_cannot_repeat_without_new_apply(self) -> None:
        """rollback 只切回 previous revision，且不能对同一个指针重复回滚。"""
        version_a = self._version_id("4" * 64)
        version_b = self._version_id("5" * 64)
        self.active.activate(
            self.owner_id,
            ProposalTargetType.PROMPT,
            "agent-behavior",
            proposal_version_id=version_a,
            expected_current_version_id=None,
        )
        self.active.activate(
            self.owner_id,
            ProposalTargetType.PROMPT,
            "agent-behavior",
            proposal_version_id=version_b,
            expected_current_version_id=version_a,
        )

        rolled_back = self.active.rollback(
            self.owner_id,
            ProposalTargetType.PROMPT,
            "agent-behavior",
            expected_current_version_id=version_b,
        )

        self.assertEqual(rolled_back.proposal_version_id, version_a)
        self.assertIsNone(rolled_back.previous_version_id)
        with self.assertRaises(EvolutionError) as raised:
            self.active.rollback(
                self.owner_id,
                ProposalTargetType.PROMPT,
                "agent-behavior",
                expected_current_version_id=version_a,
            )
        self.assertEqual(raised.exception.code, "no_previous_revision")


if __name__ == "__main__":
    unittest.main()
