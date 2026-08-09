"""执行有上限的模型与顺序 Tool Call 循环。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from miniclaw.agent.events import RunEvent, RunEventHandler, emit
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
from miniclaw.tools.base import ToolContext

if TYPE_CHECKING:
    from miniclaw.tools.executor import ToolExecutor


class AgentError(RuntimeError):
    """表示 Provider 之外的稳定 Agent Loop 失败。"""


class EmptyModelResponseError(AgentError):
    """表示模型没有 Tool Call，也没有可保存的最终文本。"""


class AgentLoopLimitError(AgentError):
    """表示模型在允许的最后一轮仍继续请求工具。"""


class AgentRunStatus(StrEnum):
    """区分最终回答与等待人工确认的正常业务结果。"""

    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting_approval"


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


class AgentRunner:
    """通过一个 Provider 顺序执行有限 Tool Call 并返回最终回答。"""

    def __init__(
        self,
        provider: ModelProvider,
        executor: ToolExecutor | None = None,
        *,
        max_iterations: int = 8,
        hard_max_iterations: int = 64,
        max_no_progress_iterations: int = 3,
    ) -> None:
        """绑定 Provider、当前可用工具和严格正数循环上限。

        Args:
            provider: 实际或 Fake 模型边界。
            executor: 可选的唯一安全 Tool 执行入口。
            max_iterations: 包含最终响应在内的最多模型调用次数。
            hard_max_iterations: 任何自适应策略都不能超过的循环硬上限。
            max_no_progress_iterations: 允许连续无进展 Tool 循环的次数上限。

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
        """返回当前执行器公开给模型的 Tool Schema。"""
        return () if self._executor is None else self._executor.schemas

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
    ) -> AgentRunResult:
        """执行模型与工具循环，直到获得非空最终回答。

        Args:
            request: 初始上下文、Tool Schema 与生成预算。
            on_text: 可选的最终可见文本流回调，由 Provider 施加背压。
            tool_context: 当前 Tool 不可由模型伪造的运行边界。
            on_intermediate: 每批 Tool Call/Result 完成后的同步持久化回调。

        Returns:
            最终回答、实际模型调用轮数与累计 Token 用量。

        Raises:
            EmptyModelResponseError: 最终响应没有文本也没有工具调用。
            AgentLoopLimitError: 最后一轮仍请求工具。
            ProviderError: Provider 调用失败，保持原具体类型。
            asyncio.CancelledError: 调用方取消，Runner 不拦截。
        """
        messages = list(request.messages)
        intermediate_messages: list[ModelMessage] = []
        input_tokens = 0
        output_tokens = 0
        input_usage_complete = True
        output_usage_complete = True
        provider_request_id: str | None = None
        seen_tool_call_ids: set[str] = set()
        round_chunks: list[str] = []

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

        for iteration in range(1, self._max_iterations + 1):
            current = replace(request, messages=tuple(messages))
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

            if not response.tool_calls:
                if not response.content.strip():
                    raise EmptyModelResponseError("model returned an empty final response")
                if on_text is not None:
                    for chunk in round_chunks:
                        await on_text(chunk)
                return AgentRunResult(
                    content=response.content,
                    iterations=iteration,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    provider_request_id=provider_request_id,
                    finish_reason=response.finish_reason,
                    context_tokens=response.input_tokens,
                    reported_input_tokens=(
                        input_tokens if input_usage_complete else None
                    ),
                    reported_output_tokens=(
                        output_tokens if output_usage_complete else None
                    ),
                    tool_calls_count=len(seen_tool_call_ids),
                    intermediate_messages=tuple(intermediate_messages),
                )
            if iteration == self._max_iterations:
                raise AgentLoopLimitError(
                    f"agent reached the model iteration limit ({self._max_iterations})"
                )

            if self._executor is not None and tool_context is None:
                raise AgentError("tool context is required")

            assistant_message = _assistant_tool_message(response)
            batch_start = len(intermediate_messages)
            messages.append(assistant_message)
            intermediate_messages.append(assistant_message)
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
                tool_message, approval_id = await self._execute_tool(
                    call,
                    tool_context,
                    on_event,
                )
                if approval_id is not None:
                    if on_intermediate is not None:
                        on_intermediate(tuple(intermediate_messages[batch_start:]))
                    return AgentRunResult(
                        content=f"Approval {approval_id} required for {call.name}.",
                        iterations=iteration,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        provider_request_id=provider_request_id,
                        finish_reason="approval_required",
                        context_tokens=response.input_tokens,
                        reported_input_tokens=(
                            input_tokens if input_usage_complete else None
                        ),
                        reported_output_tokens=(
                            output_tokens if output_usage_complete else None
                        ),
                        tool_calls_count=len(seen_tool_call_ids),
                        status=AgentRunStatus.WAITING_APPROVAL,
                        approval_id=approval_id,
                        intermediate_messages=tuple(intermediate_messages),
                    )
                messages.append(tool_message)
                intermediate_messages.append(tool_message)
            if on_intermediate is not None:
                on_intermediate(tuple(intermediate_messages[batch_start:]))

        raise AgentLoopLimitError("agent reached an unexpected loop state")

    async def _execute_tool(
        self,
        call: ToolCall,
        context: ToolContext | None,
        on_event: RunEventHandler | None,
    ) -> tuple[ModelMessage, int | None]:
        """执行已注册工具，或构造确定性未注册 Tool Result。"""
        if self._executor is None:
            result = json.dumps(
                {"ok": False, "error": "tool_not_found", "tool": call.name},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            assert context is not None
            outcome = await self._executor.execute(context, call, on_event=on_event)
            result = outcome.model_text
            if outcome.approval_id is not None:
                return (
                    ModelMessage(role="tool", content=result, tool_call_id=call.call_id),
                    outcome.approval_id,
                )
        return ModelMessage(role="tool", content=result, tool_call_id=call.call_id), None


def _assistant_tool_message(response: ModelResponse) -> ModelMessage:
    """把包含 Tool Call 的响应转换成下一请求必须原样回传的 Assistant 消息。"""
    return ModelMessage(
        role="assistant",
        content=response.content,
        tool_calls=response.tool_calls,
        reasoning_content=response.reasoning_content,
    )
