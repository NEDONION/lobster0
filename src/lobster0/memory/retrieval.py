"""Owner-scoped SQLite FTS5 Projection 与确定性 Memory Recall。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from lobster0.memory.models import DisclosureContext
from lobster0.memory.policy import MemoryDisclosurePolicy, MemoryPolicyError
from lobster0.memory.repository import MemoryUnit, MemoryUnitRepository
from lobster0.memory.text import memory_search_tokens
from lobster0.storage.database import Database

_RECALL_STATUSES = ("active", "short_term")


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """保存不可扩权的 Disclosure、查询文本和结果上限。"""

    disclosure: DisclosureContext
    query: str
    limit: int = 5


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """描述一条完整 Memory Unit 及其确定性检索分数。"""

    unit: MemoryUnit
    score: float


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    """返回 Recall 命中和不含私人正文的披露原因。"""

    items: tuple[MemoryHit, ...]
    reason_code: str


class MemoryRetrieval:
    """维护可重建 FTS5 Projection，并在 SQL 前执行 Disclosure Policy。"""

    def __init__(
        self,
        database: Database,
        policy: MemoryDisclosurePolicy | None = None,
    ) -> None:
        """绑定数据库，创建 disposable FTS5 Projection 并从 Unit 真相重建。"""
        self._database = database
        self._units = MemoryUnitRepository(database)
        self._policy = policy or MemoryDisclosurePolicy()
        self.ensure_projection()

    def ensure_projection(self) -> None:
        """幂等创建 external-content FTS5 表、同步触发器并重建索引。"""
        with self._database.connect() as connection:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    id UNINDEXED, owner_id UNINDEXED, text, search_shadow,
                    content='memory_units', content_rowid='rowid', tokenize='unicode61'
                )
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memory_units_fts_insert
                AFTER INSERT ON memory_units BEGIN
                    INSERT INTO memory_fts(rowid, id, owner_id, text, search_shadow)
                    VALUES (new.rowid, new.id, new.owner_id, new.text, new.search_shadow);
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memory_units_fts_delete
                AFTER DELETE ON memory_units BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, id, owner_id, text, search_shadow)
                    VALUES ('delete', old.rowid, old.id, old.owner_id, old.text, old.search_shadow);
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memory_units_fts_update
                AFTER UPDATE ON memory_units BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, id, owner_id, text, search_shadow)
                    VALUES ('delete', old.rowid, old.id, old.owner_id, old.text, old.search_shadow);
                    INSERT INTO memory_fts(rowid, id, owner_id, text, search_shadow)
                    VALUES (new.rowid, new.id, new.owner_id, new.text, new.search_shadow);
                END
                """
            )
            connection.execute("INSERT INTO memory_fts(memory_fts) VALUES ('rebuild')")

    def search(
        self,
        request: SearchRequest,
        *,
        now: datetime | None = None,
    ) -> MemorySearchResult:
        """按身份、状态、有效期过滤后执行 FTS5，并加载完整来源链。"""
        if not isinstance(request.query, str) or not request.query.strip():
            raise ValueError("memory search query must not be empty")
        if type(request.limit) is not int or not 1 <= request.limit <= 50:
            raise ValueError("memory search limit must be between 1 and 50")
        reason = self._allow_private(request.disclosure)
        if reason is None:
            return MemorySearchResult((), "memory_disclosure_denied")
        tokens = memory_search_tokens(request.query, maximum=64)
        if not tokens:
            return MemorySearchResult((), reason)
        expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        current = _time_text(now or datetime.now(UTC))
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT memory_units.id, bm25(memory_fts, 0.0, 0.0, 2.0, 1.0) AS rank
                FROM memory_fts
                JOIN memory_units ON memory_units.rowid = memory_fts.rowid
                WHERE memory_fts MATCH ? AND memory_units.owner_id = ?
                    AND memory_units.scope = 'private'
                    AND memory_units.status IN ('active', 'short_term')
                    AND memory_units.valid_from <= ?
                    AND (memory_units.valid_until IS NULL OR memory_units.valid_until > ?)
                ORDER BY rank ASC,
                    CASE memory_units.status WHEN 'active' THEN 0 ELSE 1 END,
                    memory_units.confidence DESC, memory_units.id ASC
                LIMIT ?
                """,
                (
                    expression,
                    request.disclosure.owner_id,
                    current,
                    current,
                    request.limit,
                ),
            ).fetchall()
        return MemorySearchResult(
            tuple(
                MemoryHit(
                    self._units.get(request.disclosure.owner_id, str(row["id"])),
                    -float(row["rank"]),
                )
                for row in rows
            ),
            reason,
        )

    def get(
        self,
        disclosure: DisclosureContext,
        unit_id: str,
        *,
        now: datetime | None = None,
    ) -> MemoryUnit | None:
        """只向已验证 Owner 返回当前可召回 Unit，跨 Owner 与缺失统一为空。"""
        if self._allow_private(disclosure) is None:
            return None
        unit = self._units.find(disclosure.owner_id, unit_id)
        current = now or datetime.now(UTC)
        if unit is None or not _recallable(unit, current):
            return None
        return unit

    def list(
        self,
        disclosure: DisclosureContext,
        *,
        limit: int = 20,
        now: datetime | None = None,
    ) -> tuple[MemoryUnit, ...]:
        """按 active/short-term、置信度和 ID 稳定列出 Owner Memory。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("memory list limit must be between 1 and 100")
        if self._allow_private(disclosure) is None:
            return ()
        current = _time_text(now or datetime.now(UTC))
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT id FROM memory_units
                WHERE owner_id = ? AND scope = 'private'
                    AND status IN ('active', 'short_term')
                    AND valid_from <= ?
                    AND (valid_until IS NULL OR valid_until > ?)
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    confidence DESC, id ASC LIMIT ?
                """,
                (disclosure.owner_id, current, current, limit),
            ).fetchall()
        return tuple(self._units.get(disclosure.owner_id, str(row[0])) for row in rows)

    def _allow_private(self, disclosure: DisclosureContext) -> str | None:
        """返回允许原因；任何身份异常都折叠为 None。"""
        try:
            decision = self._policy.decide(disclosure)
        except MemoryPolicyError:
            return None
        return decision.reason_code if decision.private_access == "full" else None


def _recallable(unit: MemoryUnit, now: datetime) -> bool:
    """判断 Unit 是否满足 v1 私人 Recall 的状态和有效期边界。"""
    current = now.astimezone(UTC)
    return (
        unit.scope == "private"
        and unit.status in _RECALL_STATUSES
        and unit.valid_from <= current
        and (unit.valid_until is None or unit.valid_until > current)
    )


def _time_text(value: datetime) -> str:
    """把带时区 datetime 编成 SQLite 可比较的 UTC ISO 文本。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("memory retrieval time must be timezone-aware")
    return value.astimezone(UTC).isoformat()
