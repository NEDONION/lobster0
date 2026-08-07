"""Agent Runtime 到本地交互层的进程内事件。"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from miniclaw.providers.base import JsonValue

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


async def emit(handler: RunEventHandler | None, event: RunEvent) -> None:
    """按顺序交付事件，同时隔离展示层普通异常。"""
    if handler is None:
        return
    try:
        await handler(event)
    except Exception:
        logger.error("RunEvent handler failed: %s", event.kind)
