"""Telegram 消息的纯 admission/normalization 边界。"""

from datetime import UTC, datetime
from typing import Protocol

from miniclaw.channels.base import (
    IgnoredInbound,
    InboundMessage,
    sanitize_inbound_text,
)
from miniclaw.config import TelegramConfig

_SIGNED_64_MIN = -(2**63)
_SIGNED_64_MAX = 2**63 - 1


class TelegramMessageView(Protocol):
    """描述 Adapter 允许从 official SDK 边界接收的有限字段。"""

    update_id: int
    message_id: int
    user_id: int
    chat_id: int
    chat_type: str
    text: str | None
    date: datetime
    is_bot: bool
    is_service: bool
    is_edited: bool
    mentioned_bot: bool
    replied_to_bot: bool
    topic_id: int | None
    bot_mention_spans: tuple[tuple[int, int], ...]


class TelegramAdapter:
    """把不可信 Telegram Update 视图转换为 MiniClaw 标准消息。"""

    def __init__(self, config: TelegramConfig, *, bot_user_id: int) -> None:
        """冻结白名单和当前 Bot 身份；构造不读取网络或 SDK。"""
        if not _positive_id(bot_user_id):
            raise ValueError("invalid Telegram bot identity")
        self._config = config
        self._bot_user_id = bot_user_id
        self._allowed_user_ids = frozenset(config.allowed_user_ids)
        self._allowed_chat_ids = frozenset(config.allowed_chat_ids)

    def __repr__(self) -> str:
        """只暴露本地账号，不显示 Bot/user/chat 平台标识。"""
        return f"TelegramAdapter(account_id={self._config.account_id!r})"

    def normalize(
        self,
        message: TelegramMessageView,
    ) -> InboundMessage | IgnoredInbound:
        """按稳定顺序 fail-closed 校验并生成平台无关入站消息。"""
        if message.is_bot:
            return IgnoredInbound("bot_message")
        if message.is_service:
            return IgnoredInbound("service_message")
        if message.is_edited:
            return IgnoredInbound("edited_message")
        if message.text is None:
            return IgnoredInbound("unsupported_message")
        if not self._valid_shape(message):
            return IgnoredInbound("invalid_message")
        if message.user_id not in self._allowed_user_ids:
            return IgnoredInbound("user_not_allowed")

        chat_type: str
        if message.chat_type == "private":
            chat_type = "p2p"
        elif message.chat_type in {"group", "supergroup"}:
            chat_type = "group"
            denied = self._validate_group(message)
            if denied is not None:
                return denied
        else:
            return IgnoredInbound("invalid_message")

        text = _remove_bot_mentions(message.text, message.bot_mention_spans)
        if text is None:
            return IgnoredInbound("invalid_message")
        text = sanitize_inbound_text(text).strip()
        if not text:
            return IgnoredInbound("empty_message")
        if len(text) > self._config.message_max_chars:
            return IgnoredInbound("message_too_large")

        message_key = f"chat:{message.chat_id}:message:{message.message_id}"
        conversation_key = f"chat:{message.chat_id}"
        if message.topic_id is not None:
            conversation_key += f":topic:{message.topic_id}"
        return InboundMessage(
            channel="telegram",
            account_id=self._config.account_id,
            event_id=f"update:{message.update_id}",
            message_id=message_key,
            external_user_id=str(message.user_id),
            external_conversation_id=conversation_key,
            chat_type=chat_type,
            message_type="text",
            text=text,
            reply_to_message_id=message_key,
            received_at=message.date.astimezone(UTC),
        )

    def _valid_shape(self, message: TelegramMessageView) -> bool:
        """校验 Telegram signed IDs、时间和 topic，不接受 bool-as-int。"""
        return (
            _nonnegative_id(message.update_id)
            and _positive_id(message.message_id)
            and _positive_id(message.user_id)
            and _signed_id(message.chat_id)
            and message.chat_id != 0
            and (
                message.topic_id is None
                or _positive_id(message.topic_id)
            )
            and isinstance(message.date, datetime)
            and message.date.tzinfo is not None
            and isinstance(message.bot_mention_spans, tuple)
        )

    def _validate_group(
        self,
        message: TelegramMessageView,
    ) -> IgnoredInbound | None:
        """群聊需要显式开关、chat allowlist 与 Bot addressing。"""
        if not self._config.allow_group_mentions:
            return IgnoredInbound("group_disabled")
        if message.chat_id not in self._allowed_chat_ids:
            return IgnoredInbound("chat_not_allowed")
        if not message.mentioned_bot and not message.replied_to_bot:
            return IgnoredInbound("bot_not_addressed")
        return None


def _remove_bot_mentions(
    text: str,
    spans: tuple[tuple[int, int], ...],
) -> str | None:
    """只删除 SDK mapper 已确认指向当前 Bot 的合法、不重叠 spans。"""
    normalized: list[tuple[int, int]] = []
    for span in spans:
        if (
            not isinstance(span, tuple)
            or len(span) != 2
            or type(span[0]) is not int
            or type(span[1]) is not int
        ):
            return None
        start, end = span
        if start < 0 or end <= start or end > len(text):
            return None
        normalized.append((start, end))
    normalized.sort()
    if any(
        left[1] > right[0]
        for left, right in zip(normalized, normalized[1:], strict=False)
    ):
        return None
    result = text
    for start, end in reversed(normalized):
        result = result[:start] + result[end:]
    return result


def _signed_id(value: object) -> bool:
    return type(value) is int and _SIGNED_64_MIN <= value <= _SIGNED_64_MAX


def _positive_id(value: object) -> bool:
    return _signed_id(value) and value > 0  # type: ignore[operator]


def _nonnegative_id(value: object) -> bool:
    return _signed_id(value) and value >= 0  # type: ignore[operator]
