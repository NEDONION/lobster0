"""MiniClaw Browser Worker 的版本化 Core 接口。"""

from .client import BrowserClient
from .models import BrowserAction, BrowserProtocolError

__all__ = ["BrowserAction", "BrowserClient", "BrowserProtocolError"]
