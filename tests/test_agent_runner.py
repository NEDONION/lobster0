"""AgentRunner 模型与工具循环的边界行为测试。"""

import asyncio
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lobster0.agent.events import RunEvent
from lobster0.agent.runner import (
    AgentError,
    AgentLoopLimitError,
    AgentNoProgressError,
    AgentRunBudget,
    AgentRunner,
    AgentRunStatus,
    EmptyModelResponseError,
)
from lobster0.bootstrap import initialize_state
from lobster0.paths import build_state_paths
from lobster0.policy.command import SAFE_EXECUTABLE_PATH
from lobster0.policy.engine import PolicyEngine
from lobster0.providers.base import JsonValue, ModelMessage, ModelRequest, ModelResponse, ToolCall
from lobster0.storage.conversations import SessionRepository, TurnRepository
from lobster0.storage.database import Database
from lobster0.storage.tooling import ApprovalRepository, ToolRunRepository
from lobster0.tools.base import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)
from lobster0.tools.command import RunCommandTool
from lobster0.tools.executor import ToolExecutor
from lobster0.tools.filesystem import ReadFileTool, WriteFileTool
from lobster0.tools.registry import ToolRegistry
from lobster0.tools.task_completion import CompleteTaskTool
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

    def executor(self, tool: Tool) -> ToolExecutor:
        """创建真实安全执行入口。"""
        return ToolExecutor(
            ToolRegistry((tool,)),
            PolicyEngine(),
            ToolRunRepository(self.database),
        )

    def test_constructor_uses_adaptive_defaults_and_rejects_invalid_budgets(self) -> None:
        """Runner 默认预算固定，并拒绝不可能的 adaptive budget 组合。"""
        provider = FakeProvider(())
        runner = AgentRunner(provider)

        self.assertEqual(
            (
                runner._max_iterations,
                runner._hard_max_iterations,
                runner._max_no_progress_iterations,
            ),
            (32, 64, 3),
        )
        invalid_budgets = (
            ({"hard_max_iterations": 0}, "hard_max_iterations"),
            ({"max_no_progress_iterations": True}, "max_no_progress_iterations"),
            (
                {"max_iterations": 40, "hard_max_iterations": 32},
                "hard_max_iterations",
            ),
        )
        for kwargs, expected in invalid_budgets:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, expected):
                    AgentRunner(provider, **kwargs)

    async def test_complete_task_ends_automation_without_extra_provider_turn(self) -> None:
        """terminal Tool 成功后必须直接返回结构化结果，不再询问 Provider。"""
        call = ToolCall(
            "call_complete",
            "complete_task",
            {"notify": True, "text": "完成"},
        )
        provider = FakeProvider((response("", tool_calls=(call,)),))
        executor = self.executor(CompleteTaskTool())
        context = replace(
            self.tool_context,
            source="automation",
            task_run_id=7,
        )

        outcome = await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            tool_context=context,
            budget=AgentRunBudget(max_turns=2, max_tool_calls=2),
        )

        self.assertEqual(outcome.terminal_response.notify, True)
        self.assertEqual(outcome.terminal_response.text, "完成")
        self.assertEqual(outcome.content, "完成")
        self.assertEqual(len(provider.requests), 1)

    async def test_automation_budget_stops_before_next_tool_side_effect(self) -> None:
        """Tool 上限在下一次 executor 调用前检查，不能多执行一次副作用。"""
        calls = (
            ToolCall("call_1", "echo", {"text": "first"}),
            ToolCall("call_2", "echo", {"text": "second"}),
        )
        provider = FakeProvider((response("", tool_calls=calls),))
        tool = _EchoTool()
        executor = self.executor(tool)

        outcome = await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            tool_context=replace(
                self.tool_context,
                source="automation",
                task_run_id=8,
            ),
            budget=AgentRunBudget(max_turns=3, max_tool_calls=1),
        )

        self.assertEqual(outcome.error_code, "task_budget_tool_calls")
        self.assertEqual(tool.executions, 1)
        self.assertEqual(len(provider.requests), 1)

    async def test_e_stop_between_tool_calls_prevents_the_next_side_effect(self) -> None:
        """同批 Tool 之间也必须重查 durable E-stop，不能只在 Run 开始时检查。"""
        calls = (
            ToolCall("call_before_halt", "echo", {"text": "first"}),
            ToolCall("call_after_halt", "echo", {"text": "second"}),
        )
        provider = FakeProvider((response("", tool_calls=calls),))
        halted = False

        class HaltingEchoTool(_EchoTool):
            """首次执行成功后模拟另一个控制面立即拉起 E-stop。"""

            async def execute(
                self,
                context: ToolContext,
                arguments: dict[str, JsonValue],
            ) -> ToolResult:
                """执行一次 Echo，并在返回前关闭后续 Automation 副作用。"""
                nonlocal halted
                result = await super().execute(context, arguments)
                halted = True
                return result

        tool = HaltingEchoTool()
        executor = self.executor(tool)

        def gate() -> bool:
            """返回外部控制面当前是否仍允许 Automation。"""
            return not halted

        outcome = await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            tool_context=replace(
                self.tool_context,
                source="automation",
                task_run_id=10,
                allowed_tool_names=frozenset({"echo"}),
                automation_gate=gate,
            ),
            budget=AgentRunBudget(max_turns=3, max_tool_calls=2),
        )

        self.assertEqual(outcome.error_code, "automation_halted")
        self.assertEqual(tool.executions, 1)

    async def test_e_stop_preempts_semantic_duplicate_filter(self) -> None:
        """E-stop 必须先于语义去重收口，不能继续请求 Provider。"""
        calls = (
            ToolCall("call_before_halt", "echo", {"text": "same"}),
            ToolCall("call_after_halt", "echo", {"text": "same"}),
        )
        provider = FakeProvider(
            (
                response("", tool_calls=calls),
                response("must not continue"),
            )
        )
        halted = False

        class HaltingEchoTool(_EchoTool):
            """首次执行后拉起 E-stop，第二次请求与其语义相同。"""

            async def execute(
                self,
                context: ToolContext,
                arguments: dict[str, JsonValue],
            ) -> ToolResult:
                """执行一次后立即关闭后续 Automation。"""
                nonlocal halted
                result = await super().execute(context, arguments)
                halted = True
                return result

        tool = HaltingEchoTool()
        executor = self.executor(tool)

        outcome = await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            tool_context=replace(
                self.tool_context,
                source="automation",
                task_run_id=11,
                allowed_tool_names=frozenset({"echo"}),
                automation_gate=lambda: not halted,
            ),
            budget=AgentRunBudget(max_turns=3, max_tool_calls=2),
        )

        self.assertEqual(outcome.error_code, "automation_halted")
        self.assertEqual(tool.executions, 1)
        self.assertEqual(len(provider.requests), 1)

    async def test_semantic_duplicate_does_not_consume_real_tool_budget(self) -> None:
        """不会真实执行的 duplicate 不能被误报为 Tool budget 超限。"""
        calls = (
            ToolCall("call_first", "echo", {"text": "same"}),
            ToolCall("call_duplicate", "echo", {"text": "same"}),
        )
        provider = FakeProvider(
            (
                response("", tool_calls=calls),
                response("done"),
            )
        )
        tool = _EchoTool()
        executor = self.executor(tool)

        outcome = await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            tool_context=replace(
                self.tool_context,
                source="automation",
                task_run_id=12,
            ),
            budget=AgentRunBudget(max_turns=3, max_tool_calls=1),
        )

        self.assertIsNone(outcome.error_code)
        self.assertEqual(outcome.content, "done")
        self.assertEqual(tool.executions, 1)

    async def test_reported_usage_budget_stops_before_any_tool(self) -> None:
        """Provider 回报已超 Token 预算时，本轮 Tool 一次也不能执行。"""
        call = ToolCall("call_over", "echo", {"text": "must-not-run"})
        provider = FakeProvider(
            (response("", tool_calls=(call,), input_tokens=11, output_tokens=2),)
        )
        tool = _EchoTool()
        executor = self.executor(tool)

        outcome = await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            tool_context=replace(
                self.tool_context,
                source="automation",
                task_run_id=9,
            ),
            budget=AgentRunBudget(
                max_turns=3,
                max_tool_calls=2,
                max_input_tokens=10,
                max_output_tokens=10,
            ),
        )

        self.assertEqual(outcome.error_code, "task_budget_input_tokens")
        self.assertEqual(tool.executions, 0)

    async def test_cost_and_utf8_output_budget_fail_with_stable_codes(self) -> None:
        """已回报费用与未知 Token 时的正文 byte 上限都必须生效。"""
        costly = FakeProvider(
            (replace(response("done"), cost_microusd=101),)
        )
        cost_outcome = await AgentRunner(costly).run(
            request(),
            budget=AgentRunBudget(
                max_turns=1,
                max_tool_calls=1,
                max_cost_microusd=100,
            ),
        )
        oversized = FakeProvider(
            (response("x" * (256 * 1024 + 1), input_tokens=None, output_tokens=None),)
        )
        output_outcome = await AgentRunner(oversized).run(
            request(),
            budget=AgentRunBudget(max_turns=1, max_tool_calls=1),
        )

        self.assertEqual(cost_outcome.error_code, "task_budget_cost")
        self.assertEqual(output_outcome.error_code, "task_budget_output_tokens")

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
                response(
                    "done",
                    reasoning="answer with the observed result",
                    input_tokens=11,
                    output_tokens=4,
                ),
            )
        )
        tool = _EchoTool()
        executor = self.executor(tool)
        events: list[RunEvent] = []

        async def capture(event: RunEvent) -> None:
            events.append(event)

        result = await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            tool_context=self.tool_context,
            on_event=capture,
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
        visible = [
            event
            for event in events
            if event.kind in {"model_reasoning", "tool_requested"}
        ]
        self.assertEqual(
            [event.kind for event in visible],
            ["model_reasoning", "tool_requested", "model_reasoning"],
        )
        self.assertEqual(visible[0].data["text"], "need echo")
        self.assertEqual(visible[1].data["arguments"], {"text": "hello"})
        self.assertEqual(
            visible[2].data["text"],
            "answer with the observed result",
        )
        usage = [event.data for event in events if event.kind == "model_usage"]
        self.assertEqual(
            [
                {
                    key: item[key]
                    for key in (
                        "iteration",
                        "context_tokens",
                        "input_tokens",
                        "output_tokens",
                        "tool_calls",
                    )
                }
                for item in usage
            ],
            [
                {
                    "iteration": 1,
                    "context_tokens": 5,
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "tool_calls": 1,
                },
                {
                    "iteration": 2,
                    "context_tokens": 11,
                    "input_tokens": 16,
                    "output_tokens": 6,
                    "tool_calls": 1,
                },
            ],
        )

    async def test_usage_event_preserves_unknown_provider_counts(self) -> None:
        """Provider 缺失 usage 时必须显示未知，不能把它伪装成零。"""
        provider = FakeProvider(
            (response("done", input_tokens=None, output_tokens=None),)
        )
        events: list[RunEvent] = []

        async def capture(event: RunEvent) -> None:
            events.append(event)

        result = await AgentRunner(provider).run(
            request(),
            tool_context=self.tool_context,
            on_event=capture,
        )

        usage = next(event for event in events if event.kind == "model_usage")
        self.assertEqual((result.input_tokens, result.output_tokens), (0, 0))
        self.assertIsNone(usage.data["context_tokens"])
        self.assertIsNone(usage.data["input_tokens"])
        self.assertIsNone(usage.data["output_tokens"])
        self.assertEqual(usage.data["tool_calls"], 0)

    async def test_first_pending_call_ends_loop_and_skips_later_calls(self) -> None:
        """首个 waiting Approval 必须结束本轮，后续同批 Tool 不得执行。"""
        pending = ToolCall(
            "call_write",
            "write_file",
            {"path": "later.txt", "content": "not-yet"},
        )
        later = ToolCall("call_echo_later", "echo", {"text": "must-not-run"})
        provider = FakeProvider((response("", tool_calls=(pending, later)),))
        echo = _EchoTool()
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(), echo)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=ApprovalRepository(self.database),
        )
        persisted: list[tuple[ModelMessage, ...]] = []

        result = await AgentRunner(provider, executor).run(
            request(*executor.schemas),
            tool_context=self.tool_context,
            on_intermediate=persisted.append,
        )

        self.assertEqual(result.status, AgentRunStatus.WAITING_APPROVAL)
        self.assertIsNotNone(result.approval_id)
        self.assertIn(f"Approval {result.approval_id}", result.content)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(echo.executions, 0)
        self.assertFalse((self.paths.workspace / "later.txt").exists())
        self.assertEqual([message.role for message in persisted[0]], ["assistant"])
        with self.database.connect_read_only() as connection:
            statuses = connection.execute(
                "SELECT status FROM tool_runs ORDER BY id"
            ).fetchall()
        self.assertEqual([row[0] for row in statuses], ["waiting_approval"])

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

    async def test_successful_progress_extends_soft_budget_and_hard_round_has_no_tools(
        self,
    ) -> None:
        """新颖成功 Tool 可越过 soft budget，但 hard 轮必须强制无工具收口。"""
        calls = tuple(
            response(
                "",
                tool_calls=(ToolCall(f"call_{index}", "echo", {"text": str(index)}),),
            )
            for index in range(4)
        )
        provider = FakeProvider((*calls, response("wrapped")))
        executor = self.executor(_EchoTool())

        result = await AgentRunner(
            provider,
            executor,
            max_iterations=3,
            hard_max_iterations=5,
            max_no_progress_iterations=3,
        ).run(request(*executor.schemas), tool_context=self.tool_context)

        self.assertEqual(result.content, "wrapped")
        self.assertEqual(result.iterations, 5)
        self.assertEqual(provider.requests[-1].tools, ())
        self.assertEqual(provider.requests[-1].messages[-1].role, "system")
        self.assertIn("evidence", provider.requests[-1].messages[-1].content.lower())
        self.assertNotIn(
            provider.requests[-1].messages[-1],
            result.intermediate_messages,
        )

    async def test_failed_tool_does_not_extend_soft_budget(self) -> None:
        """没有新颖成功结果的上一批不能把带工具请求延长到 soft 边界。"""
        missing = ToolCall("call_missing", "missing", {})
        provider = FakeProvider(
            (
                response("", tool_calls=(missing,)),
                response("fallback without more tools"),
            )
        )

        result = await AgentRunner(
            provider,
            max_iterations=2,
            hard_max_iterations=4,
        ).run(request())

        self.assertEqual(result.content, "fallback without more tools")
        self.assertEqual(provider.requests[-1].tools, ())
        self.assertEqual(provider.requests[-1].messages[-1].role, "system")

    async def test_soft_finalization_rejects_tool_call_without_executing_it(self) -> None:
        """soft 收口轮仍请求 Tool 时必须停止，且最后请求不产生 Tool 副作用。"""
        provider = FakeProvider(
            (
                response(
                    "",
                    tool_calls=(ToolCall("call_missing", "missing", {}),),
                ),
                response(
                    "",
                    tool_calls=(ToolCall("call_final", "echo", {"text": "never"}),),
                ),
            )
        )
        tool = _EchoTool()
        executor = self.executor(tool)

        with self.assertRaises(AgentLoopLimitError):
            await AgentRunner(
                provider,
                executor,
                max_iterations=2,
                hard_max_iterations=4,
            ).run(
                request(*executor.schemas),
                tool_context=self.tool_context,
            )

        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[-1].tools, ())
        self.assertEqual(tool.executions, 0)

    async def test_three_repeated_tool_fingerprints_stop_without_reexecution(self) -> None:
        """相同 Tool 语义只能真实执行一次，连续三个重复模型轮次稳定停止。"""
        provider = FakeProvider(
            tuple(
                response(
                    "",
                    tool_calls=(
                        ToolCall(f"call_{index}", "echo", {"text": "same"}),
                    ),
                )
                for index in range(4)
            )
        )
        tool = _EchoTool()
        executor = self.executor(tool)
        events: list[RunEvent] = []

        async def capture(event: RunEvent) -> None:
            events.append(event)

        with self.assertRaises(AgentNoProgressError) as stopped:
            await AgentRunner(
                provider,
                executor,
                max_iterations=8,
                hard_max_iterations=12,
                max_no_progress_iterations=3,
            ).run(
                request(*executor.schemas),
                tool_context=self.tool_context,
                on_event=capture,
            )

        self.assertEqual(tool.executions, 1)
        self.assertEqual(len(provider.requests), 4)
        self.assertEqual(
            (
                stopped.exception.no_progress_iterations,
                stopped.exception.model_iteration,
            ),
            (3, 4),
        )
        duplicate_result = json.loads(provider.requests[2].messages[-1].content)
        self.assertEqual(
            duplicate_result,
            {"ok": False, "error": "duplicate_tool_call", "tool": "echo"},
        )
        requested = [event for event in events if event.kind == "tool_requested"]
        finished = [event for event in events if event.kind == "tool_finished"]
        self.assertEqual(len(requested), 4)
        self.assertEqual(len(finished), 4)
        self.assertEqual(
            [event.data["status"] for event in finished],
            ["succeeded", "failed", "failed", "failed"],
        )
        self.assertEqual(
            [event.data.get("error_code") for event in finished[1:]],
            ["duplicate_tool_call"] * 3,
        )

    async def test_omitted_and_explicit_defaults_share_prepared_fingerprint(self) -> None:
        """省略与显式默认参数必须只执行一次同一 prepared Tool 语义。"""
        target = self.paths.workspace / "defaults.txt"
        target.write_text("hello\n", encoding="utf-8")
        calls = (
            ToolCall("call_omitted", "read_file", {"path": "defaults.txt"}),
            ToolCall(
                "call_explicit",
                "read_file",
                {"path": "defaults.txt", "offset": 1, "limit": 200},
            ),
        )
        provider = FakeProvider(
            tuple(response("", tool_calls=(call,)) for call in calls)
        )
        executor = self.executor(ReadFileTool())

        with self.assertRaises(AgentNoProgressError):
            await AgentRunner(
                provider,
                executor,
                max_iterations=4,
                hard_max_iterations=6,
                max_no_progress_iterations=1,
            ).run(request(*executor.schemas), tool_context=self.tool_context)

        with self.database.connect_read_only() as connection:
            tool_runs = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(tool_runs, 1)

    async def test_equivalent_workspace_paths_share_prepared_fingerprint(self) -> None:
        """相对路径别名规范到同一 Workspace 目标后必须只执行一次。"""
        target = self.paths.workspace / "same-path.txt"
        target.write_text("hello\n", encoding="utf-8")
        calls = (
            ToolCall(
                "call_plain",
                "read_file",
                {"path": "same-path.txt", "offset": 1, "limit": 200},
            ),
            ToolCall(
                "call_alias",
                "read_file",
                {"path": "./same-path.txt", "offset": 1, "limit": 200},
            ),
        )
        provider = FakeProvider(
            tuple(response("", tool_calls=(call,)) for call in calls)
        )
        executor = self.executor(ReadFileTool())

        with self.assertRaises(AgentNoProgressError):
            await AgentRunner(
                provider,
                executor,
                max_iterations=4,
                hard_max_iterations=6,
                max_no_progress_iterations=1,
            ).run(request(*executor.schemas), tool_context=self.tool_context)

        with self.database.connect_read_only() as connection:
            tool_runs = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(tool_runs, 1)

    async def test_equivalent_command_programs_share_policy_normalized_fingerprint(
        self,
    ) -> None:
        """命令名与同一 resolved executable 必须只执行一次规范命令。"""
        resolved_program = shutil.which("pwd", path=SAFE_EXECUTABLE_PATH)
        if resolved_program is None:
            self.skipTest("pwd is unavailable in the fixed executable path")
        executor = ToolExecutor(
            ToolRegistry(
                (RunCommandTool(executable_path=SAFE_EXECUTABLE_PATH),)
            ),
            PolicyEngine(
                security="full",
                ask="off",
                executable_path=SAFE_EXECUTABLE_PATH,
            ),
            ToolRunRepository(self.database),
        )
        calls = (
            ToolCall("call_name", "run_command", {"program": "pwd", "args": []}),
            ToolCall(
                "call_resolved",
                "run_command",
                {
                    "program": resolved_program,
                    "args": [],
                    "timeout_seconds": 30,
                },
            ),
        )
        provider = FakeProvider(
            tuple(response("", tool_calls=(call,)) for call in calls)
        )

        with self.assertRaises(AgentNoProgressError):
            await AgentRunner(
                provider,
                executor,
                max_iterations=4,
                hard_max_iterations=6,
                max_no_progress_iterations=1,
            ).run(request(*executor.schemas), tool_context=self.tool_context)

        with self.database.connect_read_only() as connection:
            tool_runs = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(tool_runs, 1)

    async def test_eighth_tool_response_stops_before_executing_more_side_effects(self) -> None:
        """hard 收口轮即使仍返回 Tool Call，也不能执行无法继续回传的动作。"""
        provider = FakeProvider(
            tuple(
                response(
                    "",
                    tool_calls=(
                        ToolCall(f"call_loop_{index}", "echo", {"text": str(index)}),
                    ),
                )
                for index in range(8)
            )
        )
        tool = _EchoTool()
        executor = self.executor(tool)

        with self.assertRaises(AgentLoopLimitError):
            await AgentRunner(
                provider,
                executor,
                max_iterations=3,
                hard_max_iterations=8,
            ).run(
                request(*executor.schemas),
                tool_context=self.tool_context,
            )

        self.assertEqual(len(provider.requests), 8)
        self.assertEqual(tool.executions, 7)
        self.assertEqual(provider.requests[-1].tools, ())
        self.assertEqual(provider.requests[-1].messages[-1].role, "system")

    async def test_cancellation_propagates_without_becoming_agent_failure(self) -> None:
        """Runner 不得吞掉 CancelledError，TurnService 需要据此保存 cancelled。"""
        provider = FakeProvider((asyncio.CancelledError(),))

        with self.assertRaises(asyncio.CancelledError):
            await AgentRunner(provider).run(request())


if __name__ == "__main__":
    unittest.main()
