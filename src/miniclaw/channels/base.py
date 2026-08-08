"""平台 Adapter、Gateway 和 Delivery 之间的最小公共契约。"""

import re
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal, Protocol, runtime_checkable

type ChatType = Literal["p2p", "group"]
type MessageType = Literal["text"]
type DeliveryKind = Literal["message", "card", "approval", "typing"]

_ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_CHANNEL_NAMES = frozenset({"feishu", "telegram", "discord"})


def sanitize_inbound_text(value: str) -> str:
    """移除会改变日志/终端状态的控制字符，同时保留换行与 Tab。"""
    return "".join(
        character
        for character in value
        if character in "\n\t"
        or ord(character) >= 0x20
        and not 0x7F <= ord(character) <= 0x9F
    )


class ChannelTransportError(RuntimeError):
    """表示已经映射为稳定码且不包含 SDK 原始正文的平台错误。"""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        unknown: bool = False,
        retry_after: float | None = None,
    ) -> None:
        if retry_after is not None and (
            type(retry_after) not in {int, float}
            or not isfinite(retry_after)
            or retry_after < 0
        ):
            raise ValueError("retry_after must be a finite non-negative number")
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.unknown = unknown
        self.retry_after = None if retry_after is None else float(retry_after)

    def __repr__(self) -> str:
        """仅显示稳定错误码和恢复属性。"""
        return (
            "ChannelTransportError("
            f"code={self.code!r}, retryable={self.retryable!r}, "
            f"unknown={self.unknown!r}, retry_after={self.retry_after!r})"
        )


@dataclass(frozen=True, slots=True)
class ChannelLimits:
    """保存单个平台 Manager/Experience 共用的非秘密本地预算。"""

    channel: str
    account_id: str
    queue_size: int
    worker_count: int
    message_max_chars: int
    progress_update_interval: float

    def __post_init__(self) -> None:
        """拒绝未知平台、非法本地账号和可被 bool 绕过的预算。"""
        if self.channel not in _CHANNEL_NAMES:
            raise ValueError("unsupported channel")
        if not isinstance(self.account_id, str) or _ACCOUNT_ID.fullmatch(self.account_id) is None:
            raise ValueError("invalid channel account_id")
        integer_limits = (self.queue_size, self.worker_count, self.message_max_chars)
        if any(type(value) is not int or value <= 0 for value in integer_limits):
            raise ValueError("channel integer limits must be positive")
        if (
            type(self.progress_update_interval) not in {int, float}
            or self.progress_update_interval <= 0
        ):
            raise ValueError("channel progress interval must be positive")


@dataclass(frozen=True, slots=True, repr=False)
class InboundMessage:
    """保存通过平台校验和清洗后的单条用户消息。"""

    channel: str
    account_id: str
    event_id: str
    message_id: str
    external_user_id: str
    external_conversation_id: str
    chat_type: ChatType
    message_type: MessageType
    text: str
    reply_to_message_id: str
    received_at: datetime

    def __repr__(self) -> str:
        """返回不包含正文和平台标识的安全诊断表示。"""
        return (
            "InboundMessage("
            f"channel={self.channel!r}, account_id={self.account_id!r}, "
            f"chat_type={self.chat_type!r}, message_type={self.message_type!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class OutboundMessage:
    """保存等待平台 Transport 投递的一段回复。"""

    channel: str
    account_id: str
    external_conversation_id: str
    reply_to_message_id: str
    content: str
    kind: DeliveryKind = "message"

    def __repr__(self) -> str:
        """返回不包含回复正文和平台目标的安全诊断表示。"""
        return (
            "OutboundMessage("
            f"channel={self.channel!r}, account_id={self.account_id!r}, "
            f"kind={self.kind!r})"
        )


@dataclass(frozen=True, slots=True)
class IgnoredInbound:
    """描述一条不进入 Agent 的消息及其稳定原因。"""

    reason: str


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """保存平台确认后的消息标识。"""

    platform_message_id: str


@runtime_checkable
class ChannelTransport(Protocol):
    """定义 Gateway 对任意 IM Transport 使用的生命周期和发送能力。"""

    async def connect(self) -> None:
        """建立连接并等待平台就绪。"""
        ...

    async def disconnect(self) -> None:
        """停止接收并释放平台连接。"""
        ...

    async def send(
        self,
        message: OutboundMessage,
        *,
        idempotency_key: str,
    ) -> SendReceipt:
        """使用稳定幂等键发送一段消息。"""
        ...
