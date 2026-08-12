"""Controlled Evolution 飞书 Proposal 摘要卡的安全边界测试。"""

import json
import unittest

from lobster0.channels.evolution_cards import ProposalSummary, proposal_summary_card


def _summary(**overrides) -> ProposalSummary:
    """构造一条默认已通过评测的摘要。"""
    values = {
        "proposal_id": 9,
        "target_type": "prompt",
        "target_name": "agent-behavior",
        "status": "approved",
        "rationale": "优先调用工具而不是给操作说明",
        "candidate_hash": "a" * 64,
        "eval_passed": True,
        "eval_total_cases": 54,
        "eval_passed_cases": 54,
        "eval_safety_failures": 0,
    }
    values.update(overrides)
    return ProposalSummary(**values)


class ProposalSummaryCardTest(unittest.TestCase):
    """验证卡片只展示摘要，且不提供任何执行入口。"""

    def test_card_shows_target_status_and_hash_preview(self) -> None:
        """卡片必须展示目标、状态、评测结论与候选指纹前缀。"""
        card = proposal_summary_card(_summary())
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertIn("prompt:agent-behavior", rendered)
        self.assertIn("approved", rendered)
        self.assertIn("54/54", rendered)
        self.assertIn("a" * 12, rendered)

    def test_card_never_carries_an_action_button(self) -> None:
        """apply 必须消费本机 Core Approval；卡片不得成为执行入口。"""
        card = proposal_summary_card(_summary())

        tags = {element.get("tag") for element in card["elements"]}
        self.assertNotIn("action", tags)
        self.assertNotIn("button", json.dumps(card, ensure_ascii=False))

    def test_card_never_carries_the_full_candidate(self) -> None:
        """完整候选正文只能在本机看；卡片只给指纹前缀与查看命令。"""
        card = proposal_summary_card(_summary())
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertNotIn("a" * 64, rendered)
        self.assertIn("lobster0 evolve show 9", rendered)

    def test_unevaluated_proposal_is_never_shown_as_passing(self) -> None:
        """未评测必须明说"未评测"，不能留空让人误以为通过。"""
        card = proposal_summary_card(_summary(eval_passed=None))
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertIn("未评测", rendered)
        self.assertEqual(card["header"]["template"], "grey")

    def test_failed_evaluation_is_visually_distinct(self) -> None:
        """未通过评测的提案必须一眼可辨。"""
        card = proposal_summary_card(
            _summary(eval_passed=False, eval_passed_cases=51, eval_safety_failures=1)
        )

        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("未通过", json.dumps(card, ensure_ascii=False))

    def test_repr_does_not_leak_the_rationale(self) -> None:
        """日志里出现摘要对象时不得带出改动理由。"""
        self.assertNotIn("优先调用工具", repr(_summary()))

    def test_oversized_rationale_is_rejected(self) -> None:
        """超长理由必须拒绝，避免把整段正文推到 IM。"""
        with self.assertRaises(ValueError):
            _summary(rationale="x" * 201)

    def test_invalid_identifiers_are_rejected(self) -> None:
        """编号、目标与哈希都必须是合法形状。"""
        for override in (
            {"proposal_id": 0},
            {"target_name": ""},
            {"candidate_hash": "short"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                _summary(**override)


if __name__ == "__main__":
    unittest.main()
