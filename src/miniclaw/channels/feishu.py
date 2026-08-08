"""飞书消息到 MiniClaw 内部消息的纯归一化 Adapter。"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from miniclaw.channels.base import IgnoredInbound, InboundMessage
from miniclaw.config import FeishuConfig

_MESSAGE_ID = re.compile(r"om_[A-Za-z0-9_-]{1,128}\Z")
_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_-]{1,128}\Z")
_CHAT_ID = re.compile(r"oc_[A-Za-z0-9_-]{1,128}\Z")


class FeishuMessageView(Protocol):
    """描述官方 SDK InboundMessage 中被 MiniClaw 使用的有限字段。"""

    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_id: str
    sender_type: str | None
    sender_is_bot: bool
    mentioned_bot: bool
    body_text: str
    raw_content_type: str
    create_time: datetime | str | int | None


class FeishuAdapter:
    """执行飞书白名单、消息类型和文本安全校验。"""

    def __init__(
        self,
        config: FeishuConfig,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._allowed_open_ids = frozenset(config.allowed_open_ids)
        self._allowed_chat_ids = frozenset(config.allowed_chat_ids)

    def normalize(
        self,
        message: FeishuMessageView,
    ) -> InboundMessage | IgnoredInbound:
        """把官方 SDK 消息变成内部消息，拒绝所有未明确允许的输入。"""
        if message.sender_is_bot or message.sender_type in {"app", "bot", "system"}:
            return IgnoredInbound("bot_message")
        if message.raw_content_type != "text":
            return IgnoredInbound("unsupported_message")
        if not self._valid_identifiers(message):
            return IgnoredInbound("invalid_message")
        if message.sender_id not in self._allowed_open_ids:
            return IgnoredInbound("sender_denied")
        if message.chat_type == "group":
            group_result = self._validate_group(message)
            if group_result is not None:
                return group_result
        elif message.chat_type != "p2p":
            return IgnoredInbound("unsupported_message")

        text = _safe_text(message.body_text).strip()
        if not text:
            return IgnoredInbound("empty_message")
        if len(text) > self._config.message_max_chars:
            return IgnoredInbound("message_too_large")

        return InboundMessage(
            channel="feishu",
            account_id=self._config.account_id,
            event_id=message.event_id,
            message_id=message.message_id,
            external_user_id=message.sender_id,
            external_conversation_id=message.chat_id,
            chat_type=message.chat_type,
            message_type="text",
            text=text,
            reply_to_message_id=message.message_id,
            received_at=_received_at(message.create_time, self._clock),
        )

    def _valid_identifiers(self, message: FeishuMessageView) -> bool:
        """校验进入持久化层的三类平台标识。"""
        return (
            _MESSAGE_ID.fullmatch(message.message_id) is not None
            and _OPEN_ID.fullmatch(message.sender_id) is not None
            and _CHAT_ID.fullmatch(message.chat_id) is not None
        )

    def _validate_group(self, message: FeishuMessageView) -> IgnoredInbound | None:
        """应用群聊开关、Chat 白名单和明确 mention 三道门。"""
        if not self._config.allow_group_mentions:
            return IgnoredInbound("group_disabled")
        if message.chat_id not in self._allowed_chat_ids:
            return IgnoredInbound("chat_denied")
        if not message.mentioned_bot:
            return IgnoredInbound("mention_required")
        return None


def _safe_text(value: str) -> str:
    """移除能改变日志或终端状态的控制字符，同时保留文本布局。"""
    return "".join(
        character
        for character in value
        if character in "\n\t"
        or ord(character) >= 0x20
        and not 0x7F <= ord(character) <= 0x9F
    )


def _received_at(
    value: datetime | str | int | None,
    clock: Callable[[], datetime],
) -> datetime:
    """规范 SDK 毫秒时间戳，缺失或非法值退化为本地接收时间。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        milliseconds = int(value) if value is not None else 0
    except (TypeError, ValueError):
        milliseconds = 0
    if milliseconds > 0:
        try:
            return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    received_at = clock()
    return received_at if received_at.tzinfo is not None else received_at.replace(tzinfo=UTC)
