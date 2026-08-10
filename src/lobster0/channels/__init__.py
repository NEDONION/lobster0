"""Lobster0 的平台无关 Channel 契约与飞书 Adapter。"""

from lobster0.channels.base import (
    ChannelTransport,
    ChannelTransportError,
    IgnoredInbound,
    InboundMessage,
    OutboundMessage,
    SendReceipt,
)

__all__ = [
    "ChannelTransport",
    "ChannelTransportError",
    "IgnoredInbound",
    "InboundMessage",
    "OutboundMessage",
    "SendReceipt",
]
