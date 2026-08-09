"""Channel Agent 公开进度投影的安全与边界测试。"""

import json
import unittest

from miniclaw.agent.events import RunEvent
from miniclaw.channels.progress import (
    ProgressProjector,
    progress_from_metadata,
    progress_to_metadata,
)


class ProgressProjectorTest(unittest.TestCase):
    """验证原始 Runtime 事件只能生成脱敏、有界的公开快照。"""

    def test_tool_sequence_omits_reasoning_secrets_and_raw_output(self) -> None:
        """公开步骤只展示命令意图，不保留推理、凭据值或 Tool 输出。"""
        projector = ProgressProjector(clock=lambda: 1.0)
        projector.apply(RunEvent("turn_started", 7, {"session_id": 1}))
        projector.apply(RunEvent("model_reasoning", 7, {"text": "private chain"}))
        projector.apply(
            RunEvent(
                "model_usage",
                7,
                {"iteration": 2, "input_tokens": 20, "output_tokens": 4, "tool_calls": 1},
            )
        )
        projector.apply(
            RunEvent(
                "tool_requested",
                7,
                {
                    "call_id": "call_1",
                    "tool_name": "run_command",
                    "summary": "run_command",
                    "arguments": {
                        "program": "/Users/owner/.local/bin/lark-cli",
                        "args": [
                            "drive",
                            "+search",
                            "--token",
                            "secret-value",
                            "--page-size",
                            "100",
                        ],
                    },
                },
            )
        )
        projector.apply(
            RunEvent(
                "tool_started",
                7,
                {"call_id": "call_1", "tool_name": "run_command"},
            )
        )
        projector.apply(
            RunEvent(
                "tool_finished",
                7,
                {
                    "call_id": "call_1",
                    "tool_name": "run_command",
                    "status": "succeeded",
                    "duration_ms": 428,
                    "preview": "private tool output",
                },
            )
        )

        progress = projector.finish("你有 327 个飞书文档。", failed=False)

        self.assertEqual(progress.status, "completed")
        self.assertEqual(progress.summary, "任务已完成")
        self.assertEqual(progress.steps[1].title, "查询飞书云空间")
        self.assertIn("lark-cli drive +search", progress.steps[1].detail)
        public = json.dumps(progress_to_metadata(progress), ensure_ascii=False)
        self.assertNotIn("secret-value", public)
        self.assertNotIn("private chain", public)
        self.assertNotIn("private tool output", public)

    def test_known_tools_show_only_allowlisted_targets(self) -> None:
        """文件、搜索、网络与 Memory Tool 只展示安全的目标字段。"""
        cases = (
            ("read_file", {"path": "docs/roadmap.md"}, "查看文件", "docs/roadmap.md"),
            ("grep", {"pattern": "token", "root": "src"}, "搜索文件内容", "src"),
            ("http_get", {"url": "https://example.com/a?q=secret"}, "访问公开网页", "example.com"),
            ("read_memory", {"scope": "recent"}, "查看记忆", "recent"),
            ("memory_get", {"unit_id": "mem-language"}, "查看记忆", "mem-language"),
            (
                "memory_correct",
                {"unit_id": "mem-language", "text": "secret preference"},
                "提议纠错",
                "mem-language",
            ),
            ("memory_review_list", {}, "查看记忆审批", "Owner"),
            ("unknown", {"password": "secret"}, "调用工具", "unknown"),
        )
        for tool_name, arguments, title, expected in cases:
            with self.subTest(tool_name=tool_name):
                projector = ProgressProjector(clock=lambda: 1.0)
                projector.apply(
                    RunEvent(
                        "tool_requested",
                        1,
                        {
                            "call_id": "call",
                            "tool_name": tool_name,
                            "arguments": arguments,
                        },
                    )
                )
                progress = projector.finish(None, failed=True)
                self.assertEqual(progress.steps[-1].title, title)
                self.assertIn(expected, progress.steps[-1].detail)
                self.assertNotIn("secret", progress.steps[-1].detail)

    def test_duplicate_tool_terminal_event_is_failed_and_safely_described(self) -> None:
        """重复 Tool 的终态事件必须清除 pending，且只展示固定安全说明。"""
        projector = ProgressProjector(clock=lambda: 1.0)
        projector.apply(
            RunEvent(
                "model_usage",
                1,
                {"iteration": 2, "tool_calls": 2},
            )
        )
        projector.apply(
            RunEvent(
                "tool_requested",
                1,
                {
                    "call_id": "call_duplicate",
                    "tool_name": "read_file",
                    "arguments": {"path": "private-alias.txt"},
                },
            )
        )
        projector.apply(
            RunEvent(
                "tool_finished",
                1,
                {
                    "call_id": "call_duplicate",
                    "tool_name": "read_file",
                    "status": "failed",
                    "error_code": "duplicate_tool_call",
                    "preview": "private duplicate payload",
                },
            )
        )

        progress = projector.finish(None, failed=True)

        self.assertEqual(progress.steps[-1].status, "failed")
        self.assertEqual(progress.steps[-1].detail, "重复 Tool 请求，已跳过执行")
        self.assertEqual(progress.tool_calls, 2)
        public = json.dumps(progress_to_metadata(progress), ensure_ascii=False)
        self.assertNotIn("private duplicate payload", public)
        self.assertNotIn("private-alias.txt", public)

    def test_steps_and_fields_are_bounded_and_control_characters_removed(self) -> None:
        """恶意长字段与大量调用不能放大卡片或注入控制字符。"""
        projector = ProgressProjector(clock=lambda: 1.0)
        for index in range(20):
            projector.apply(
                RunEvent(
                    "tool_requested",
                    1,
                    {
                        "call_id": f"call_{index}",
                        "tool_name": "read_file",
                        "arguments": {"path": f"bad\x00\n{index}" + "x" * 400},
                    },
                )
            )
        progress = projector.finish("done", failed=False)
        self.assertLessEqual(len(progress.steps), 16)
        self.assertIn("较早步骤", progress.steps[1].title)
        self.assertTrue(all(len(step.detail) <= 240 for step in progress.steps))
        self.assertNotIn("\x00", repr(progress.steps))

    def test_final_answer_preserves_original_whitespace_for_delivery_offsets(self) -> None:
        """最终答案必须保持原始字符前缀，避免卡片偏移把末尾文字重复发送。"""
        answer = "结果如下：\n\n| 项目 | 内容 |\n| --- | --- |\n| 标题 | 文档 A |\n\n需要继续吗？"

        progress = ProgressProjector(clock=lambda: 1.0).finish(answer, failed=False)

        self.assertEqual(progress.final_answer, answer)

    def test_metadata_round_trip_omits_answer_and_rejects_malformed_values(self) -> None:
        """持久化 trace 不复制答案，损坏值按无 trace 处理。"""
        projector = ProgressProjector(clock=lambda: 1.0)
        projector.apply(
            RunEvent(
                "tool_requested",
                1,
                {"call_id": "call", "tool_name": "glob", "arguments": {"pattern": "*.md"}},
            )
        )
        progress = projector.finish("private final answer", failed=False)

        metadata = progress_to_metadata(progress)
        restored_answer = "restored\n\nanswer"
        restored = progress_from_metadata(metadata, restored_answer)

        self.assertNotIn("private final answer", json.dumps(metadata))
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.final_answer, restored_answer)
        self.assertEqual(restored.steps, progress.steps)
        self.assertIsNone(progress_from_metadata({"status": "wrong"}, "answer"))


if __name__ == "__main__":
    unittest.main()
