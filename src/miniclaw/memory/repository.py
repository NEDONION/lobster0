"""Memory Autopilot 的 SQLite 状态机与 Owner-scoped Repository。"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import cast

from miniclaw.memory.models import SourceRef
from miniclaw.providers.base import JsonValue
from miniclaw.storage.database import Database

_RUN_TERMINAL = frozenset({"completed", "dead_letter"})
_UNIT_STATUSES = frozenset(
    {
        "observed",
        "short_term",
        "review_required",
        "active",
        "rejected",
        "superseded",
        "archived",
        "expired",
    }
)


class MemoryStateError(RuntimeError):
    """表示 Memory 状态转换、Owner 边界或来源关联非法。"""


class MemoryDataError(RuntimeError):
    """表示 SQLite 中的 Memory 行无法按稳定契约解码。"""


@dataclass(frozen=True, slots=True)
class MemoryFlushRun:
    """描述一个可租约、重试并跨重启恢复的 Flush Run。"""

    id: int
    owner_id: int
    first_message_id: int
    last_message_id: int
    extractor: str
    prompt_hash: str
    status: str
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempts: int
    next_attempt_at: datetime | None
    last_error_code: str | None
    markdown_committed_at: datetime | None
    projection_committed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """描述 Provider 输出经严格解析后进入验证状态机的候选。"""

    id: int
    run_id: int
    ordinal: int
    candidate_hash: str
    text: str
    kind: str
    scope: str
    confidence: float
    sensitivity: str
    status: str
    source_message_ids: tuple[int, ...]
    metadata: dict[str, JsonValue]
    rejection_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryUnit:
    """描述一个带完整来源链和生命周期状态的结构化 Memory Unit。"""

    id: str
    owner_id: int
    candidate_id: int | None
    key: str
    text: str
    text_hash: str
    kind: str
    scope: str
    status: str
    confidence: float
    sensitivity: str
    valid_from: datetime
    valid_until: datetime | None
    supersedes_unit_id: str | None
    markdown_hash: str | None
    search_shadow: str
    sources: tuple[SourceRef, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryReview:
    """描述一个绑定预览哈希且只能由 Owner 决策的 Review。"""

    id: int
    owner_id: int
    candidate_id: int | None
    unit_id: str | None
    review_type: str
    preview_hash: str
    requested_transition: str
    payload: dict[str, JsonValue]
    status: str
    created_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryManifest:
    """描述一个 Owner Markdown 文件的最后已验证投影版本。"""

    owner_id: int
    relative_path: str
    content_hash: str
    last_valid_hash: str
    mtime_ns: int
    parser_version: str
    status: str
    last_scanned_at: datetime


class MemoryRunRepository:
    """持久化 Flush Run，并用 ``BEGIN IMMEDIATE`` 串行化 lease claim。"""

    def __init__(self, database: Database) -> None:
        """绑定已经迁移到 Memory v3 Schema 的数据库。"""
        self._database = database

    def enqueue(
        self,
        *,
        owner_id: int,
        first_message_id: int,
        last_message_id: int,
        extractor: str,
        prompt_hash: str,
        now: datetime | None = None,
    ) -> MemoryFlushRun:
        """幂等创建一个 source range Flush Run，并验证端点属于 Owner。"""
        _require_positive(owner_id, "owner_id")
        _require_positive(first_message_id, "first_message_id")
        _require_positive(last_message_id, "last_message_id")
        if first_message_id > last_message_id:
            raise MemoryStateError("memory source range is invalid")
        version = _require_text(extractor, "extractor", maximum=120)
        digest = _require_hash(prompt_hash, "prompt_hash")
        timestamp = _utc_text(now or datetime.now(UTC))
        with self._database.connect() as connection:
            _validate_owner_range(
                connection,
                owner_id,
                first_message_id,
                last_message_id,
            )
            connection.execute(
                """
                INSERT INTO memory_flush_runs (
                    owner_id, first_message_id, last_message_id, extractor_version,
                    prompt_hash, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                ON CONFLICT(
                    owner_id, first_message_id, last_message_id,
                    extractor_version, prompt_hash
                ) DO NOTHING
                """,
                (
                    owner_id,
                    first_message_id,
                    last_message_id,
                    version,
                    digest,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM memory_flush_runs
                WHERE owner_id = ? AND first_message_id = ? AND last_message_id = ?
                    AND extractor_version = ? AND prompt_hash = ?
                """,
                (owner_id, first_message_id, last_message_id, version, digest),
            ).fetchone()
        assert row is not None
        return _flush_run(row)

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> MemoryFlushRun | None:
        """原子 claim 到期的 queued/retry/stale Run，并限制每 Owner 一个活跃 lease。"""
        worker = _require_text(worker_id, "worker_id", maximum=120)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        current = _utc_text(now)
        expires = _utc_text(_aware_utc(now) + timedelta(seconds=lease_seconds))
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT candidate.id
                FROM memory_flush_runs AS candidate
                WHERE (
                    candidate.status = 'queued'
                    OR (
                        candidate.status = 'retry'
                        AND candidate.next_attempt_at IS NOT NULL
                        AND candidate.next_attempt_at <= ?
                    )
                    OR (
                        candidate.status = 'running'
                        AND candidate.lease_expires_at <= ?
                    )
                )
                AND NOT EXISTS (
                    SELECT 1 FROM memory_flush_runs AS active
                    WHERE active.owner_id = candidate.owner_id
                        AND active.status = 'running'
                        AND active.lease_expires_at > ?
                )
                ORDER BY candidate.owner_id, candidate.id
                LIMIT 1
                """,
                (current, current, current),
            ).fetchone()
            if row is None:
                return None
            run_id = int(row[0])
            updated = connection.execute(
                """
                UPDATE memory_flush_runs
                SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                    attempts = attempts + 1, next_attempt_at = NULL, updated_at = ?
                WHERE id = ? AND status NOT IN ('completed', 'dead_letter')
                """,
                (worker, expires, current, run_id),
            ).rowcount
            if updated != 1:
                raise MemoryStateError("memory run could not be claimed")
            claimed = connection.execute(
                "SELECT * FROM memory_flush_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        assert claimed is not None
        return _flush_run(claimed)

    def mark_retry(
        self,
        run_id: int,
        worker_id: str,
        *,
        error_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> MemoryFlushRun:
        """把当前 Worker 的 running Run 转为带到期时间的 retry。"""
        next_time = _aware_utc(next_attempt_at)
        current_time = _aware_utc(now)
        if next_time < current_time:
            raise ValueError("next_attempt_at must not precede now")
        return self._transition_owned_run(
            run_id,
            worker_id,
            status="retry",
            now=current_time,
            extra=(
                "next_attempt_at = ?, last_error_code = ?",
                (
                    _utc_text(next_time),
                    _require_text(error_code, "error_code", maximum=120),
                ),
            ),
        )

    def mark_markdown_committed(
        self,
        run_id: int,
        worker_id: str,
        *,
        now: datetime,
    ) -> MemoryFlushRun:
        """记录 Markdown 已成为真相源，并进入可恢复的 Projection 阶段。"""
        timestamp = _aware_utc(now)
        return self._transition_owned_run(
            run_id,
            worker_id,
            status="projection_pending",
            now=timestamp,
            extra=("markdown_committed_at = ?", (_utc_text(timestamp),)),
        )

    def complete_projection(self, run_id: int, *, now: datetime) -> MemoryFlushRun:
        """只把 projection_pending Run 原子结算为 completed。"""
        _require_positive(run_id, "run_id")
        timestamp = _utc_text(now)
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE memory_flush_runs
                SET status = 'completed', projection_committed_at = ?, completed_at = ?,
                    updated_at = ?, last_error_code = NULL
                WHERE id = ? AND status = 'projection_pending'
                """,
                (timestamp, timestamp, timestamp, run_id),
            ).rowcount
            if updated != 1:
                raise MemoryStateError("memory run is not projection_pending")
            row = connection.execute(
                "SELECT * FROM memory_flush_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        assert row is not None
        return _flush_run(row)

    def mark_dead_letter(
        self,
        run_id: int,
        worker_id: str,
        *,
        error_code: str,
        now: datetime,
    ) -> MemoryFlushRun:
        """把不可恢复 running Run 结算为不可回退的 dead_letter。"""
        timestamp = _aware_utc(now)
        return self._transition_owned_run(
            run_id,
            worker_id,
            status="dead_letter",
            now=timestamp,
            extra=(
                "last_error_code = ?, completed_at = ?",
                (
                    _require_text(error_code, "error_code", maximum=120),
                    _utc_text(timestamp),
                ),
            ),
        )

    def get(self, run_id: int) -> MemoryFlushRun:
        """按内部 ID 读取一个 Run，缺失或损坏时稳定失败。"""
        _require_positive(run_id, "run_id")
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM memory_flush_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise MemoryStateError("memory run does not exist")
        return _flush_run(row)

    def _transition_owned_run(
        self,
        run_id: int,
        worker_id: str,
        *,
        status: str,
        now: datetime,
        extra: tuple[str, tuple[object, ...]],
    ) -> MemoryFlushRun:
        """实现 running + lease owner 条件绑定的单次转换。"""
        _require_positive(run_id, "run_id")
        worker = _require_text(worker_id, "worker_id", maximum=120)
        if status in _RUN_TERMINAL | {"retry", "projection_pending"}:
            pass
        else:
            raise ValueError("memory run transition target is invalid")
        assignments, values = extra
        timestamp = _utc_text(now)
        sql = (
            "UPDATE memory_flush_runs SET status = ?, lease_owner = NULL, "
            "lease_expires_at = NULL, updated_at = ?, "
            f"{assignments} WHERE id = ? AND status = 'running' AND lease_owner = ?"
        )
        with self._database.connect() as connection:
            updated = connection.execute(
                sql,
                (status, timestamp, *values, run_id, worker),
            ).rowcount
            if updated != 1:
                raise MemoryStateError("memory run lease or state changed")
            row = connection.execute(
                "SELECT * FROM memory_flush_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        assert row is not None
        return _flush_run(row)


class MemoryCandidateRepository:
    """持久化有界 Candidate 及其来源 ID，不接受任意 Provider JSON。"""

    def __init__(self, database: Database) -> None:
        """绑定 Candidate 所在的数据库。"""
        self._database = database

    def create(
        self,
        *,
        run_id: int,
        ordinal: int,
        text: str,
        kind: str,
        scope: str,
        confidence: float,
        sensitivity: str,
        source_message_ids: tuple[int, ...],
        metadata: dict[str, JsonValue] | None = None,
        now: datetime | None = None,
    ) -> MemoryCandidate:
        """创建 observed Candidate，并由规范字段计算幂等哈希。"""
        _require_positive(run_id, "run_id")
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")
        normalized = _require_text(text, "candidate text", maximum=2_000)
        category = _require_text(kind, "candidate kind", maximum=120)
        if scope not in {"private", "public", "group"}:
            raise ValueError("candidate scope is invalid")
        if sensitivity not in {"low", "medium", "high", "secret"}:
            raise ValueError("candidate sensitivity is invalid")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise ValueError("candidate confidence is invalid")
        numeric_confidence = float(confidence)
        if not 0 <= numeric_confidence <= 1:
            raise ValueError("candidate confidence is invalid")
        sources = _positive_ids(source_message_ids, "source_message_ids")
        metadata_json = _encode_object(metadata or {}, "candidate metadata")
        source_json = json.dumps(sources, separators=(",", ":"))
        candidate_hash = hashlib.sha256(
            json.dumps(
                {
                    "kind": category,
                    "scope": scope,
                    "sources": sources,
                    "text": normalized,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        timestamp = _utc_text(now or datetime.now(UTC))
        with self._database.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO memory_candidates (
                        run_id, ordinal, candidate_hash, text, kind, scope, confidence,
                        sensitivity, status, source_ids_json, metadata_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'observed', ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        ordinal,
                        candidate_hash,
                        normalized,
                        category,
                        scope,
                        numeric_confidence,
                        sensitivity,
                        source_json,
                        metadata_json,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise MemoryStateError("memory candidate already exists or is invalid") from error
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE run_id = ? AND ordinal = ?",
                (run_id, ordinal),
            ).fetchone()
        assert row is not None
        return _candidate(row)

    def get(self, candidate_id: int) -> MemoryCandidate:
        """读取并严格解码一个 Candidate。"""
        _require_positive(candidate_id, "candidate_id")
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise MemoryStateError("memory candidate does not exist")
        return _candidate(row)


class MemoryUnitRepository:
    """持久化 Owner-scoped Memory Unit、来源链和生命周期状态。"""

    def __init__(self, database: Database) -> None:
        """绑定 Unit 与 Source 表所在数据库。"""
        self._database = database

    def create(
        self,
        *,
        unit_id: str,
        owner_id: int,
        key: str,
        text: str,
        kind: str,
        scope: str,
        status: str,
        confidence: float,
        sensitivity: str,
        valid_from: datetime,
        valid_until: datetime | None,
        sources: tuple[SourceRef, ...],
        candidate_id: int | None = None,
        supersedes_unit_id: str | None = None,
        markdown_hash: str | None = None,
        search_shadow: str = "",
        now: datetime | None = None,
    ) -> MemoryUnit:
        """原子创建 Unit 及全部可核验 SourceRef。"""
        identifier = _require_text(unit_id, "unit_id", maximum=160)
        _require_positive(owner_id, "owner_id")
        memory_key = _require_text(key, "memory key", maximum=200)
        normalized = _require_text(text, "memory text", maximum=8_000)
        category = _require_text(kind, "memory kind", maximum=120)
        if scope not in {"private", "public", "group"}:
            raise ValueError("memory scope is invalid")
        if status not in _UNIT_STATUSES:
            raise ValueError("memory status is invalid")
        if sensitivity not in {"low", "medium", "high"}:
            raise ValueError("memory sensitivity is invalid")
        numeric_confidence = _confidence(confidence)
        start = _aware_utc(valid_from)
        end = None if valid_until is None else _aware_utc(valid_until)
        if end is not None and end <= start:
            raise ValueError("valid_until must follow valid_from")
        if not sources:
            raise MemoryStateError("memory unit requires at least one source")
        text_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if markdown_hash is not None:
            markdown_hash = _require_hash(markdown_hash, "markdown_hash")
        timestamp = _utc_text(now or datetime.now(UTC))
        with self._database.connect() as connection:
            _validate_sources(connection, owner_id, sources)
            try:
                connection.execute(
                    """
                    INSERT INTO memory_units (
                        id, owner_id, candidate_id, memory_key, text, text_hash,
                        kind, scope, status, confidence, sensitivity, valid_from,
                        valid_until, supersedes_unit_id, markdown_hash, search_shadow,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        owner_id,
                        candidate_id,
                        memory_key,
                        normalized,
                        text_hash,
                        category,
                        scope,
                        status,
                        numeric_confidence,
                        sensitivity,
                        _utc_text(start),
                        None if end is None else _utc_text(end),
                        supersedes_unit_id,
                        markdown_hash,
                        search_shadow,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO memory_sources (
                        unit_id, message_id, session_id, channel, ordinal
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            identifier,
                            source.message_id,
                            source.session_id,
                            source.channel,
                            ordinal,
                        )
                        for ordinal, source in enumerate(sources)
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise MemoryStateError("memory unit already exists or is invalid") from error
        return self.get(owner_id, identifier)

    def validate_sources(self, owner_id: int, sources: tuple[SourceRef, ...]) -> None:
        """在 Markdown 写入前验证全部 SourceRef 属于当前 Owner。"""
        _require_positive(owner_id, "owner_id")
        if not sources:
            raise MemoryStateError("memory unit requires at least one source")
        with self._database.connect_read_only() as connection:
            _validate_sources(connection, owner_id, sources)

    def find(self, owner_id: int, unit_id: str) -> MemoryUnit | None:
        """按 Owner 和 Unit ID 查询；跨 Owner 与缺失统一返回 None。"""
        _require_positive(owner_id, "owner_id")
        identifier = _require_text(unit_id, "unit_id", maximum=160)
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM memory_units WHERE owner_id = ? AND id = ?",
                (owner_id, identifier),
            ).fetchone()
            if row is None:
                return None
            sources = connection.execute(
                """
                SELECT message_id, session_id, channel FROM memory_sources
                WHERE unit_id = ? ORDER BY ordinal
                """,
                (identifier,),
            ).fetchall()
        return _unit(row, sources)

    def get(self, owner_id: int, unit_id: str) -> MemoryUnit:
        """按 Owner 读取 Unit，缺失时不泄露是否属于其他 Owner。"""
        unit = self.find(owner_id, unit_id)
        if unit is None:
            raise MemoryStateError("memory unit does not exist")
        return unit


class MemoryReviewRepository:
    """持久化与 Owner、预览哈希和目标转换绑定的 Review。"""

    def __init__(self, database: Database) -> None:
        """绑定 Review 表所在数据库。"""
        self._database = database

    def create(
        self,
        *,
        owner_id: int,
        review_type: str,
        preview_hash: str,
        requested_transition: str,
        unit_id: str | None,
        payload: dict[str, JsonValue],
        candidate_id: int | None = None,
        now: datetime | None = None,
    ) -> MemoryReview:
        """幂等创建 pending Review，不在 payload 中保存未约束任意对象。"""
        _require_positive(owner_id, "owner_id")
        if review_type not in {
            "sensitivity",
            "conflict",
            "behavior",
            "correction",
            "forget",
            "weekly",
        }:
            raise ValueError("memory review type is invalid")
        digest = _require_hash(preview_hash, "preview_hash")
        transition = _require_text(
            requested_transition,
            "requested_transition",
            maximum=120,
        )
        payload_json = _encode_object(payload, "review payload")
        timestamp = _utc_text(now or datetime.now(UTC))
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_reviews (
                    owner_id, candidate_id, unit_id, review_type, preview_hash,
                    requested_transition, payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(owner_id, preview_hash) DO NOTHING
                """,
                (
                    owner_id,
                    candidate_id,
                    unit_id,
                    review_type,
                    digest,
                    transition,
                    payload_json,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_reviews WHERE owner_id = ? AND preview_hash = ?",
                (owner_id, digest),
            ).fetchone()
        assert row is not None
        return _review(row)

    def get(self, owner_id: int, review_id: int) -> MemoryReview:
        """按 Owner 读取 Review，并严格校验持久 JSON。"""
        _require_positive(owner_id, "owner_id")
        _require_positive(review_id, "review_id")
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM memory_reviews WHERE owner_id = ? AND id = ?",
                (owner_id, review_id),
            ).fetchone()
        if row is None:
            raise MemoryStateError("memory review does not exist")
        return _review(row)


class MemoryManifestRepository:
    """保存 Markdown 文件 hash/mtime，用于检测直接编辑和投影漂移。"""

    def __init__(self, database: Database) -> None:
        """绑定 Manifest 表所在数据库。"""
        self._database = database

    def find(self, owner_id: int, relative_path: str) -> MemoryManifest | None:
        """按 Owner 和受限相对路径读取 Manifest。"""
        _require_positive(owner_id, "owner_id")
        normalized = _relative_path(relative_path)
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_manifests
                WHERE owner_id = ? AND relative_path = ?
                """,
                (owner_id, normalized),
            ).fetchone()
        return None if row is None else _manifest(row)

    def upsert(
        self,
        *,
        owner_id: int,
        relative_path: str,
        content_hash: str,
        last_valid_hash: str,
        mtime_ns: int,
        parser_version: str,
        status: str,
        now: datetime | None = None,
    ) -> MemoryManifest:
        """原子记录刚完成 fsync/replace 的 Markdown 版本。"""
        _require_positive(owner_id, "owner_id")
        normalized = _relative_path(relative_path)
        digest = _require_hash(content_hash, "content_hash")
        valid_digest = _require_hash(last_valid_hash, "last_valid_hash")
        if type(mtime_ns) is not int or mtime_ns < 0:
            raise ValueError("mtime_ns must be a non-negative integer")
        parser = _require_text(parser_version, "parser_version", maximum=80)
        if status not in {"current", "drift", "error"}:
            raise ValueError("manifest status is invalid")
        timestamp = _utc_text(now or datetime.now(UTC))
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_manifests (
                    owner_id, relative_path, content_hash, last_valid_hash,
                    mtime_ns, parser_version, status, last_scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, relative_path) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    last_valid_hash = excluded.last_valid_hash,
                    mtime_ns = excluded.mtime_ns,
                    parser_version = excluded.parser_version,
                    status = excluded.status,
                    last_scanned_at = excluded.last_scanned_at
                """,
                (
                    owner_id,
                    normalized,
                    digest,
                    valid_digest,
                    mtime_ns,
                    parser,
                    status,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM memory_manifests
                WHERE owner_id = ? AND relative_path = ?
                """,
                (owner_id, normalized),
            ).fetchone()
        assert row is not None
        return _manifest(row)

def _flush_run(row: sqlite3.Row) -> MemoryFlushRun:
    """把 SQLite Row 严格恢复为 MemoryFlushRun。"""
    return MemoryFlushRun(
        id=int(row["id"]),
        owner_id=int(row["owner_id"]),
        first_message_id=int(row["first_message_id"]),
        last_message_id=int(row["last_message_id"]),
        extractor=str(row["extractor_version"]),
        prompt_hash=str(row["prompt_hash"]),
        status=str(row["status"]),
        lease_owner=cast(str | None, row["lease_owner"]),
        lease_expires_at=_optional_time(row["lease_expires_at"]),
        attempts=int(row["attempts"]),
        next_attempt_at=_optional_time(row["next_attempt_at"]),
        last_error_code=cast(str | None, row["last_error_code"]),
        markdown_committed_at=_optional_time(row["markdown_committed_at"]),
        projection_committed_at=_optional_time(row["projection_committed_at"]),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        completed_at=_optional_time(row["completed_at"]),
    )


def _candidate(row: sqlite3.Row) -> MemoryCandidate:
    """把 Candidate Row 连同严格 JSON 字段恢复为公共对象。"""
    raw_sources = _decode_json(row["source_ids_json"], "candidate sources")
    if not isinstance(raw_sources, list) or any(
        type(value) is not int or value <= 0 for value in raw_sources
    ):
        raise MemoryDataError("memory candidate sources are invalid")
    metadata = _decode_object(row["metadata_json"], "candidate metadata")
    return MemoryCandidate(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        ordinal=int(row["ordinal"]),
        candidate_hash=str(row["candidate_hash"]),
        text=str(row["text"]),
        kind=str(row["kind"]),
        scope=str(row["scope"]),
        confidence=float(row["confidence"]),
        sensitivity=str(row["sensitivity"]),
        status=str(row["status"]),
        source_message_ids=tuple(raw_sources),
        metadata=metadata,
        rejection_code=cast(str | None, row["rejection_code"]),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _unit(row: sqlite3.Row, source_rows: list[sqlite3.Row]) -> MemoryUnit:
    """把 Unit 与已排序 Source rows 恢复为不可变对象。"""
    return MemoryUnit(
        id=str(row["id"]),
        owner_id=int(row["owner_id"]),
        candidate_id=cast(int | None, row["candidate_id"]),
        key=str(row["memory_key"]),
        text=str(row["text"]),
        text_hash=str(row["text_hash"]),
        kind=str(row["kind"]),
        scope=str(row["scope"]),
        status=str(row["status"]),
        confidence=float(row["confidence"]),
        sensitivity=str(row["sensitivity"]),
        valid_from=_parse_time(row["valid_from"]),
        valid_until=_optional_time(row["valid_until"]),
        supersedes_unit_id=cast(str | None, row["supersedes_unit_id"]),
        markdown_hash=cast(str | None, row["markdown_hash"]),
        search_shadow=str(row["search_shadow"]),
        sources=tuple(
            SourceRef(
                message_id=int(source["message_id"]),
                session_id=int(source["session_id"]),
                channel=str(source["channel"]),
            )
            for source in source_rows
        ),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _review(row: sqlite3.Row) -> MemoryReview:
    """把 Review Row 严格恢复为公共对象。"""
    return MemoryReview(
        id=int(row["id"]),
        owner_id=int(row["owner_id"]),
        candidate_id=cast(int | None, row["candidate_id"]),
        unit_id=cast(str | None, row["unit_id"]),
        review_type=str(row["review_type"]),
        preview_hash=str(row["preview_hash"]),
        requested_transition=str(row["requested_transition"]),
        payload=_decode_object(row["payload_json"], "memory review payload"),
        status=str(row["status"]),
        created_at=_parse_time(row["created_at"]),
        decided_at=_optional_time(row["decided_at"]),
    )


def _manifest(row: sqlite3.Row) -> MemoryManifest:
    """把 Manifest Row 严格恢复为公共对象。"""
    return MemoryManifest(
        owner_id=int(row["owner_id"]),
        relative_path=str(row["relative_path"]),
        content_hash=str(row["content_hash"]),
        last_valid_hash=str(row["last_valid_hash"]),
        mtime_ns=int(row["mtime_ns"]),
        parser_version=str(row["parser_version"]),
        status=str(row["status"]),
        last_scanned_at=_parse_time(row["last_scanned_at"]),
    )


def _validate_owner_range(
    connection: sqlite3.Connection,
    owner_id: int,
    first_message_id: int,
    last_message_id: int,
) -> None:
    """验证 range 两个端点都属于同一个 Owner。"""
    rows = connection.execute(
        """
        SELECT messages.id
        FROM messages
        JOIN sessions ON sessions.id = messages.session_id
        WHERE messages.id IN (?, ?) AND sessions.user_id = ?
        """,
        (first_message_id, last_message_id, owner_id),
    ).fetchall()
    expected = {first_message_id, last_message_id}
    if {int(row[0]) for row in rows} != expected:
        raise MemoryStateError("memory source range does not belong to owner")


def _validate_sources(
    connection: sqlite3.Connection,
    owner_id: int,
    sources: tuple[SourceRef, ...],
) -> None:
    """验证每个来源的 Message、Session、Channel 和 Owner 关联。"""
    if len({source.message_id for source in sources}) != len(sources):
        raise MemoryStateError("memory sources contain duplicate messages")
    for source in sources:
        row = connection.execute(
            """
            SELECT sessions.user_id, sessions.channel
            FROM messages
            JOIN sessions ON sessions.id = messages.session_id
            WHERE messages.id = ? AND messages.session_id = ?
            """,
            (source.message_id, source.session_id),
        ).fetchone()
        if row is None or int(row["user_id"]) != owner_id or row["channel"] != source.channel:
            raise MemoryStateError("memory source does not belong to owner session")


def _require_positive(value: int, field: str) -> None:
    """拒绝 bool、零和负数形式的内部 ID。"""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")


def _require_text(value: str, field: str, *, maximum: int) -> str:
    """规范化有界单行标识或有界多行正文。"""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} is invalid")
    if "\x00" in value:
        raise ValueError(f"{field} is invalid")
    return value.strip()


def _require_hash(value: str, field: str) -> str:
    """验证小写 64 位 SHA-256 文本。"""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 hash")
    return value


def _relative_path(value: str) -> str:
    """只接受不含父跳转的 POSIX 相对路径。"""
    normalized = _require_text(value, "relative_path", maximum=240)
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("relative_path is invalid")
    return path.as_posix()


def _positive_ids(values: tuple[int, ...], field: str) -> tuple[int, ...]:
    """验证非空、去重且保持顺序的正整数 ID 元组。"""
    if not values or any(type(value) is not int or value <= 0 for value in values):
        raise ValueError(f"{field} is invalid")
    if len(set(values)) != len(values):
        raise ValueError(f"{field} contains duplicates")
    return values


def _confidence(value: float) -> float:
    """把非 bool 数字收窄到闭区间置信度。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("memory confidence is invalid")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError("memory confidence is invalid")
    return result


def _aware_utc(value: datetime) -> datetime:
    """要求带时区时间并统一转换为 UTC。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("memory timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    """把带时区时间编码为可字典序比较的 UTC ISO 文本。"""
    return _aware_utc(value).isoformat()


def _parse_time(value: object) -> datetime:
    """严格解析 SQLite 中的带时区 ISO 时间。"""
    if not isinstance(value, str):
        raise MemoryDataError("memory timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MemoryDataError("memory timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise MemoryDataError("memory timestamp is invalid")
    return parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    """解析可空的 SQLite 时间字段。"""
    return None if value is None else _parse_time(value)


def _encode_object(value: dict[str, JsonValue], field: str) -> str:
    """把有界标准 JSON object 编码为确定性文本。"""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} is not standard JSON") from error
    if len(encoded.encode("utf-8")) > 16_384:
        raise ValueError(f"{field} is too large")
    return encoded


def _decode_json(value: object, field: str) -> JsonValue:
    """严格解码标准 JSON，拒绝 NaN/Infinity 与非文本列。"""
    if not isinstance(value, str):
        raise MemoryDataError(f"{field} is invalid")
    try:
        return cast(
            JsonValue,
            json.loads(
                value,
                parse_constant=lambda constant: (_raise_json_constant(constant)),
            ),
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise MemoryDataError(f"{field} is invalid") from error


def _decode_object(value: object, field: str) -> dict[str, JsonValue]:
    """解码并要求顶层为 JSON object。"""
    decoded = _decode_json(value, field)
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise MemoryDataError(f"{field} is invalid")
    return cast(dict[str, JsonValue], decoded)


def _raise_json_constant(constant: str) -> JsonValue:
    """让 json.loads 对非标准数值常量稳定失败。"""
    raise ValueError(f"non-standard JSON constant: {constant}")
