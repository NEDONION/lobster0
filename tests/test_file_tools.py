"""Workspace 文本文件 Tool 的读取窗口与原子写入边界测试。"""

import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lobster0.providers.base import JsonValue
from lobster0.tools.base import ToolContext, ToolResult, ToolValidationError
from lobster0.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool


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

    async def test_personal_home_read_uses_redacted_display_path(self) -> None:
        """读取 Personal Home 文件时返回稳定标签，不暴露真实 Home 前缀。"""
        home = self.workspace.parent / "owner"
        documents = home / "Documents"
        documents.mkdir(parents=True)
        target = documents / "note.md"
        target.write_text("personal\n", encoding="utf-8")
        context = ToolContext(
            user_id=self.context.user_id,
            session_id=self.context.session_id,
            turn_id=self.context.turn_id,
            state_home=self.context.state_home,
            workspace=self.context.workspace,
            read_only_roots=(home,),
            owner_home=home,
        )

        result = await ReadFileTool().execute(
            context,
            ReadFileTool().validate({"path": str(target)}),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["path"], "home/Documents/note.md")
        self.assertNotIn(str(home), str(result.data))


class WriteFileToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 ``write_file`` 只做有限、原子且不隐式建目录的文本写入。"""

    def setUp(self) -> None:
        """创建隔离 Workspace 和额外只读根。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name).resolve()
        self.workspace = root / "workspace"
        self.read_only = root / "shared"
        self.workspace.mkdir()
        self.read_only.mkdir()
        self.context = ToolContext(
            user_id=1,
            session_id=2,
            turn_id=3,
            state_home=root / "state",
            workspace=self.workspace,
            read_only_roots=(self.read_only,),
        )
        self.tool = WriteFileTool()

    async def _run(self, arguments: dict[str, JsonValue]) -> ToolResult:
        """通过公开校验和执行边界运行一次写入。"""
        return await self.tool.execute(self.context, self.tool.validate(arguments))

    def test_validate_defaults_schema_and_utf8_byte_limit(self) -> None:
        """公开 Schema、overwrite 默认值和 256 KiB UTF-8 上限必须一致。"""
        self.assertEqual(
            self.tool.validate({"path": "note.txt", "content": "你好"}),
            {"path": "note.txt", "content": "你好", "overwrite": False},
        )
        self.assertEqual(self.tool.definition.risk.value, "medium")
        self.assertEqual(
            self.tool.definition.parameters,
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )
        for arguments in (
            {"path": "", "content": "x"},
            {"path": "x", "content": "a\0b"},
            {"path": "x", "content": "😀" * (64 * 1024 + 1)},
            {"path": "x", "content": "x", "overwrite": 1},
            {"path": "x", "content": "x", "parents": True},
        ):
            with self.subTest(arguments=list(arguments)), self.assertRaises(
                ToolValidationError
            ):
                self.tool.validate(arguments)

    async def test_creates_owner_only_utf8_file_without_parents(self) -> None:
        """新文件必须完整出现、权限私有，缺失父目录不能留下半文件。"""
        result = await self._run({"path": "note.txt", "content": "你好\n"})

        target = self.workspace / "note.txt"
        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "你好\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(
            result.data,
            {"path": "note.txt", "bytes": 7, "overwritten": False},
        )

        missing = await self._run({"path": "missing/note.txt", "content": "x"})
        self.assertEqual(missing.error_code, "parent_not_found")
        self.assertFalse((self.workspace / "missing").exists())

    async def test_existing_file_requires_overwrite_and_preserves_mode(self) -> None:
        """默认不得覆盖；显式覆盖必须替换完整内容并保留原权限。"""
        target = self.workspace / "note.txt"
        target.write_text("old", encoding="utf-8")
        target.chmod(0o640)

        refused = await self._run({"path": "note.txt", "content": "new"})
        self.assertEqual(refused.error_code, "file_exists")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")

        replaced = await self._run(
            {"path": "note.txt", "content": "new", "overwrite": True}
        )
        self.assertTrue(replaced.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(replaced.data["overwritten"], True)

    async def test_replace_failure_keeps_original_and_removes_temp_file(self) -> None:
        """原子替换失败时原文件不能损坏，同目录临时文件必须清理。"""
        target = self.workspace / "note.txt"
        target.write_text("old", encoding="utf-8")

        with mock.patch("lobster0.tools.filesystem.os.replace", side_effect=OSError):
            result = await self._run(
                {"path": "note.txt", "content": "new", "overwrite": True}
            )

        self.assertEqual(result.error_code, "write_failed")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual([path.name for path in self.workspace.iterdir()], ["note.txt"])

    async def test_write_guard_rejects_read_only_sensitive_and_symlink_targets(self) -> None:
        """直接调用 Tool 也不能绕开只读根、敏感文件或 symlink 防线。"""
        target = self.workspace / "real.txt"
        target.write_text("old", encoding="utf-8")
        (self.workspace / "alias.txt").symlink_to(target)
        for raw_path, code in (
            (str(self.read_only / "x.txt"), "read_only_path"),
            (".env", "sensitive_path"),
            ("alias.txt", "symlink_path"),
        ):
            with self.subTest(raw_path=raw_path):
                result = await self._run(
                    {"path": raw_path, "content": "secret", "overwrite": True}
                )
                self.assertEqual(result.error_code, code)

    async def test_personal_profile_writes_to_explicit_external_root(self) -> None:
        """显式 Personal 写根可写，结果路径不得泄露真实 Home 绝对路径。"""
        home = self.workspace.parent / "owner"
        documents = home / "Documents"
        documents.mkdir(parents=True)
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
        target = documents / "note.md"

        result = await self.tool.execute(
            context,
            self.tool.validate({"path": str(target), "content": "personal\n"}),
        )

        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "personal\n")
        self.assertEqual(
            result.data,
            {"path": "home/Documents/note.md", "bytes": 9, "overwritten": False},
        )


class EditFileToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 ``edit_file`` 只替换唯一精确文本并保留文件属性。"""

    def setUp(self) -> None:
        """创建隔离 Workspace 与待编辑 Tool。"""
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
        self.tool = EditFileTool()

    async def _run(self, arguments: dict[str, JsonValue]) -> ToolResult:
        """通过公开校验与执行接口运行一次精确编辑。"""
        return await self.tool.execute(self.context, self.tool.validate(arguments))

    def test_validate_requires_exact_nonempty_text_contract(self) -> None:
        """路径、非空 old_text、字符串 new_text 与未知字段必须严格校验。"""
        self.assertEqual(
            self.tool.validate({"path": "note.txt", "old_text": "old", "new_text": ""}),
            {"path": "note.txt", "old_text": "old", "new_text": ""},
        )
        for arguments in (
            {"path": "note.txt", "old_text": "", "new_text": "new"},
            {"path": "note.txt", "old_text": "old", "new_text": 1},
            {"path": "note.txt", "old_text": "old\0", "new_text": "new"},
            {"path": "note.txt", "old_text": "old", "new_text": "new", "regex": True},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError):
                self.tool.validate(arguments)

    async def test_replaces_one_exact_match_and_preserves_mode(self) -> None:
        """唯一匹配必须原子替换，并保留原文件权限。"""
        target = self.workspace / "note.txt"
        target.write_text("before old after\n", encoding="utf-8")
        target.chmod(0o640)

        result = await self._run(
            {"path": "note.txt", "old_text": "old", "new_text": "new"}
        )

        self.assertTrue(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "before new after\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(result.data, {"path": "note.txt", "bytes": 17})

    async def test_zero_overlapping_or_multiple_matches_never_modify_file(self) -> None:
        """零次、重叠多次和普通多次匹配都不能猜测替换目标。"""
        cases = (
            ("alpha", "missing", "text_not_found"),
            ("aaa", "aa", "text_not_unique"),
            ("old and old", "old", "text_not_unique"),
        )
        for index, (content, old_text, code) in enumerate(cases):
            target = self.workspace / f"case-{index}.txt"
            target.write_text(content, encoding="utf-8")
            with self.subTest(content=content):
                result = await self._run(
                    {"path": target.name, "old_text": old_text, "new_text": "new"}
                )
                self.assertEqual(result.error_code, code)
                self.assertEqual(target.read_text(encoding="utf-8"), content)

    async def test_binary_and_oversized_files_fail_without_replacement(self) -> None:
        """非 UTF-8 或超过 1 MiB 的文件不能被覆盖。"""
        binary = self.workspace / "binary.txt"
        binary.write_bytes(b"old\0value")
        binary_result = await self._run(
            {"path": binary.name, "old_text": "old", "new_text": "new"}
        )
        self.assertEqual(binary_result.error_code, "binary_file")

        large = self.workspace / "large.txt"
        large.write_bytes(b"old" + b"x" * (1024 * 1024))
        large_result = await self._run(
            {"path": large.name, "old_text": "old", "new_text": "new"}
        )
        self.assertEqual(large_result.error_code, "file_too_large")

    async def test_edit_result_larger_than_one_mib_keeps_original(self) -> None:
        """替换后的 UTF-8 文件超过上限时不能发布超大结果。"""
        target = self.workspace / "growth.txt"
        target.write_text("old", encoding="utf-8")

        result = await self._run(
            {
                "path": target.name,
                "old_text": "old",
                "new_text": "x" * (1024 * 1024 + 1),
            }
        )

        self.assertEqual(result.error_code, "file_too_large")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")

    async def test_file_changed_after_read_is_not_overwritten(self) -> None:
        """读取后的并发修改必须让精确编辑失败，不能覆盖别人的新内容。"""
        target = self.workspace / "changed.txt"
        target.write_text("old", encoding="utf-8")
        real_mkstemp = tempfile.mkstemp

        def change_then_create_temp(*args: object, **kwargs: object) -> tuple[int, str]:
            target.write_text("someone else", encoding="utf-8")
            return real_mkstemp(*args, **kwargs)

        with mock.patch(
            "lobster0.tools.filesystem.tempfile.mkstemp",
            side_effect=change_then_create_temp,
        ):
            result = await self._run(
                {"path": target.name, "old_text": "old", "new_text": "new"}
            )

        self.assertEqual(result.error_code, "file_changed")
        self.assertEqual(target.read_text(encoding="utf-8"), "someone else")


if __name__ == "__main__":
    unittest.main()
