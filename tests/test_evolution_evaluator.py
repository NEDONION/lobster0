"""Controlled Evolution 确定性 Gate、预算与 receipt 绑定测试。"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lobster0 import __version__
from lobster0.evals.cases import load_cases
from lobster0.evals.runner import EvalCaseResult as RunnerCaseResult
from lobster0.evals.runner import EvalSuiteResult
from lobster0.evolution.evaluator import (
    EvaluationBudget,
    EvaluationError,
    case_result_hash,
    eval_receipt_hash,
    evaluate_gate,
    evaluate_proposal_version,
    latest_passing_run,
    suite_manifest_hash,
)
from lobster0.evolution.models import (
    EvalCaseStatus,
    EvalRunStatus,
    FeedbackRating,
    ProposalTargetType,
)
from lobster0.evolution.repository import (
    EvalRepository,
    FeedbackRepository,
    ProposalRepository,
)
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations
from lobster0.storage.repositories import OwnerRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = PROJECT_ROOT / "evals" / "scenarios"


def _runner_case(case_id: str, *, passed: bool, duration_ms: int = 5) -> RunnerCaseResult:
    """构造一条最小的 Runner case 判定。"""
    return RunnerCaseResult(
        case_id=case_id,
        passed=passed,
        duration_ms=duration_ms,
        failures=() if passed else ("expected_answer",),
        tool_runs=(),
        audit_events=(),
        approval_statuses=(),
        request_count=1,
        memory_evidence=(),
    )


def _suite(*results: RunnerCaseResult, duration_ms: int = 10) -> EvalSuiteResult:
    """把若干 case 判定聚合成 Runner suite 结果。"""
    passed = sum(result.passed for result in results)
    return EvalSuiteResult(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        duration_ms=duration_ms,
        cases=results,
    )


class SuiteManifestHashTest(unittest.TestCase):
    """验证 manifest 哈希对 case 文件的任何变化都敏感。"""

    def setUp(self) -> None:
        """创建一个隔离的场景目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "a.v1.jsonl").write_text('{"id": "A-001"}\n', encoding="utf-8")

    def test_hash_is_stable_and_content_sensitive(self) -> None:
        """同内容哈希稳定；修改任一 case 文件必须改变 manifest。"""
        first = suite_manifest_hash(self.root)
        self.assertEqual(first, suite_manifest_hash(self.root))

        (self.root / "a.v1.jsonl").write_text('{"id": "A-002"}\n', encoding="utf-8")
        self.assertNotEqual(first, suite_manifest_hash(self.root))

    def test_adding_a_case_file_changes_the_manifest(self) -> None:
        """新增 case 文件必须改变 manifest，避免"减少 case"逃过门禁。"""
        first = suite_manifest_hash(self.root)
        (self.root / "b.v1.jsonl").write_text('{"id": "B-001"}\n', encoding="utf-8")
        self.assertNotEqual(first, suite_manifest_hash(self.root))

    def test_missing_or_empty_root_fails_closed(self) -> None:
        """目录不存在或没有任何 versioned case 都必须 fail closed。"""
        with self.assertRaises(EvaluationError) as missing:
            suite_manifest_hash(self.root / "does-not-exist")
        self.assertEqual(missing.exception.code, "suite_root_invalid")

        empty = Path(self.temporary_directory.name) / "empty"
        empty.mkdir()
        with self.assertRaises(EvaluationError) as no_cases:
            suite_manifest_hash(empty)
        self.assertEqual(no_cases.exception.code, "suite_empty")

    def test_real_repository_suite_is_hashable(self) -> None:
        """仓库真实场景目录必须能计算出 manifest。"""
        self.assertRegex(suite_manifest_hash(SCENARIO_ROOT), r"\A[0-9a-f]{64}\Z")


class GateDecisionTest(unittest.TestCase):
    """验证确定性 Gate 的四类稳定违规码。"""

    def setUp(self) -> None:
        """加载真实 case 以获得安全子集的真实 ID。"""
        self.cases = load_cases(SCENARIO_ROOT)
        self.safety = next(case for case in self.cases if case.capability == "safety")
        self.other = next(case for case in self.cases if case.capability != "safety")

    def test_all_green_passes_with_no_violations(self) -> None:
        """全部通过且未超预算时不应有任何违规码。"""
        suite = _suite(
            _runner_case(self.safety.id, passed=True),
            _runner_case(self.other.id, passed=True),
        )

        outcome = evaluate_gate(self.cases, suite, EvaluationBudget())

        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.violations, ())
        self.assertEqual(outcome.safety_failures, 0)

    def test_any_regression_fails_the_gate(self) -> None:
        """非安全 case 失败必须记为 regression_failed，但不计入 safety_failures。"""
        suite = _suite(
            _runner_case(self.safety.id, passed=True),
            _runner_case(self.other.id, passed=False),
        )

        outcome = evaluate_gate(self.cases, suite, EvaluationBudget())

        self.assertFalse(outcome.passed)
        self.assertIn("regression_failed", outcome.violations)
        self.assertEqual(outcome.safety_failures, 0)

    def test_one_safety_failure_is_counted_and_blocks(self) -> None:
        """安全 case 失败必须同时记 regression_failed 与 safety_failed。"""
        suite = _suite(
            _runner_case(self.safety.id, passed=False),
            _runner_case(self.other.id, passed=True),
        )

        outcome = evaluate_gate(self.cases, suite, EvaluationBudget())

        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.safety_failures, 1)
        self.assertIn("safety_failed", outcome.violations)
        self.assertIn("regression_failed", outcome.violations)

    def test_duration_budget_is_enforced(self) -> None:
        """超过时间预算即使全绿也必须拦截。"""
        suite = _suite(_runner_case(self.other.id, passed=True), duration_ms=5_000)

        outcome = evaluate_gate(
            self.cases, suite, EvaluationBudget(max_total_duration_ms=1_000)
        )

        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.violations, ("duration_budget_exceeded",))

    def test_empty_suite_cannot_manufacture_a_pass(self) -> None:
        """0/0 不能被当作通过。"""
        outcome = evaluate_gate((), _suite(), EvaluationBudget())

        self.assertFalse(outcome.passed)
        self.assertIn("suite_empty", outcome.violations)

    def test_budget_rejects_non_positive_bound(self) -> None:
        """预算上限必须是正整数，不能被 0/负数/bool 绕过。"""
        for invalid in (0, -1, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                EvaluationBudget(max_total_duration_ms=invalid)


class ReceiptBindingTest(unittest.TestCase):
    """验证 receipt 绑定全部输入，任一变化都会失效。"""

    def setUp(self) -> None:
        """构造一份基准 receipt 输入。"""
        self.outcome = evaluate_gate(
            (), _suite(_runner_case("CORE-001", passed=True)), EvaluationBudget()
        )
        self.kwargs = {
            "proposal_version_id": 9,
            "suite_manifest_hash": "a" * 64,
            "case_result_hashes": ("b" * 64,),
            "budget": EvaluationBudget(max_total_duration_ms=60_000),
            "runner_version": __version__,
            "outcome": self.outcome,
        }

    def test_receipt_is_stable_for_identical_inputs(self) -> None:
        """同输入必须得到同 receipt。"""
        self.assertEqual(eval_receipt_hash(**self.kwargs), eval_receipt_hash(**self.kwargs))

    def test_every_bound_input_changes_the_receipt(self) -> None:
        """version、manifest、case 结果、预算、Runner 版本、判定各自都必须绑定。"""
        baseline = eval_receipt_hash(**self.kwargs)
        variants = {
            "proposal_version_id": {"proposal_version_id": 10},
            "suite_manifest_hash": {"suite_manifest_hash": "c" * 64},
            "case_result_hashes": {"case_result_hashes": ("d" * 64,)},
            "budget": {"budget": EvaluationBudget(max_total_duration_ms=30_000)},
            "runner_version": {"runner_version": "0.0.0"},
            "outcome": {"outcome": replace(self.outcome, passed=False)},
        }
        for name, override in variants.items():
            with self.subTest(changed=name):
                self.assertNotEqual(baseline, eval_receipt_hash(**{**self.kwargs, **override}))

    def test_case_result_hash_is_status_sensitive(self) -> None:
        """同一 case 的 passed 与 failed 不能得到相同结果哈希。"""
        passed = case_result_hash("CORE-001", status=EvalCaseStatus.PASSED, failures=())
        failed = case_result_hash(
            "CORE-001", status=EvalCaseStatus.FAILED, failures=("expected_answer",)
        )

        self.assertNotEqual(passed, failed)
        self.assertRegex(passed, r"\A[0-9a-f]{64}\Z")


class EvaluateProposalVersionTest(unittest.IsolatedAsyncioTestCase):
    """验证完整编排：写 EvalRun、逐 case 结果与最终结算。"""

    def setUp(self) -> None:
        """创建已迁移数据库、一个 Proposal 与其首个 version。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "lobster0.db")
        apply_migrations(self.database)
        owner = OwnerRepository(self.database).get_or_create()
        with self.database.connect() as connection:
            session = connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, 'cli', 'local', 'evo-eval', 'active',
                          '2026-08-11T00:00:00+00:00', '2026-08-11T00:00:00+00:00')
                """,
                (owner.id,),
            ).lastrowid
            message = connection.execute(
                """
                INSERT INTO messages (session_id, role, content, created_at)
                VALUES (?, 'assistant', 'reply', '2026-08-11T00:00:00+00:00')
                """,
                (session,),
            ).lastrowid
        feedback = FeedbackRepository(self.database).record(
            owner_id=owner.id,
            message_id=int(message),
            rating=FeedbackRating.BAD,
            redacted_reason="reason",
            context_hash="a" * 64,
        )
        _, version = ProposalRepository(self.database).create_draft(
            owner_id=owner.id,
            feedback_id=feedback.id,
            target_type=ProposalTargetType.PROMPT,
            target_name="agent-behavior",
            base_hash="b" * 64,
            candidate_hash="c" * 64,
            manifest_json="{}",
            candidate_ref="agent-behavior/c.md",
            rationale="r1",
        )
        self.version_id = version.id
        self.evals = EvalRepository(self.database)

    async def test_all_green_run_is_recorded_and_receipt_is_reproducible(self) -> None:
        """全绿评测必须结算为 passed，并写入逐 case 结果与可复算 receipt。"""
        cases = load_cases(SCENARIO_ROOT)
        active = tuple(
            case
            for case in cases
            if case.status == "active" and "offline" in case.layers
        )
        suite = _suite(*(_runner_case(case.id, passed=True) for case in active))

        async def runner(selected):
            """返回预置全绿结果，避免真实跑完整 Agent 回归。"""
            self.assertEqual(tuple(case.id for case in selected), tuple(case.id for case in active))
            return suite

        receipt = await evaluate_proposal_version(
            self.evals,
            proposal_version_id=self.version_id,
            suite_root=SCENARIO_ROOT,
            suite_runner=runner,
        )

        self.assertTrue(receipt.outcome.passed)
        self.assertEqual(receipt.runner_version, __version__)
        self.assertEqual(receipt.suite_manifest_hash, suite_manifest_hash(SCENARIO_ROOT))
        stored = self.evals.get_run(1)
        self.assertEqual(stored.status, EvalRunStatus.PASSED)
        self.assertEqual(stored.receipt_hash, receipt.receipt_hash)
        self.assertEqual(stored.total_cases, len(active))
        self.assertEqual(len(self.evals.list_case_results(stored.id)), len(active))
        self.assertEqual(latest_passing_run(self.evals, stored.id).id, stored.id)

    async def test_failed_gate_is_recorded_and_blocks_approval_lookup(self) -> None:
        """有失败 case 时必须结算为 failed，且不能被当作可审批的 passed run。"""
        cases = load_cases(SCENARIO_ROOT)
        active = tuple(
            case
            for case in cases
            if case.status == "active" and "offline" in case.layers
        )
        results = [_runner_case(case.id, passed=True) for case in active]
        results[0] = _runner_case(active[0].id, passed=False)

        async def runner(selected):
            """返回含一条失败 case 的结果。"""
            del selected
            return _suite(*results)

        receipt = await evaluate_proposal_version(
            self.evals,
            proposal_version_id=self.version_id,
            suite_root=SCENARIO_ROOT,
            suite_runner=runner,
        )

        self.assertFalse(receipt.outcome.passed)
        stored = self.evals.get_run(1)
        self.assertEqual(stored.status, EvalRunStatus.FAILED)
        with self.assertRaises(EvaluationError) as raised:
            latest_passing_run(self.evals, stored.id)
        self.assertEqual(raised.exception.code, "eval_run_not_passed")

    async def test_only_offline_layer_cases_reach_the_offline_runner(self) -> None:
        """channel/browser 层的 case 不能被喂给离线 runner，否则会产生假失败。"""
        seen: list[str] = []

        async def runner(selected):
            """记录每条被送进 runner 的 case 是否属于 offline 层。"""
            seen.extend(
                case.id for case in selected if "offline" not in case.layers
            )
            self.assertTrue(selected)
            return _suite(*(_runner_case(case.id, passed=True) for case in selected))

        await evaluate_proposal_version(
            self.evals,
            proposal_version_id=self.version_id,
            suite_root=SCENARIO_ROOT,
            suite_runner=runner,
        )

        self.assertEqual(seen, [])

    async def test_runner_crash_settles_the_run_as_error(self) -> None:
        """Runner 抛错时 EvalRun 不能停留在 running，必须结算为 error。"""

        async def runner(selected):
            """模拟 Runner 内部崩溃。"""
            del selected
            raise RuntimeError("runner exploded")

        with self.assertRaises(RuntimeError):
            await evaluate_proposal_version(
                self.evals,
                proposal_version_id=self.version_id,
                suite_root=SCENARIO_ROOT,
                suite_runner=runner,
            )

        stored = self.evals.get_run(1)
        self.assertEqual(stored.status, EvalRunStatus.ERROR)
        self.assertIsNone(stored.receipt_hash)


if __name__ == "__main__":
    unittest.main()
