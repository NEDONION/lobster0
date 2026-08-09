"""飞书 Agent Claw Trail 卡片的结构、视觉状态和预算测试。"""

import json
import unittest

from miniclaw.channels.feishu_cards import (
    _safe_markdown_prefix_length,
    render_agent_progress_card,
    render_compact_progress,
)
from miniclaw.channels.progress import AgentProgress, ProgressStep


def _progress(*, status: str = "completed", answer: str = "你有 327 个飞书文档。") -> AgentProgress:
    """构造不依赖 Runtime 的公开进度 fixture。"""
    return AgentProgress(
        status=status,  # type: ignore[arg-type]
        summary="任务已完成" if status == "completed" else "正在执行：查询飞书云空间",
        steps=(
            ProgressStep(1, None, "理解请求", "已识别目标", "succeeded"),
            ProgressStep(
                2,
                "call_1",
                "查询飞书云空间",
                "lark-cli drive +search --page-size 100",
                "succeeded" if status == "completed" else "running",
                428 if status == "completed" else None,
            ),
        ),
        public_text="",
        final_answer=answer,
        iterations=2,
        tool_calls=1,
        input_tokens=20,
        output_tokens=4,
        duration_ms=851,
    )


class FeishuAgentCardTest(unittest.TestCase):
    """验证 Card 2.0 输出和超长正文的无损续发偏移。"""

    def test_completed_card_contains_claw_trail_answer_and_metrics(self) -> None:
        """完成态单卡应同时呈现轨迹、答案、工具与模型轮次。"""
        progress = _progress()

        rendered_card = render_agent_progress_card(progress)
        card = rendered_card.card
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "green")
        self.assertEqual(card["header"]["title"]["content"], "MiniClaw · 已完成")
        self.assertIn("Claw Trail", rendered)
        self.assertIn("查询飞书云空间", rendered)
        self.assertIn("你有 327 个飞书文档", rendered)
        self.assertIn("2 步 · 1 次工具请求 · 2 轮模型", rendered)
        self.assertLessEqual(len(rendered.encode("utf-8")), 20 * 1024)
        self.assertEqual(rendered_card.visible_answer_chars, len(progress.final_answer))

    def test_statuses_map_to_distinct_headers(self) -> None:
        """运行、完成、未完成和等待态必须有稳定的颜色与标题。"""
        expected = {
            "running": ("blue", "MiniClaw · 执行中"),
            "completed": ("green", "MiniClaw · 已完成"),
            "incomplete": ("red", "MiniClaw · 未完成"),
            "waiting": ("orange", "MiniClaw · 等待中"),
        }
        for status, (template, title) in expected.items():
            with self.subTest(status=status):
                card = render_agent_progress_card(_progress(status=status, answer="")).card
                self.assertEqual(card["header"]["template"], template)
                self.assertEqual(card["header"]["title"]["content"], title)

    def test_long_answer_returns_exact_visible_prefix_and_stays_under_budget(self) -> None:
        """超长答案按 Unicode 字符边界裁剪，并返回精确续发偏移。"""
        answer = "飞书文档🙂" * 10_000
        rendered = render_agent_progress_card(_progress(answer=answer))
        encoded = json.dumps(rendered.card, ensure_ascii=False).encode("utf-8")
        visible = answer[: rendered.visible_answer_chars]

        self.assertLessEqual(len(encoded), 20 * 1024)
        self.assertGreater(rendered.visible_answer_chars, 0)
        self.assertLess(rendered.visible_answer_chars, len(answer))
        self.assertIn(visible[:100], encoded.decode("utf-8"))

    def _final_content(self, answer: str) -> str:
        """返回完成卡的最终回答 Markdown 内容。"""
        card = render_agent_progress_card(_progress(answer=answer)).card
        elements = card["body"]["elements"]
        return next(
            element["content"]
            for element in elements
            if isinstance(element, dict)
            and isinstance(element.get("content"), str)
            and element["content"].startswith("**最终回答**")
        )

    def test_final_answer_preserves_commonmark_and_only_converts_tables(self) -> None:
        """最终回答保留常见 Markdown，仅将 fence 外的表格降级为 bullet。"""
        answer = (
            "# 结论\n\n"
            "普通段落含 **粗体**、[链接](https://example.com) 和 `code`。\n\n"
            "> 引用\n\n"
            "1. 第一项\n2. 第二项\n\n"
            "- [x] 已完成\n- [ ] 待处理\n\n"
            "```python\nprint('| not a table |')\n<at id=all></at>\n```\n\n"
            "| 项目 | 内容 |\n"
            "| --- | --- |\n"
            "| 标题 | 202608_求职公司调研 |\n"
            "| 类型 | <at id=all></at> |\n\n"
            "<at id=all></at>"
        )

        final_content = self._final_content(answer)

        self.assertIn("# 结论", final_content)
        self.assertIn("普通段落含 **粗体**、[链接](https://example.com) 和 `code`。", final_content)
        self.assertIn("> 引用", final_content)
        self.assertIn("1. 第一项\n2. 第二项", final_content)
        self.assertIn("- [x] 已完成\n- [ ] 待处理", final_content)
        self.assertIn("```python\nprint('| not a table |')\n<at id=all></at>\n```", final_content)
        self.assertIn("- **标题**：202608_求职公司调研", final_content)
        self.assertIn("- **类型**：&lt;at id=all&gt;&lt;/at&gt;", final_content)
        self.assertNotIn("| --- |", final_content)
        self.assertNotIn("| 项目 | 内容 |", final_content)
        self.assertIn("&lt;at id=all&gt;&lt;/at&gt;", final_content)

    def test_table_tokenizer_preserves_escaped_and_inline_code_pipes(self) -> None:
        """表格单元格中的转义管道和不同长度的行内 code span 不得被拆坏。"""
        answer = (
            "| 项目 | 内容 |\n"
            "| --- | --- |\n"
            "| 路径\\|别名 | ``<at id=all></at> | `inner|pipe` `` <at id=all></at> |"
        )

        final_content = self._final_content(answer)

        self.assertIn("- **路径\\|别名**：``<at id=all></at> | `inner|pipe` ``", final_content)
        self.assertIn("&lt;at id=all&gt;&lt;/at&gt;", final_content)
        self.assertNotIn("| --- |", final_content)

    def test_table_tokenizer_accepts_all_outer_pipe_variants(self) -> None:
        """表格无论有无左右外框管道都应识别，且必须保留单元格内容。"""
        variants = (
            "项目 | 内容\n--- | ---\n标题 | 无外框",
            "| 项目 | 内容\n| --- | ---\n| 标题 | 仅左框",
            "项目 | 内容 |\n--- | --- |\n标题 | 仅右框 |",
            "| 项目 | 内容 |\n| --- | --- |\n| 标题 | 双边框 |",
        )

        for answer in variants:
            with self.subTest(answer=answer):
                final_content = self._final_content(answer)
                self.assertIn("- **标题**：", final_content)
                self.assertNotIn("--- | ---", final_content)

    def test_table_tokenizer_accepts_one_column_table_with_double_outer_pipes(self) -> None:
        """双外框的一列表格应降级为 bullet，并要求 separator 与有效数据行。"""
        answer = "| 名称 |\n| --- |\n| 文档 A |\n| 文档 B |"

        final_content = self._final_content(answer)

        self.assertIn("- **名称**：文档 A", final_content)
        self.assertIn("- **名称**：文档 B", final_content)
        self.assertNotIn("| --- |", final_content)

    def test_plain_pipe_text_without_table_contract_is_preserved(self) -> None:
        """只有普通管道文本但缺少 separator 或有效数据行时不得误判为表格。"""
        answers = (
            "普通 | 管道文本\n下一段仍是正文",
            "| 名称 |\n| --- |\n| |\n后续正文",
        )

        for answer in answers:
            with self.subTest(answer=answer):
                self.assertIn(answer, self._final_content(answer))

    def test_internal_card_fields_are_strict_plain_text_markdown(self) -> None:
        """内部摘要与轨迹字段必须实体化 mention 和预编码实体，不能变回标签。"""
        progress = AgentProgress(
            status="running",
            summary=(
                "摘要 <at id=all></at> &lt;at id=all&gt; `code` \\ "
                "**bold** [link](https://example.com)"
            ),
            steps=(
                ProgressStep(
                    1,
                    None,
                    "标题 <at id=all></at> &lt;",
                    "详情 <at id=all></at> &lt;",
                    "running",
                ),
            ),
            public_text="过程 <at id=all></at> &lt;",
            final_answer="",
            iterations=1,
            tool_calls=0,
            input_tokens=None,
            output_tokens=None,
            duration_ms=1,
        )

        rendered = json.dumps(
            render_agent_progress_card(progress).card,
            ensure_ascii=False,
        )

        self.assertNotIn("<at id=all>", rendered)
        self.assertIn("&lt;at id=all&gt;&lt;/at&gt;", rendered)
        self.assertIn("&amp;lt;at id=all&amp;gt;", rendered)
        self.assertIn("\\\\`code\\\\`", rendered)
        self.assertIn("\\\\", rendered)
        self.assertIn("\\\\*\\\\*bold\\\\*\\\\*", rendered)
        self.assertIn("\\\\[link\\\\]\\\\(https://example\\\\.com\\\\)", rendered)

    def test_incomplete_or_mismatched_table_rows_are_not_silently_lost(self) -> None:
        """无数据行的伪表格保留原文，真实表格只消费列宽匹配的数据行。"""
        incomplete = "| 项目 | 内容 |\n| --- | --- |\n| | |\n普通段落"
        mixed = "| 字段 | 值 |\n| --- | --- |\n| 正常 | 数据 |\n| 多余 | 一 | 二 |"

        incomplete_content = self._final_content(incomplete)
        mixed_content = self._final_content(mixed)

        self.assertIn("| 项目 | 内容 |\n| --- | --- |\n| | |\n普通段落", incomplete_content)
        self.assertIn("- **正常**：数据", mixed_content)
        self.assertIn("| 多余 | 一 | 二 |", mixed_content)

    def test_raw_html_escapes_only_outside_inline_code_spans(self) -> None:
        """不同长度的有效行内 code span 保留原文，代码外 mention 必须转义。"""
        answer = "`<at id=all></at>` 和 ``<at id=all></at> `keep` ``，外部 <at id=all></at>"

        final_content = self._final_content(answer)

        self.assertIn("`<at id=all></at>`", final_content)
        self.assertIn("``<at id=all></at> `keep` ``", final_content)
        self.assertIn("外部 &lt;at id=all&gt;&lt;/at&gt;", final_content)

    def test_long_fenced_answer_closes_visible_fence_and_keeps_exact_tail(self) -> None:
        """截断后的卡补齐代码 fence，但续发偏移始终对应原始字符串前缀。"""
        answer = "# 日志\n\n```python\n" + ("print('🙂')\n" * 4_000) + "```\n\n尾部结论"

        rendered = render_agent_progress_card(_progress(answer=answer))
        final_content = next(
            element["content"]
            for element in rendered.card["body"]["elements"]
            if isinstance(element, dict)
            and isinstance(element.get("content"), str)
            and element["content"].startswith("**最终回答**")
        )

        self.assertGreater(rendered.visible_answer_chars, 0)
        self.assertLess(rendered.visible_answer_chars, len(answer))
        visible = rendered.visible_answer_chars
        self.assertEqual(answer[:visible] + answer[visible:], answer)
        self.assertEqual(final_content.count("```"), 2)
        card_bytes = len(json.dumps(rendered.card, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(card_bytes, 20 * 1024)

    def test_safe_prefix_prefers_newline_without_emptying_single_line(self) -> None:
        """结构化裁剪优先换行边界，但单行长文本仍能返回最大安全前缀。"""
        self.assertEqual(_safe_markdown_prefix_length("第一段\n第二段", 5), 4)
        self.assertEqual(_safe_markdown_prefix_length("没有换行的长句", 4), 4)

    def test_final_answer_preserves_code_while_compact_renderer_shows_steps(self) -> None:
        """最终回答保留行内代码，紧凑预览同样展示步骤。"""
        progress = _progress(answer="答案含有 `code` 和反斜线 \\")

        card = render_agent_progress_card(progress).card
        elements = card["body"]["elements"]
        rendered = "\n".join(
            element.get("content", "")
            for element in elements
            if isinstance(element, dict) and isinstance(element.get("content"), str)
        )
        compact = render_compact_progress(progress)

        self.assertIn("`code`", rendered)
        self.assertIn("Claw Trail", compact)
        self.assertIn("查询飞书云空间", compact)


if __name__ == "__main__":
    unittest.main()
