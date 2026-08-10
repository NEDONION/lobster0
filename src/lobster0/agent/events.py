"""Agent Runtime 到本地交互层的进程内事件。"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
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


def tool_display_summary(tool_name: str, arguments: dict[str, JsonValue]) -> str:
    """生成有界的 Tool 展示摘要，隐藏正文、凭据、路径与完整命令参数。

    审批卡片与 Tool 事件共用这一份摘要，保证用户在“批准前看到的描述”与
    “执行时看到的描述”完全一致。返回值始终比裸工具名多带一段信息，避免
    前端把工具名和摘要渲染成重复的两行。

    Args:
        tool_name: 已解析的 Tool 名称。
        arguments: Tool 的原始参数；本函数负责裁剪出可安全展示的部分。

    Returns:
        单行、不含 Secret 的展示文本。
    """
    if tool_name == "run_command":
        program = arguments.get("program")
        args = arguments.get("args")
        if isinstance(program, str) and isinstance(args, list):
            program_label = PurePosixPath(program).name or "command"
            suffix = "arg" if len(args) == 1 else "args"
            return f"run_command {program_label} · {len(args)} {suffix}"
    if tool_name == "http_get":
        url = arguments.get("url")
        if isinstance(url, str):
            try:
                parsed = urlsplit(url)
                hostname = parsed.hostname
                port = parsed.port or 443
            except ValueError:
                hostname = None
            if hostname is not None:
                host_text = f"[{hostname}]" if ":" in hostname else hostname
                return f"http_get https://{host_text}:{port}"
    if tool_name in {"browser_click", "browser_type", "browser_press"}:
        origin = arguments.get("origin")
        role = arguments.get("role")
        if isinstance(origin, str) and isinstance(role, str):
            try:
                parsed = urlsplit(origin)
                hostname = parsed.hostname
                port = parsed.port or 443
            except ValueError:
                hostname = None
            if hostname is not None:
                host_text = f"[{hostname}]" if ":" in hostname else hostname
                summary = f"{tool_name} https://{host_text}:{port} · {role}"
                text = arguments.get("text")
                if tool_name == "browser_type" and isinstance(text, str):
                    summary += f" · {len(text)} chars"
                return summary
    path = arguments.get("path")
    if isinstance(path, str):
        return f"{tool_name} {PurePosixPath(path).name}"
    return f"{tool_name} request"
