"""Channel 契约测试使用的纯内存对象。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from miniclaw.channels.base import OutboundMessage, SendReceipt


@dataclass(frozen=True, slots=True)
class FakeFeishuMessage:
    """模拟官方 SDK 归一化后的最小消息视图。"""

    event_id: str = "evt_test"
    message_id: str = "om_test"
    chat_id: str = "oc_allowed"
    chat_type: str = "p2p"
    sender_id: str = "ou_owner"
    sender_type: str | None = "user"
    sender_is_bot: bool = False
    mentioned_bot: bool = False
    body_text: str = "你好"
    raw_content_type: str = "text"
    create_time: datetime = datetime(2026, 8, 8, tzinfo=UTC)


class FakeChannelTransport:
    """按预设结果发送并记录幂等键的异步 Transport。"""

    def __init__(self, outcomes: Sequence[SendReceipt | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.sent: list[tuple[OutboundMessage, str]] = []
        self.connected = False

    async def connect(self) -> None:
        """标记连接已建立。"""
        self.connected = True

    async def disconnect(self) -> None:
        """标记连接已关闭。"""
        self.connected = False

    async def send(
        self,
        message: OutboundMessage,
        *,
        idempotency_key: str,
    ) -> SendReceipt:
        """返回下一项预设结果。"""
        self.sent.append((message, idempotency_key))
        if not self._outcomes:
            raise AssertionError("FakeChannelTransport has no configured outcome")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
