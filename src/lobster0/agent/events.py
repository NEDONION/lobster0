"""Agent Runtime 到本地交互层的进程内事件。"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from lobster0.providers.base import JsonValue

logger = logging.getLogger(__name__)

type RunEventKind = Literal[
    "turn_started",
    "model_text_delta",
    "model_usage",
    "model_reasoning",
    "tool_requested",
    "tool_started",
    "tool_finished",
    "approval_required",
    "turn_finished",
    "turn_failed",
    "turn_cancelled",
]


@dataclass(frozen=True, slots=True)
class RunEvent:
    """描述当前进程内一次可展示的 Agent 运行变化。"""

    kind: RunEventKind
    turn_id: int
    data: dict[str, JsonValue]


type RunEventHandler = Callable[[RunEvent], Awaitable[None]]


def display_tool_arguments(
    tool_name: str,
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """返回 UI/Channel 可展示的 Tool 参数，隐藏 Browser typed text 与 unstable refs。"""
    if not tool_name.startswith("browser_"):
        return dict(arguments)
    if tool_name == "browser_open":
        value = arguments.get("url")
        try:
            parsed = urlsplit(value) if isinstance(value, str) else None
            hostname = parsed.hostname if parsed is not None else None
            port = parsed.port if parsed is not None else None
        except ValueError:
            hostname = None
            port = None
        if hostname is None:
            return {"origin": "HTTPS target"}
        host = f"[{hostname.casefold()}]" if ":" in hostname else hostname.casefold()
        return {"origin": f"https://{host}" + (f":{port}" if port not in {None, 443} else "")}
    fields = {
        "browser_snapshot": ("cursor",),
        "browser_click": ("origin", "role"),
        "browser_type": ("origin", "role", "input_kind"),
        "browser_press": ("origin", "role", "key"),
        "browser_scroll": ("delta_y",),
        "browser_screenshot": ("full_page",),
        "browser_close": (),
    }.get(tool_name, ())
    visible = {name: arguments[name] for name in fields if name in arguments}
    if tool_name == "browser_type":
        visible["text"] = "<redacted>"
    return visible


async def emit(handler: RunEventHandler | None, event: RunEvent) -> None:
    """按顺序交付事件，同时隔离展示层普通异常。"""
    if handler is None:
        return
    try:
        await handler(event)
    except Exception:
        logger.error("RunEvent handler failed: %s", event.kind)
