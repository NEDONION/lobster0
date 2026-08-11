"""飞书 /good、/bad 反馈命令的严格解析、Owner gate 与脱敏测试。"""

import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lobster0.channels.feedback_commands import ChannelFeedbackController
from lobster0.evolution.models import Feedback, FeedbackRating, FeedbackStatus
from lobster0.evolution.repository import EvolutionError


@dataclass(slots=True)
class _FakeDelivery:
    """保存反查到的最小 Delivery 字段。"""

    message_id: int | None


@dataclass(slots=True)
class _FakeMessage:
    """保存反查到的最小消息字段。"""

    id: int
    role: str
    content: str


@dataclass(slots=True)
class FakeDeliveryLookup:
    """按固定平台 message ID 返回预置 Delivery。"""

    by_platform_id: dict[str, _FakeDelivery] = field(default_factory=dict)
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    def find_sent_by_platform_message_id(
        self, *, channel: str, account_id: str, platform_message_id: str, kind: str
    ) -> _FakeDelivery | None:
        """记录调用参数并返回预置结果。"""
        self.calls.append((channel, account_id, platform_message_id, kind))
        return self.by_platform_id.get(platform_message_id)


@dataclass(slots=True)
class FakeMessageLookup:
    """按固定内部 ID 返回预置消息。"""

    by_id: dict[int, _FakeMessage] = field(default_factory=dict)

    def get(self, message_id: int) -> _FakeMessage | None:
        """返回预置消息或 None。"""
        return self.by_id.get(message_id)


@dataclass(slots=True)
class FakeFeedbackLedger:
    """记录 record 调用并可注入固定错误。"""

    error: EvolutionError | None = None
    calls: list[dict[str, object]] = field(default_factory=list)
    next_id: int = 1

    def record(
        self,
        *,
        owner_id: int,
        message_id: int,
        rating: FeedbackRating,
        redacted_reason: str | None,
        context_hash: str,
    ) -> Feedback:
        """回放固定错误，或返回一条构造好的 Feedback。"""
        self.calls.append(
            {
                "owner_id": owner_id,
                "message_id": message_id,
                "rating": rating,
                "redacted_reason": redacted_reason,
                "context_hash": context_hash,
            }
        )
        if self.error is not None:
            raise self.error
        feedback = Feedback(
            id=self.next_id,
            owner_id=owner_id,
            message_id=message_id,
            rating=rating,
            redacted_reason=redacted_reason,
            context_hash=context_hash,
            status=FeedbackStatus.OPEN,
            created_at=datetime(2026, 8, 11, tzinfo=UTC),
            forgotten_at=None,
        )
        self.next_id += 1
        return feedback


class ChannelFeedbackControllerTest(unittest.IsolatedAsyncioTestCase):
    """验证严格解析、Owner gate、回复归属与脱敏。"""

    def setUp(self) -> None:
        """构造三个可注入的假 Repository 与 Controller。"""
        self.deliveries = FakeDeliveryLookup()
        self.messages = FakeMessageLookup()
        self.feedback = FakeFeedbackLedger()
        self.controller = ChannelFeedbackController(
            owner_external_user_id="ou_owner",
            feedback=self.feedback,
            deliveries=self.deliveries,
            messages=self.messages,
        )

    def _seed_target_message(
        self, *, platform_message_id: str = "om_target", content: str = "reply content"
    ) -> None:
        """注册一条可以被回复的、已发送的 assistant message。"""
        self.messages.by_id[42] = _FakeMessage(id=42, role="assistant", content=content)
        self.deliveries.by_platform_id[platform_message_id] = _FakeDelivery(message_id=42)

    async def test_plain_language_is_not_handled(self) -> None:
        """自然语言里出现 good/bad 不应该被当作命令消费。"""
        outcome = await self.controller.handle_text(
            user_id=1,
            actor_external_user_id="ou_owner",
            text="今天天气 good，昨天 bad",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_target",
        )
        self.assertFalse(outcome.handled)
        self.assertEqual(self.feedback.calls, [])

    async def test_malformed_slash_command_returns_usage(self) -> None:
        """/good 或 /bad 前缀但形状不对时必须返回用法提示而不是静默忽略。"""
        outcome = await self.controller.handle_text(
            user_id=1,
            actor_external_user_id="ou_owner",
            text="/goodmorning",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_target",
        )
        self.assertTrue(outcome.handled)
        self.assertIsNotNone(outcome.notice)
        self.assertEqual(self.feedback.calls, [])

    async def test_non_owner_actor_is_rejected(self) -> None:
        """非 Owner 发送 /good 必须被拒绝且不产生任何反馈记录。"""
        self._seed_target_message()
        outcome = await self.controller.handle_text(
            user_id=1,
            actor_external_user_id="ou_someone_else",
            text="/good",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_target",
        )
        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.error_code, "not_owner")
        self.assertEqual(self.feedback.calls, [])

    async def test_command_outside_a_reply_is_rejected(self) -> None:
        """不是回复任何消息时，/good、/bad 都必须拒绝而不是猜测目标。"""
        outcome = await self.controller.handle_text(
            user_id=1,
            actor_external_user_id="ou_owner",
            text="/good",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="",
        )
        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.error_code, "not_a_reply")
        self.assertIn("回复", outcome.notice)
        self.assertEqual(self.feedback.calls, [])

    async def test_reply_to_unknown_message_is_rejected(self) -> None:
        """反查不到 Delivery 或消息不是 assistant 角色时都必须 fail closed。"""
        outcome = await self.controller.handle_text(
            user_id=1,
            actor_external_user_id="ou_owner",
            text="/good",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_unknown",
        )
        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.error_code, "target_not_found")
        self.assertEqual(self.feedback.calls, [])

    async def test_reply_to_user_message_is_rejected(self) -> None:
        """只能评价 assistant message，不能评价用户自己发的消息。"""
        self.messages.by_id[99] = _FakeMessage(id=99, role="user", content="hi")
        self.deliveries.by_platform_id["om_user"] = _FakeDelivery(message_id=99)
        outcome = await self.controller.handle_text(
            user_id=1,
            actor_external_user_id="ou_owner",
            text="/good",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_user",
        )
        self.assertEqual(outcome.error_code, "target_not_found")

    async def test_good_records_feedback_with_no_reason(self) -> None:
        """/good 不带原因，确认文案里不能出现完整上下文。"""
        self._seed_target_message(content="sensitive tool output")
        outcome = await self.controller.handle_text(
            user_id=7,
            actor_external_user_id="ou_owner",
            text="/good",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_target",
        )
        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.feedback_id, 1)
        self.assertEqual(outcome.rating, FeedbackRating.GOOD)
        self.assertEqual(len(self.feedback.calls), 1)
        recorded = self.feedback.calls[0]
        self.assertEqual(recorded["owner_id"], 7)
        self.assertEqual(recorded["message_id"], 42)
        self.assertIsNone(recorded["redacted_reason"])
        self.assertNotIn("sensitive tool output", outcome.notice)

    async def test_bad_with_reason_is_redacted_before_storage(self) -> None:
        """/bad 的原因必须先脱敏（邮箱、路径、Token）再传给 Repository。"""
        self._seed_target_message()
        outcome = await self.controller.handle_text(
            user_id=7,
            actor_external_user_id="ou_owner",
            text="/bad 联系 owner@example.com 看 /Users/owner/secret/report.txt，"
            "token=sk-abcdef123456",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_target",
        )
        self.assertTrue(outcome.handled)
        reason = self.feedback.calls[0]["redacted_reason"]
        self.assertNotIn("owner@example.com", reason)
        self.assertNotIn("/Users/owner/secret/report.txt", reason)
        self.assertNotIn("sk-abcdef123456", reason)

    async def test_duplicate_feedback_returns_stable_notice(self) -> None:
        """重复反馈必须映射成稳定提示，而不是把内部错误码直接展示。"""
        self._seed_target_message()
        self.feedback.error = EvolutionError(
            "feedback_already_recorded", "owner already recorded feedback for this message"
        )
        outcome = await self.controller.handle_text(
            user_id=7,
            actor_external_user_id="ou_owner",
            text="/bad 没有真正调用工具",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_target",
        )
        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.error_code, "feedback_already_recorded")
        self.assertNotIn("feedback_already_recorded", outcome.notice)


if __name__ == "__main__":
    unittest.main()
