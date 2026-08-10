"""Discord 紧凑 Markdown 与 progress renderer 测试。"""

import unittest

from lobster0.channels.discord_rendering import (
    render_discord_progress,
    render_discord_text,
)
from lobster0.channels.progress import ProgressProjector


class DiscordRenderingTest(unittest.TestCase):
    """验证 Discord 正文大小、长度和代码块边界。"""

    def test_headings_are_body_sized_and_blank_runs_collapse(self) -> None:
        """代码块外标题应降为正文粗体，连续空行最多保留一个。"""
        content = "# 核心能力\n\n\n## 文件与代码\n\n- 读取 `README.md`"

        rendered = render_discord_text(content, max_chars=2000)

        self.assertEqual(
            rendered,
            "**核心能力**\n\n**文件与代码**\n\n- 读取 `README.md`",
        )

    def test_fenced_code_preserves_heading_syntax_and_blank_lines(self) -> None:
        """围栏代码内部的标题和空行必须逐字保留。"""
        content = "## 示例\n\n```md\n# 代码里的标题\n\n\n- item\n```"

        rendered = render_discord_text(content, max_chars=2000)

        self.assertEqual(
            rendered,
            "**示例**\n\n```md\n# 代码里的标题\n\n\n- item\n```",
        )

    def test_near_limit_heading_falls_back_without_losing_text(self) -> None:
        """粗体标记超限时应退化为普通正文，而不是截断标题。"""
        content = "# 1234567"

        rendered = render_discord_text(content, max_chars=7)

        self.assertEqual(rendered, "1234567")

    def test_over_limit_complete_text_returns_none_instead_of_truncating(self) -> None:
        """完整回答无法容纳时返回 None，让 durable Outbox 负责分片。"""
        self.assertIsNone(render_discord_text("12345678", max_chars=7))

    def test_running_progress_is_compact_and_has_no_trail_or_metrics(self) -> None:
        """Discord 运行态只显示当前阶段，不展开 Claw Trail。"""
        progress = ProgressProjector(clock=lambda: 0.0).snapshot()

        rendered = render_discord_progress(progress, max_chars=2000)

        self.assertEqual(rendered, "⏳ **Lobster0 正在处理**\n正在理解请求")
        self.assertNotIn("Claw Trail", rendered)
        self.assertNotIn("个工具", rendered)


if __name__ == "__main__":
    unittest.main()
