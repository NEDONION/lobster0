"""供 Bridge/TUI 使用的本地 Owner Memory Console。"""

from collections.abc import Callable
from datetime import UTC, datetime

from lobster0.memory.models import DisclosureContext
from lobster0.memory.reconcile import MemoryReconciler
from lobster0.memory.repository import MemoryUnit
from lobster0.memory.retrieval import MemoryRetrieval, SearchRequest
from lobster0.memory.review import MemoryReviewService, ReviewPreview
from lobster0.providers.base import JsonValue
from lobster0.storage.database import Database


class MemoryConsole:
    """把本地命令绑定到唯一 Owner，统一返回可安全显示的 JSON 数据。"""

    def __init__(
        self,
        database: Database,
        owner_id: int,
        retrieval: MemoryRetrieval,
        governance: MemoryReviewService,
        reconciler: MemoryReconciler,
        schedule_flush: Callable[[], None],
    ) -> None:
        """绑定数据库、Owner、Retrieval、Review 治理与非阻塞 Flush 信号。"""
        self._database = database
        self._owner_id = owner_id
        self._retrieval = retrieval
        self._governance = governance
        self._reconciler = reconciler
        self._schedule_flush = schedule_flush
        self._disclosure = DisclosureContext(owner_id, owner_id, "cli", "local", True)

    def command(
        self,
        *,
        action: JsonValue,
        query: JsonValue = None,
        unit_id: JsonValue = None,
        limit: JsonValue = 10,
        review_id: JsonValue = None,
        preview_hash: JsonValue = None,
    ) -> dict[str, JsonValue]:
        """执行已由 Bridge 校验的查询、Review、Forget 和 Flush 命令。"""
        if not isinstance(action, str):
            raise ValueError("memory action is invalid")
        if type(limit) is not int:
            raise ValueError("memory limit is invalid")
        if action == "status":
            return self._status()
        if action == "flush":
            self._schedule_flush()
            return {"scheduled": True}
        if action == "rebuild":
            result = self._reconciler.scan(self._owner_id, force=True)
            if not result.errors:
                self._retrieval.ensure_projection()
            return {
                "added": len(result.added),
                "updated": len(result.updated),
                "errors": [
                    {"path": item.path, "line": item.line, "code": item.code}
                    for item in result.errors
                ],
                "projection_rebuilt": not result.errors,
            }
        if action == "list":
            return {
                "items": [
                    _unit_summary(item)
                    for item in self._retrieval.list(self._disclosure, limit=limit)
                ]
            }
        if action == "search" and isinstance(query, str):
            result = self._retrieval.search(
                SearchRequest(self._disclosure, query, limit),
            )
            return {
                "items": [_unit_summary(hit.unit) for hit in result.items],
                "reason_code": result.reason_code,
            }
        if action == "why" and isinstance(unit_id, str):
            unit = self._retrieval.get(self._disclosure, unit_id)
            return {"item": None if unit is None else _unit_summary(unit)}
        if action == "review":
            return {
                "items": [
                    _review_summary(item)
                    for item in self._governance.list(self._disclosure, limit=limit)
                ]
            }
        if action == "forget" and isinstance(unit_id, str):
            return _review_summary(
                self._governance.preview_forget(
                    self._disclosure,
                    unit_id,
                    now=datetime.now(UTC),
                )
            )
        if (
            action in {"approve", "reject"}
            and type(review_id) is int
            and isinstance(preview_hash, str)
        ):
            result = self._governance.decide(
                self._disclosure,
                review_id,
                preview_hash,
                approve=action == "approve",
                now=datetime.now(UTC),
            )
            return {
                "review_id": result.review_id,
                "status": result.status,
                "unit_ids": list(result.unit_ids),
            }
        raise ValueError("memory action payload is invalid")

    def _status(self) -> dict[str, JsonValue]:
        """汇总不含正文的 Unit/buffer/review 状态计数。"""
        with self._database.connect_read_only() as connection:
            unit_rows = connection.execute(
                """
                SELECT status, COUNT(*) FROM memory_units
                WHERE owner_id = ? GROUP BY status ORDER BY status
                """,
                (self._owner_id,),
            ).fetchall()
            buffer_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM memory_buffers
                    WHERE owner_id = ? AND status != 'flushed'
                    """,
                    (self._owner_id,),
                ).fetchone()[0]
            )
            review_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM memory_reviews
                    WHERE owner_id = ? AND status = 'pending'
                    """,
                    (self._owner_id,),
                ).fetchone()[0]
            )
        return {
            "units": {str(row[0]): int(row[1]) for row in unit_rows},
            "pending_buffers": buffer_count,
            "pending_reviews": review_count,
        }


def _unit_summary(unit: MemoryUnit) -> dict[str, JsonValue]:
    """编码 Console 可显示的 Unit 与内部证据 ID。"""
    return {
        "unit_id": unit.id,
        "key": unit.key,
        "text": unit.text,
        "status": unit.status,
        "confidence": unit.confidence,
        "source_message_ids": [source.message_id for source in unit.sources],
    }


def _review_summary(review: ReviewPreview) -> dict[str, JsonValue]:
    """编码 Owner 本地 UI 可见且被 hash 绑定的 Review 预览。"""
    return {
        "review_id": review.review_id,
        "review_type": review.review_type,
        "unit_id": review.unit_id,
        "text": review.text,
        "status": review.current_status,
        "requested_transition": review.requested_transition,
        "preview_hash": review.preview_hash,
    }
