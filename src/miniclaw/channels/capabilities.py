"""Phase 4 Feishu Capability compatibility adapter；新代码使用 Experience。"""

import hashlib
from collections.abc import Callable
from typing import Any, Protocol

from miniclaw.channels.base import ChannelTransportError, SendReceipt
from miniclaw.channels.experience import (
    ChannelExperience,
    ExperienceActivity,
    ExperienceOutcome,
)
from miniclaw.channels.observability import ChannelObserver
from miniclaw.storage.channels import StoredInboundEvent


class CapabilityTransport(Protocol):
    """保留 Phase 4 Feishu Transport 的旧 API 形状。"""

    async def add_typing(self, message_id: str) -> str | None: ...

    async def remove_typing(self, message_id: str, reaction_id: str | None) -> bool: ...

    async def send_card(
        self,
        *,
        conversation_id: str,
        reply_to_message_id: str,
        card: dict[str, Any],
        idempotency_key: str,
    ) -> SendReceipt: ...

    async def update_card(
        self,
        platform_message_id: str,
        card: dict[str, Any],
    ) -> SendReceipt: ...


CapabilityOutcome = ExperienceOutcome
CapabilityActivity = ExperienceActivity


class _LegacyFeishuExperienceTransport:
    """把旧 Reaction/Card API 收窄为平台无关 Experience 意图。"""

    def __init__(self, transport: CapabilityTransport) -> None:
        self._transport = transport
        self._typing: dict[str, tuple[str, str | None]] = {}
        self._progress_text: dict[str, str] = {}

    async def start_typing(self, event: StoredInboundEvent) -> str | None:
        reaction_id = await self._transport.add_typing(event.reply_to_message_id)
        if reaction_id is None:
            return None
        source = f"{event.external_message_id}\0{reaction_id}"
        token = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
        self._typing[token] = (event.reply_to_message_id, reaction_id)
        return token

    async def stop_typing(self, token: str | None) -> None:
        if token is None:
            return
        target = self._typing.pop(token, None)
        if target is None:
            return
        removed = await self._transport.remove_typing(*target)
        if not removed:
            raise ChannelTransportError("feishu_typing_remove_failed")

    async def create_progress(
        self,
        event: StoredInboundEvent,
        text: str,
        *,
        idempotency_key: str,
    ) -> SendReceipt:
        receipt = await self._transport.send_card(
            conversation_id=event.external_conversation_id,
            reply_to_message_id=event.reply_to_message_id,
            card=_progress_card(text, incomplete=False, completed=False),
            idempotency_key=idempotency_key,
        )
        self._progress_text[receipt.platform_message_id] = text
        return receipt

    async def update_progress(
        self,
        platform_message_id: str,
        text: str,
        *,
        incomplete: bool,
        completed: bool,
    ) -> SendReceipt:
        if completed and not incomplete and self._progress_text.get(platform_message_id) == text:
            return SendReceipt(platform_message_id)
        receipt = await self._transport.update_card(
            platform_message_id,
            _progress_card(text, incomplete=incomplete, completed=completed),
        )
        self._progress_text[platform_message_id] = text
        return receipt


class _LegacyObserver:
    """把新 Experience capability 名映射为 Phase 4 稳定事件名。"""

    _NAMES = {
        "typing_start": "typing_add",
        "typing_stop": "typing_remove",
        "progress_create": "progress_card_create",
        "progress_update": "progress_card_update",
    }

    def __init__(self, observer: ChannelObserver) -> None:
        self._observer = observer

    def capability(self, **values: Any) -> None:
        values["capability"] = self._NAMES.get(
            values.get("capability"),
            values.get("capability"),
        )
        self._observer.capability(**values)


class ChannelCapabilities:
    """保留旧构造入口，内部完全委托平台无关 `ChannelExperience`。"""

    def __init__(
        self,
        *,
        transport: CapabilityTransport,
        streaming_card: bool,
        update_interval: float = 0.5,
        max_visible_chars: int = 20_000,
        clock: Callable[[], float] | None = None,
        observer: ChannelObserver | None = None,
    ) -> None:
        adapter = _LegacyFeishuExperienceTransport(transport)
        self._experience = ChannelExperience(
            transport=adapter,
            progress_enabled=streaming_card,
            update_interval=update_interval,
            max_visible_chars=max_visible_chars,
            clock=clock,
            observer=None if observer is None else _LegacyObserver(observer),
        )

    def activity(self, event: StoredInboundEvent) -> ExperienceActivity:
        """返回新 ExperienceActivity，调用方无需感知兼容层。"""
        return self._experience.activity(event)


def _progress_card(
    text: str,
    *,
    incomplete: bool,
    completed: bool,
) -> dict[str, Any]:
    """只在 Feishu compatibility adapter 内构造平台 Card。"""
    visible = text or "…"
    if incomplete:
        visible = f"{visible}\n\n⚠️ **回复未完成，请稍后重试。**"
    if incomplete:
        title, template = "MiniClaw 回复未完成", "red"
    elif completed:
        title, template = "MiniClaw 回复", "green"
    else:
        title, template = "MiniClaw 正在回复", "blue"
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "body": {"elements": [{"tag": "markdown", "content": visible}]},
    }
