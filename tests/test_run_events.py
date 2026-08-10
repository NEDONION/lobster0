"""Agent Runtime 进程内展示事件的行为测试。"""

import asyncio
import unittest

from lobster0.agent.events import (
    RunEvent,
    display_tool_arguments,
    emit,
    tool_display_summary,
)


class RunEventTest(unittest.IsolatedAsyncioTestCase):
    """验证事件按序交付且展示层故障不改变 Core 控制流。"""

    async def test_emit_delivers_the_exact_event_once(self) -> None:
        """删除或重复回调都会让消费者看到错误的运行状态。"""
        captured: list[RunEvent] = []
        event = RunEvent("turn_started", 42, {"session_id": 7})

        async def capture(value: RunEvent) -> None:
            captured.append(value)

        await emit(capture, event)

        self.assertEqual(captured, [event])

    async def test_display_handler_error_is_redacted_and_does_not_escape(self) -> None:
        """TUI 普通异常不能把 Turn 变成失败，也不能把异常私密文本写入日志。"""

        async def fail(_: RunEvent) -> None:
            raise RuntimeError("private-render-value")

        with self.assertLogs("lobster0.agent.events", level="ERROR") as logs:
            await emit(fail, RunEvent("model_text_delta", 42, {"text": "secret"}))

        output = "\n".join(logs.output)
        self.assertIn("model_text_delta", output)
        self.assertNotIn("private-render-value", output)
        self.assertNotIn("secret", output)

    async def test_cancellation_still_propagates_to_the_turn(self) -> None:
        """CancelledError 必须越过展示边界，触发现有 Turn/Tool 取消路径。"""

        async def cancel(_: RunEvent) -> None:
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await emit(cancel, RunEvent("turn_cancelled", 42, {}))

    async def test_missing_handler_is_a_noop(self) -> None:
        """没有 TUI/Channel 订阅时 Core 仍可运行。"""
        await emit(None, RunEvent("turn_finished", 42, {"status": "completed"}))

    def test_browser_display_arguments_hide_typed_text_refs_and_url_query(self) -> None:
        """Browser 活动只展示 action target，不把 typed text、refs 或 URL query 交给 UI。"""
        typed = display_tool_arguments(
            "browser_type",
            {
                "origin": "https://example.com",
                "generation": "private-generation",
                "ref": "@e7",
                "role": "textbox",
                "input_kind": "text",
                "text": "private typed value",
            },
        )
        opened = display_tool_arguments(
            "browser_open",
            {"url": "https://example.com/private?token=must-not-display"},
        )

        self.assertEqual(
            typed,
            {
                "origin": "https://example.com",
                "role": "textbox",
                "input_kind": "text",
                "text": "<redacted>",
            },
        )
        self.assertEqual(opened, {"origin": "https://example.com"})
        self.assertNotIn("private-generation", str(typed))
        self.assertNotIn("must-not-display", str(opened))


class ToolDisplaySummaryTest(unittest.TestCase):
    """摘要必须比工具名更有信息量，同时不泄露正文、凭据与完整参数。"""

    def test_command_summary_reports_program_and_argument_count_only(self) -> None:
        """完整 argv 可能含密钥或路径，只暴露程序名与参数个数。"""
        summary = tool_display_summary(
            "run_command",
            {"program": "/usr/bin/git", "args": ["push", "--force"]},
        )

        self.assertEqual(summary, "run_command git · 2 args")
        self.assertNotIn("--force", summary)

    def test_command_summary_uses_singular_for_one_argument(self) -> None:
        """单参数不应显示成 1 args。"""
        self.assertEqual(
            tool_display_summary("run_command", {"program": "ls", "args": ["-l"]}),
            "run_command ls · 1 arg",
        )

    def test_http_summary_keeps_authority_and_drops_path_and_query(self) -> None:
        """URL 的 path 与 query 常含 token，只保留 authority。"""
        summary = tool_display_summary(
            "http_get",
            {"url": "https://example.com/private?token=must-not-display"},
        )

        self.assertEqual(summary, "http_get https://example.com:443")
        self.assertNotIn("must-not-display", summary)

    def test_browser_type_summary_reports_length_instead_of_typed_text(self) -> None:
        """键入内容可能是密码，只报字符数。"""
        summary = tool_display_summary(
            "browser_type",
            {"origin": "https://example.com", "role": "textbox", "text": "secret-value"},
        )

        self.assertEqual(summary, "browser_type https://example.com:443 · textbox · 12 chars")
        self.assertNotIn("secret-value", summary)

    def test_path_summary_keeps_basename_only(self) -> None:
        """完整路径会泄露目录结构，只保留文件名。"""
        summary = tool_display_summary("read_file", {"path": "/Users/someone/secret/notes.md"})

        self.assertEqual(summary, "read_file notes.md")
        self.assertNotIn("someone", summary)

    def test_unknown_tool_falls_back_without_raising(self) -> None:
        """未知工具或缺字段时必须仍返回可展示的稳定文本。"""
        self.assertEqual(tool_display_summary("mystery_tool", {}), "mystery_tool request")
        self.assertEqual(
            tool_display_summary("run_command", {"program": 42, "args": "not-a-list"}),
            "run_command request",
        )

    def test_summary_always_differs_from_the_bare_tool_name(self) -> None:
        """摘要等于工具名时前端会退化成重复两行，这里保证始终带附加信息。"""
        for tool_name, arguments in (
            ("run_command", {"program": "ls", "args": []}),
            ("http_get", {"url": "https://example.com"}),
            ("read_file", {"path": "/tmp/a.txt"}),
            ("mystery_tool", {}),
        ):
            with self.subTest(tool=tool_name):
                self.assertNotEqual(tool_display_summary(tool_name, arguments), tool_name)


if __name__ == "__main__":
    unittest.main()
