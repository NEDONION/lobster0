"""飞书 Typing 与 streaming progress card 的 best-effort 能力层。"""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from miniclaw.agent.events import RunEvent
from miniclaw.channels.base import SendReceipt
from miniclaw.storage.channels import StoredInboundEvent


class CapabilityTransport(Protocol):
    """收窄能力层使用的平台 API，避免依赖 concrete FeishuTransport。"""

    async def add_typing(self, message_id: str) -> str | None:
        """添加 Typing reaction。"""
        ...

    async def remove_typing(self, message_id: str, reaction_id: str | None) -> bool:
        """移除 Typing reaction。"""
        ...

    async def send_card(
        self,
        *,
        conversation_id: str,
        reply_to_message_id: str,
        card: dict[str, Any],
        idempotency_key: str,
    ) -> SendReceipt:
        """创建进度卡片。"""
        ...

    async def update_card(
        self,
        platform_message_id: str,
        card: dict[str, Any],
    ) -> SendReceipt:
        """更新进度卡片。"""
        ...


@dataclass(frozen=True, slots=True)
class CapabilityOutcome:
    """描述非权威平台能力的最终状态。"""

    card_created: bool
    card_failed: bool
    final_markdown_required: bool = True


class ChannelCapabilities:
    """为每条已 claim 消息创建独立且有界的能力会话。"""

    def __init__(
        self,
        *,
        transport: CapabilityTransport,
        streaming_card: bool,
        update_interval: float = 0.5,
        max_visible_chars: int = 20_000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """绑定 Transport 与不可由平台输入放大的更新预算。"""
        if update_interval <= 0 or max_visible_chars <= 0:
            raise ValueError("Channel capability limits must be positive")
        self._transport = transport
        self._streaming_card = streaming_card
        self._update_interval = update_interval
        self._max_visible_chars = max_visible_chars
        self._clock = clock or time.monotonic

    def activity(self, event: StoredInboundEvent) -> "CapabilityActivity":
        """创建单条消息专用状态，禁止跨 Conversation 共享内容。"""
        return CapabilityActivity(
            transport=self._transport,
            event=event,
            streaming_card=self._streaming_card,
            update_interval=self._update_interval,
            max_visible_chars=self._max_visible_chars,
            clock=self._clock,
        )


class CapabilityActivity:
    """聚合单个 Turn 的公开 text delta 并维护平台进度视图。"""

    def __init__(
        self,
        *,
        transport: CapabilityTransport,
        event: StoredInboundEvent,
        streaming_card: bool,
        update_interval: float,
        max_visible_chars: int,
        clock: Callable[[], float],
    ) -> None:
        self._transport = transport
        self._event = event
        self._streaming_card = streaming_card
        self._update_interval = update_interval
        self._max_visible_chars = max_visible_chars
        self._clock = clock
        self._typing_reaction_id: str | None = None
        self._card_message_id: str | None = None
        self._visible_text = ""
        self._last_rendered = ""
        self._last_update_at: float | None = None
        self._card_failed = False
        self._finished = False
        self.idempotency_key = _card_idempotency_key(event)

    async def start(self) -> None:
        """在 Turn 开始前 best-effort 开启 Typing。"""
        try:
            self._typing_reaction_id = await self._transport.add_typing(
                self._event.reply_to_message_id
            )
        except Exception:
            self._typing_reaction_id = None

    async def on_event(self, event: RunEvent) -> None:
        """只消费公开 model_text_delta；其他 trace 类型一律忽略。"""
        if self._finished or event.kind != "model_text_delta":
            return
        text = event.data.get("text")
        if not isinstance(text, str) or not text:
            return
        self._visible_text = _bounded_append(
            self._visible_text,
            text,
            self._max_visible_chars,
        )
        if not self._streaming_card or self._card_failed:
            return
        if self._card_message_id is None:
            await self._create_card()
            return
        now = self._clock()
        if (
            self._last_update_at is None
            or now - self._last_update_at >= self._update_interval
        ):
            await self._update_card(self._visible_text, incomplete=False)

    async def finish(
        self,
        *,
        content: str | None,
        failed: bool,
    ) -> CapabilityOutcome:
        """刷新终态卡片并无条件 best-effort 移除 Typing。"""
        if self._finished:
            return self._outcome()
        self._finished = True
        try:
            if self._card_message_id is not None and not self._card_failed:
                final_text = (
                    self._visible_text
                    if failed or not isinstance(content, str)
                    else content[: self._max_visible_chars]
                )
                if failed:
                    await self._update_card(final_text, incomplete=True)
                elif final_text != self._last_rendered:
                    await self._update_card(final_text, incomplete=False)
        finally:
            try:
                await self._transport.remove_typing(
                    self._event.reply_to_message_id,
                    self._typing_reaction_id,
                )
            except Exception:
                pass
        return self._outcome()

    async def _create_card(self) -> None:
        """用稳定 UUID 创建第一帧进度卡片。"""
        try:
            receipt = await self._transport.send_card(
                conversation_id=self._event.external_conversation_id,
                reply_to_message_id=self._event.reply_to_message_id,
                card=_progress_card(self._visible_text, incomplete=False),
                idempotency_key=self.idempotency_key,
            )
        except Exception:
            self._card_failed = True
            return
        self._card_message_id = receipt.platform_message_id
        self._last_rendered = self._visible_text
        self._last_update_at = self._clock()

    async def _update_card(self, text: str, *, incomplete: bool) -> None:
        """更新同一张卡片；失败后永久关闭本次进度视图。"""
        if self._card_message_id is None:
            return
        try:
            await self._transport.update_card(
                self._card_message_id,
                _progress_card(text, incomplete=incomplete),
            )
        except Exception:
            self._card_failed = True
            return
        self._last_rendered = text
        self._last_update_at = self._clock()

    def _outcome(self) -> CapabilityOutcome:
        """生成不包含消息正文或平台 ID 的状态。"""
        return CapabilityOutcome(
            card_created=self._card_message_id is not None,
            card_failed=self._card_failed,
        )


def _progress_card(text: str, *, incomplete: bool) -> dict[str, Any]:
    """只用公开回答文本构造飞书 v2 卡片。"""
    visible = text or "…"
    if incomplete:
        visible = f"{visible}\n\n⚠️ **回复未完成，请稍后重试。**"
    title = "MiniClaw 回复未完成" if incomplete else "MiniClaw 正在回复"
    template = "red" if incomplete else "blue"
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": visible},
            ]
        },
    }


def _bounded_append(current: str, delta: str, limit: int) -> str:
    """限制进度卡片内存和平台 payload，不改变最终持久化回复。"""
    remaining = limit - len(current)
    if remaining <= 0:
        return current
    return current + delta[:remaining]


def _card_idempotency_key(event: StoredInboundEvent) -> str:
    """从稳定入站键派生不泄露平台标识的 32 字符 UUID。"""
    source = (
        f"{event.key.channel}\0{event.key.account_id}\0"
        f"{event.external_message_id}\0progress-card"
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
