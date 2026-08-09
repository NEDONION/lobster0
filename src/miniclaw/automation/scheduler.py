"""把到期 ScheduledTask 幂等转换为 TaskRun 的轻量异步 Scheduler。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from miniclaw.automation.models import ScheduleKind
from miniclaw.automation.parser import next_occurrence
from miniclaw.automation.repository import (
    AutomationControlRepository,
    AutomationStateError,
    ScheduledTaskRepository,
    TaskRunRepository,
)

_MAX_IDLE_SECONDS = 60.0


def _utc_now() -> datetime:
    """返回 Scheduler 默认使用的 aware UTC 当前时间。"""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SchedulerTick:
    """汇总一次有界扫描的公开计数与下一次持久化唤醒时间。"""

    scanned: int
    enqueued: int
    misfired: int
    next_wake_at: datetime | None
    halted: bool = False


class Scheduler:
    """只负责扫描、幂等入队和推进 Schedule，不执行 Agent。"""

    def __init__(
        self,
        tasks: ScheduledTaskRepository,
        runs: TaskRunRepository,
        control: AutomationControlRepository,
        *,
        max_active_tasks: int,
        misfire_grace_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存 Repository、硬上限、可注入时钟和 lifecycle event。"""
        if type(max_active_tasks) is not int or not 1 <= max_active_tasks <= 1000:
            raise ValueError("max_active_tasks must be between 1 and 1000")
        if type(misfire_grace_seconds) is not int or misfire_grace_seconds < 0:
            raise ValueError("misfire_grace_seconds must be non-negative")
        self._tasks = tasks
        self._runs = runs
        self._control = control
        self._max_active_tasks = max_active_tasks
        self._misfire_grace_seconds = misfire_grace_seconds
        self._clock = clock or _utc_now
        self._wake_event = asyncio.Event()
        self._stopping = False
        self._loop_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """返回后台 loop 是否已启动且尚未结束。"""
        return self._loop_task is not None and not self._loop_task.done()

    async def tick(self, now: datetime) -> SchedulerTick:
        """原子入队一批到期 Task，并对每条 Task 最多补一个 slot。"""
        current = _as_utc(now)
        state = self._control.status()
        if state.halted:
            self._control.touch_scheduler(now=current)
            return SchedulerTick(0, 0, 0, None, halted=True)

        due = self._tasks.list_due(now=current, limit=self._max_active_tasks)
        enqueued = 0
        misfired = 0
        halted = False
        for task in due:
            slot = task.schedule.next_run_at
            if slot is None:
                continue
            is_late = (current - slot).total_seconds() > self._misfire_grace_seconds
            if is_late:
                misfired += 1
            try:
                if task.schedule.kind is ScheduleKind.ONCE and is_late:
                    self._runs.record_misfire_and_complete(
                        task,
                        scheduled_for=slot,
                        now=current,
                    )
                    continue
                terminal = task.schedule.kind is ScheduleKind.ONCE
                next_run_at = None
                if not terminal:
                    next_run_at = next_occurrence(task.schedule, after=current)
                self._runs.enqueue_and_advance(
                    task,
                    scheduled_for=slot,
                    next_run_at=next_run_at,
                    terminal=terminal,
                )
                enqueued += 1
            except AutomationStateError as error:
                code = str(error)
                if code == "automation_halted":
                    halted = True
                    break
                if code == "task_version_conflict":
                    continue
                raise

        self._control.touch_scheduler(now=current)
        next_wake_at = None if halted else self._tasks.next_due_at()
        return SchedulerTick(
            scanned=len(due),
            enqueued=enqueued,
            misfired=misfired,
            next_wake_at=next_wake_at,
            halted=halted,
        )

    async def start(self) -> None:
        """幂等启动非阻塞 Scheduler loop。"""
        if self.running:
            return
        self._stopping = False
        self._loop_task = asyncio.create_task(
            self._run_loop(),
            name="miniclaw-automation-scheduler",
        )

    async def stop(self) -> None:
        """幂等停止新 tick，并等待后台 loop 退出。"""
        task = self._loop_task
        if task is None:
            return
        self._stopping = True
        self._wake_event.set()
        await task
        self._loop_task = None

    def wake(self) -> None:
        """通知 loop 立即重算更早的持久化 next_run_at。"""
        self._wake_event.set()

    async def _run_loop(self) -> None:
        """以 Event 或最多 60 秒 timeout 驱动 tick，避免阻塞 sleep。"""
        while not self._stopping:
            self._wake_event.clear()
            now = _as_utc(self._clock())
            result = await self.tick(now)
            if self._stopping:
                break
            if self._wake_event.is_set():
                continue
            delay = _wake_delay(now, result.next_wake_at)
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
            except TimeoutError:
                continue


def _as_utc(value: datetime) -> datetime:
    """要求 aware datetime 并规范化为 UTC。"""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler time must be timezone-aware")
    return value.astimezone(UTC)


def _wake_delay(now: datetime, next_wake_at: datetime | None) -> float:
    """把持久化 next wake 转成 0..60 秒的 asyncio timeout。"""
    if next_wake_at is None:
        return _MAX_IDLE_SECONDS
    seconds = (next_wake_at - now).total_seconds()
    return max(0.0, min(_MAX_IDLE_SECONDS, seconds))
