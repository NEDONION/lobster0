"""协调明确 remember 的意图校验、Markdown 真相和 SQLite Projection。"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from lobster0.memory.markdown_store import (
    MarkdownMemoryError,
    MarkdownUnitDocument,
    MemoryMarkdownStore,
)
from lobster0.memory.models import DisclosureContext, SourceRef
from lobster0.memory.policy import MemoryDisclosurePolicy, MemoryPolicyError
from lobster0.memory.repository import (
    MemoryReviewRepository,
    MemoryStateError,
    MemoryUnitRepository,
)
from lobster0.memory.review import review_preview_hash
from lobster0.memory.store import MemoryError, MemoryStore

_EXPLICIT_INTENT = re.compile(
    r"(?:记住|记得|记下来|保存.{0,8}(?:偏好|记忆|信息)|remember|keep in mind)",
    re.IGNORECASE,
)
_BEHAVIOR_RULE = re.compile(
    r"(?:自动执行|不要询问|无需询问|所有命令|改变权限|忽略规则|永远允许|"
    r"always execute|never ask|permission|bypass)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ExplicitMemoryRequest:
    """保存由 Core 绑定的明确记忆意图、来源和披露边界。"""

    disclosure: DisclosureContext
    source: SourceRef
    latest_user_text: str
    fact: str
    now: datetime


@dataclass(frozen=True, slots=True)
class RememberResult:
    """描述明确 remember 已持久化的 Unit 和可选 Review。"""

    unit_id: str
    status: str
    review_id: int | None
    markdown_hash: str


class MemoryService:
    """以 Markdown-first 顺序提交明确 Owner Memory。"""

    def __init__(
        self,
        markdown: MemoryMarkdownStore,
        units: MemoryUnitRepository,
        reviews: MemoryReviewRepository,
        legacy_store: MemoryStore,
        disclosure_policy: MemoryDisclosurePolicy | None = None,
    ) -> None:
        """绑定真相源、Projection、Review 和现有 Secret 校验器。"""
        self._markdown = markdown
        self._units = units
        self._reviews = reviews
        self._legacy_store = legacy_store
        self._disclosure_policy = disclosure_policy or MemoryDisclosurePolicy()

    @property
    def markdown(self) -> MemoryMarkdownStore:
        """返回当前原子 Markdown Store，供运维和测试读取固定路径。"""
        return self._markdown

    def remember_explicit(self, request: ExplicitMemoryRequest) -> RememberResult:
        """校验明确意图后直接提交普通事实，规则类只进入 Review。"""
        if not isinstance(request.latest_user_text, str) or not _EXPLICIT_INTENT.search(
            request.latest_user_text
        ):
            raise MemoryError(
                "memory_intent_required",
                "memory was not stored because explicit remember intent is required",
            )
        try:
            decision = self._disclosure_policy.decide(request.disclosure)
        except MemoryPolicyError as error:
            raise MemoryError(
                "memory_disclosure_denied",
                "memory was not stored in this conversation",
            ) from error
        if decision.capture_scope != "private" or decision.private_access != "full":
            raise MemoryError(
                "memory_disclosure_denied",
                "memory was not stored in this conversation",
            )
        fact, _ = self._legacy_store.validate_candidate(
            request.fact,
            "explicit owner request",
        )
        if request.source.channel != request.disclosure.channel:
            raise MemoryError(
                "invalid_memory_source",
                "memory source does not match the current conversation",
            )
        try:
            self._units.validate_sources(
                request.disclosure.owner_id,
                (request.source,),
            )
        except MemoryStateError as error:
            raise MemoryError(
                "invalid_memory_source",
                "memory source could not be verified",
            ) from error
        behavior = _BEHAVIOR_RULE.search(fact) is not None
        kind, key = _classify(fact, behavior=behavior)
        active = self._units.active_for_key(request.disclosure.owner_id, key)
        conflict = active is not None and active.text != fact
        status = "review_required" if behavior or conflict else "active"
        unit_id = _unit_id(request.disclosure.owner_id, request.source, fact)
        existing = self._units.find(request.disclosure.owner_id, unit_id)
        if existing is None:
            existing = self._units.find_by_text(request.disclosure.owner_id, fact)
        if existing is not None:
            return RememberResult(
                existing.id,
                existing.status,
                _existing_review_id(self._reviews, request.disclosure.owner_id, existing.id),
                existing.markdown_hash or "",
            )
        document = MarkdownUnitDocument(
            unit_id=unit_id,
            owner_id=request.disclosure.owner_id,
            key=key,
            text=fact,
            kind=kind,
            scope="private",
            status=status,
            confidence=1.0,
            sensitivity="high" if status == "review_required" else "low",
            valid_from=request.now,
            valid_until=None,
            sources=(request.source,),
        )
        try:
            write = self._markdown.append(document)
        except (MarkdownMemoryError, OSError) as error:
            raise MemoryError(
                "memory_write_failed",
                "memory could not be stored atomically",
            ) from error
        try:
            unit = self._units.create(
                unit_id=unit_id,
                owner_id=request.disclosure.owner_id,
                key=key,
                text=fact,
                kind=kind,
                scope="private",
                status=status,
                confidence=1.0,
                sensitivity="high" if status == "review_required" else "low",
                valid_from=request.now,
                valid_until=None,
                sources=(request.source,),
                markdown_hash=write.block_hash,
                now=request.now,
            )
        except MemoryStateError as error:
            recovered = self._units.find(request.disclosure.owner_id, unit_id)
            if recovered is None:
                raise MemoryError(
                    "memory_projection_failed",
                    "memory truth was stored but its projection is pending recovery",
                ) from error
            unit = recovered
        review_id: int | None = None
        if status == "review_required":
            review_type = "conflict" if conflict else "behavior"
            preview_hash = review_preview_hash(
                unit,
                review_type=review_type,
                requested_transition="active",
            )
            review = self._reviews.create(
                owner_id=request.disclosure.owner_id,
                review_type=review_type,
                preview_hash=preview_hash,
                requested_transition="active",
                unit_id=unit.id,
                payload={
                    "unit_hash": unit.text_hash,
                    "active_unit_id": None if active is None else active.id,
                },
                now=request.now,
            )
            review_id = review.id
            if conflict and active is not None:
                self._units.record_conflict(
                    owner_id=request.disclosure.owner_id,
                    key=key,
                    active_unit_id=active.id,
                    incoming_unit_id=unit.id,
                    now=request.now,
                )
        return RememberResult(unit.id, unit.status, review_id, write.block_hash)


def _unit_id(owner_id: int, source: SourceRef, fact: str) -> str:
    """从 Owner、来源消息和规范事实生成重试稳定的 Unit ID。"""
    digest = hashlib.sha256(
        f"{owner_id}\0{source.message_id}\0{fact}".encode()
    ).hexdigest()
    return f"mem-{digest[:24]}"


def _classify(fact: str, *, behavior: bool) -> tuple[str, str]:
    """为首版明确事实生成稳定 kind/key，后续可由治理层细化。"""
    folded = fact.casefold()
    if behavior:
        return "behavior_rule", f"behavior.{hashlib.sha256(fact.encode()).hexdigest()[:16]}"
    if ("中文" in fact or "英文" in fact or "language" in folded) and (
        "回复" in fact or "answer" in folded
    ):
        return "preference", "preference.language"
    if "简洁" in fact or "concise" in folded:
        return "preference", "preference.response_style"
    if "偏好" in fact or "喜欢" in fact or "prefer" in folded:
        return "preference", f"preference.{hashlib.sha256(fact.encode()).hexdigest()[:16]}"
    return "fact", f"fact.{hashlib.sha256(fact.encode()).hexdigest()[:16]}"


def _existing_review_id(
    reviews: MemoryReviewRepository,
    owner_id: int,
    unit_id: str,
) -> int | None:
    """为重复明确请求返回同 Unit 仍 pending 的稳定 Review ID。"""
    review = reviews.pending_for_unit(owner_id, unit_id)
    return None if review is None else review.id
