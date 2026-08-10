"""执行 Memory TTL、周审候选、对账、legacy 导入和 lease 回收。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from lobster0.memory.markdown_store import MarkdownUnitDocument, MemoryMarkdownStore
from lobster0.memory.migration import LegacyMemoryImporter, LegacyMigrationResult
from lobster0.memory.reconcile import MemoryReconciler, ReconcileResult
from lobster0.memory.repository import (
    MemoryReviewRepository,
    MemoryUnit,
    MemoryUnitRepository,
)
from lobster0.memory.review import review_preview_hash
from lobster0.storage.database import Database


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    """汇总一轮维护中的过期、Review、lease 和可选扫描结果。"""

    expired_unit_ids: tuple[str, ...]
    weekly_review_id: int | None
    reclaimed_leases: int
    reconcile: ReconcileResult | None
    migration: LegacyMigrationResult | None


class MemoryMaintenance:
    """以幂等短事务执行可重复的 Memory 后台维护。"""

    def __init__(
        self,
        database: Database,
        markdown: MemoryMarkdownStore,
        units: MemoryUnitRepository,
        reviews: MemoryReviewRepository,
        *,
        reconciler: MemoryReconciler | None = None,
        importer: LegacyMemoryImporter | None = None,
    ) -> None:
        """绑定 Projection、Markdown、Review 及可选对账/迁移组件。"""
        self._database = database
        self._markdown = markdown
        self._units = units
        self._reviews = reviews
        self._reconciler = reconciler
        self._importer = importer

    def run_due(
        self,
        owner_id: int,
        *,
        now: datetime | None = None,
    ) -> MaintenanceResult:
        """执行一轮有界维护；所有时间与 Owner 均由 Core 提供。"""
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("memory owner_id is invalid")
        current = _aware(now or datetime.now(UTC))
        reconcile = (
            None
            if self._reconciler is None
            else self._reconciler.scan(owner_id, now=current)
        )
        migration = (
            None
            if self._importer is None or (reconcile is not None and reconcile.errors)
            else self._importer.import_all(owner_id, now=current)
        )
        expired = (
            ()
            if reconcile is not None and reconcile.errors
            else self._expire(owner_id, current)
        )
        weekly_review_id = self._weekly_review(owner_id, current)
        reclaimed = self._reclaim_leases(owner_id, current)
        return MaintenanceResult(
            expired,
            weekly_review_id,
            reclaimed,
            reconcile,
            migration,
        )

    def _expire(self, owner_id: int, now: datetime) -> tuple[str, ...]:
        """Markdown-first 把到期非终态 Unit 转成 expired 并保留来源。"""
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT id FROM memory_units
                WHERE owner_id = ?
                    AND status IN ('active', 'short_term', 'review_required')
                    AND valid_until IS NOT NULL AND valid_until <= ?
                ORDER BY id
                """,
                (owner_id, now.isoformat()),
            ).fetchall()
        units = tuple(self._units.get(owner_id, str(row["id"])) for row in rows)
        if not units:
            return ()
        write = self._markdown.upsert_many(
            tuple(_document(unit, "expired") for unit in units)
        )
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for unit in units:
                changed = connection.execute(
                    """
                    UPDATE memory_units
                    SET status = 'expired', markdown_hash = ?, updated_at = ?
                    WHERE owner_id = ? AND id = ? AND status = ? AND text_hash = ?
                    """,
                    (
                        write.block_hashes[unit.id],
                        now.isoformat(),
                        owner_id,
                        unit.id,
                        unit.status,
                        unit.text_hash,
                    ),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("memory expiry target changed")
                connection.execute(
                    """
                    INSERT INTO memory_audit (
                        owner_id, event_type, unit_id, reason_code,
                        metadata_json, created_at
                    ) VALUES (?, 'unit_expired', ?, 'ttl_elapsed', '{}', ?)
                    """,
                    (owner_id, unit.id, now.isoformat()),
                )
        return tuple(unit.id for unit in units)

    def _weekly_review(self, owner_id: int, now: datetime) -> int | None:
        """每个 ISO 周为一个 active Unit 创建至多一个只读 retain Review。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT id FROM memory_units
                WHERE owner_id = ? AND status = 'active'
                ORDER BY updated_at, id LIMIT 1
                """,
                (owner_id,),
            ).fetchone()
        if row is None:
            return None
        unit = self._units.get(owner_id, str(row["id"]))
        target_hash = review_preview_hash(
            unit,
            review_type="weekly",
            requested_transition="retain",
        )
        iso = now.isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
        preview_hash = hashlib.sha256(f"{target_hash}\0{week}".encode()).hexdigest()
        review = self._reviews.create(
            owner_id=owner_id,
            review_type="weekly",
            preview_hash=preview_hash,
            requested_transition="retain",
            unit_id=unit.id,
            payload={
                "target_preview_hash": target_hash,
                "week": week,
            },
            now=now,
        )
        return review.id

    def _reclaim_leases(self, owner_id: int, now: datetime) -> int:
        """把到期 running Run 送回立即可 claim 的 retry 状态。"""
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                """
                UPDATE memory_flush_runs
                SET status = 'retry', lease_owner = NULL, lease_expires_at = NULL,
                    next_attempt_at = ?, last_error_code = 'memory_lease_expired',
                    updated_at = ?
                WHERE owner_id = ? AND status = 'running' AND lease_expires_at <= ?
                """,
                (now.isoformat(), now.isoformat(), owner_id, now.isoformat()),
            ).rowcount
            if count:
                connection.execute(
                    """
                    INSERT INTO memory_audit (
                        owner_id, event_type, reason_code, metadata_json, created_at
                    ) VALUES (?, 'leases_reclaimed', 'memory_lease_expired', ?, ?)
                    """,
                    (
                        owner_id,
                        json.dumps({"count": count}, separators=(",", ":")),
                        now.isoformat(),
                    ),
                )
        return count


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


def _aware(value: datetime) -> datetime:
    """要求 timezone-aware datetime 并统一为 UTC。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("memory maintenance time must be timezone-aware")
    return value.astimezone(UTC)
