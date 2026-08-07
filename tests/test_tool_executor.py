"""Policy → Tool → SQLite 唯一执行入口测试。"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths
from miniclaw.policy.engine import PolicyEngine
from miniclaw.providers.base import JsonValue, ToolCall
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ToolRunRepository
from miniclaw.tools.base import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.registry import ToolRegistry


class _EchoTool:
    """返回输入文本的 low-risk 测试 Tool。"""

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
        """返回输入文本。"""
        del context
        return ToolResult.success({"text": arguments["text"]})


class _BrokenTool(_EchoTool):
    """抛出包含私密文本的测试 Tool。"""

    definition = ToolDefinition(
        name="broken",
        description="Fail.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.LOW,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受空参数。"""
        if arguments:
            raise ToolValidationError("arguments must be empty")
        return arguments

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """模拟不可向模型泄露的内部异常。"""
        del context, arguments
        raise RuntimeError("private-test-value")


class _CancelledTool(_BrokenTool):
    """模拟用户中断的 Tool。"""

    definition = ToolDefinition(
        name="cancel",
        description="Cancel.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.LOW,
    )

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """传播 asyncio 取消。"""
        del context, arguments
        raise asyncio.CancelledError


class _ApprovalTool(_BrokenTool):
    """模拟 P2.2 才允许执行的 medium-risk Tool。"""

    definition = ToolDefinition(
        name="approval",
        description="Needs approval.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.MEDIUM,
    )


class _InvalidResultTool(_BrokenTool):
    """返回 JSON 不允许的非有限浮点数。"""

    definition = ToolDefinition(
        name="invalid_result",
        description="Return an invalid result.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.LOW,
    )

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """模拟违反 ToolResult JSON 契约的插件。"""
        del context, arguments
        return ToolResult.success({"value": float("nan")})


class _WorkspaceReadTool:
    """模拟只读文件 Tool 的路径参数与成功执行。"""

    def __init__(self, name: str, path_argument: str) -> None:
        """按真实 Tool 名和路径参数名创建 low-risk 定义。"""
        self._path_argument = path_argument
        self.definition = ToolDefinition(
            name=name,
            description="Read from the workspace.",
            parameters={
                "type": "object",
                "properties": {path_argument: {"type": "string"}},
                "required": [path_argument],
                "additionalProperties": False,
            },
            risk=ToolRisk.LOW,
        )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受定义中的单个字符串路径参数。"""
        if set(arguments) != {self._path_argument} or not isinstance(
            arguments[self._path_argument], str
        ):
            raise ToolValidationError(f"{self._path_argument} must be a string")
        return arguments

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """返回成功，供测试观察是否越过 Policy 边界。"""
        del context
        return ToolResult.success({"path": arguments[self._path_argument]})


class ToolExecutorTest(unittest.IsolatedAsyncioTestCase):
    """验证 Tool 只能经过 Policy 和持久化执行入口。"""

    def setUp(self) -> None:
        """创建带真实外键记录的临时 SQLite 状态。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(self.paths)
        self.database = Database(self.paths.database)
        session = SessionRepository(self.database).get_or_create_cli(
            initialized.owner.id,
            "tool-executor-test",
        )
        turns = TurnRepository(self.database)
        turn = turns.create_with_user_message(
            session.id,
            "event-1",
            "test-model",
            "use a tool",
        )
        turns.mark_running(turn.id)
        self.context = ToolContext(
            user_id=initialized.owner.id,
            session_id=session.id,
            turn_id=turn.id,
            state_home=self.paths.home,
            workspace=self.paths.workspace,
            read_only_roots=(),
        )

    def executor(self, tool: Tool, *, result_max_chars: int = 20_000) -> ToolExecutor:
        """使用真实 Registry、Policy 与 Repository 创建执行器。"""
        return ToolExecutor(
            ToolRegistry((tool,)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            result_max_chars=result_max_chars,
        )

    async def test_low_risk_tool_executes_and_persists_succeeded_run(self) -> None:
        """low-risk Tool 必须记录 running/succeeded 审计轨迹。"""
        call = ToolCall("call_1", "echo", {"text": "hello"})

        model_text = await self.executor(_EchoTool()).execute(self.context, call)

        self.assertEqual(json.loads(model_text)["data"], {"text": "hello"})
        with self.database.connect_read_only() as connection:
            run = connection.execute("SELECT * FROM tool_runs").fetchone()
            events = connection.execute(
                "SELECT event_type FROM audit_events ORDER BY id"
            ).fetchall()
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["policy_action"], "allow")
        self.assertEqual(run["tool_name"], "echo")
        self.assertEqual([row[0] for row in events], ["tool.started", "tool.succeeded"])

    async def test_unexpected_tool_error_is_redacted_and_persisted(self) -> None:
        """内部异常只能变成稳定错误码，原始文本不得泄露。"""
        result = await self.executor(_BrokenTool()).execute(
            self.context,
            ToolCall("call_broken", "broken", {}),
        )

        self.assertEqual(json.loads(result)["error"]["code"], "tool_failed")
        self.assertNotIn("private-test-value", result)
        with self.database.connect_read_only() as connection:
            run = connection.execute("SELECT status FROM tool_runs").fetchone()
            events = connection.execute(
                "SELECT event_type FROM audit_events ORDER BY id"
            ).fetchall()
        self.assertEqual(run[0], "failed")
        self.assertEqual([row[0] for row in events], ["tool.started", "tool.failed"])

    async def test_cancel_marks_tool_run_interrupted_and_propagates(self) -> None:
        """取消必须持久化 interrupted，并继续向上抛出。"""
        with self.assertRaises(asyncio.CancelledError):
            await self.executor(_CancelledTool()).execute(
                self.context,
                ToolCall("call_cancel", "cancel", {}),
            )

        with self.database.connect_read_only() as connection:
            status = connection.execute("SELECT status FROM tool_runs").fetchone()[0]
        self.assertEqual(status, "interrupted")

    async def test_oversized_result_becomes_bounded_failure(self) -> None:
        """过大的 Tool 输出不能直接塞进模型上下文。"""
        result = await self.executor(_EchoTool(), result_max_chars=200).execute(
            self.context,
            ToolCall("call_large", "echo", {"text": "x" * 1000}),
        )

        self.assertLessEqual(len(result), 200)
        self.assertEqual(json.loads(result)["error"]["code"], "tool_result_too_large")
        with self.database.connect_read_only() as connection:
            status = connection.execute("SELECT status FROM tool_runs").fetchone()[0]
        self.assertEqual(status, "failed")

    async def test_invalid_unknown_and_unapproved_tools_do_not_start_runs(self) -> None:
        """校验失败、未知 Tool 和未审批动作都不能创建 running 记录。"""
        cases = (
            (self.executor(_EchoTool()), ToolCall("bad", "echo", {}), "invalid_arguments"),
            (self.executor(_EchoTool()), ToolCall("missing", "missing", {}), "tool_not_found"),
            (
                self.executor(_ApprovalTool()),
                ToolCall("approval", "approval", {}),
                "approval_required",
            ),
        )
        for executor, call, expected_code in cases:
            with self.subTest(call=call):
                result = await executor.execute(self.context, call)
                self.assertEqual(json.loads(result)["error"]["code"], expected_code)

        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(count, 0)

    async def test_invalid_tool_result_is_redacted_and_marks_run_failed(self) -> None:
        """ToolResult 编码失败也必须收口，不能留下 running ToolRun。"""
        result = await self.executor(_InvalidResultTool()).execute(
            self.context,
            ToolCall("call_invalid_result", "invalid_result", {}),
        )

        self.assertEqual(json.loads(result)["error"]["code"], "tool_failed")
        self.assertNotIn("NaN", result)
        with self.database.connect_read_only() as connection:
            status = connection.execute("SELECT status FROM tool_runs").fetchone()[0]
        self.assertEqual(status, "failed")

    async def test_workspace_policy_allows_safe_read_tool_before_starting_run(self) -> None:
        """合法 Workspace 路径必须通过预检并正常创建 ToolRun。"""
        result = await self.executor(_WorkspaceReadTool("read_file", "path")).execute(
            self.context,
            ToolCall("call_read", "read_file", {"path": "notes.txt"}),
        )

        self.assertTrue(json.loads(result)["ok"])
        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(count, 1)

    async def test_workspace_policy_denies_unsafe_read_tools_before_starting_runs(self) -> None:
        """文件 Tool 的逃逸和敏感根必须保留具体错误码且不创建 ToolRun。"""
        cases = (
            (
                _WorkspaceReadTool("read_file", "path"),
                ToolCall("escape", "read_file", {"path": "../outside.txt"}),
                "workspace_escape",
            ),
            (
                _WorkspaceReadTool("glob", "root"),
                ToolCall("glob_sensitive", "glob", {"root": ".env"}),
                "sensitive_path",
            ),
            (
                _WorkspaceReadTool("grep", "root"),
                ToolCall("grep_sensitive", "grep", {"root": ".ssh"}),
                "sensitive_path",
            ),
        )
        for tool, call, expected_code in cases:
            with self.subTest(tool=call.name):
                result = await self.executor(tool).execute(self.context, call)
                self.assertEqual(json.loads(result)["error"]["code"], expected_code)

        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(count, 0)

    async def test_workspace_resolution_errors_are_redacted_before_starting_runs(self) -> None:
        """路径解析异常必须返回稳定错误且不能创建或泄露 ToolRun。"""
        loop = self.context.workspace / "loop"
        loop.symlink_to(loop)
        calls = (
            ToolCall("nul_path", "read_file", {"path": "invalid" + chr(0) + "path"}),
            ToolCall("loop_path", "read_file", {"path": "loop/file.txt"}),
        )
        for call in calls:
            with self.subTest(call=call.call_id):
                result = await self.executor(_WorkspaceReadTool("read_file", "path")).execute(
                    self.context,
                    call,
                )
                self.assertEqual(json.loads(result)["error"]["code"], "workspace_escape")
                self.assertNotIn(str(self.paths.home), result)

        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
