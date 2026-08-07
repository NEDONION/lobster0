"""Policy → Tool → SQLite 唯一执行入口测试。"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths
from miniclaw.policy.engine import PolicyEngine
from miniclaw.providers.base import JsonValue, ToolCall
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ApprovalRepository, ToolRunRepository
from miniclaw.tools.base import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.filesystem import WriteFileTool
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

    def executor(
        self,
        tool: Tool,
        *,
        result_max_chars: int = 20_000,
        approvals: ApprovalRepository | None = None,
    ) -> ToolExecutor:
        """使用真实 Registry、Policy 与 Repository 创建执行器。"""
        if approvals is None:
            return ToolExecutor(
                ToolRegistry((tool,)),
                PolicyEngine(),
                ToolRunRepository(self.database),
                result_max_chars=result_max_chars,
            )
        return ToolExecutor(
            ToolRegistry((tool,)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            result_max_chars=result_max_chars,
            approvals=approvals,
            approval_ttl_seconds=600,
        )

    async def test_low_risk_tool_executes_and_persists_succeeded_run(self) -> None:
        """low-risk Tool 必须记录 running/succeeded 审计轨迹。"""
        call = ToolCall("call_1", "echo", {"text": "hello"})

        model_text = (await self.executor(_EchoTool()).execute(self.context, call)).model_text

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
        result = (
            await self.executor(_BrokenTool()).execute(
                self.context,
                ToolCall("call_broken", "broken", {}),
            )
        ).model_text

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
        result = (
            await self.executor(_EchoTool(), result_max_chars=200).execute(
                self.context,
                ToolCall("call_large", "echo", {"text": "x" * 1000}),
            )
        ).model_text

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
                result = (await executor.execute(self.context, call)).model_text
                self.assertEqual(json.loads(result)["error"]["code"], expected_code)

        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(count, 0)

    async def test_invalid_tool_result_is_redacted_and_marks_run_failed(self) -> None:
        """ToolResult 编码失败也必须收口，不能留下 running ToolRun。"""
        result = (
            await self.executor(_InvalidResultTool()).execute(
                self.context,
                ToolCall("call_invalid_result", "invalid_result", {}),
            )
        ).model_text

        self.assertEqual(json.loads(result)["error"]["code"], "tool_failed")
        self.assertNotIn("NaN", result)
        with self.database.connect_read_only() as connection:
            status = connection.execute("SELECT status FROM tool_runs").fetchone()[0]
        self.assertEqual(status, "failed")

    async def test_workspace_policy_allows_safe_read_tool_before_starting_run(self) -> None:
        """合法 Workspace 路径必须通过预检并正常创建 ToolRun。"""
        result = (
            await self.executor(_WorkspaceReadTool("read_file", "path")).execute(
                self.context,
                ToolCall("call_read", "read_file", {"path": "notes.txt"}),
            )
        ).model_text

        self.assertTrue(json.loads(result)["ok"])
        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(count, 1)

    async def test_workspace_policy_denies_unsafe_read_tools_before_starting_runs(self) -> None:
        """两个 Policy 拒绝必须留下脱敏审计，但不能创建 ToolRun。"""
        secret = "TOP-SECRET-CONTENT"
        sensitive = self.context.workspace / ".env"
        sensitive.write_text(secret, encoding="utf-8")
        outside = self.context.workspace.parent / "outside-private.txt"
        cases = (
            (
                _WorkspaceReadTool("read_file", "path"),
                ToolCall("escape", "read_file", {"path": str(outside)}),
                "workspace_escape",
            ),
            (
                _WorkspaceReadTool("glob", "root"),
                ToolCall("glob_sensitive", "glob", {"root": ".env"}),
                "sensitive_path",
            ),
        )
        for tool, call, expected_code in cases:
            with self.subTest(tool=call.name):
                result = (await self.executor(tool).execute(self.context, call)).model_text
                self.assertEqual(json.loads(result)["error"]["code"], expected_code)

        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
            events = connection.execute(
                """
                SELECT event_type, user_id, session_id, turn_id, summary, metadata_json
                FROM audit_events ORDER BY id
                """
            ).fetchall()
        self.assertEqual(count, 0)
        self.assertEqual([event["event_type"] for event in events], ["tool.denied"] * 2)
        self.assertEqual(
            [(event["user_id"], event["session_id"], event["turn_id"]) for event in events],
            [(self.context.user_id, self.context.session_id, self.context.turn_id)] * 2,
        )
        metadata = [json.loads(event["metadata_json"]) for event in events]
        self.assertEqual([item["tool_name"] for item in metadata], ["read_file", "glob"])
        self.assertEqual(
            [item["error_code"] for item in metadata],
            ["workspace_escape", "sensitive_path"],
        )
        for item in metadata:
            self.assertRegex(item["arguments_hash"], r"^[0-9a-f]{12}$")
            self.assertNotIn("tool_run_id", item)
        persisted = "".join(event["summary"] + event["metadata_json"] for event in events)
        for private_value in (str(outside), ".env", secret):
            self.assertNotIn(private_value, persisted)

    async def test_workspace_policy_hard_denies_sensitive_write_before_approval(self) -> None:
        """敏感写路径必须硬拒绝并审计，不能创建可批准的动作。"""
        result = (
            await self.executor(WriteFileTool()).execute(
                self.context,
                ToolCall(
                    "write_secret",
                    "write_file",
                    {"path": ".env", "content": "SECRET=value"},
                ),
            )
        ).model_text

        self.assertEqual(json.loads(result)["error"]["code"], "sensitive_path")
        with self.database.connect_read_only() as connection:
            run_count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
            event = connection.execute(
                "SELECT event_type, metadata_json FROM audit_events"
            ).fetchone()
        self.assertEqual(run_count, 0)
        self.assertEqual(event["event_type"], "tool.denied")
        self.assertEqual(json.loads(event["metadata_json"])["error_code"], "sensitive_path")
        self.assertNotIn("SECRET=value", event["metadata_json"])

    async def test_medium_write_creates_bound_approval_without_touching_file(self) -> None:
        """安全写请求必须变成带 ID 的 waiting Approval，不能提前执行 Tool。"""
        target = self.context.workspace / "approved-later.txt"
        executor = self.executor(
            WriteFileTool(),
            approvals=ApprovalRepository(self.database),
        )

        outcome = await executor.execute(
            self.context,
            ToolCall(
                "write_later",
                "write_file",
                {"path": "approved-later.txt", "content": "private-content"},
            ),
        )

        self.assertIsNotNone(outcome.approval_id)
        self.assertEqual(
            json.loads(outcome.model_text)["error"]["code"],
            "approval_required",
        )
        self.assertFalse(target.exists())
        self.assertNotIn("private-content", outcome.model_text)
        self.assertNotIn(str(self.paths.home), outcome.model_text)
        with self.database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT tr.status AS run_status, tr.arguments_json,
                       a.id AS approval_id, a.status AS approval_status
                FROM tool_runs tr JOIN approvals a ON a.tool_run_id = tr.id
                """
            ).fetchone()
        self.assertEqual(
            (row["run_status"], row["approval_status"]),
            ("waiting_approval", "pending"),
        )
        self.assertEqual(row["approval_id"], outcome.approval_id)
        self.assertEqual(json.loads(row["arguments_json"])["path"], str(target))

    async def test_policy_deny_fails_closed_when_audit_write_fails(self) -> None:
        """拒绝审计无法落库时不能返回一个伪装正常的 Policy 结果。"""
        executor = self.executor(_WorkspaceReadTool("read_file", "path"))
        with (
            patch.object(
                ToolRunRepository,
                "deny",
                side_effect=sqlite3.OperationalError("audit unavailable"),
                create=True,
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            await executor.execute(
                self.context,
                ToolCall("escape", "read_file", {"path": "../outside.txt"}),
            )

        with self.database.connect_read_only() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0], 0)

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
                result = (
                    await self.executor(_WorkspaceReadTool("read_file", "path")).execute(
                        self.context,
                        call,
                    )
                ).model_text
                self.assertEqual(json.loads(result)["error"]["code"], "workspace_escape")
                self.assertNotIn(str(self.paths.home), result)

        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
