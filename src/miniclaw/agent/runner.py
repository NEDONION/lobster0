"""执行有上限的模型与顺序 Tool Call 循环。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from miniclaw.agent.events import RunEvent, RunEventHandler, emit
from miniclaw.automation.models import TaskResponse
from miniclaw.policy.approvals import ApprovalDecision
from miniclaw.providers.base import (
    JsonValue,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StreamHandler,
    ToolCall,
)
from miniclaw.storage.tooling import StoredToolRun
from miniclaw.tools.base import ToolContext, ToolResult

if TYPE_CHECKING:
    from miniclaw.tools.executor import PreparedToolCall, ToolExecutor

_MAX_AUTOMATION_OUTPUT_BYTES = 256 * 1024


class AgentError(RuntimeError):
    """表示 Provider 之外的稳定 Agent Loop 失败。"""


class EmptyModelResponseError(AgentError):
    """表示模型没有 Tool Call，也没有可保存的最终文本。"""


class AgentLoopLimitError(AgentError):
    """表示模型在允许的最后一轮仍继续请求工具。"""


class AgentNoProgressError(AgentError):
    """表示模型连续多轮没有产生新的成功 Tool 结果。"""

    def __init__(self, *, no_progress_iterations: int, model_iteration: int) -> None:
        """保存停止时连续无进展轮数和当前模型轮次。

        Args:
            no_progress_iterations: 已达到的连续无进展模型轮数。
            model_iteration: 本次 Agent Run 的当前模型调用轮次。

        Raises:
            ValueError: 任一诊断轮次不是正整数。
        """
        if type(no_progress_iterations) is not int or no_progress_iterations <= 0:
            raise ValueError("no_progress_iterations must be a positive integer")
        if type(model_iteration) is not int or model_iteration <= 0:
            raise ValueError("model_iteration must be a positive integer")
        self.no_progress_iterations = no_progress_iterations
        self.model_iteration = model_iteration
        super().__init__(
            "agent stopped after "
            f"{no_progress_iterations} model rounds without a new successful tool result "
            f"at model iteration {model_iteration}"
        )


class AgentRunStatus(StrEnum):
    """区分最终回答与等待人工确认的正常业务结果。"""

    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting_approval"


@dataclass(frozen=True, slots=True)
class AgentRunBudget:
    """保存单次 Automation Agent Loop 不可扩大的硬预算。"""

    max_turns: int
    max_tool_calls: int
    timeout_seconds: int = 600
    max_input_tokens: int = 64_000
    max_output_tokens: int = 16_000
    max_cost_microusd: int | None = None

    def __post_init__(self) -> None:
        """拒绝 bool、零值、负值与非法可选费用。"""
        for name in (
            "max_turns",
            "max_tool_calls",
            "timeout_seconds",
            "max_input_tokens",
            "max_output_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_cost_microusd is not None and (
            type(self.max_cost_microusd) is not int
            or self.max_cost_microusd < 0
        ):
            raise ValueError("max_cost_microusd must be non-negative")


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """汇总一次 Agent Loop 的最终文本、轮数、用量和诊断 ID。"""

    content: str
    iterations: int
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None
    finish_reason: str
    context_tokens: int | None
    reported_input_tokens: int | None
    reported_output_tokens: int | None
    tool_calls_count: int
    status: AgentRunStatus = AgentRunStatus.COMPLETED
    approval_id: int | None = None
    intermediate_messages: tuple[ModelMessage, ...] = ()
    reported_cost_microusd: int | None = None
    terminal_response: TaskResponse | None = None
    error_code: str | None = None


class AgentRunner:
    """通过一个 Provider 顺序执行有限 Tool Call 并返回最终回答。"""

    def __init__(
        self,
        provider: ModelProvider,
        executor: ToolExecutor | None = None,
        *,
        max_iterations: int = 32,
        hard_max_iterations: int = 64,
        max_no_progress_iterations: int = 3,
    ) -> None:
        """绑定 Provider、当前可用工具和严格正数循环上限。

        Args:
            provider: 实际或 Fake 模型边界。
            executor: 可选的唯一安全 Tool 执行入口。
            max_iterations: 包含最终响应在内的最多模型调用次数，默认 32。
            hard_max_iterations: 任何自适应策略都不能超过的循环硬上限，默认 64。
            max_no_progress_iterations: 允许连续无进展 Tool 循环的次数上限，默认 3。

        Raises:
            ValueError: 任一循环预算不是正整数，或 hard 上限低于常规上限。
        """
        if type(max_iterations) is not int or max_iterations <= 0:
            raise ValueError("max_iterations must be a positive integer")
        if type(hard_max_iterations) is not int or hard_max_iterations <= 0:
            raise ValueError("hard_max_iterations must be a positive integer")
        if type(max_no_progress_iterations) is not int or max_no_progress_iterations <= 0:
            raise ValueError("max_no_progress_iterations must be a positive integer")
        if hard_max_iterations < max_iterations:
            raise ValueError("hard_max_iterations must be greater than or equal to max_iterations")
        self._provider = provider
        self._executor = executor
        self._max_iterations = max_iterations
        self._hard_max_iterations = hard_max_iterations
        self._max_no_progress_iterations = max_no_progress_iterations

    @property
    def tool_schemas(self) -> tuple[dict[str, JsonValue], ...]:
        """返回交互式模型可见 Schema，并隐藏 automation-only terminal Tool。"""
        return self.tool_schemas_for(None)

    def tool_schemas_for(
        self,
        allowed_tool_names: frozenset[str] | None,
    ) -> tuple[dict[str, JsonValue], ...]:
        """按 execution profile 过滤 Schema；交互式默认隐藏 complete_task。"""
        if self._executor is None:
            return ()
        schemas = self._executor.schemas
        if allowed_tool_names is None:
            return tuple(
                schema
                for schema in schemas
                if schema["function"]["name"] != "complete_task"
            )
        return tuple(
            schema
            for schema in schemas
            if schema["function"]["name"] in allowed_tool_names
        )

    async def execute_approved(
        self,
        context: ToolContext,
        run: StoredToolRun,
        approval_id: int,
        decision: ApprovalDecision,
        on_event: RunEventHandler | None = None,
    ) -> str:
        """通过同一 Executor 执行已消费的绑定 ToolRun。"""
        if self._executor is None:
            raise AgentError("tool executor is required for approval continuation")
        return (
            await self._executor.execute_approved(
                context,
                run,
                approval_id=approval_id,
                decision=decision,
                on_event=on_event,
            )
        ).model_text

    async def run(
        self,
        request: ModelRequest,
        on_text: StreamHandler | None = None,
        *,
        tool_context: ToolContext | None = None,
        on_intermediate: Callable[[tuple[ModelMessage, ...]], None] | None = None,
        on_event: RunEventHandler | None = None,
        budget: AgentRunBudget | None = None,
    ) -> AgentRunResult:
        """执行模型与工具循环，直到获得非空最终回答。

        Args:
            request: 初始上下文、Tool Schema 与生成预算。
            on_text: 可选的最终可见文本流回调，由 Provider 施加背压。
            tool_context: 当前 Tool 不可由模型伪造的运行边界。
            on_intermediate: 每批 Tool Call/Result 完成后的同步持久化回调。
            on_event: 可选的结构化运行事件回调。
            budget: 可选的 Turn、Tool、Token 与费用硬预算。

        Returns:
            最终回答、实际模型调用轮数与累计 Token 用量。

        Raises:
            EmptyModelResponseError: 最终响应没有文本也没有工具调用。
            AgentLoopLimitError: 最后一轮仍请求工具。
            AgentNoProgressError: 连续多轮没有新的成功 Tool 结果。
            ProviderError: Provider 调用失败，保持原具体类型。
            asyncio.CancelledError: 调用方取消，Runner 不拦截。
        """
        messages = list(request.messages)
        intermediate_messages: list[ModelMessage] = []
        input_tokens = 0
        output_tokens = 0
        cost_microusd = 0
        input_usage_complete = True
        output_usage_complete = True
        cost_usage_complete = True
        provider_request_id: str | None = None
        seen_tool_call_ids: set[str] = set()
        attempted_tool_fingerprints: set[str] = set()
        consecutive_no_progress = 0
        previous_batch_progressed = False
        soft_budget_extended = False
        executed_tool_calls = 0
        round_chunks: list[str] = []

        def build_result(
            *,
            content: str,
            iterations: int,
            finish_reason: str,
            context_tokens: int | None,
            status: AgentRunStatus = AgentRunStatus.COMPLETED,
            approval_id: int | None = None,
            terminal_response: TaskResponse | None = None,
            error_code: str | None = None,
        ) -> AgentRunResult:
            return AgentRunResult(
                content=content,
                iterations=iterations,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider_request_id=provider_request_id,
                finish_reason=finish_reason,
                context_tokens=context_tokens,
                reported_input_tokens=input_tokens if input_usage_complete else None,
                reported_output_tokens=output_tokens if output_usage_complete else None,
                tool_calls_count=len(seen_tool_call_ids),
                status=status,
                approval_id=approval_id,
                intermediate_messages=tuple(intermediate_messages),
                reported_cost_microusd=(
                    cost_microusd if cost_usage_complete else None
                ),
                terminal_response=terminal_response,
                error_code=error_code,
            )

        async def capture_text(chunk: str) -> None:
            round_chunks.append(chunk)
            if tool_context is not None:
                await emit(
                    on_event,
                    RunEvent(
                        "model_text_delta",
                        tool_context.turn_id,
                        {"text": chunk},
                    ),
                )

        loop_limit = (
            self._hard_max_iterations
            if budget is None
            else min(self._hard_max_iterations, budget.max_turns)
        )
        for iteration in range(1, loop_limit + 1):
            preflight_error = _budget_preflight_error(
                budget,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                input_usage_complete=input_usage_complete,
                output_usage_complete=output_usage_complete,
                cost_usage_complete=cost_usage_complete,
                has_prior_response=iteration > 1,
            )
            if preflight_error is not None:
                return build_result(
                    content="",
                    iterations=iteration - 1,
                    finish_reason=preflight_error,
                    context_tokens=None,
                    error_code=preflight_error,
                )
            provider_output_limit = _provider_output_limit(
                request.max_output_tokens,
                budget,
                output_tokens,
                output_usage_complete,
            )
            hard_boundary = iteration == self._hard_max_iterations
            soft_boundary = iteration == self._max_iterations
            if soft_boundary and previous_batch_progressed and not hard_boundary:
                soft_budget_extended = True
            finalization_request = hard_boundary or (
                soft_boundary and not soft_budget_extended
            )
            current_messages = tuple(messages)
            if finalization_request:
                current_messages += (
                    ModelMessage(
                        role="system",
                        content=(
                            "Provide the best evidence-based final answer now using only "
                            "the tool results already available. Do not call tools."
                        ),
                    ),
                )
            current = replace(
                request,
                messages=current_messages,
                tools=() if finalization_request else request.tools,
                max_output_tokens=provider_output_limit,
            )
            round_chunks.clear()

            response = await self._provider.complete(
                current,
                capture_text if on_text is not None or on_event is not None else None,
            )
            if response.input_tokens is None:
                input_usage_complete = False
            else:
                input_tokens += response.input_tokens
            if response.output_tokens is None:
                output_usage_complete = False
            else:
                output_tokens += response.output_tokens
            if response.cost_microusd is None:
                cost_usage_complete = False
            elif type(response.cost_microusd) is not int or response.cost_microusd < 0:
                raise AgentError("model returned invalid cost usage")
            else:
                cost_microusd += response.cost_microusd
            provider_request_id = response.provider_request_id or provider_request_id
            call_ids = [call.call_id for call in response.tool_calls]
            if (
                any(not call_id.strip() for call_id in call_ids)
                or len(set(call_ids)) != len(call_ids)
                or not seen_tool_call_ids.isdisjoint(call_ids)
            ):
                raise AgentError("model returned an empty or duplicate tool call id")
            seen_tool_call_ids.update(call_ids)
            if tool_context is not None:
                await emit(
                    on_event,
                    RunEvent(
                        "model_usage",
                        tool_context.turn_id,
                        {
                            "iteration": iteration,
                            "context_tokens": response.input_tokens,
                            "input_tokens": (
                                input_tokens if input_usage_complete else None
                            ),
                            "output_tokens": (
                                output_tokens if output_usage_complete else None
                            ),
                            "tool_calls": len(seen_tool_call_ids),
                            "provider_request_id": provider_request_id,
                            "cost_microusd": (
                                cost_microusd if cost_usage_complete else None
                            ),
                        },
                    ),
                )
            if tool_context is not None and response.reasoning_content:
                await emit(
                    on_event,
                    RunEvent(
                        "model_reasoning",
                        tool_context.turn_id,
                        {"text": response.reasoning_content},
                    ),
                )

            budget_error = _budget_response_error(
                budget,
                response,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                input_usage_complete=input_usage_complete,
                output_usage_complete=output_usage_complete,
                cost_usage_complete=cost_usage_complete,
            )
            if budget_error is not None:
                return build_result(
                    content="",
                    iterations=iteration,
                    finish_reason=budget_error,
                    context_tokens=response.input_tokens,
                    error_code=budget_error,
                )

            if not response.tool_calls:
                if not response.content.strip():
                    raise EmptyModelResponseError("model returned an empty final response")
                if on_text is not None:
                    for chunk in round_chunks:
                        await on_text(chunk)
                return build_result(
                    content=response.content,
                    iterations=iteration,
                    finish_reason=response.finish_reason,
                    context_tokens=response.input_tokens,
                )
            if (
                budget is not None
                and budget.max_turns <= self._hard_max_iterations
                and iteration == loop_limit
            ):
                return build_result(
                    content="",
                    iterations=iteration,
                    finish_reason="task_budget_turns",
                    context_tokens=response.input_tokens,
                    error_code="task_budget_turns",
                )
            if finalization_request:
                raise AgentLoopLimitError(
                    f"agent reached the model iteration limit ({iteration})"
                )

            if self._executor is not None and tool_context is None:
                raise AgentError("tool context is required")

            assistant_message = _assistant_tool_message(response)
            batch_start = len(intermediate_messages)
            messages.append(assistant_message)
            intermediate_messages.append(assistant_message)
            batch_progressed = False
            for call in response.tool_calls:
                if tool_context is not None:
                    await emit(
                        on_event,
                        RunEvent(
                            "tool_requested",
                            tool_context.turn_id,
                            {
                                "call_id": call.call_id,
                                "tool_name": call.name,
                                "summary": call.name,
                                "arguments": call.arguments,
                            },
                        ),
                    )
                prepared: PreparedToolCall | None = None
                fingerprint_call = call
                if self._executor is not None:
                    assert tool_context is not None
                    prepared = self._executor.prepare(tool_context, call)
                    fingerprint_call = prepared.call
                fingerprint = _tool_fingerprint(fingerprint_call)
                prepared_result = None if prepared is None else prepared.unstarted_result
                control_stop = (
                    prepared_result is not None
                    and not prepared_result.ok
                    and prepared_result.error_code == "automation_halted"
                )
                if fingerprint in attempted_tool_fingerprints and not control_stop:
                    tool_message = ModelMessage(
                        role="tool",
                        content=json.dumps(
                            {
                                "ok": False,
                                "error": "duplicate_tool_call",
                                "tool": call.name,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        tool_call_id=call.call_id,
                    )
                    approval_id = None
                    executed_result = None
                    if tool_context is not None:
                        await emit(
                            on_event,
                            RunEvent(
                                "tool_finished",
                                tool_context.turn_id,
                                {
                                    "call_id": call.call_id,
                                    "tool_name": call.name,
                                    "status": "failed",
                                    "error_code": "duplicate_tool_call",
                                    "preview": tool_message.content,
                                },
                            ),
                        )
                else:
                    if (
                        not control_stop
                        and budget is not None
                        and executed_tool_calls >= budget.max_tool_calls
                    ):
                        budget_message = ModelMessage(
                            role="tool",
                            content=_budget_tool_result(call.name),
                            tool_call_id=call.call_id,
                        )
                        messages.append(budget_message)
                        intermediate_messages.append(budget_message)
                        if on_intermediate is not None:
                            on_intermediate(tuple(intermediate_messages[batch_start:]))
                        return build_result(
                            content="",
                            iterations=iteration,
                            finish_reason="task_budget_tool_calls",
                            context_tokens=response.input_tokens,
                            error_code="task_budget_tool_calls",
                        )
                    attempted_tool_fingerprints.add(fingerprint)
                    tool_message, approval_id, executed_result = await self._execute_tool(
                        call,
                        tool_context,
                        on_event,
                        prepared,
                    )
                    executed_tool_calls += 1
                    batch_progressed = batch_progressed or _tool_succeeded(tool_message)
                if (
                    executed_result is not None
                    and not executed_result.ok
                    and executed_result.error_code == "automation_halted"
                    and tool_context is not None
                    and tool_context.source == "automation"
                ):
                    messages.append(tool_message)
                    intermediate_messages.append(tool_message)
                    if on_intermediate is not None:
                        on_intermediate(tuple(intermediate_messages[batch_start:]))
                    return build_result(
                        content="",
                        iterations=iteration,
                        finish_reason="automation_halted",
                        context_tokens=response.input_tokens,
                        error_code="automation_halted",
                    )
                if approval_id is not None:
                    if on_intermediate is not None:
                        on_intermediate(tuple(intermediate_messages[batch_start:]))
                    return build_result(
                        content=f"Approval {approval_id} required for {call.name}.",
                        iterations=iteration,
                        finish_reason="approval_required",
                        context_tokens=response.input_tokens,
                        status=AgentRunStatus.WAITING_APPROVAL,
                        approval_id=approval_id,
                    )
                messages.append(tool_message)
                intermediate_messages.append(tool_message)
                terminal_response = _terminal_response(
                    call.name,
                    executed_result,
                    tool_context,
                )
                if terminal_response is not None:
                    if on_intermediate is not None:
                        on_intermediate(tuple(intermediate_messages[batch_start:]))
                    return build_result(
                        content=terminal_response.text,
                        iterations=iteration,
                        finish_reason="complete_task",
                        context_tokens=response.input_tokens,
                        terminal_response=terminal_response,
                    )
            if on_intermediate is not None:
                on_intermediate(tuple(intermediate_messages[batch_start:]))
            previous_batch_progressed = batch_progressed
            if batch_progressed:
                consecutive_no_progress = 0
            else:
                consecutive_no_progress += 1
                if consecutive_no_progress >= self._max_no_progress_iterations:
                    raise AgentNoProgressError(
                        no_progress_iterations=consecutive_no_progress,
                        model_iteration=iteration,
                    )

        raise AgentLoopLimitError("agent reached an unexpected loop state")

    async def _execute_tool(
        self,
        call: ToolCall,
        context: ToolContext | None,
        on_event: RunEventHandler | None,
        prepared: PreparedToolCall | None = None,
    ) -> tuple[ModelMessage, int | None, ToolResult | None]:
        """执行已注册工具，或构造确定性未注册 Tool Result。"""
        if self._executor is None:
            result = json.dumps(
                {"ok": False, "error": "tool_not_found", "tool": call.name},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            assert context is not None
            assert prepared is not None
            outcome = await self._executor.execute_prepared(
                context,
                prepared,
                on_event=on_event,
            )
            result = outcome.model_text
            if outcome.approval_id is not None:
                return (
                    ModelMessage(role="tool", content=result, tool_call_id=call.call_id),
                    outcome.approval_id,
                    None,
                )
            executed_result = outcome.result
        if self._executor is None:
            executed_result = None
        return (
            ModelMessage(role="tool", content=result, tool_call_id=call.call_id),
            None,
            executed_result,
        )


def _assistant_tool_message(response: ModelResponse) -> ModelMessage:
    """把包含 Tool Call 的响应转换成下一请求必须原样回传的 Assistant 消息。"""
    return ModelMessage(
        role="assistant",
        content=response.content,
        tool_calls=response.tool_calls,
        reasoning_content=response.reasoning_content,
    )


def _tool_fingerprint(call: ToolCall) -> str:
    """按 Tool 名称和规范化参数生成跨 call ID 的稳定调用指纹。"""
    return json.dumps(
        {"name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _tool_succeeded(message: ModelMessage) -> bool:
    """判断 Tool Message 是否携带结构化成功结果。"""
    try:
        payload = json.loads(message.content)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def _budget_preflight_error(
    budget: AgentRunBudget | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cost_microusd: int,
    input_usage_complete: bool,
    output_usage_complete: bool,
    cost_usage_complete: bool,
    has_prior_response: bool,
) -> str | None:
    """在下一次 Provider 请求前判断已知预算是否已经耗尽。"""
    if budget is None or not has_prior_response:
        return None
    if input_usage_complete and input_tokens >= budget.max_input_tokens:
        return "task_budget_input_tokens"
    if output_usage_complete and output_tokens >= budget.max_output_tokens:
        return "task_budget_output_tokens"
    if (
        budget.max_cost_microusd is not None
        and cost_usage_complete
        and cost_microusd >= budget.max_cost_microusd
    ):
        return "task_budget_cost"
    return None


def _provider_output_limit(
    requested_limit: int | None,
    budget: AgentRunBudget | None,
    output_tokens: int,
    output_usage_complete: bool,
) -> int | None:
    """收紧本轮 Provider max_output_tokens，绝不扩大原请求限制。"""
    if budget is None:
        return requested_limit
    remaining = budget.max_output_tokens
    if output_usage_complete:
        remaining = max(1, remaining - output_tokens)
    if requested_limit is None:
        return remaining
    return min(requested_limit, remaining)


def _budget_response_error(
    budget: AgentRunBudget | None,
    response: ModelResponse,
    *,
    input_tokens: int,
    output_tokens: int,
    cost_microusd: int,
    input_usage_complete: bool,
    output_usage_complete: bool,
    cost_usage_complete: bool,
) -> str | None:
    """在任何 Tool 副作用前检查本轮回报用量和正文 UTF-8 上限。"""
    if budget is None:
        return None
    if len(response.content.encode("utf-8")) > _MAX_AUTOMATION_OUTPUT_BYTES:
        return "task_budget_output_tokens"
    if input_usage_complete and input_tokens > budget.max_input_tokens:
        return "task_budget_input_tokens"
    if output_usage_complete and output_tokens > budget.max_output_tokens:
        return "task_budget_output_tokens"
    if (
        budget.max_cost_microusd is not None
        and cost_usage_complete
        and cost_microusd > budget.max_cost_microusd
    ):
        return "task_budget_cost"
    return None


def _budget_tool_result(tool_name: str) -> str:
    """为未执行的超预算 Tool Call 生成稳定协议结果。"""
    return json.dumps(
        {
            "error": {
                "code": "task_budget_tool_calls",
                "message": "automation tool call budget exhausted",
                "retryable": False,
            },
            "ok": False,
            "tool": tool_name,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _terminal_response(
    tool_name: str,
    result: ToolResult | None,
    context: ToolContext | None,
) -> TaskResponse | None:
    """只从 automation complete_task 的原始成功 ToolResult 构造终态。"""
    if (
        tool_name != "complete_task"
        or result is None
        or not result.ok
        or context is None
        or context.source != "automation"
        or context.task_run_id is None
    ):
        return None
    data = result.data
    if not isinstance(data, dict) or set(data) != {"notify", "text"}:
        raise AgentError("complete_task returned invalid terminal data")
    try:
        return TaskResponse(notify=data["notify"], text=data["text"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentError("complete_task returned invalid terminal data") from exc
