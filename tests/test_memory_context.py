"""Memory Recall 注入模型 Context 的预算与完整性测试。"""

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from lobster0.memory.context import MemoryContextSelector
from lobster0.memory.models import SourceRef
from lobster0.memory.repository import MemoryUnit
from lobster0.memory.retrieval import MemoryHit, MemorySearchResult


class MemoryContextSelectorTest(unittest.TestCase):
    """验证 8%/2200 token 上限与整 Unit 选择。"""

    def test_budget_keeps_whole_units_and_stable_order(self) -> None:
        """预算不足时整条丢弃，不能把事实截成半句。"""
        base = MemoryUnit(
            id="mem-a",
            owner_id=1,
            candidate_id=None,
            key="preference.language",
            text="用户偏好中文回复",
            text_hash="a" * 64,
            kind="preference",
            scope="private",
            status="active",
            confidence=1.0,
            sensitivity="low",
            valid_from=datetime.now(UTC),
            valid_until=None,
            supersedes_unit_id=None,
            markdown_hash=None,
            search_shadow="",
            sources=(SourceRef(1, 1, "cli"),),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        result = MemorySearchResult(
            (
                MemoryHit(base, 10.0),
                MemoryHit(replace(base, id="mem-b", text="甲" * 500), 9.0),
            ),
            "owner_local",
        )

        selected = MemoryContextSelector().select(result, provider_window=1_000)

        self.assertEqual(selected.budget_tokens, 80)
        self.assertIn("用户偏好中文回复", selected.text)
        self.assertNotIn("甲", selected.text)
        self.assertEqual(selected.unit_ids, ("mem-a",))

    def test_denied_result_produces_empty_context(self) -> None:
        """Disclosure 拒绝后的空结果不能生成可区分的私人文本。"""
        selected = MemoryContextSelector().select(
            MemorySearchResult((), "group_private_denied"),
            provider_window=32_000,
        )
        self.assertEqual(selected.text, "")
        self.assertEqual(selected.unit_ids, ())


if __name__ == "__main__":
    unittest.main()
