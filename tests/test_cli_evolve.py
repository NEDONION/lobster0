"""Lobster0 evolve CLI 子命令的行为测试。"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from lobster0.evolution.models import (  # noqa: E402
    EvalRunStatus,
    FeedbackRating,
    ProposalStatus,
    ProposalTargetType,
)
from lobster0.evolution.repository import (  # noqa: E402
    ActiveRevisionRepository,
    EvalRepository,
    FeedbackRepository,
    ProposalRepository,
)
from lobster0.paths import build_state_paths  # noqa: E402
from lobster0.storage.database import Database  # noqa: E402
from lobster0.storage.repositories import OwnerRepository  # noqa: E402
from tests.test_cli import run_cli  # noqa: E402


class CliEvolveTest(unittest.TestCase):
    """验证 evolve 每条命令只做一个动作，且高危动作必须先有审批。"""

    def setUp(self) -> None:
        """初始化状态并植入一条反馈。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.home = self.temporary_directory.name
        run_cli(["init", "--home", self.home])
        self.paths = build_state_paths(Path(self.home).resolve())
        self.database = Database(self.paths.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.candidate = Path(self.home) / "candidate.md"
        self.candidate.write_text(
            "Prefer calling a listed tool over describing manual steps.", encoding="utf-8"
        )
        self.feedback_id = self._seed_feedback()

    def _seed_feedback(self) -> int:
        """插入一条 assistant message 与 bad 反馈，返回反馈 ID。"""
        with self.database.connect() as connection:
            session = connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, 'cli', 'local', 'evolve-cli', 'active',
                          '2026-08-11T00:00:00+00:00', '2026-08-11T00:00:00+00:00')
                """,
                (self.owner.id,),
            ).lastrowid
            message = connection.execute(
                """
                INSERT INTO messages (session_id, role, content, created_at)
                VALUES (?, 'assistant', 'SECRET_SENTINEL reply', '2026-08-11T00:00:00+00:00')
                """,
                (session,),
            ).lastrowid
        return FeedbackRepository(self.database).record(
            owner_id=self.owner.id,
            message_id=int(message),
            rating=FeedbackRating.BAD,
            redacted_reason="没有真正调用工具",
            context_hash="a" * 64,
        ).id

    def _propose(self) -> tuple[int, str, str]:
        """执行 propose 并返回退出码与输出。"""
        return run_cli(
            [
                "evolve",
                "--home",
                self.home,
                "propose",
                "--feedback",
                str(self.feedback_id),
                "--candidate-file",
                str(self.candidate),
            ]
        )

    def _passing_eval_run(self) -> int:
        """直接写入一次通过的 EvalRun，避免在单测里跑完整回归。"""
        proposals = ProposalRepository(self.database)
        proposal = proposals.get(self.owner.id, 1)
        proposals.transition(
            self.owner.id,
            proposal.id,
            expected_status=ProposalStatus.DRAFT,
            new_status=ProposalStatus.EVALUATING,
        )
        evals = EvalRepository(self.database)
        run = evals.start_run(
            proposal_version_id=proposal.current_version_id, suite_manifest_hash="b" * 64
        )
        evals.complete_run(
            run.id,
            status=EvalRunStatus.PASSED,
            receipt_hash="c" * 64,
            total_cases=54,
            passed_cases=54,
            safety_failures=0,
            duration_ms=100,
        )
        return run.id

    def test_propose_writes_candidate_and_show_excludes_body(self) -> None:
        """propose 落盘候选并绑定哈希；show 不输出候选正文或被评价的回答。"""
        exit_code, stdout, _ = self._propose()

        self.assertEqual(exit_code, 0)
        self.assertIn("proposal=1", stdout)
        self.assertIn("candidate_hash=", stdout)
        self.assertNotIn("Prefer calling a listed tool", stdout)

        shown = run_cli(["evolve", "--home", self.home, "show", "1"])
        self.assertEqual(shown[0], 0)
        self.assertIn("status=draft", shown[1])
        self.assertIn("active=-", shown[1])
        self.assertNotIn("SECRET_SENTINEL", shown[1])
        self.assertNotIn("Prefer calling a listed tool", shown[1])

    def test_propose_rejects_a_diff_candidate(self) -> None:
        """候选必须是完整正文；diff/patch 形状必须被 hard deny。"""
        self.candidate.write_text(
            "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8"
        )

        exit_code, _, stderr = self._propose()

        self.assertEqual(exit_code, 4)
        self.assertIn("diff_patch_denied", stderr)

    def test_apply_requires_an_approved_approval(self) -> None:
        """未经 Owner 批准时 apply 必须拒绝，且不切换 active 指针。"""
        self._propose()
        run_id = self._passing_eval_run()
        requested = run_cli(
            [
                "evolve",
                "--home",
                self.home,
                "request",
                "1",
                "--eval-run",
                str(run_id),
            ]
        )
        self.assertEqual(requested[0], 0)
        self.assertIn("status=pending", requested[1])

        applied = run_cli(["evolve", "--home", self.home, "apply", "1"])

        self.assertEqual(applied[0], 4)
        active = ActiveRevisionRepository(self.database).get(
            self.owner.id, ProposalTargetType.PROMPT, "agent-behavior"
        )
        self.assertIsNone(active)

    def test_full_approve_apply_flow_switches_the_pointer_once(self) -> None:
        """批准后 apply 切换指针；重复 apply 必须被拒绝。"""
        self._propose()
        run_id = self._passing_eval_run()
        run_cli(
            ["evolve", "--home", self.home, "request", "1", "--eval-run", str(run_id)]
        )
        proposals = ProposalRepository(self.database)
        proposals.transition(
            self.owner.id,
            1,
            expected_status=ProposalStatus.EVALUATING,
            new_status=ProposalStatus.APPROVED,
        )
        approved = run_cli(["evolve", "--home", self.home, "approve", "1"])
        self.assertEqual(approved[0], 0)
        self.assertIn("status=approved", approved[1])

        applied = run_cli(["evolve", "--home", self.home, "apply", "1"])
        repeated = run_cli(["evolve", "--home", self.home, "apply", "1"])

        self.assertEqual(applied[0], 0)
        self.assertIn("applied proposal=1", applied[1])
        self.assertEqual(repeated[0], 4)
        active = ActiveRevisionRepository(self.database).get(
            self.owner.id, ProposalTargetType.PROMPT, "agent-behavior"
        )
        self.assertIsNotNone(active)
        self.assertEqual(
            proposals.get(self.owner.id, 1).status, ProposalStatus.APPLIED
        )

    def test_denied_approval_cannot_apply(self) -> None:
        """Owner 拒绝后 apply 必须失败。"""
        self._propose()
        run_id = self._passing_eval_run()
        run_cli(
            ["evolve", "--home", self.home, "request", "1", "--eval-run", str(run_id)]
        )
        denied = run_cli(["evolve", "--home", self.home, "deny", "1"])
        self.assertEqual(denied[0], 0)
        self.assertIn("status=denied", denied[1])

        applied = run_cli(["evolve", "--home", self.home, "apply", "1"])

        self.assertEqual(applied[0], 4)

    def test_uninitialized_state_returns_config_error(self) -> None:
        """未初始化的 home 必须返回退出码 2。"""
        with tempfile.TemporaryDirectory() as empty:
            result = run_cli(["evolve", "--home", empty, "show", "1"])
        self.assertEqual(result[0], 2)


if __name__ == "__main__":
    unittest.main()
