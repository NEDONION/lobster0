"""Tool 参数校验、Policy、执行和持久化的唯一入口。"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from miniclaw.agent.events import RunEvent, RunEventHandler, emit
from miniclaw.policy.approvals import (
    ApprovalDecision,
    ApprovalError,
    available_approval_decisions,
    canonical_arguments_json,
)
from miniclaw.policy.command import NormalizedCommand
from miniclaw.policy.engine import PolicyAction, PolicyDecision, PolicyEngine
from miniclaw.policy.network import NetworkRule, normalize_network_rule
from miniclaw.providers.base import JsonValue, ToolCall
from miniclaw.storage.tooling import (
    ApprovalRepository,
    PolicyRuleRepository,
    StoredToolRun,
    ToolRunRepository,
)
from miniclaw.tools.base import Tool, ToolContext, ToolResult, ToolValidationError
from miniclaw.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """返回模型文本，并在等待人工确认时携带持久 Approval ID。"""

    model_text: str
    approval_id: int | None = None
    succeeded: bool = False


class _PreparedConsumption:
    """以线程锁原子记录 prepared plan 是否已经被消费。"""

    def __init__(self) -> None:
        """初始化尚未消费的单次执行状态。"""
        self._lock = Lock()
        self._consumed = False

    def consume(self) -> bool:
        """首次调用原子标记为已消费并返回 True，之后返回 False。"""
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
            return True


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    """保存参数快照不可变、只能消费一次的 Tool 执行计划。"""

    _call_id: str
    _tool_name: str
    _arguments_json: str = field(repr=False)
    _context: ToolContext = field(repr=False)
    _tool: Tool | None = field(repr=False)
    _decision: PolicyDecision | None = field(repr=False)
    _model_text: str | None = field(repr=False)
    _unstarted_status: str | None = field(repr=False)
    _executor_token: object = field(repr=False)
    _consumption: _PreparedConsumption = field(
        default_factory=_PreparedConsumption,
        repr=False,
        compare=False,
    )

    @property
    def call(self) -> ToolCall:
        """从不可变 JSON 快照恢复一个外部可安全修改的独立 ToolCall 副本。"""
        arguments = json.loads(self._arguments_json)
        if not isinstance(arguments, dict):
            raise RuntimeError("prepared Tool arguments snapshot is invalid")
        return ToolCall(self._call_id, self._tool_name, arguments)


class ToolExecutor:
    """确保任何 Tool 都不能绕过验证、Policy 与 ToolRun。"""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        runs: ToolRunRepository,
        *,
        result_max_chars: int = 20_000,
        approvals: ApprovalRepository | None = None,
        policy_rules: PolicyRuleRepository | None = None,
        approval_ttl_seconds: int = 600,
    ) -> None:
        if type(result_max_chars) is not int or result_max_chars <= 0:
            raise ValueError("result_max_chars must be a positive integer")
        if type(approval_ttl_seconds) is not int or approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be a positive integer")
        self._registry = registry
        self._policy = policy
        self._runs = runs
        self._result_max_chars = result_max_chars
        self._approvals = approvals
        self._policy_rules = policy_rules
        self._approval_ttl_seconds = approval_ttl_seconds
        self._prepare_token = object()

    @property
    def schemas(self) -> tuple[dict[str, JsonValue], ...]:
        """返回模型可见的稳定 Tool Schema。"""
        return self._registry.schemas

    async def execute(
        self,
        context: ToolContext,
        call: ToolCall,
        *,
        on_event: RunEventHandler | None = None,
    ) -> ToolExecution:
        """按 get → validate → policy → start → execute → finish 执行。"""
        prepared = self.prepare(context, call)
        return await self.execute_prepared(context, prepared, on_event=on_event)

    def prepare(self, context: ToolContext, call: ToolCall) -> PreparedToolCall:
        """一次完成 Tool 参数校验与 Policy 规范化，返回绑定当前执行器的计划。

        Args:
            context: 不可由模型伪造的当前 Tool 运行边界。
            call: Provider 返回的原始 Tool Call。

        Returns:
            携带规范参数、Policy 决策或稳定预检失败的执行计划。
        """
        tool = self._registry.get(call.name)
        if tool is None:
            return PreparedToolCall(
                _call_id=call.call_id,
                _tool_name=call.name,
                _arguments_json=canonical_arguments_json(call.arguments),
                _context=context,
                _tool=None,
                _decision=None,
                _model_text=ToolResult.failure(
                    "tool_not_found",
                    f"tool is not available: {call.name}",
                ).to_model_text(call.name),
                _unstarted_status="rejected",
                _executor_token=self._prepare_token,
            )
        try:
            arguments = tool.validate(call.arguments)
        except ToolValidationError as error:
            return PreparedToolCall(
                _call_id=call.call_id,
                _tool_name=call.name,
                _arguments_json=canonical_arguments_json(call.arguments),
                _context=context,
                _tool=None,
                _decision=None,
                _model_text=ToolResult.failure(
                    "invalid_arguments",
                    str(error),
                ).to_model_text(call.name),
                _unstarted_status="rejected",
                _executor_token=self._prepare_token,
            )

        decision = self._policy.authorize(tool.definition, context, arguments)
        normalized_source = (
            arguments
            if decision.normalized_arguments is None
            else decision.normalized_arguments
        )
        return PreparedToolCall(
            _call_id=call.call_id,
            _tool_name=call.name,
            _arguments_json=canonical_arguments_json(normalized_source),
            _context=context,
            _tool=tool,
            _decision=decision,
            _model_text=None,
            _unstarted_status=None,
            _executor_token=self._prepare_token,
        )

    async def execute_prepared(
        self,
        context: ToolContext,
        prepared: PreparedToolCall,
        *,
        on_event: RunEventHandler | None = None,
    ) -> ToolExecution:
        """执行同一执行器和 ToolContext 生成的 prepared call，不再解析参数。

        Args:
            context: prepare 时绑定的同一个 Tool 运行边界。
            prepared: 已完成参数与 Policy 规范化的执行计划。
            on_event: 可选的结构化运行事件回调。

        Returns:
            模型可见结果、可选审批 ID 与成功标记。

        Raises:
            ValueError: prepared call 来自其他执行器或不同 ToolContext。
            asyncio.CancelledError: Tool 执行被调用方取消。
        """
        if (
            prepared._executor_token is not self._prepare_token
            or prepared._context is not context
        ):
            raise ValueError("prepared Tool call does not belong to this execution context")
        if not prepared._consumption.consume():
            raise ValueError("prepared Tool call has already been consumed")
        call = prepared.call
        if prepared._model_text is not None:
            assert prepared._unstarted_status is not None
            return await _finish_unstarted(
                context,
                call,
                prepared._model_text,
                prepared._unstarted_status,
                on_event,
            )
        tool = prepared._tool
        decision = prepared._decision
        assert tool is not None and decision is not None
        arguments = call.arguments
        if decision.action is not PolicyAction.ALLOW:
            if decision.action is PolicyAction.DENY:
                self._runs.deny(context, call, arguments, decision.error_code)
            if decision.action is PolicyAction.REQUIRE_APPROVAL and self._approvals is not None:
                approval = self._approvals.create_waiting(
                    context,
                    call,
                    arguments,
                    decision,
                    ttl_seconds=self._approval_ttl_seconds,
                    summary=_approval_summary(call.name, arguments),
                )
                await emit(
                    on_event,
                    RunEvent(
                        "approval_required",
                        context.turn_id,
                        {
                            "approval_id": approval.id,
                            "call_id": call.call_id,
                            "tool_name": call.name,
                            "summary": approval.summary,
                            "arguments": arguments,
                            "expires_at": approval.expires_at.isoformat(),
                            "grant_modes": [
                                mode.value for mode in decision.approval_modes
                            ],
                        },
                    ),
                )
                return ToolExecution(
                    ToolResult.failure(
                        "approval_required",
                        f"approval {approval.id} is required for {call.name}",
                    ).to_model_text(call.name),
                    approval.id,
                )
            code = (
                "approval_required"
                if decision.action is PolicyAction.REQUIRE_APPROVAL
                else decision.error_code
            )
            return await _finish_unstarted(
                context,
                call,
                ToolResult.failure(code, decision.reason).to_model_text(call.name),
                "denied" if decision.action is PolicyAction.DENY else "failed",
                on_event,
            )

        run_id = self._runs.start(context, call, arguments, decision)
        return await self._execute_started(
            context,
            tool,
            arguments,
            run_id,
            call.call_id,
            on_event,
        )

    async def execute_approved(
        self,
        context: ToolContext,
        run: StoredToolRun,
        *,
        approval_id: int,
        decision: ApprovalDecision,
        on_event: RunEventHandler | None = None,
    ) -> ToolExecution:
        """执行已由 Approval 原子 claim 的唯一 running ToolRun。"""
        if run.status != "running":
            raise ValueError("approved ToolRun must be running")
        if decision not in available_approval_decisions(run.tool_name, run.arguments):
            raise ApprovalError("scope_forbidden", "approval scope is not allowed")
        tool = self._registry.get(run.tool_name)
        if tool is None:
            result = ToolResult.failure("tool_not_found", "approved tool is not available")
            model_text = result.to_model_text(run.tool_name)
            self._runs.fail(run.id, model_text, 0, result.error_code)
            return await _finish_unstarted(
                context,
                ToolCall(run.tool_call_id, run.tool_name, run.arguments),
                model_text,
                "failed",
                on_event,
            )
        try:
            arguments = tool.validate(run.arguments)
        except ToolValidationError:
            result = ToolResult.failure(
                "invalid_arguments",
                "approved tool arguments are no longer valid",
            )
            model_text = result.to_model_text(run.tool_name)
            self._runs.fail(run.id, model_text, 0, result.error_code)
            return await _finish_unstarted(
                context,
                ToolCall(run.tool_call_id, run.tool_name, run.arguments),
                model_text,
                "failed",
                on_event,
            )
        execution = await self._execute_started(
            context,
            tool,
            arguments,
            run.id,
            run.tool_call_id,
            on_event,
        )
        if execution.succeeded and decision in {
            ApprovalDecision.SESSION,
            ApprovalDecision.ALWAYS,
        }:
            self._apply_grant(context, approval_id, run, decision)
        return execution

    def _apply_grant(
        self,
        context: ToolContext,
        approval_id: int,
        run: StoredToolRun,
        decision: ApprovalDecision,
    ) -> None:
        """成功后应用当前 Runtime 或持久 exact 规则。"""
        persistent = decision is ApprovalDecision.ALWAYS
        if run.tool_name == "run_command":
            rule = _command_scope(run.arguments)
            if persistent:
                if self._policy_rules is None:
                    raise ApprovalError("scope_unavailable", "persistent rules are unavailable")
                self._policy_rules.add_command_from_approval(
                    context.user_id,
                    approval_id,
                )
            self._policy.add_session_command(rule)
            return
        if run.tool_name == "http_get":
            rule = _network_scope(run.arguments)
            if persistent:
                if self._policy_rules is None:
                    raise ApprovalError("scope_unavailable", "persistent rules are unavailable")
                self._policy_rules.add_network_from_approval(
                    context.user_id,
                    approval_id,
                )
            self._policy.add_session_network(rule)
            return
        raise ApprovalError("scope_forbidden", "approval scope is not allowed")

    async def _execute_started(
        self,
        context: ToolContext,
        tool: Tool,
        arguments: dict[str, JsonValue],
        run_id: int,
        call_id: str,
        on_event: RunEventHandler | None,
    ) -> ToolExecution:
        """执行并终结一个已经处于 running 的 ToolRun。"""
        started = time.monotonic()
        try:
            await emit(
                on_event,
                RunEvent(
                    "tool_started",
                    context.turn_id,
                    {"call_id": call_id, "tool_name": tool.definition.name},
                ),
            )
            result = await tool.execute(context, arguments)
            if not isinstance(result, ToolResult):
                raise TypeError("tool returned an invalid result")
            model_text = result.to_model_text(tool.definition.name)
        except asyncio.CancelledError:
            self._runs.interrupt(run_id, _elapsed_ms(started))
            raise
        except Exception:  # noqa: BLE001 - 内部异常必须在 Tool 边界脱敏
            result = ToolResult.failure("tool_failed", "tool execution failed")
            model_text = result.to_model_text(tool.definition.name)
        if len(model_text) > self._result_max_chars:
            result = ToolResult.failure(
                "tool_result_too_large",
                "tool result exceeded the configured size limit",
            )
            model_text = result.to_model_text(tool.definition.name)
        duration_ms = _elapsed_ms(started)
        if result.ok:
            self._runs.succeed(run_id, model_text, duration_ms)
        else:
            self._runs.fail(run_id, model_text, duration_ms, result.error_code)
        await emit(
            on_event,
            RunEvent(
                "tool_finished",
                context.turn_id,
                {
                    "call_id": call_id,
                    "tool_name": tool.definition.name,
                    "status": "succeeded" if result.ok else "failed",
                    "duration_ms": duration_ms,
                    "preview": model_text,
                },
            ),
        )
        return ToolExecution(model_text, succeeded=result.ok)


async def _finish_unstarted(
    context: ToolContext,
    call: ToolCall,
    model_text: str,
    status: str,
    on_event: RunEventHandler | None,
) -> ToolExecution:
    """终结未进入 running 的 Tool 请求并更新可见卡片。"""
    await emit(
        on_event,
        RunEvent(
            "tool_finished",
            context.turn_id,
            {
                "call_id": call.call_id,
                "tool_name": call.name,
                "status": status,
                "preview": model_text,
            },
        ),
    )
    return ToolExecution(model_text)


def _elapsed_ms(started: float) -> int:
    """把 monotonic 秒安全转成非负毫秒。"""
    return max(0, round((time.monotonic() - started) * 1000))


def _approval_summary(tool_name: str, arguments: dict[str, JsonValue]) -> str:
    """生成有界审批摘要，隐藏正文、凭据、路径和完整命令参数。"""
    if tool_name == "run_command":
        program = arguments.get("program")
        args = arguments.get("args")
        if isinstance(program, str) and isinstance(args, list):
            program_label = Path(program).name or "command"
            suffix = "arg" if len(args) == 1 else "args"
            return f"run_command {program_label} · {len(args)} {suffix}"
    if tool_name == "http_get":
        url = arguments.get("url")
        if isinstance(url, str):
            try:
                parsed = urlsplit(url)
                hostname = parsed.hostname
                port = parsed.port or 443
            except ValueError:
                hostname = None
            if hostname is not None:
                host_text = f"[{hostname}]" if ":" in hostname else hostname
                return f"http_get https://{host_text}:{port}"
    path = arguments.get("path")
    if isinstance(path, str):
        return f"{tool_name} {Path(path).name}"
    return f"{tool_name} request"


def _command_scope(arguments: dict[str, JsonValue]) -> NormalizedCommand:
    """从已验证且 hash 绑定的参数恢复 exact argv。"""
    program = arguments.get("program")
    args = arguments.get("args")
    if not isinstance(program, str) or not isinstance(args, list) or any(
        not isinstance(argument, str) for argument in args
    ):
        raise ApprovalError("hash_mismatch", "approval command arguments are invalid")
    return NormalizedCommand(program, tuple(args))


def _network_scope(arguments: dict[str, JsonValue]) -> NetworkRule:
    """从已验证且 hash 绑定的 URL 恢复 exact authority。"""
    url = arguments.get("url")
    if not isinstance(url, str):
        raise ApprovalError("hash_mismatch", "approval network arguments are invalid")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port or 443
    except ValueError:
        hostname = None
    if hostname is None:
        raise ApprovalError("hash_mismatch", "approval network arguments are invalid")
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    return normalize_network_rule(host_text if port == 443 else f"{host_text}:{port}")
