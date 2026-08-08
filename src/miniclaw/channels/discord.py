"""Discord 消息的纯 DM/Guild/Thread admission 边界。"""

from datetime import UTC, datetime
from typing import Protocol

from miniclaw.channels.base import (
    IgnoredInbound,
    InboundMessage,
    sanitize_inbound_text,
)
from miniclaw.config import DiscordConfig

_SNOWFLAKE_MAX = 2**64 - 1


class DiscordMessageView(Protocol):
    """描述 Adapter 可接收的有限 Discord Message 字段。"""

    message_id: int
    author_id: int
    channel_id: int
    guild_id: int | None
    thread_id: int | None
    content: str
    created_at: datetime
    author_is_bot: bool
    webhook_id: int | None
    is_system: bool
    mentioned_bot: bool
    replied_to_bot: bool


class DiscordAdapter:
    """把不可信 Discord Message 视图转换为内部标准消息。"""

    def __init__(self, config: DiscordConfig, *, bot_user_id: int) -> None:
        """冻结白名单和 Bot snowflake，不读取 discord.py 或网络。"""
        if not _snowflake(bot_user_id):
            raise ValueError("invalid Discord bot identity")
        self._config = config
        self._bot_user_id = bot_user_id
        self._allowed_user_ids = frozenset(config.allowed_user_ids)
        self._allowed_guild_ids = frozenset(config.allowed_guild_ids)
        self._allowed_channel_ids = frozenset(config.allowed_channel_ids)

    def __repr__(self) -> str:
        """只显示本地账号，不显示任何平台 snowflake。"""
        return f"DiscordAdapter(account_id={self._config.account_id!r})"

    def normalize(
        self,
        message: DiscordMessageView,
    ) -> InboundMessage | IgnoredInbound:
        """按稳定顺序 fail-closed 校验 DM/Guild/Thread 消息。"""
        if message.author_is_bot:
            return IgnoredInbound("bot_message")
        if message.webhook_id is not None:
            return IgnoredInbound("webhook_message")
        if message.is_system:
            return IgnoredInbound("system_message")
        if not self._valid_shape(message):
            return IgnoredInbound("invalid_message")
        if message.author_id not in self._allowed_user_ids:
            return IgnoredInbound("user_not_allowed")

        if message.guild_id is None:
            chat_type = "p2p"
            conversation = f"channel:{message.channel_id}"
        else:
            chat_type = "group"
            denied = self._validate_guild(message)
            if denied is not None:
                return denied
            conversation = (
                f"guild:{message.guild_id}:channel:{message.channel_id}"
            )
            if message.thread_id is not None:
                conversation += f":thread:{message.thread_id}"

        text = sanitize_inbound_text(
            _remove_bot_mentions(message.content, self._bot_user_id)
        ).strip()
        if not text:
            return IgnoredInbound("empty_message")
        if len(text) > self._config.message_max_chars:
            return IgnoredInbound("message_too_large")
        snowflake = str(message.message_id)
        return InboundMessage(
            channel="discord",
            account_id=self._config.account_id,
            event_id=snowflake,
            message_id=snowflake,
            external_user_id=str(message.author_id),
            external_conversation_id=conversation,
            chat_type=chat_type,
            message_type="text",
            text=text,
            reply_to_message_id=snowflake,
            received_at=message.created_at.astimezone(UTC),
        )

    def _valid_shape(self, message: DiscordMessageView) -> bool:
        """校验 unsigned 64-bit snowflake、Thread 关系与 aware time。"""
        return (
            _snowflake(message.message_id)
            and _snowflake(message.author_id)
            and _snowflake(message.channel_id)
            and (
                message.guild_id is None
                or _snowflake(message.guild_id)
            )
            and (
                message.thread_id is None
                or _snowflake(message.thread_id)
            )
            and not (message.thread_id is not None and message.guild_id is None)
            and isinstance(message.content, str)
            and isinstance(message.created_at, datetime)
            and message.created_at.tzinfo is not None
        )

    def _validate_guild(
        self,
        message: DiscordMessageView,
    ) -> IgnoredInbound | None:
        """Guild 需要开关、guild/channel allowlist 和明确 addressing。"""
        if not self._config.allow_guild_mentions:
            return IgnoredInbound("guild_disabled")
        if message.guild_id not in self._allowed_guild_ids:
            return IgnoredInbound("guild_not_allowed")
        if message.channel_id not in self._allowed_channel_ids:
            return IgnoredInbound("channel_not_allowed")
        if not message.mentioned_bot and not message.replied_to_bot:
            return IgnoredInbound("bot_not_addressed")
        return None


def _remove_bot_mentions(content: str, bot_user_id: int) -> str:
    """只删除 Discord 当前 Bot 的 standard/nickname mention token。"""
    return content.replace(f"<@{bot_user_id}>", "").replace(
        f"<@!{bot_user_id}>",
        "",
    )


def _snowflake(value: object) -> bool:
    return type(value) is int and 0 < value <= _SNOWFLAKE_MAX
