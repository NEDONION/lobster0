"""执行有上限的模型与顺序 Tool Call 循环。"""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace

from miniclaw.providers.base import (
    JsonValue,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StreamHandler,
    ToolCall,
)

type ToolHandler = Callable[[dict[str, JsonValue]], Awaitable[str]]


class AgentError(RuntimeError):
    """表示 Provider 之外的稳定 Agent Loop 失败。"""


class EmptyModelResponseError(AgentError):
    """表示模型没有 Tool Call，也没有可保存的最终文本。"""


class AgentLoopLimitError(AgentError):
    """表示模型在允许的最后一轮仍继续请求工具。"""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """汇总一次 Agent Loop 的最终文本、轮数、用量和诊断 ID。"""

    content: str
    iterations: int
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None
    finish_reason: str


class AgentRunner:
    """通过一个 Provider 顺序执行有限 Tool Call 并返回最终回答。"""

    def __init__(
        self,
        provider: ModelProvider,
        tools: Mapping[str, ToolHandler] | None = None,
        *,
        max_iterations: int = 8,
    ) -> None:
        """绑定 Provider、当前可用工具和严格正数循环上限。

        Args:
            provider: 实际或 Fake 模型边界。
            tools: 工具名到异步执行函数的映射；Phase 1 默认空。
            max_iterations: 包含最终响应在内的最多模型调用次数。

        Raises:
            ValueError: 循环上限不是正整数。
        """
        if type(max_iterations) is not int or max_iterations <= 0:
            raise ValueError("max_iterations must be a positive integer")
        self._provider = provider
        self._tools = {} if tools is None else dict(tools)
        self._max_iterations = max_iterations

    async def run(
        self,
        request: ModelRequest,
        on_text: StreamHandler | None = None,
    ) -> AgentRunResult:
        """执行模型与工具循环，直到获得非空最终回答。

        Args:
            request: 初始上下文、Tool Schema 与生成预算。
            on_text: 可选的最终可见文本流回调，由 Provider 施加背压。

        Returns:
            最终回答、实际模型调用轮数与累计 Token 用量。

        Raises:
            EmptyModelResponseError: 最终响应没有文本也没有工具调用。
            AgentLoopLimitError: 最后一轮仍请求工具。
            ProviderError: Provider 调用失败，保持原具体类型。
            asyncio.CancelledError: 调用方取消，Runner 不拦截。
        """
        messages = list(request.messages)
        input_tokens = 0
        output_tokens = 0
        provider_request_id: str | None = None

        for iteration in range(1, self._max_iterations + 1):
            current = replace(request, messages=tuple(messages))
            response = await self._provider.complete(current, on_text)
            input_tokens += response.input_tokens or 0
            output_tokens += response.output_tokens or 0
            provider_request_id = response.provider_request_id or provider_request_id

            if not response.tool_calls:
                if not response.content.strip():
                    raise EmptyModelResponseError("model returned an empty final response")
                return AgentRunResult(
                    content=response.content,
                    iterations=iteration,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    provider_request_id=provider_request_id,
                    finish_reason=response.finish_reason,
                )
            if iteration == self._max_iterations:
                raise AgentLoopLimitError(
                    f"agent reached the model iteration limit ({self._max_iterations})"
                )

            messages.append(_assistant_tool_message(response))
            for call in response.tool_calls:
                messages.append(await self._execute_tool(call))

        raise AgentLoopLimitError("agent reached an unexpected loop state")

    async def _execute_tool(self, call: ToolCall) -> ModelMessage:
        """执行已注册工具，或构造确定性未注册 Tool Result。"""
        handler = self._tools.get(call.name)
        if handler is None:
            result = json.dumps(
                {"ok": False, "error": "tool_not_found", "tool": call.name},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            result = await handler(call.arguments)
        return ModelMessage(role="tool", content=result, tool_call_id=call.call_id)


def _assistant_tool_message(response: ModelResponse) -> ModelMessage:
    """把包含 Tool Call 的响应转换成下一请求必须原样回传的 Assistant 消息。"""
    return ModelMessage(
        role="assistant",
        content=response.content,
        tool_calls=response.tool_calls,
        reasoning_content=response.reasoning_content,
    )
