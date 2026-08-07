"""AgentRunner 模型与工具循环的边界行为测试。"""

import asyncio
import json
import unittest

from miniclaw.agent.runner import AgentLoopLimitError, AgentRunner, EmptyModelResponseError
from miniclaw.providers.base import ModelMessage, ModelRequest, ModelResponse, ToolCall
from tests.fakes.fake_provider import FakeProvider


def response(
    content: str,
    *,
    tool_calls: tuple[ToolCall, ...] = (),
    reasoning: str | None = None,
    input_tokens: int | None = 5,
    output_tokens: int | None = 2,
) -> ModelResponse:
    """创建字段完整、手工可预测的模型响应。"""
    return ModelResponse(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning,
        finish_reason="tool_calls" if tool_calls else "stop",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_request_id="req_test",
    )


def request(*tools: dict[str, object]) -> ModelRequest:
    """创建一个带可选 Tool Schema 的最小用户请求。"""
    return ModelRequest(
        model="deepseek-v4-pro",
        messages=(ModelMessage(role="user", content="hello"),),
        tools=tuple(tools),
    )


class AgentRunnerTest(unittest.IsolatedAsyncioTestCase):
    """验证 Runner 只编排模型和工具，不触碰 Channel 或 Storage。"""

    async def test_final_text_returns_usage_and_single_iteration(self) -> None:
        """无 Tool Call 的正常响应应一次结束并保留可观察用量。"""
        provider = FakeProvider((response("world", input_tokens=9, output_tokens=3),))

        result = await AgentRunner(provider).run(request())

        self.assertEqual(result.content, "world")
        self.assertEqual(result.iterations, 1)
        self.assertEqual((result.input_tokens, result.output_tokens), (9, 3))
        self.assertEqual(result.provider_request_id, "req_test")

    async def test_tool_result_continues_with_reasoning_and_aggregates_usage(self) -> None:
        """Tool 轮次必须回传 reasoning、调用与结果，并累计两次模型用量。"""
        call = ToolCall("call_1", "echo", {"text": "hello"})
        provider = FakeProvider(
            (
                response("checking", tool_calls=(call,), reasoning="need echo"),
                response("done", input_tokens=11, output_tokens=4),
            )
        )
        observed_arguments: list[dict[str, object]] = []

        async def echo(arguments: dict[str, object]) -> str:
            observed_arguments.append(arguments)
            return str(arguments["text"])

        result = await AgentRunner(provider, {"echo": echo}).run(
            request({"type": "function", "function": {"name": "echo"}})
        )

        self.assertEqual(result.content, "done")
        self.assertEqual(result.iterations, 2)
        self.assertEqual((result.input_tokens, result.output_tokens), (16, 6))
        self.assertEqual(observed_arguments, [{"text": "hello"}])
        continued = provider.requests[1].messages
        self.assertEqual(continued[-2].role, "assistant")
        self.assertEqual(continued[-2].reasoning_content, "need echo")
        self.assertEqual(continued[-1].role, "tool")
        self.assertEqual(continued[-1].tool_call_id, "call_1")

    async def test_unknown_tool_becomes_structured_result_then_model_can_finish(self) -> None:
        """未注册工具不能让进程崩溃，应作为确定性 Tool Result 回传模型。"""
        call = ToolCall("call_missing", "missing", {})
        provider = FakeProvider((response("", tool_calls=(call,)), response("fallback")))

        result = await AgentRunner(provider).run(request())

        tool_result = json.loads(provider.requests[1].messages[-1].content)
        self.assertEqual(result.content, "fallback")
        self.assertEqual(
            tool_result,
            {"ok": False, "error": "tool_not_found", "tool": "missing"},
        )

    async def test_empty_final_response_is_rejected(self) -> None:
        """没有 Tool Call 的空白最终内容不能保存成正常 Assistant Message。"""
        provider = FakeProvider((response("  "),))

        with self.assertRaises(EmptyModelResponseError):
            await AgentRunner(provider).run(request())

    async def test_eighth_tool_response_stops_before_executing_more_side_effects(self) -> None:
        """第八次仍请求工具时必须停止，且不能执行已经无法继续回传的最后动作。"""
        call = ToolCall("call_loop", "echo", {"text": "x"})
        provider = FakeProvider(tuple(response("", tool_calls=(call,)) for _ in range(8)))
        executions = 0

        async def echo(arguments: dict[str, object]) -> str:
            nonlocal executions
            executions += 1
            return "x"

        with self.assertRaises(AgentLoopLimitError):
            await AgentRunner(provider, {"echo": echo}, max_iterations=8).run(request())

        self.assertEqual(len(provider.requests), 8)
        self.assertEqual(executions, 7)

    async def test_cancellation_propagates_without_becoming_agent_failure(self) -> None:
        """Runner 不得吞掉 CancelledError，TurnService 需要据此保存 cancelled。"""
        provider = FakeProvider((asyncio.CancelledError(),))

        with self.assertRaises(asyncio.CancelledError):
            await AgentRunner(provider).run(request())


if __name__ == "__main__":
    unittest.main()
