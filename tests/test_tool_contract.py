"""Lobster0 Tool 数据契约与 Registry 行为测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from lobster0.memory.store import MemoryStore
from lobster0.paths import build_state_paths
from lobster0.providers.base import JsonValue
from lobster0.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
)
from lobster0.tools.command import RunCommandTool
from lobster0.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from lobster0.tools.memory import ProposeMemoryTool, ReadMemoryTool
from lobster0.tools.registry import ToolRegistry
from lobster0.tools.search import GlobTool, GrepTool
from lobster0.tools.system import SystemInfoTool
from lobster0.tools.web import HttpGetTool


class _EchoTool:
    """提供稳定 Schema 与返回值的测试 Tool。"""

    definition = ToolDefinition(
        name="echo",
        description="Echo one text value.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """原样返回当前测试提供的结构化参数。"""
        return arguments

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """返回参数，供后续 Executor 测试复用同一 Tool。"""
        return ToolResult.success(arguments)


class ToolContractTest(unittest.TestCase):
    """验证模型可见 Schema 与 Tool Result 的稳定边界。"""

    def test_builtin_registry_exposes_ten_tools_in_stable_order(self) -> None:
        """内置 Tool Schema 必须按名称稳定排序，避免模型请求漂移。"""
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(build_state_paths(Path(directory).resolve()))
            registry = ToolRegistry(
                (
                    SystemInfoTool(),
                    ReadFileTool(),
                    WriteFileTool(),
                    EditFileTool(),
                    GlobTool(),
                    GrepTool(),
                    HttpGetTool(),
                    RunCommandTool(),
                    ReadMemoryTool(store),
                    ProposeMemoryTool(store),
                )
            )

            names = [schema["function"]["name"] for schema in registry.schemas]

        self.assertEqual(
            names,
            [
                "edit_file",
                "glob",
                "grep",
                "http_get",
                "propose_memory",
                "read_file",
                "read_memory",
                "run_command",
                "system_info",
                "write_file",
            ],
        )

    def test_registry_emits_stable_openai_schema_and_rejects_duplicate_names(self) -> None:
        """Registry 必须稳定列出工具，并在启动时拒绝同名覆盖。"""
        registry = ToolRegistry((_EchoTool(),))

        tool = registry.get("echo")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.definition.name, "echo")
        self.assertEqual(
            registry.schemas,
            (
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "Echo one text value.",
                        "parameters": _EchoTool.definition.parameters,
                    },
                },
            ),
        )
        with self.assertRaisesRegex(ValueError, "duplicate tool name: echo"):
            ToolRegistry((_EchoTool(), _EchoTool()))

    def test_run_command_schema_teaches_direct_execution_and_macos_app_launch(self) -> None:
        """模型必须知道 run_command 只执行单个程序，并用 open -a 启动 macOS 应用。"""
        schema = ToolRegistry((RunCommandTool(),)).schemas[0]
        description = schema["function"]["description"].casefold()

        self.assertIn("single executable", description)
        self.assertIn("never use a shell", description)
        self.assertIn("request approval", description)
        self.assertIn("open -a", description)
        self.assertIn("system_info applications", description)
        self.assertIn("lark-cli", description)
        self.assertIn("active feishu", description)
        self.assertIn("do not claim", description)

    def test_tool_result_uses_stable_model_json_without_traceback(self) -> None:
        """成功和失败结果必须是模型可解析且不含内部异常的 JSON。"""
        success = json.loads(ToolResult.success({"value": 1}).to_model_text("echo"))
        failure = json.loads(
            ToolResult.failure("invalid_arguments", "text is required").to_model_text("echo")
        )

        self.assertEqual(success, {"ok": True, "tool": "echo", "data": {"value": 1}})
        self.assertEqual(
            failure,
            {
                "ok": False,
                "tool": "echo",
                "error": {
                    "code": "invalid_arguments",
                    "message": "text is required",
                    "retryable": False,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
