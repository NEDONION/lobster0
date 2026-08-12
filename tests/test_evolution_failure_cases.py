"""从 /bad 反馈生成 failure case 草稿的行为测试。"""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from lobster0.evals.cases import load_cases
from lobster0.evolution.failure_cases import (
    FailureCaseError,
    build_failure_case,
    write_failure_case,
)
from lobster0.evolution.models import Feedback, FeedbackRating, FeedbackStatus


def _feedback(
    *,
    rating: FeedbackRating = FeedbackRating.BAD,
    status: FeedbackStatus = FeedbackStatus.OPEN,
    reason: str | None = "没有真正调用工具",
) -> Feedback:
    """构造一条最小反馈。"""
    return Feedback(
        id=42,
        owner_id=1,
        message_id=7,
        rating=rating,
        redacted_reason=reason,
        context_hash="a" * 64,
        status=status,
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
        forgotten_at=None,
    )


class BuildFailureCaseTest(unittest.TestCase):
    """验证草稿的形状、脱敏与"绝不自动入门禁"这条硬约束。"""

    def test_draft_is_planned_and_live_only(self) -> None:
        """草稿必须是 planned + live：脚本化回复证明不了行为改进。"""
        draft = build_failure_case(
            _feedback(),
            user_query="帮我查看本机的系统版本",
            failing_answer="请自行打开系统设置查看版本号，我这边看不到。",
            tool_names=(),
        )

        self.assertEqual(draft.case_id, "EVO-FAILURE-042")
        self.assertEqual(draft.document["status"], "planned")
        self.assertEqual(draft.document["layers"], ["live"])
        self.assertEqual(draft.document["capability"], "controlled_evolution")
        self.assertIn("owner-review-required", draft.document["tags"])

    def test_query_and_answer_are_redacted(self) -> None:
        """提问与失败回答都必须先脱敏，草稿里不得残留邮箱、路径或 Token。"""
        draft = build_failure_case(
            _feedback(),
            user_query="把 /Users/owner/private/notes.md 发到 owner@example.com",
            failing_answer="我已经把 token=sk-abcdef123456 发给 owner@example.com 了。",
            tool_names=(),
        )

        rendered = draft.to_jsonl()
        self.assertNotIn("owner@example.com", rendered)
        self.assertNotIn("/Users/owner/private/notes.md", rendered)
        self.assertNotIn("sk-abcdef123456", rendered)

    def test_distinctive_fragments_become_answer_excludes(self) -> None:
        """失败回答的显著片段应沉淀为"不要再这样答"。"""
        draft = build_failure_case(
            _feedback(),
            user_query="查看系统版本",
            failing_answer="请自行打开系统设置查看版本号。我这边没有权限读取本机信息。",
            tool_names=(),
        )

        excludes = draft.document["expected"]["answer_excludes"]
        self.assertTrue(excludes)
        self.assertTrue(all(8 <= len(item) <= 60 for item in excludes))

    def test_observed_tools_are_recorded_for_owner_comparison(self) -> None:
        """实际调用过的 Tool 要记下来，供 Owner 对照"本该调用什么"。"""
        draft = build_failure_case(
            _feedback(),
            user_query="查看系统版本",
            failing_answer="我看不到。",
            tool_names=("read_file", "system_info"),
        )

        tags = draft.document["tags"]
        self.assertIn("observed-tool:read_file", tags)
        self.assertIn("observed-tool:system_info", tags)

    def test_source_hash_binds_the_originating_feedback(self) -> None:
        """草稿必须绑定来源反馈的 context hash，保证可追溯。"""
        draft = build_failure_case(
            _feedback(),
            user_query="查看系统版本",
            failing_answer="我看不到。",
            tool_names=(),
        )

        self.assertIn("source-context:" + "a" * 16, draft.document["tags"])

    def test_good_feedback_cannot_become_a_failure_case(self) -> None:
        """好评描述不了"要修什么"。"""
        with self.assertRaises(FailureCaseError) as raised:
            build_failure_case(
                _feedback(rating=FeedbackRating.GOOD),
                user_query="查看系统版本",
                failing_answer="好的。",
                tool_names=(),
            )
        self.assertEqual(raised.exception.code, "feedback_not_bad")

    def test_forgotten_feedback_is_never_revived(self) -> None:
        """Owner 已经要求遗忘的材料不得被重新做成 case。"""
        with self.assertRaises(FailureCaseError) as raised:
            build_failure_case(
                _feedback(status=FeedbackStatus.FORGOTTEN, reason=None),
                user_query="查看系统版本",
                failing_answer="我看不到。",
                tool_names=(),
            )
        self.assertEqual(raised.exception.code, "feedback_forgotten")

    def test_empty_query_is_rejected(self) -> None:
        """脱敏后提问为空时无法成案。"""
        with self.assertRaises(FailureCaseError) as raised:
            build_failure_case(
                _feedback(),
                user_query="   ",
                failing_answer="我看不到。",
                tool_names=(),
            )
        self.assertEqual(raised.exception.code, "empty_query")

    def test_rendering_is_deterministic(self) -> None:
        """同输入必须得到同一行 JSONL。"""
        args = {
            "user_query": "查看系统版本",
            "failing_answer": "请自行打开系统设置查看版本号。",
            "tool_names": ("system_info",),
        }
        first = build_failure_case(_feedback(), **args).to_jsonl()
        second = build_failure_case(_feedback(), **args).to_jsonl()

        self.assertEqual(first, second)


class WriteFailureCaseTest(unittest.TestCase):
    """验证草稿落盘的权限与防覆盖。"""

    def setUp(self) -> None:
        """准备一个隔离目录与一份草稿。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.draft = build_failure_case(
            _feedback(),
            user_query="查看系统版本",
            failing_answer="请自行打开系统设置查看版本号。",
            tool_names=(),
        )

    def test_draft_is_owner_only_and_never_overwrites(self) -> None:
        """草稿必须 0600，且不覆盖已存在文件。"""
        path = self.root / "drafts" / "case.jsonl"

        write_failure_case(self.draft, path)

        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FailureCaseError) as raised:
            write_failure_case(self.draft, path)
        self.assertEqual(raised.exception.code, "draft_exists")

    def test_draft_parses_as_a_valid_versioned_case(self) -> None:
        """草稿必须能被既有 case loader 接受，否则 Owner 提升时才会炸。"""
        suite = self.root / "suite"
        suite.mkdir()
        write_failure_case(self.draft, suite / "draft.v1.jsonl")

        cases = load_cases(suite)

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].id, "EVO-FAILURE-042")
        self.assertEqual(cases[0].status, "planned")

    def test_planned_draft_stays_out_of_the_active_gate(self) -> None:
        """草稿绝不能被算进 active 门禁。"""
        suite = self.root / "suite"
        suite.mkdir()
        write_failure_case(self.draft, suite / "draft.v1.jsonl")

        active = [case for case in load_cases(suite) if case.status == "active"]

        self.assertEqual(active, [])

    def test_written_document_round_trips(self) -> None:
        """写出的 JSON 必须与内存中的草稿一致。"""
        path = self.root / "case.jsonl"
        write_failure_case(self.draft, path)

        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")), self.draft.document
        )


if __name__ == "__main__":
    unittest.main()
