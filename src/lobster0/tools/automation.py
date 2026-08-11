"""用户可见、action-style 的 Automation Task control Tool。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from lobster0.automation.guard import (
    AutomationGuardError,
    AutomationPromptGuard,
    DeliveryOrigin,
    resolve_delivery_target,
)
from lobster0.automation.models import (
    DeliveryTarget,
    ScheduledTask,
    ScheduleSpec,
    TaskBudget,
)
from lobster0.automation.parser import ScheduleError, parse_schedule
from lobster0.automation.repository import (
    AutomationStateError,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from lobster0.config import ChannelConfig
from lobster0.providers.base import JsonValue
from lobster0.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)

_ACTIONS = frozenset(
    {"create", "list", "show", "update", "pause", "resume", "cancel", "run_now"}
)
_ACTION_RISK = {
    "list": ToolRisk.LOW,
    "show": ToolRisk.LOW,
    "create": ToolRisk.MEDIUM,
    "update": ToolRisk.MEDIUM,
    "pause": ToolRisk.MEDIUM,
    "resume": ToolRisk.MEDIUM,
    "run_now": ToolRisk.MEDIUM,
    "cancel": ToolRisk.HIGH,
}
_CREATE_FIELDS = frozenset(
    {"action", "name", "schedule", "prompt", "skills", "delivery", "budget"}
)
_CREATE_REQUIRED = frozenset({"action", "name", "schedule", "prompt"})
_UPDATE_FIELDS = frozenset(
    {"action", "task_id", "version", "name", "schedule", "prompt", "skills", "delivery", "budget"}
)
_UPDATE_VALUES = _UPDATE_FIELDS - {"action", "task_id", "version"}
_BUDGET_FIELDS = frozenset(
    {
        "timeout_seconds",
        "max_turns",
        "max_tool_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_microusd",
    }
)


def _utc_now() -> datetime:
    """返回 Tool 默认使用的 aware UTC 当前时间。"""
    return datetime.now(UTC)


class ManageTaskTool:
    """为 Owner 提供有限 Task Ledger CRUD，后台 Run 不能调用。"""

    definition = ToolDefinition(
        name="manage_task",
        description=(
            "Create, inspect, update, pause, resume, cancel, or manually run a durable "
            "Lobster0 automation task. Never include credentials in task prompts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(_ACTIONS)},
                "task_id": {"type": "integer", "minimum": 1},
                "version": {"type": "integer", "minimum": 1},
                "name": {"type": "string", "minLength": 1, "maxLength": 200},
                # 这三个字段此前都是裸的 {"type": "object"}，模型无从得知字段名，
                # 只能一次次猜、一次次被拒。形状必须写在 Schema 里。
                "schedule": {
                    "type": "object",
                    "description": (
                        "When to run. Examples: "
                        '{"kind": "cron", "expression": "0 9 * * *"} runs daily at 09:00; '
                        '{"kind": "interval", "expression": "3600"} runs every 3600 seconds '
                        "(minimum 300); "
                        '{"kind": "once", "expression": "2026-08-12T09:00:00+08:00"} runs '
                        "a single time at an RFC 3339 instant with an explicit offset."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["cron", "interval", "once", "heartbeat"],
                        },
                        "expression": {"type": "string", "minLength": 1},
                        "timezone": {
                            "type": "string",
                            "description": "IANA name, defaults to UTC.",
                        },
                    },
                    "required": ["kind", "expression"],
                    "additionalProperties": False,
                },
                "prompt": {"type": "string", "minLength": 1},
                "skills": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "delivery": {
                    "type": "object",
                    "description": (
                        "Where the result goes. Omit it to keep the run silent. "
                        '{"route": "owner"} sends to the Owner\'s default channel.'
                    ),
                    "properties": {
                        "route": {
                            "type": "string",
                            "enum": ["origin", "owner", "explicit", "none"],
                        },
                        "channel": {"type": "string"},
                        "account_id": {"type": "string"},
                        "conversation_id": {"type": "string"},
                    },
                    "required": ["route"],
                    "additionalProperties": False,
                },
                "budget": {
                    "type": "object",
                    "description": "Per-run limits; omit to use defaults.",
                    "properties": {
                        "timeout_seconds": {"type": "integer", "minimum": 1},
                        "max_turns": {"type": "integer", "minimum": 1},
                        "max_tool_calls": {"type": "integer", "minimum": 1},
                        "max_input_tokens": {"type": "integer", "minimum": 1},
                        "max_output_tokens": {"type": "integer", "minimum": 1},
                        "max_cost_microusd": {"type": "integer", "minimum": 0},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk=ToolRisk.HIGH,
    )

    def __init__(
        self,
        tasks: ScheduledTaskRepository,
        runs: TaskRunRepository,
        guard: AutomationPromptGuard,
        channels: ChannelConfig,
        *,
        enabled: bool,
        misfire_grace_seconds: int,
        clock: Callable[[], datetime] | None = None,
        wake: Callable[[], None] | None = None,
    ) -> None:
        """绑定 Repository、Guard、Channel allowlist、时钟与可选 wake callback。"""
        if type(enabled) is not bool:
            raise ValueError("automation enabled must be bool")
        if type(misfire_grace_seconds) is not int or misfire_grace_seconds < 0:
            raise ValueError("misfire_grace_seconds must be non-negative")
        self._tasks = tasks
        self._runs = runs
        self._guard = guard
        self._channels = channels
        self._enabled = enabled
        self._misfire_grace_seconds = misfire_grace_seconds
        self._clock = clock or _utc_now
        self._wake = wake

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """按 action 拒绝未知字段、bool ID、空 update 和非法嵌套形状。"""
        action = arguments.get("action")
        if not isinstance(action, str) or action not in _ACTIONS:
            raise ToolValidationError("manage_task action is invalid")
        fields = set(arguments)
        if action == "list":
            self._require_fields(fields, {"action"}, {"action"})
        elif action in {"show", "run_now"}:
            self._require_fields(fields, {"action", "task_id"}, {"action", "task_id"})
            _positive_int(arguments["task_id"], "task_id")
        elif action in {"pause", "resume", "cancel"}:
            expected = {"action", "task_id", "version"}
            self._require_fields(fields, expected, expected)
            _positive_int(arguments["task_id"], "task_id")
            _positive_int(arguments["version"], "version")
        elif action == "create":
            self._require_fields(fields, _CREATE_FIELDS, _CREATE_REQUIRED)
            self._validate_mutable_fields(arguments, creating=True)
        else:
            self._require_fields(
                fields,
                _UPDATE_FIELDS,
                {"action", "task_id", "version"},
            )
            if not fields & _UPDATE_VALUES:
                raise ToolValidationError("manage_task update requires a changed field")
            _positive_int(arguments["task_id"], "task_id")
            _positive_int(arguments["version"], "version")
            self._validate_mutable_fields(arguments, creating=False)
        return arguments

    def prepare(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """在 Policy/Approval 写库前规范 Prompt、Schedule、Budget 与 Delivery。"""
        if context.source == "automation":
            raise AutomationGuardError("recursive_automation_denied")
        if not _trusted_owner(context):
            raise AutomationGuardError("task_owner_required")
        if not self._enabled:
            raise AutomationGuardError("automation_disabled")
        prepared = dict(arguments)
        if "prompt" in prepared:
            skills = _skill_tuple(prepared.get("skills", []))
            guarded = self._guard.validate(cast(str, prepared["prompt"]), skills)
            prepared["prompt"] = guarded.prompt
            if "skills" in prepared or prepared["action"] == "create":
                prepared["skills"] = list(guarded.skill_names)
        elif "skills" in prepared:
            prepared["skills"] = list(
                self._guard.validate_skills(_skill_tuple(prepared["skills"]))
            )
        elif prepared["action"] == "create":
            prepared["skills"] = []

        if "schedule" in prepared:
            spec = parse_schedule(
                _object_mapping(prepared["schedule"], "schedule"),
                now=self._now(),
                misfire_grace_seconds=self._misfire_grace_seconds,
            )
            prepared["schedule"] = {
                "kind": spec.kind.value,
                "expression": spec.expression,
                "timezone": spec.timezone,
            }
        if "budget" in prepared:
            prepared["budget"] = _budget_mapping(
                _task_budget(prepared["budget"])
            )
        elif prepared["action"] == "create":
            prepared["budget"] = _budget_mapping(TaskBudget())

        if "delivery" in prepared:
            target = resolve_delivery_target(
                _object_mapping(prepared["delivery"], "delivery"),
                _delivery_origin(context),
                self._channels,
            )
            prepared["delivery"] = _delivery_mapping(target)
        elif prepared["action"] == "create":
            prepared["delivery"] = _delivery_mapping(
                DeliveryTarget(route="none", channel="none")
            )
        return prepared

    def effective_risk(self, arguments: dict[str, JsonValue]) -> ToolRisk:
        """把只读、变更和不可逆 action 映射为 LOW/MEDIUM/HIGH。"""
        action = arguments.get("action")
        return _ACTION_RISK.get(action, ToolRisk.CRITICAL)

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """执行已规范化 action，并只返回脱敏 Task/Run 摘要。"""
        if context.source == "automation":
            return ToolResult.failure(
                "recursive_automation_denied",
                "automation runs cannot manage tasks",
            )
        if not _trusted_owner(context):
            return ToolResult.failure(
                "task_owner_required",
                "verified owner context required",
            )
        if not self._enabled:
            return ToolResult.failure("automation_disabled", "automation is disabled")
        action = cast(str, arguments["action"])
        try:
            if action == "list":
                tasks = self._tasks.list(owner_id=context.user_id)
                return ToolResult.success(
                    {"tasks": [_task_summary(task) for task in tasks]}
                )
            if action == "show":
                task = self._tasks.get(
                    cast(int, arguments["task_id"]),
                    owner_id=context.user_id,
                )
                return ToolResult.success(_task_detail(task))
            if action == "create":
                task = self._create(context, arguments)
                self._notify_scheduler()
                return ToolResult.success(_task_summary(task))
            if action == "update":
                task = self._update(context, arguments)
                self._notify_scheduler()
                return ToolResult.success(_task_summary(task))
            if action == "run_now":
                task = self._tasks.get(
                    cast(int, arguments["task_id"]),
                    owner_id=context.user_id,
                )
                run = self._runs.enqueue(
                    task,
                    scheduled_for=self._now(),
                    idempotency_key=f"manual:{uuid4().hex}",
                )
                self._notify_scheduler()
                return ToolResult.success(
                    {"run_id": run.id, "task_id": task.id, "status": run.status.value}
                )
            task_id = cast(int, arguments["task_id"])
            version = cast(int, arguments["version"])
            if action == "pause":
                task = self._tasks.pause(
                    task_id,
                    owner_id=context.user_id,
                    expected_version=version,
                )
            elif action == "resume":
                task = self._tasks.resume(
                    task_id,
                    owner_id=context.user_id,
                    expected_version=version,
                )
            else:
                task = self._tasks.cancel(
                    task_id,
                    owner_id=context.user_id,
                    expected_version=version,
                )
            self._notify_scheduler()
            return ToolResult.success(_task_summary(task))
        except (AutomationStateError, ScheduleError, ValueError) as error:
            code = getattr(error, "code", str(error))
            if not isinstance(code, str) or not code:
                code = "task_operation_failed"
            return ToolResult.failure(code, code)

    def _create(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ScheduledTask:
        """从 prepared create arguments 构造并持久化 Task。"""
        return self._tasks.create(
            owner_id=context.user_id,
            name=cast(str, arguments["name"]),
            schedule=self._schedule(arguments["schedule"]),
            prompt=cast(str, arguments["prompt"]),
            skill_names=_skill_tuple(arguments.get("skills", [])),
            delivery=_delivery_target(arguments["delivery"]),
            policy_profile="automation-default",
            budget=_task_budget(arguments["budget"]),
        )

    def _update(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ScheduledTask:
        """把 prepared update 中存在的字段交给 optimistic Repository。"""
        return self._tasks.update(
            cast(int, arguments["task_id"]),
            owner_id=context.user_id,
            expected_version=cast(int, arguments["version"]),
            name=cast(str, arguments["name"]) if "name" in arguments else None,
            schedule=(
                self._schedule(arguments["schedule"])
                if "schedule" in arguments
                else None
            ),
            prompt=cast(str, arguments["prompt"]) if "prompt" in arguments else None,
            skill_names=(
                _skill_tuple(arguments["skills"])
                if "skills" in arguments
                else None
            ),
            delivery=(
                _delivery_target(arguments["delivery"])
                if "delivery" in arguments
                else None
            ),
            budget=_task_budget(arguments["budget"]) if "budget" in arguments else None,
        )

    def _schedule(self, value: JsonValue) -> ScheduleSpec:
        """从 prepared mapping 按当前时间重建 ScheduleSpec。"""
        return parse_schedule(
            _object_mapping(value, "schedule"),
            now=self._now(),
            misfire_grace_seconds=self._misfire_grace_seconds,
        )

    def _now(self) -> datetime:
        """读取并规范化注入时钟。"""
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("automation clock must be timezone-aware")
        return value.astimezone(UTC)

    def _notify_scheduler(self) -> None:
        """尽力唤醒 Scheduler；持久化成功不依赖进程内通知。"""
        if self._wake is None:
            return
        try:
            self._wake()
        except Exception:  # noqa: BLE001 - SQLite 是事实源，wake 只是优化
            return

    @staticmethod
    def _require_fields(
        actual: set[str],
        allowed: set[str] | frozenset[str],
        required: set[str] | frozenset[str],
    ) -> None:
        """验证 action 对象的允许字段和必需字段。"""
        if actual - set(allowed) or not set(required) <= actual:
            raise ToolValidationError("manage_task fields are invalid for action")

    @staticmethod
    def _validate_mutable_fields(
        arguments: dict[str, JsonValue],
        *,
        creating: bool,
    ) -> None:
        """验证 create/update 可变字段的 JSON 形状。"""
        if "name" in arguments:
            name = arguments["name"]
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ToolValidationError("manage_task name is invalid")
        if "prompt" in arguments:
            prompt = arguments["prompt"]
            if not isinstance(prompt, str) or not prompt.strip():
                raise ToolValidationError("manage_task prompt is invalid")
        if "schedule" in arguments:
            schedule = _object_mapping(arguments["schedule"], "schedule")
            if set(schedule) - {"kind", "expression", "timezone"}:
                raise ToolValidationError("manage_task schedule fields are invalid")
            if not {"kind", "expression"} <= set(schedule):
                raise ToolValidationError("manage_task schedule is incomplete")
        if "skills" in arguments:
            _skill_tuple(arguments["skills"])
        if "delivery" in arguments:
            _object_mapping(arguments["delivery"], "delivery")
        if "budget" in arguments:
            budget = _object_mapping(arguments["budget"], "budget")
            if set(budget) - _BUDGET_FIELDS:
                raise ToolValidationError("manage_task budget fields are invalid")
        if creating and ("name" not in arguments or "prompt" not in arguments):
            raise ToolValidationError("manage_task create fields are incomplete")


def _positive_int(value: JsonValue, name: str) -> int:
    """拒绝 bool 与非正 action ID/version。"""
    if type(value) is not int or value <= 0:
        raise ToolValidationError(f"manage_task {name} must be positive")
    return value


def _object_mapping(value: JsonValue, name: str) -> dict[str, JsonValue]:
    """把 JSON object 收窄为字符串键 mapping。"""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ToolValidationError(f"manage_task {name} must be an object")
    return value


def _skill_tuple(value: JsonValue) -> tuple[str, ...]:
    """把有限 JSON Skill array 转换为 tuple。"""
    if (
        not isinstance(value, list)
        or len(value) > 3
        or any(not isinstance(name, str) for name in value)
    ):
        raise ToolValidationError("manage_task skills are invalid")
    return tuple(cast(list[str], value))


def _task_budget(value: JsonValue) -> TaskBudget:
    """把有限 budget mapping 与默认值合并成强类型模型。"""
    mapping = _object_mapping(value, "budget")
    if set(mapping) - _BUDGET_FIELDS:
        raise ToolValidationError("manage_task budget fields are invalid")
    try:
        return TaskBudget(**mapping)
    except (TypeError, ValueError) as exc:
        raise ToolValidationError("manage_task budget is invalid") from exc


def _budget_mapping(budget: TaskBudget) -> dict[str, JsonValue]:
    """把 TaskBudget 编码为完整 canonical mapping。"""
    return {
        "timeout_seconds": budget.timeout_seconds,
        "max_turns": budget.max_turns,
        "max_tool_calls": budget.max_tool_calls,
        "max_input_tokens": budget.max_input_tokens,
        "max_output_tokens": budget.max_output_tokens,
        "max_cost_microusd": budget.max_cost_microusd,
    }


def _delivery_origin(context: ToolContext) -> DeliveryOrigin | None:
    """从 Core ToolContext 构造模型不可伪造的 origin；缺字段时返回空。"""
    disclosure = context.disclosure
    if (
        disclosure is None
        or disclosure.channel not in {"feishu", "telegram", "discord", "cli"}
        or context.account_id is None
        or context.external_conversation_id is None
    ):
        return None
    return DeliveryOrigin(
        channel=cast(str, disclosure.channel),
        account_id=context.account_id,
        external_conversation_id=context.external_conversation_id,
        conversation_kind=disclosure.conversation_kind,
        identity_verified=disclosure.identity_verified,
    )


def _trusted_owner(context: ToolContext) -> bool:
    """验证 Core 注入的可信 Owner、Requester 与披露身份完全一致。"""
    disclosure = context.disclosure
    return (
        context.trusted_owner
        and disclosure is not None
        and disclosure.identity_verified
        and disclosure.owner_id == context.user_id
        and disclosure.requester_user_id == context.user_id
    )


def _delivery_mapping(target: DeliveryTarget) -> dict[str, JsonValue]:
    """把已解析 target 编码成 Approval 可绑定的 canonical mapping。"""
    return {
        "route": target.route,
        "channel": target.channel,
        "account_id": target.account_id,
        "conversation_id": target.conversation_id,
    }


def _delivery_target(value: JsonValue) -> DeliveryTarget:
    """从 prepared mapping 还原冻结后的 DeliveryTarget。"""
    mapping = _object_mapping(value, "delivery")
    if set(mapping) != {"route", "channel", "account_id", "conversation_id"}:
        raise ToolValidationError("prepared delivery is invalid")
    try:
        return DeliveryTarget(
            route=cast(str, mapping["route"]),
            channel=cast(str, mapping["channel"]),
            account_id=cast(str | None, mapping["account_id"]),
            conversation_id=cast(str | None, mapping["conversation_id"]),
        )
    except (TypeError, ValueError) as exc:
        raise ToolValidationError("prepared delivery is invalid") from exc


def _task_summary(task: ScheduledTask) -> dict[str, JsonValue]:
    """返回不含 Prompt 和平台 ID 的有界 Task 摘要。"""
    return {
        "task_id": task.id,
        "name": task.name,
        "status": task.status.value,
        "schedule_kind": task.schedule.kind.value,
        "next_run_at": (
            None if task.schedule.next_run_at is None else task.schedule.next_run_at.isoformat()
        ),
        "version": task.version,
    }


def _task_detail(task: ScheduledTask) -> dict[str, JsonValue]:
    """返回可管理但不暴露 Prompt 正文或 conversation/account ID 的详情。"""
    return {
        **_task_summary(task),
        "schedule": {
            "kind": task.schedule.kind.value,
            "expression": task.schedule.expression,
            "timezone": task.schedule.timezone,
        },
        "prompt_bytes": len(task.prompt.encode("utf-8")),
        "skills": list(task.skill_names),
        "delivery": {
            "route": task.delivery.route,
            "channel": task.delivery.channel,
        },
        "budget": _budget_mapping(task.budget),
        "last_run_at": None if task.last_run_at is None else task.last_run_at.isoformat(),
    }
