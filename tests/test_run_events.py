"""Agent Runtime 进程内展示事件的行为测试。"""

import asyncio
import unittest

from lobster0.agent.events import RunEvent, display_tool_arguments, emit


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


if __name__ == "__main__":
    unittest.main()
