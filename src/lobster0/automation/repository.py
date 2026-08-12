"""Automation Task/Run/E-stop 的 SQLite 事务仓储。"""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from lobster0.automation.models import (
    DeliveryTarget,
    RunStatus,
    ScheduledTask,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskResponse,
    TaskRun,
    TaskRunSnapshot,
    TaskStatus,
)
from lobster0.storage.database import Database


class AutomationStateError(RuntimeError):
    """表示 Automation 状态机或乐观并发条件不成立。"""


class AutomationDataError(RuntimeError):
    """表示持久化 Automation JSON 或枚举已损坏。"""


@dataclass(frozen=True, slots=True)
class AutomationControl:
    """保存 durable E-stop 与 Scheduler heartbeat 的公开状态。"""

    halted: bool
    reason: str | None
    revision: int
    scheduler_heartbeat_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """汇总一次 stale lease 恢复实际处理的 Run 数。"""

    requeued: int
    interrupted: int


def _utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(UTC)


class ScheduledTaskRepository:
    """管理 owner-scoped ScheduledTask 和 optimistic transitions。"""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存数据库与可注入 UTC 时钟。"""
        self._database = database
        self._clock = clock or _utc_now

    def create(
        self,
        *,
        owner_id: int,
        name: str,
        schedule: ScheduleSpec,
        prompt: str,
        skill_names: tuple[str, ...],
        delivery: DeliveryTarget,
        policy_profile: str,
        budget: TaskBudget,
        system_key: str | None = None,
    ) -> ScheduledTask:
        """原子创建一条 active Task，并返回数据库分配的 ID。"""
        now = _as_utc(self._clock(), "task create time")
        _validate_task_input(owner_id, name, prompt, policy_profile)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO scheduled_tasks (
                    owner_id, name, schedule_kind, schedule_expression, timezone,
                    prompt, skill_names_json, delivery_json, policy_profile,
                    budget_json, system_key, status, next_run_at, last_run_at,
                    version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, 1, ?, ?)
                """,
                (
                    owner_id,
                    name.strip(),
                    schedule.kind.value,
                    schedule.expression,
                    schedule.timezone,
                    prompt,
                    _json_dumps(list(skill_names)),
                    _delivery_json(delivery),
                    policy_profile.strip(),
                    _budget_json(budget),
                    system_key,
                    _datetime_text(schedule.next_run_at),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            task_id = cast(int, cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(row)

    def get(self, task_id: int, *, owner_id: int | None = None) -> ScheduledTask:
        """按内部 ID 读取 Task，并可额外约束 Owner。"""
        query = "SELECT * FROM scheduled_tasks WHERE id = ?"
        parameters: tuple[object, ...] = (task_id,)
        if owner_id is not None:
            query += " AND owner_id = ?"
            parameters = (task_id, owner_id)
        with self._database.connect_read_only() as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise AutomationStateError("task_not_found")
        return _task_from_row(row)

    def get_by_system_key(
        self,
        owner_id: int,
        system_key: str,
    ) -> ScheduledTask | None:
        """按 Owner 与稳定 system key 读取系统 Task。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE owner_id = ? AND system_key = ?
                """,
                (owner_id, system_key),
            ).fetchone()
        return None if row is None else _task_from_row(row)

    def count_system_owned(self, kind: str) -> int:
        """统计指定 system key 前缀的 Task，供 Doctor 与 reconcile 验证。"""
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("system task kind must be non-empty")
        with self._database.connect_read_only() as connection:
            value = connection.execute(
                """
                SELECT COUNT(*) FROM scheduled_tasks
                WHERE system_key LIKE ?
                """,
                (f"system:{kind.strip()}:%",),
            ).fetchone()[0]
        return int(value)

    def reconcile_system(
        self,
        task_id: int,
        *,
        owner_id: int,
        schedule: ScheduleSpec,
        prompt: str,
        delivery: DeliveryTarget,
        budget: TaskBudget,
        status: TaskStatus = TaskStatus.ACTIVE,
    ) -> ScheduledTask:
        """仅供受管配置更新 system Task，并保持 identity/history 不变。"""
        if status not in {TaskStatus.ACTIVE, TaskStatus.PAUSED}:
            raise ValueError("system task reconcile status is invalid")
        now = _as_utc(self._clock(), "system task reconcile time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE id = ? AND owner_id = ? AND system_key IS NOT NULL
                """,
                (task_id, owner_id),
            ).fetchone()
            if row is None:
                raise AutomationStateError("system_task_not_found")
            updated = connection.execute(
                """
                UPDATE scheduled_tasks
                SET schedule_kind = ?, schedule_expression = ?, timezone = ?,
                    prompt = ?, delivery_json = ?, budget_json = ?, status = ?,
                    next_run_at = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND owner_id = ? AND version = ?
                  AND system_key IS NOT NULL
                """,
                (
                    schedule.kind.value,
                    schedule.expression,
                    schedule.timezone,
                    prompt,
                    _delivery_json(delivery),
                    _budget_json(budget),
                    status.value,
                    _datetime_text(schedule.next_run_at),
                    now.isoformat(),
                    task_id,
                    owner_id,
                    row["version"],
                ),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_version_conflict")
            result = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(result)

    def list(
        self,
        *,
        owner_id: int,
        statuses: Sequence[TaskStatus] | None = None,
        limit: int = 100,
    ) -> tuple[ScheduledTask, ...]:
        """按 ID 稳定列出 Owner 的有限 Task。"""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("task list limit must be between 1 and 1000")
        parameters: list[object] = [owner_id]
        query = "SELECT * FROM scheduled_tasks WHERE owner_id = ?"
        if statuses:
            values = tuple(status.value for status in statuses)
            query += f" AND status IN ({','.join('?' for _ in values)})"
            parameters.extend(values)
        query += " ORDER BY id LIMIT ?"
        parameters.append(limit)
        with self._database.connect_read_only() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def list_due(self, *, now: datetime, limit: int) -> tuple[ScheduledTask, ...]:
        """列出 next_run_at 已到期的 active Task。"""
        current = _as_utc(now, "task due time")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("task due limit must be between 1 and 1000")
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at, id LIMIT ?
                """,
                (current.isoformat(), limit),
            ).fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def next_due_at(self) -> datetime | None:
        """返回所有 active Task 中最早的持久化 next_run_at。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT next_run_at FROM scheduled_tasks
                WHERE status = 'active' AND next_run_at IS NOT NULL
                ORDER BY next_run_at, id LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return _parse_required_datetime(row["next_run_at"])

    def pause(
        self, task_id: int, *, owner_id: int, expected_version: int
    ) -> ScheduledTask:
        """把 active Task 变为 paused。"""
        return self._transition(
            task_id,
            owner_id=owner_id,
            expected_version=expected_version,
            source=TaskStatus.ACTIVE,
            target=TaskStatus.PAUSED,
        )

    def update(
        self,
        task_id: int,
        *,
        owner_id: int,
        expected_version: int,
        name: str | None = None,
        schedule: ScheduleSpec | None = None,
        prompt: str | None = None,
        skill_names: tuple[str, ...] | None = None,
        delivery: DeliveryTarget | None = None,
        policy_profile: str | None = None,
        budget: TaskBudget | None = None,
    ) -> ScheduledTask:
        """用 optimistic version 原子更新 active/paused Task 的显式字段。"""
        if all(
            value is None
            for value in (
                name,
                schedule,
                prompt,
                skill_names,
                delivery,
                policy_profile,
                budget,
            )
        ):
            raise ValueError("task update requires at least one field")
        now = _as_utc(self._clock(), "task update time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ? AND owner_id = ?",
                (task_id, owner_id),
            ).fetchone()
            if row is None:
                raise AutomationStateError("task_not_found")
            _check_task_row(row, expected_version)
            current = _task_from_row(row)
            if current.system_key is not None:
                raise AutomationStateError("system_task_immutable")
            if current.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
                raise AutomationStateError("task_terminal")
            selected_name = current.name if name is None else name
            selected_schedule = current.schedule if schedule is None else schedule
            selected_prompt = current.prompt if prompt is None else prompt
            selected_skills = current.skill_names if skill_names is None else skill_names
            selected_delivery = current.delivery if delivery is None else delivery
            selected_profile = (
                current.policy_profile if policy_profile is None else policy_profile
            )
            selected_budget = current.budget if budget is None else budget
            _validate_task_input(
                owner_id,
                selected_name,
                selected_prompt,
                selected_profile,
            )
            updated = connection.execute(
                """
                UPDATE scheduled_tasks
                SET name = ?, schedule_kind = ?, schedule_expression = ?, timezone = ?,
                    prompt = ?, skill_names_json = ?, delivery_json = ?,
                    policy_profile = ?, budget_json = ?, next_run_at = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND owner_id = ? AND version = ?
                  AND status IN ('active', 'paused')
                """,
                (
                    selected_name.strip(),
                    selected_schedule.kind.value,
                    selected_schedule.expression,
                    selected_schedule.timezone,
                    selected_prompt,
                    _json_dumps(list(selected_skills)),
                    _delivery_json(selected_delivery),
                    selected_profile.strip(),
                    _budget_json(selected_budget),
                    _datetime_text(selected_schedule.next_run_at),
                    now.isoformat(),
                    task_id,
                    owner_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_version_conflict")
            result = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(result)

    def resume(
        self, task_id: int, *, owner_id: int, expected_version: int
    ) -> ScheduledTask:
        """把 paused Task 恢复为 active。"""
        return self._transition(
            task_id,
            owner_id=owner_id,
            expected_version=expected_version,
            source=TaskStatus.PAUSED,
            target=TaskStatus.ACTIVE,
        )

    def cancel(
        self, task_id: int, *, owner_id: int, expected_version: int
    ) -> ScheduledTask:
        """把 active/paused Task 永久变为 cancelled。"""
        now = _as_utc(self._clock(), "task cancel time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status, version, system_key FROM scheduled_tasks
                WHERE id = ? AND owner_id = ?
                """,
                (task_id, owner_id),
            ).fetchone()
            _check_task_row(row, expected_version)
            if row["system_key"] is not None:
                raise AutomationStateError("system_task_immutable")
            if row["status"] in {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value}:
                raise AutomationStateError("task_terminal")
            updated = connection.execute(
                """
                UPDATE scheduled_tasks
                SET status = 'cancelled', next_run_at = NULL,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND owner_id = ? AND version = ?
                  AND status IN ('active', 'paused')
                """,
                (now.isoformat(), task_id, owner_id, expected_version),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_state_conflict")
            result = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(result)

    def advance(
        self,
        task_id: int,
        *,
        expected_version: int,
        scheduled_for: datetime,
        next_run_at: datetime | None,
        terminal: bool = False,
    ) -> ScheduledTask:
        """在 enqueue 成功后推进下次 slot 或结束 one-shot Task。"""
        slot = _as_utc(scheduled_for, "scheduled_for")
        next_time = None if next_run_at is None else _as_utc(next_run_at, "next_run_at")
        now = _as_utc(self._clock(), "task advance time")
        status = TaskStatus.COMPLETED.value if terminal else TaskStatus.ACTIVE.value
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, next_run_at = ?, last_run_at = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND status = 'active'
                """,
                (
                    status,
                    _datetime_text(next_time),
                    slot.isoformat(),
                    now.isoformat(),
                    task_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_version_conflict")
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(row)

    def _transition(
        self,
        task_id: int,
        *,
        owner_id: int,
        expected_version: int,
        source: TaskStatus,
        target: TaskStatus,
    ) -> ScheduledTask:
        """执行一个单源状态的 optimistic transition。"""
        now = _as_utc(self._clock(), "task transition time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status, version, system_key FROM scheduled_tasks
                WHERE id = ? AND owner_id = ?
                """,
                (task_id, owner_id),
            ).fetchone()
            _check_task_row(row, expected_version)
            if row["system_key"] is not None:
                raise AutomationStateError("system_task_immutable")
            if row["status"] in {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value}:
                raise AutomationStateError("task_terminal")
            updated = connection.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND owner_id = ? AND version = ? AND status = ?
                """,
                (
                    target.value,
                    now.isoformat(),
                    task_id,
                    owner_id,
                    expected_version,
                    source.value,
                ),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_state_conflict")
            result = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(result)


class TaskRunRepository:
    """管理 TaskRun enqueue、claim、lease、终态与崩溃恢复。"""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存数据库和可注入 UTC 时钟。"""
        self._database = database
        self._clock = clock or _utc_now

    def enqueue(
        self,
        task: ScheduledTask,
        *,
        scheduled_for: datetime,
        idempotency_key: str | None = None,
    ) -> TaskRun:
        """在 E-stop 检查后幂等创建一条 queued Run。"""
        slot = _as_utc(scheduled_for, "scheduled_for")
        key = idempotency_key or task_run_idempotency_key(task.id, slot)
        snapshot = _task_snapshot_json(task)
        now = _as_utc(self._clock(), "task run create time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _raise_if_halted(connection)
            if task.status is not TaskStatus.ACTIVE:
                raise AutomationStateError("task_not_active")
            connection.execute(
                """
                INSERT INTO task_runs (
                    task_id, scheduled_for, idempotency_key, snapshot_json,
                    status, attempt, usage_json, created_at
                ) VALUES (?, ?, ?, ?, 'queued', 0, '{}', ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (task.id, slot.isoformat(), key, snapshot, now.isoformat()),
            )
            row = connection.execute(
                "SELECT * FROM task_runs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return _run_from_row(row)

    def enqueue_child(
        self,
        task: ScheduledTask,
        *,
        parent_run_id: int,
        subagent_id: str,
        scheduled_for: datetime,
        idempotency_key: str | None = None,
    ) -> TaskRun:
        """为一次 depth-1 派发创建子 Run。

        复用同一张表：子 Run 的生命周期、lease 与重启恢复与普通 Run 完全一致，
        复制一张表只会让恢复逻辑分叉。

        Raises:
            AutomationStateError: 父 Run 不存在，或它本身已经是子 Run
                （``subagent_depth_exceeded``）。工具层已经通过「子 Agent 拿不到
                delegate_task」保证了深度，这里是第二道——绕过工具层直接调
                仓库也不行。
        """
        slot = _as_utc(scheduled_for, "scheduled_for")
        key = idempotency_key or f"subagent:{parent_run_id}:{subagent_id}:{slot.isoformat()}"
        snapshot = _task_snapshot_json(task)
        now = _as_utc(self._clock(), "task run create time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _raise_if_halted(connection)
            parent = connection.execute(
                "SELECT parent_run_id FROM task_runs WHERE id = ?", (parent_run_id,)
            ).fetchone()
            if parent is None:
                raise AutomationStateError("task_run_not_found")
            if parent["parent_run_id"] is not None:
                raise AutomationStateError("subagent_depth_exceeded")
            connection.execute(
                """
                INSERT INTO task_runs (
                    task_id, scheduled_for, idempotency_key, snapshot_json,
                    status, attempt, usage_json, created_at,
                    parent_run_id, subagent_id
                ) VALUES (?, ?, ?, ?, 'queued', 0, '{}', ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    task.id,
                    slot.isoformat(),
                    key,
                    snapshot,
                    now.isoformat(),
                    parent_run_id,
                    subagent_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM task_runs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return _run_from_row(row)

    def list_children(self, parent_run_id: int, *, limit: int = 50) -> tuple[TaskRun, ...]:
        """列出一次派发下的全部子 Run，供界面展示参与的子任务。"""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise AutomationDataError("task run limit is invalid")
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT * FROM task_runs WHERE parent_run_id = ? ORDER BY id LIMIT ?",
                (parent_run_id, limit),
            ).fetchall()
        return tuple(_run_from_row(row) for row in rows)

    def enqueue_and_advance(
        self,
        task: ScheduledTask,
        *,
        scheduled_for: datetime,
        next_run_at: datetime | None,
        terminal: bool,
    ) -> tuple[TaskRun, ScheduledTask]:
        """在一个 transaction 中创建唯一 Run 并推进 Task。"""
        slot = _as_utc(scheduled_for, "scheduled_for")
        next_time = None if next_run_at is None else _as_utc(next_run_at, "next_run_at")
        key = task_run_idempotency_key(task.id, slot)
        now = _as_utc(self._clock(), "enqueue advance time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _raise_if_halted(connection)
            connection.execute(
                """
                INSERT INTO task_runs (
                    task_id, scheduled_for, idempotency_key, snapshot_json,
                    status, attempt, usage_json, created_at
                ) VALUES (?, ?, ?, ?, 'queued', 0, '{}', ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (task.id, slot.isoformat(), key, _task_snapshot_json(task), now.isoformat()),
            )
            updated = connection.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, next_run_at = ?, last_run_at = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND status = 'active'
                """,
                (
                    TaskStatus.COMPLETED.value if terminal else TaskStatus.ACTIVE.value,
                    _datetime_text(next_time),
                    slot.isoformat(),
                    now.isoformat(),
                    task.id,
                    task.version,
                ),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_version_conflict")
            run_row = connection.execute(
                "SELECT * FROM task_runs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            task_row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task.id,)
            ).fetchone()
        return _run_from_row(run_row), _task_from_row(task_row)

    def record_misfire_and_complete(
        self,
        task: ScheduledTask,
        *,
        scheduled_for: datetime,
        now: datetime,
    ) -> tuple[TaskRun, ScheduledTask]:
        """原子记录过期 once 的 failed Run，并把 Task 置为 completed。"""
        slot = _as_utc(scheduled_for, "scheduled_for")
        current = _as_utc(now, "misfire completion time")
        key = task_run_idempotency_key(task.id, slot)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _raise_if_halted(connection)
            connection.execute(
                """
                INSERT INTO task_runs (
                    task_id, scheduled_for, idempotency_key, snapshot_json,
                    status, attempt, completed_at, error_code, usage_json, created_at
                ) VALUES (?, ?, ?, ?, 'failed', 0, ?, 'schedule_misfire', '{}', ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    task.id,
                    slot.isoformat(),
                    key,
                    _task_snapshot_json(task),
                    current.isoformat(),
                    current.isoformat(),
                ),
            )
            updated = connection.execute(
                """
                UPDATE scheduled_tasks
                SET status = 'completed', next_run_at = NULL, last_run_at = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND status = 'active'
                """,
                (slot.isoformat(), current.isoformat(), task.id, task.version),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_version_conflict")
            run_row = connection.execute(
                "SELECT * FROM task_runs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            task_row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task.id,)
            ).fetchone()
        return _run_from_row(run_row), _task_from_row(task_row)

    def get(self, run_id: int) -> TaskRun:
        """按内部 ID 读取并严格解析一条 TaskRun。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise AutomationStateError("task_run_not_found")
        return _run_from_row(row)

    def list_succeeded(self) -> tuple[TaskRun, ...]:
        """按 ID 列出可幂等补投影的 succeeded Run。"""
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_runs
                WHERE status = 'succeeded' AND response_json IS NOT NULL
                ORDER BY id
                """
            ).fetchall()
        return tuple(_run_from_row(row) for row in rows)

    def count_active(self) -> int:
        """统计实际占用 Worker 并发的 claimed/running Run。"""
        with self._database.connect_read_only() as connection:
            value = connection.execute(
                """
                SELECT COUNT(*) FROM task_runs
                WHERE status IN ('claimed', 'running')
                """
            ).fetchone()[0]
        return int(value)

    def cancel_queued_for_task(self, task_id: int, *, now: datetime) -> int:
        """配置关闭时取消尚未 claim 的指定 system Task Run。"""
        current = _as_utc(now, "task queue cancel time")
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE task_runs
                SET status = 'cancelled', completed_at = ?,
                    error_code = 'task_disabled'
                WHERE task_id = ? AND status = 'queued'
                """,
                (current.isoformat(), task_id),
            )
        return int(updated.rowcount)

    def list(
        self,
        *,
        task_id: int | None = None,
        statuses: Sequence[RunStatus] | None = None,
        limit: int = 100,
    ) -> tuple[TaskRun, ...]:
        """按 ID 稳定列出有限 Run。"""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("task run list limit must be between 1 and 1000")
        query = "SELECT * FROM task_runs WHERE 1 = 1"
        parameters: list[object] = []
        if task_id is not None:
            query += " AND task_id = ?"
            parameters.append(task_id)
        if statuses:
            values = tuple(status.value for status in statuses)
            query += f" AND status IN ({','.join('?' for _ in values)})"
            parameters.extend(values)
        query += " ORDER BY id LIMIT ?"
        parameters.append(limit)
        with self._database.connect_read_only() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return tuple(_run_from_row(row) for row in rows)

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> TaskRun | None:
        """原子 claim 最早 queued Run 并写入 worker/lease。"""
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if type(lease_seconds) is not int or lease_seconds < 10:
            raise ValueError("lease_seconds must be an integer of at least 10")
        current = _as_utc(now, "claim time")
        lease = current + timedelta(seconds=lease_seconds)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _raise_if_halted(connection)
            row = connection.execute(
                """
                SELECT id FROM task_runs
                WHERE status = 'queued'
                ORDER BY scheduled_for, id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE task_runs
                SET status = 'claimed', attempt = attempt + 1, worker_id = ?,
                    claimed_at = ?, lease_expires_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id.strip(), current.isoformat(), lease.isoformat(), row["id"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (row["id"],)
            ).fetchone()
        return _run_from_row(claimed)

    def mark_running(self, run_id: int, worker_id: str, *, now: datetime) -> TaskRun:
        """把当前 Worker 持有的 claimed Run 标为 running。"""
        current = _as_utc(now, "run start time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _raise_if_halted(connection)
            updated = connection.execute(
                """
                UPDATE task_runs SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'claimed' AND worker_id = ?
                  AND lease_expires_at > ?
                """,
                (current.isoformat(), run_id, worker_id, current.isoformat()),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_run_transition")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row)

    def renew_lease(
        self,
        run_id: int,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> TaskRun:
        """仅由当前 Worker 延长 claimed/running Run 的 lease。"""
        current = _as_utc(now, "lease renewal time")
        lease = current + timedelta(seconds=lease_seconds)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE task_runs SET lease_expires_at = ?
                WHERE id = ? AND worker_id = ? AND status IN ('claimed', 'running')
                  AND lease_expires_at > ?
                """,
                (lease.isoformat(), run_id, worker_id, current.isoformat()),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_lease_lost")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row)

    def mark_waiting(
        self,
        run_id: int,
        worker_id: str,
        *,
        session_id: int | None,
        turn_id: int | None,
        approval_id: int | None,
    ) -> TaskRun:
        """把 running Run 变为不持有 lease 的 waiting_approval。"""
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE task_runs
                SET status = 'waiting_approval', session_id = ?, turn_id = ?,
                    approval_id = ?, worker_id = NULL, lease_expires_at = NULL
                WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (session_id, turn_id, approval_id, run_id, worker_id),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_run_transition")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row)

    def resume_waiting(
        self,
        run_id: int,
        approval_id: int,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> TaskRun:
        """用绑定的 Approval 把 waiting Run 原子恢复为持 lease 的 running。"""
        if type(approval_id) is not int or approval_id <= 0:
            raise ValueError("approval_id must be a positive integer")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if type(lease_seconds) is not int or lease_seconds < 10:
            raise ValueError("lease_seconds must be an integer of at least 10")
        current = _as_utc(now, "approval continuation time")
        lease = current + timedelta(seconds=lease_seconds)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _raise_if_halted(connection)
            updated = connection.execute(
                """
                UPDATE task_runs
                SET status = 'running', worker_id = ?, lease_expires_at = ?,
                    approval_id = NULL
                WHERE id = ? AND status = 'waiting_approval' AND approval_id = ?
                """,
                (worker_id.strip(), lease.isoformat(), run_id, approval_id),
            )
            if updated.rowcount != 1:
                raise AutomationStateError("task_run_transition")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row)

    def finish(
        self,
        run_id: int,
        *,
        status: RunStatus,
        now: datetime,
        worker_id: str | None = None,
        response: TaskResponse | None = None,
        result_preview: str | None = None,
        error_code: str | None = None,
        usage: dict[str, int | None] | None = None,
        session_id: int | None = None,
        turn_id: int | None = None,
    ) -> TaskRun:
        """把 running/waiting Run 原子结算为一个不可回退终态。"""
        if status not in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.INTERRUPTED,
        }:
            raise ValueError("finish requires a terminal run status")
        current = _as_utc(now, "run completion time")
        for value, name in ((session_id, "session_id"), (turn_id, "turn_id")):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        response_json = None if response is None else _response_json(response)
        usage_json = _json_dumps({} if usage is None else usage)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, worker_id FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None or row["status"] not in {"running", "waiting_approval", "claimed"}:
                raise AutomationStateError("task_run_transition")
            if row["status"] in {"running", "claimed"} and row["worker_id"] != worker_id:
                raise AutomationStateError("task_lease_lost")
            connection.execute(
                """
                UPDATE task_runs
                SET status = ?, worker_id = NULL, lease_expires_at = NULL,
                    completed_at = ?, response_json = ?, result_preview = ?,
                    error_code = ?, usage_json = ?,
                    session_id = COALESCE(?, session_id),
                    turn_id = COALESCE(?, turn_id)
                WHERE id = ?
                """,
                (
                    status.value,
                    current.isoformat(),
                    response_json,
                    result_preview,
                    error_code,
                    usage_json,
                    session_id,
                    turn_id,
                    run_id,
                ),
            )
            result = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(result)

    def recover_stale(self, *, now: datetime) -> RecoveryResult:
        """requeue 未开始 claim，并 interrupt 可能已有副作用的 running Run。"""
        current = _as_utc(now, "stale recovery time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            requeued = connection.execute(
                """
                UPDATE task_runs
                SET status = 'queued', worker_id = NULL, lease_expires_at = NULL,
                    claimed_at = NULL
                WHERE status = 'claimed' AND lease_expires_at <= ?
                """,
                (current.isoformat(),),
            ).rowcount
            interrupted = connection.execute(
                """
                UPDATE task_runs
                SET status = 'interrupted', worker_id = NULL, lease_expires_at = NULL,
                    completed_at = ?, error_code = 'task_lease_lost'
                WHERE status = 'running' AND lease_expires_at <= ?
                """,
                (current.isoformat(), current.isoformat()),
            ).rowcount
        return RecoveryResult(requeued=requeued, interrupted=interrupted)


class AutomationControlRepository:
    """管理只有本地运维入口可修改的 durable E-stop。"""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存数据库和可注入 UTC 时钟。"""
        self._database = database
        self._clock = clock or _utc_now

    def status(self) -> AutomationControl:
        """只读返回当前 halt revision 和 Scheduler heartbeat。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM automation_control WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise AutomationDataError("automation_control_missing")
        return _control_from_row(row)

    def halt(self, reason: str, *, now: datetime | None = None) -> AutomationControl:
        """持久化停止原因并增加 revision。"""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValueError("halt reason must be 1..200 characters")
        current = _as_utc(now or self._clock(), "halt time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE automation_control
                SET halted = 1, reason = ?, revision = revision + 1, updated_at = ?
                WHERE singleton = 1
                """,
                (reason.strip(), current.isoformat()),
            )
            row = connection.execute(
                "SELECT * FROM automation_control WHERE singleton = 1"
            ).fetchone()
        return _control_from_row(row)

    def unhalt(self, *, now: datetime | None = None) -> AutomationControl:
        """由本地运维入口解除 E-stop 并增加 revision。"""
        current = _as_utc(now or self._clock(), "unhalt time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE automation_control
                SET halted = 0, reason = NULL, revision = revision + 1, updated_at = ?
                WHERE singleton = 1
                """,
                (current.isoformat(),),
            )
            row = connection.execute(
                "SELECT * FROM automation_control WHERE singleton = 1"
            ).fetchone()
        return _control_from_row(row)

    def touch_scheduler(self, *, now: datetime | None = None) -> AutomationControl:
        """更新 Doctor 可读取的最近 Scheduler heartbeat。"""
        current = _as_utc(now or self._clock(), "scheduler heartbeat time")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE automation_control
                SET scheduler_heartbeat_at = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (current.isoformat(), current.isoformat()),
            )
            row = connection.execute(
                "SELECT * FROM automation_control WHERE singleton = 1"
            ).fetchone()
        return _control_from_row(row)


def task_run_idempotency_key(task_id: int, scheduled_for: datetime) -> str:
    """为一个 task/UTC slot 生成稳定 SHA-256 幂等键。"""
    if type(task_id) is not int or task_id <= 0:
        raise ValueError("task_id must be positive")
    slot = _as_utc(scheduled_for, "scheduled_for")
    material = f"v1:{task_id}:{slot.isoformat()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _check_task_row(row: sqlite3.Row | None, expected_version: int) -> None:
    """检查 Task 是否存在且 version 与调用方快照一致。"""
    if row is None:
        raise AutomationStateError("task_not_found")
    if type(expected_version) is not int or row["version"] != expected_version:
        raise AutomationStateError("task_version_conflict")


def _raise_if_halted(connection: sqlite3.Connection) -> None:
    """在同一 transaction 内拒绝 halted 状态的新工作。"""
    row = connection.execute(
        "SELECT halted FROM automation_control WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise AutomationDataError("automation_control_missing")
    if row["halted"]:
        raise AutomationStateError("automation_halted")


def _validate_task_input(owner_id: int, name: str, prompt: str, profile: str) -> None:
    """在 SQL 前验证 Task 的最小 identity 和文本边界。"""
    if type(owner_id) is not int or owner_id <= 0:
        raise ValueError("owner_id must be positive")
    if not all(isinstance(value, str) and value.strip() for value in (name, prompt, profile)):
        raise ValueError("task name, prompt and policy profile must be non-empty")


def _task_from_row(row: sqlite3.Row | None) -> ScheduledTask:
    """严格把 scheduled_tasks 行还原为不可变模型。"""
    if row is None:
        raise AutomationDataError("task_row_missing")
    try:
        skills_raw = _json_loads(row["skill_names_json"])
        delivery_raw = _json_loads(row["delivery_json"])
        budget_raw = _json_loads(row["budget_json"])
        if not isinstance(skills_raw, list) or any(
            not isinstance(name, str) for name in skills_raw
        ):
            raise ValueError
        if not isinstance(delivery_raw, dict) or not isinstance(budget_raw, dict):
            raise ValueError
        return ScheduledTask(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            schedule=ScheduleSpec(
                kind=ScheduleKind(row["schedule_kind"]),
                expression=row["schedule_expression"],
                timezone=row["timezone"],
                next_run_at=_parse_datetime(row["next_run_at"]),
            ),
            prompt=row["prompt"],
            skill_names=tuple(skills_raw),
            delivery=DeliveryTarget(
                route=delivery_raw["route"],
                channel=delivery_raw["channel"],
                account_id=delivery_raw.get("account_id"),
                conversation_id=delivery_raw.get("conversation_id"),
            ),
            policy_profile=row["policy_profile"],
            budget=TaskBudget(**budget_raw),
            status=TaskStatus(row["status"]),
            version=row["version"],
            system_key=row["system_key"],
            last_run_at=_parse_datetime(row["last_run_at"]),
            created_at=_parse_required_datetime(row["created_at"]),
            updated_at=_parse_required_datetime(row["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AutomationDataError("task_data_invalid") from error


def _run_from_row(row: sqlite3.Row | None) -> TaskRun:
    """严格解析 TaskRun 行，并验证 snapshot/response JSON。"""
    if row is None:
        raise AutomationDataError("task_run_row_missing")
    try:
        snapshot = _json_loads(row["snapshot_json"])
        usage = _json_loads(row["usage_json"])
        if not isinstance(snapshot, dict) or not isinstance(usage, dict):
            raise ValueError
        response = None
        if row["response_json"] is not None:
            response_raw = _json_loads(row["response_json"])
            if not isinstance(response_raw, dict):
                raise ValueError
            response = TaskResponse(
                notify=response_raw["notify"], text=response_raw["text"]
            )
        return TaskRun(
            id=row["id"],
            task_id=row["task_id"],
            scheduled_for=_parse_required_datetime(row["scheduled_for"]),
            idempotency_key=row["idempotency_key"],
            status=RunStatus(row["status"]),
            attempt=row["attempt"],
            created_at=_parse_required_datetime(row["created_at"]),
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            approval_id=row["approval_id"],
            worker_id=row["worker_id"],
            lease_expires_at=_parse_datetime(row["lease_expires_at"]),
            claimed_at=_parse_datetime(row["claimed_at"]),
            started_at=_parse_datetime(row["started_at"]),
            completed_at=_parse_datetime(row["completed_at"]),
            response=response,
            result_preview=row["result_preview"],
            error_code=row["error_code"],
            snapshot=_snapshot_from_mapping(snapshot),
            parent_run_id=row["parent_run_id"],
            subagent_id=row["subagent_id"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AutomationDataError("task_run_data_invalid") from error


def _snapshot_from_mapping(value: object) -> TaskRunSnapshot:
    """把已解析 canonical snapshot JSON 还原为执行期不可变模型。"""
    if not isinstance(value, dict):
        raise ValueError("task snapshot must be an object")
    skills = value.get("skill_names")
    delivery = value.get("delivery")
    budget = value.get("budget")
    if (
        value.get("schema_version") != 1
        or not isinstance(skills, list)
        or any(not isinstance(name, str) for name in skills)
        or not isinstance(delivery, dict)
        or not isinstance(budget, dict)
    ):
        raise ValueError("task snapshot shape is invalid")
    return TaskRunSnapshot(
        owner_id=value["owner_id"],
        name=value["name"],
        prompt=value["prompt"],
        skill_names=tuple(skills),
        delivery=DeliveryTarget(
            route=delivery["route"],
            channel=delivery["channel"],
            account_id=delivery.get("account_id"),
            conversation_id=delivery.get("conversation_id"),
        ),
        policy_profile=value["policy_profile"],
        budget=TaskBudget(**budget),
    )


def _control_from_row(row: sqlite3.Row | None) -> AutomationControl:
    """解析 automation_control singleton。"""
    if row is None:
        raise AutomationDataError("automation_control_missing")
    try:
        return AutomationControl(
            halted=bool(row["halted"]),
            reason=row["reason"],
            revision=row["revision"],
            scheduler_heartbeat_at=_parse_datetime(row["scheduler_heartbeat_at"]),
            updated_at=_parse_required_datetime(row["updated_at"]),
        )
    except (TypeError, ValueError) as error:
        raise AutomationDataError("automation_control_invalid") from error


def _task_snapshot_json(task: ScheduledTask) -> str:
    """把 Run 所需 Task 事实编码为 canonical JSON。"""
    return _json_dumps(
        {
            "schema_version": 1,
            "task_id": task.id,
            "owner_id": task.owner_id,
            "name": task.name,
            "prompt": task.prompt,
            "skill_names": list(task.skill_names),
            "delivery": _delivery_mapping(task.delivery),
            "policy_profile": task.policy_profile,
            "budget": _budget_mapping(task.budget),
        }
    )


def _delivery_json(delivery: DeliveryTarget) -> str:
    """编码一个已验证 DeliveryTarget。"""
    return _json_dumps(_delivery_mapping(delivery))


def _delivery_mapping(delivery: DeliveryTarget) -> dict[str, str | None]:
    """返回 DeliveryTarget 的稳定 JSON mapping。"""
    return {
        "route": delivery.route,
        "channel": delivery.channel,
        "account_id": delivery.account_id,
        "conversation_id": delivery.conversation_id,
    }


def _budget_json(budget: TaskBudget) -> str:
    """编码一个已验证 TaskBudget。"""
    return _json_dumps(_budget_mapping(budget))


def _budget_mapping(budget: TaskBudget) -> dict[str, int | None]:
    """返回 TaskBudget 的稳定 JSON mapping。"""
    return {
        "timeout_seconds": budget.timeout_seconds,
        "max_turns": budget.max_turns,
        "max_tool_calls": budget.max_tool_calls,
        "max_input_tokens": budget.max_input_tokens,
        "max_output_tokens": budget.max_output_tokens,
        "max_cost_microusd": budget.max_cost_microusd,
    }


def _response_json(response: TaskResponse) -> str:
    """编码 terminal TaskResponse。"""
    return _json_dumps({"notify": response.notify, "text": response.text})


def _json_dumps(value: object) -> str:
    """生成不接受 NaN 的 UTF-8 canonical JSON。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_loads(value: object) -> object:
    """解析标准 JSON，失败时只返回稳定错误。"""
    if not isinstance(value, str):
        raise AutomationDataError("automation_json_invalid")
    try:
        return json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise AutomationDataError("automation_json_invalid") from error


def _reject_json_constant(value: str) -> None:
    """拒绝 NaN 与 Infinity 等非标准 JSON 常量。"""
    raise ValueError("non-standard JSON constant")


def _datetime_text(value: datetime | None) -> str | None:
    """把可选 UTC 时间转换为 ISO 文本。"""
    return None if value is None else _as_utc(value, "datetime").isoformat()


def _parse_required_datetime(value: object) -> datetime:
    """解析必需 UTC 时间。"""
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError("datetime missing")
    return parsed


def _parse_datetime(value: object) -> datetime | None:
    """解析 SQLite 中的可选 ISO 时间；singleton seed 按 UTC 解释。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("datetime must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _as_utc(parsed, "stored datetime")


def _as_utc(value: datetime, name: str) -> datetime:
    """要求 aware datetime 并规范化为 UTC。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
