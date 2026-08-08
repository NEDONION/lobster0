"""平台 Adapter、Gateway 和 Delivery 之间的最小公共契约。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

type ChatType = Literal["p2p", "group"]
type MessageType = Literal["text"]
type DeliveryKind = Literal["message", "card", "approval", "typing"]


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
