"""Tool 参数校验、Policy、执行和持久化的唯一入口。"""

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from miniclaw.agent.events import RunEvent, RunEventHandler, emit
from miniclaw.checkpoints.store import CheckpointError, CheckpointStore
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
from miniclaw.sandbox.base import ExecutionPlan, ExecutionReceipt, SandboxPlanError
from miniclaw.sandbox.repository import ExecutionPlanRepository
from miniclaw.storage.tooling import (
    ApprovalRepository,
    PolicyRuleRepository,
    StoredToolRun,
    ToolRunRepository,
)
from miniclaw.tools.base import Tool, ToolContext, ToolResult, ToolRisk, ToolValidationError
from miniclaw.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """返回模型文本，并在等待人工确认时携带持久 Approval ID。"""

    model_text: str
    approval_id: int | None = None
    succeeded: bool = False
    result: ToolResult | None = None


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
    _execution_plan: ExecutionPlan | None = field(repr=False)
    _model_text: str | None = field(repr=False)
    _unstarted_result: ToolResult | None = field(repr=False)
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

    @property
    def unstarted_result(self) -> ToolResult | None:
        """返回预检已确定的无副作用结果，供 Runner 优先处理控制面终止。"""
        return self._unstarted_result


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
        execution_plans: ExecutionPlanRepository | None = None,
        checkpoint_store: CheckpointStore | None = None,
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
        self._execution_plans = execution_plans or runs.execution_plans
        self._checkpoint_store = checkpoint_store
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
        if (
            context.allowed_tool_names is not None
            and call.name not in context.allowed_tool_names
        ):
            result = ToolResult.failure(
                "tool_not_allowed",
                "tool is not allowed in this execution profile",
            )
            return PreparedToolCall(
                _call_id=call.call_id,
                _tool_name=call.name,
                _arguments_json=canonical_arguments_json(call.arguments),
                _context=context,
                _tool=None,
                _decision=None,
                _execution_plan=None,
                _model_text=result.to_model_text(call.name),
                _unstarted_result=result,
                _unstarted_status="denied",
                _executor_token=self._prepare_token,
            )
        if (
            context.source == "automation"
            and context.automation_gate is not None
            and not context.automation_gate()
        ):
            result = ToolResult.failure(
                "automation_halted",
                "automation is halted",
            )
            return PreparedToolCall(
                _call_id=call.call_id,
                _tool_name=call.name,
                _arguments_json=canonical_arguments_json(call.arguments),
                _context=context,
                _tool=None,
                _decision=None,
                _execution_plan=None,
                _model_text=result.to_model_text(call.name),
                _unstarted_result=result,
                _unstarted_status="denied",
                _executor_token=self._prepare_token,
            )
        tool = self._registry.get(call.name)
        if tool is None:
            result = ToolResult.failure(
                "tool_not_found",
                f"tool is not available: {call.name}",
            )
            return PreparedToolCall(
                _call_id=call.call_id,
                _tool_name=call.name,
                _arguments_json=canonical_arguments_json(call.arguments),
                _context=context,
                _tool=None,
                _decision=None,
                _execution_plan=None,
                _model_text=result.to_model_text(call.name),
                _unstarted_result=result,
                _unstarted_status="rejected",
                _executor_token=self._prepare_token,
            )
        try:
            arguments = tool.validate(call.arguments)
        except ToolValidationError as error:
            result = ToolResult.failure("invalid_arguments", str(error))
            return PreparedToolCall(
                _call_id=call.call_id,
                _tool_name=call.name,
                _arguments_json=canonical_arguments_json(call.arguments),
                _context=context,
                _tool=None,
                _decision=None,
                _execution_plan=None,
                _model_text=result.to_model_text(call.name),
                _unstarted_result=result,
                _unstarted_status="rejected",
                _executor_token=self._prepare_token,
            )
        prepare = getattr(tool, "prepare", None)
        if prepare is not None:
            try:
                arguments = prepare(context, arguments)
            except ValueError as error:
                code = _safe_prepare_error_code(error)
                result = ToolResult.failure(code, code)
                return PreparedToolCall(
                    _call_id=call.call_id,
                    _tool_name=call.name,
                    _arguments_json=canonical_arguments_json(call.arguments),
                    _context=context,
                    _tool=None,
                    _decision=None,
                    _execution_plan=None,
                    _model_text=result.to_model_text(call.name),
                    _unstarted_result=result,
                    _unstarted_status="rejected",
                    _executor_token=self._prepare_token,
                )
            if not isinstance(arguments, dict):
                result = ToolResult.failure(
                    "invalid_arguments",
                    "tool preparation returned invalid arguments",
                )
                return PreparedToolCall(
                    _call_id=call.call_id,
                    _tool_name=call.name,
                    _arguments_json=canonical_arguments_json(call.arguments),
                    _context=context,
                    _tool=None,
                    _decision=None,
                    _execution_plan=None,
                    _model_text=result.to_model_text(call.name),
                    _unstarted_result=result,
                    _unstarted_status="rejected",
                    _executor_token=self._prepare_token,
                )

        definition = tool.definition
        effective_risk = getattr(tool, "effective_risk", None)
        if effective_risk is not None:
            risk = effective_risk(arguments)
            if not isinstance(risk, ToolRisk):
                result = ToolResult.failure(
                    "invalid_tool_risk",
                    "tool returned invalid effective risk",
                )
                return PreparedToolCall(
                    _call_id=call.call_id,
                    _tool_name=call.name,
                    _arguments_json=canonical_arguments_json(arguments),
                    _context=context,
                    _tool=None,
                    _decision=None,
                    _execution_plan=None,
                    _model_text=result.to_model_text(call.name),
                    _unstarted_result=result,
                    _unstarted_status="rejected",
                    _executor_token=self._prepare_token,
                )
            definition = replace(definition, risk=risk)

        decision = self._policy.authorize(definition, context, arguments)
        arguments = decision.normalized_arguments or arguments
        execution_plan: ExecutionPlan | None = None
        if decision.action is not PolicyAction.DENY:
            build_plan = getattr(tool, "build_execution_plan", None)
            if build_plan is not None:
                try:
                    candidate = build_plan(context, arguments)
                except SandboxPlanError as error:
                    result = ToolResult.failure(error.code, error.code)
                    return PreparedToolCall(
                        _call_id=call.call_id,
                        _tool_name=call.name,
                        _arguments_json=canonical_arguments_json(arguments),
                        _context=context,
                        _tool=None,
                        _decision=None,
                        _execution_plan=None,
                        _model_text=result.to_model_text(call.name),
                        _unstarted_result=result,
                        _unstarted_status="rejected",
                        _executor_token=self._prepare_token,
                    )
                if not isinstance(candidate, ExecutionPlan):
                    result = ToolResult.failure(
                        "execution_plan_invalid",
                        "tool returned invalid execution plan",
                    )
                    return PreparedToolCall(
                        _call_id=call.call_id,
                        _tool_name=call.name,
                        _arguments_json=canonical_arguments_json(arguments),
                        _context=context,
                        _tool=None,
                        _decision=None,
                        _execution_plan=None,
                        _model_text=result.to_model_text(call.name),
                        _unstarted_result=result,
                        _unstarted_status="rejected",
                        _executor_token=self._prepare_token,
                    )
                execution_plan = candidate
        return PreparedToolCall(
            _call_id=call.call_id,
            _tool_name=call.name,
            _arguments_json=canonical_arguments_json(arguments),
            _context=context,
            _tool=tool,
            _decision=decision,
            _execution_plan=execution_plan,
            _model_text=None,
            _unstarted_result=None,
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
        """执行同一执行器和 ToolContext 生成的不可变单次计划。

        Args:
            context: prepare 时绑定的同一个 Tool 运行边界。
            prepared: 已完成校验、Policy 规范化与 Sandbox Plan 绑定的计划。
            on_event: 可选的结构化运行事件回调。

        Returns:
            模型可见结果、可选审批 ID 与原始 ToolResult。

        Raises:
            ValueError: 计划来自其他执行器、不同 Context 或已被消费。
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
        execution_plan = prepared._execution_plan
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
                    execution_plan=execution_plan,
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

        run_id = self._runs.start(
            context,
            call,
            arguments,
            decision,
            execution_plan=execution_plan,
        )
        return await self._execute_started(
            context,
            tool,
            arguments,
            run_id,
            call.call_id,
            on_event,
            execution_plan=execution_plan,
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
        if (
            context.allowed_tool_names is not None
            and run.tool_name not in context.allowed_tool_names
        ):
            return await self._reject_approved_profile(
                context,
                run,
                "tool_not_allowed",
                "tool is not allowed in this execution profile",
                on_event,
            )
        if (
            context.source == "automation"
            and context.automation_gate is not None
            and not context.automation_gate()
        ):
            return await self._reject_approved_profile(
                context,
                run,
                "automation_halted",
                "automation is halted",
                on_event,
            )
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
        execution_plan: ExecutionPlan | None = None
        if getattr(tool, "execute_plan", None) is not None:
            try:
                execution_plan = self._execution_plans.get(run.id)
                if (
                    run.execution_plan_hash is None
                    or execution_plan.sha256 != run.execution_plan_hash
                ):
                    raise SandboxPlanError("execution_plan_mismatch")
            except SandboxPlanError as error:
                result = ToolResult.failure(error.code, error.code)
                model_text = result.to_model_text(run.tool_name)
                self._runs.fail(run.id, model_text, 0, error.code)
                return await _finish_unstarted(
                    context,
                    ToolCall(run.tool_call_id, run.tool_name, run.arguments),
                    model_text,
                    "failed",
                    on_event,
                    result=result,
                )
        execution = await self._execute_started(
            context,
            tool,
            arguments,
            run.id,
            run.tool_call_id,
            on_event,
            execution_plan=execution_plan,
        )
        if execution.succeeded and decision in {
            ApprovalDecision.SESSION,
            ApprovalDecision.ALWAYS,
        }:
            self._apply_grant(context, approval_id, run, decision)
        return execution

    async def _reject_approved_profile(
        self,
        context: ToolContext,
        run: StoredToolRun,
        error_code: str,
        message: str,
        on_event: RunEventHandler | None,
    ) -> ToolExecution:
        """结算已 consume 但被 automation profile 拒绝的 ToolRun。"""
        result = ToolResult.failure(error_code, message)
        model_text = result.to_model_text(run.tool_name)
        self._runs.fail(run.id, model_text, 0, error_code)
        return await _finish_unstarted(
            context,
            ToolCall(run.tool_call_id, run.tool_name, run.arguments),
            model_text,
            "denied",
            on_event,
            result=result,
        )

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
        *,
        execution_plan: ExecutionPlan | None = None,
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
            if self._checkpoint_store is not None:
                paths = _checkpoint_paths(tool, context, arguments, execution_plan)
                paths = tuple(
                    path for path in paths if self._checkpoint_store.contains(path)
                )
                if paths:
                    await asyncio.to_thread(
                        self._checkpoint_store.capture,
                        paths,
                        reason=tool.definition.name,
                        now=datetime.now(UTC),
                        turn_id=context.turn_id,
                        task_run_id=context.task_run_id,
                        tool_run_id=run_id,
                    )
            if (
                context.source == "automation"
                and context.automation_gate is not None
                and not context.automation_gate()
            ):
                result = ToolResult.failure("automation_halted", "automation is halted")
                model_text = result.to_model_text(tool.definition.name)
            else:
                receipt: ExecutionReceipt | None = None
                execute_plan = getattr(tool, "execute_plan", None)
                if execution_plan is not None and execute_plan is not None:
                    planned = await execute_plan(context, execution_plan)
                    if (
                        not isinstance(planned, tuple)
                        or len(planned) != 2
                        or not isinstance(planned[0], ToolResult)
                        or not isinstance(planned[1], ExecutionReceipt)
                    ):
                        raise TypeError("tool returned an invalid planned result")
                    result, receipt = planned
                    self._execution_plans.complete(run_id, receipt)
                else:
                    result = await tool.execute(context, arguments)
                if not isinstance(result, ToolResult):
                    raise TypeError("tool returned an invalid result")
                model_text = result.to_model_text(tool.definition.name)
        except asyncio.CancelledError:
            self._runs.interrupt(run_id, _elapsed_ms(started))
            raise
        except SandboxPlanError as error:
            result = ToolResult.failure(error.code, error.code)
            model_text = result.to_model_text(tool.definition.name)
        except CheckpointError as error:
            result = ToolResult.failure(error.code, error.code)
            model_text = result.to_model_text(tool.definition.name)
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
        return ToolExecution(model_text, succeeded=result.ok, result=result)


def _checkpoint_paths(
    tool: Tool,
    context: ToolContext,
    arguments: dict[str, JsonValue],
    execution_plan: ExecutionPlan | None,
) -> tuple[Path, ...]:
    """收集 Tool exact targets；automation command 只捕获声明的 writable roots。"""
    declared = getattr(tool, "checkpoint_paths", None)
    if declared is not None:
        paths = declared(context, arguments)
        if not isinstance(paths, tuple) or any(not isinstance(path, Path) for path in paths):
            raise CheckpointError("checkpoint_path_denied")
        return paths
    if context.source == "automation" and execution_plan is not None:
        return execution_plan.write_roots
    return ()


async def _finish_unstarted(
    context: ToolContext,
    call: ToolCall,
    model_text: str,
    status: str,
    on_event: RunEventHandler | None,
    *,
    result: ToolResult | None = None,
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
    return ToolExecution(model_text, result=result)


def _elapsed_ms(started: float) -> int:
    """把 monotonic 秒安全转成非负毫秒。"""
    return max(0, round((time.monotonic() - started) * 1000))


def _safe_prepare_error_code(error: ValueError) -> str:
    """只接受有限 snake_case code，避免把 prepare 异常正文返回模型。"""
    candidate = getattr(error, "code", str(error))
    if (
        isinstance(candidate, str)
        and 3 <= len(candidate) <= 64
        and candidate[0].islower()
        and all(
            character.islower() or character.isdigit() or character == "_"
            for character in candidate
        )
    ):
        return candidate
    return "invalid_arguments"


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
    if tool_name in {"browser_click", "browser_type"}:
        origin = arguments.get("origin")
        role = arguments.get("role")
        if isinstance(origin, str) and isinstance(role, str):
            try:
                parsed = urlsplit(origin)
                hostname = parsed.hostname
                port = parsed.port or 443
            except ValueError:
                hostname = None
            if hostname is not None:
                host_text = f"[{hostname}]" if ":" in hostname else hostname
                summary = f"{tool_name} https://{host_text}:{port} · {role}"
                text = arguments.get("text")
                if tool_name == "browser_type" and isinstance(text, str):
                    summary += f" · {len(text)} chars"
                return summary
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
