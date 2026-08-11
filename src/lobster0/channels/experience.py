"""平台无关 Typing 与结构化 Agent progress 的 best-effort Experience 层。"""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from lobster0.agent.events import RunEvent
from lobster0.channels.base import ChannelTransportError
from lobster0.channels.observability import ChannelObserver
from lobster0.channels.progress import AgentProgress, ProgressProjector
from lobster0.storage.channels import StoredInboundEvent


@dataclass(frozen=True, slots=True)
class ProgressReceipt:
    """保存 progress 消息 ID 与其中已覆盖的最终答案字符数。"""

    platform_message_id: str
    visible_answer_chars: int = 0


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
        progress: AgentProgress,
        *,
        idempotency_key: str,
    ) -> ProgressReceipt:
        """创建首个结构化公开进度。"""
        ...

    async def update_progress(
        self,
        platform_message_id: str,
        progress: AgentProgress,
    ) -> ProgressReceipt:
        """更新同一结构化进度的中间或终态。"""
        ...


@dataclass(frozen=True, slots=True)
class ExperienceOutcome:
    """描述体验能力终态，以及平台是否仍需普通文本投递。"""

    progress_created: bool
    progress_failed: bool
    final_delivery_required: bool = True
    final_delivery_offset: int = 0
    final_reply_to_message_id: str | None = None
    progress_message_id: str | None = None

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
        """创建单 Turn 私有状态，禁止跨 Conversation 共享进度快照。"""
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
    """投影公开 Agent 步骤，并维护单个 Turn 的 Typing/progress。"""

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
        self._clock = clock
        self._observer = observer
        self._projector = ProgressProjector(
            clock=clock,
            public_text_limit=max_visible_chars,
        )
        self._typing_token: str | None = None
        self._progress_message_id: str | None = None
        self._last_update_at: float | None = None
        self._progress_failed = False
        self._completed_final_progress = False
        self._final_delivery_offset = 0
        self._final_reply_to_message_id: str | None = None
        self._finished = False
        self._final_progress: AgentProgress | None = None
        self.idempotency_key = _progress_idempotency_key(event)

    async def start(self) -> None:
        """Turn 开始前开启 Typing，并为终态单卡 best-effort 创建首帧。"""
        try:
            self._typing_token = await self._transport.start_typing(self._event)
        except Exception as error:
            self._typing_token = None
            self._observe_failure("typing_start", error)
        if (
            self._progress_enabled
            and self._progress_is_final
            and not self._progress_failed
        ):
            await self._create_progress(self._projector.snapshot())

    async def on_event(self, event: RunEvent) -> None:
        """消费 Runtime 事件并只向 Transport 交付脱敏结构化快照。"""
        if self._finished:
            return
        changed = self._projector.apply(event)
        if not changed or not self._progress_enabled or self._progress_failed:
            return
        progress = self._projector.snapshot()
        if self._progress_message_id is None:
            should_create = (
                event.kind == "tool_started"
                if self._progress_is_final
                else event.kind == "model_text_delta"
            )
            if should_create:
                await self._create_progress(progress)
            return
        now = self._clock()
        if self._last_update_at is None or now - self._last_update_at >= self._update_interval:
            await self._update_progress(progress)

    def finalize(self, *, content: str | None, failed: bool) -> AgentProgress:
        """同步、幂等冻结将被持久化并交付的同一份终态快照。"""
        if self._final_progress is None:
            self._final_progress = self._projector.finish(content, failed=failed)
        return self._final_progress

    async def finish(
        self,
        *,
        content: str | None,
        failed: bool,
        progress: AgentProgress | None = None,
    ) -> ExperienceOutcome:
        """幂等刷新平台终态，并无条件 best-effort 清理 Typing。"""
        if self._finished:
            return self._outcome()
        self._finished = True
        final_progress = progress or self.finalize(content=content, failed=failed)
        try:
            if (
                self._progress_is_final
                and self._progress_enabled
                and self._progress_message_id is None
                and not self._progress_failed
                and isinstance(content, str)
            ):
                receipt = await self._create_progress(final_progress)
            else:
                receipt = None
            if self._progress_message_id is not None and not self._progress_failed:
                if receipt is None:
                    receipt = await self._update_progress(final_progress)
                if (
                    self._progress_is_final
                    and not self._progress_failed
                    and isinstance(content, str)
                    and receipt is not None
                ):
                    visible_length = min(len(content), receipt.visible_answer_chars)
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

    async def _create_progress(self, progress: AgentProgress) -> ProgressReceipt | None:
        """用稳定本地 key 创建第一帧结构化 progress。"""
        try:
            receipt = await self._transport.create_progress(
                self._event,
                progress,
                idempotency_key=self.idempotency_key,
            )
            if not receipt.platform_message_id:
                raise ChannelTransportError(
                    f"{self._event.key.channel}_progress_failed"
                )
        except Exception as error:
            self._progress_failed = True
            self._observe_failure("progress_create", error)
            return None
        self._progress_message_id = receipt.platform_message_id
        self._last_update_at = self._clock()
        return receipt

    async def _update_progress(
        self,
        progress: AgentProgress,
    ) -> ProgressReceipt | None:
        """更新结构化 progress；一次失败后关闭本 Turn 的 progress 能力。"""
        if self._progress_message_id is None:
            return None
        try:
            receipt = await self._transport.update_progress(
                self._progress_message_id,
                progress,
            )
        except Exception as error:
            self._progress_failed = True
            self._observe_failure("progress_update", error)
            return None
        self._last_update_at = self._clock()
        return receipt

    def _outcome(self) -> ExperienceOutcome:
        """生成不包含正文的终态。

        ``progress_message_id`` 是 Owner 在 IM 里真正看到并可以「回复」的那条卡片的平台 ID。
        Manager 需要它把卡片与内部 assistant message 关联起来，否则 Owner 回复卡片时
        没有任何记录可以反查（真实事故：飞书 /good 一直提示"没有找到这条回答"）。
        """
        return ExperienceOutcome(
            progress_created=self._progress_message_id is not None,
            progress_failed=self._progress_failed,
            final_delivery_required=not self._completed_final_progress,
            final_delivery_offset=self._final_delivery_offset,
            final_reply_to_message_id=self._final_reply_to_message_id,
            progress_message_id=self._progress_message_id,
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


def _progress_idempotency_key(event: StoredInboundEvent) -> str:
    """从稳定入站键派生不泄露平台 ID 的 32 字符 key。"""
    source = (
        f"{event.key.channel}\0{event.key.account_id}\0"
        f"{event.external_message_id}\0progress"
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
