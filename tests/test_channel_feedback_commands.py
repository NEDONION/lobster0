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
    session_id: int = 1


@dataclass(slots=True)
class FakeDeliveryLookup:
    """按固定平台 message ID 与 delivery kind 返回预置 Delivery。"""

    by_platform_id: dict[str, _FakeDelivery] = field(default_factory=dict)
    by_card_platform_id: dict[str, _FakeDelivery] = field(default_factory=dict)
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    def find_sent_by_platform_message_id(
        self, *, channel: str, account_id: str, platform_message_id: str, kind: str
    ) -> _FakeDelivery | None:
        """记录调用参数并按 kind 返回对应预置结果。"""
        self.calls.append((channel, account_id, platform_message_id, kind))
        source = self.by_platform_id if kind == "message" else self.by_card_platform_id
        return source.get(platform_message_id)


@dataclass(slots=True)
class FakeMessageLookup:
    """按固定内部 ID 返回预置消息。"""

    by_id: dict[int, _FakeMessage] = field(default_factory=dict)

    created_reasons: list[tuple[int, str]] = field(default_factory=list)
    next_reason_id: int = 900
    reason_error: Exception | None = None

    def get(self, message_id: int) -> _FakeMessage | None:
        """返回预置消息或 None。"""
        return self.by_id.get(message_id)

    def create_feedback_reason(self, session_id: int, content: str) -> _FakeMessage:
        """记录 Owner 原话的落库调用，或回放注入的失败。"""
        if self.reason_error is not None:
            raise self.reason_error
        self.created_reasons.append((session_id, content))
        message = _FakeMessage(
            id=self.next_reason_id, role="user", content=content, session_id=session_id
        )
        self.next_reason_id += 1
        self.by_id[message.id] = message
        return message


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
        reason_message_id: int | None = None,
    ) -> Feedback:
        """回放固定错误，或返回一条构造好的 Feedback。"""
        self.calls.append(
            {
                "owner_id": owner_id,
                "message_id": message_id,
                "rating": rating,
                "redacted_reason": redacted_reason,
                "context_hash": context_hash,
                "reason_message_id": reason_message_id,
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
        # 不能留死胡同：必须说明可能的原因和下一步怎么做。
        assert outcome.notice is not None
        self.assertIn("原因", outcome.notice)
        self.assertIn("重试", outcome.notice)

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

    async def test_bad_reason_is_persisted_as_an_owner_message(self) -> None:
        """Owner 的原话必须落成同会话的 user message，并绑定到该条反馈。

        这是 Memory correction candidate 唯一合法的出处来源：``SourceRef`` 只接受
        可核验的真实消息，编不出来。角色必须是 user——记成 assistant 等于把
        Owner 说的话安到模型头上，出处就假了。
        """
        self._seed_target_message()
        outcome = await self.controller.handle_text(
            user_id=7,
            actor_external_user_id="ou_owner",
            text="/bad 你记错了，我的部署机器是 mac 不是 linux",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_target",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(
            self.messages.created_reasons,
            [(1, "你记错了，我的部署机器是 mac 不是 linux")],
        )
        reason_message_id = self.feedback.calls[0]["reason_message_id"]
        self.assertIsNotNone(reason_message_id)
        stored = self.messages.by_id[reason_message_id]
        self.assertEqual(stored.role, "user")
        # 落库的是原话而不是脱敏版：脱敏是给 Proposal 展示用的，
        # 出处必须是 Owner 真正说过的那句。
        self.assertEqual(stored.content, "你记错了，我的部署机器是 mac 不是 linux")

    async def test_good_without_reason_persists_no_owner_message(self) -> None:
        """``/good`` 与不带原因的 ``/bad`` 没有原话可存，不得凭空造一条消息。"""
        self._seed_target_message()
        for text in ("/good", "/bad"):
            with self.subTest(command=text):
                self.messages.created_reasons.clear()
                self.feedback.calls.clear()
                await self.controller.handle_text(
                    user_id=7,
                    actor_external_user_id="ou_owner",
                    text=text,
                    channel="feishu",
                    account_id="default",
                    reply_to_platform_message_id="om_target",
                )
                self.assertEqual(self.messages.created_reasons, [])
                self.assertIsNone(self.feedback.calls[0]["reason_message_id"])

    async def test_reason_persist_failure_still_records_the_feedback(self) -> None:
        """存原话失败只该让这条反馈不能用于改记忆，不该让 ``/bad`` 整体失败。

        Owner 表达不满是第一位的；出处缺失是能力降级，不是错误。
        """
        self._seed_target_message()
        self.messages.reason_error = RuntimeError("disk full")

        outcome = await self.controller.handle_text(
            user_id=7,
            actor_external_user_id="ou_owner",
            text="/bad 你记错了，我用的是 mac",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_target",
        )

        self.assertTrue(outcome.handled)
        self.assertIsNone(outcome.error_code)
        self.assertEqual(len(self.feedback.calls), 1)
        self.assertIsNone(self.feedback.calls[0]["reason_message_id"])

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


class FeedbackOnCardReplyTest(unittest.IsolatedAsyncioTestCase):
    """Owner 回复的是 Experience 直接发出的卡片，而不是普通文本消息。

    真实事故：飞书里 /good 一直提示"没有找到这条回答"。原因是 Owner 看到并回复的是那张
    绿色进度卡，而反查只认 ``delivery_kind='message'``；卡片的平台 ID 当时甚至没有落库。
    """

    def setUp(self) -> None:
        """构造只登记了卡片、没有普通文本投递的场景。"""
        self.deliveries = FakeDeliveryLookup()
        self.messages = FakeMessageLookup()
        self.feedback = FakeFeedbackLedger()
        self.controller = ChannelFeedbackController(
            owner_external_user_id="ou_owner",
            feedback=self.feedback,
            deliveries=self.deliveries,
            messages=self.messages,
        )
        self.messages.by_id[7] = _FakeMessage(id=7, role="assistant", content="卡片里的回答")
        self.deliveries.by_card_platform_id["om_card"] = _FakeDelivery(message_id=7)

    async def test_reply_to_a_card_records_feedback(self) -> None:
        """回复卡片必须能记录反馈，而不是报"没有找到这条回答"。"""
        outcome = await self.controller.handle_text(
            user_id=3,
            actor_external_user_id="ou_owner",
            text="/good",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_card",
        )

        self.assertTrue(outcome.handled)
        self.assertIsNone(outcome.error_code)
        self.assertEqual(outcome.feedback_id, 1)
        self.assertEqual(self.feedback.calls[0]["message_id"], 7)

    async def test_message_kind_is_tried_before_card(self) -> None:
        """普通文本仍然优先命中，卡片只是补充来源。"""
        await self.controller.handle_text(
            user_id=3,
            actor_external_user_id="ou_owner",
            text="/good",
            channel="feishu",
            account_id="default",
            reply_to_platform_message_id="om_card",
        )

        self.assertEqual([call[3] for call in self.deliveries.calls], ["message", "card"])
