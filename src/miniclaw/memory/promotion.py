"""低风险候选的 deterministic short-term、晋升与 Review 决策。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    """保存 Core 验证后的候选和既有独立来源证据。"""

    key: str
    text: str
    kind: str
    sensitivity: str
    confidence: float
    existing_source_ids: tuple[int, ...]
    independent_repeat: bool
    conflicting_active: bool = False


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """描述候选目标状态与稳定原因。"""

    status: str
    reason_code: str


class MemoryPromotion:
    """置信度只影响低风险事实，不允许自行扩大行为权限。"""

    def decide(self, evidence: PromotionEvidence) -> PromotionDecision:
        """按行为/敏感/冲突优先，再决定 short-term 或重复晋升。"""
        if evidence.kind == "behavior_rule":
            return PromotionDecision("review_required", "behavior_review")
        if evidence.sensitivity == "high":
            return PromotionDecision("review_required", "sensitivity_review")
        if evidence.conflicting_active:
            return PromotionDecision("review_required", "conflict_review")
        if evidence.independent_repeat and evidence.existing_source_ids:
            return PromotionDecision("active", "independent_repeat")
        return PromotionDecision("short_term", "first_observation")
