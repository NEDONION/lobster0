"""MiniClaw Textual TUI 的无头交互测试。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from textual.containers import VerticalScroll
from textual.widgets import Button, Markdown, Static, TextArea

from miniclaw.agent.events import RunEvent
from miniclaw.paths import build_state_paths
from miniclaw.tools.base import ToolDefinition, ToolRisk
from miniclaw.tui.app import MiniClawApp, ToolCard, _terminal_safe

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
                tool_definitions=(
                    ToolDefinition(
                        name="read_file",
                        description="Read one workspace file.",
                        parameters={"type": "object"},
                        risk=ToolRisk.LOW,
                    ),
                ),
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

    async def test_model_deltas_update_one_temporary_message_then_finalize_it(self) -> None:
        """每个 Turn 只能有一个流式 Assistant 卡片，完成后保留完整正文。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)

        async with app.run_test() as pilot:
            await app.on_run_event(RunEvent("model_text_delta", 9, {"text": "你"}))
            await app.on_run_event(RunEvent("model_text_delta", 9, {"text": "好"}))
            await pilot.pause()

            message = app.query_one("#assistant-9", Markdown)
            self.assertEqual(message.source, "你好")
            self.assertTrue(message.has_class("temporary"))
            self.assertEqual(len(app.query("#assistant-9")), 1)

            await app.on_run_event(
                RunEvent(
                    "turn_finished",
                    9,
                    {"status": "completed", "content": "你好"},
                )
            )
            await pilot.pause()

            self.assertEqual(message.source, "你好")
            self.assertFalse(message.has_class("temporary"))

    async def test_tool_events_update_one_text_labelled_safe_card(self) -> None:
        """任意 call ID 只能映射一张卡，状态和预览不能依赖颜色或解释 ANSI。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)
        call_id = "call/with unsafe css id"

        async with app.run_test() as pilot:
            await app.on_run_event(
                RunEvent(
                    "tool_requested",
                    11,
                    {
                        "call_id": call_id,
                        "tool_name": "read_file",
                        "summary": "README.md",
                    },
                )
            )
            await app.on_run_event(
                RunEvent(
                    "tool_started",
                    11,
                    {"call_id": call_id, "tool_name": "read_file"},
                )
            )
            await app.on_run_event(
                RunEvent(
                    "tool_finished",
                    11,
                    {
                        "call_id": call_id,
                        "tool_name": "read_file",
                        "status": "succeeded",
                        "duration_ms": 18,
                        "preview": "safe\x1b[2J preview",
                    },
                )
            )
            await pilot.pause()

            card = app.query_one(ToolCard)
            rendered = str(card.render())
            self.assertEqual(len(app.query(ToolCard)), 1)
            self.assertEqual(card.status, "succeeded")
            self.assertIn("Tool: read_file", rendered)
            self.assertIn("Status: succeeded", rendered)
            self.assertIn("18 ms", rendered)
            self.assertIn("safe[2J preview", rendered)
            self.assertNotIn("\x1b", rendered)

    async def test_local_slash_commands_render_without_contacting_the_agent(self) -> None:
        """help/status/tools/unknown 都应只更新本地 transcript。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)

        async with app.run_test() as pilot:
            for command in ("/help", "/status", "/tools", "/missing"):
                self.assertTrue(await app.handle_local_command(command))
            self.assertFalse(await app.handle_local_command("ordinary message"))
            await pilot.pause()

            output = "\n".join(
                str(message.render())
                for message in app.query(Static)
                if message.has_class("local-message")
            )
            self.assertIn("/help", output)
            self.assertIn("model: deepseek-v4-pro", output)
            self.assertIn("read_file (low)", output)
            self.assertIn("Unknown command: /missing", output)

    async def test_new_command_clears_visible_transcript_and_changes_session(self) -> None:
        """新 Session 应清空界面投影，但不创建第二个 App 或 Runtime。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)

        async with app.run_test() as pilot:
            await app.handle_local_command("/help")
            self.assertGreater(len(app.query(".local-message")), 0)

            self.assertTrue(await app.handle_local_command("/new"))
            await pilot.pause()

            self.assertNotEqual(app.session_id, "default")
            self.assertEqual(len(app.query("#transcript > *")), 0)
            self.assertIn(app.session_id, str(app.query_one("#status", Static).render()))

    async def test_quit_command_exits_the_same_app(self) -> None:
        """exit/quit 必须结束唯一 TUI，而不是跳转到另一套界面。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)

        async with app.run_test():
            self.assertTrue(await app.handle_local_command("/quit"))

        self.assertEqual(app.return_value, 0)

    async def test_missing_state_initializes_inside_the_same_app(self) -> None:
        """裸入口缺少状态时应原地初始化，再聚焦同一个聊天输入框。"""
        app = MiniClawApp(self.paths)
        original_app_id = id(app)

        async with app.run_test() as pilot:
            self.assertEqual(len(app.query("#composer")), 0)
            self.assertEqual(len(app.query("#onboarding")), 1)
            self.assertIsInstance(app.query_one("#initialize"), Button)

            await pilot.click("#initialize")
            await pilot.pause()

            self.assertEqual(id(app), original_app_id)
            self.assertTrue(self.paths.database.is_file())
            self.assertTrue(self.paths.config.is_file())
            self.assertEqual(len(app.query("#onboarding")), 0)
            self.assertIs(app.focused, app.query_one("#composer", TextArea))


if __name__ == "__main__":
    unittest.main()
