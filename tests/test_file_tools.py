"""``read_file`` 的 UTF-8 行窗口与安全边界测试。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import ToolContext, ToolResult, ToolValidationError
from miniclaw.tools.filesystem import ReadFileTool


class ReadFileToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 ``read_file`` 只返回允许 Workspace 内的有限文本窗口。"""

    def setUp(self) -> None:
        """创建不含真实用户文件的临时 Workspace。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name).resolve()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.context = ToolContext(
            user_id=1,
            session_id=2,
            turn_id=3,
            state_home=root / "state",
            workspace=self.workspace,
            read_only_roots=(),
        )

    async def _run(self, arguments: dict[str, JsonValue]) -> ToolResult:
        """通过公开校验与执行接口运行一次文件读取。"""
        tool = ReadFileTool()
        return await tool.execute(self.context, tool.validate(arguments))

    async def test_reads_utf8_lines_with_one_based_offset(self) -> None:
        """偏移量从一开始，并返回窗口后的续读位置。"""
        (self.workspace / "notes.txt").write_text("一\n二\n三\n四\n", encoding="utf-8")
        tool = ReadFileTool()

        result = await tool.execute(
            self.context,
            tool.validate({"path": "notes.txt", "offset": 2, "limit": 2}),
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            result.data,
            {
                "path": "notes.txt",
                "content": "二\n三\n",
                "offset": 2,
                "lines": 2,
                "truncated": True,
                "next_offset": 4,
            },
        )

    def test_validate_defaults_and_definition_match_the_public_contract(self) -> None:
        """默认窗口和公开 JSON Schema 必须与模型调用契约一致。"""
        tool = ReadFileTool()

        self.assertEqual(
            tool.validate({"path": "notes.txt"}),
            {"path": "notes.txt", "offset": 1, "limit": 200},
        )
        self.assertEqual(
            tool.definition.parameters,
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    async def test_binary_and_invalid_utf8_are_rejected(self) -> None:
        """NUL 和非法 UTF-8 不能作为文本内容交给模型。"""
        for name, content in (("nul.bin", b"a\x00b"), ("bad.txt", b"\xff")):
            (self.workspace / name).write_bytes(content)
            with self.subTest(name=name):
                result = await self._run({"path": name})
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "binary_file")

    def test_validate_rejects_booleans_unknown_keys_and_large_limit(self) -> None:
        """JSON 布尔值不能伪装整数，未知参数和超上限也必须拒绝。"""
        for arguments in (
            {"path": "a", "offset": True},
            {"path": "a", "limit": 1001},
            {"path": "a", "command": "cat"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError):
                ReadFileTool().validate(arguments)

    async def test_missing_and_directory_paths_have_stable_redacted_errors(self) -> None:
        """不存在路径和目录必须以稳定码失败，且不泄露临时绝对路径。"""
        (self.workspace / "folder").mkdir()
        for name, code in (("missing.txt", "not_found"), ("folder", "not_a_file")):
            with self.subTest(name=name):
                result = await self._run({"path": name})
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, code)
                self.assertNotIn(str(self.workspace.parent), result.error_message or "")

    async def test_read_is_bounded_and_validates_utf8_split_at_the_byte_limit(self) -> None:
        """512 KiB 边界截断多字节 UTF-8 时仍应识别为合法文本。"""
        limit = 512 * 1024
        (self.workspace / "boundary.txt").write_bytes(b"x" * (limit - 1) + "一".encode() + b"\n")

        result = await self._run({"path": "boundary.txt", "limit": 1000})

        self.assertTrue(result.ok)
        self.assertIsInstance(result.data, dict)
        assert isinstance(result.data, dict)
        self.assertLessEqual(len(result.data["content"].encode()), limit)
        self.assertTrue(result.data["truncated"])
        self.assertEqual(result.data["next_offset"], 2)


if __name__ == "__main__":
    unittest.main()
