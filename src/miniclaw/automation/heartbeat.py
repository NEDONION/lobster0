"""把 Heartbeat 配置 reconcile 为唯一 system-owned ScheduledTask。"""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from miniclaw.automation.models import (
    DeliveryTarget,
    ScheduledTask,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskStatus,
)
from miniclaw.automation.parser import next_occurrence
from miniclaw.automation.repository import ScheduledTaskRepository, TaskRunRepository
from miniclaw.config import HeartbeatConfig

_SYSTEM_KEY = "system:heartbeat:v1"
_HEARTBEAT_PROMPT = """Inspect MiniClaw's local health using only the allowed tools.
Return one bounded summary only when the owner needs to act.
End exactly once with complete_task: use notify=false and empty text when healthy,
or notify=true with a concise actionable Chinese summary when attention is required.
Never create, update, or schedule another task.
"""


@dataclass(frozen=True, slots=True)
class HeartbeatReconcileResult:
    """汇总 system Task identity、入队数量和下一次 UTC slot。"""

    task_id: int | None
    enqueued: int
    next_run_at: datetime | None
    disabled: bool = False
    delayed_busy: bool = False


class HeartbeatReconciler:
    """在单一 Task Ledger 中执行 config、active hours 与容量 reconcile。"""

    def __init__(
        self,
        config: HeartbeatConfig,
        *,
        owner_id: int,
        tasks: ScheduledTaskRepository,
        runs: TaskRunRepository,
        max_concurrent_runs: int,
        delivery: DeliveryTarget,
    ) -> None:
        """绑定已校验配置、Owner、Ledger、全局并发和冻结投递目标。"""
        if not isinstance(config, HeartbeatConfig):
            raise TypeError("heartbeat config is required")
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("heartbeat owner_id must be positive")
        if type(max_concurrent_runs) is not int or not 1 <= max_concurrent_runs <= 16:
            raise ValueError("heartbeat max_concurrent_runs is invalid")
        if not isinstance(delivery, DeliveryTarget):
            raise TypeError("heartbeat delivery target is required")
        self._config = config
        self._owner_id = owner_id
        self._tasks = tasks
        self._runs = runs
        self._max_concurrent_runs = max_concurrent_runs
        self._delivery = delivery
        self._timezone = ZoneInfo(config.timezone)
        self._start = _parse_clock(config.active_hours_start)
        self._end = _parse_clock(config.active_hours_end)
        self._budget = TaskBudget(
            timeout_seconds=min(180, config.interval_seconds),
            max_turns=4,
            max_tool_calls=10,
            max_input_tokens=32_000,
            max_output_tokens=4_000,
        )

    def reconcile(self, now: datetime) -> HeartbeatReconcileResult:
        """幂等创建/更新 Heartbeat，并在活跃且有容量时入队一个到期 slot。"""
        current = _as_utc(now)
        existing = self._tasks.get_by_system_key(self._owner_id, _SYSTEM_KEY)
        if not self._config.enabled:
            if existing is None:
                return HeartbeatReconcileResult(None, 0, None, disabled=True)
            paused = self._reconcile_task(
                existing,
                ScheduleSpec(
                    ScheduleKind.HEARTBEAT,
                    str(self._config.interval_seconds),
                    self._config.timezone,
                    None,
                ),
                status=TaskStatus.PAUSED,
            )
            self._runs.cancel_queued_for_task(paused.id, now=current)
            return HeartbeatReconcileResult(paused.id, 0, None, disabled=True)

        active = _is_active(current.astimezone(self._timezone), self._start, self._end)
        next_slot = current if active else self._next_active_start(current)
        if existing is None:
            task = self._tasks.create(
                owner_id=self._owner_id,
                name="MiniClaw Heartbeat",
                schedule=ScheduleSpec(
                    ScheduleKind.HEARTBEAT,
                    str(self._config.interval_seconds),
                    self._config.timezone,
                    next_slot,
                ),
                prompt=_HEARTBEAT_PROMPT,
                skill_names=(),
                delivery=self._delivery,
                policy_profile="automation-heartbeat",
                budget=self._budget,
                system_key=_SYSTEM_KEY,
            )
        else:
            selected_slot = existing.schedule.next_run_at
            config_changed = (
                existing.schedule.kind is not ScheduleKind.HEARTBEAT
                or existing.schedule.expression != str(self._config.interval_seconds)
                or existing.schedule.timezone != self._config.timezone
                or existing.prompt != _HEARTBEAT_PROMPT
                or existing.delivery != self._delivery
                or existing.budget != self._budget
                or existing.status is not TaskStatus.ACTIVE
            )
            if config_changed or selected_slot is None:
                selected_slot = next_slot
            task = self._reconcile_task(
                existing,
                ScheduleSpec(
                    ScheduleKind.HEARTBEAT,
                    str(self._config.interval_seconds),
                    self._config.timezone,
                    selected_slot,
                ),
                status=TaskStatus.ACTIVE,
            ) if config_changed or existing.schedule.next_run_at is None else existing

        if not active:
            target = self._next_active_start(current)
            if task.schedule.next_run_at != target:
                task = self._reconcile_task(
                    task,
                    replace(task.schedule, next_run_at=target),
                    status=TaskStatus.ACTIVE,
                )
            return HeartbeatReconcileResult(task.id, 0, target)

        slot = task.schedule.next_run_at
        if slot is None or slot > current:
            return HeartbeatReconcileResult(task.id, 0, slot)
        if self._runs.count_active() >= self._max_concurrent_runs:
            delayed = self._bounded_next(current + timedelta(seconds=60))
            task = self._reconcile_task(
                task,
                replace(task.schedule, next_run_at=delayed),
                status=TaskStatus.ACTIVE,
            )
            return HeartbeatReconcileResult(
                task.id,
                0,
                task.schedule.next_run_at,
                delayed_busy=True,
            )

        candidate = next_occurrence(task.schedule, after=current)
        if candidate is None:
            candidate = current + timedelta(seconds=self._config.interval_seconds)
        following = self._bounded_next(candidate)
        _, task = self._runs.enqueue_and_advance(
            task,
            scheduled_for=slot,
            next_run_at=following,
            terminal=False,
        )
        return HeartbeatReconcileResult(task.id, 1, task.schedule.next_run_at)

    def _reconcile_task(
        self,
        task: ScheduledTask,
        schedule: ScheduleSpec,
        *,
        status: TaskStatus,
    ) -> ScheduledTask:
        """经 system-only Repository 更新受管字段。"""
        return self._tasks.reconcile_system(
            task.id,
            owner_id=self._owner_id,
            schedule=schedule,
            prompt=_HEARTBEAT_PROMPT,
            delivery=self._delivery,
            budget=self._budget,
            status=status,
        )

    def _bounded_next(self, candidate: datetime) -> datetime:
        """保留活跃窗口内候选，否则推进到下一个窗口起点。"""
        normalized = _as_utc(candidate)
        if _is_active(normalized.astimezone(self._timezone), self._start, self._end):
            return normalized
        return self._next_active_start(normalized)

    def _next_active_start(self, current: datetime) -> datetime:
        """计算严格晚于当前时间的下一窗口起点并规范化 DST gap/fold。"""
        local = _as_utc(current).astimezone(self._timezone)
        local_time = local.timetz().replace(tzinfo=None)
        if self._start < self._end:
            day = local.date() if local_time < self._start else local.date() + timedelta(days=1)
        else:
            day = local.date() if self._end <= local_time < self._start else local.date()
            candidate = _local_candidate(day, self._start, self._timezone)
            if candidate <= local:
                day += timedelta(days=1)
        return _local_candidate(day, self._start, self._timezone).astimezone(UTC)


def _parse_clock(value: str) -> time:
    """解析已由 Config 校验的 HH:MM。"""
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        raise ValueError("heartbeat active hour is invalid") from error


def _is_active(local: datetime, start: time, end: time) -> bool:
    """判断本地 wall clock 是否落在普通或跨午夜活跃窗口。"""
    clock = local.timetz().replace(tzinfo=None)
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def _local_candidate(day: date, clock: time, timezone: ZoneInfo) -> datetime:
    """选择 DST fold 第一次，并把不存在的 wall clock 正向规范化。"""
    requested = datetime.combine(day, clock, timezone).replace(fold=0)
    return requested.astimezone(UTC).astimezone(timezone)


def _as_utc(value: datetime) -> datetime:
    """要求 aware 时间并规范化为 UTC。"""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("heartbeat time must be timezone-aware")
    return value.astimezone(UTC)
