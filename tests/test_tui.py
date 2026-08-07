"""MiniClaw Textual TUI 的无头交互测试。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from textual.containers import VerticalScroll
from textual.widgets import Static, TextArea

from miniclaw.paths import build_state_paths
from miniclaw.tui.app import MiniClawApp, _terminal_safe

if TYPE_CHECKING:
    from miniclaw.runtime import AgentRuntime


class TuiShellTest(unittest.IsolatedAsyncioTestCase):
    """验证唯一 TUI 的最小布局、焦点与安全渲染边界。"""

    def setUp(self) -> None:
        """创建不接触真实 MiniClaw 状态的临时路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.runtime = cast(
            "AgentRuntime",
            SimpleNamespace(
                model="deepseek-v4-pro",
                workspace=self.paths.workspace,
            ),
        )

    async def test_eighty_by_twenty_four_starts_with_one_focused_composer(self) -> None:
        """小终端仍应显示三块主区域，并把键盘焦点放在唯一输入框。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()

            status = app.query_one("#status", Static)
            transcript = app.query_one("#transcript", VerticalScroll)
            composer = app.query_one("#composer", TextArea)
            rendered_status = str(status.render())

            self.assertIs(app.focused, composer)
            self.assertEqual(len(app.query("#composer")), 1)
            self.assertEqual(len(app.query("#transcript")), 1)
            self.assertGreater(transcript.size.height, 0)
            self.assertGreater(composer.size.height, 0)
            self.assertIn("MiniClaw", rendered_status)
            self.assertIn("deepseek-v4-pro", rendered_status)
            self.assertIn("session:default", rendered_status)
            self.assertNotIn(str(self.paths.home.parent), rendered_status)

    def test_terminal_safe_removes_ansi_and_controls_but_keeps_text_layout(self) -> None:
        """模型或 Tool 输出不能用 ANSI、OSC、C0/C1 改写终端状态。"""
        unsafe = (
            "你好\n\t**MiniClaw**"
            "\x1b[2J"
            "\x1b]8;;https://evil.example\x07link\x1b]8;;\x07"
            "\x00\x08\x7f\x85"
        )

        cleaned = _terminal_safe(unsafe)

        self.assertEqual(cleaned, "你好\n\t**MiniClaw**[2J]8;;https://evil.examplelink]8;;")
        for character in cleaned:
            self.assertTrue(
                character in "\n\t"
                or ord(character) >= 0x20
                and not 0x7F <= ord(character) <= 0x9F
            )


if __name__ == "__main__":
    unittest.main()
