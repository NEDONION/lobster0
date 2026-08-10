"""ToolRun、Approval 与 Audit Event 的原子 SQLite 写入。"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from lobster0.policy.approvals import (
    ApprovalDecision,
    ApprovalError,
    available_approval_decisions,
    canonical_arguments_hash,
    canonical_arguments_json,
)
from lobster0.policy.command import NormalizedCommand, command_rule_is_persistable
from lobster0.policy.engine import PolicyDecision
from lobster0.policy.modes import PermissionMode
from lobster0.policy.network import NetworkPolicyError, NetworkRule, normalize_network_rule
from lobster0.providers.base import JsonValue, ToolCall
from lobster0.sandbox.base import ExecutionPlan
from lobster0.sandbox.repository import ExecutionPlanRepository, insert_execution_plan
from lobster0.storage.database import Database
from lobster0.tools.base import ToolContext


class ToolStateError(RuntimeError):
    """表示 ToolRun 不满足预期的状态迁移。"""


class PermissionModeAuditRepository:
    """把进程级权限模式变化保存为不含平台身份的审计事件。"""

    def __init__(self, database: Database) -> None:
        """绑定已经完成 schema 初始化的 SQLite 数据库。"""
        self._database = database

    def record(
        self,
        user_id: int,
        previous: PermissionMode,
        current: PermissionMode,
        source: str,
    ) -> None:
        """先持久化安全枚举，再允许 PermissionState 完成模式切换。"""
        metadata = json.dumps(
            {
                "current_mode": current.value,
                "previous_mode": previous.value,
                "source": source,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_type, user_id, summary, metadata_json, created_at
                ) VALUES ('policy.mode_changed', ?, 'Changed permission mode', ?, ?)
                """,
                (user_id, metadata, datetime.now(UTC).isoformat()),
            )


@dataclass(frozen=True, slots=True)
class StoredApproval:
    """表示一条可跨进程查询的参数绑定 Approval。"""

    id: int
    user_id: int
    turn_id: int
    tool_run_id: int
    tool_name: str
    arguments_hash: str
    execution_plan_hash: str | None
    summary: str
    status: str
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalPresentation:
    """保存 Channel 可展示的脱敏 Approval 与 Core 允许模式。"""

    approval: StoredApproval
    grant_modes: tuple[ApprovalDecision, ...]


@dataclass(frozen=True, slots=True)
class StoredToolRun:
    """表示 Approval 消费后可交给 Executor 的唯一绑定 ToolRun。"""

    id: int
    turn_id: int
    tool_call_id: str
    tool_name: str
    arguments: dict[str, JsonValue]
    arguments_hash: str
    execution_plan_hash: str | None
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
        execution_plan: ExecutionPlan | None = None,
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
            if execution_plan is not None:
                insert_execution_plan(connection, run_id, execution_plan)
            approval_cursor = connection.execute(
                """
                INSERT INTO approvals (
                    user_id, turn_id, tool_run_id, tool_name, arguments_hash,
                    summary, status, expires_at, created_at, execution_plan_hash
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
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
                    execution_plan.sha256 if execution_plan is not None else None,
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
        self._expire_due(user_id)
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
        self._expire_due(user_id, approval_id)
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

    def presentation(self, user_id: int, approval_id: int) -> ApprovalPresentation:
        """校验参数绑定并返回不含完整 arguments 的 Channel 展示字段。"""
        approval = self.get(user_id, approval_id)
        if approval.status == "expired":
            raise ApprovalError("expired", "approval has expired")
        if approval.status not in {"pending", "approved"}:
            raise ApprovalError("already_decided", "approval is not pending")
        with self._database.connect_read_only() as connection:
            row = _approval_join_row(connection, approval_id)
        failure = _approval_access_error(row, user_id)
        if failure is not None:
            raise failure
        arguments = _decode_arguments(row["arguments_json"])
        expected_hash = canonical_arguments_hash(row["tool_name"], arguments)
        if (
            expected_hash != row["arguments_hash"]
            or expected_hash != row["tool_run_arguments_hash"]
        ):
            raise ApprovalError("hash_mismatch", "approval arguments no longer match")
        plan_failure = _execution_plan_access_error(row)
        if plan_failure is not None:
            raise plan_failure
        return ApprovalPresentation(
            approval=approval,
            grant_modes=available_approval_decisions(row["tool_name"], arguments),
        )

    def _expire_due(self, user_id: int, approval_id: int | None = None) -> None:
        """在查询前只结算当前 Owner 到期记录，不消费或执行任何 Tool。"""
        now = self._now()
        query = """
            SELECT a.*, tr.tool_call_id, tr.arguments_json,
                   tr.arguments_hash AS tool_run_arguments_hash,
                   tr.status AS tool_run_status, t.session_id
            FROM approvals a
            JOIN tool_runs tr ON tr.id = a.tool_run_id
            JOIN turns t ON t.id = a.turn_id
            WHERE a.user_id = ? AND a.status IN ('pending', 'approved')
              AND a.expires_at <= ?
        """
        parameters: tuple[object, ...] = (user_id, now.isoformat())
        if approval_id is not None:
            query += " AND a.id = ?"
            parameters += (approval_id,)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(query, parameters).fetchall()
            for row in rows:
                _expire_approval(connection, row, now)

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

    def validate_decision(
        self,
        user_id: int,
        approval_id: int,
        decision: ApprovalDecision,
    ) -> None:
        """在任何批准或执行副作用前校验参数绑定的授权作用域。"""
        self._expire_due(user_id, approval_id)
        with self._database.connect_read_only() as connection:
            row = _approval_join_row(connection, approval_id)
        failure = _approval_access_error(row, user_id)
        if failure is not None:
            raise failure
        if row["status"] not in {"pending", "approved"}:
            raise ApprovalError("already_decided", "approval is not pending")
        arguments = _decode_arguments(row["arguments_json"])
        expected_hash = canonical_arguments_hash(row["tool_name"], arguments)
        if (
            expected_hash != row["arguments_hash"]
            or expected_hash != row["tool_run_arguments_hash"]
        ):
            raise ApprovalError("hash_mismatch", "approval arguments no longer match")
        plan_failure = _execution_plan_access_error(row)
        if plan_failure is not None:
            raise plan_failure
        if decision not in available_approval_decisions(row["tool_name"], arguments):
            raise ApprovalError("scope_forbidden", "approval scope is not allowed")

    def deny(self, user_id: int, approval_id: int) -> StoredToolRun:
        """原子拒绝 pending Approval，并终止绑定 ToolRun。"""
        now = self._now()
        failure: ApprovalError | None = None
        stored: StoredToolRun | None = None
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _approval_join_row(connection, approval_id)
            failure = _approval_access_error(row, user_id)
            if failure is None and row["status"] != "pending":
                failure = ApprovalError("already_decided", "approval is not pending")
            if failure is None and _parse_time(row["expires_at"]) <= now:
                _expire_approval(connection, row, now)
                failure = ApprovalError("expired", "approval has expired")
            if failure is None:
                approval_update = connection.execute(
                    """
                    UPDATE approvals SET status = 'denied', decided_at = ?
                    WHERE id = ? AND user_id = ? AND status = 'pending' AND expires_at > ?
                    """,
                    (now.isoformat(), approval_id, user_id, now.isoformat()),
                )
                run_update = connection.execute(
                    """
                    UPDATE tool_runs SET status = 'denied', completed_at = ?
                    WHERE id = ? AND status = 'waiting_approval'
                    """,
                    (now.isoformat(), row["tool_run_id"]),
                )
                if approval_update.rowcount != 1 or run_update.rowcount != 1:
                    raise ToolStateError("Approval or ToolRun cannot be denied")
                _insert_approval_audit(
                    connection,
                    "approval.denied",
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
                    arguments={},
                    arguments_hash=row["arguments_hash"],
                    execution_plan_hash=row["execution_plan_hash"],
                    status="denied",
                )
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
                    failure = _execution_plan_access_error(row)
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
                    execution_plan_hash=row["execution_plan_hash"],
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


class PolicyRuleRepository:
    """保存并读取由 Approval 产生的窄 Policy allow 规则。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    def command_rules(self, user_id: int) -> tuple[NormalizedCommand, ...]:
        """读取当前 Owner 的 enabled exact-argv 规则；损坏数据失败关闭。"""
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT rule_json FROM policy_rules
                WHERE user_id = ? AND tool_name = 'run_command' AND enabled = 1
                ORDER BY id
                """,
                (user_id,),
            ).fetchall()
        return tuple(_decode_command_rule(row["rule_json"]) for row in rows)

    def network_rules(self, user_id: int) -> tuple[NetworkRule, ...]:
        """读取当前 Owner 的 enabled exact-hostname 规则；损坏数据失败关闭。"""
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT rule_json FROM policy_rules
                WHERE user_id = ? AND tool_name = 'http_get' AND enabled = 1
                ORDER BY id
                """,
                (user_id,),
            ).fetchall()
        return tuple(_decode_network_rule(row["rule_json"]) for row in rows)

    def add_command_from_approval(self, user_id: int, approval_id: int) -> int:
        """从已成功消费的 run_command Approval 幂等创建 exact-argv 规则。"""
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _approval_join_row(connection, approval_id)
            failure = _approval_access_error(row, user_id)
            if failure is not None:
                raise failure
            if (
                row["status"] != "consumed"
                or row["tool_name"] != "run_command"
                or row["tool_run_status"] != "succeeded"
            ):
                raise ApprovalError(
                    "already_decided",
                    "approval did not complete a command successfully",
                )
            arguments = _decode_arguments(row["arguments_json"])
            expected_hash = canonical_arguments_hash(row["tool_name"], arguments)
            if (
                expected_hash != row["arguments_hash"]
                or expected_hash != row["tool_run_arguments_hash"]
            ):
                raise ApprovalError("hash_mismatch", "approval arguments no longer match")
            command = _command_from_arguments(arguments)
            if not command_rule_is_persistable(command):
                raise ApprovalError(
                    "scope_forbidden",
                    "approval command cannot become a persistent rule",
                )
            rule_json = json.dumps(
                {
                    "type": "exact_argv",
                    "resolved_program": command.resolved_program,
                    "args": list(command.args),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            existing = connection.execute(
                """
                SELECT id FROM policy_rules
                WHERE user_id = ? AND tool_name = 'run_command'
                  AND rule_json = ? AND enabled = 1
                """,
                (user_id, rule_json),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO policy_rules (
                    user_id, tool_name, rule_json, source_approval_id, created_at
                ) VALUES (?, 'run_command', ?, ?, ?)
                """,
                (user_id, rule_json, approval_id, now),
            )
            rule_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_type, user_id, turn_id, summary, metadata_json, created_at
                ) VALUES ('policy_rule.created', ?, ?, 'Created exact command rule', ?, ?)
                """,
                (
                    user_id,
                    row["turn_id"],
                    json.dumps(
                        {
                            "approval_id": approval_id,
                            "policy_rule_id": rule_id,
                            "tool_name": "run_command",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        return rule_id

    def add_network_from_approval(self, user_id: int, approval_id: int) -> int:
        """从成功 http_get Approval 幂等创建 exact hostname + port 规则。"""
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _approval_join_row(connection, approval_id)
            failure = _approval_access_error(row, user_id)
            if failure is not None:
                raise failure
            if (
                row["status"] != "consumed"
                or row["tool_name"] != "http_get"
                or row["tool_run_status"] != "succeeded"
            ):
                raise ApprovalError(
                    "already_decided",
                    "approval did not complete an HTTPS request successfully",
                )
            arguments = _decode_arguments(row["arguments_json"])
            expected_hash = canonical_arguments_hash(row["tool_name"], arguments)
            if (
                expected_hash != row["arguments_hash"]
                or expected_hash != row["tool_run_arguments_hash"]
            ):
                raise ApprovalError("hash_mismatch", "approval arguments no longer match")
            rule = _network_from_arguments(arguments)
            rule_json = json.dumps(
                {
                    "type": "exact_hostname",
                    "hostname": rule.hostname,
                    "port": rule.port,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            existing = connection.execute(
                """
                SELECT id FROM policy_rules
                WHERE user_id = ? AND tool_name = 'http_get'
                  AND rule_json = ? AND enabled = 1
                """,
                (user_id, rule_json),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO policy_rules (
                    user_id, tool_name, rule_json, source_approval_id, created_at
                ) VALUES (?, 'http_get', ?, ?, ?)
                """,
                (user_id, rule_json, approval_id, now),
            )
            rule_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_type, user_id, turn_id, summary, metadata_json, created_at
                ) VALUES ('policy_rule.created', ?, ?, 'Created exact hostname rule', ?, ?)
                """,
                (
                    user_id,
                    row["turn_id"],
                    json.dumps(
                        {
                            "approval_id": approval_id,
                            "policy_rule_id": rule_id,
                            "tool_name": "http_get",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        return rule_id


class ToolRunRepository:
    """保存 running → terminal ToolRun 及其最小审计摘要。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def execution_plans(self) -> ExecutionPlanRepository:
        """返回与 ToolRun 共用数据库的 plan repository。"""
        return ExecutionPlanRepository(self._database)

    def start(
        self,
        context: ToolContext,
        call: ToolCall,
        arguments: dict[str, JsonValue],
        decision: PolicyDecision,
        execution_plan: ExecutionPlan | None = None,
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
            if execution_plan is not None:
                insert_execution_plan(connection, run_id, execution_plan)
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

    def interrupt_stale_runs(
        self,
        *,
        stale_before: datetime | None = None,
    ) -> tuple[int, ...]:
        """把旧 running 记录终止并审计；永远不重放原始参数。"""
        now = datetime.now(UTC)
        cutoff = stale_before or (now - timedelta(minutes=5))
        if cutoff.tzinfo is None:
            raise ValueError("stale_before must be timezone-aware")
        cutoff = cutoff.astimezone(UTC)
        recovered: list[int] = []
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT tr.id, tr.tool_name, tr.arguments_hash, tr.turn_id,
                       t.session_id, s.user_id
                FROM tool_runs tr
                JOIN turns t ON t.id = tr.turn_id
                JOIN sessions s ON s.id = t.session_id
                WHERE tr.status = 'running' AND tr.created_at < ?
                ORDER BY tr.id
                """,
                (cutoff.isoformat(),),
            ).fetchall()
            for row in rows:
                updated = connection.execute(
                    """
                    UPDATE tool_runs SET status = 'interrupted', completed_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now.isoformat(), row["id"]),
                )
                if updated.rowcount != 1:
                    continue
                recovered.append(int(row["id"]))
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        event_type, user_id, session_id, turn_id,
                        summary, metadata_json, created_at
                    ) VALUES ('tool.interrupted', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["user_id"],
                        row["session_id"],
                        row["turn_id"],
                        f"Interrupted stale {row['tool_name']}",
                        _metadata(
                            row["id"],
                            row["tool_name"],
                            row["arguments_hash"],
                            error_code="stale_recovery",
                        ),
                        now.isoformat(),
                    ),
                )
        return tuple(recovered)

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
               tr.status AS tool_run_status, t.session_id,
               ep.plan_hash AS stored_execution_plan_hash
        FROM approvals a
        JOIN tool_runs tr ON tr.id = a.tool_run_id
        JOIN turns t ON t.id = a.turn_id
        LEFT JOIN execution_plans ep ON ep.tool_run_id = tr.id
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


def _execution_plan_access_error(row: sqlite3.Row) -> ApprovalError | None:
    """校验 Approval 复制的 plan hash 与 immutable plan row 一致。"""
    approval_hash = row["execution_plan_hash"]
    stored_hash = row["stored_execution_plan_hash"]
    if approval_hash is None and stored_hash is None:
        return None
    if approval_hash != stored_hash:
        return ApprovalError("hash_mismatch", "approval execution plan no longer matches")
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


def _command_from_arguments(arguments: dict[str, JsonValue]) -> NormalizedCommand:
    """从已 hash 绑定的规范参数恢复 exact-argv；不重新搜索 PATH。"""
    program = arguments.get("program")
    args = arguments.get("args")
    if (
        set(arguments) != {"program", "args", "timeout_seconds"}
        or not isinstance(program, str)
        or not Path(program).is_absolute()
        or not isinstance(args, list)
        or any(not isinstance(argument, str) for argument in args)
    ):
        raise ApprovalError("hash_mismatch", "approval command arguments are invalid")
    return NormalizedCommand(program, tuple(args))


def _network_from_arguments(arguments: dict[str, JsonValue]) -> NetworkRule:
    """从已规范化且 hash 绑定的 URL 提取精确 authority。"""
    url = arguments.get("url")
    timeout = arguments.get("timeout_seconds")
    if (
        set(arguments) != {"url", "timeout_seconds"}
        or not isinstance(url, str)
        or type(timeout) is not int
    ):
        raise ApprovalError("hash_mismatch", "approval network arguments are invalid")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port or 443
    except ValueError:
        raise ApprovalError("hash_mismatch", "approval network arguments are invalid") from None
    if parsed.scheme != "https" or hostname is None:
        raise ApprovalError("hash_mismatch", "approval network arguments are invalid")
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    value = host_text if port == 443 else f"{host_text}:{port}"
    try:
        return normalize_network_rule(value)
    except NetworkPolicyError:
        raise ApprovalError("hash_mismatch", "approval network arguments are invalid") from None


def _decode_command_rule(value: str) -> NormalizedCommand:
    """严格恢复持久 exact-argv 规则；未知类型或字段失败关闭。"""
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        raise ToolStateError("stored command policy rule is invalid") from None
    if not isinstance(decoded, dict) or set(decoded) != {
        "type",
        "resolved_program",
        "args",
    }:
        raise ToolStateError("stored command policy rule is invalid")
    program = decoded["resolved_program"]
    args = decoded["args"]
    if (
        decoded["type"] != "exact_argv"
        or not isinstance(program, str)
        or not Path(program).is_absolute()
        or not isinstance(args, list)
        or any(not isinstance(argument, str) for argument in args)
    ):
        raise ToolStateError("stored command policy rule is invalid")
    return NormalizedCommand(program, tuple(args))


def _decode_network_rule(value: str) -> NetworkRule:
    """严格恢复持久 exact hostname 规则；未知字段失败关闭。"""
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        raise ToolStateError("stored network policy rule is invalid") from None
    if not isinstance(decoded, dict) or set(decoded) != {"type", "hostname", "port"}:
        raise ToolStateError("stored network policy rule is invalid")
    hostname = decoded["hostname"]
    port = decoded["port"]
    if (
        decoded["type"] != "exact_hostname"
        or not isinstance(hostname, str)
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise ToolStateError("stored network policy rule is invalid")
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    value = host_text if port == 443 else f"{host_text}:{port}"
    try:
        rule = normalize_network_rule(value)
    except NetworkPolicyError:
        raise ToolStateError("stored network policy rule is invalid") from None
    if rule != NetworkRule(hostname, port):
        raise ToolStateError("stored network policy rule is invalid")
    return rule


def _approval_from_row(row: sqlite3.Row) -> StoredApproval:
    """把 SQLite Row 转为不可变 Approval。"""
    return StoredApproval(
        id=row["id"],
        user_id=row["user_id"],
        turn_id=row["turn_id"],
        tool_run_id=row["tool_run_id"],
        tool_name=row["tool_name"],
        arguments_hash=row["arguments_hash"],
        execution_plan_hash=row["execution_plan_hash"],
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
