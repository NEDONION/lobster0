"""Telegram 纯 Adapter 的 admission、identity 和文本安全测试。"""

import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from lobster0.channels.base import IgnoredInbound, InboundMessage
from lobster0.channels.telegram import TelegramAdapter
from lobster0.config import TelegramConfig


@dataclass(frozen=True, slots=True)
class FakeTelegramMessage:
    """不依赖 official SDK 的窄消息视图。"""

    update_id: int = 100
    message_id: int = 200
    user_id: int = 300
    chat_id: int = 300
    chat_type: str = "private"
    text: str | None = "你好"
    date: datetime = datetime(2026, 8, 8, tzinfo=UTC)
    is_bot: bool = False
    is_service: bool = False
    is_edited: bool = False
    mentioned_bot: bool = False
    replied_to_bot: bool = False
    topic_id: int | None = None
    bot_mention_spans: tuple[tuple[int, int], ...] = ()


class TelegramAdapterTest(unittest.TestCase):
    """验证 Telegram Update 只有通过 fail-closed admission 才进入 Core。"""

    def setUp(self) -> None:
        self.config = TelegramConfig(
            enabled=True,
            account_id="personal",
            owner_user_id=300,
            allowed_user_ids=(300, 301),
            allowed_chat_ids=(-100123,),
            allow_group_mentions=True,
            message_max_chars=100,
        )
        self.adapter = TelegramAdapter(self.config, bot_user_id=999)

    def test_owner_private_message_has_stable_composite_identity(self) -> None:
        """允许的私聊应转为不含 SDK object 的标准消息。"""
        result = self.adapter.normalize(FakeTelegramMessage(text="  你好\n世界  "))

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.channel, "telegram")
        self.assertEqual(result.account_id, "personal")
        self.assertEqual(result.event_id, "update:100")
        self.assertEqual(result.message_id, "chat:300:message:200")
        self.assertEqual(result.external_user_id, "300")
        self.assertEqual(result.external_conversation_id, "chat:300")
        self.assertEqual(result.chat_type, "p2p")
        self.assertEqual(result.reply_to_message_id, "chat:300:message:200")
        self.assertEqual(result.text, "你好\n世界")
        self.assertEqual(result.received_at.tzinfo, UTC)
        self.assertFalse(hasattr(result, "raw"))

    def test_private_user_must_be_allowlisted(self) -> None:
        """私聊也不能仅因为知道 Bot 就绕过 user allowlist。"""
        result = self.adapter.normalize(FakeTelegramMessage(user_id=777, chat_id=777))

        self.assertEqual(result, IgnoredInbound("user_not_allowed"))

    def test_group_requires_user_chat_and_explicit_addressing(self) -> None:
        """群聊必须同时通过 user/chat allowlist，并 mention 或 reply Bot。"""
        allowed = FakeTelegramMessage(
            user_id=301,
            chat_id=-100123,
            chat_type="supergroup",
            text="@lobster0_bot 帮我总结",
            mentioned_bot=True,
            bot_mention_spans=((0, 13),),
        )
        result = self.adapter.normalize(allowed)

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.chat_type, "group")
        self.assertEqual(result.text, "帮我总结")

        cases = (
            (replace(allowed, user_id=777), "user_not_allowed"),
            (replace(allowed, chat_id=-999), "chat_not_allowed"),
            (
                replace(
                    allowed,
                    mentioned_bot=False,
                    bot_mention_spans=(),
                    replied_to_bot=False,
                ),
                "bot_not_addressed",
            ),
        )
        for message, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.adapter.normalize(message), IgnoredInbound(reason))

        reply = replace(
            allowed,
            text="继续",
            mentioned_bot=False,
            bot_mention_spans=(),
            replied_to_bot=True,
        )
        self.assertIsInstance(self.adapter.normalize(reply), InboundMessage)

    def test_forum_topic_is_an_independent_conversation(self) -> None:
        """同一群的不同 forum topic 不能共享 Agent session。"""
        result = self.adapter.normalize(
            FakeTelegramMessage(
                user_id=301,
                chat_id=-100123,
                chat_type="supergroup",
                mentioned_bot=True,
                topic_id=42,
            )
        )

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.external_conversation_id, "chat:-100123:topic:42")

    def test_only_the_bot_mention_span_is_removed(self) -> None:
        """普通 @name 和正文必须保留，只移除 Transport 确认属于自己的 entity。"""
        text = "问 @alice，@lobster0_bot 帮我整理"
        start = text.index("@lobster0_bot")
        result = self.adapter.normalize(
            FakeTelegramMessage(
                user_id=301,
                chat_id=-100123,
                chat_type="group",
                text=text,
                mentioned_bot=True,
                bot_mention_spans=((start, start + len("@lobster0_bot")),),
            )
        )

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.text, "问 @alice， 帮我整理")

    def test_bot_service_edited_non_text_empty_and_oversized_are_ignored(self) -> None:
        """不支持或危险的 Update 形状必须产生稳定 ignore reason。"""
        cases = (
            (FakeTelegramMessage(is_bot=True), "bot_message"),
            (FakeTelegramMessage(is_service=True), "service_message"),
            (FakeTelegramMessage(is_edited=True), "edited_message"),
            (FakeTelegramMessage(text=None), "unsupported_message"),
            (FakeTelegramMessage(text=" \n\t\x00\x1b\x7f\x85 "), "empty_message"),
            (FakeTelegramMessage(text="x" * 101), "message_too_large"),
        )
        for message, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.adapter.normalize(message), IgnoredInbound(reason))

    def test_invalid_ids_timestamp_chat_type_and_mention_span_fail_closed(self) -> None:
        """bool-as-int、越界 ID、naive time 与伪造 entity 不能进入 Core。"""
        cases = (
            FakeTelegramMessage(update_id=True),
            FakeTelegramMessage(message_id=0),
            FakeTelegramMessage(user_id=2**63),
            FakeTelegramMessage(chat_id=-(2**63) - 1),
            FakeTelegramMessage(chat_type="channel"),
            FakeTelegramMessage(date=datetime(2026, 8, 8)),
            FakeTelegramMessage(bot_mention_spans=((0, 999),)),
        )
        for message in cases:
            with self.subTest(message=message):
                self.assertEqual(
                    self.adapter.normalize(message),
                    IgnoredInbound("invalid_message"),
                )

    def test_control_characters_are_removed_and_repr_is_redacted(self) -> None:
        """输入清洗保留布局，内部 repr 不暴露正文或完整平台 ID。"""
        result = self.adapter.normalize(
            FakeTelegramMessage(
                update_id=87654321,
                message_id=987654321,
                user_id=300,
                chat_id=300,
                text="secret\x00\x1b[31m\n\ttext\x7f\x85",
            )
        )

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.text, "secret[31m\n\ttext")
        for value in ("87654321", "987654321", "300", "secret"):
            self.assertNotIn(value, repr(result))
        self.assertNotIn("999", repr(self.adapter))


if __name__ == "__main__":
    unittest.main()
