"""Controlled Evolution 的唯一业务 Facade：预览、审批、原子应用与回滚。

安全不变量：Agent/Provider 只能到达 Task 2～4 的 record/propose/evaluate 路径；本模块的
``preview_apply`` / ``apply`` / ``rollback`` 只由本机 CLI 调用，且每一次真正切换指针前都会
重新校验 Approval 归属、TTL、精确绑定哈希、eval receipt 与 active base，任何一项不符即
fail closed，不留半应用状态。
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lobster0.evolution.evaluator import EvaluationError, latest_passing_run
from lobster0.evolution.models import (
    EvolutionAction,
    EvolutionApproval,
    Proposal,
    ProposalStatus,
    ProposalTargetType,
    ProposalVersion,
)
from lobster0.evolution.proposals import PROMPT_BLOCKS
from lobster0.evolution.repository import (
    ActiveRevisionRepository,
    EvalRepository,
    EvolutionApprovalRepository,
    EvolutionError,
    ProposalRepository,
)
from lobster0.evolution.revisions import (
    ApplyReceipt,
    approval_binding_hash,
    verify_prompt_artifact,
)

_DEFAULT_TTL_SECONDS = 900


@dataclass(frozen=True, slots=True)
class ApplyPreview:
    """审批前展示给 Owner 的封闭字段；不包含候选正文。"""

    proposal_id: int
    proposal_version_id: int
    proposal_version_ordinal: int
    target: str
    base_hash: str
    candidate_hash: str
    eval_receipt_hash: str
    binding_hash: str


@dataclass(frozen=True, slots=True)
class RollbackReceipt:
    """描述一次成功的 active pointer 回退。"""

    proposal_id: int
    target_type: ProposalTargetType
    target_name: str
    restored_version_id: int


class EvolutionService:
    """Phase 7 首版的唯一 Facade；不为三类 target 各造一套 Controller。"""

    def __init__(
        self,
        *,
        proposals: ProposalRepository,
        evals: EvalRepository,
        approvals: EvolutionApprovalRepository,
        active: ActiveRevisionRepository,
        prompt_versions_root: Path,
        clock: type[datetime] = datetime,
    ) -> None:
        """绑定四个 Repository 与 owner-only prompt version store。"""
        self._proposals = proposals
        self._evals = evals
        self._approvals = approvals
        self._active = active
        self._prompt_versions_root = prompt_versions_root
        self._clock = clock

    def preview_apply(
        self, owner_id: int, proposal_id: int, *, eval_run_id: int
    ) -> ApplyPreview:
        """校验 Proposal、eval receipt 与 artifact，返回待审批绑定。

        Raises:
            EvolutionError: Proposal 状态不可审批、版本缺失或 artifact 损坏。
            EvaluationError: 指定 EvalRun 没有通过确定性 Gate。
        """
        proposal, version = self._require_evaluating(owner_id, proposal_id)
        run = latest_passing_run(self._evals, eval_run_id)
        if run.proposal_version_id != version.id:
            raise EvolutionError(
                "eval_version_mismatch", "eval run does not belong to the current version"
            )
        self._require_valid_artifact(proposal, version)
        assert run.receipt_hash is not None
        binding = approval_binding_hash(
            action=EvolutionAction.APPLY,
            proposal_id=proposal.id,
            proposal_version_ordinal=version.ordinal,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
            base_hash=version.base_hash,
            candidate_hash=version.candidate_hash,
            eval_receipt_hash=run.receipt_hash,
        )
        return ApplyPreview(
            proposal_id=proposal.id,
            proposal_version_id=version.id,
            proposal_version_ordinal=version.ordinal,
            target=f"{proposal.target_type.value}:{proposal.target_name}",
            base_hash=version.base_hash,
            candidate_hash=version.candidate_hash,
            eval_receipt_hash=run.receipt_hash,
            binding_hash=binding,
        )

    def request_apply_approval(
        self,
        owner_id: int,
        proposal_id: int,
        *,
        eval_run_id: int,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> EvolutionApproval:
        """为一次 apply 创建绑定精确哈希的 pending 审批。"""
        preview = self.preview_apply(owner_id, proposal_id, eval_run_id=eval_run_id)
        return self._approvals.request(
            owner_id=owner_id,
            proposal_version_id=preview.proposal_version_id,
            eval_run_id=eval_run_id,
            action=EvolutionAction.APPLY,
            binding_hash=preview.binding_hash,
            summary=f"apply {preview.target} v{preview.proposal_version_ordinal}",
            ttl_seconds=ttl_seconds,
        )

    def apply(self, owner_id: int, approval_id: int) -> ApplyReceipt:
        """消费 approved 审批并原子切换 active pointer。

        Raises:
            EvolutionError: 审批不可用、绑定漂移、artifact 损坏或 active base 已变化。
        """
        approval = self._approvals.get(owner_id, approval_id)
        if approval.action is not EvolutionAction.APPLY:
            raise EvolutionError("approval_action_mismatch", "approval is not an apply")
        version = self._proposals.get_version(owner_id, approval.proposal_version_id)
        proposal = self._proposals.get(owner_id, version.proposal_id)
        self._require_status(proposal, ProposalStatus.APPROVED)
        run = latest_passing_run(self._evals, approval.eval_run_id or 0)
        self._require_valid_artifact(proposal, version)
        binding = approval_binding_hash(
            action=EvolutionAction.APPLY,
            proposal_id=proposal.id,
            proposal_version_ordinal=version.ordinal,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
            base_hash=version.base_hash,
            candidate_hash=version.candidate_hash,
            eval_receipt_hash=run.receipt_hash,
        )
        expected_current_id = self._expected_base_pointer(owner_id, proposal, version)
        self._approvals.consume(owner_id, approval_id, expected_binding_hash=binding)
        revision = self._active.activate(
            owner_id,
            proposal.target_type,
            proposal.target_name,
            proposal_version_id=version.id,
            expected_current_version_id=expected_current_id,
        )
        self._proposals.transition(
            owner_id,
            proposal.id,
            expected_status=ProposalStatus.APPROVED,
            new_status=ProposalStatus.APPLIED,
        )
        return ApplyReceipt(
            proposal_id=proposal.id,
            proposal_version_id=version.id,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
            previous_version_id=revision.previous_version_id,
            revision=revision,
        )

    def request_rollback_approval(
        self,
        owner_id: int,
        proposal_id: int,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> EvolutionApproval:
        """为一次 rollback 创建绑定当前 active version 的 pending 审批。"""
        proposal = self._proposals.get(owner_id, proposal_id)
        self._require_status(proposal, ProposalStatus.APPLIED)
        pointer = self._require_pointer(owner_id, proposal)
        version = self._proposals.get_version(owner_id, pointer.proposal_version_id)
        if pointer.previous_version_id is None:
            raise EvolutionError(
                "no_previous_revision", "active revision has no previous version"
            )
        binding = approval_binding_hash(
            action=EvolutionAction.ROLLBACK,
            proposal_id=proposal.id,
            proposal_version_ordinal=version.ordinal,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
            base_hash=version.base_hash,
            candidate_hash=version.candidate_hash,
            eval_receipt_hash=None,
        )
        return self._approvals.request(
            owner_id=owner_id,
            proposal_version_id=version.id,
            eval_run_id=None,
            action=EvolutionAction.ROLLBACK,
            binding_hash=binding,
            summary=(
                f"rollback {proposal.target_type.value}:{proposal.target_name} "
                f"v{version.ordinal}"
            ),
            ttl_seconds=ttl_seconds,
        )

    def rollback(self, owner_id: int, approval_id: int) -> RollbackReceipt:
        """消费 approved 审批并把 active pointer 原子切回 previous revision。

        Raises:
            EvolutionError: 审批不可用、当前指针已变化或没有可回滚的 previous revision。
        """
        approval = self._approvals.get(owner_id, approval_id)
        if approval.action is not EvolutionAction.ROLLBACK:
            raise EvolutionError("approval_action_mismatch", "approval is not a rollback")
        version = self._proposals.get_version(owner_id, approval.proposal_version_id)
        proposal = self._proposals.get(owner_id, version.proposal_id)
        self._require_status(proposal, ProposalStatus.APPLIED)
        binding = approval_binding_hash(
            action=EvolutionAction.ROLLBACK,
            proposal_id=proposal.id,
            proposal_version_ordinal=version.ordinal,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
            base_hash=version.base_hash,
            candidate_hash=version.candidate_hash,
            eval_receipt_hash=None,
        )
        self._approvals.consume(owner_id, approval_id, expected_binding_hash=binding)
        restored = self._active.rollback(
            owner_id,
            proposal.target_type,
            proposal.target_name,
            expected_current_version_id=version.id,
        )
        self._proposals.transition(
            owner_id,
            proposal.id,
            expected_status=ProposalStatus.APPLIED,
            new_status=ProposalStatus.ROLLED_BACK,
        )
        return RollbackReceipt(
            proposal_id=proposal.id,
            target_type=proposal.target_type,
            target_name=proposal.target_name,
            restored_version_id=restored.proposal_version_id,
        )

    def _require_evaluating(
        self, owner_id: int, proposal_id: int
    ) -> tuple[Proposal, ProposalVersion]:
        """要求 Proposal 处于 evaluating 且绑定了当前版本。"""
        proposal = self._proposals.get(owner_id, proposal_id)
        self._require_status(proposal, ProposalStatus.EVALUATING)
        if proposal.current_version_id is None:
            raise EvolutionError("proposal_version_missing", "proposal has no current version")
        version = self._proposals.get_version(owner_id, proposal.current_version_id)
        return proposal, version

    @staticmethod
    def _require_status(proposal: Proposal, expected: ProposalStatus) -> None:
        """要求 Proposal 正处于期望状态，否则 fail closed。"""
        if proposal.status is not expected:
            raise EvolutionError(
                "proposal_status_invalid",
                f"proposal must be {expected.value} for this action",
            )

    def _expected_base_pointer(
        self, owner_id: int, proposal: Proposal, version: ProposalVersion
    ) -> int | None:
        """解析这次 apply 允许的 CAS 期望值，并要求它仍等于候选的 base hash。

        这里刻意不把"当前指针"直接当作期望值——那样 CAS 必然成功，等于没有比较。按文档
        第 11 节，必须验证"当前 active 内容哈希仍等于 proposal 的 base_hash"：评测之后
        若有别的 Proposal 抢先切换过同一个 target，本次 apply 必须 fail closed。

        Returns:
            允许传给 ``activate`` 的期望当前版本 ID；目标从未激活过时为 ``None``。

        Raises:
            EvolutionError: active base 已经不是候选评测时所基于的内容。
        """
        pointer = self._active.get(owner_id, proposal.target_type, proposal.target_name)
        if pointer is None:
            expected_base = self._baseline_hash(proposal)
            if version.base_hash != expected_base:
                raise EvolutionError(
                    "active_base_changed",
                    "active revision no longer matches the proposal's base version",
                )
            return None
        current_version = self._proposals.get_version(owner_id, pointer.proposal_version_id)
        if current_version.candidate_hash != version.base_hash:
            raise EvolutionError(
                "active_base_changed",
                "active revision no longer matches the proposal's base version",
            )
        return pointer.proposal_version_id

    @staticmethod
    def _baseline_hash(proposal: Proposal) -> str:
        """返回一个目标"从未被 Evolution 改过"时的基线内容哈希。"""
        if proposal.target_type is ProposalTargetType.PROMPT:
            base_text = PROMPT_BLOCKS.get(proposal.target_name)
            if base_text is None:
                raise EvolutionError(
                    "unknown_prompt_block", "prompt block is not in the Core registry"
                )
            return hashlib.sha256(base_text.encode("utf-8")).hexdigest()
        return hashlib.sha256(b"").hexdigest()

    def _require_pointer(self, owner_id: int, proposal: Proposal):
        """读取目标当前 active pointer；缺失时 fail closed。"""
        pointer = self._active.get(owner_id, proposal.target_type, proposal.target_name)
        if pointer is None:
            raise EvolutionError("no_active_revision", "target has no active revision")
        return pointer

    def _require_valid_artifact(
        self, proposal: Proposal, version: ProposalVersion
    ) -> None:
        """Prompt 目标必须有完整、哈希匹配的 artifact 才能进入审批或应用。"""
        if proposal.target_type is not ProposalTargetType.PROMPT:
            return
        check = verify_prompt_artifact(self._prompt_versions_root, version)
        if not check.valid:
            raise EvolutionError(
                "artifact_invalid", f"candidate artifact is not usable: {check.reason}"
            )


__all__ = [
    "ApplyPreview",
    "ApplyReceipt",
    "EvaluationError",
    "EvolutionService",
    "RollbackReceipt",
]
