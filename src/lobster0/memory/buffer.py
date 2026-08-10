"""只保存消息引用、不复制私人正文的 durable Memory buffer。"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from lobster0.storage.database import Database


class MemoryBufferStateError(RuntimeError):
    """表示 buffer 来源关联、Owner 边界或状态转换非法。"""


@dataclass(frozen=True, slots=True)
class MemoryBuffer:
    """描述一个已完成 Turn 等待异步 Flush 的消息范围引用。"""

    id: int
    owner_id: int
    session_id: int
    turn_id: int
    first_message_id: int
    last_message_id: int
    capture_scope: str
    status: str
    flush_run_id: int | None
    created_at: datetime
    flushed_at: datetime | None


class MemoryBufferRepository:
    """持久化非阻塞 Capture receipt，并原子绑定 Flush Run。"""

    def __init__(self, database: Database) -> None:
        """绑定已经应用 Memory v3 migration 的数据库。"""
        self._database = database

    def capture(
        self,
        *,
        owner_id: int,
        session_id: int,
        turn_id: int,
        first_message_id: int,
        last_message_id: int,
        capture_scope: str,
        now: datetime | None = None,
    ) -> MemoryBuffer:
        """幂等保存一个 Turn 的 source range，且从不复制 Message 正文。"""
        for name, value in (
            ("owner_id", owner_id),
            ("session_id", session_id),
            ("turn_id", turn_id),
            ("first_message_id", first_message_id),
            ("last_message_id", last_message_id),
        ):
            _positive(value, name)
        if first_message_id > last_message_id:
            raise MemoryBufferStateError("memory buffer source range is invalid")
        if capture_scope not in {"private", "public"}:
            raise ValueError("memory buffer capture_scope is invalid")
        timestamp = _time_text(now or datetime.now(UTC))
        with self._database.connect() as connection:
            _validate_capture_source(
                connection,
                owner_id=owner_id,
                session_id=session_id,
                turn_id=turn_id,
                first_message_id=first_message_id,
                last_message_id=last_message_id,
            )
            connection.execute(
                """
                INSERT INTO memory_buffers (
                    owner_id, session_id, turn_id, first_message_id, last_message_id,
                    capture_scope, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(turn_id) DO NOTHING
                """,
                (
                    owner_id,
                    session_id,
                    turn_id,
                    first_message_id,
                    last_message_id,
                    capture_scope,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_buffers WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        assert row is not None
        result = _buffer(row)
        if (
            result.owner_id,
            result.session_id,
            result.first_message_id,
            result.last_message_id,
            result.capture_scope,
        ) != (
            owner_id,
            session_id,
            first_message_id,
            last_message_id,
            capture_scope,
        ):
            raise MemoryBufferStateError("memory buffer idempotency key changed")
        return result

    def capture_completed_turn(
        self,
        *,
        owner_id: int,
        session_id: int,
        turn_id: int,
        capture_scope: str,
        now: datetime | None = None,
    ) -> MemoryBuffer:
        """从已完成 Turn 解析完整 Message range 并创建 durable receipt。"""
        for name, value in (
            ("owner_id", owner_id),
            ("session_id", session_id),
            ("turn_id", turn_id),
        ):
            _positive(value, name)
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT MIN(messages.id), MAX(messages.id)
                FROM turns
                JOIN sessions ON sessions.id = turns.session_id
                JOIN messages ON messages.turn_id = turns.id
                WHERE turns.id = ? AND turns.session_id = ? AND sessions.user_id = ?
                    AND turns.status = 'completed'
                """,
                (turn_id, session_id, owner_id),
            ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            raise MemoryBufferStateError("completed memory Turn has no source range")
        return self.capture(
            owner_id=owner_id,
            session_id=session_id,
            turn_id=turn_id,
            first_message_id=int(row[0]),
            last_message_id=int(row[1]),
            capture_scope=capture_scope,
            now=now,
        )

    def pending_count(self, owner_id: int) -> int:
        """返回 Owner 当前未绑定 Run 的 buffer 数量。"""
        _positive(owner_id, "owner_id")
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM memory_buffers
                WHERE owner_id = ? AND status = 'pending'
                """,
                (owner_id,),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def list_pending(self, owner_id: int, *, limit: int = 5) -> tuple[MemoryBuffer, ...]:
        """按 ID 顺序读取有界 pending buffer，供 FlushCoordinator 组批。"""
        _positive(owner_id, "owner_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("memory buffer limit must be between 1 and 100")
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_buffers
                WHERE owner_id = ? AND status = 'pending'
                ORDER BY id LIMIT ?
                """,
                (owner_id, limit),
            ).fetchall()
        return tuple(_buffer(row) for row in rows)

    def pending_owner_ids(self) -> tuple[int, ...]:
        """返回至少有一个 pending buffer 的有序 Owner ID。"""
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT owner_id FROM memory_buffers
                WHERE status = 'pending' ORDER BY owner_id
                """
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def assign(self, owner_id: int, buffer_ids: tuple[int, ...], run_id: int) -> None:
        """在一个 IMMEDIATE 事务中把完整 buffer 集合绑定给同 Owner Run。"""
        _positive(owner_id, "owner_id")
        _positive(run_id, "run_id")
        if not buffer_ids or any(type(value) is not int or value <= 0 for value in buffer_ids):
            raise ValueError("buffer_ids must contain positive integers")
        if len(set(buffer_ids)) != len(buffer_ids):
            raise ValueError("buffer_ids must be unique")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT first_message_id, last_message_id FROM memory_flush_runs
                WHERE id = ? AND owner_id = ?
                """,
                (run_id, owner_id),
            ).fetchone()
            if run is None:
                raise MemoryBufferStateError("memory flush run does not belong to owner")
            for buffer_id in buffer_ids:
                updated = connection.execute(
                    """
                    UPDATE memory_buffers SET status = 'assigned', flush_run_id = ?
                    WHERE id = ? AND owner_id = ? AND status = 'pending'
                        AND first_message_id >= ? AND last_message_id <= ?
                    """,
                    (
                        run_id,
                        buffer_id,
                        owner_id,
                        int(run["first_message_id"]),
                        int(run["last_message_id"]),
                    ),
                ).rowcount
                if updated != 1:
                    raise MemoryBufferStateError("memory buffer is not assignable")

    def mark_flushed(self, run_id: int, now: datetime) -> int:
        """把 Run 绑定的 assigned buffers 一次性结算为 flushed。"""
        _positive(run_id, "run_id")
        timestamp = _time_text(now)
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE memory_buffers SET status = 'flushed', flushed_at = ?
                WHERE flush_run_id = ? AND status = 'assigned'
                """,
                (timestamp, run_id),
            ).rowcount
            if updated <= 0:
                raise MemoryBufferStateError("memory flush run has no assigned buffers")
        return updated

    def get(self, buffer_id: int) -> MemoryBuffer:
        """按内部 ID 读取 buffer，缺失时稳定失败。"""
        _positive(buffer_id, "buffer_id")
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM memory_buffers WHERE id = ?",
                (buffer_id,),
            ).fetchone()
        if row is None:
            raise MemoryBufferStateError("memory buffer does not exist")
        return _buffer(row)


def _validate_capture_source(
    connection: sqlite3.Connection,
    *,
    owner_id: int,
    session_id: int,
    turn_id: int,
    first_message_id: int,
    last_message_id: int,
) -> None:
    """验证 Turn 和 range endpoints 全部属于给定 Owner Session。"""
    turn = connection.execute(
        """
        SELECT 1 FROM turns
        JOIN sessions ON sessions.id = turns.session_id
        WHERE turns.id = ? AND turns.session_id = ? AND sessions.user_id = ?
        """,
        (turn_id, session_id, owner_id),
    ).fetchone()
    endpoints = connection.execute(
        """
        SELECT id FROM messages
        WHERE session_id = ? AND id IN (?, ?)
        """,
        (session_id, first_message_id, last_message_id),
    ).fetchall()
    if turn is None or {int(row[0]) for row in endpoints} != {
        first_message_id,
        last_message_id,
    }:
        raise MemoryBufferStateError("memory buffer source does not belong to owner turn")


def _buffer(row: sqlite3.Row) -> MemoryBuffer:
    """把 SQLite Row 严格恢复为 MemoryBuffer。"""
    return MemoryBuffer(
        id=int(row["id"]),
        owner_id=int(row["owner_id"]),
        session_id=int(row["session_id"]),
        turn_id=int(row["turn_id"]),
        first_message_id=int(row["first_message_id"]),
        last_message_id=int(row["last_message_id"]),
        capture_scope=str(row["capture_scope"]),
        status=str(row["status"]),
        flush_run_id=(None if row["flush_run_id"] is None else int(row["flush_run_id"])),
        created_at=_parse_time(row["created_at"]),
        flushed_at=(None if row["flushed_at"] is None else _parse_time(row["flushed_at"])),
    )


def _positive(value: int, field: str) -> None:
    """验证内部 ID 是非 bool 正整数。"""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")


def _time_text(value: datetime) -> str:
    """把带时区时间统一编码为 UTC ISO 文本。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("memory buffer timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_time(value: object) -> datetime:
    """严格解析持久化的带时区 ISO 时间。"""
    if not isinstance(value, str):
        raise MemoryBufferStateError("memory buffer timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MemoryBufferStateError("memory buffer timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise MemoryBufferStateError("memory buffer timestamp is invalid")
    return parsed.astimezone(UTC)
