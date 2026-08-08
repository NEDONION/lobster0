"""供 Bridge/TUI 使用的本地 Owner Memory Console。"""

from collections.abc import Callable

from miniclaw.memory.models import DisclosureContext
from miniclaw.memory.repository import MemoryUnit
from miniclaw.memory.retrieval import MemoryRetrieval, SearchRequest
from miniclaw.providers.base import JsonValue
from miniclaw.storage.database import Database


class MemoryConsole:
    """把本地命令绑定到唯一 Owner，统一返回可安全显示的 JSON 数据。"""

    def __init__(
        self,
        database: Database,
        owner_id: int,
        retrieval: MemoryRetrieval,
        schedule_flush: Callable[[], None],
    ) -> None:
        """绑定数据库、Owner、Retrieval 与非阻塞 Flush 信号。"""
        self._database = database
        self._owner_id = owner_id
        self._retrieval = retrieval
        self._schedule_flush = schedule_flush
        self._disclosure = DisclosureContext(owner_id, owner_id, "cli", "local", True)

    def command(
        self,
        *,
        action: JsonValue,
        query: JsonValue = None,
        unit_id: JsonValue = None,
        limit: JsonValue = 10,
    ) -> dict[str, JsonValue]:
        """执行已由 Bridge 校验的 status/list/search/why/flush 命令。"""
        if not isinstance(action, str):
            raise ValueError("memory action is invalid")
        if type(limit) is not int:
            raise ValueError("memory limit is invalid")
        if action == "status":
            return self._status()
        if action == "flush":
            self._schedule_flush()
            return {"scheduled": True}
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
