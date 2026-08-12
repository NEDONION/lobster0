"""Evolution Approval、原子 apply/rollback、崩溃恢复与 Runtime 读取测试。"""

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lobster0.evolution.models import (
    EvalRunStatus,
    EvolutionAction,
    EvolutionApprovalStatus,
    FeedbackRating,
    ProposalStatus,
    ProposalTargetType,
)
from lobster0.evolution.proposals import (
    PROMPT_BLOCKS,
    validate_prompt_candidate,
    validate_skill_candidate,
)
from lobster0.evolution.repository import (
    ActiveRevisionRepository,
    EvalRepository,
    EvolutionApprovalRepository,
    EvolutionError,
    FeedbackRepository,
    ProposalRepository,
)
from lobster0.evolution.revisions import (
    active_prompt_text,
    active_skill_document,
    approval_binding_hash,
    prompt_artifact_path,
    recover_active_prompt_revision,
    stale_orphan_artifacts,
    verify_prompt_artifact,
)
from lobster0.evolution.service import EvolutionService
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations
from lobster0.storage.repositories import OwnerRepository

_BLOCK = "agent-behavior"
_CANDIDATE = "Always call the listed tool before answering from local state."


class _Clock:
    """可控时钟，用于验证 TTL 过期而不需要真实等待。"""

    def __init__(self, now: datetime) -> None:
        """记录当前时间。"""
        self._now = now

    def now(self, tz=UTC) -> datetime:
        """返回当前受控时间。"""
        del tz
        return self._now

    def advance(self, seconds: int) -> None:
        """把受控时钟向前推进。"""
        self._now += timedelta(seconds=seconds)


class EvolutionApplyTestCase(unittest.TestCase):
    """搭建一条走到 evaluating 的完整 Prompt Proposal。"""

    def setUp(self) -> None:
        """创建数据库、候选 artifact、通过的 EvalRun 与 Service。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.prompt_versions = root / "prompts" / "versions"
        self.skill_versions = root / "skills" / "versions"
        self.skill_staging = root / "skill-staging"
        self.database = Database(root / "lobster0.db")
        apply_migrations(self.database)
        self.owner_id = OwnerRepository(self.database).get_or_create().id
        self.clock = _Clock(datetime(2026, 8, 11, tzinfo=UTC))

        self.proposals = ProposalRepository(self.database)
        self.evals = EvalRepository(self.database)
        self.approvals = EvolutionApprovalRepository(self.database, clock=self.clock)
        self.active = ActiveRevisionRepository(self.database)
        self.service = EvolutionService(
            proposals=self.proposals,
            evals=self.evals,
            approvals=self.approvals,
            active=self.active,
            prompt_versions_root=self.prompt_versions,
            skill_versions_root=self.skill_versions,
        )

        material = validate_prompt_candidate(self.prompt_versions, _BLOCK, _CANDIDATE)
        feedback_id = self._record_feedback()
        self.proposal, self.version = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=feedback_id,
            target_type=ProposalTargetType.PROMPT,
            target_name=_BLOCK,
            base_hash=material.base_hash,
            candidate_hash=material.candidate_hash,
            manifest_json=material.manifest_json,
            candidate_ref=material.candidate_ref,
            rationale="修复未调用工具",
        )
        self.proposals.transition(
            self.owner_id,
            self.proposal.id,
            expected_status=ProposalStatus.DRAFT,
            new_status=ProposalStatus.EVALUATING,
        )
        self.eval_run = self._passing_eval_run(self.version.id)

    def _record_feedback(self) -> int:
        """插入一条最小 assistant message 与反馈，返回反馈 ID。"""
        self._conversations = getattr(self, "_conversations", 0) + 1
        conversation = f"apply-test-{self._conversations}"
        with self.database.connect() as connection:
            session = connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, 'cli', 'local', ?, 'active',
                          '2026-08-11T00:00:00+00:00', '2026-08-11T00:00:00+00:00')
                """,
                (self.owner_id, conversation),
            ).lastrowid
            message = connection.execute(
                """
                INSERT INTO messages (session_id, role, content, created_at)
                VALUES (?, 'assistant', 'reply', '2026-08-11T00:00:00+00:00')
                """,
                (session,),
            ).lastrowid
        return FeedbackRepository(self.database).record(
            owner_id=self.owner_id,
            message_id=int(message),
            rating=FeedbackRating.BAD,
            redacted_reason="没有真正调用工具",
            context_hash="a" * 64,
        ).id

    def _passing_eval_run(self, version_id: int):
        """为一个 version 结算一次通过的 EvalRun。"""
        run = self.evals.start_run(
            proposal_version_id=version_id, suite_manifest_hash="b" * 64
        )
        return self.evals.complete_run(
            run.id,
            status=EvalRunStatus.PASSED,
            receipt_hash="c" * 64,
            total_cases=10,
            passed_cases=10,
            safety_failures=0,
            duration_ms=100,
        )

    def _independent_version(self, text: str):
        """用一条独立 Proposal 生成同目标的另一个候选版本。"""
        material = validate_prompt_candidate(self.prompt_versions, _BLOCK, text)
        _, version = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=self._record_feedback(),
            target_type=ProposalTargetType.PROMPT,
            target_name=_BLOCK,
            base_hash=material.base_hash,
            candidate_hash=material.candidate_hash,
            manifest_json=material.manifest_json,
            candidate_ref=material.candidate_ref,
            rationale="independent",
        )
        return version

    def _approved_apply(self) -> int:
        """走完 request → decide(approved)，返回可消费的 approval ID。"""
        approval = self.service.request_apply_approval(
            self.owner_id, self.proposal.id, eval_run_id=self.eval_run.id
        )
        self.proposals.transition(
            self.owner_id,
            self.proposal.id,
            expected_status=ProposalStatus.EVALUATING,
            new_status=ProposalStatus.APPROVED,
        )
        self.approvals.decide(self.owner_id, approval.id, approved=True)
        return approval.id


class ApprovalBindingTest(EvolutionApplyTestCase):
    """验证审批绑定精确 hash，且任何一项漂移都会失效。"""

    def test_preview_binds_base_candidate_and_eval_receipt(self) -> None:
        """预览必须同时绑定 base、candidate 与 eval receipt 三个哈希。"""
        preview = self.service.preview_apply(
            self.owner_id, self.proposal.id, eval_run_id=self.eval_run.id
        )

        self.assertEqual(preview.base_hash, self.version.base_hash)
        self.assertEqual(preview.candidate_hash, self.version.candidate_hash)
        self.assertEqual(preview.eval_receipt_hash, "c" * 64)
        self.assertEqual(preview.target, f"prompt:{_BLOCK}")

    def test_every_bound_field_changes_the_binding_hash(self) -> None:
        """绑定对象的每一项都必须真正参与哈希。"""
        common = {
            "action": EvolutionAction.APPLY,
            "proposal_id": 1,
            "proposal_version_ordinal": 1,
            "target_type": ProposalTargetType.PROMPT,
            "target_name": _BLOCK,
            "base_hash": "a" * 64,
            "candidate_hash": "b" * 64,
            "eval_receipt_hash": "c" * 64,
        }
        baseline = approval_binding_hash(**common)
        variants = {
            "action": {"action": EvolutionAction.ROLLBACK},
            "proposal_id": {"proposal_id": 2},
            "ordinal": {"proposal_version_ordinal": 2},
            "target_name": {"target_name": "other-block"},
            "base_hash": {"base_hash": "d" * 64},
            "candidate_hash": {"candidate_hash": "e" * 64},
            "eval_receipt_hash": {"eval_receipt_hash": "f" * 64},
        }
        for name, override in variants.items():
            with self.subTest(changed=name):
                self.assertNotEqual(baseline, approval_binding_hash(**{**common, **override}))

    def test_eval_run_from_another_version_is_rejected(self) -> None:
        """不属于当前 version 的 EvalRun 不能用于审批。"""
        other = self.evals.start_run(
            proposal_version_id=self.version.id, suite_manifest_hash="d" * 64
        )
        self.evals.complete_run(
            other.id,
            status=EvalRunStatus.FAILED,
            receipt_hash="e" * 64,
            total_cases=10,
            passed_cases=9,
            safety_failures=0,
            duration_ms=10,
        )
        with self.assertRaises(Exception) as raised:
            self.service.preview_apply(
                self.owner_id, self.proposal.id, eval_run_id=other.id
            )
        self.assertEqual(getattr(raised.exception, "code", None), "eval_run_not_passed")

    def test_apply_approval_requires_an_eval_run(self) -> None:
        """apply 审批在 Repository 层就必须拒绝缺少 eval_run 的请求。"""
        with self.assertRaises(EvolutionError) as raised:
            self.approvals.request(
                owner_id=self.owner_id,
                proposal_version_id=self.version.id,
                eval_run_id=None,
                action=EvolutionAction.APPLY,
                binding_hash="a" * 64,
                summary="apply",
                ttl_seconds=60,
            )
        self.assertEqual(raised.exception.code, "eval_receipt_required")


class ApprovalLifecycleTest(EvolutionApplyTestCase):
    """验证 TTL、单次消费与 Owner 归属。"""

    def test_expired_approval_cannot_be_decided_or_consumed(self) -> None:
        """超过 TTL 的审批必须结算为 expired，不能再批准或消费。"""
        approval = self.service.request_apply_approval(
            self.owner_id, self.proposal.id, eval_run_id=self.eval_run.id, ttl_seconds=60
        )
        self.clock.advance(61)

        with self.assertRaises(EvolutionError) as raised:
            self.approvals.decide(self.owner_id, approval.id, approved=True)
        self.assertEqual(raised.exception.code, "approval_expired")
        self.assertEqual(
            self.approvals.get(self.owner_id, approval.id).status,
            EvolutionApprovalStatus.EXPIRED,
        )

    def test_approval_is_consumed_exactly_once(self) -> None:
        """同一条审批不能被消费两次。"""
        approval_id = self._approved_apply()
        self.service.apply(self.owner_id, approval_id)

        with self.assertRaises(EvolutionError) as raised:
            self.service.apply(self.owner_id, approval_id)
        self.assertIn(
            raised.exception.code,
            {"approval_not_approved", "proposal_status_invalid"},
        )

    def test_other_owner_cannot_read_or_consume(self) -> None:
        """跨 Owner 读取审批必须拒绝。"""
        approval = self.service.request_apply_approval(
            self.owner_id, self.proposal.id, eval_run_id=self.eval_run.id
        )
        with self.assertRaises(EvolutionError) as raised:
            self.approvals.get(self.owner_id + 1, approval.id)
        self.assertEqual(raised.exception.code, "not_owner")

    def test_consume_rejects_a_drifted_binding(self) -> None:
        """执行方重算出的绑定与审批不一致时必须拒绝消费。"""
        approval_id = self._approved_apply()
        with self.assertRaises(EvolutionError) as raised:
            self.approvals.consume(
                self.owner_id, approval_id, expected_binding_hash="0" * 64
            )
        self.assertEqual(raised.exception.code, "approval_binding_mismatch")


class ApplyAndRollbackTest(EvolutionApplyTestCase):
    """验证原子切换、Runtime 读取与回滚。"""

    def test_apply_switches_pointer_and_runtime_reads_candidate(self) -> None:
        """apply 之后 Runtime 下一次读取必须拿到候选正文。"""
        self.assertEqual(
            active_prompt_text(
                self.proposals,
                self.active,
                self.prompt_versions,
                owner_id=self.owner_id,
                block_id=_BLOCK,
            ),
            PROMPT_BLOCKS[_BLOCK],
        )

        receipt = self.service.apply(self.owner_id, self._approved_apply())

        self.assertEqual(receipt.proposal_version_id, self.version.id)
        self.assertIsNone(receipt.previous_version_id)
        self.assertEqual(
            self.proposals.get(self.owner_id, self.proposal.id).status,
            ProposalStatus.APPLIED,
        )
        self.assertEqual(
            active_prompt_text(
                self.proposals,
                self.active,
                self.prompt_versions,
                owner_id=self.owner_id,
                block_id=_BLOCK,
            ),
            _CANDIDATE,
        )

    def test_rollback_restores_previous_and_runtime_reads_base(self) -> None:
        """回滚后 Runtime 必须读回上一版本；这里上一版本即内置 base。"""
        self.service.apply(self.owner_id, self._approved_apply())
        version_two = self._independent_version("A second candidate revision.")
        self.active.activate(
            self.owner_id,
            ProposalTargetType.PROMPT,
            _BLOCK,
            proposal_version_id=version_two.id,
            expected_current_version_id=self.version.id,
        )
        self.assertEqual(
            active_prompt_text(
                self.proposals,
                self.active,
                self.prompt_versions,
                owner_id=self.owner_id,
                block_id=_BLOCK,
            ),
            "A second candidate revision.",
        )

        restored = self.active.rollback(
            self.owner_id,
            ProposalTargetType.PROMPT,
            _BLOCK,
            expected_current_version_id=version_two.id,
        )

        self.assertEqual(restored.proposal_version_id, self.version.id)
        self.assertEqual(
            active_prompt_text(
                self.proposals,
                self.active,
                self.prompt_versions,
                owner_id=self.owner_id,
                block_id=_BLOCK,
            ),
            _CANDIDATE,
        )

    def test_apply_fails_closed_when_active_base_changed(self) -> None:
        """评测后有人抢先切换了 active base 时，apply 必须 fail closed。"""
        approval_id = self._approved_apply()
        other = validate_prompt_candidate(self.prompt_versions, _BLOCK, "Someone else won.")
        feedback_id = self._record_feedback()
        _, rival = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=feedback_id,
            target_type=ProposalTargetType.PROMPT,
            target_name=_BLOCK,
            base_hash=other.base_hash,
            candidate_hash=other.candidate_hash,
            manifest_json=other.manifest_json,
            candidate_ref=other.candidate_ref,
            rationale="rival",
        )
        self.active.activate(
            self.owner_id,
            ProposalTargetType.PROMPT,
            _BLOCK,
            proposal_version_id=rival.id,
            expected_current_version_id=None,
        )

        with self.assertRaises(EvolutionError) as raised:
            self.service.apply(self.owner_id, approval_id)
        self.assertEqual(raised.exception.code, "active_base_changed")


class ArtifactIntegrityTest(EvolutionApplyTestCase):
    """验证 artifact 校验、崩溃恢复与孤儿识别。"""

    def test_tampered_artifact_is_rejected_and_runtime_falls_back(self) -> None:
        """artifact 被改写后必须被识别，Runtime 回退到内置 base 而不加载它。"""
        self.service.apply(self.owner_id, self._approved_apply())
        path = prompt_artifact_path(self.prompt_versions, self.version.candidate_ref)
        path.write_text("tampered content", encoding="utf-8")

        check = verify_prompt_artifact(self.prompt_versions, self.version)

        self.assertFalse(check.valid)
        self.assertEqual(check.reason, "artifact_hash_mismatch")
        self.assertEqual(
            active_prompt_text(
                self.proposals,
                self.active,
                self.prompt_versions,
                owner_id=self.owner_id,
                block_id=_BLOCK,
            ),
            PROMPT_BLOCKS[_BLOCK],
        )

    def test_missing_artifact_is_detected(self) -> None:
        """artifact 被删除时必须报告 artifact_missing。"""
        path = prompt_artifact_path(self.prompt_versions, self.version.candidate_ref)
        path.unlink()

        check = verify_prompt_artifact(self.prompt_versions, self.version)

        self.assertFalse(check.valid)
        self.assertEqual(check.reason, "artifact_missing")

    def test_recovery_rolls_back_a_corrupted_active_pointer(self) -> None:
        """DB commit 后 artifact 损坏这一崩溃窗口：重启必须回退指针，不加载坏内容。"""
        self.service.apply(self.owner_id, self._approved_apply())
        version_two = self._independent_version("Second revision.")
        self.active.activate(
            self.owner_id,
            ProposalTargetType.PROMPT,
            _BLOCK,
            proposal_version_id=version_two.id,
            expected_current_version_id=self.version.id,
        )
        prompt_artifact_path(self.prompt_versions, version_two.candidate_ref).unlink()

        check = recover_active_prompt_revision(
            self.proposals,
            self.active,
            self.prompt_versions,
            owner_id=self.owner_id,
            block_id=_BLOCK,
        )

        self.assertFalse(check.valid)
        pointer = self.active.get(self.owner_id, ProposalTargetType.PROMPT, _BLOCK)
        self.assertEqual(pointer.proposal_version_id, self.version.id)

    def test_recovery_is_a_noop_when_artifact_is_intact(self) -> None:
        """artifact 完好时恢复不得改变指针。"""
        self.service.apply(self.owner_id, self._approved_apply())

        check = recover_active_prompt_revision(
            self.proposals,
            self.active,
            self.prompt_versions,
            owner_id=self.owner_id,
            block_id=_BLOCK,
        )

        self.assertTrue(check.valid)
        pointer = self.active.get(self.owner_id, ProposalTargetType.PROMPT, _BLOCK)
        self.assertEqual(pointer.proposal_version_id, self.version.id)

    def test_orphan_artifacts_are_reported_but_not_deleted(self) -> None:
        """stage 完成、DB commit 前崩溃留下的孤儿必须能被识别且不被自动删除。"""
        orphan_text = "An artifact nobody references."
        validate_prompt_candidate(self.prompt_versions, _BLOCK, orphan_text)
        orphan_hash = hashlib.sha256(orphan_text.encode("utf-8")).hexdigest()

        orphans = stale_orphan_artifacts(
            self.prompt_versions, referenced_hashes=frozenset({self.version.candidate_hash})
        )

        self.assertEqual([path.stem for path in orphans], [orphan_hash])
        self.assertTrue(orphans[0].is_file())

    def test_artifact_reference_cannot_escape_the_store(self) -> None:
        """受控引用不能用 .. 或绝对路径逃出 version store。"""
        for unsafe in ("../../etc/passwd", "/etc/passwd"):
            with self.subTest(unsafe=unsafe), self.assertRaises(EvolutionError) as raised:
                prompt_artifact_path(self.prompt_versions, unsafe)
            self.assertEqual(raised.exception.code, "artifact_ref_unsafe")


if __name__ == "__main__":
    unittest.main()


class SkillTargetApplyTest(EvolutionApplyTestCase):
    """Skill 目标必须能走完与 Prompt 相同的审批、应用与回滚链路。

    此前只有 Prompt 端到端可用：Skill 候选只校验调用方给的临时目录，从不复制进版本库，
    因此 apply 阶段根本不知道候选文件在哪，整条链路对 Skill 是断的。
    """

    def _stage_skill(self, name: str, body: str) -> None:
        """在隔离 staging 根下写入一个格式合法的候选 Skill。"""
        directory = self.skill_staging / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture skill\nversion: 1\n---\n\n"
            f"# Instructions\n\n{body}\n",
            encoding="utf-8",
        )

    def _skill_proposal(self, name: str = "weather-skill", body: str = "查天气。"):
        """创建一个已进入 evaluating 且带通过 EvalRun 的 Skill Proposal。"""
        self._stage_skill(name, body)
        material = validate_skill_candidate(
            self.skill_staging, versions_root=self.skill_versions
        )
        proposal, version = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=self._record_feedback(),
            target_type=ProposalTargetType.SKILL,
            target_name=name,
            base_hash=material.base_hash,
            candidate_hash=material.candidate_hash,
            manifest_json=material.manifest_json,
            candidate_ref=material.candidate_ref,
            rationale="新增查天气技能",
        )
        self.proposals.transition(
            self.owner_id,
            proposal.id,
            expected_status=ProposalStatus.DRAFT,
            new_status=ProposalStatus.EVALUATING,
        )
        return proposal, version, self._passing_eval_run(version.id)

    def test_candidate_is_persisted_into_the_version_store(self) -> None:
        """校验通过的 Skill 必须按内容哈希落进版本库，apply 才有稳定位置可读。"""
        _, version, _ = self._skill_proposal()

        stored = self.skill_versions / version.candidate_ref
        self.assertTrue(stored.is_file())
        self.assertIn("weather-skill", stored.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(stored.read_bytes()).hexdigest(), version.candidate_hash
        )

    def test_full_approve_apply_rollback_flow_for_a_skill(self) -> None:
        """Skill 必须能审批、应用、并回滚到上一版。"""
        proposal, version, run = self._skill_proposal()
        approval = self.service.request_apply_approval(
            self.owner_id, proposal.id, eval_run_id=run.id
        )
        self.proposals.transition(
            self.owner_id,
            proposal.id,
            expected_status=ProposalStatus.EVALUATING,
            new_status=ProposalStatus.APPROVED,
        )
        self.approvals.decide(self.owner_id, approval.id, approved=True)

        receipt = self.service.apply(self.owner_id, approval.id)

        self.assertEqual(receipt.target_type, ProposalTargetType.SKILL)
        pointer = self.active.get(
            self.owner_id, ProposalTargetType.SKILL, "weather-skill"
        )
        self.assertIsNotNone(pointer)
        self.assertEqual(pointer.proposal_version_id, version.id)
        self.assertEqual(
            self.proposals.get(self.owner_id, proposal.id).status,
            ProposalStatus.APPLIED,
        )

    def test_runtime_reads_the_active_skill_and_fails_safe(self) -> None:
        """Runtime 能读到生效版本；artifact 损坏时必须退化为 None 而不是加载可疑内容。"""
        proposal, version, run = self._skill_proposal(body="独特的技能正文标记")
        approval = self.service.request_apply_approval(
            self.owner_id, proposal.id, eval_run_id=run.id
        )
        self.proposals.transition(
            self.owner_id,
            proposal.id,
            expected_status=ProposalStatus.EVALUATING,
            new_status=ProposalStatus.APPROVED,
        )
        self.approvals.decide(self.owner_id, approval.id, approved=True)
        self.service.apply(self.owner_id, approval.id)

        document = active_skill_document(
            self.proposals,
            self.active,
            self.skill_versions,
            owner_id=self.owner_id,
            skill_name="weather-skill",
        )
        self.assertIsNotNone(document)
        self.assertIn("独特的技能正文标记", document)

        (self.skill_versions / version.candidate_ref).unlink()
        self.assertIsNone(
            active_skill_document(
                self.proposals,
                self.active,
                self.skill_versions,
                owner_id=self.owner_id,
                skill_name="weather-skill",
            )
        )

    def test_unknown_skill_has_no_active_document(self) -> None:
        """从未被 Evolution 改过的 Skill 必须返回 None，由调用方读磁盘常规版本。"""
        self.assertIsNone(
            active_skill_document(
                self.proposals,
                self.active,
                self.skill_versions,
                owner_id=self.owner_id,
                skill_name="never-proposed",
            )
        )

    def test_corrupted_skill_artifact_blocks_approval(self) -> None:
        """artifact 损坏时不得进入审批，必须 fail closed。"""
        proposal, version, run = self._skill_proposal()
        (self.skill_versions / version.candidate_ref).write_text("被篡改", encoding="utf-8")

        with self.assertRaises(EvolutionError) as raised:
            self.service.request_apply_approval(
                self.owner_id, proposal.id, eval_run_id=run.id
            )
        self.assertEqual(raised.exception.code, "artifact_invalid")


class MemoryTargetApplyTest(EvolutionApplyTestCase):
    """Memory 目标的应用必须穿过既有 Memory 治理，且不得假装能靠切指针回滚。"""

    class _RecordingReviews:
        """记录 Evolution 是否把决定原样转达给 Memory domain。"""

        def __init__(self, error: Exception | None = None) -> None:
            """保存可注入的 Memory 侧错误与调用记录。"""
            self.calls: list[dict[str, object]] = []
            self._error = error

        def decide(self, disclosure, review_id, preview_hash, *, approve, now):
            """记录一次调用；注入错误时原样抛出。"""
            self.calls.append(
                {
                    "owner_id": disclosure.owner_id,
                    "review_id": review_id,
                    "preview_hash": preview_hash,
                    "approve": approve,
                }
            )
            if self._error is not None:
                raise self._error

    def _memory_service(self, reviews) -> EvolutionService:
        """构造一个绑定了 Memory Review 服务的 EvolutionService。"""
        return EvolutionService(
            proposals=self.proposals,
            evals=self.evals,
            approvals=self.approvals,
            active=self.active,
            prompt_versions_root=self.prompt_versions,
            skill_versions_root=self.skill_versions,
            memory_reviews=reviews,
        )

    def _memory_proposal(self, candidate_ref: str = "memory-review:42"):
        """创建一个已进入 evaluating 且带通过 EvalRun 的 Memory Proposal。"""
        proposal, version = self.proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=self._record_feedback(),
            target_type=ProposalTargetType.MEMORY,
            target_name="unit-abc",
            base_hash="d" * 64,
            candidate_hash="e" * 64,
            manifest_json='{"kind":"memory"}',
            candidate_ref=candidate_ref,
            rationale="遗忘一条过期事实",
        )
        self.proposals.transition(
            self.owner_id,
            proposal.id,
            expected_status=ProposalStatus.DRAFT,
            new_status=ProposalStatus.EVALUATING,
        )
        return proposal, version, self._passing_eval_run(version.id)

    def _approve(self, service: EvolutionService, proposal, run) -> int:
        """走到 approved 并返回可消费的审批 ID。"""
        approval = service.request_apply_approval(
            self.owner_id, proposal.id, eval_run_id=run.id
        )
        self.proposals.transition(
            self.owner_id,
            proposal.id,
            expected_status=ProposalStatus.EVALUATING,
            new_status=ProposalStatus.APPROVED,
        )
        self.approvals.decide(self.owner_id, approval.id, approved=True)
        return approval.id

    def test_apply_delegates_to_the_memory_review_service(self) -> None:
        """应用必须把 review_id 与 candidate_hash 原样交给 Memory domain。"""
        reviews = self._RecordingReviews()
        service = self._memory_service(reviews)
        proposal, version, run = self._memory_proposal()
        approval_id = self._approve(service, proposal, run)

        receipt = service.apply(self.owner_id, approval_id)

        self.assertEqual(receipt.target_type, ProposalTargetType.MEMORY)
        self.assertEqual(len(reviews.calls), 1)
        self.assertEqual(reviews.calls[0]["review_id"], 42)
        self.assertEqual(reviews.calls[0]["preview_hash"], version.candidate_hash)
        self.assertTrue(reviews.calls[0]["approve"])
        self.assertEqual(reviews.calls[0]["owner_id"], self.owner_id)

    def test_memory_domain_rejection_surfaces_as_a_stable_code(self) -> None:
        """Memory 侧拒绝时必须转译成稳定错误码，而不是泄露内部异常。"""
        from lobster0.memory.store import MemoryError

        reviews = self._RecordingReviews(
            error=MemoryError("memory_review_preview_mismatch", "preview changed")
        )
        service = self._memory_service(reviews)
        proposal, _, run = self._memory_proposal()
        approval_id = self._approve(service, proposal, run)

        with self.assertRaises(EvolutionError) as raised:
            service.apply(self.owner_id, approval_id)
        self.assertEqual(raised.exception.code, "memory_review_preview_mismatch")

    def test_apply_without_a_memory_service_fails_closed(self) -> None:
        """未配置 Memory Review 服务时必须拒绝，而不是静默跳过治理规则。"""
        service = self._memory_service(None)
        proposal, _, run = self._memory_proposal()
        approval_id = self._approve(service, proposal, run)

        with self.assertRaises(EvolutionError) as raised:
            service.apply(self.owner_id, approval_id)
        self.assertEqual(raised.exception.code, "memory_service_unavailable")

    def test_malformed_candidate_reference_is_rejected(self) -> None:
        """候选引用不是 memory-review:<id> 形状时必须拒绝。"""
        service = self._memory_service(self._RecordingReviews())
        proposal, _, run = self._memory_proposal(candidate_ref="not-a-review-ref")
        approval_id = self._approve(service, proposal, run)

        with self.assertRaises(EvolutionError) as raised:
            service.apply(self.owner_id, approval_id)
        self.assertEqual(raised.exception.code, "memory_candidate_ref_invalid")

    def test_memory_rollback_is_explicitly_unsupported(self) -> None:
        """Memory 改动已落进 Markdown truth，切指针撤不回来，必须显式拒绝。"""
        reviews = self._RecordingReviews()
        service = self._memory_service(reviews)
        proposal, _, run = self._memory_proposal()
        service.apply(self.owner_id, self._approve(service, proposal, run))

        with self.assertRaises(EvolutionError) as raised:
            service.request_rollback_approval(self.owner_id, proposal.id)
        self.assertEqual(raised.exception.code, "memory_rollback_unsupported")


class RealMemoryCandidateApplyTest(unittest.TestCase):
    """用真实的 build_memory_forget_candidate 跑通 apply。

    真实事故：``_baseline_hash`` 对 Memory 返回空内容哈希，而真实候选生成器把 base_hash
    算成 ``sha256(unit_id:status)``。两者永远对不上，导致任何真实 Memory 候选在 apply 时
    必然 ``active_base_changed``。用手写 base_hash 的测试发现不了，必须走真实生成器。
    """

    def setUp(self) -> None:
        """初始化真实 Memory 状态并造出一条可遗忘的 Unit。"""
        from lobster0.bootstrap import initialize_state
        from lobster0.memory.markdown_store import MemoryMarkdownStore
        from lobster0.memory.models import DisclosureContext, SourceRef
        from lobster0.memory.repository import (
            MemoryManifestRepository,
            MemoryReviewRepository,
            MemoryUnitRepository,
        )
        from lobster0.memory.review import MemoryReviewService
        from lobster0.memory.service import ExplicitMemoryRequest, MemoryService
        from lobster0.memory.store import MemoryStore
        from lobster0.paths import build_state_paths
        from lobster0.storage.conversations import SessionRepository, TurnRepository

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.paths = build_state_paths((root / "state").resolve())
        self.owner_id = initialize_state(self.paths).owner.id
        self.database = Database(self.paths.database)

        units = MemoryUnitRepository(self.database)
        reviews = MemoryReviewRepository(self.database)
        markdown = MemoryMarkdownStore(self.paths, MemoryManifestRepository(self.database))
        legacy = MemoryStore(self.paths)
        memory = MemoryService(markdown, units, reviews, legacy)
        session = SessionRepository(self.database).get_or_create_cli(self.owner_id, "evo")
        turn = TurnRepository(self.database).create_with_user_message(
            session.id, "evo-src", "test-model", "请记住我喜欢简洁回答"
        )
        with self.database.connect_read_only() as connection:
            message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?", (turn.id,)
                ).fetchone()[0]
            )
        disclosure = DisclosureContext(self.owner_id, self.owner_id, "cli", "local", True)
        self.unit_id = memory.remember_explicit(
            ExplicitMemoryRequest(
                disclosure,
                SourceRef(message_id, session.id, "cli"),
                "请记住我喜欢简洁回答",
                "用户喜欢简洁回答",
                datetime(2026, 8, 11, tzinfo=UTC),
            )
        ).unit_id
        self.governance = MemoryReviewService(
            self.database, markdown, units, reviews, legacy
        )

    def test_real_forget_candidate_can_be_applied(self) -> None:
        """真实候选必须能一路走到 applied，Unit 被真正归档。"""
        from lobster0.evolution.proposals import build_memory_forget_candidate

        material = build_memory_forget_candidate(
            self.governance,
            owner_id=self.owner_id,
            unit_id=self.unit_id,
            now=datetime(2026, 8, 11, 1, tzinfo=UTC),
        )
        proposals = ProposalRepository(self.database)
        evals = EvalRepository(self.database)
        approvals = EvolutionApprovalRepository(self.database)
        active = ActiveRevisionRepository(self.database)
        service = EvolutionService(
            proposals=proposals,
            evals=evals,
            approvals=approvals,
            active=active,
            prompt_versions_root=self.paths.prompt_versions,
            skill_versions_root=self.paths.skill_versions,
            memory_reviews=self.governance,
        )
        with self.database.connect() as connection:
            message = connection.execute(
                "SELECT id FROM messages WHERE role = 'assistant' LIMIT 1"
            ).fetchone()
            if message is None:
                session = connection.execute(
                    "SELECT id FROM sessions LIMIT 1"
                ).fetchone()[0]
                message = (
                    connection.execute(
                        "INSERT INTO messages (session_id, role, content, created_at) "
                        "VALUES (?, 'assistant', 'r', '2026-08-11T00:00:00+00:00')",
                        (session,),
                    ).lastrowid,
                )
        feedback_id = FeedbackRepository(self.database).record(
            owner_id=self.owner_id,
            message_id=int(message[0]),
            rating=FeedbackRating.BAD,
            redacted_reason="这条记忆过期了",
            context_hash="a" * 64,
        ).id
        proposal, version = proposals.create_draft(
            owner_id=self.owner_id,
            feedback_id=feedback_id,
            target_type=ProposalTargetType.MEMORY,
            target_name=self.unit_id,
            base_hash=material.base_hash,
            candidate_hash=material.candidate_hash,
            manifest_json=material.manifest_json,
            candidate_ref=material.candidate_ref,
            rationale="遗忘过期记忆",
        )
        proposals.transition(
            self.owner_id,
            proposal.id,
            expected_status=ProposalStatus.DRAFT,
            new_status=ProposalStatus.EVALUATING,
        )
        run = evals.start_run(
            proposal_version_id=version.id, suite_manifest_hash="b" * 64
        )
        evals.complete_run(
            run.id,
            status=EvalRunStatus.PASSED,
            receipt_hash="c" * 64,
            total_cases=1,
            passed_cases=1,
            safety_failures=0,
            duration_ms=1,
        )
        approval = service.request_apply_approval(
            self.owner_id, proposal.id, eval_run_id=run.id
        )
        proposals.transition(
            self.owner_id,
            proposal.id,
            expected_status=ProposalStatus.EVALUATING,
            new_status=ProposalStatus.APPROVED,
        )
        approvals.decide(self.owner_id, approval.id, approved=True)

        receipt = service.apply(self.owner_id, approval.id)

        self.assertEqual(receipt.target_type, ProposalTargetType.MEMORY)
        self.assertEqual(
            proposals.get(self.owner_id, proposal.id).status, ProposalStatus.APPLIED
        )
        with self.database.connect_read_only() as connection:
            status = connection.execute(
                "SELECT status FROM memory_units WHERE id = ?", (self.unit_id,)
            ).fetchone()[0]
        self.assertEqual(status, "archived")
