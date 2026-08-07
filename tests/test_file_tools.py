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

    async def test_next_offset_reads_real_lines_beyond_512_kib(self) -> None:
        """第一页的 cursor 必须能读取 512 KiB 之后的真实完整行。"""
        full_page = (b"x" * 1023 + b"\n") * 512
        (self.workspace / "paged.txt").write_bytes(full_page + b"tail\n")

        first = await self._run({"path": "paged.txt", "limit": 1000})
        self.assertIsInstance(first.data, dict)
        assert isinstance(first.data, dict)
        self.assertEqual(len(first.data["content"].encode()), 512 * 1024)
        self.assertEqual(first.data["lines"], 512)
        self.assertEqual(first.data["next_offset"], 513)

        resumed = await self._run({"path": "paged.txt", "offset": 513})
        self.assertEqual(
            resumed.data,
            {
                "path": "paged.txt",
                "content": "tail\n",
                "offset": 513,
                "lines": 1,
                "truncated": False,
            },
        )

    async def test_byte_budget_stops_before_a_complete_line_without_losing_it(self) -> None:
        """页尾放不下的普通行必须由下一行号完整续读，不能切断后跳过。"""
        first_line = "a" * (400 * 1024) + "\n"
        second_line = "b" * (200 * 1024) + "\n"
        (self.workspace / "whole-lines.txt").write_text(
            first_line + second_line,
            encoding="utf-8",
        )

        first = await self._run({"path": "whole-lines.txt", "limit": 1000})
        self.assertEqual(first.data["content"], first_line)
        self.assertEqual(first.data["lines"], 1)
        self.assertEqual(first.data["next_offset"], 2)

        resumed = await self._run({"path": "whole-lines.txt", "offset": 2})
        self.assertEqual(resumed.data["content"], second_line)
        self.assertFalse(resumed.data["truncated"])

    async def test_continuation_preserves_final_line_without_newline(self) -> None:
        """512 KiB 之后无末尾换行的最后一行也必须能完整续读。"""
        full_page = (b"x" * 1023 + b"\n") * 512
        (self.workspace / "no-newline.txt").write_bytes(full_page + b"final")

        first = await self._run({"path": "no-newline.txt", "limit": 1000})
        resumed = await self._run(
            {"path": "no-newline.txt", "offset": first.data["next_offset"]}
        )

        self.assertEqual(resumed.data["content"], "final")
        self.assertEqual(resumed.data["lines"], 1)
        self.assertFalse(resumed.data["truncated"])

    async def test_line_larger_than_512_kib_fails_without_a_lossy_cursor(self) -> None:
        """单行无法装入一页时必须稳定失败，不能发布会丢数据的 cursor。"""
        (self.workspace / "long.txt").write_bytes(b"x" * (512 * 1024 + 1))

        result = await self._run({"path": "long.txt"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "line_too_large")
        self.assertNotIn(str(self.workspace.parent), result.error_message or "")

    async def test_four_byte_utf8_starting_after_512_kib_is_not_binary(self) -> None:
        """窗口外才开始的完整 emoji 不能被误报为非法 UTF-8。"""
        (self.workspace / "emoji.txt").write_bytes(b"x" * (512 * 1024) + "😀".encode())

        result = await self._run({"path": "emoji.txt"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "line_too_large")

    async def test_eof_and_offset_past_lines_have_no_next_offset(self) -> None:
        """普通 EOF 与超出已有行的偏移量不能虚构续读 cursor。"""
        (self.workspace / "single.txt").write_text("only line\n", encoding="utf-8")

        for arguments in ({"path": "single.txt"}, {"path": "single.txt", "offset": 2}):
            with self.subTest(arguments=arguments):
                result = await self._run(arguments)
                self.assertIsInstance(result.data, dict)
                assert isinstance(result.data, dict)
                self.assertFalse(result.data["truncated"])
                self.assertNotIn("next_offset", result.data)


if __name__ == "__main__":
    unittest.main()
