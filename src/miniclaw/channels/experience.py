"""平台无关 Typing 与 progress preview 的 best-effort Experience 层。"""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from miniclaw.agent.events import RunEvent
from miniclaw.channels.base import ChannelTransportError, SendReceipt
from miniclaw.channels.observability import ChannelObserver
from miniclaw.storage.channels import StoredInboundEvent


@runtime_checkable
class ChannelExperienceTransport(Protocol):
    """表达用户体验意图，不暴露任何平台 Card、View 或 SDK 类型。"""

    async def start_typing(self, event: StoredInboundEvent) -> str | None:
        """为当前入站消息开启 Typing，并返回 opaque cleanup token。"""
        ...

    async def stop_typing(self, token: str | None) -> None:
        """best-effort 结束与 token 对应的 Typing。"""
        ...

    async def create_progress(
        self,
        event: StoredInboundEvent,
        text: str,
        *,
        idempotency_key: str,
    ) -> SendReceipt:
        """创建首个公开回答 preview。"""
        ...

    async def update_progress(
        self,
        platform_message_id: str,
        text: str,
        *,
        incomplete: bool,
        completed: bool,
    ) -> SendReceipt:
        """更新同一 preview 的中间或终态。"""
        ...


@dataclass(frozen=True, slots=True)
class ExperienceOutcome:
    """描述体验能力终态，以及平台是否仍需普通文本投递。"""

    progress_created: bool
    progress_failed: bool
    final_delivery_required: bool = True
    final_delivery_offset: int = 0
    final_reply_to_message_id: str | None = None

    @property
    def card_created(self) -> bool:
        """提供 Phase 4 compatibility 读别名。"""
        return self.progress_created

    @property
    def card_failed(self) -> bool:
        """提供 Phase 4 compatibility 读别名。"""
        return self.progress_failed

    @property
    def final_markdown_required(self) -> bool:
        """提供 Phase 4 compatibility 读别名。"""
        return self.final_delivery_required


class ChannelExperience:
    """为每条 claimed Inbox 消息创建独立、有界的体验状态。"""

    def __init__(
        self,
        *,
        transport: ChannelExperienceTransport,
        progress_enabled: bool,
        progress_is_final: bool = False,
        update_interval: float = 0.5,
        max_visible_chars: int = 20_000,
        clock: Callable[[], float] | None = None,
        observer: ChannelObserver | None = None,
    ) -> None:
        """绑定 Transport 和不能由平台输入放大的本地预算。"""
        if (
            not isinstance(progress_enabled, bool)
            or not isinstance(progress_is_final, bool)
            or type(update_interval) not in {int, float}
            or update_interval <= 0
            or type(max_visible_chars) is not int
            or max_visible_chars <= 0
        ):
            raise ValueError("Channel experience limits must be positive")
        self._transport = transport
        self._progress_enabled = progress_enabled
        self._progress_is_final = progress_is_final
        self._update_interval = float(update_interval)
        self._max_visible_chars = max_visible_chars
        self._clock = clock or time.monotonic
        self._observer = observer

    def activity(self, event: StoredInboundEvent) -> "ExperienceActivity":
        """创建单 Turn 私有状态，禁止跨 Conversation 共享 preview 文本。"""
        return ExperienceActivity(
            transport=self._transport,
            event=event,
            progress_enabled=self._progress_enabled,
            progress_is_final=self._progress_is_final,
            update_interval=self._update_interval,
            max_visible_chars=self._max_visible_chars,
            clock=self._clock,
            observer=self._observer,
        )


class ExperienceActivity:
    """聚合公开 model text delta，并维护单个 Turn 的 Typing/preview。"""

    def __init__(
        self,
        *,
        transport: ChannelExperienceTransport,
        event: StoredInboundEvent,
        progress_enabled: bool,
        progress_is_final: bool,
        update_interval: float,
        max_visible_chars: int,
        clock: Callable[[], float],
        observer: ChannelObserver | None,
    ) -> None:
        self._transport = transport
        self._event = event
        self._progress_enabled = progress_enabled
        self._progress_is_final = progress_is_final
        self._update_interval = update_interval
        self._max_visible_chars = max_visible_chars
        self._clock = clock
        self._observer = observer
        self._typing_token: str | None = None
        self._progress_message_id: str | None = None
        self._visible_text = ""
        self._last_rendered = ""
        self._last_update_at: float | None = None
        self._progress_failed = False
        self._completed_final_progress = False
        self._final_delivery_offset = 0
        self._final_reply_to_message_id: str | None = None
        self._finished = False
        self.idempotency_key = _progress_idempotency_key(event)

    async def start(self) -> None:
        """Turn 开始前 best-effort 开启平台 Typing。"""
        try:
            self._typing_token = await self._transport.start_typing(self._event)
        except Exception as error:
            self._typing_token = None
            self._observe_failure("typing_start", error)

    async def on_event(self, event: RunEvent) -> None:
        """只消费公开 model_text_delta，忽略 reasoning 与 Tool trace。"""
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
        # A final card is externally visible and cannot be atomically replaced by
        # the durable Approval card.  Buffer preview text until the Turn outcome is
        # known so a tool-call response with content never leaves two Feishu cards.
        if self._progress_is_final:
            return
        if not self._progress_enabled or self._progress_failed:
            return
        if self._progress_message_id is None:
            await self._create_progress()
            return
        now = self._clock()
        if self._last_update_at is None or now - self._last_update_at >= self._update_interval:
            await self._update_progress(
                self._visible_text,
                incomplete=False,
                completed=False,
            )

    async def finish(
        self,
        *,
        content: str | None,
        failed: bool,
    ) -> ExperienceOutcome:
        """幂等刷新平台终态，并无条件 best-effort 清理 Typing。"""
        if self._finished:
            return self._outcome()
        self._finished = True
        try:
            if (
                self._progress_is_final
                and self._progress_enabled
                and self._progress_message_id is None
                and not self._progress_failed
                and not failed
                and isinstance(content, str)
            ):
                self._visible_text = content[: self._max_visible_chars]
                await self._create_progress()
            if self._progress_message_id is not None and not self._progress_failed:
                final_text = (
                    self._visible_text
                    if failed or not isinstance(content, str)
                    else content[: self._max_visible_chars]
                )
                await self._update_progress(
                    final_text,
                    incomplete=failed,
                    completed=not failed,
                )
                if (
                    self._progress_is_final
                    and not failed
                    and not self._progress_failed
                    and isinstance(content, str)
                ):
                    visible_length = min(len(content), self._max_visible_chars)
                    self._final_delivery_offset = visible_length
                    if visible_length < len(content):
                        self._final_reply_to_message_id = self._progress_message_id
                    else:
                        self._completed_final_progress = True
        finally:
            try:
                await self._transport.stop_typing(self._typing_token)
            except Exception as error:
                self._observe_failure("typing_stop", error)
        return self._outcome()

    async def _create_progress(self) -> None:
        """用稳定本地 key 创建第一帧 preview。"""
        try:
            receipt = await self._transport.create_progress(
                self._event,
                self._visible_text,
                idempotency_key=self.idempotency_key,
            )
            if not receipt.platform_message_id:
                raise ChannelTransportError(
                    f"{self._event.key.channel}_progress_failed"
                )
        except Exception as error:
            self._progress_failed = True
            self._observe_failure("progress_create", error)
            return
        self._progress_message_id = receipt.platform_message_id
        self._last_rendered = self._visible_text
        self._last_update_at = self._clock()

    async def _update_progress(
        self,
        text: str,
        *,
        incomplete: bool,
        completed: bool,
    ) -> None:
        """更新 preview；一次失败后关闭本 Turn 的 progress 能力。"""
        if self._progress_message_id is None:
            return
        try:
            await self._transport.update_progress(
                self._progress_message_id,
                text,
                incomplete=incomplete,
                completed=completed,
            )
        except Exception as error:
            self._progress_failed = True
            self._observe_failure("progress_update", error)
            return
        self._last_rendered = text
        self._last_update_at = self._clock()

    def _outcome(self) -> ExperienceOutcome:
        """生成不包含正文或平台 ID 的终态。"""
        return ExperienceOutcome(
            progress_created=self._progress_message_id is not None,
            progress_failed=self._progress_failed,
            final_delivery_required=not self._completed_final_progress,
            final_delivery_offset=self._final_delivery_offset,
            final_reply_to_message_id=self._final_reply_to_message_id,
        )

    def _observe_failure(self, capability: str, error: Exception) -> None:
        """把体验异常压缩为稳定码，Observer 永远看不到异常正文。"""
        if self._observer is None:
            return
        error_code = (
            error.code
            if isinstance(error, ChannelTransportError)
            else f"{self._event.key.channel}_{capability.split('_', 1)[0]}_failed"
        )
        try:
            self._observer.capability(
                channel=self._event.key.channel,
                account_id=self._event.key.account_id,
                external_message_id=self._event.external_message_id,
                capability=capability,
                error_code=error_code,
                event_row_id=(
                    self._event.storage_rowid
                    if self._event.storage_rowid > 0
                    else None
                ),
                session_id=self._event.session_id,
            )
        except Exception:
            return


def _bounded_append(current: str, delta: str, limit: int) -> str:
    """限制 preview 内存和平台 payload，不改变最终 durable 回复。"""
    remaining = limit - len(current)
    if remaining <= 0:
        return current
    return current + delta[:remaining]


def _progress_idempotency_key(event: StoredInboundEvent) -> str:
    """从稳定入站键派生不泄露平台 ID 的 32 字符 key。"""
    source = (
        f"{event.key.channel}\0{event.key.account_id}\0"
        f"{event.external_message_id}\0progress"
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
