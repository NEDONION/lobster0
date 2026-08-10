"""Owner-bound Memory Review、冲突 supersede、纠错与 Forget 决策。"""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from lobster0.memory.markdown_store import MarkdownUnitDocument, MemoryMarkdownStore
from lobster0.memory.models import DisclosureContext, SourceRef
from lobster0.memory.policy import MemoryDisclosurePolicy, MemoryPolicyError
from lobster0.memory.repository import (
    MemoryReview,
    MemoryReviewRepository,
    MemoryStateError,
    MemoryUnit,
    MemoryUnitRepository,
)
from lobster0.memory.store import MemoryError, MemoryStore
from lobster0.storage.database import Database

_CORRECTION_INTENT = re.compile(
    r"(?:更正|纠正|改成|更新记忆|correct|update (?:that )?memory)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ReviewPreview:
    """展示给本地 Owner 的 preview-hash-bound Review 摘要。"""

    review_id: int
    review_type: str
    unit_id: str
    text: str
    current_status: str
    requested_transition: str
    preview_hash: str


@dataclass(frozen=True, slots=True)
class ReviewDecisionResult:
    """描述 Owner Review 的终态及发生转换的 Unit IDs。"""

    review_id: int
    status: str
    unit_ids: tuple[str, ...]


class MemoryReviewService:
    """验证 Owner/preview 后，按 Markdown-first 顺序执行治理状态转换。"""

    def __init__(
        self,
        database: Database,
        markdown: MemoryMarkdownStore,
        units: MemoryUnitRepository,
        reviews: MemoryReviewRepository,
        legacy_store: MemoryStore,
        policy: MemoryDisclosurePolicy | None = None,
    ) -> None:
        """绑定数据库、真相源、Repository、Secret validator 和 Disclosure Policy。"""
        self._database = database
        self._markdown = markdown
        self._units = units
        self._reviews = reviews
        self._legacy_store = legacy_store
        self._policy = policy or MemoryDisclosurePolicy()

    def list(
        self,
        disclosure: DisclosureContext,
        *,
        limit: int = 50,
    ) -> tuple[ReviewPreview, ...]:
        """只向已验证本地/私聊 Owner 列出 pending Review。"""
        owner_id = self._owner(disclosure)
        return tuple(
            self._preview(review)
            for review in self._reviews.list_pending(owner_id, limit=limit)
            if review.unit_id is not None
        )

    def get(self, disclosure: DisclosureContext, review_id: int) -> ReviewPreview:
        """按 Owner 读取一个 pending Review 的绑定预览。"""
        owner_id = self._owner(disclosure)
        try:
            review = self._reviews.get(owner_id, review_id)
        except MemoryStateError as error:
            raise MemoryError(
                "memory_review_not_found",
                "memory review was not found",
            ) from error
        if review.status != "pending" or review.unit_id is None:
            raise MemoryError("memory_review_not_pending", "memory review is not pending")
        return self._preview(review)

    def preview_forget(
        self,
        disclosure: DisclosureContext,
        unit_id: str,
        *,
        now: datetime,
    ) -> ReviewPreview:
        """创建绑定当前 Unit hash/status 的 Forget Review，不立即归档。"""
        owner_id = self._owner(disclosure)
        try:
            unit = self._units.get(owner_id, unit_id)
        except MemoryStateError as error:
            raise MemoryError("memory_not_found", "memory unit was not found") from error
        if unit.status not in {"active", "short_term"}:
            raise MemoryError("memory_forget_invalid", "memory unit cannot be forgotten")
        pending = self._reviews.pending_for_unit(owner_id, unit.id)
        if pending is not None and pending.review_type == "forget":
            return self._preview(pending)
        target_hash = review_preview_hash(
            unit,
            review_type="forget",
            requested_transition="archived",
        )
        preview_hash = hashlib.sha256(
            f"{target_hash}\0forget-request\0{_time_text(now)}".encode()
        ).hexdigest()
        review = self._reviews.create(
            owner_id=owner_id,
            review_type="forget",
            preview_hash=preview_hash,
            requested_transition="archived",
            unit_id=unit.id,
            payload={
                "unit_hash": unit.text_hash,
                "current_status": unit.status,
                "target_preview_hash": target_hash,
            },
            now=now,
        )
        return self._preview(review)

    def propose_correction(
        self,
        disclosure: DisclosureContext,
        unit_id: str,
        new_text: str,
        *,
        source: SourceRef,
        latest_user_text: str,
        now: datetime,
    ) -> ReviewPreview:
        """从明确纠错 User Message 创建新 Unit，旧 Unit 在批准前保持不变。"""
        owner_id = self._owner(disclosure)
        if not isinstance(latest_user_text, str) or not _CORRECTION_INTENT.search(
            latest_user_text
        ):
            raise MemoryError(
                "memory_correction_intent_required",
                "memory correction requires explicit owner intent",
            )
        try:
            old = self._units.get(owner_id, unit_id)
        except MemoryStateError as error:
            raise MemoryError("memory_not_found", "memory unit was not found") from error
        if old.status not in {"active", "short_term"}:
            raise MemoryError(
                "memory_correction_invalid",
                "memory unit cannot be corrected",
            )
        fact, _ = self._legacy_store.validate_candidate(
            new_text,
            "explicit owner correction",
        )
        if fact == old.text:
            raise MemoryError(
                "memory_correction_unchanged",
                "memory correction did not change the fact",
            )
        if source.channel != disclosure.channel:
            raise MemoryError(
                "invalid_memory_source",
                "memory source does not match the current conversation",
            )
        try:
            self._units.validate_sources(owner_id, (source,))
        except MemoryStateError as error:
            raise MemoryError(
                "invalid_memory_source",
                "memory source could not be verified",
            ) from error
        digest = hashlib.sha256(
            f"{owner_id}\0{old.id}\0{source.message_id}\0{fact}".encode()
        ).hexdigest()
        incoming_id = f"corr-{digest[:24]}"
        existing = self._units.find(owner_id, incoming_id)
        if existing is None:
            write = self._markdown.append(
                MarkdownUnitDocument(
                    unit_id=incoming_id,
                    owner_id=owner_id,
                    key=old.key,
                    text=fact,
                    kind=old.kind,
                    scope=old.scope,
                    status="review_required",
                    confidence=1.0,
                    sensitivity=old.sensitivity,
                    valid_from=now,
                    valid_until=None,
                    sources=(source,),
                )
            )
            incoming = self._units.create(
                unit_id=incoming_id,
                owner_id=owner_id,
                key=old.key,
                text=fact,
                kind=old.kind,
                scope=old.scope,
                status="review_required",
                confidence=1.0,
                sensitivity=old.sensitivity,
                valid_from=now,
                valid_until=None,
                sources=(source,),
                supersedes_unit_id=old.id,
                markdown_hash=write.block_hash,
                now=now,
            )
        else:
            incoming = existing
        preview_hash = review_preview_hash(
            incoming,
            review_type="correction",
            requested_transition="active",
        )
        review = self._reviews.create(
            owner_id=owner_id,
            review_type="correction",
            preview_hash=preview_hash,
            requested_transition="active",
            unit_id=incoming.id,
            payload={"unit_hash": incoming.text_hash, "active_unit_id": old.id},
            now=now,
        )
        self._units.record_conflict(
            owner_id=owner_id,
            key=old.key,
            active_unit_id=old.id,
            incoming_unit_id=incoming.id,
            now=now,
        )
        return self._preview(review)

    def decide(
        self,
        disclosure: DisclosureContext,
        review_id: int,
        preview_hash: str,
        *,
        approve: bool,
        now: datetime,
    ) -> ReviewDecisionResult:
        """校验未变化预览，原子更新 Markdown 后消费或拒绝 Review。"""
        owner_id = self._owner(disclosure)
        if type(approve) is not bool:
            raise ValueError("memory review approve must be bool")
        try:
            review = self._reviews.get(owner_id, review_id)
        except MemoryStateError as error:
            raise MemoryError(
                "memory_review_not_found",
                "memory review was not found",
            ) from error
        if review.status != "pending" or review.unit_id is None:
            raise MemoryError("memory_review_not_pending", "memory review is not pending")
        if preview_hash != review.preview_hash:
            raise MemoryError("memory_review_preview_mismatch", "memory review preview changed")
        try:
            unit = self._units.get(owner_id, review.unit_id)
        except MemoryStateError as error:
            raise MemoryError(
                "memory_review_target_changed",
                "memory review target changed after preview",
            ) from error
        expected = review_preview_hash(
            unit,
            review_type=review.review_type,
            requested_transition=review.requested_transition,
        )
        target_preview_hash = review.payload.get(
            "target_preview_hash",
            review.preview_hash,
        )
        if expected != target_preview_hash:
            raise MemoryError(
                "memory_review_target_changed",
                "memory review target changed after preview",
            )
        transitions = self._transitions(review, unit, approve=approve)
        batch = None
        if transitions:
            batch = self._markdown.upsert_many(
                tuple(_document(item, status) for item, status in transitions)
            )
        timestamp = _time_text(now)
        try:
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """
                    SELECT status, preview_hash FROM memory_reviews
                    WHERE id = ? AND owner_id = ?
                    """,
                    (review.id, owner_id),
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "pending"
                    or current["preview_hash"] != preview_hash
                ):
                    raise MemoryError(
                        "memory_review_not_pending",
                        "memory review is not pending",
                    )
                for item, status in transitions:
                    assert batch is not None
                    updated = connection.execute(
                        """
                        UPDATE memory_units
                        SET status = ?, markdown_hash = ?, updated_at = ?
                        WHERE id = ? AND owner_id = ? AND status = ? AND text_hash = ?
                        """,
                        (
                            status,
                            batch.block_hashes[item.id],
                            timestamp,
                            item.id,
                            owner_id,
                            item.status,
                            item.text_hash,
                        ),
                    ).rowcount
                    if updated != 1:
                        raise MemoryError(
                            "memory_review_target_changed",
                            "memory review target changed after preview",
                        )
                final_status = "consumed" if approve else "rejected"
                connection.execute(
                    """
                    UPDATE memory_reviews SET status = ?, decided_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (final_status, timestamp, review.id),
                )
                if review.review_type in {"conflict", "correction"}:
                    connection.execute(
                        """
                        UPDATE memory_conflicts SET status = ?, resolution = ?, resolved_at = ?
                        WHERE owner_id = ? AND incoming_unit_id = ? AND status = 'pending'
                        """,
                        (
                            "resolved" if approve else "dismissed",
                            "incoming" if approve else "active",
                            timestamp,
                            owner_id,
                            review.unit_id,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO memory_audit (
                        owner_id, event_type, unit_id, review_id,
                        reason_code, metadata_json, created_at
                    ) VALUES (?, 'review_decided', ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        review.unit_id,
                        review.id,
                        "approved" if approve else "rejected",
                        json.dumps(
                            {"review_type": review.review_type},
                            separators=(",", ":"),
                        ),
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise MemoryError(
                "memory_review_apply_failed",
                "memory review could not be applied",
            ) from error
        return ReviewDecisionResult(
            review.id,
            "consumed" if approve else "rejected",
            tuple(item.id for item, _ in transitions),
        )

    def _transitions(
        self,
        review: MemoryReview,
        unit: MemoryUnit,
        *,
        approve: bool,
    ) -> tuple[tuple[MemoryUnit, str], ...]:
        """把 Review type/decision 映射为封闭 Unit 状态转换集合。"""
        if review.review_type == "weekly":
            return ()
        if not approve:
            return () if review.review_type == "forget" else ((unit, "rejected"),)
        if review.review_type == "forget":
            return ((unit, "archived"),)
        if review.review_type in {"conflict", "correction"}:
            active_id = review.payload.get("active_unit_id")
            if not isinstance(active_id, str):
                raise MemoryError(
                    "memory_review_data_invalid",
                    "memory review data is invalid",
                )
            active = self._units.get(review.owner_id, active_id)
            if active.status not in {"active", "short_term"} or active.key != unit.key:
                raise MemoryError(
                    "memory_review_target_changed",
                    "memory review target changed after preview",
                )
            return ((active, "superseded"), (unit, "active"))
        return ((unit, "active"),)

    def _preview(self, review: MemoryReview) -> ReviewPreview:
        """把 pending Review 与当前 Unit 组合成 Owner 可见预览。"""
        assert review.unit_id is not None
        unit = self._units.get(review.owner_id, review.unit_id)
        return ReviewPreview(
            review.id,
            review.review_type,
            unit.id,
            unit.text,
            unit.status,
            review.requested_transition,
            review.preview_hash,
        )

    def _owner(self, disclosure: DisclosureContext) -> int:
        """返回获准 Owner ID；群聊、非 Owner 和身份异常统一拒绝。"""
        try:
            decision = self._policy.decide(disclosure)
        except MemoryPolicyError as error:
            raise MemoryError(
                "memory_disclosure_denied",
                "memory review is unavailable in this conversation",
            ) from error
        if decision.private_access != "full":
            raise MemoryError(
                "memory_disclosure_denied",
                "memory review is unavailable in this conversation",
            )
        return disclosure.owner_id


def review_preview_hash(
    unit: MemoryUnit,
    *,
    review_type: str,
    requested_transition: str,
) -> str:
    """绑定 Unit ID/text hash/current status、Review type 和目标转换。"""
    return hashlib.sha256(
        (
            f"{unit.id}\0{unit.text_hash}\0{unit.status}\0"
            f"{review_type}\0{requested_transition}"
        ).encode()
    ).hexdigest()


def _document(unit: MemoryUnit, status: str) -> MarkdownUnitDocument:
    """把现有 Unit 克隆为仅状态变化的 Markdown 文档。"""
    return MarkdownUnitDocument(
        unit_id=unit.id,
        owner_id=unit.owner_id,
        key=unit.key,
        text=unit.text,
        kind=unit.kind,
        scope=unit.scope,
        status=status,
        confidence=unit.confidence,
        sensitivity=unit.sensitivity,
        valid_from=unit.valid_from,
        valid_until=unit.valid_until,
        sources=unit.sources,
    )


def _time_text(value: datetime) -> str:
    """把带时区 Review 时间转为 UTC ISO 文本。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("memory review time must be timezone-aware")
    return value.astimezone(UTC).isoformat()
