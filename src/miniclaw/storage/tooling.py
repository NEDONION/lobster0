"""ToolRun、Approval 与 Audit Event 的原子 SQLite 写入。"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from miniclaw.policy.approvals import (
    ApprovalError,
    canonical_arguments_hash,
    canonical_arguments_json,
)
from miniclaw.policy.engine import PolicyDecision
from miniclaw.providers.base import JsonValue, ToolCall
from miniclaw.storage.database import Database
from miniclaw.tools.base import ToolContext


class ToolStateError(RuntimeError):
    """表示 ToolRun 不满足预期的状态迁移。"""


@dataclass(frozen=True, slots=True)
class StoredApproval:
    """表示一条可跨进程查询的参数绑定 Approval。"""

    id: int
    user_id: int
    turn_id: int
    tool_run_id: int
    tool_name: str
    arguments_hash: str
    summary: str
    status: str
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredToolRun:
    """表示 Approval 消费后可交给 Executor 的唯一绑定 ToolRun。"""

    id: int
    turn_id: int
    tool_call_id: str
    tool_name: str
    arguments: dict[str, JsonValue]
    arguments_hash: str
    status: str


class ApprovalRepository:
    """以 SQLite 条件更新保存 pending → approved → consumed 生命周期。"""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    def create_waiting(
        self,
        context: ToolContext,
        call: ToolCall,
        arguments: dict[str, JsonValue],
        decision: PolicyDecision,
        *,
        ttl_seconds: int,
        summary: str,
    ) -> StoredApproval:
        """原子创建 waiting ToolRun、pending Approval 和脱敏审计。"""
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("approval ttl_seconds must be a positive integer")
        if not summary.strip():
            raise ValueError("approval summary must not be empty")
        arguments_json = canonical_arguments_json(arguments)
        arguments_hash = canonical_arguments_hash(call.name, arguments)
        now = self._now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._database.connect() as connection:
            run_cursor = connection.execute(
                """
                INSERT INTO tool_runs (
                    turn_id, tool_call_id, tool_name, arguments_json,
                    arguments_hash, policy_action, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'waiting_approval', ?)
                """,
                (
                    context.turn_id,
                    call.call_id,
                    call.name,
                    arguments_json,
                    arguments_hash,
                    decision.action.value,
                    now.isoformat(),
                ),
            )
            run_id = int(run_cursor.lastrowid)
            approval_cursor = connection.execute(
                """
                INSERT INTO approvals (
                    user_id, turn_id, tool_run_id, tool_name, arguments_hash,
                    summary, status, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    context.user_id,
                    context.turn_id,
                    run_id,
                    call.name,
                    arguments_hash,
                    summary.strip(),
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )
            approval_id = int(approval_cursor.lastrowid)
            _insert_approval_audit(
                connection,
                "approval.created",
                context.user_id,
                context.session_id,
                context.turn_id,
                approval_id,
                run_id,
                call.name,
                arguments_hash,
                now,
            )
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        return _approval_from_row(row)

    def list(self, user_id: int, *, status: str | None = None) -> tuple[StoredApproval, ...]:
        """按 ID 返回当前 Owner 的 Approval，可选稳定状态过滤。"""
        if status is not None and status not in {
            "pending",
            "approved",
            "denied",
            "expired",
            "consumed",
        }:
            raise ValueError("invalid approval status")
        query = "SELECT * FROM approvals WHERE user_id = ?"
        parameters: tuple[object, ...] = (user_id,)
        if status is not None:
            query += " AND status = ?"
            parameters += (status,)
        query += " ORDER BY id"
        with self._database.connect_read_only() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_approval_from_row(row) for row in rows)

    def get(self, user_id: int, approval_id: int) -> StoredApproval:
        """读取一条 Approval，并区分不存在与 Owner 不匹配。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            raise ApprovalError("not_found", "approval was not found")
        if row["user_id"] != user_id:
            raise ApprovalError("not_owner", "approval belongs to a different owner")
        return _approval_from_row(row)

    def approve(self, user_id: int, approval_id: int) -> StoredApproval:
        """把未过期 pending Approval 原子改为 approved。"""
        now = self._now()
        failure: ApprovalError | None = None
        stored: StoredApproval | None = None
        with self._database.connect() as connection:
            row = _approval_join_row(connection, approval_id)
            failure = _approval_access_error(row, user_id)
            if failure is None and row["status"] != "pending":
                failure = ApprovalError("already_decided", "approval is not pending")
            if failure is None and _parse_time(row["expires_at"]) <= now:
                _expire_approval(connection, row, now)
                failure = ApprovalError("expired", "approval has expired")
            if failure is None:
                updated = connection.execute(
                    """
                    UPDATE approvals SET status = 'approved', decided_at = ?
                    WHERE id = ? AND user_id = ? AND status = 'pending' AND expires_at > ?
                    """,
                    (now.isoformat(), approval_id, user_id, now.isoformat()),
                )
                if updated.rowcount != 1:
                    raise ToolStateError("Approval is not pending")
                _insert_approval_audit(
                    connection,
                    "approval.approved",
                    row["user_id"],
                    row["session_id"],
                    row["turn_id"],
                    row["id"],
                    row["tool_run_id"],
                    row["tool_name"],
                    row["arguments_hash"],
                    now,
                )
                stored_row = connection.execute(
                    "SELECT * FROM approvals WHERE id = ?",
                    (approval_id,),
                ).fetchone()
                stored = _approval_from_row(stored_row)
        if failure is not None:
            raise failure
        assert stored is not None
        return stored

    def consume(self, user_id: int, approval_id: int) -> StoredToolRun:
        """原子 claim 已批准参数；成功后同一 Approval 永远不能再执行。"""
        now = self._now()
        failure: ApprovalError | None = None
        stored: StoredToolRun | None = None
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _approval_join_row(connection, approval_id)
            failure = _approval_access_error(row, user_id)
            if failure is None and row["status"] != "approved":
                failure = ApprovalError("already_decided", "approval is not approved")
            if failure is None and _parse_time(row["expires_at"]) <= now:
                _expire_approval(connection, row, now)
                failure = ApprovalError("expired", "approval has expired")
            if failure is None:
                arguments = _decode_arguments(row["arguments_json"])
                expected_hash = canonical_arguments_hash(row["tool_name"], arguments)
                if (
                    expected_hash != row["arguments_hash"]
                    or expected_hash != row["tool_run_arguments_hash"]
                ):
                    failure = ApprovalError(
                        "hash_mismatch",
                        "approval arguments no longer match",
                    )
            if failure is None:
                approval_update = connection.execute(
                    """
                    UPDATE approvals SET status = 'consumed', decided_at = ?
                    WHERE id = ? AND user_id = ? AND status = 'approved' AND expires_at > ?
                    """,
                    (now.isoformat(), approval_id, user_id, now.isoformat()),
                )
                run_update = connection.execute(
                    """
                    UPDATE tool_runs SET status = 'running'
                    WHERE id = ? AND status = 'waiting_approval'
                    """,
                    (row["tool_run_id"],),
                )
                if approval_update.rowcount != 1 or run_update.rowcount != 1:
                    raise ToolStateError("Approval or ToolRun cannot be consumed")
                _insert_approval_audit(
                    connection,
                    "approval.consumed",
                    row["user_id"],
                    row["session_id"],
                    row["turn_id"],
                    row["id"],
                    row["tool_run_id"],
                    row["tool_name"],
                    row["arguments_hash"],
                    now,
                )
                stored = StoredToolRun(
                    id=row["tool_run_id"],
                    turn_id=row["turn_id"],
                    tool_call_id=row["tool_call_id"],
                    tool_name=row["tool_name"],
                    arguments=arguments,
                    arguments_hash=expected_hash,
                    status="running",
                )
        if failure is not None:
            raise failure
        assert stored is not None
        return stored

    def _now(self) -> datetime:
        """读取 timezone-aware UTC 时钟，拒绝模糊的本地时间。"""
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("approval clock must be timezone-aware")
        return value.astimezone(UTC)


class ToolRunRepository:
    """保存 running → terminal ToolRun 及其最小审计摘要。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    def start(
        self,
        context: ToolContext,
        call: ToolCall,
        arguments: dict[str, JsonValue],
        decision: PolicyDecision,
    ) -> int:
        """在一个事务中创建 running ToolRun 与 started 审计事件。"""
        arguments_json = _arguments_json(arguments)
        arguments_hash = _arguments_hash(call.name, arguments_json)
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tool_runs (
                    turn_id, tool_call_id, tool_name, arguments_json,
                    arguments_hash, policy_action, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    context.turn_id,
                    call.call_id,
                    call.name,
                    arguments_json,
                    arguments_hash,
                    decision.action.value,
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_type, user_id, session_id, turn_id,
                    summary, metadata_json, created_at
                ) VALUES ('tool.started', ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.user_id,
                    context.session_id,
                    context.turn_id,
                    f"Started {call.name}",
                    _metadata(run_id, call.name, arguments_hash),
                    now,
                ),
            )
        return run_id

    def deny(
        self,
        context: ToolContext,
        call: ToolCall,
        arguments: dict[str, JsonValue],
        error_code: str,
    ) -> None:
        """只写入脱敏拒绝审计，不创建 ToolRun 或 started 事件。"""
        arguments_hash = _arguments_hash(call.name, _arguments_json(arguments))
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_type, user_id, session_id, turn_id,
                    summary, metadata_json, created_at
                ) VALUES ('tool.denied', ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.user_id,
                    context.session_id,
                    context.turn_id,
                    f"Denied {call.name}",
                    _metadata(None, call.name, arguments_hash, error_code=error_code),
                    now,
                ),
            )

    def succeed(self, run_id: int, result_preview: str, duration_ms: int) -> None:
        """把唯一 running ToolRun 原子转为 succeeded 并写审计。"""
        self._finish(run_id, "succeeded", result_preview, duration_ms)

    def fail(
        self,
        run_id: int,
        result_preview: str,
        duration_ms: int,
        error_code: str | None,
    ) -> None:
        """把唯一 running ToolRun 原子转为 failed 并写安全错误码。"""
        self._finish(run_id, "failed", result_preview, duration_ms, error_code)

    def interrupt(self, run_id: int, duration_ms: int) -> None:
        """把被取消的 running ToolRun 原子转为 interrupted。"""
        self._finish(run_id, "interrupted", None, duration_ms)

    def _finish(
        self,
        run_id: int,
        status: str,
        result_preview: str | None,
        duration_ms: int,
        error_code: str | None = None,
    ) -> None:
        """实现三个终态共用的受限状态迁移和审计事务。"""
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE tool_runs SET
                    status = ?, result_preview = ?,
                    duration_ms = ?, completed_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    result_preview[:2000] if result_preview is not None else None,
                    duration_ms,
                    now,
                    run_id,
                ),
            )
            if updated.rowcount != 1:
                raise ToolStateError("ToolRun is not running")
            row = connection.execute(
                """
                SELECT tr.tool_name, tr.arguments_hash, tr.turn_id,
                       t.session_id, s.user_id
                FROM tool_runs tr
                JOIN turns t ON t.id = tr.turn_id
                JOIN sessions s ON s.id = t.session_id
                WHERE tr.id = ?
                """,
                (run_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_type, user_id, session_id, turn_id,
                    summary, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"tool.{status}",
                    row["user_id"],
                    row["session_id"],
                    row["turn_id"],
                    f"{status.capitalize()} {row['tool_name']}",
                    _metadata(
                        run_id,
                        row["tool_name"],
                        row["arguments_hash"],
                        error_code=error_code,
                    ),
                    now,
                ),
            )


def _metadata(
    run_id: int | None,
    tool_name: str,
    arguments_hash: str,
    *,
    error_code: str | None = None,
) -> str:
    """生成不含原始参数的稳定 Audit metadata。"""
    metadata = {"tool_name": tool_name, "arguments_hash": arguments_hash[:12]}
    if run_id is not None:
        metadata["tool_run_id"] = run_id
    if error_code is not None:
        metadata["error_code"] = error_code
    return json.dumps(
        metadata,
        separators=(",", ":"),
        sort_keys=True,
    )


def _arguments_json(arguments: dict[str, JsonValue]) -> str:
    """把已规范化参数编码为稳定 JSON，供执行记录和 hash 共用。"""
    return canonical_arguments_json(arguments)


def _arguments_hash(tool_name: str, arguments_json: str) -> str:
    """返回绑定 Tool 名与规范参数的稳定 SHA-256。"""
    return canonical_arguments_hash(tool_name, _decode_arguments(arguments_json))


def _approval_join_row(
    connection: sqlite3.Connection,
    approval_id: int,
) -> sqlite3.Row | None:
    """读取 Approval 与绑定 ToolRun/Session 的单行状态。"""
    return connection.execute(
        """
        SELECT a.*, tr.tool_call_id, tr.arguments_json,
               tr.arguments_hash AS tool_run_arguments_hash,
               tr.status AS tool_run_status, t.session_id
        FROM approvals a
        JOIN tool_runs tr ON tr.id = a.tool_run_id
        JOIN turns t ON t.id = a.turn_id
        WHERE a.id = ?
        """,
        (approval_id,),
    ).fetchone()


def _approval_access_error(row: sqlite3.Row | None, user_id: int) -> ApprovalError | None:
    """返回 not-found/not-owner 错误，供事务退出后安全抛出。"""
    if row is None:
        return ApprovalError("not_found", "approval was not found")
    if row["user_id"] != user_id:
        return ApprovalError("not_owner", "approval belongs to a different owner")
    return None


def _expire_approval(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
) -> None:
    """在同一事务终止过期 Approval 和尚未运行的 ToolRun。"""
    connection.execute(
        """
        UPDATE approvals SET status = 'expired', decided_at = ?
        WHERE id = ? AND status IN ('pending', 'approved')
        """,
        (now.isoformat(), row["id"]),
    )
    connection.execute(
        """
        UPDATE tool_runs SET status = 'denied', completed_at = ?
        WHERE id = ? AND status = 'waiting_approval'
        """,
        (now.isoformat(), row["tool_run_id"]),
    )
    _insert_approval_audit(
        connection,
        "approval.expired",
        row["user_id"],
        row["session_id"],
        row["turn_id"],
        row["id"],
        row["tool_run_id"],
        row["tool_name"],
        row["arguments_hash"],
        now,
    )


def _insert_approval_audit(
    connection: sqlite3.Connection,
    event_type: str,
    user_id: int,
    session_id: int,
    turn_id: int,
    approval_id: int,
    run_id: int,
    tool_name: str,
    arguments_hash: str,
    now: datetime,
) -> None:
    """插入不含原始参数、文件内容或绝对路径的 Approval 审计。"""
    metadata = json.dumps(
        {
            "approval_id": approval_id,
            "tool_run_id": run_id,
            "tool_name": tool_name,
            "arguments_hash": arguments_hash[:12],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO audit_events (
            event_type, user_id, session_id, turn_id,
            summary, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            user_id,
            session_id,
            turn_id,
            f"Approval {event_type.removeprefix('approval.')} for {tool_name}",
            metadata,
            now.isoformat(),
        ),
    )


def _decode_arguments(value: str) -> dict[str, JsonValue]:
    """严格恢复标准 JSON object；损坏记录按 hash mismatch 失败关闭。"""
    try:
        decoded = json.loads(
            value,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        raise ApprovalError("hash_mismatch", "approval arguments are invalid") from None
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise ApprovalError("hash_mismatch", "approval arguments are invalid")
    return cast(dict[str, JsonValue], decoded)


def _approval_from_row(row: sqlite3.Row) -> StoredApproval:
    """把 SQLite Row 转为不可变 Approval。"""
    return StoredApproval(
        id=row["id"],
        user_id=row["user_id"],
        turn_id=row["turn_id"],
        tool_run_id=row["tool_run_id"],
        tool_name=row["tool_name"],
        arguments_hash=row["arguments_hash"],
        summary=row["summary"],
        status=row["status"],
        expires_at=_parse_time(row["expires_at"]),
        decided_at=(
            _parse_time(row["decided_at"])
            if row["decided_at"] is not None
            else None
        ),
        created_at=_parse_time(row["created_at"]),
    )


def _reject_json_constant(value: str) -> JsonValue:
    """让 `json.loads` 拒绝 Python 默认接受的 NaN/Infinity。"""
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_time(value: str) -> datetime:
    """解析 Schema 保存的 timezone-aware ISO 时间。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ToolStateError("stored timestamp is not timezone-aware")
    return parsed.astimezone(UTC)
