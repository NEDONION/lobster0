"""SQLite-backed Channel Inbox 的有界队列、Worker 与重启恢复。"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from lobster0.agent.events import RunEventHandler
from lobster0.agent.runner import AgentLoopLimitError, AgentNoProgressError
from lobster0.agent.turn import TurnResult
from lobster0.channels.approvals import (
    ApprovalCommandOutcome,
    ApprovalEnvelope,
    approval_delivery_payload,
)
from lobster0.channels.base import DeliveryKind, InboundMessage
from lobster0.channels.delivery import split_message
from lobster0.channels.experience import ChannelExperience
from lobster0.channels.feedback_commands import FeedbackCommandOutcome
from lobster0.channels.observability import ChannelObserver
from lobster0.channels.progress import progress_from_metadata, progress_to_metadata
from lobster0.memory.models import ConversationKind
from lobster0.policy.modes import PermissionMode, PermissionState
from lobster0.providers.base import (
    ProviderAuthenticationError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    StreamHandler,
)
from lobster0.storage.channels import (
    ChannelIdentityRepository,
    ChannelStateError,
    DeliveryRepository,
    InboundEventKey,
    InboundEventRepository,
    StoredInboundEvent,
)
from lobster0.storage.conversations import (
    ConversationStateError,
    MessageRepository,
    SessionRepository,
    TurnRepository,
)

_FAILURE_NOTICE = "抱歉，这条消息处理失败了，请稍后重试。"


class TurnHandler(Protocol):
    """收窄 ChannelManager 对共享 TurnService 使用的公共入口。"""

    async def handle_inbound(
        self,
        *,
        user_id: int,
        channel: str,
        account_id: str,
        external_conversation_id: str,
        inbound_event_id: str,
        text: str,
        on_text: StreamHandler | None = None,
        on_event: RunEventHandler | None = None,
        trusted_owner: bool = False,
        conversation_kind: ConversationKind = "unknown",
        identity_verified: bool = False,
    ) -> TurnResult:
        """处理一条已经完成 Channel 校验的消息。"""
        ...


class ApprovalHandler(Protocol):
    """收窄 Manager 对 Channel Approval Controller 的使用。"""

    async def handle_text(
        self,
        *,
        user_id: int,
        actor_external_user_id: str,
        text: str,
        on_event: RunEventHandler | None = None,
    ) -> ApprovalCommandOutcome:
        """识别并处理文本控制命令。"""
        ...

    def prompt(self, *, user_id: int, approval_id: int) -> ApprovalEnvelope:
        """构建 Core 已校验的审批展示。"""
        ...


class FeedbackHandler(Protocol):
    """收窄 Manager 对 Channel Feedback Controller 的使用。"""

    async def handle_text(
        self,
        *,
        user_id: int,
        actor_external_user_id: str,
        text: str,
        channel: str,
        account_id: str,
        reply_to_platform_message_id: str,
    ) -> "FeedbackCommandOutcome":
        """识别并处理 /good、/bad 反馈命令。"""
        ...


@dataclass(frozen=True, slots=True)
class InboundAcceptance:
    """描述 callback 是否首次持久化并成功写入内存 wake-up 队列。"""

    inserted: bool
    enqueued: bool


@dataclass(slots=True)
class _LockEntry:
    """保存一个 Conversation Lock 和当前持有/等待计数。"""

    lock: asyncio.Lock
    users: int = 0


class ChannelManager:
    """把持久化 Inbox 串行交给共享 Agent，并生成 Delivery Outbox。"""

    def __init__(
        self,
        *,
        owner_id: int,
        owner_external_user_id: str,
        permission_state: PermissionState,
        service: TurnHandler,
        sessions: SessionRepository,
        messages: MessageRepository,
        turns: TurnRepository,
        identities: ChannelIdentityRepository,
        inbound: InboundEventRepository,
        deliveries: DeliveryRepository,
        channel: str,
        account_id: str,
        queue_size: int,
        worker_count: int,
        message_max_chars: int = 30_000,
        feeder_interval: float = 0.25,
        observer: ChannelObserver | None = None,
    ) -> None:
        """绑定唯一 Runtime、Repository 与不可由平台消息改变的并发预算。"""
        if (
            queue_size <= 0
            or worker_count <= 0
            or message_max_chars < 8
            or feeder_interval <= 0
        ):
            raise ValueError("ChannelManager limits must be positive")
        self.owner_id = owner_id
        if not owner_external_user_id or any(
            ord(character) < 0x20 for character in owner_external_user_id
        ):
            raise ValueError("owner_external_user_id is invalid")
        self._owner_external_user_id = owner_external_user_id
        self._permission_state = permission_state
        self.service = service
        self._sessions = sessions
        self._messages = messages
        self._turns = turns
        self._identities = identities
        self._inbound = inbound
        self._deliveries = deliveries
        self._channel = channel
        self._account_id = account_id
        self._worker_count = worker_count
        self._message_max_chars = message_max_chars
        self._feeder_interval = feeder_interval
        self._queue: asyncio.Queue[InboundEventKey] = asyncio.Queue(maxsize=queue_size)
        self._enqueued: set[InboundEventKey] = set()
        self._enqueued_guard = asyncio.Lock()
        self._locks: dict[str, _LockEntry] = {}
        self._locks_guard = asyncio.Lock()
        self._workers: list[asyncio.Task[None]] = []
        self._feeder: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._experience: ChannelExperience | None = None
        self._approvals: ApprovalHandler | None = None
        self._feedback: FeedbackHandler | None = None
        self._observer = observer

    def attach_experience(self, experience: ChannelExperience) -> None:
        """在启动前绑定依赖同一个 Transport 的平台无关体验能力。"""
        if self._workers or self._feeder is not None:
            raise RuntimeError("Channel experience must be attached before start")
        self._experience = experience

    def attach_capabilities(self, capabilities: ChannelExperience) -> None:
        """保留 Phase 4 进程内兼容别名；新装配应调用 attach_experience。"""
        self.attach_experience(capabilities)

    def attach_approvals(self, approvals: ApprovalHandler) -> None:
        """在启动前绑定只调用 Core continuation 的审批控制器。"""
        if self._workers or self._feeder is not None:
            raise RuntimeError("Channel approvals must be attached before start")
        self._approvals = approvals

    def attach_feedback(self, feedback: FeedbackHandler) -> None:
        """在启动前绑定只写 Feedback Ledger 的反馈命令控制器。"""
        if self._workers or self._feeder is not None:
            raise RuntimeError("Channel feedback must be attached before start")
        self._feedback = feedback

    async def receive(self, message: InboundMessage) -> InboundAcceptance:
        """先持久化消息，再 best-effort 写入有界内存队列。"""
        if message.channel != self._channel or message.account_id != self._account_id:
            raise ChannelStateError("channel_account_mismatch")
        result = self._inbound.record(message)
        if not result.inserted:
            self._observe_inbound(message, result.event, "duplicate", False)
            return InboundAcceptance(False, False)
        enqueued = await self._enqueue(result.event.key)
        self._observe_inbound(message, result.event, "accepted", enqueued)
        return InboundAcceptance(True, enqueued)

    async def start(self) -> None:
        """恢复遗留状态并启动 feeder 与有限数量 Worker。"""
        if self._workers or self._feeder is not None:
            return
        self._stopping.clear()
        await self._recover_stale()
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"channel-worker-{index}")
            for index in range(self._worker_count)
        ]
        self._feeder = asyncio.create_task(self._feed(), name="channel-feeder")

    async def wait_idle(self, *, timeout: float) -> None:
        """等待当前 queued/running Inbox 全部到达终态。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("ChannelManager did not become idle")
            await asyncio.wait_for(self._queue.join(), timeout=remaining)
            queued = self._inbound.list_by_status(
                self._channel,
                self._account_id,
                "queued",
            )
            running = self._inbound.list_by_status(
                self._channel,
                self._account_id,
                "running",
            )
            if not queued and not running:
                return
            await asyncio.sleep(min(self._feeder_interval, remaining))

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        """停止 feeder，有限等待已入队任务，然后取消 Worker。"""
        if not self._workers and self._feeder is None:
            return
        self._stopping.set()
        if self._feeder is not None:
            self._feeder.cancel()
            await asyncio.gather(self._feeder, return_exceptions=True)
            self._feeder = None
        try:
            await self.wait_idle(timeout=drain_timeout)
        except TimeoutError:
            pass
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _feed(self) -> None:
        """周期扫描 SQLite queued 状态，补回丢失的内存 wake-up。"""
        while not self._stopping.is_set():
            for event in self._inbound.list_by_status(
                self._channel,
                self._account_id,
                "queued",
            ):
                if not await self._enqueue(event.key):
                    break
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._feeder_interval,
                )
            except TimeoutError:
                continue

    async def _enqueue(self, key: InboundEventKey) -> bool:
        """去重并非阻塞写入 wake-up 队列；满队列留给 feeder。"""
        async with self._enqueued_guard:
            if key in self._enqueued:
                return False
            try:
                self._queue.put_nowait(key)
            except asyncio.QueueFull:
                return False
            self._enqueued.add(key)
            return True

    async def _worker(self, index: int) -> None:
        """持续消费持久事件；单次故障不能终止 Worker。"""
        del index
        while True:
            key = await self._queue.get()
            async with self._enqueued_guard:
                self._enqueued.discard(key)
            try:
                event = self._inbound.claim(key)
                if event is not None:
                    await self._process(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._fail_running_event(key, "channel_worker_failed")
            finally:
                self._queue.task_done()

    async def _process(self, event: StoredInboundEvent) -> None:
        """在 Conversation Lock 内调用共享 Agent 并先写 Outbox 再结算事件。"""
        async with self._conversation_lock(event.external_conversation_id):
            self._identities.get_or_create(
                self.owner_id,
                event.key.channel,
                event.key.account_id,
                event.external_user_id,
            )
            session = self._sessions.get_or_create(
                self.owner_id,
                event.key.channel,
                event.key.account_id,
                event.external_conversation_id,
            )
            self._inbound.bind_session(event.key, session.id)
            started = time.monotonic()
            self._observe_turn(
                event,
                status="started",
                session_id=session.id,
                started=started,
            )
            activity = (
                None
                if self._experience is None
                else self._experience.activity(event)
            )
            if activity is not None:
                await activity.start()
            permission_notice = self._control_notice(event, session.id)
            if permission_notice is not None:
                final_delivery_required = True
                if activity is not None:
                    outcome = await activity.finish(
                        content=permission_notice,
                        failed=False,
                    )
                    final_delivery_required = outcome.final_delivery_required
                if final_delivery_required:
                    self._create_notice_delivery(session.id, event, permission_notice)
                self._inbound.mark_completed(event.key)
                self._observe_turn(
                    event,
                    status="completed",
                    session_id=session.id,
                    started=started,
                )
                return
            if self._feedback is not None:
                feedback_notice = await self._handle_feedback_command(event)
                if feedback_notice is not None:
                    final_delivery_required = True
                    if activity is not None:
                        outcome = await activity.finish(
                            content=feedback_notice,
                            failed=False,
                        )
                        final_delivery_required = outcome.final_delivery_required
                    if final_delivery_required:
                        self._create_notice_delivery(session.id, event, feedback_notice)
                    self._inbound.mark_completed(event.key)
                    self._observe_turn(
                        event,
                        status="completed",
                        session_id=session.id,
                        started=started,
                    )
                    return
            if self._approvals is not None:
                try:
                    command = await self._approvals.handle_text(
                        user_id=self.owner_id,
                        actor_external_user_id=event.external_user_id,
                        text=event.content,
                        on_event=None if activity is None else activity.on_event,
                    )
                except asyncio.CancelledError:
                    failure_notice = self._failure_diagnostics(
                        session_id=session.id,
                        event=event,
                        fallback_error_code="channel_turn_interrupted",
                        stage="Gateway 运行期",
                        reason="Gateway 已停止或重启，当前任务已安全取消。",
                        suggestion="等待 Gateway 恢复 ready 后重新发送任务。",
                    )
                    if activity is not None:
                        await activity.finish(content=failure_notice, failed=True)
                    self._fail_running_event(event.key, "channel_turn_interrupted")
                    self._observe_turn(
                        event,
                        status="interrupted",
                        session_id=session.id,
                        started=started,
                        error_code="channel_turn_interrupted",
                    )
                    raise
                except Exception:
                    failure_notice = self._failure_diagnostics(
                        session_id=session.id,
                        event=event,
                        fallback_error_code="channel_control_failed",
                        stage="审批控制",
                        reason="审批控制处理失败，已安全停止。",
                        suggestion="请重新发送审批指令；若 Tool 已执行，系统不会自动重试。",
                    )
                    final_delivery_required = True
                    if activity is not None:
                        outcome = await activity.finish(
                            content=failure_notice,
                            failed=True,
                        )
                        final_delivery_required = outcome.final_delivery_required
                    if final_delivery_required:
                        self._create_failure_delivery(
                            session.id,
                            event,
                            content=failure_notice,
                        )
                    self._inbound.mark_failed(event.key, "channel_control_failed")
                    self._observe_turn(
                        event,
                        status="failed",
                        session_id=session.id,
                        started=started,
                        error_code="channel_control_failed",
                    )
                    return
                if command.handled:
                    visible = (
                        command.result.content
                        if command.result is not None
                        else command.notice
                    )
                    waiting_for_approval = (
                        command.result is not None
                        and command.result.approval_id is not None
                    )
                    final_delivery_required = True
                    final_delivery_offset = 0
                    final_reply_to_message_id: str | None = None
                    if activity is not None:
                        outcome = await activity.finish(
                            content=None if waiting_for_approval else visible,
                            failed=waiting_for_approval,
                        )
                        final_delivery_required = outcome.final_delivery_required
                        final_delivery_offset = outcome.final_delivery_offset
                        final_reply_to_message_id = outcome.final_reply_to_message_id
                    if command.result is not None:
                        self._create_result_delivery(
                            session.id,
                            event,
                            command.result,
                            message_delivery_required=final_delivery_required,
                            content_offset=final_delivery_offset,
                            reply_to_message_id=final_reply_to_message_id,
                        )
                    elif command.notice is not None and final_delivery_required:
                        self._create_notice_delivery(session.id, event, command.notice)
                    self._inbound.mark_completed(event.key)
                    self._observe_turn(
                        event,
                        status=(
                            "waiting_approval"
                            if command.result is not None
                            and command.result.approval_id is not None
                            else "completed"
                        ),
                        session_id=session.id,
                        started=started,
                        result=command.result,
                    )
                    return
            try:
                result = await self.service.handle_inbound(
                    user_id=self.owner_id,
                    channel=event.key.channel,
                    account_id=event.key.account_id,
                    external_conversation_id=event.external_conversation_id,
                    inbound_event_id=event.external_message_id,
                    text=event.content,
                    on_event=None if activity is None else activity.on_event,
                    trusted_owner=self._trusted_owner(event),
                    conversation_kind=self._conversation_kind(event),
                    identity_verified=self._verified_owner(event),
                )
            except asyncio.CancelledError:
                failure_notice = self._failure_diagnostics(
                    session_id=session.id,
                    event=event,
                    fallback_error_code="channel_turn_interrupted",
                    stage="Gateway 运行期",
                    reason="Gateway 已停止或重启，当前任务已安全取消。",
                    suggestion="等待 Gateway 恢复 ready 后重新发送任务。",
                )
                if activity is not None:
                    await activity.finish(content=failure_notice, failed=True)
                self._fail_running_event(event.key, "channel_turn_interrupted")
                self._observe_turn(
                    event,
                    status="interrupted",
                    session_id=session.id,
                    started=started,
                    error_code="channel_turn_interrupted",
                )
                raise
            except Exception as error:
                stage, reason, suggestion = _failure_profile(error)
                failure_notice = self._failure_diagnostics(
                    session_id=session.id,
                    event=event,
                    fallback_error_code=_failure_error_code(error),
                    stage=stage,
                    reason=reason,
                    suggestion=suggestion,
                    error=error,
                )
                final_delivery_required = True
                if activity is not None:
                    outcome = await activity.finish(
                        content=failure_notice,
                        failed=True,
                    )
                    final_delivery_required = outcome.final_delivery_required
                if final_delivery_required:
                    self._create_failure_delivery(
                        session.id,
                        event,
                        content=failure_notice,
                    )
                self._inbound.mark_failed(event.key, "channel_turn_failed")
                self._observe_turn(
                    event,
                    status="failed",
                    session_id=session.id,
                    started=started,
                    error_code="channel_turn_failed",
                )
                return

            final_delivery_required = True
            final_delivery_offset = 0
            final_reply_to_message_id: str | None = None
            if activity is not None:
                waiting_for_approval = result.approval_id is not None
                progress = activity.finalize(
                    content=None if waiting_for_approval else result.content,
                    failed=waiting_for_approval,
                )
                if result.message_id is not None and not waiting_for_approval:
                    self._messages.save_experience_trace(
                        result.message_id,
                        progress_to_metadata(progress),
                    )
                outcome = await activity.finish(
                    content=None if waiting_for_approval else result.content,
                    failed=waiting_for_approval,
                    progress=progress,
                )
                final_delivery_required = outcome.final_delivery_required
                final_delivery_offset = outcome.final_delivery_offset
                final_reply_to_message_id = outcome.final_reply_to_message_id

            self._create_result_delivery(
                session.id,
                event,
                result,
                message_delivery_required=final_delivery_required,
                content_offset=final_delivery_offset,
                reply_to_message_id=final_reply_to_message_id,
            )
            self._inbound.mark_completed(event.key)
            self._observe_turn(
                event,
                status=(
                    "waiting_approval" if result.approval_id is not None else "completed"
                ),
                session_id=session.id,
                started=started,
                result=result,
            )

    async def _handle_feedback_command(self, event: StoredInboundEvent) -> str | None:
        """处理 /good、/bad；不是反馈命令时返回 ``None`` 让消息继续进入模型。

        任何内部失败都收口为一句安全提示：记录反馈失败不应该让整条消息处理失败，
        更不应该把内部错误码回显给 IM。
        """
        assert self._feedback is not None
        try:
            outcome = await self._feedback.handle_text(
                user_id=self.owner_id,
                actor_external_user_id=event.external_user_id,
                text=event.content,
                channel=event.key.channel,
                account_id=event.key.account_id,
                reply_to_platform_message_id=event.replied_to_message_id,
            )
        except Exception:  # noqa: BLE001 - 反馈命令必须收口为稳定提示
            return "记录反馈失败，请稍后重试。"
        if not outcome.handled:
            return None
        return outcome.notice or "已处理。"

    def _trusted_owner(self, event: StoredInboundEvent) -> bool:
        """只有配置 Owner 的点对点消息可以携带自动化信任。"""
        return self._verified_owner(event) and event.chat_type == "p2p"

    def _verified_owner(self, event: StoredInboundEvent) -> bool:
        """只依据 Core 配置的外部身份映射判断发言者是否为 Owner。"""
        return event.external_user_id == self._owner_external_user_id

    @staticmethod
    def _conversation_kind(event: StoredInboundEvent) -> ConversationKind:
        """把平台已校验 chat_type 收窄为 Memory 的封闭会话类型。"""
        if event.chat_type == "p2p":
            return "direct"
        if event.chat_type == "group":
            return "group"
        return "unknown"

    def _control_notice(self, event: StoredInboundEvent, session_id: int) -> str | None:
        """把 Owner 控制命令分发到不进入模型的处理分支。"""
        parts = event.content.split()
        if not parts:
            return None
        if parts[0] == "/permissions":
            return self._permission_notice(parts, event)
        if parts[0] == "/reset":
            return self._reset_notice(parts, event, session_id)
        return None

    def _reset_notice(
        self,
        parts: list[str],
        event: StoredInboundEvent,
        session_id: int,
    ) -> str:
        """重置当前会话的模型上下文，让后续消息从干净历史开始。"""
        if not self._trusted_owner(event):
            return "只有 Owner 私聊可以重置会话上下文。"
        if len(parts) != 1:
            return "用法：/reset"
        try:
            compaction = self._messages.reset_context(session_id)
        except Exception:  # noqa: BLE001 - 控制命令必须收口为稳定提示
            return "会话上下文重置失败，历史保持不变。"
        if compaction is None:
            return "会话上下文已经是干净的，无需重置。"
        return (
            "会话上下文已重置：之前的历史不再进入模型，消息记录、Tool 和文件都没有被改动。"
        )

    def _permission_notice(
        self,
        parts: list[str],
        event: StoredInboundEvent,
    ) -> str | None:
        """处理不进入模型的权限查询/切换命令，并返回可投递提示。"""
        if not self._trusted_owner(event):
            return "只有 Owner 私聊可以查看或切换权限模式。"
        if len(parts) == 1:
            return f"当前权限模式：{self._permission_state.mode.value}"
        if len(parts) != 2:
            return "用法：/permissions safe|smart|autopilot|yolo"
        try:
            selected = PermissionMode(parts[1])
        except ValueError:
            return "用法：/permissions safe|smart|autopilot|yolo"
        try:
            self._permission_state.set_mode(
                selected,
                user_id=self.owner_id,
                source=self._channel,
            )
        except Exception:  # noqa: BLE001 - 控制命令必须收口为稳定提示
            return "权限模式切换失败，原模式保持不变。"
        return f"权限模式已切换为：{selected.value}"

    def _create_result_delivery(
        self,
        session_id: int,
        event: StoredInboundEvent,
        result: TurnResult,
        *,
        message_delivery_required: bool = True,
        content_offset: int = 0,
        reply_to_message_id: str | None = None,
    ) -> None:
        """按平台终态创建完整 fallback、卡片后缀或 durable Approval Outbox。"""
        if result.message_id is not None:
            if not message_delivery_required:
                return
            self._create_message_delivery(
                result.message_id,
                result.content,
                event,
                content_offset=content_offset,
                reply_to_message_id=reply_to_message_id,
            )
            return
        if result.approval_id is None:
            return
        self._create_approval_delivery(session_id, event, result.approval_id)

    def _create_message_delivery(
        self,
        message_id: int,
        content: str,
        event: StoredInboundEvent,
        *,
        content_offset: int = 0,
        reply_to_message_id: str | None = None,
    ) -> None:
        """把完整 fallback 或卡片未展示后缀写成可恢复文本分片。"""
        if not 0 <= content_offset <= len(content):
            raise ChannelStateError("invalid_delivery_content_offset")
        remaining = content[content_offset:]
        if not remaining:
            return
        self._deliveries.create_parts(
            message_id=message_id,
            channel=event.key.channel,
            account_id=event.key.account_id,
            external_conversation_id=event.external_conversation_id,
            reply_to_message_id=reply_to_message_id or event.reply_to_message_id,
            kind="message",
            contents=split_message(
                remaining,
                max_chars=self._message_max_chars,
                preserve_code_fences=self._channel == "telegram",
            ),
        )

    def _create_approval_delivery(
        self,
        session_id: int,
        event: StoredInboundEvent,
        approval_id: int,
    ) -> None:
        """创建 durable Approval card，构造失败时退化为普通文本提示。"""
        prompt: ApprovalEnvelope | None = None
        if self._approvals is not None:
            try:
                prompt = self._approvals.prompt(
                    user_id=self.owner_id,
                    approval_id=approval_id,
                )
            except Exception:
                prompt = None
        if prompt is None:
            self._create_notice_delivery(
                session_id,
                event,
                f"需要审批，编号 #{approval_id}。请在 Lobster0 中处理。",
            )
            return
        notice = self._messages.create_channel_notice(session_id, prompt.fallback_text)
        self._deliveries.create_parts(
            message_id=notice.id,
            channel=event.key.channel,
            account_id=event.key.account_id,
            external_conversation_id=event.external_conversation_id,
            reply_to_message_id=event.reply_to_message_id,
            kind="approval",
            contents=(approval_delivery_payload(prompt),),
        )

    def _create_notice_delivery(
        self,
        session_id: int,
        event: StoredInboundEvent,
        content: str,
        *,
        kind: DeliveryKind = "message",
    ) -> None:
        """保存 Channel 控制提示并建立可恢复 Delivery。"""
        notice = self._messages.create_channel_notice(session_id, content)
        self._deliveries.create_parts(
            message_id=notice.id,
            channel=event.key.channel,
            account_id=event.key.account_id,
            external_conversation_id=event.external_conversation_id,
            reply_to_message_id=event.reply_to_message_id,
            kind=kind,
            contents=split_message(
                notice.content,
                max_chars=self._message_max_chars,
                preserve_code_fences=self._channel == "telegram",
            ),
        )

    def _create_failure_delivery(
        self,
        session_id: int,
        event: StoredInboundEvent,
        *,
        content: str = _FAILURE_NOTICE,
    ) -> None:
        """把任意内部失败压缩为固定、可投递且不含异常正文的提示。"""
        notice = self._messages.create_channel_notice(session_id, content)
        self._deliveries.create_parts(
            message_id=notice.id,
            channel=event.key.channel,
            account_id=event.key.account_id,
            external_conversation_id=event.external_conversation_id,
            reply_to_message_id=event.reply_to_message_id,
            kind="message",
            contents=split_message(
                notice.content,
                max_chars=self._message_max_chars,
                preserve_code_fences=self._channel == "telegram",
            ),
        )

    def _failure_diagnostics(
        self,
        *,
        session_id: int,
        event: StoredInboundEvent,
        fallback_error_code: str,
        stage: str,
        reason: str,
        suggestion: str,
        error: Exception | None = None,
    ) -> str:
        """生成不含异常正文的诊断，并关联 Turn、ToolRun 与安全整数指标。"""
        turn_id: int | None = None
        persisted_error_code: str | None = None
        try:
            turn = self._turns.get_by_inbound(
                session_id,
                event.external_message_id,
            )
            turn_id = turn.id
            persisted_error_code = turn.error_code
        except ConversationStateError:
            pass
        error_code = _safe_error_code(persisted_error_code, fallback_error_code)
        tool_count: int | None = None
        if self._observer is not None and turn_id is not None:
            try:
                tool_count = self._observer.tool_count(turn_id)
            except Exception:
                tool_count = None
        if tool_count == 0:
            tool_status = "0 个真实 ToolRun，未发生 Tool 副作用。"
        elif tool_count is None:
            tool_status = "暂时无法读取；请检查 ToolRun 审计记录确认是否有副作用。"
        else:
            tool_status = (
                f"已记录 {tool_count} 个真实 ToolRun；系统不会自动重试，"
                "请检查 ToolRun 确认副作用。"
            )
        references: list[str] = []
        if turn_id is not None:
            references.append(f"Turn #{turn_id}")
        if event.storage_rowid > 0:
            references.append(f"Event #{event.storage_rowid}")
        debug_reference = " · ".join(references) or "未生成内部编号"
        diagnostics = [
            f"- 失败阶段：{stage}",
            f"- 错误码：`{error_code}`",
            f"- 原因：{reason}",
            f"- 调试编号：{debug_reference}",
        ]
        if isinstance(error, AgentNoProgressError):
            diagnostics.extend(
                (
                    f"- 当前模型轮次：{error.model_iteration}",
                    f"- 连续无进展轮次：{error.no_progress_iterations}",
                )
            )
        diagnostics.extend(
            (
                f"- Tool 状态：{tool_status}",
                f"- 下一步：{suggestion}",
            )
        )
        return "\n".join(diagnostics)

    async def _recover_stale(self) -> None:
        """恢复遗留 Inbox/Delivery，绝不重放已经开始的 Turn。"""
        self._deliveries.recover_sending(self._channel, self._account_id)
        for event in self._inbound.list_by_status(
            self._channel,
            self._account_id,
            "running",
        ):
            if event.session_id is None:
                self._inbound.recover_running(
                    event.key,
                    "queued",
                    "channel_queue_recovered",
                )
                continue
            try:
                turn = self._turns.get_by_inbound(
                    event.session_id,
                    event.external_message_id,
                )
            except ConversationStateError:
                self._inbound.recover_running(
                    event.key,
                    "queued",
                    "channel_queue_recovered",
                )
                continue
            if turn.status == "completed":
                assistant = self._messages.final_assistant_for_turn(turn.id)
                final_delivery_required = True
                final_delivery_offset = 0
                final_reply_to_message_id: str | None = None
                if self._experience is not None:
                    activity = self._experience.activity(event)
                    stored_trace = self._messages.experience_trace(assistant.id)
                    progress = (
                        None
                        if stored_trace is None
                        else progress_from_metadata(stored_trace, assistant.content)
                    )
                    outcome = await activity.finish(
                        content=assistant.content,
                        failed=False,
                        progress=progress,
                    )
                    final_delivery_required = outcome.final_delivery_required
                    final_delivery_offset = outcome.final_delivery_offset
                    final_reply_to_message_id = outcome.final_reply_to_message_id
                if final_delivery_required:
                    self._create_message_delivery(
                        assistant.id,
                        assistant.content,
                        event,
                        content_offset=final_delivery_offset,
                        reply_to_message_id=final_reply_to_message_id,
                    )
                self._inbound.recover_running(event.key, "completed", None)
                continue
            if turn.status == "waiting_approval":
                approval_id = turn.runtime_snapshot.get("approval_id")
                if type(approval_id) is int and approval_id > 0:
                    self._create_approval_delivery(
                        turn.session_id,
                        event,
                        approval_id,
                    )
                    self._inbound.recover_running(event.key, "completed", None)
                    continue
            if turn.status in {"queued", "running"}:
                self._turns.fail(
                    turn.id,
                    "channel_turn_interrupted",
                    "channel Turn was interrupted by restart",
                )
            self._inbound.recover_running(
                event.key,
                "failed",
                "channel_turn_interrupted",
            )

    def _fail_running_event(self, key: InboundEventKey, error_code: str) -> None:
        """best-effort 结算 Worker 异常，不覆盖已有终态。"""
        try:
            self._inbound.mark_failed(key, error_code)
        except ChannelStateError:
            return

    def _observe_inbound(
        self,
        message: InboundMessage,
        event: StoredInboundEvent,
        status: str,
        enqueued: bool,
    ) -> None:
        """把首次落库/重复投递映射为不含正文与完整外部 ID 的 Audit。"""
        if self._observer is None:
            return
        try:
            self._observer.inbound(
                channel=event.key.channel,
                account_id=event.key.account_id,
                external_message_id=event.external_message_id,
                external_conversation_id=message.external_conversation_id,
                status=status,
                event_row_id=(event.storage_rowid if event.storage_rowid > 0 else None),
                enqueued=enqueued,
                user_id=self.owner_id,
            )
        except Exception:
            return

    def _observe_turn(
        self,
        event: StoredInboundEvent,
        *,
        status: str,
        session_id: int,
        started: float,
        result: TurnResult | None = None,
        error_code: str | None = None,
    ) -> None:
        """记录一个 Turn 的安全内部关联、排队时间和终态耗时。"""
        if self._observer is None:
            return
        turn_id = None if result is None else result.turn_id
        internal_message_id = None if result is None else result.message_id
        if turn_id is None and status != "started":
            try:
                turn_id = self._turns.get_by_inbound(
                    session_id,
                    event.external_message_id,
                ).id
            except ConversationStateError:
                turn_id = None
        queue_wait_ms = max(
            0,
            int((datetime.now(UTC) - event.received_at).total_seconds() * 1000),
        )
        try:
            self._observer.turn(
                channel=event.key.channel,
                account_id=event.key.account_id,
                external_message_id=event.external_message_id,
                status=status,
                event_row_id=(event.storage_rowid if event.storage_rowid > 0 else None),
                user_id=self.owner_id,
                session_id=session_id,
                turn_id=turn_id,
                internal_message_id=internal_message_id,
                queue_wait_ms=queue_wait_ms,
                agent_duration_ms=(
                    None
                    if status == "started"
                    else max(0, int((time.monotonic() - started) * 1000))
                ),
                tool_count=(
                    None if status == "started" else self._observer.tool_count(turn_id)
                ),
                approval_state=(
                    None
                    if status == "started"
                    else (
                        "waiting"
                        if result is not None and result.approval_id is not None
                        else "none"
                    )
                ),
                error_code=error_code,
            )
        except Exception:
            return

    @asynccontextmanager
    async def _conversation_lock(self, conversation_id: str) -> AsyncIterator[None]:
        """按会话串行，并在最后一个等待者退出后释放锁对象。"""
        async with self._locks_guard:
            entry = self._locks.get(conversation_id)
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
                self._locks[conversation_id] = entry
            entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._locks_guard:
                entry.users -= 1
                if entry.users == 0:
                    self._locks.pop(conversation_id, None)


def _failure_profile(error: Exception) -> tuple[str, str, str]:
    """把异常类型映射为安全失败阶段、原因和行动建议。"""
    if isinstance(error, AgentNoProgressError):
        return (
            "Agent Tool Loop",
            "连续多轮没有新的成功 Tool 结果，已停止重复执行。",
            "请检查 Claw Trail 与 ToolRun；调整请求后重试。",
        )
    if isinstance(error, AgentLoopLimitError):
        return (
            "Agent Tool Loop",
            "模型在无 Tool 的预算收口轮仍请求 Tool；最后一次 Tool 请求未执行。",
            "请检查 Claw Trail 与 ToolRun；拆分任务或调整预算配置后重试。",
        )
    if isinstance(error, ProviderProtocolError):
        if str(error).startswith("model provider rejected the request with status"):
            return (
                "模型请求",
                "模型服务直接拒绝了这次请求（协议或参数校验未通过），未生成任何工具调用。",
                "请重试；若持续失败，请检查 Turn 的 error_message 获取服务商返回的具体原因。",
            )
        return (
            "模型响应校验",
            "模型生成的工具参数格式错误，已安全停止。",
            "请重试，或把任务拆成较短的步骤。",
        )
    if isinstance(error, ProviderTimeoutError):
        return "模型请求", "模型服务响应超时。", "请稍后重试。"
    if isinstance(error, ProviderRateLimitError):
        return "模型请求", "模型服务当前限流。", "请稍后重试。"
    if isinstance(error, ProviderAuthenticationError):
        return "模型请求", "模型服务认证失败。", "请检查本机 API Key 配置。"
    if isinstance(error, ProviderServerError):
        return "模型请求", "模型服务暂时不可用。", "请稍后重试。"
    return "Agent 执行", "消息处理失败，已安全停止。", "请稍后重试。"


def _failure_error_code(error: Exception) -> str:
    """把公开异常类型映射为稳定错误码，未知异常使用 Channel 兜底码。"""
    mappings = (
        (AgentNoProgressError, "loop_no_progress"),
        (AgentLoopLimitError, "loop_limit"),
        (ProviderAuthenticationError, "provider_authentication"),
        (ProviderRateLimitError, "provider_rate_limit"),
        (ProviderTimeoutError, "provider_timeout"),
        (ProviderProtocolError, "provider_protocol"),
        (ProviderServerError, "provider_server"),
    )
    for error_type, error_code in mappings:
        if isinstance(error, error_type):
            return error_code
    return "channel_turn_failed"


def _safe_error_code(value: str | None, fallback: str) -> str:
    """只接受小写 ASCII 稳定码，非法持久值退回调用方给定安全码。"""
    if (
        isinstance(value, str)
        and 0 < len(value) <= 64
        and all(
            character == "_"
            or "a" <= character <= "z"
            or "0" <= character <= "9"
            for character in value
        )
    ):
        return value
    return fallback
