"""ToolRun 状态与 Audit Event 的原子 SQLite 写入。"""

import hashlib
import json
from datetime import UTC, datetime

from miniclaw.policy.engine import PolicyDecision
from miniclaw.providers.base import JsonValue, ToolCall
from miniclaw.storage.database import Database
from miniclaw.tools.base import ToolContext


class ToolStateError(RuntimeError):
    """表示 ToolRun 不满足预期的状态迁移。"""


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
    return json.dumps(
        arguments,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _arguments_hash(tool_name: str, arguments_json: str) -> str:
    """返回绑定 Tool 名与规范参数的稳定 SHA-256。"""
    return hashlib.sha256(f"{tool_name}\n{arguments_json}".encode()).hexdigest()
