"""把 Extractor、Validator、Promotion、Markdown 与 Projection 串成可恢复 Handler。"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from lobster0.memory.extractor import ExtractedCandidate
from lobster0.memory.flush import FlushSourceMessage
from lobster0.memory.markdown_store import MarkdownUnitDocument, MemoryMarkdownStore
from lobster0.memory.models import SourceRef
from lobster0.memory.promotion import MemoryPromotion, PromotionEvidence
from lobster0.memory.repository import (
    MemoryCandidate,
    MemoryCandidateRepository,
    MemoryFlushRun,
    MemoryReviewRepository,
    MemoryStateError,
    MemoryUnit,
    MemoryUnitRepository,
)
from lobster0.memory.review import review_preview_hash
from lobster0.memory.validator import MemoryCandidateValidator
from lobster0.storage.database import Database


class CandidateExtractor(Protocol):
    """收窄 Pipeline 对 Provider Extractor 的异步接口。"""

    async def extract(
        self,
        messages: tuple[FlushSourceMessage, ...],
    ) -> tuple[ExtractedCandidate, ...]:
        """从有界消息范围返回严格候选。"""
        ...


class MemoryPipelineHandler:
    """实现 Markdown-first、Provider-candidate-only 的 FlushHandler。"""

    def __init__(
        self,
        database: Database,
        extractor: CandidateExtractor,
        markdown: MemoryMarkdownStore,
        candidates: MemoryCandidateRepository,
        units: MemoryUnitRepository,
        reviews: MemoryReviewRepository,
        *,
        validator: MemoryCandidateValidator | None = None,
        promotion: MemoryPromotion | None = None,
    ) -> None:
        """绑定 Pipeline 所有持久化边界和 deterministic Policy。"""
        self._database = database
        self._extractor = extractor
        self._markdown = markdown
        self._candidates = candidates
        self._units = units
        self._reviews = reviews
        self._validator = validator or MemoryCandidateValidator()
        self._promotion = promotion or MemoryPromotion()

    async def write_markdown(
        self,
        run: MemoryFlushRun,
        messages: tuple[FlushSourceMessage, ...],
    ) -> None:
        """提取/验证后幂等 upsert Markdown，拒绝候选从不进入持久化表。"""
        extracted = await self._extractor.extract(messages)
        source_map = {message.id: message for message in messages}
        seen_hashes: set[str] = set()
        for ordinal, raw in enumerate(extracted):
            validation = self._validator.validate(raw, messages)
            if validation.decision == "rejected":
                continue
            assert validation.text is not None
            assert validation.kind is not None
            assert validation.key is not None
            assert validation.sensitivity is not None
            assert validation.confidence is not None
            candidate_hash = hashlib.sha256(
                validation.text.encode("utf-8")
            ).hexdigest()
            if candidate_hash in seen_hashes:
                continue
            seen_hashes.add(candidate_hash)
            sources = tuple(
                SourceRef(
                    message_id=source_id,
                    session_id=source_map[source_id].session_id,
                    channel=source_map[source_id].channel,
                )
                for source_id in validation.source_message_ids
            )
            existing = self._units.find_by_text(run.owner_id, validation.text)
            conflicting = self._units.active_for_key(run.owner_id, validation.key)
            independent = existing is not None and any(
                source.message_id not in {item.message_id for item in existing.sources}
                for source in sources
            )
            promotion = self._promotion.decide(
                PromotionEvidence(
                    validation.key,
                    validation.text,
                    validation.kind,
                    validation.sensitivity,
                    validation.confidence,
                    () if existing is None else tuple(
                        item.message_id for item in existing.sources
                    ),
                    independent,
                    conflicting_active=(
                        conflicting is not None
                        and (existing is None or conflicting.id != existing.id)
                    ),
                )
            )
            status = (
                "review_required"
                if validation.decision == "review_required"
                else promotion.status
            )
            if existing is not None and existing.status == "active":
                status = "active"
            unit_id = (
                existing.id
                if existing is not None
                else _automatic_unit_id(run.owner_id, validation.key, validation.text)
            )
            all_sources = _merge_sources(
                () if existing is None else existing.sources,
                sources,
            )
            valid_until = (
                run.created_at + timedelta(days=30)
                if status == "short_term"
                else None
            )
            document = MarkdownUnitDocument(
                unit_id=unit_id,
                owner_id=run.owner_id,
                key=validation.key,
                text=validation.text,
                kind=validation.kind,
                scope="private",
                status=status,
                confidence=validation.confidence,
                sensitivity=validation.sensitivity,
                valid_from=(
                    run.created_at if existing is None else existing.valid_from
                ),
                valid_until=valid_until,
                sources=all_sources,
            )
            write = self._markdown.upsert(document)
            self._candidates.create(
                run_id=run.id,
                ordinal=ordinal,
                text=validation.text,
                kind=validation.kind,
                scope="private",
                confidence=validation.confidence,
                sensitivity=validation.sensitivity,
                source_message_ids=validation.source_message_ids,
                metadata={
                    "unit_id": unit_id,
                    "key": validation.key,
                    "target_status": status,
                    "markdown_hash": write.block_hash,
                    "valid_from": document.valid_from.isoformat(),
                    "valid_until": (
                        None if valid_until is None else valid_until.isoformat()
                    ),
                },
                now=run.created_at,
            )

    async def project(self, run: MemoryFlushRun) -> None:
        """从持久 Candidate metadata 幂等创建/更新 Unit、Source 与 Review。"""
        for candidate in self._candidates.list_for_run(run.id):
            fields = _projection_fields(candidate)
            sources = self._load_sources(run, candidate.source_message_ids)
            existing = self._units.find(run.owner_id, fields.unit_id)
            if existing is None:
                unit = self._units.create(
                    unit_id=fields.unit_id,
                    owner_id=run.owner_id,
                    key=fields.key,
                    text=candidate.text,
                    kind=candidate.kind,
                    scope="private",
                    status=fields.status,
                    confidence=candidate.confidence,
                    sensitivity=candidate.sensitivity,
                    valid_from=fields.valid_from,
                    valid_until=fields.valid_until,
                    sources=sources,
                    candidate_id=candidate.id,
                    markdown_hash=fields.markdown_hash,
                    now=run.created_at,
                )
            else:
                unit = self._units.merge_sources_and_status(
                    owner_id=run.owner_id,
                    unit_id=existing.id,
                    sources=sources,
                    status=fields.status,
                    markdown_hash=fields.markdown_hash,
                    now=run.created_at,
                )
            self._candidates.mark_status(candidate.id, "committed", now=run.created_at)
            if unit.status == "review_required":
                self._create_review(run, candidate, unit)

    def _load_sources(
        self,
        run: MemoryFlushRun,
        source_ids: tuple[int, ...],
    ) -> tuple[SourceRef, ...]:
        """从 SQLite 重新绑定 Provider source ids，防止伪造 Session/Channel。"""
        placeholders = ",".join("?" for _ in source_ids)
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                f"""
                SELECT messages.id, messages.session_id, sessions.channel
                FROM messages JOIN sessions ON sessions.id = messages.session_id
                WHERE sessions.user_id = ? AND messages.id IN ({placeholders})
                    AND messages.id BETWEEN ? AND ? AND messages.role = 'user'
                ORDER BY messages.id
                """,
                (
                    run.owner_id,
                    *source_ids,
                    run.first_message_id,
                    run.last_message_id,
                ),
            ).fetchall()
        if {int(row["id"]) for row in rows} != set(source_ids):
            raise MemoryStateError("memory candidate source changed before projection")
        by_id = {
            int(row["id"]): SourceRef(
                int(row["id"]),
                int(row["session_id"]),
                str(row["channel"]),
            )
            for row in rows
        }
        return tuple(by_id[source_id] for source_id in source_ids)

    def _create_review(
        self,
        run: MemoryFlushRun,
        candidate: MemoryCandidate,
        unit: MemoryUnit,
    ) -> None:
        """为行为/敏感/冲突 Unit 创建 preview-hash-bound pending Review。"""
        active = self._units.active_for_key(run.owner_id, unit.key)
        review_type = (
            "conflict"
            if active is not None and active.id != unit.id
            else "behavior" if unit.kind == "behavior_rule" else "sensitivity"
        )
        preview_hash = review_preview_hash(
            unit,
            review_type=review_type,
            requested_transition="active",
        )
        self._reviews.create(
            owner_id=run.owner_id,
            review_type=review_type,
            preview_hash=preview_hash,
            requested_transition="active",
            unit_id=unit.id,
            candidate_id=candidate.id,
            payload={
                "unit_hash": unit.text_hash,
                "active_unit_id": None if active is None else active.id,
            },
            now=run.created_at,
        )


def _automatic_unit_id(owner_id: int, key: str, text: str) -> str:
    """由 Owner/key/text 生成跨重试稳定且不含正文的自动 Unit ID。"""
    digest = hashlib.sha256(f"{owner_id}\0{key}\0{text}".encode()).hexdigest()
    return f"auto-{digest[:24]}"


def _merge_sources(
    existing: tuple[SourceRef, ...],
    incoming: tuple[SourceRef, ...],
) -> tuple[SourceRef, ...]:
    """按首次出现顺序合并 SourceRef，并以 message id 去重。"""
    selected: list[SourceRef] = []
    seen: set[int] = set()
    for source in (*existing, *incoming):
        if source.message_id not in seen:
            seen.add(source.message_id)
            selected.append(source)
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class _ProjectionFields:
    """保存 Candidate metadata 严格解码后的 Projection 字段。"""

    unit_id: str
    key: str
    status: str
    markdown_hash: str
    valid_from: datetime
    valid_until: datetime | None


def _projection_fields(candidate: MemoryCandidate) -> _ProjectionFields:
    """严格解码由 Pipeline 自己写入的 Projection metadata。"""
    metadata = candidate.metadata
    unit_id = metadata.get("unit_id")
    key = metadata.get("key")
    status = metadata.get("target_status")
    markdown_hash = metadata.get("markdown_hash")
    valid_from = metadata.get("valid_from")
    valid_until = metadata.get("valid_until")
    if (
        not isinstance(unit_id, str)
        or not isinstance(key, str)
        or status not in {"short_term", "active", "review_required"}
        or not isinstance(markdown_hash, str)
        or not isinstance(valid_from, str)
        or (valid_until is not None and not isinstance(valid_until, str))
    ):
        raise MemoryStateError("memory candidate projection metadata is invalid")
    try:
        parsed_start = datetime.fromisoformat(valid_from)
        parsed_end = None if valid_until is None else datetime.fromisoformat(valid_until)
    except ValueError:
        raise MemoryStateError("memory candidate projection time is invalid") from None
    if parsed_start.tzinfo is None or (parsed_end is not None and parsed_end.tzinfo is None):
        raise MemoryStateError("memory candidate projection time is invalid")
    start = parsed_start.astimezone(UTC)
    end = None if parsed_end is None else parsed_end.astimezone(UTC)
    return _ProjectionFields(
        unit_id,
        key,
        status,
        markdown_hash,
        start,
        end,
    )
