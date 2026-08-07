"""AgentRunner 模型与工具循环的边界行为测试。"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from miniclaw.agent.runner import (
    AgentError,
    AgentLoopLimitError,
    AgentRunner,
    EmptyModelResponseError,
)
from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths
from miniclaw.policy.engine import PolicyEngine
from miniclaw.providers.base import JsonValue, ModelMessage, ModelRequest, ModelResponse, ToolCall
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ToolRunRepository
from miniclaw.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.registry import ToolRegistry
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


class _EchoTool:
    """记录执行次数并返回输入文本的 Runner 测试 Tool。"""

    definition = ToolDefinition(
        name="echo",
        description="Echo text.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self) -> None:
        self.executions = 0

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受单个字符串 text。"""
        if set(arguments) != {"text"} or not isinstance(arguments["text"], str):
            raise ToolValidationError("text must be a string")
        return arguments

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """记录并返回参数。"""
        del context
        self.executions += 1
        return ToolResult.success({"text": arguments["text"]})


class AgentRunnerTest(unittest.IsolatedAsyncioTestCase):
    """验证 Runner 只编排模型和工具，不触碰 Channel 或 Storage。"""

    def setUp(self) -> None:
        """创建 ToolExecutor 所需的真实临时 Turn。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(self.paths)
        self.database = Database(self.paths.database)
        session = SessionRepository(self.database).get_or_create_cli(
            initialized.owner.id,
            "runner-test",
        )
        turns = TurnRepository(self.database)
        turn = turns.create_with_user_message(
            session.id,
            "runner-event",
            "test-model",
            "hello",
        )
        turns.mark_running(turn.id)
        self.tool_context = ToolContext(
            initialized.owner.id,
            session.id,
            turn.id,
            self.paths.home,
            self.paths.workspace,
            (),
        )

    def executor(self, tool: _EchoTool) -> ToolExecutor:
        """创建真实安全执行入口。"""
        return ToolExecutor(
            ToolRegistry((tool,)),
            PolicyEngine(),
            ToolRunRepository(self.database),
        )

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
        tool = _EchoTool()
        executor = self.executor(tool)

        result = await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            tool_context=self.tool_context,
        )

        self.assertEqual(result.content, "done")
        self.assertEqual(result.iterations, 2)
        self.assertEqual((result.input_tokens, result.output_tokens), (16, 6))
        self.assertEqual(tool.executions, 1)
        self.assertEqual(
            [(message.role, message.tool_call_id) for message in result.intermediate_messages],
            [("assistant", None), ("tool", "call_1")],
        )
        self.assertEqual(
            json.loads(result.intermediate_messages[-1].content)["data"],
            {"text": "hello"},
        )
        continued = provider.requests[1].messages
        self.assertEqual(continued[-2].role, "assistant")
        self.assertEqual(continued[-2].reasoning_content, "need echo")
        self.assertEqual(continued[-1].role, "tool")
        self.assertEqual(continued[-1].tool_call_id, "call_1")

    async def test_tool_call_requires_runtime_context(self) -> None:
        """有执行器但没有当前用户/Turn 边界时不能执行 Tool。"""
        call = ToolCall("call_1", "echo", {"text": "hello"})
        provider = FakeProvider((response("", tool_calls=(call,)),))
        executor = self.executor(_EchoTool())

        with self.assertRaises(AgentError):
            await AgentRunner(provider, executor).run(request(*executor.schemas))

    async def test_duplicate_tool_call_ids_in_one_batch_execute_nothing(self) -> None:
        """同一模型响应复用 call ID 时必须在任何 Tool 执行前拒绝。"""
        duplicate = (
            ToolCall("call_same", "echo", {"text": "first"}),
            ToolCall("call_same", "echo", {"text": "second"}),
        )
        provider = FakeProvider((response("", tool_calls=duplicate),))
        tool = _EchoTool()
        executor = self.executor(tool)

        with self.assertRaises(AgentError):
            await AgentRunner(provider, executor).run(
                request(*executor.schemas),
                tool_context=self.tool_context,
            )

        self.assertEqual(tool.executions, 0)
        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(count, 0)

    async def test_later_round_cannot_reuse_previous_tool_call_id(self) -> None:
        """后续模型轮次复用旧 call ID 时，不能再次执行同一动作。"""
        call = ToolCall("call_reused", "echo", {"text": "once"})
        provider = FakeProvider(
            (
                response("", tool_calls=(call,)),
                response("", tool_calls=(call,)),
            )
        )
        tool = _EchoTool()
        executor = self.executor(tool)

        with self.assertRaises(AgentError):
            await AgentRunner(provider, executor).run(
                request(*executor.schemas),
                tool_context=self.tool_context,
            )

        self.assertEqual(tool.executions, 1)
        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(count, 1)

    async def test_text_callback_only_receives_final_model_round(self) -> None:
        """Tool 前的草稿 content 不能被 Channel 当成最终答案发送。"""
        call = ToolCall("call_stream", "echo", {"text": "hello"})
        provider = FakeProvider(
            (
                response("checking", tool_calls=(call,)),
                response("done"),
            )
        )
        executor = self.executor(_EchoTool())
        visible: list[str] = []

        async def on_text(chunk: str) -> None:
            visible.append(chunk)

        await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            on_text,
            tool_context=self.tool_context,
        )

        self.assertEqual(visible, ["done"])

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
        provider = FakeProvider(
            tuple(
                response(
                    "",
                    tool_calls=(ToolCall(f"call_loop_{index}", "echo", {"text": "x"}),),
                )
                for index in range(8)
            )
        )
        tool = _EchoTool()
        executor = self.executor(tool)

        with self.assertRaises(AgentLoopLimitError):
            await AgentRunner(provider, executor, max_iterations=8).run(
                request(*executor.schemas),
                tool_context=self.tool_context,
            )

        self.assertEqual(len(provider.requests), 8)
        self.assertEqual(tool.executions, 7)

    async def test_cancellation_propagates_without_becoming_agent_failure(self) -> None:
        """Runner 不得吞掉 CancelledError，TurnService 需要据此保存 cancelled。"""
        provider = FakeProvider((asyncio.CancelledError(),))

        with self.assertRaises(asyncio.CancelledError):
            await AgentRunner(provider).run(request())


if __name__ == "__main__":
    unittest.main()
