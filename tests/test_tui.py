"""MiniClaw Textual TUI 的无头交互测试。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import mock

from textual.containers import VerticalScroll
from textual.widgets import Button, Collapsible, Markdown, Static, TextArea

from miniclaw.agent.events import RunEvent
from miniclaw.paths import build_state_paths
from miniclaw.policy.approvals import ApprovalDecision
from miniclaw.tools.base import ToolDefinition, ToolRisk
from miniclaw.tui.app import (
    ApprovalModal,
    MiniClawApp,
    ReasoningCard,
    ToolCard,
    _terminal_safe,
)

if TYPE_CHECKING:
    from miniclaw.runtime import AgentRuntime


class FakeTurnService:
    """记录 TUI 调用，并用真实 RunEvent 模拟一次完成的 Turn。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []

    async def handle(self, owner_id, text, conversation_id, *, on_event=None):
        self.calls.append((owner_id, text, conversation_id))
        assert on_event is not None
        await on_event(RunEvent("turn_started", 21, {}))
        await on_event(
            RunEvent(
                "model_usage",
                21,
                {
                    "iteration": 2,
                    "context_tokens": 1_200,
                    "input_tokens": 1_500,
                    "output_tokens": 64,
                    "tool_calls": 2,
                    "provider_request_id": "req-21",
                },
            )
        )
        await on_event(RunEvent("model_text_delta", 21, {"text": "pong"}))
        await on_event(
            RunEvent(
                "turn_finished",
                21,
                {
                    "status": "completed",
                    "content": "pong",
                    "context_tokens": 1_200,
                    "input_tokens": 1_500,
                    "output_tokens": 64,
                    "iterations": 2,
                    "tool_calls": 2,
                    "provider_request_id": "req-21",
                    "duration_ms": 432,
                },
            )
        )


class BlockingTurnService:
    """保持 Turn 运行，直到测试通过 Esc 取消 Worker。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def handle(self, owner_id, text, conversation_id, *, on_event=None):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FailingTurnService:
    """模拟 Provider 失败，验证输入草稿不会丢失。"""

    async def handle(self, owner_id, text, conversation_id, *, on_event=None):
        del owner_id, text, conversation_id, on_event
        raise RuntimeError("provider failed")


class ApprovalTurnService:
    """先请求审批，再记录 TUI 通过同一 TurnService 的续跑决定。"""

    def __init__(self) -> None:
        self.decisions: list[tuple[int, int, ApprovalDecision]] = []

    async def handle(self, owner_id, text, conversation_id, *, on_event=None):
        assert on_event is not None
        await on_event(
            RunEvent(
                "tool_requested",
                31,
                {
                    "call_id": "write-1",
                    "tool_name": "write_file",
                    "summary": "write_file",
                },
            )
        )
        await on_event(
            RunEvent(
                "approval_required",
                31,
                {
                    "approval_id": 7,
                    "call_id": "write-1",
                    "tool_name": "write_file",
                    "summary": "write_file notes.txt",
                    "arguments": {"path": "/safe/workspace/notes.txt", "content": "hello"},
                    "expires_at": "2030-01-01T00:00:00+00:00",
                    "grant_modes": ["once"],
                },
            )
        )

    async def continue_approval(
        self,
        owner_id,
        approval_id,
        *,
        decision,
        on_event=None,
    ):
        self.decisions.append((owner_id, approval_id, decision))
        assert on_event is not None
        await on_event(
            RunEvent(
                "tool_finished",
                31,
                {
                    "call_id": "write-1",
                    "tool_name": "write_file",
                    "status": (
                        "denied"
                        if decision is ApprovalDecision.DENY
                        else "succeeded"
                    ),
                },
            )
        )
        await on_event(RunEvent("model_text_delta", 32, {"text": "continued"}))
        await on_event(
            RunEvent(
                "turn_finished",
                32,
                {"status": "completed", "content": "continued"},
            )
        )


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
                owner_id=1,
                model="deepseek-v4-pro",
                workspace=self.paths.workspace,
                ui_language="zh-CN",
                context_budget_tokens=128_000,
                service=FakeTurnService(),
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

    async def test_enter_runs_one_turn_and_shift_enter_keeps_a_newline(self) -> None:
        """Enter 发送并清空输入；Shift+Enter 只在同一输入框内换行。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)

        async with app.run_test() as pilot:
            composer = app.query_one("#composer", TextArea)
            composer.load_text("ping")
            composer.cursor_location = (0, 4)
            await pilot.press("shift+enter")
            self.assertEqual(composer.text, "ping\n")

            composer.load_text("ping")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(self.runtime.service.calls, [(1, "ping", "default")])
            self.assertEqual(composer.text, "")
            self.assertFalse(composer.disabled)
            self.assertIs(app.focused, composer)
            self.assertEqual(app.query_one("#assistant-21", Markdown).source, "pong")
            user_output = "\n".join(
                str(message.render())
                for message in app.query(".user-message .message-body")
            )
            self.assertIn("ping", user_output)
            user_role = "\n".join(
                str(message.render()) for message in app.query(".user-message .role")
            )
            self.assertIn("你", user_role)
            assistant_output = "\n".join(
                str(message.render()) for message in app.query(".assistant-message .role")
            )
            self.assertIn("MiniClaw", assistant_output)
            self.assertEqual(len(app.query(".conversation-message")), 2)
            telemetry = str(app.query_one("#telemetry", Static).render())
            self.assertIn("1.2k/128k", telemetry)
            self.assertIn("输入 1.5k", telemetry)
            self.assertIn("输出 64", telemetry)
            self.assertIn("工具 2", telemetry)
            self.assertIn("迭代 2", telemetry)
            self.assertIn("432 ms", telemetry)

    async def test_escape_cancels_the_active_turn_and_restores_composer(self) -> None:
        """Esc 只能取消当前 Worker，取消后仍停留在同一个 App。"""
        service = BlockingTurnService()
        runtime = cast(
            "AgentRuntime",
            SimpleNamespace(
                owner_id=1,
                model="deepseek-v4-pro",
                workspace=self.paths.workspace,
                ui_language="zh-CN",
                context_budget_tokens=128_000,
                service=service,
                tool_definitions=(),
            ),
        )
        app = MiniClawApp(self.paths, runtime=runtime)

        async with app.run_test() as pilot:
            composer = app.query_one("#composer", TextArea)
            composer.load_text("wait")
            await pilot.press("enter")
            await asyncio.wait_for(service.started.wait(), timeout=0.5)

            self.assertTrue(composer.disabled)
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertTrue(service.cancelled)
            self.assertFalse(composer.disabled)
            self.assertIs(app.focused, composer)
            self.assertEqual(composer.text, "wait")
            output = "\n".join(
                str(message.render()) for message in app.query(".local-message")
            )
            self.assertIn("已取消", output)

    async def test_failed_turn_restores_long_submitted_text_exactly(self) -> None:
        """长输入在 Provider 失败后必须逐字回填，不能静默丢失。"""
        text = "很长的粘贴内容\n" + "数据" * 32_768
        runtime = cast(
            "AgentRuntime",
            SimpleNamespace(
                owner_id=1,
                model="deepseek-v4-pro",
                workspace=self.paths.workspace,
                ui_language="zh-CN",
                context_budget_tokens=128_000,
                service=FailingTurnService(),
                tool_definitions=(),
            ),
        )
        app = MiniClawApp(self.paths, runtime=runtime)

        async with app.run_test() as pilot:
            composer = app.query_one("#composer", TextArea)
            composer.load_text(text)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(composer.text, text)

    async def test_approval_modal_shows_exact_arguments_and_allows_once(self) -> None:
        """审批只能在原 Turn 返回后，通过同一 Service 选择一次 Allow once。"""
        service = ApprovalTurnService()
        runtime = cast(
            "AgentRuntime",
            SimpleNamespace(
                owner_id=1,
                model="deepseek-v4-pro",
                workspace=self.paths.workspace,
                ui_language="zh-CN",
                context_budget_tokens=128_000,
                service=service,
                tool_definitions=(),
            ),
        )
        app = MiniClawApp(self.paths, runtime=runtime)

        async with app.run_test() as pilot:
            app.query_one("#composer", TextArea).load_text("write")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            modal = app.screen
            self.assertIsInstance(modal, ApprovalModal)
            self.assertEqual(modal.query_one("#approval-deny"), app.focused)
            visible = "\n".join(str(widget.render()) for widget in modal.query(Static))
            self.assertIn("write_file notes.txt", visible)
            self.assertIn("/safe/workspace/notes.txt", visible)
            self.assertIn('"content": "hello"', visible)
            self.assertEqual(len(modal.query("#approval-once")), 1)
            self.assertEqual(len(modal.query("#approval-always")), 0)

            await pilot.click("#approval-once")
            await pilot.pause()
            await app.workers.wait_for_complete()

            self.assertEqual(service.decisions, [(1, 7, ApprovalDecision.ONCE)])
            self.assertEqual(app.query_one(ToolCard).status, "succeeded")
            self.assertEqual(app.query_one("#assistant-32", Markdown).source, "continued")

    async def test_approval_escape_denies_once(self) -> None:
        """审批默认焦点和 Esc 都必须走 Deny，不能静默留下待执行动作。"""
        service = ApprovalTurnService()
        runtime = cast(
            "AgentRuntime",
            SimpleNamespace(
                owner_id=1,
                model="deepseek-v4-pro",
                workspace=self.paths.workspace,
                ui_language="zh-CN",
                context_budget_tokens=128_000,
                service=service,
                tool_definitions=(),
            ),
        )
        app = MiniClawApp(self.paths, runtime=runtime)

        async with app.run_test() as pilot:
            app.query_one("#composer", TextArea).load_text("write")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.press("escape")
            await pilot.pause()
            await app.workers.wait_for_complete()

            self.assertEqual(service.decisions, [(1, 7, ApprovalDecision.DENY)])
            self.assertEqual(app.query_one(ToolCard).status, "denied")

    async def test_approval_modal_only_shows_core_session_and_always_modes(self) -> None:
        """TUI 必须按 Core grant_modes 展示 Session/Always，不能自行放宽。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)
        decisions: list[ApprovalDecision | None] = []
        event = RunEvent(
            "approval_required",
            41,
            {
                "approval_id": 9,
                "call_id": "command-9",
                "tool_name": "run_command",
                "summary": 'run_command ["/usr/bin/open","-a","Lark"]',
                "arguments": {
                    "program": "/usr/bin/open",
                    "args": ["-a", "Lark"],
                    "timeout_seconds": 30,
                },
                "expires_at": "2030-01-01T00:00:00+00:00",
                "grant_modes": ["once", "session", "always"],
            },
        )

        async with app.run_test() as pilot:
            modal = ApprovalModal(event, language="zh-CN")
            app.push_screen(modal, decisions.append)
            await pilot.pause()

            self.assertEqual(len(modal.query("#approval-once")), 1)
            self.assertEqual(len(modal.query("#approval-session")), 1)
            self.assertEqual(len(modal.query("#approval-always")), 1)
            labels = "\n".join(str(button.label) for button in modal.query(Button))
            self.assertIn("本次运行允许", labels)
            self.assertIn("始终允许", labels)
            await pilot.click("#approval-session")
            await pilot.pause()

        self.assertEqual(decisions, [ApprovalDecision.SESSION])

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
            self.assertIn("会话:default", rendered_status)
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
        """调用概要始终可见，参数与结果可以单独展开且不解释 ANSI。"""
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
                        "arguments": {"path": "README.md", "offset": 0},
                    },
                )
            )
            card = app.query_one(ToolCard)
            self.assertIn("状态: requested", card.title)
            await app.on_run_event(
                RunEvent(
                    "tool_started",
                    11,
                    {"call_id": call_id, "tool_name": "read_file"},
                )
            )
            self.assertIn("状态: running", card.title)
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

            self.assertEqual(len(app.query(ToolCard)), 1)
            self.assertEqual(card.status, "succeeded")
            self.assertTrue(card.collapsed)
            self.assertIn("工具: read_file", card.title)
            self.assertIn("状态: succeeded", card.title)

            card.query_one("CollapsibleTitle").focus()
            await pilot.press("enter")
            await pilot.pause()

            detail = str(card.query_one(".trace-detail", Static).render())
            self.assertFalse(card.collapsed)
            self.assertIn('"path": "README.md"', detail)
            self.assertIn("requested -> running -> succeeded", detail)
            self.assertIn("18 ms", detail)
            self.assertIn("safe[2J preview", detail)
            self.assertNotIn("\x1b", detail)

    async def test_reasoning_and_tool_traces_remain_visible_and_toggle_together(self) -> None:
        """Provider reasoning 与 Tool 摘要都保留，Ctrl+O 只折叠详情而不隐藏卡片。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)

        async with app.run_test() as pilot:
            await app.on_run_event(
                RunEvent(
                    "model_reasoning",
                    12,
                    {"text": "inspect\x1b[2J the workspace"},
                )
            )
            await app.on_run_event(
                RunEvent(
                    "tool_requested",
                    12,
                    {
                        "call_id": "read-12",
                        "tool_name": "read_file",
                        "summary": "read_file",
                        "arguments": {"path": "README.md"},
                    },
                )
            )
            await pilot.pause()

            reasoning = app.query_one(ReasoningCard)
            tool = app.query_one(ToolCard)
            self.assertTrue(reasoning.collapsed)
            self.assertTrue(tool.collapsed)
            self.assertTrue(reasoning.display)
            self.assertTrue(tool.display)
            self.assertIn("思考", reasoning.title)
            self.assertNotIn("\x1b", str(reasoning.query_one(Static).render()))

            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertFalse(reasoning.collapsed)
            self.assertFalse(tool.collapsed)
            self.assertTrue(reasoning.display)
            self.assertTrue(tool.display)

            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertTrue(reasoning.collapsed)
            self.assertTrue(tool.collapsed)
            self.assertEqual(len(app.query(Collapsible)), 2)

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
            self.assertIn("模型: deepseek-v4-pro", output)
            self.assertIn("read_file (low)", output)
            self.assertIn("未知命令: /missing", output)

    async def test_language_defaults_to_chinese_and_can_switch_to_english(self) -> None:
        """UI 默认中文，/lang en 只切换界面文案且不调用 Agent。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)

        async with app.run_test() as pilot:
            self.assertIn("会话:default", str(app.query_one("#status", Static).render()))
            self.assertTrue(await app.handle_local_command("/lang en"))
            await pilot.pause()

            status = str(app.query_one("#status", Static).render())
            self.assertIn("session:default", status)
            self.assertNotIn("会话:default", status)
            self.assertTrue(await app.handle_local_command("/lang zh"))
            self.assertIn("会话:default", str(app.query_one("#status", Static).render()))

    async def test_missing_provider_usage_stays_na_and_status_exposes_request_id(self) -> None:
        """Provider 未上报 usage 时不能估算；/status 应保留诊断 request ID。"""
        app = MiniClawApp(self.paths, runtime=self.runtime)

        async with app.run_test() as pilot:
            initial = str(app.query_one("#telemetry", Static).render())
            self.assertIn("上下文 N/A/128k", initial)
            self.assertIn("输入 N/A", initial)
            await app.on_run_event(
                RunEvent(
                    "model_usage",
                    55,
                    {
                        "iteration": 1,
                        "context_tokens": None,
                        "input_tokens": None,
                        "output_tokens": None,
                        "tool_calls": 0,
                        "provider_request_id": "req-missing-usage",
                    },
                )
            )
            await app.handle_local_command("/status")
            await pilot.pause()

            telemetry = str(app.query_one("#telemetry", Static).render())
            output = "\n".join(
                str(message.render()) for message in app.query(".local-message")
            )
            self.assertIn("上下文 N/A/128k", telemetry)
            self.assertIn("输入 N/A", telemetry)
            self.assertIn("输出 N/A", telemetry)
            self.assertIn("provider_request_id: req-missing-usage", output)

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

            with mock.patch("miniclaw.tui.app._load_runtime", return_value=self.runtime):
                await pilot.click("#initialize")
                await pilot.pause()

            self.assertEqual(id(app), original_app_id)
            self.assertTrue(self.paths.database.is_file())
            self.assertTrue(self.paths.config.is_file())
            self.assertEqual(len(app.query("#onboarding")), 0)
            self.assertIs(app.focused, app.query_one("#composer", TextArea))


if __name__ == "__main__":
    unittest.main()
