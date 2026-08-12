"""执行有上限的模型与顺序 Tool Call 循环。"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from lobster0.agent.events import (
    RunEvent,
    RunEventHandler,
    display_tool_arguments,
    emit,
    tool_display_summary,
)
from lobster0.automation.models import TaskResponse
from lobster0.policy.approvals import ApprovalDecision
from lobster0.providers.base import (
    JsonValue,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StreamHandler,
    ToolCall,
)
from lobster0.storage.tooling import StoredToolRun
from lobster0.tools.base import ToolContext, ToolResult

if TYPE_CHECKING:
    from lobster0.tools.executor import PreparedToolCall, ToolExecutor

_MAX_AUTOMATION_OUTPUT_BYTES = 256 * 1024


# 模型偶尔会把工具调用的内部标记原样写进正文，而不是编码成结构化的
# ``tool_calls`` 字段。已实测到的一种是 DeepSeek 的 ``<｜｜DSML｜｜tool_calls>``
# （竖线是 U+FF5C 全角）；半角 ``<|tool_calls|>`` 是同族写法。此时既没有可执行的
# 工具调用，正文也不是给人看的答案——继续把它当最终回复保存，用户只会看到一串乱码。
_UNPARSED_TOOL_CALL_MARKUP = re.compile(
    r"<\s*/?\s*[|｜]{1,2}\s*(?:DSML\s*[|｜]{1,2}\s*)?(?:tool_calls|invoke)\b",
    re.IGNORECASE,
)


def looks_like_unparsed_tool_call(content: str) -> bool:
    """判断正文是否是未被解析的工具调用标记。

    只匹配"尖括号 + 竖线"这种模型专用标记，普通文本里提到 ``tool_calls``、
    甚至贴出 ``<invoke>`` XML 标签都不会命中。

    Args:
        content: 模型返回的最终正文。

    Returns:
        正文疑似工具调用标记时返回真。
    """
    return bool(_UNPARSED_TOOL_CALL_MARKUP.search(content))


class AgentError(RuntimeError):
    """表示 Provider 之外的稳定 Agent Loop 失败。"""


class EmptyModelResponseError(AgentError):
    """表示模型没有 Tool Call，也没有可保存的最终文本。"""


class UnparsedToolCallError(AgentError):
    """表示模型把工具调用标记写进了正文，没有产生可执行的工具调用。"""


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


class AgentTurnDeadlineError(AgentError):
    """表示单次 Turn 的墙钟预算已经耗尽。

    与 ``AgentNoProgressError`` 一样，这是一个**正常的停止**：抛出前该批次的所有
    Tool 结果都已经通过 ``on_intermediate`` 落库，Turn 由 ``TurnService`` 的同一个
    异常分支标成 ``failed``，不会出现半写状态。
    """

    def __init__(
        self,
        *,
        deadline_seconds: int,
        elapsed_seconds: float,
        model_iterations: int,
        activity: str,
    ) -> None:
        """保存停止时的预算、真实耗时、已完成轮数和当时正在做的事。

        Args:
            deadline_seconds: 生效的单轮墙钟预算。
            model_iterations: 已经完成的模型调用轮数。
            elapsed_seconds: 单调时钟测得的本轮真实耗时，统一保存为 float。
            activity: 超时前最后一次执行的 Tool 展示摘要，已脱敏。

        Raises:
            ValueError: 任一诊断字段不满足类型或取值约束。
        """
        if type(deadline_seconds) is not int or deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be a positive integer")
        if type(elapsed_seconds) not in {int, float} or elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be a non-negative number")
        if type(model_iterations) is not int or model_iterations < 0:
            raise ValueError("model_iterations must be a non-negative integer")
        if not isinstance(activity, str) or not activity:
            raise ValueError("activity must not be empty")
        self.deadline_seconds = deadline_seconds
        self.elapsed_seconds = float(elapsed_seconds)
        self.model_iterations = model_iterations
        self.activity = activity
        super().__init__(
            f"agent stopped after {elapsed_seconds:.1f}s, exceeding the "
            f"{deadline_seconds}s turn deadline after {model_iterations} model rounds "
            f"while running {activity}"
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
        max_iterations: int = 200,
        hard_max_iterations: int = 400,
        max_no_progress_iterations: int = 12,
        max_turn_seconds: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """绑定 Provider、当前可用工具和严格正数循环上限。

        Args:
            provider: 实际或 Fake 模型边界。
            executor: 可选的唯一安全 Tool 执行入口。
            max_iterations: 包含最终响应在内的最多模型调用次数，默认 200。到点时
                发一轮"不带工具、请给最终答案"的收口请求，不是硬性终止。
            hard_max_iterations: 任何自适应策略都不能超过的循环硬上限，默认 400。
                这是 ``run`` 一定会终止的**唯一结构性保证**，不接受"无限"。
            max_no_progress_iterations: 允许连续无进展 Tool 循环的次数上限，默认 12。
                只有真正成功的 Tool 结果才清零，因此计数单调递增、必然收敛；
                墙钟预算默认关闭后，这是卡死 Turn 的主要终止路径。
            max_turn_seconds: 单次 Turn 的墙钟预算秒数，默认 ``0`` 表示**不限时**。
                墙钟时间与"任务是否在正常推进"无关，按时间杀掉正常长任务是错的；
                运维想封顶时设正整数即可，机制本身完全保留。
            clock: 单调时钟来源；必须单调，默认 ``time.monotonic``，NTP 回拨不会
                让预算提前触发或永不触发。测试注入假时钟以避免真的 sleep。

        Raises:
            ValueError: 任一循环预算不是正整数、墙钟预算为负，或 hard 上限低于
                常规上限。
        """
        if type(max_iterations) is not int or max_iterations <= 0:
            raise ValueError("max_iterations must be a positive integer")
        if type(hard_max_iterations) is not int or hard_max_iterations <= 0:
            raise ValueError("hard_max_iterations must be a positive integer")
        if type(max_no_progress_iterations) is not int or max_no_progress_iterations <= 0:
            raise ValueError("max_no_progress_iterations must be a positive integer")
        if type(max_turn_seconds) is not int or max_turn_seconds < 0:
            raise ValueError("max_turn_seconds must be a non-negative integer")
        if not callable(clock):
            raise ValueError("clock must be callable")
        if hard_max_iterations < max_iterations:
            raise ValueError("hard_max_iterations must be greater than or equal to max_iterations")
        self._provider = provider
        self._executor = executor
        self._max_iterations = max_iterations
        self._hard_max_iterations = hard_max_iterations
        self._max_no_progress_iterations = max_no_progress_iterations
        self._max_turn_seconds = max_turn_seconds
        self._clock = clock

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
            AgentTurnDeadlineError: 本轮墙钟预算耗尽；只在工具边界抛出。
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
        turn_started = self._clock()
        last_activity = "model response"
        # 墙钟预算只覆盖没有其他墙钟主人的交互式 Turn。automation 传进来的
        # AgentRunBudget 已经由 automation/runner.py 的
        # ``asyncio.timeout(budget.timeout_seconds)``（默认 600s）在外层封顶，
        # 这里再叠加一层只会把后台任务悄悄砍短。
        # ``max_turn_seconds == 0`` 是默认值，表示 Owner 不希望长程任务被秒表打断；
        # 想停止应该由人发 /stop，见 channels/manager.py。
        deadline_active = budget is None and self._max_turn_seconds > 0

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
            # 第 1 轮 elapsed≈0，检查没有意义；从第 2 轮起，每次发起 Provider 请求前
            # 都在"没有任何未完成副作用"的时刻检查墙钟预算。
            if deadline_active and iteration > 1:
                overrun = self._deadline_overrun(turn_started)
                if overrun is not None:
                    raise AgentTurnDeadlineError(
                        deadline_seconds=self._max_turn_seconds,
                        elapsed_seconds=overrun,
                        model_iterations=iteration - 1,
                        activity=last_activity,
                    )
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
                if looks_like_unparsed_tool_call(response.content):
                    raise UnparsedToolCallError(
                        "model emitted tool-call markup as text instead of a tool call"
                    )
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
            for call_index, call in enumerate(response.tool_calls):
                # 一个响应可以带多条串行 Tool Call；只在迭代边界检查的话，一个批次
                # 就能把预算超出 N 倍单条工具超时。这里在执行下一条之前再检查一次，
                # 并给该批次剩余的每一条补一条确定性结果，保证存下来的历史里每个
                # tool_call_id 都恰好有一条对应的 tool 消息。
                batch_overrun = (
                    self._deadline_overrun(turn_started) if deadline_active else None
                )
                if batch_overrun is not None:
                    for skipped in response.tool_calls[call_index:]:
                        deadline_message = ModelMessage(
                            role="tool",
                            content=_deadline_tool_result(skipped.name),
                            tool_call_id=skipped.call_id,
                        )
                        messages.append(deadline_message)
                        intermediate_messages.append(deadline_message)
                    if on_intermediate is not None:
                        on_intermediate(tuple(intermediate_messages[batch_start:]))
                    raise AgentTurnDeadlineError(
                        deadline_seconds=self._max_turn_seconds,
                        elapsed_seconds=batch_overrun,
                        model_iterations=iteration,
                        activity=last_activity,
                    )
                if tool_context is not None:
                    await emit(
                        on_event,
                        RunEvent(
                            "tool_requested",
                            tool_context.turn_id,
                            {
                                "call_id": call.call_id,
                                "tool_name": call.name,
                                "summary": tool_display_summary(call.name, call.arguments),
                                "arguments": display_tool_arguments(call.name, call.arguments),
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
                    last_activity = tool_display_summary(call.name, call.arguments)
                    tool_message, approval_id, executed_result = await self._execute_tool(
                        call,
                        tool_context,
                        on_event,
                        prepared,
                    )
                    if control_stop and executed_result is None:
                        executed_result = prepared_result
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

    def _deadline_overrun(self, turn_started: float) -> float | None:
        """返回已经超出墙钟预算时的真实耗时，未超出时返回 ``None``。

        只读单调时钟，不做任何取消或副作用；调用方负责在安全边界上处理。
        """
        elapsed = self._clock() - turn_started
        if elapsed >= self._max_turn_seconds:
            return elapsed
        return None

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


def _deadline_tool_result(tool_name: str) -> str:
    """为墙钟预算耗尽而未执行的 Tool Call 生成稳定协议结果。"""
    return json.dumps(
        {
            "error": {
                "code": "turn_deadline",
                "message": "turn wall-clock budget exhausted",
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
