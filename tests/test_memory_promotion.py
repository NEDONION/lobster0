"""Memory 自动 short-term、重复晋升与冲突治理测试。"""

import unittest

from lobster0.memory.promotion import MemoryPromotion, PromotionEvidence


class MemoryPromotionTest(unittest.TestCase):
    """验证置信度不能单独扩大 Agent 行为或覆盖冲突事实。"""

    def test_first_low_risk_fact_is_short_term_and_independent_repeat_promotes(self) -> None:
        """首见进入 short_term；独立 User source 重复后才晋升 active。"""
        promotion = MemoryPromotion()
        first = promotion.decide(
            PromotionEvidence(
                "preference.language",
                "中文回复",
                "preference",
                "low",
                0.95,
                (),
                False,
            )
        )
        repeated = promotion.decide(
            PromotionEvidence(
                "preference.language",
                "中文回复",
                "preference",
                "low",
                0.95,
                (7,),
                True,
            )
        )

        self.assertEqual(first.status, "short_term")
        self.assertEqual(repeated.status, "active")

    def test_behavior_sensitive_and_conflicting_fact_require_review(self) -> None:
        """行为规则、高敏和同 key 冲突均必须由 Owner review。"""
        promotion = MemoryPromotion()
        cases = (
            PromotionEvidence("behavior.x", "自动执行", "behavior_rule", "high", 1.0, (), False),
            PromotionEvidence("person.health", "健康信息", "fact", "high", 1.0, (), False),
            PromotionEvidence(
                "preference.language",
                "英文回复",
                "preference",
                "low",
                1.0,
                (7,),
                False,
                conflicting_active=True,
            ),
        )
        self.assertEqual(
            [promotion.decide(item).status for item in cases],
            ["review_required", "review_required", "review_required"],
        )


if __name__ == "__main__":
    unittest.main()
