"""MiniClaw 的平台无关 Channel 契约与飞书 Adapter。"""

from miniclaw.channels.base import (
    ChannelTransport,
    IgnoredInbound,
    InboundMessage,
    OutboundMessage,
    SendReceipt,
)

__all__ = [
    "ChannelTransport",
    "IgnoredInbound",
    "InboundMessage",
    "OutboundMessage",
    "SendReceipt",
]
