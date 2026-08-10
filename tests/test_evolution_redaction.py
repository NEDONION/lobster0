"""Feedback 上下文脱敏与哈希的行为测试。"""

import unittest

from lobster0.evolution.redaction import feedback_context_hash, redact_feedback_context


class RedactFeedbackContextTest(unittest.TestCase):
    """验证脱敏覆盖邮箱、绝对路径、Token 与长度上限。"""

    def test_redacts_email_path_and_token(self) -> None:
        """常见三类敏感信息都必须被替换掉，不能原样出现在输出里。"""
        redacted = redact_feedback_context(
            "联系 owner@example.com，日志在 /Users/owner/project/secret.log，"
            "token=sk-abcdef123456"
        )

        self.assertNotIn("owner@example.com", redacted)
        self.assertNotIn("/Users/owner/project/secret.log", redacted)
        self.assertNotIn("sk-abcdef123456", redacted)

    def test_truncates_beyond_bound(self) -> None:
        """超过 4000 字符的正文必须被裁剪并标注截断。"""
        redacted = redact_feedback_context("x" * 5_000)

        self.assertLessEqual(len(redacted), 4_000 + len("<truncated>"))
        self.assertTrue(redacted.endswith("<truncated>"))

    def test_short_plain_text_is_unchanged(self) -> None:
        """不含敏感模式的普通文本必须原样保留。"""
        plain = "普通回答，没有敏感信息"
        self.assertEqual(redact_feedback_context(plain), plain)


class FeedbackContextHashTest(unittest.TestCase):
    """验证 context_hash 稳定、确定且不可逆。"""

    def test_hash_is_stable_and_sensitive_to_content(self) -> None:
        """同一输入哈希稳定；不同输入必须产生不同哈希。"""
        first = feedback_context_hash("同一段脱敏文本")
        second = feedback_context_hash("同一段脱敏文本")
        different = feedback_context_hash("另一段脱敏文本")

        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertRegex(first, r"\A[0-9a-f]{64}\Z")


if __name__ == "__main__":
    unittest.main()
