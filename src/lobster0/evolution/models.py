"""Controlled Evolution 的不可变数据对象与状态枚举。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class FeedbackRating(StrEnum):
    """Owner 对一条 assistant message 的评价。"""

    GOOD = "good"
    BAD = "bad"


class FeedbackStatus(StrEnum):
    """Feedback 记录的留存状态。"""

    OPEN = "open"
    FORGOTTEN = "forgotten"


class ProposalTargetType(StrEnum):
    """Proposal 允许修改的三类受限目标。"""

    PROMPT = "prompt"
    SKILL = "skill"
    MEMORY = "memory"


class ProposalStatus(StrEnum):
    """Proposal 生命周期状态；跳转必须遵守文档第 7 节状态机。"""

    DRAFT = "draft"
    EVALUATING = "evaluating"
    REJECTED = "rejected"
    APPROVED = "approved"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class EvalRunStatus(StrEnum):
    """一次 EvalRun 的整体结果。"""

    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class EvalCaseStatus(StrEnum):
    """一条 EvalCaseResult 的判定。"""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Feedback:
    """一条 Owner 对 assistant message 的评价与脱敏材料。"""

    id: int
    owner_id: int
    message_id: int
    rating: FeedbackRating
    redacted_reason: str | None
    context_hash: str
    status: FeedbackStatus
    created_at: datetime
    forgotten_at: datetime | None


@dataclass(frozen=True, slots=True)
class Proposal:
    """一个受限目标上的改进提案；不可变内容存在其 ProposalVersion 中。"""

    id: int
    owner_id: int
    feedback_id: int
    target_type: ProposalTargetType
    target_name: str
    status: ProposalStatus
    current_version_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProposalVersion:
    """一个 Proposal 下 append-only、不可原地覆盖的具体候选内容。"""

    id: int
    proposal_id: int
    ordinal: int
    base_hash: str
    candidate_hash: str
    manifest_json: str
    candidate_ref: str
    rationale: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvalRun:
    """一次针对某个 immutable ProposalVersion 的回归评测。"""

    id: int
    proposal_version_id: int
    suite_manifest_hash: str
    status: EvalRunStatus
    receipt_hash: str | None
    total_cases: int
    passed_cases: int
    safety_failures: int
    duration_ms: int | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """一次 EvalRun 中单条 case 的判定与脱敏诊断。"""

    id: int
    eval_run_id: int
    case_id: str
    suite_version: str
    status: EvalCaseStatus
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    result_hash: str


class EvolutionAction(StrEnum):
    """Owner 可以审批的两种 Evolution 高危动作。"""

    APPLY = "evolution.apply"
    ROLLBACK = "evolution.rollback"


class EvolutionApprovalStatus(StrEnum):
    """Evolution Approval 的生命周期状态；``consumed`` 表示已经被使用过一次。"""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class EvolutionApproval:
    """一次绑定精确 hash、有 TTL 且只能消费一次的 Owner 审批。"""

    id: int
    owner_id: int
    proposal_version_id: int
    eval_run_id: int | None
    action: EvolutionAction
    binding_hash: str
    summary: str
    status: EvolutionApprovalStatus
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ActiveRevision:
    """一个 (owner, target) 当前生效的 ProposalVersion 指针。"""

    owner_id: int
    target_type: ProposalTargetType
    target_name: str
    proposal_version_id: int
    previous_version_id: int | None
    activated_at: datetime
