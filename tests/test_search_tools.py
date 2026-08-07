"""``glob`` 与 ``grep`` 的 Workspace 安全搜索测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import ToolContext, ToolResult, ToolValidationError
from miniclaw.tools.search import GlobTool, GrepTool


class GlobToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 ``glob`` 只返回 Workspace 内稳定且有限的普通文件路径。"""

    def setUp(self) -> None:
        """创建互相隔离的 Workspace 与外部目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name).resolve()
        self.workspace = root / "workspace"
        self.outside = root / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()
        (self.outside / "hidden.py").write_text("secret", encoding="utf-8")
        self.context = ToolContext(
            user_id=1,
            session_id=2,
            turn_id=3,
            state_home=root / "state",
            workspace=self.workspace,
            read_only_roots=(),
        )

    async def _glob(self, arguments: dict[str, JsonValue]) -> ToolResult:
        """通过公开校验与执行接口运行一次路径匹配。"""
        tool = GlobTool()
        return await tool.execute(self.context, tool.validate(arguments))

    async def test_glob_returns_sorted_safe_relative_paths(self) -> None:
        """结果必须排序，并静默过滤敏感文件和目录 symlink 后代。"""
        (self.workspace / "b.py").write_text("", encoding="utf-8")
        (self.workspace / "a.py").write_text("", encoding="utf-8")
        (self.workspace / ".env").write_text("SECRET=x", encoding="utf-8")
        (self.workspace / "jump").symlink_to(self.outside, target_is_directory=True)

        result = await self._glob({"pattern": "**/*", "limit": 20})

        self.assertTrue(result.ok)
        self.assertEqual(result.data["matches"], ["a.py", "b.py"])
        self.assertFalse(result.data["truncated"])
        self.assertNotIn(".env", repr(result.data))
        self.assertNotIn("jump/hidden.py", repr(result.data))

    async def test_recursive_patterns_include_top_level_files(self) -> None:
        """``**/*`` 与 ``**/*.py`` 不能漏掉 root 顶层普通文件。"""
        (self.workspace / "top.py").write_text("", encoding="utf-8")
        nested = self.workspace / "nested"
        nested.mkdir()
        (nested / "child.py").write_text("", encoding="utf-8")
        (nested / "note.txt").write_text("", encoding="utf-8")

        all_files = await self._glob({"pattern": "**/*"})
        python_files = await self._glob({"pattern": "**/*.py"})

        self.assertEqual(
            all_files.data["matches"],
            ["nested/child.py", "nested/note.txt", "top.py"],
        )
        self.assertEqual(python_files.data["matches"], ["nested/child.py", "top.py"])

    async def test_glob_enforces_result_limit_and_reports_truncation(self) -> None:
        """超过返回上限时只交付排序后的窗口并标记截断。"""
        for name in ("c.txt", "a.txt", "b.txt"):
            (self.workspace / name).write_text("", encoding="utf-8")

        result = await self._glob({"pattern": "*.txt", "limit": 2})

        self.assertEqual(result.data, {"matches": ["a.txt", "b.txt"], "truncated": True})

    def test_validate_defaults_definition_and_rejects_invalid_parameters(self) -> None:
        """公开 Schema、默认值和相对 glob 参数必须保持一致。"""
        tool = GlobTool()
        self.assertEqual(
            tool.validate({"pattern": "**/*"}),
            {"pattern": "**/*", "root": ".", "limit": 200},
        )
        self.assertEqual(
            tool.definition.parameters,
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "root": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        )
        for arguments in (
            {},
            {"pattern": ""},
            {"pattern": "/tmp/*"},
            {"pattern": 1},
            {"pattern": "*", "root": ""},
            {"pattern": "*", "root": 1},
            {"pattern": "*", "limit": True},
            {"pattern": "*", "limit": 0},
            {"pattern": "*", "limit": 201},
            {"pattern": "*", "command": "find"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError):
                tool.validate(arguments)

    async def test_unsafe_roots_fail_with_stable_redacted_errors(self) -> None:
        """逃逸、敏感和 symlink-loop root 必须失败且不暴露绝对路径。"""
        (self.workspace / ".env").write_text("SECRET=x", encoding="utf-8")
        loop = self.workspace / "loop"
        loop.symlink_to(loop)
        cases = (
            ("../outside", "workspace_escape"),
            (".env", "sensitive_path"),
            ("loop", "workspace_escape"),
        )
        for root, code in cases:
            with self.subTest(root=root):
                result = await self._glob({"pattern": "*", "root": root})
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, code)
                self.assertNotIn(str(self.workspace.parent), result.error_message or "")

    async def test_file_symlink_to_another_allowed_root_is_safely_skipped(self) -> None:
        """解析到其他允许根的文件 symlink 不能使 root 相对展示崩溃。"""
        shared = self.workspace.parent / "shared"
        shared.mkdir()
        target = shared / "shared.txt"
        target.write_text("shared", encoding="utf-8")
        (self.workspace / "alias.txt").symlink_to(target)
        context = ToolContext(
            user_id=1,
            session_id=2,
            turn_id=3,
            state_home=self.context.state_home,
            workspace=self.workspace,
            read_only_roots=(shared,),
        )
        tool = GlobTool()

        result = await tool.execute(context, tool.validate({"pattern": "*"}))

        self.assertEqual(result.data, {"matches": [], "truncated": False})


class GrepToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 ``grep`` 有界扫描 Workspace 内的 UTF-8 普通文件。"""

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

    async def _grep(self, arguments: dict[str, JsonValue]) -> ToolResult:
        """通过公开校验与执行接口运行一次文本搜索。"""
        tool = GrepTool()
        return await tool.execute(self.context, tool.validate(arguments))

    async def test_grep_returns_path_line_number_and_bounded_text(self) -> None:
        """匹配项必须包含 root 相对路径、一开始行号和单行文本。"""
        (self.workspace / "agent.py").write_text(
            "class AgentRunner:\n    pass\n",
            encoding="utf-8",
        )

        result = await self._grep({"pattern": "AgentRunner", "glob": "**/*.py"})

        self.assertEqual(
            result.data["matches"],
            [{"path": "agent.py", "line": 1, "text": "class AgentRunner:"}],
        )
        self.assertFalse(result.data["truncated"])

    async def test_invalid_regex_has_stable_error_code(self) -> None:
        """非法 Python 正则必须返回精确且不含编译细节的错误码。"""
        result = await self._grep({"pattern": "["})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "invalid_pattern")
        self.assertNotIn(str(self.workspace.parent), result.error_message or "")

    async def test_binary_invalid_utf8_and_large_files_are_skipped(self) -> None:
        """NUL、非法 UTF-8 和超过 1 MiB 的文件不能产生结果。"""
        (self.workspace / "good.txt").write_text("needle\n", encoding="utf-8")
        (self.workspace / "nul.txt").write_bytes(b"needle\x00")
        (self.workspace / "bad.txt").write_bytes(b"needle\xff")
        (self.workspace / "large.txt").write_bytes(b"needle" + b"x" * (1024 * 1024))

        result = await self._grep({"pattern": "needle", "glob": "*.txt"})

        self.assertEqual(
            result.data,
            {
                "matches": [{"path": "good.txt", "line": 1, "text": "needle"}],
                "truncated": False,
            },
        )

    async def test_each_line_matches_once_and_text_is_limited_to_500_characters(self) -> None:
        """一行多次命中仍只返回一次，展示文本最多 500 字符且无换行。"""
        line = "needle needle " + "x" * 600
        (self.workspace / "long.txt").write_text(line + "\n", encoding="utf-8")

        result = await self._grep({"pattern": "needle"})

        self.assertEqual(len(result.data["matches"]), 1)
        self.assertEqual(result.data["matches"][0]["text"], line[:500])

    async def test_match_limit_stops_search_and_marks_truncation(self) -> None:
        """达到结果 limit 后必须立即停止并标记截断。"""
        (self.workspace / "many.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")

        result = await self._grep({"pattern": "hit", "limit": 2})

        self.assertEqual(len(result.data["matches"]), 2)
        self.assertTrue(result.data["truncated"])

    async def test_matches_are_sorted_by_path_then_line(self) -> None:
        """跨目录匹配结果必须按相对路径和行号稳定排序。"""
        (self.workspace / "z.txt").write_text("hit\n", encoding="utf-8")
        nested = self.workspace / "a"
        nested.mkdir()
        (nested / "a.txt").write_text("hit\n", encoding="utf-8")

        result = await self._grep({"pattern": "hit"})

        self.assertEqual(
            result.data["matches"],
            [
                {"path": "a/a.txt", "line": 1, "text": "hit"},
                {"path": "z.txt", "line": 1, "text": "hit"},
            ],
        )

    async def test_file_count_limit_stops_after_200_candidates(self) -> None:
        """即使没有文本命中，也不能选择超过 200 个普通文件。"""
        for index in range(201):
            (self.workspace / f"f{index:03}.txt").write_text("none", encoding="utf-8")

        result = await self._grep({"pattern": "needle"})

        self.assertEqual(result.data["matches"], [])
        self.assertTrue(result.data["truncated"])

    async def test_total_read_limit_stops_at_20_mib(self) -> None:
        """累计读取达到 20 MiB 后必须停止，不读取后续候选。"""
        content = b"x" * (1024 * 1024)
        for index in range(21):
            (self.workspace / f"f{index:02}.txt").write_bytes(content)

        result = await self._grep({"pattern": "needle"})

        self.assertEqual(result.data["matches"], [])
        self.assertTrue(result.data["truncated"])

    async def test_disappearing_permission_and_symlink_loop_candidates_are_skipped(self) -> None:
        """候选 I/O 失败和 symlink loop 必须安全跳过且不泄露 OSError。"""
        candidate = self.workspace / "candidate.txt"
        candidate.write_text("needle", encoding="utf-8")
        loop = self.workspace / "loop.txt"
        loop.symlink_to(loop)

        for error in (FileNotFoundError(str(candidate)), PermissionError(str(candidate))):
            with self.subTest(error=type(error).__name__), patch(
                "pathlib.Path.open",
                side_effect=error,
            ):
                result = await self._grep({"pattern": "needle"})
                self.assertTrue(result.ok)
                self.assertEqual(result.data["matches"], [])
                self.assertNotIn(str(self.workspace.parent), repr(result.data))

    def test_validate_defaults_definition_and_rejects_invalid_parameters(self) -> None:
        """grep 的 Schema、默认值和相对文件 glob 必须保持一致。"""
        tool = GrepTool()
        self.assertEqual(
            tool.validate({"pattern": "needle"}),
            {"pattern": "needle", "glob": "**/*", "root": ".", "limit": 100},
        )
        self.assertEqual(
            tool.definition.parameters,
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                    "root": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        )
        for arguments in (
            {},
            {"pattern": ""},
            {"pattern": 1},
            {"pattern": "x", "glob": ""},
            {"pattern": "x", "glob": "/tmp/*"},
            {"pattern": "x", "glob": 1},
            {"pattern": "x", "root": ""},
            {"pattern": "x", "root": 1},
            {"pattern": "x", "limit": True},
            {"pattern": "x", "limit": 0},
            {"pattern": "x", "limit": 101},
            {"pattern": "x", "command": "grep"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError):
                tool.validate(arguments)


if __name__ == "__main__":
    unittest.main()
