"""Phase 6 Automation 的不可变公共数据契约。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

type DeliveryRoute = Literal["origin", "owner", "explicit", "none"]
type DeliveryChannel = Literal["feishu", "telegram", "discord", "cli", "none"]

_ACTIVE_RUN_STATUSES = frozenset({"claimed", "running"})
_TERMINAL_RUN_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
)


class ScheduleKind(StrEnum):
    """表示支持的四种调度表达式。"""

    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"
    HEARTBEAT = "heartbeat"


class TaskStatus(StrEnum):
    """表示 ScheduledTask 的持久状态。"""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    """表示 TaskRun 的 claim、执行与终态。"""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """保存已规范化的调度类型、表达式、时区和下次 UTC 时间。"""

    kind: ScheduleKind
    expression: str
    timezone: str
    next_run_at: datetime | None

    def __post_init__(self) -> None:
        """拒绝空表达式、空时区和非 UTC aware 时间。"""
        if not isinstance(self.kind, ScheduleKind):
            raise ValueError("schedule kind is invalid")
        if not isinstance(self.expression, str) or not self.expression.strip():
            raise ValueError("schedule expression must be non-empty")
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise ValueError("schedule timezone must be non-empty")
        _require_aware_utc(self.next_run_at, "schedule next_run_at", optional=True)


@dataclass(frozen=True, slots=True)
class TaskBudget:
    """保存单次后台 Run 不可扩大的资源预算。"""

    timeout_seconds: int = 600
    max_turns: int = 8
    max_tool_calls: int = 30
    max_input_tokens: int = 64_000
    max_output_tokens: int = 16_000
    max_cost_microusd: int | None = None

    def __post_init__(self) -> None:
        """拒绝 bool、零值、负值和非法可选费用。"""
        for name in (
            "timeout_seconds",
            "max_turns",
            "max_tool_calls",
            "max_input_tokens",
            "max_output_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"task budget {name} must be a positive integer")
        if self.max_cost_microusd is not None and (
            type(self.max_cost_microusd) is not int or self.max_cost_microusd < 0
        ):
            raise ValueError("task budget max_cost_microusd must be non-negative")


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """保存创建 Task 时已经解析并冻结的投递目的地。"""

    route: DeliveryRoute
    channel: DeliveryChannel
    account_id: str | None = None
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        """拒绝无目的地 route 和携带目的地的静默 route。"""
        if self.route == "none":
            if (
                self.channel != "none"
                or self.account_id is not None
                or self.conversation_id is not None
            ):
                raise ValueError("delivery none route cannot carry a destination")
            return
        if self.route not in {"origin", "owner", "explicit"}:
            raise ValueError("delivery route is invalid")
        if self.channel not in {"feishu", "telegram", "discord"}:
            raise ValueError("delivery route requires an IM channel")
        if not _is_non_empty_text(self.account_id) or not _is_non_empty_text(
            self.conversation_id
        ):
            raise ValueError("delivery route requires account and conversation")


@dataclass(frozen=True, slots=True)
class TaskResponse:
    """保存后台 Agent 通过 terminal Tool 返回的结构化结果。"""

    notify: bool
    text: str

    def __post_init__(self) -> None:
        """静默响应必须为空；通知响应必须包含有限文本。"""
        if type(self.notify) is not bool or not isinstance(self.text, str):
            raise ValueError("task response notify/text is invalid")
        if not self.notify and self.text:
            raise ValueError("notify=false requires empty text")
        if self.notify and not self.text.strip():
            raise ValueError("notify=true requires non-empty text")
        if len(self.text.encode("utf-8")) > 256 * 1024:
            raise ValueError("task response text is too large")


@dataclass(frozen=True, slots=True)
class TaskRunSnapshot:
    """保存 Run 入队时冻结、执行所需且不含 Schedule 的 Task 事实。"""

    owner_id: int
    name: str
    prompt: str
    skill_names: tuple[str, ...]
    delivery: DeliveryTarget
    policy_profile: str
    budget: TaskBudget

    def __post_init__(self) -> None:
        """校验 Owner、文本、Skill、Delivery 与 Budget。"""
        _require_positive_int(self.owner_id, "task snapshot owner_id")
        for value, name in (
            (self.name, "task snapshot name"),
            (self.prompt, "task snapshot prompt"),
            (self.policy_profile, "task snapshot policy_profile"),
        ):
            if not _is_non_empty_text(value):
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.skill_names, tuple) or any(
            not _is_non_empty_text(skill) for skill in self.skill_names
        ):
            raise ValueError("task snapshot skill_names are invalid")
        if not isinstance(self.delivery, DeliveryTarget):
            raise ValueError("task snapshot delivery is invalid")
        if not isinstance(self.budget, TaskBudget):
            raise ValueError("task snapshot budget is invalid")


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    """保存一条 owner-scoped、可乐观并发更新的 ScheduledTask。"""

    id: int
    owner_id: int
    name: str
    schedule: ScheduleSpec
    prompt: str
    skill_names: tuple[str, ...]
    delivery: DeliveryTarget
    policy_profile: str
    budget: TaskBudget
    status: TaskStatus
    version: int
    created_at: datetime
    updated_at: datetime
    system_key: str | None = None
    last_run_at: datetime | None = None

    def __post_init__(self) -> None:
        """校验内部 ID、文本、Skill、版本与 UTC 时间。"""
        _require_positive_int(self.id, "task id")
        _require_positive_int(self.owner_id, "task owner_id")
        _require_positive_int(self.version, "task version")
        for value, name in (
            (self.name, "task name"),
            (self.prompt, "task prompt"),
            (self.policy_profile, "task policy_profile"),
        ):
            if not _is_non_empty_text(value):
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.schedule, ScheduleSpec):
            raise ValueError("task schedule is invalid")
        if not isinstance(self.delivery, DeliveryTarget):
            raise ValueError("task delivery is invalid")
        if not isinstance(self.budget, TaskBudget):
            raise ValueError("task budget is invalid")
        if not isinstance(self.status, TaskStatus):
            raise ValueError("task status is invalid")
        if not isinstance(self.skill_names, tuple) or any(
            not _is_non_empty_text(name) for name in self.skill_names
        ):
            raise ValueError("task skill_names must be non-empty strings")
        if self.system_key is not None and not _is_non_empty_text(self.system_key):
            raise ValueError("task system_key must be non-empty when present")
        _require_aware_utc(self.created_at, "task created_at")
        _require_aware_utc(self.updated_at, "task updated_at")
        _require_aware_utc(self.last_run_at, "task last_run_at", optional=True)


@dataclass(frozen=True, slots=True)
class TaskRun:
    """保存一个不可与其他 schedule slot 混淆的 TaskRun 快照。"""

    id: int
    task_id: int
    scheduled_for: datetime
    idempotency_key: str
    status: RunStatus
    attempt: int
    created_at: datetime
    session_id: int | None = None
    turn_id: int | None = None
    # depth-1 派发：为空表示这是一次普通 Run。深度由此推导，不单独存列。
    parent_run_id: int | None = None
    subagent_id: str | None = None
    approval_id: int | None = None
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    response: TaskResponse | None = None
    result_preview: str | None = None
    error_code: str | None = None
    snapshot: TaskRunSnapshot | None = None

    def __post_init__(self) -> None:
        """校验状态、lease、引用 ID、幂等键和全部 UTC 时间。"""
        _require_positive_int(self.id, "task run id")
        _require_positive_int(self.task_id, "task run task_id")
        if type(self.attempt) is not int or self.attempt < 0:
            raise ValueError("task run attempt must be a non-negative integer")
        if not isinstance(self.status, RunStatus):
            raise ValueError("task run status is invalid")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValueError("task run idempotency_key must be non-empty")
        for value, name in (
            (self.session_id, "session_id"),
            (self.turn_id, "turn_id"),
            (self.approval_id, "approval_id"),
        ):
            if value is not None:
                _require_positive_int(value, f"task run {name}")
        for value, name in (
            (self.scheduled_for, "scheduled_for"),
            (self.created_at, "created_at"),
            (self.lease_expires_at, "lease_expires_at"),
            (self.claimed_at, "claimed_at"),
            (self.started_at, "started_at"),
            (self.completed_at, "completed_at"),
        ):
            _require_aware_utc(
                value,
                f"task run {name}",
                optional=name not in {"scheduled_for", "created_at"},
            )
        if self.status.value in _TERMINAL_RUN_STATUSES and (
            self.worker_id is not None or self.lease_expires_at is not None
        ):
            raise ValueError("terminal task run cannot retain worker or lease")
        if self.status.value in _ACTIVE_RUN_STATUSES and (
            not _is_non_empty_text(self.worker_id) or self.lease_expires_at is None
        ):
            raise ValueError("active task run requires worker and lease")
        if self.response is not None and not isinstance(self.response, TaskResponse):
            raise ValueError("task run response is invalid")
        if self.snapshot is not None and not isinstance(self.snapshot, TaskRunSnapshot):
            raise ValueError("task run snapshot is invalid")


def _require_positive_int(value: object, name: str) -> None:
    """拒绝 bool 和非正内部整数 ID。"""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_aware_utc(
    value: datetime | None,
    name: str,
    *,
    optional: bool = False,
) -> None:
    """要求持久化时间为 aware UTC，或在允许时为空。"""
    if value is None:
        if optional:
            return
        raise ValueError(f"{name} must be timezone-aware UTC")
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _is_non_empty_text(value: object) -> bool:
    """判断值是否是不含 NUL 的非空文本。"""
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value
