"""Phase 6 飞书 Automation Live 的只读 durable evaluator。"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.evals.cases import EvalCase
from miniclaw.evals.production_evidence import ProductionEvidenceError, validate_commit
from miniclaw.storage.database import Database, DatabaseError

_CASE_STATUSES = frozenset({"pass", "fail", "skip"})
_EVIDENCE_KEYS = frozenset(
    {
        "approval_id_bound",
        "budget_stopped",
        "continuation_terminal",
        "delivery_once",
        "gateway_restart_recovered",
        "idempotency_key_reused",
        "lease_released",
        "no_side_effect",
        "one_slot_only",
        "original_budget_preserved",
        "provider_request_observed",
        "stale_run_interrupted",
        "structured_silence",
        "task_identity_preserved",
        "two_slots_once",
        "zero_claim",
    }
)
_EXPECTED_FIXTURES = frozenset(
    {
        "live_approval_continuation",
        "live_budget_stop",
        "live_delivery_unknown_recovery",
        "live_durable_estop",
        "live_gateway_restart",
        "live_interrupted_recovery",
        "live_interval_two_slots",
        "live_one_shot_delivery",
        "live_structured_silence",
        "live_waiting_approval",
    }
)


@dataclass(frozen=True, slots=True)
class PendingAutomationRun:
    """保存 checkpoint 时一个 waiting Run 的绑定事实与 snapshot hash。"""

    run_id: int
    approval_id: int
    turn_id: int
    snapshot_hash: str


@dataclass(frozen=True, slots=True)
class AutomationLiveCheckpoint:
    """保存人工动作前相关事实表的高水位和目标 Task。"""

    task_ids: tuple[int, ...]
    task_id: int
    run_id: int
    turn_id: int
    tool_run_id: int
    approval_id: int
    delivery_id: int
    control_revision: int
    control_halted: bool
    captured_at: str
    pending_runs: tuple[PendingAutomationRun, ...]


@dataclass(frozen=True, slots=True)
class AutomationLiveCaseResult:
    """保存单个 Live case 的封闭结论，不包含数据库行或外部 ID。"""

    case_id: str
    status: str
    evidence_passed: tuple[str, ...]
    evidence_failed: tuple[str, ...]
    human_status: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _Facts:
    """保存 evaluator 内存中的相关 SQLite 行。"""

    runs: tuple[sqlite3.Row, ...]
    turns: tuple[sqlite3.Row, ...]
    tools: tuple[sqlite3.Row, ...]
    approvals: tuple[sqlite3.Row, ...]
    deliveries: tuple[sqlite3.Row, ...]
    new_task_count: int
    control_revision: int
    control_halted: bool


def capture_automation_checkpoint(
    database: Path,
    *,
    task_ids: Sequence[int],
    now: datetime | None = None,
) -> AutomationLiveCheckpoint:
    """只读捕获目标 Task 与事实表高水位。

    Args:
        database: 已初始化的 MiniClaw SQLite 文件。
        task_ids: 本 case 允许关联的内部 Task ID。
        now: 可注入的 UTC checkpoint 时间。

    Returns:
        不含业务正文的不可变 checkpoint。

    Raises:
        ValueError: Task ID 或时钟无效。
        DatabaseError: 数据库不可读或 schema 不完整。
    """
    targets = tuple(sorted(set(task_ids)))
    if not targets or any(type(task_id) is not int or task_id <= 0 for task_id in targets):
        raise ValueError("automation_live_task_ids_invalid")
    captured = datetime.now(UTC) if now is None else now
    if not isinstance(captured, datetime) or captured.tzinfo is None:
        raise ValueError("automation_live_clock_invalid")
    captured_at = captured.astimezone(UTC).isoformat()
    with Database(database).connect_read_only() as connection:
        pending_rows = connection.execute(
            f"""
            SELECT id, approval_id, turn_id, snapshot_json
            FROM task_runs
            WHERE task_id IN ({','.join('?' for _ in targets)})
              AND status = 'waiting_approval'
              AND approval_id IS NOT NULL AND turn_id IS NOT NULL
            ORDER BY id
            """,
            targets,
        ).fetchall()
        control = connection.execute(
            "SELECT halted, revision FROM automation_control WHERE singleton = 1"
        ).fetchone()
        if control is None:
            raise DatabaseError("automation control is unavailable")
        return AutomationLiveCheckpoint(
            task_ids=targets,
            task_id=_maximum(connection, "scheduled_tasks"),
            run_id=_maximum(connection, "task_runs"),
            turn_id=_maximum(connection, "turns"),
            tool_run_id=_maximum(connection, "tool_runs"),
            approval_id=_maximum(connection, "approvals"),
            delivery_id=_maximum(connection, "deliveries"),
            control_revision=int(control["revision"]),
            control_halted=bool(control["halted"]),
            captured_at=captured_at,
            pending_runs=tuple(
                PendingAutomationRun(
                    run_id=int(row["id"]),
                    approval_id=int(row["approval_id"]),
                    turn_id=int(row["turn_id"]),
                    snapshot_hash=hashlib.sha256(
                        str(row["snapshot_json"]).encode("utf-8")
                    ).hexdigest(),
                )
                for row in pending_rows
            ),
        )


def evaluate_automation_case(
    database: Path,
    checkpoint: AutomationLiveCheckpoint,
    case: EvalCase,
) -> AutomationLiveCaseResult:
    """只读评价一个版本化 Automation Live case。

    Args:
        database: MiniClaw SQLite 文件。
        checkpoint: 人工动作前捕获的高水位。
        case: 固定 `FEISHU-AUTO-001..010` 场景。

    Returns:
        只含封闭 evidence key 和稳定错误码的结果。
    """
    requirements = tuple(case.expected.automation_evidence)
    if not _valid_input(checkpoint, case, requirements):
        return _failed_result(case.id, requirements, "automation_case_invalid")
    try:
        with Database(database).connect_read_only() as connection:
            facts = _load_facts(connection, checkpoint)
    except (DatabaseError, OSError, sqlite3.Error, TypeError, ValueError):
        return _failed_result(case.id, requirements, "automation_evidence_unavailable")

    if _clock_rolled_back(facts.runs, checkpoint):
        return _failed_result(case.id, requirements, "clock_rollback")
    if _has_pending_leak(facts, case.automation_fixture or ""):
        return _failed_result(case.id, requirements, "pending_approval_leak")

    checks = _evidence_checks(facts, checkpoint, case)
    passed = tuple(key for key in requirements if checks.get(key, False))
    failed = tuple(key for key in requirements if key not in passed)
    common = _common_expectations(facts, case)
    if failed or not common:
        return AutomationLiveCaseResult(
            case.id,
            "fail",
            passed,
            failed,
            None,
            "automation_evidence_failed",
        )
    return AutomationLiveCaseResult(case.id, "pass", passed, (), None, None)


def build_automation_evidence_report(
    *,
    commit: str,
    started_at: str,
    finished_at: str,
    results: Sequence[AutomationLiveCaseResult],
    secret_matches: int,
) -> dict[str, object]:
    """构造十条 Automation Live 的封闭生产 Evidence 报告。

    Args:
        commit: clean repository commit。
        started_at: UTC 起始时间。
        finished_at: UTC 结束时间。
        results: 单 case 结论。
        secret_matches: exact Secret scan 匿名计数。

    Returns:
        可交给 private writer 的标准 JSON object。

    Raises:
        ValueError: report 字段、结果或时间不符合闭合契约。
    """
    try:
        normalized_commit = validate_commit(commit)
    except ProductionEvidenceError:
        raise ValueError("invalid_automation_evidence_report") from None
    if not _is_timestamp(started_at) or not _is_timestamp(finished_at):
        raise ValueError("invalid_automation_evidence_report")
    if type(secret_matches) is not int or secret_matches < 0:
        raise ValueError("invalid_automation_evidence_report")
    normalized = tuple(_validate_result(result) for result in results)
    if len({result.case_id for result in normalized}) != len(normalized):
        raise ValueError("invalid_automation_evidence_report")
    checks = [
        {
            "case_id": result.case_id,
            "status": result.status,
            "evidence": [
                {"key": key, "status": status}
                for status, keys in (
                    ("pass", result.evidence_passed),
                    ("fail", result.evidence_failed),
                )
                for key in keys
            ],
            "human_status": result.human_status,
            "error_code": result.error_code,
        }
        for result in normalized
    ]
    counts = {
        "cases_total": len(normalized),
        "cases_passed": sum(result.status == "pass" for result in normalized),
        "cases_failed": sum(result.status == "fail" for result in normalized),
        "cases_skipped": sum(result.status == "skip" for result in normalized),
        "secret_matches": secret_matches,
    }
    expected = {f"FEISHU-AUTO-{index:03d}" for index in range(1, 11)}
    verified = (
        secret_matches == 0
        and {result.case_id for result in normalized} == expected
        and all(result.status == "pass" for result in normalized)
        and all(result.human_status in {None, "pass"} for result in normalized)
    )
    return {
        "schema_version": 1,
        "suite": "feishu-automation",
        "commit": normalized_commit,
        "started_at": started_at,
        "finished_at": finished_at,
        "checks": checks,
        "counts": counts,
        "secret_matches": secret_matches,
        "release_status": (
            "FEISHU_AUTOMATION_VERIFIED" if verified else "FEISHU_AUTOMATION_FAILED"
        ),
    }


def _valid_input(
    checkpoint: AutomationLiveCheckpoint,
    case: EvalCase,
    requirements: tuple[str, ...],
) -> bool:
    """验证 evaluator 输入只使用固定 case、fixture 与 evidence。"""
    return (
        isinstance(checkpoint, AutomationLiveCheckpoint)
        and re.fullmatch(r"FEISHU-AUTO-(?:00[1-9]|010)", case.id) is not None
        and case.capability == "feishu_automation_e2e"
        and case.automation_fixture in _EXPECTED_FIXTURES
        and bool(requirements)
        and len(requirements) == len(set(requirements))
        and all(key in _EVIDENCE_KEYS for key in requirements)
    )


def _load_facts(
    connection: sqlite3.Connection,
    checkpoint: AutomationLiveCheckpoint,
) -> _Facts:
    """读取 checkpoint 后或 continuation 绑定的最小相关行。"""
    targets = checkpoint.task_ids
    pending_ids = tuple(item.run_id for item in checkpoint.pending_runs)
    parameters: tuple[object, ...] = (*targets, checkpoint.run_id, *pending_ids)
    pending_clause = ""
    if pending_ids:
        pending_clause = f" OR id IN ({','.join('?' for _ in pending_ids)})"
    runs = tuple(
        connection.execute(
            f"""
            SELECT id, task_id, turn_id, approval_id, scheduled_for, idempotency_key,
                   snapshot_json, status, worker_id, lease_expires_at, completed_at,
                   response_json, error_code, created_at
            FROM task_runs
            WHERE task_id IN ({','.join('?' for _ in targets)})
              AND (id > ?{pending_clause})
            ORDER BY id
            """,
            parameters,
        ).fetchall()
    )
    turn_ids = tuple(
        sorted(
            {
                *(
                    int(row["turn_id"])
                    for row in runs
                    if row["turn_id"] is not None
                ),
                *(item.turn_id for item in checkpoint.pending_runs),
            }
        )
    )
    turns = _select_by_ids(
        connection,
        "turns",
        "id, parent_turn_id, runtime_snapshot_json, status, started_at, completed_at",
        turn_ids,
    )
    tools = _select_by_foreign_ids(
        connection,
        "tool_runs",
        "turn_id",
        "id, turn_id, tool_name, status, created_at, completed_at",
        turn_ids,
    )
    approval_ids = tuple(
        sorted(
            {
                *(item.approval_id for item in checkpoint.pending_runs),
                *(int(row["approval_id"]) for row in runs if row["approval_id"] is not None),
            }
        )
    )
    approvals = _select_by_ids(
        connection,
        "approvals",
        "id, turn_id, tool_run_id, status, created_at, decided_at",
        approval_ids,
    )
    run_ids = tuple(int(row["id"]) for row in runs)
    deliveries = _select_by_foreign_ids(
        connection,
        "deliveries",
        "task_run_id",
        "id, task_run_id, channel, part_index, delivery_kind, idempotency_key, "
        "status, attempts, created_at, sent_at",
        run_ids,
        minimum_id=checkpoint.delivery_id,
    )
    control = connection.execute(
        "SELECT halted, revision FROM automation_control WHERE singleton = 1"
    ).fetchone()
    if control is None:
        raise ValueError("automation control missing")
    new_task_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM scheduled_tasks WHERE id > ?",
            (checkpoint.task_id,),
        ).fetchone()[0]
    )
    return _Facts(
        runs=runs,
        turns=turns,
        tools=tools,
        approvals=approvals,
        deliveries=deliveries,
        new_task_count=new_task_count,
        control_revision=int(control["revision"]),
        control_halted=bool(control["halted"]),
    )


def _evidence_checks(
    facts: _Facts,
    checkpoint: AutomationLiveCheckpoint,
    case: EvalCase,
) -> dict[str, bool]:
    """从最小 facts 派生固定 evidence key，不接受调用方表达式。"""
    runs = facts.runs
    deliveries = facts.deliveries
    sent_once = (
        len(deliveries) == (case.expected.delivery_count or 0)
        and all(row["channel"] == "feishu" and row["status"] == "sent" for row in deliveries)
        and len({row["idempotency_key"] for row in deliveries}) == len(deliveries)
    )
    provider_observed = any(_provider_observed(row) for row in facts.turns)
    pending = checkpoint.pending_runs
    continuation = False
    budget_preserved = False
    if len(pending) == 1 and len(runs) == 1:
        bound = pending[0]
        run = runs[0]
        approval = next(
            (row for row in facts.approvals if int(row["id"]) == bound.approval_id),
            None,
        )
        turn = next(
            (row for row in facts.turns if row["parent_turn_id"] == bound.turn_id),
            None,
        )
        tool_names = {(row["tool_name"], row["status"]) for row in facts.tools}
        continuation = (
            int(run["id"]) == bound.run_id
            and run["status"] == "succeeded"
            and approval is not None
            and approval["status"] == "consumed"
            and turn is not None
            and ("write_file", "succeeded") in tool_names
            and ("complete_task", "succeeded") in tool_names
        )
        budget_preserved = (
            hashlib.sha256(str(run["snapshot_json"]).encode("utf-8")).hexdigest()
            == bound.snapshot_hash
        )
    waiting_bound = _waiting_is_bound(facts)
    structured_silence = len(runs) == 1 and _is_structured_silence(runs[0])
    budget_stopped = (
        len(runs) == 1
        and runs[0]["status"] == "failed"
        and isinstance(runs[0]["error_code"], str)
        and str(runs[0]["error_code"]).startswith("task_budget_")
        and len(facts.tools) <= 1
        and not deliveries
    )
    no_side_effect = (
        not any(row["status"] == "succeeded" for row in facts.tools)
        if case.automation_fixture == "live_waiting_approval"
        else budget_stopped
    )
    return {
        "one_slot_only": len(runs) == 1 and len({runs[0]["idempotency_key"]}) == 1,
        "delivery_once": sent_once,
        "provider_request_observed": provider_observed,
        "two_slots_once": (
            len(runs) == 2
            and len({row["task_id"] for row in runs}) == 1
            and len({row["scheduled_for"] for row in runs}) == 2
            and len({row["idempotency_key"] for row in runs}) == 2
            and all(row["status"] == "succeeded" for row in runs)
            and sent_once
        ),
        "task_identity_preserved": (
            bool(runs)
            and all(int(row["task_id"]) in checkpoint.task_ids for row in runs)
            and facts.new_task_count == 0
        ),
        "gateway_restart_recovered": len(runs) == 1 and runs[0]["status"] == "succeeded",
        "stale_run_interrupted": len(runs) == 1 and runs[0]["status"] == "interrupted",
        "lease_released": bool(runs)
        and all(row["worker_id"] is None and row["lease_expires_at"] is None for row in runs),
        "approval_id_bound": waiting_bound,
        "continuation_terminal": continuation,
        "original_budget_preserved": budget_preserved,
        "structured_silence": structured_silence and not deliveries,
        "zero_claim": (
            facts.control_halted
            and facts.control_revision > checkpoint.control_revision
            and not runs
            and not deliveries
        ),
        "budget_stopped": budget_stopped,
        "no_side_effect": no_side_effect,
        "idempotency_key_reused": (
            len(deliveries) == 1
            and deliveries[0]["status"] == "sent"
            and int(deliveries[0]["attempts"]) >= 2
        ),
    }


def _common_expectations(facts: _Facts, case: EvalCase) -> bool:
    """验证版本化 status 与 Delivery 数量的公共约束。"""
    expected = case.expected.automation_status
    if expected == "halted":
        status_matches = facts.control_halted and not facts.runs
    elif expected == "failed":
        status_matches = bool(facts.runs) and all(
            row["status"] in {"failed", "interrupted", "timed_out", "cancelled"}
            for row in facts.runs
        )
    else:
        status_matches = bool(facts.runs) and all(row["status"] == expected for row in facts.runs)
    delivery_count = case.expected.delivery_count
    return status_matches and (
        delivery_count is None or len(facts.deliveries) == delivery_count
    )


def _waiting_is_bound(facts: _Facts) -> bool:
    """判断 waiting Run、Approval 与 ToolRun 是否同一条绑定链。"""
    if len(facts.runs) != 1 or len(facts.approvals) != 1:
        return False
    run = facts.runs[0]
    approval = facts.approvals[0]
    tool = next(
        (row for row in facts.tools if int(row["id"]) == int(approval["tool_run_id"])),
        None,
    )
    return (
        run["status"] == "waiting_approval"
        and run["worker_id"] is None
        and run["lease_expires_at"] is None
        and run["approval_id"] == approval["id"]
        and approval["status"] == "pending"
        and tool is not None
        and tool["status"] == "waiting_approval"
    )


def _has_pending_leak(facts: _Facts, fixture: str) -> bool:
    """除 waiting case 外拒绝相关 Approval 保持 pending。"""
    return fixture != "live_waiting_approval" and any(
        row["status"] == "pending" for row in facts.approvals
    )


def _provider_observed(turn: sqlite3.Row) -> bool:
    """只判断 Provider request existence bit，不返回 ID。"""
    try:
        value = json.loads(str(turn["runtime_snapshot_json"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(value, Mapping) and isinstance(value.get("provider_request_id"), str)


def _is_structured_silence(run: sqlite3.Row) -> bool:
    """判断 terminal response 是否为 notify=false + empty text。"""
    try:
        value = json.loads(str(run["response_json"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return value == {"notify": False, "text": ""}


def _clock_rolled_back(
    runs: Sequence[sqlite3.Row],
    checkpoint: AutomationLiveCheckpoint,
) -> bool:
    """拒绝 checkpoint 后新建但时间早于 checkpoint 的 Run。"""
    captured = _parse_timestamp(checkpoint.captured_at)
    for row in runs:
        if int(row["id"]) <= checkpoint.run_id:
            continue
        created = _parse_timestamp(str(row["created_at"]))
        if created is None or captured is None or created < captured:
            return True
    return False


def _select_by_ids(
    connection: sqlite3.Connection,
    table: str,
    columns: str,
    ids: tuple[int, ...],
) -> tuple[sqlite3.Row, ...]:
    """从固定表按内部 ID 读取稳定顺序行。"""
    if table not in {"turns", "approvals"} or not ids:
        return ()
    return tuple(
        connection.execute(
            f"SELECT {columns} FROM {table} WHERE id IN "
            f"({','.join('?' for _ in ids)}) ORDER BY id",
            ids,
        ).fetchall()
    )


def _select_by_foreign_ids(
    connection: sqlite3.Connection,
    table: str,
    foreign_key: str,
    columns: str,
    ids: tuple[int, ...],
    *,
    minimum_id: int | None = None,
) -> tuple[sqlite3.Row, ...]:
    """从固定 Tool/Delivery 表按内部外键读取相关行。"""
    allowed = {("tool_runs", "turn_id"), ("deliveries", "task_run_id")}
    if (table, foreign_key) not in allowed or not ids:
        return ()
    suffix = "" if minimum_id is None else " AND id > ?"
    parameters: tuple[object, ...] = ids
    if minimum_id is not None:
        parameters = (*parameters, minimum_id)
    return tuple(
        connection.execute(
            f"SELECT {columns} FROM {table} WHERE {foreign_key} IN "
            f"({','.join('?' for _ in ids)}){suffix} ORDER BY id",
            parameters,
        ).fetchall()
    )


def _maximum(connection: sqlite3.Connection, table: str) -> int:
    """读取固定事实表的最大内部 ID。"""
    if table not in {
        "scheduled_tasks",
        "task_runs",
        "turns",
        "tool_runs",
        "approvals",
        "deliveries",
    }:
        raise ValueError("unsupported automation checkpoint table")
    return int(connection.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])


def _failed_result(
    case_id: str,
    requirements: tuple[str, ...],
    error_code: str,
) -> AutomationLiveCaseResult:
    """构造不回显数据库事实的稳定失败结果。"""
    return AutomationLiveCaseResult(case_id, "fail", (), requirements, None, error_code)


def _validate_result(result: AutomationLiveCaseResult) -> AutomationLiveCaseResult:
    """验证 report 输入使用封闭 ID、状态和 evidence。"""
    if (
        not isinstance(result, AutomationLiveCaseResult)
        or re.fullmatch(r"FEISHU-AUTO-(?:00[1-9]|010)", result.case_id) is None
        or result.status not in _CASE_STATUSES
        or result.human_status not in {None, "pass", "fail", "skip"}
        or any(
            key not in _EVIDENCE_KEYS
            for key in (*result.evidence_passed, *result.evidence_failed)
        )
        or len({*result.evidence_passed, *result.evidence_failed})
        != len(result.evidence_passed) + len(result.evidence_failed)
        or (
            result.error_code is not None
            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", result.error_code) is None
        )
    ):
        raise ValueError("invalid_automation_evidence_report")
    if result.status == "pass" and (result.evidence_failed or result.error_code is not None):
        raise ValueError("invalid_automation_evidence_report")
    return result


def _is_timestamp(value: object) -> bool:
    """判断值是否为不含本地信息的 UTC ISO-8601 时间。"""
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    return _parse_timestamp(value) is not None


def _parse_timestamp(value: str) -> datetime | None:
    """解析 UTC/offset ISO-8601 时间并规范化为 UTC。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)
