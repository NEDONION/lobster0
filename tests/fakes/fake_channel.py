"""Channel 契约测试使用的纯内存对象。"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from lobster0.channels.base import OutboundMessage, SendReceipt


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
    parent_message_id: str = ""
    resources: list = field(default_factory=list)
    create_time: datetime = datetime(2026, 8, 8, tzinfo=UTC)


class FakeChannelTransport:
    """按预设结果发送并记录幂等键的异步 Transport。"""

    def __init__(
        self,
        outcomes: Sequence[SendReceipt | BaseException],
        *,
        card_outcomes: Sequence[SendReceipt | BaseException] = (),
    ) -> None:
        self._outcomes = list(outcomes)
        self._card_outcomes = list(card_outcomes)
        self.sent: list[tuple[OutboundMessage, str]] = []
        self.cards_sent: list[tuple[str, str, dict[str, Any], str]] = []
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

    async def send_card(
        self,
        *,
        conversation_id: str,
        reply_to_message_id: str,
        card: dict[str, Any],
        idempotency_key: str,
    ) -> SendReceipt:
        """记录 durable Card 发送并返回独立预设结果。"""
        self.cards_sent.append(
            (conversation_id, reply_to_message_id, card, idempotency_key)
        )
        if not self._card_outcomes:
            raise AssertionError("FakeChannelTransport has no configured card outcome")
        outcome = self._card_outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass(frozen=True, slots=True)
class FakeSdkConfig:
    """记录 official SDK config constructor 收到的显式参数。"""

    kind: str
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FakeSdkSendResult:
    """模拟 official SDK 的 SendResult。"""

    success: bool
    message_id: str | None = None
    error: Any = None


class FakeOfficialChannel:
    """模拟 official FeishuChannel 的生命周期与平台调用。"""

    def __init__(self, outcomes: Sequence[FakeSdkSendResult | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.handlers: dict[str, Any] = {}
        self.constructor_kwargs: dict[str, Any] = {}
        self.sent: list[tuple[str, Any, Any]] = []
        self.cards_updated: list[tuple[str, dict[str, Any]]] = []
        self.typing_added: list[str] = []
        self.typing_removed: list[tuple[str, str]] = []
        self.connected = False
        self.disconnected = False
        self.connect_until_ready_called = False
        self.legacy_connect_called = False
        self.connect_calls: list[str] = []

    def on(self, name: str, handler: Any):
        """注册事件 handler 并返回 unsubscribe。"""
        self.handlers[name] = handler

        def unsubscribe() -> None:
            self.handlers.pop(name, None)

        return unsubscribe

    async def connect(self) -> None:
        """记录不适合长连接 Gateway 的旧前台接口。"""
        self.legacy_connect_called = True
        self.connect_calls.append("connect")
        self.connected = True

    async def connect_until_ready(self) -> None:
        """模拟 WebSocket 就绪后立即返回的后台接口。"""
        self.connect_until_ready_called = True
        self.connect_calls.append("connect_until_ready")
        self.connected = True

    async def disconnect(self) -> None:
        """模拟优雅断开。"""
        self.disconnected = True
        self.connected = False

    async def send(self, to: str, message: Any, opts: Any = None):
        """记录目标、消息、选项并返回预设结果。"""
        self.sent.append((to, message, opts))
        if not self.outcomes:
            return FakeSdkSendResult(True, "om_default")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def add_typing_reaction(self, message_id: str) -> str:
        """返回可用于移除的 reaction id。"""
        self.typing_added.append(message_id)
        return "reaction_typing"

    async def remove_typing_reaction(self, message_id: str, reaction_id: str) -> bool:
        """记录 Typing 清理。"""
        self.typing_removed.append((message_id, reaction_id))
        return True

    async def update_card(self, message_id: str, card: dict[str, Any]):
        """记录卡片更新。"""
        self.cards_updated.append((message_id, card))
        return FakeSdkSendResult(True, message_id)


class FakeOfficialSdk:
    """提供与 lark_channel 有限公共 API 同形的注入式模块。"""

    FEISHU_DOMAIN = "https://open.feishu.cn"
    LARK_DOMAIN = "https://open.larksuite.com"

    def __init__(
        self,
        outcomes: Sequence[FakeSdkSendResult | BaseException] = (),
    ) -> None:
        self.channel = FakeOfficialChannel(outcomes)

    def SecurityConfig(self, **values: Any) -> FakeSdkConfig:  # noqa: N802
        """记录严格安全配置。"""
        return FakeSdkConfig("security", values)

    def PolicyConfig(self, **values: Any) -> FakeSdkConfig:  # noqa: N802
        """记录白名单策略。"""
        return FakeSdkConfig("policy", values)

    def InboundConfig(self, **values: Any) -> FakeSdkConfig:  # noqa: N802
        """记录入站配置。"""
        return FakeSdkConfig("inbound", values)

    def TransportConfig(self, **values: Any) -> FakeSdkConfig:  # noqa: N802
        """记录 WebSocket 配置。"""
        return FakeSdkConfig("transport", values)

    def MediaCacheConfig(self, **values: Any) -> FakeSdkConfig:  # noqa: N802
        """记录媒体缓存配置；图片能否被下载到本地取决于它。"""
        return FakeSdkConfig("media_cache", values)

    def ChannelConfig(self, **values: Any) -> FakeSdkConfig:  # noqa: N802
        """记录整包 ChannelConfig；media_cache 只能经由它传给 SDK。"""
        return FakeSdkConfig("channel_config", values)

    def SendOpts(self, **values: Any) -> SimpleNamespace:  # noqa: N802
        """模拟 official SendOpts。"""
        return SimpleNamespace(**values)

    def FeishuChannel(self, **values: Any) -> FakeOfficialChannel:  # noqa: N802
        """返回单一 Channel 实例并保留 constructor 参数供断言。"""
        self.channel.constructor_kwargs = values
        return self.channel
