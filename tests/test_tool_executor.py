"""Policy → Tool → SQLite 唯一执行入口测试。"""

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths
from miniclaw.policy.approvals import ApprovalDecision
from miniclaw.policy.engine import PolicyEngine
from miniclaw.providers.base import JsonValue, ToolCall
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import (
    ApprovalRepository,
    PolicyRuleRepository,
    ToolRunRepository,
)
from miniclaw.tools.base import (
    Tool,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)
from miniclaw.tools.command import RunCommandTool
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.filesystem import WriteFileTool
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.web import HttpGetTool


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


class _DefaultingTool(_EchoTool):
    """补齐可选参数并记录 prepare/execute 次数的测试 Tool。"""

    definition = ToolDefinition(
        name="defaulting",
        description="Default a limit.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self) -> None:
        """初始化验证与执行计数。"""
        self.validations = 0
        self.executions = 0

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验文本并把省略的 limit 规范为 10。"""
        self.validations += 1
        if set(arguments) - {"text", "limit"} or not isinstance(
            arguments.get("text"), str
        ):
            raise ToolValidationError("text must be a string")
        limit = arguments.get("limit", 10)
        if type(limit) is not int:
            raise ToolValidationError("limit must be an integer")
        return {"text": arguments["text"], "limit": limit}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """记录执行并回显规范参数。"""
        del context
        self.executions += 1
        return ToolResult.success(arguments)


class _NestedArgumentsTool:
    """记录嵌套规范参数的 low-risk 测试 Tool。"""

    definition = ToolDefinition(
        name="nested",
        description="Record a nested target.",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "options": {"type": "object"},
            },
            "required": ["target", "options"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self) -> None:
        """初始化实际执行参数记录。"""
        self.executed_arguments: dict[str, JsonValue] | None = None

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验 target 与嵌套字符串 labels，并返回独立规范副本。"""
        target = arguments.get("target")
        options = arguments.get("options")
        if (
            set(arguments) != {"target", "options"}
            or not isinstance(target, str)
            or not isinstance(options, dict)
        ):
            raise ToolValidationError("target and options are required")
        labels = options.get("labels")
        if not isinstance(labels, list) or any(
            not isinstance(label, str) for label in labels
        ):
            raise ToolValidationError("options.labels must be strings")
        return {"target": target, "options": {"labels": list(labels)}}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """记录实际参数并返回成功。"""
        del context
        self.executed_arguments = deepcopy(arguments)
        return ToolResult.success(arguments)


class _BlockingTool(_EchoTool):
    """在副作用后等待放行，以复现并发重复 execute。"""

    definition = ToolDefinition(
        name="blocking",
        description="Block after one execution starts.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.LOW,
    )

    def __init__(self) -> None:
        """初始化执行计数和并发同步事件。"""
        self.executions = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受空参数。"""
        if arguments:
            raise ToolValidationError("arguments must be empty")
        return {}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """记录一次副作用，等待测试放行后返回。"""
        del context, arguments
        self.executions += 1
        self.started.set()
        await self.release.wait()
        return ToolResult.success({"executions": self.executions})


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

    async def test_prepared_call_is_normalized_once_and_executed_without_revalidation(
        self,
    ) -> None:
        """prepare 的规范参数必须原样进入执行，不能二次 validate 产生 TOCTOU。"""
        tool = _DefaultingTool()
        executor = self.executor(tool)

        prepared = executor.prepare(
            self.context,
            ToolCall("call_default", "defaulting", {"text": "hello"}),
        )
        outcome = await executor.execute_prepared(
            self.context,
            prepared,
        )

        self.assertEqual(
            prepared.call.arguments,
            {"text": "hello", "limit": 10},
        )
        self.assertEqual(json.loads(outcome.model_text)["data"]["limit"], 10)
        self.assertEqual((tool.validations, tool.executions), (1, 1))

    async def test_prepared_arguments_are_immutable_against_nested_external_mutation(
        self,
    ) -> None:
        """外部篡改 prepared 暴露的嵌套参数不能改变真实执行或审计目标。"""
        tool = _NestedArgumentsTool()
        executor = self.executor(tool)
        prepared = executor.prepare(
            self.context,
            ToolCall(
                "call_nested",
                "nested",
                {"target": "safe", "options": {"labels": ["approved"]}},
            ),
        )

        exposed = prepared.call.arguments
        exposed["target"] = "tampered"
        options = exposed["options"]
        assert isinstance(options, dict)
        labels = options["labels"]
        assert isinstance(labels, list)
        labels.append("bypass")
        outcome = await executor.execute_prepared(self.context, prepared)

        expected = {"target": "safe", "options": {"labels": ["approved"]}}
        self.assertEqual(prepared.call.arguments, expected)
        self.assertEqual(tool.executed_arguments, expected)
        self.assertEqual(json.loads(outcome.model_text)["data"], expected)
        with self.database.connect_read_only() as connection:
            stored = connection.execute(
                "SELECT arguments_json FROM tool_runs"
            ).fetchone()[0]
        self.assertEqual(json.loads(stored), expected)

    async def test_prepared_call_is_consumed_once_under_concurrent_execution(self) -> None:
        """并发 execute_prepared 只能一个进入 ToolRun 与真实副作用。"""
        tool = _BlockingTool()
        executor = self.executor(tool)
        prepared = executor.prepare(
            self.context,
            ToolCall("call_once", "blocking", {}),
        )

        first = asyncio.create_task(executor.execute_prepared(self.context, prepared))
        await tool.started.wait()
        second = asyncio.create_task(executor.execute_prepared(self.context, prepared))
        await asyncio.sleep(0)
        tool.release.set()
        first_result, second_result = await asyncio.gather(
            first,
            second,
            return_exceptions=True,
        )

        self.assertNotIsInstance(first_result, Exception)
        self.assertIsInstance(second_result, ValueError)
        self.assertIn("already been consumed", str(second_result))
        with self.database.connect_read_only() as connection:
            run_count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(tool.executions, 1)
        self.assertEqual(run_count, 1)

    async def test_prepared_approval_is_consumed_before_second_record_can_be_created(
        self,
    ) -> None:
        """同一 prepared Approval 只能创建一个 waiting ToolRun 与审批记录。"""
        approvals = ApprovalRepository(self.database)
        executor = self.executor(_ApprovalTool(), approvals=approvals)
        prepared = executor.prepare(
            self.context,
            ToolCall("call_approval_once", "approval", {}),
        )

        first = await executor.execute_prepared(self.context, prepared)
        with self.assertRaisesRegex(ValueError, "already been consumed"):
            await executor.execute_prepared(self.context, prepared)

        self.assertIsNotNone(first.approval_id)
        with self.database.connect_read_only() as connection:
            run_count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
            approval_count = connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        self.assertEqual((run_count, approval_count), (1, 1))

    async def test_prepared_call_rejects_other_executor_and_context_without_consuming(
        self,
    ) -> None:
        """错误执行器或 Context 必须先拒绝，且不能消费合法执行机会。"""
        owner = self.executor(_EchoTool())
        foreign = self.executor(_EchoTool())
        prepared = owner.prepare(
            self.context,
            ToolCall("call_bound", "echo", {"text": "hello"}),
        )
        other_context = replace(self.context)
        self.assertIsNot(other_context, self.context)

        with self.assertRaisesRegex(ValueError, "execution context"):
            await foreign.execute_prepared(self.context, prepared)
        with self.assertRaisesRegex(ValueError, "execution context"):
            await owner.execute_prepared(other_context, prepared)
        outcome = await owner.execute_prepared(self.context, prepared)

        self.assertEqual(json.loads(outcome.model_text)["data"], {"text": "hello"})
        with self.database.connect_read_only() as connection:
            run_count = connection.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
        self.assertEqual(run_count, 1)

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

    async def test_command_approval_summary_hides_long_and_sensitive_arguments(self) -> None:
        """命令审批只展示程序和参数数量，不能把长短凭据复制到卡片。"""
        secret = "PRIVATE-DOCUMENT-CONTENT-" * 40
        short_secret = "abc123token"
        approvals = ApprovalRepository(self.database)
        executor = self.executor(RunCommandTool(), approvals=approvals)

        outcome = await executor.execute(
            self.context,
            ToolCall(
                "compact-command",
                "run_command",
                {
                    "program": sys.executable,
                    "args": [
                        short_secret,
                        "+create",
                        "--content",
                        secret,
                        "--token",
                        "private-token",
                    ],
                    "timeout_seconds": 30,
                },
            ),
        )

        assert outcome.approval_id is not None
        summary = approvals.presentation(
            self.context.user_id,
            outcome.approval_id,
        ).approval.summary
        self.assertLessEqual(len(summary), 160)
        self.assertIn("run_command", summary)
        self.assertIn(Path(sys.executable).name, summary)
        self.assertIn("6 args", summary)
        self.assertNotIn(short_secret, summary)
        self.assertNotIn("+create", summary)
        self.assertNotIn(secret, summary)
        self.assertNotIn("private-token", summary)

    async def test_personal_external_write_requires_once_then_creates_file(self) -> None:
        """Personal 外部写根在批准前无副作用，Allow once 后才创建文件。"""
        home = self.context.workspace.parent / "owner"
        documents = home / "Documents"
        documents.mkdir(parents=True)
        target = documents / "approved.md"
        context = ToolContext(
            user_id=self.context.user_id,
            session_id=self.context.session_id,
            turn_id=self.context.turn_id,
            state_home=self.context.state_home,
            workspace=self.context.workspace,
            read_only_roots=(home,),
            write_roots=(documents,),
            owner_home=home,
        )
        approvals = ApprovalRepository(self.database)
        executor = self.executor(WriteFileTool(), approvals=approvals)

        pending = await executor.execute(
            context,
            ToolCall(
                "personal-write",
                "write_file",
                {"path": str(target), "content": "approved\n"},
            ),
        )

        self.assertIsNotNone(pending.approval_id)
        self.assertFalse(target.exists())
        assert pending.approval_id is not None
        approvals.approve(context.user_id, pending.approval_id)
        run = approvals.consume(context.user_id, pending.approval_id)
        approved = await executor.execute_approved(
            context,
            run,
            approval_id=pending.approval_id,
            decision=ApprovalDecision.ONCE,
        )

        self.assertTrue(approved.succeeded)
        self.assertEqual(target.read_text(encoding="utf-8"), "approved\n")
        self.assertNotIn(str(home), approved.model_text)

    async def test_approval_event_exposes_only_core_safe_grant_modes(self) -> None:
        """TUI 只能显示 Core 根据归一化参数给出的授权范围。"""
        osascript = self.context.workspace / "osascript"
        osascript.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        osascript.chmod(0o700)
        cases = (
            (
                RunCommandTool(),
                PolicyEngine(),
                ToolCall(
                    "safe-command",
                    "run_command",
                    {
                        "program": sys.executable,
                        "args": ["script.py"],
                        "timeout_seconds": 30,
                    },
                ),
                ["once", "session", "always"],
            ),
            (
                RunCommandTool(),
                PolicyEngine(),
                ToolCall(
                    "inline-osascript",
                    "run_command",
                    {
                        "program": str(osascript),
                        "args": ["-e", "display dialog 1"],
                        "timeout_seconds": 30,
                    },
                ),
                ["once", "session"],
            ),
            (
                HttpGetTool(resolver=lambda _host, _port: ("93.184.216.34",)),
                PolicyEngine(
                    network_resolver=lambda _host, _port: ("93.184.216.34",)
                ),
                ToolCall("https", "http_get", {"url": "https://example.com/data"}),
                ["once", "session", "always"],
            ),
            (
                WriteFileTool(),
                PolicyEngine(),
                ToolCall(
                    "write",
                    "write_file",
                    {"path": "note.txt", "content": "private"},
                ),
                ["once"],
            ),
        )

        for tool, policy, call, expected in cases:
            with self.subTest(call=call.call_id):
                events = []

                async def capture(event, captured=events) -> None:
                    captured.append(event)

                executor = ToolExecutor(
                    ToolRegistry((tool,)),
                    policy,
                    ToolRunRepository(self.database),
                    approvals=ApprovalRepository(self.database),
                )
                await executor.execute(self.context, call, on_event=capture)
                approval = next(
                    event for event in events if event.kind == "approval_required"
                )
                self.assertEqual(approval.data["grant_modes"], expected)

    async def test_session_command_grant_lasts_only_for_current_policy_engine(self) -> None:
        """Session 只放行当前 Runtime 的同一条 exact argv，重建后立即失效。"""
        program = self.context.workspace / "safe-command"
        program.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
        program.chmod(0o700)
        arguments = {
            "program": str(program),
            "args": ["status"],
            "timeout_seconds": 30,
        }
        approvals = ApprovalRepository(self.database)
        policy = PolicyEngine()
        executor = ToolExecutor(
            ToolRegistry((RunCommandTool(),)),
            policy,
            ToolRunRepository(self.database),
            approvals=approvals,
            policy_rules=PolicyRuleRepository(self.database),
        )
        pending = await executor.execute(
            self.context,
            ToolCall("session-first", "run_command", arguments),
        )
        assert pending.approval_id is not None
        approvals.approve(self.context.user_id, pending.approval_id)
        run = approvals.consume(self.context.user_id, pending.approval_id)

        approved = await executor.execute_approved(
            self.context,
            run,
            approval_id=pending.approval_id,
            decision=ApprovalDecision.SESSION,
        )
        repeated = await executor.execute(
            self.context,
            ToolCall("session-second", "run_command", arguments),
        )
        restarted = ToolExecutor(
            ToolRegistry((RunCommandTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
            policy_rules=PolicyRuleRepository(self.database),
        )
        after_restart = await restarted.execute(
            self.context,
            ToolCall("session-third", "run_command", arguments),
        )

        self.assertTrue(approved.succeeded)
        self.assertIsNone(repeated.approval_id)
        self.assertIsNotNone(after_restart.approval_id)
        with self.database.connect_read_only() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM policy_rules").fetchone()[0],
                0,
            )

    async def test_always_command_grant_persists_only_after_success(self) -> None:
        """Always 必须在成功执行后落 exact 规则，并让新 Runtime 复用。"""
        program = self.context.workspace / "persistent-command"
        program.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
        program.chmod(0o700)
        arguments = {
            "program": str(program),
            "args": ["status"],
            "timeout_seconds": 30,
        }
        approvals = ApprovalRepository(self.database)
        rules = PolicyRuleRepository(self.database)
        policy = PolicyEngine()
        executor = ToolExecutor(
            ToolRegistry((RunCommandTool(),)),
            policy,
            ToolRunRepository(self.database),
            approvals=approvals,
            policy_rules=rules,
        )
        pending = await executor.execute(
            self.context,
            ToolCall("always-first", "run_command", arguments),
        )
        assert pending.approval_id is not None
        approvals.approve(self.context.user_id, pending.approval_id)
        run = approvals.consume(self.context.user_id, pending.approval_id)

        approved = await executor.execute_approved(
            self.context,
            run,
            approval_id=pending.approval_id,
            decision=ApprovalDecision.ALWAYS,
        )
        restarted = ToolExecutor(
            ToolRegistry((RunCommandTool(),)),
            PolicyEngine(command_rules=rules.command_rules(self.context.user_id)),
            ToolRunRepository(self.database),
            approvals=approvals,
            policy_rules=rules,
        )
        repeated = await restarted.execute(
            self.context,
            ToolCall("always-second", "run_command", arguments),
        )

        self.assertTrue(approved.succeeded)
        self.assertIsNone(repeated.approval_id)
        with self.database.connect_read_only() as connection:
            stored = connection.execute(
                "SELECT rule_json FROM policy_rules"
            ).fetchall()
            events = connection.execute(
                "SELECT event_type, metadata_json FROM audit_events "
                "WHERE event_type = 'policy_rule.created'"
            ).fetchall()
        self.assertEqual(len(stored), 1)
        self.assertEqual(len(events), 1)
        self.assertNotIn("ok", stored[0]["rule_json"])
        self.assertNotIn("status", events[0]["metadata_json"])

    async def test_failed_command_never_creates_session_or_persistent_rule(self) -> None:
        """执行失败时 Session/Always 都不能产生可复用规则。"""
        program = self.context.workspace / "vanishing-command"
        program.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
        program.chmod(0o700)
        arguments = {
            "program": str(program),
            "args": [],
            "timeout_seconds": 30,
        }
        approvals = ApprovalRepository(self.database)
        rules = PolicyRuleRepository(self.database)
        policy = PolicyEngine()
        executor = ToolExecutor(
            ToolRegistry((RunCommandTool(),)),
            policy,
            ToolRunRepository(self.database),
            approvals=approvals,
            policy_rules=rules,
        )
        pending = await executor.execute(
            self.context,
            ToolCall("failed-always", "run_command", arguments),
        )
        assert pending.approval_id is not None
        approvals.approve(self.context.user_id, pending.approval_id)
        run = approvals.consume(self.context.user_id, pending.approval_id)
        program.unlink()

        failed = await executor.execute_approved(
            self.context,
            run,
            approval_id=pending.approval_id,
            decision=ApprovalDecision.ALWAYS,
        )

        self.assertFalse(failed.succeeded)
        with self.database.connect_read_only() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM policy_rules").fetchone()[0],
                0,
            )

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
