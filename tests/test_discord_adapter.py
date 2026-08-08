"""Discord 纯 Adapter 的 DM/Guild/Thread admission 测试。"""

import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from miniclaw.channels.base import IgnoredInbound, InboundMessage
from miniclaw.channels.discord import DiscordAdapter
from miniclaw.config import DiscordConfig


@dataclass(frozen=True, slots=True)
class FakeDiscordMessage:
    """不 import discord.py 的窄消息视图。"""

    message_id: int = 700
    author_id: int = 300
    channel_id: int = 400
    guild_id: int | None = None
    thread_id: int | None = None
    content: str = "你好"
    created_at: datetime = datetime(2026, 8, 8, tzinfo=UTC)
    author_is_bot: bool = False
    webhook_id: int | None = None
    is_system: bool = False
    mentioned_bot: bool = False
    replied_to_bot: bool = False


class DiscordAdapterTest(unittest.TestCase):
    """验证 Discord 消息在进入 Core 前的 fail-closed 规则。"""

    def setUp(self) -> None:
        self.config = DiscordConfig(
            enabled=True,
            account_id="personal",
            owner_user_id=300,
            allowed_user_ids=(300, 301),
            allowed_guild_ids=(500,),
            allowed_channel_ids=(400,),
            allow_guild_mentions=True,
            message_max_chars=100,
        )
        self.adapter = DiscordAdapter(self.config, bot_user_id=999)

    def test_owner_dm_is_normalized_with_channel_conversation(self) -> None:
        """Owner DM 应形成稳定 snowflake identity，不携带 SDK object。"""
        result = self.adapter.normalize(FakeDiscordMessage(content="  你好\n世界  "))

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.channel, "discord")
        self.assertEqual(result.account_id, "personal")
        self.assertEqual(result.event_id, "700")
        self.assertEqual(result.message_id, "700")
        self.assertEqual(result.external_user_id, "300")
        self.assertEqual(result.external_conversation_id, "channel:400")
        self.assertEqual(result.chat_type, "p2p")
        self.assertEqual(result.reply_to_message_id, "700")
        self.assertEqual(result.text, "你好\n世界")
        self.assertEqual(result.received_at.tzinfo, UTC)
        self.assertFalse(hasattr(result, "raw"))

    def test_dm_author_must_be_allowlisted(self) -> None:
        """非白名单用户不能通过 DM 触发个人 Agent。"""
        self.assertEqual(
            self.adapter.normalize(FakeDiscordMessage(author_id=777)),
            IgnoredInbound("user_not_allowed"),
        )

    def test_guild_requires_user_guild_channel_and_addressing(self) -> None:
        """Guild 消息必须通过三层 allowlist，并 mention 或 reply Bot。"""
        allowed = FakeDiscordMessage(
            author_id=301,
            guild_id=500,
            channel_id=400,
            content="<@999> 帮我总结",
            mentioned_bot=True,
        )
        result = self.adapter.normalize(allowed)
        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.chat_type, "group")
        self.assertEqual(result.text, "帮我总结")
        self.assertEqual(result.external_conversation_id, "guild:500:channel:400")

        cases = (
            (replace(allowed, author_id=777), "user_not_allowed"),
            (replace(allowed, guild_id=888), "guild_not_allowed"),
            (replace(allowed, channel_id=888), "channel_not_allowed"),
            (
                replace(allowed, content="hello", mentioned_bot=False),
                "bot_not_addressed",
            ),
        )
        for message, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.adapter.normalize(message), IgnoredInbound(reason))

        reply = replace(
            allowed,
            content="继续",
            mentioned_bot=False,
            replied_to_bot=True,
        )
        self.assertIsInstance(self.adapter.normalize(reply), InboundMessage)

    def test_thread_inherits_parent_allowlist_but_gets_independent_conversation(self) -> None:
        """Thread 使用 parent channel admission，但 session identity 加 thread snowflake。"""
        result = self.adapter.normalize(
            FakeDiscordMessage(
                author_id=301,
                guild_id=500,
                channel_id=400,
                thread_id=600,
                mentioned_bot=True,
            )
        )

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(
            result.external_conversation_id,
            "guild:500:channel:400:thread:600",
        )

    def test_only_current_bot_mentions_are_removed(self) -> None:
        """只移除当前 Bot 的两种 mention 形式，其他用户 mention 保留。"""
        result = self.adapter.normalize(
            FakeDiscordMessage(
                author_id=301,
                guild_id=500,
                channel_id=400,
                content="问 <@123>，<@!999> 帮我整理",
                mentioned_bot=True,
            )
        )

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.text, "问 <@123>， 帮我整理")

    def test_bot_webhook_system_empty_control_and_oversized_are_ignored(self) -> None:
        """回声和不支持形状使用稳定 reason 静默过滤。"""
        cases = (
            (FakeDiscordMessage(author_is_bot=True), "bot_message"),
            (FakeDiscordMessage(webhook_id=800), "webhook_message"),
            (FakeDiscordMessage(is_system=True), "system_message"),
            (FakeDiscordMessage(content=" \n\t\x00\x1b\x7f\x85 "), "empty_message"),
            (FakeDiscordMessage(content="x" * 101), "message_too_large"),
        )
        for message, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.adapter.normalize(message), IgnoredInbound(reason))

    def test_invalid_snowflake_time_and_thread_shape_fail_closed(self) -> None:
        """bool、零/越界 snowflake、naive time 和 DM thread 都不能进入 Core。"""
        cases = (
            FakeDiscordMessage(message_id=True),
            FakeDiscordMessage(author_id=0),
            FakeDiscordMessage(channel_id=2**64),
            FakeDiscordMessage(guild_id=-1),
            FakeDiscordMessage(thread_id=0),
            FakeDiscordMessage(thread_id=600, guild_id=None),
            FakeDiscordMessage(created_at=datetime(2026, 8, 8)),
        )
        for message in cases:
            with self.subTest(message=message):
                self.assertEqual(
                    self.adapter.normalize(message),
                    IgnoredInbound("invalid_message"),
                )

    def test_control_characters_are_removed_and_repr_is_redacted(self) -> None:
        """正文清洗保留布局，repr 不显示正文或完整 snowflake。"""
        result = self.adapter.normalize(
            FakeDiscordMessage(
                message_id=987654321,
                content="secret\x00\x1b[31m\n\ttext\x7f\x85",
            )
        )

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.text, "secret[31m\n\ttext")
        for value in ("987654321", "300", "400", "secret"):
            self.assertNotIn(value, repr(result))
        self.assertNotIn("999", repr(self.adapter))


if __name__ == "__main__":
    unittest.main()
