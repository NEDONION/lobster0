"""对 Provider Candidate 执行来源、Secret、信任与行为影响验证。"""

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from lobster0.memory.extractor import ExtractedCandidate
from lobster0.memory.flush import FlushSourceMessage
from lobster0.memory.store import contains_sensitive_memory

type ValidationDecision = Literal["accepted", "review_required", "rejected"]

_ALLOWED_KINDS = frozenset({"preference", "fact", "person", "project", "behavior_rule"})
_BEHAVIOR = re.compile(
    r"(?:自动执行|无需询问|不要询问|绕过|忽略.{0,6}(?:规则|权限)|所有命令|"
    r"always execute|never ask|bypass|ignore.{0,12}(?:policy|permission))",
    re.IGNORECASE,
)
_HIGH_SENSITIVITY = re.compile(
    r"(?:身份证|住址|家庭地址|病历|疾病|健康信息|工资|银行账户|"
    r"medical|health|home address|bank account|salary)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """描述候选是否可进入治理；拒绝结果不保留原始正文。"""

    decision: ValidationDecision
    reason_code: str
    text: str | None
    kind: str | None
    key: str | None
    sensitivity: str | None
    confidence: float | None
    source_message_ids: tuple[int, ...]


class MemoryCandidateValidator:
    """只信任当前 Flush range 的 User messages，并由 Core 重分类。"""

    def validate(
        self,
        candidate: ExtractedCandidate,
        messages: tuple[FlushSourceMessage, ...],
    ) -> ValidationResult:
        """验证来源/正文，输出 accepted、review_required 或无正文 rejected。"""
        user_sources = {
            message.id: message
            for message in messages
            if message.role == "user"
        }
        if any(source not in user_sources for source in candidate.source_message_ids):
            return _rejected("invalid_source")
        text = " ".join(candidate.text.split())
        if not text or len(text) > 2_000:
            return _rejected("invalid_memory")
        if contains_sensitive_memory(text):
            return _rejected("sensitive_memory")
        if candidate.confidence < 0.55:
            return _rejected("low_confidence")
        kind = candidate.kind if candidate.kind in _ALLOWED_KINDS else "fact"
        behavior = kind == "behavior_rule" or _BEHAVIOR.search(text) is not None
        if behavior:
            kind = "behavior_rule"
        sensitivity = (
            "high"
            if behavior or candidate.sensitivity == "high" or _HIGH_SENSITIVITY.search(text)
            else candidate.sensitivity
        )
        key = _memory_key(kind, text)
        decision: ValidationDecision = (
            "review_required" if sensitivity == "high" or behavior else "accepted"
        )
        return ValidationResult(
            decision,
            "policy_review" if decision == "review_required" else "validated",
            text,
            kind,
            key,
            sensitivity,
            candidate.confidence,
            candidate.source_message_ids,
        )


def _rejected(reason_code: str) -> ValidationResult:
    """创建不携带候选正文的稳定拒绝结果。"""
    return ValidationResult("rejected", reason_code, None, None, None, None, None, ())


def _memory_key(kind: str, text: str) -> str:
    """按少量稳定语义键和文本哈希生成 Core-owned memory key。"""
    folded = text.casefold()
    if kind == "behavior_rule":
        prefix = "behavior"
    elif ("中文" in text or "英文" in text or "language" in folded) and (
        "回复" in text or "answer" in folded
    ):
        return "preference.language"
    elif "简洁" in text or "concise" in folded:
        return "preference.response_style"
    else:
        prefix = kind
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}.{digest}"
