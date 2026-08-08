"""SQLite-backed Channel Inbox 的有界队列、Worker 与重启恢复。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from miniclaw.agent.events import RunEventHandler
from miniclaw.agent.turn import TurnResult
from miniclaw.channels.base import InboundMessage
from miniclaw.channels.delivery import split_message
from miniclaw.providers.base import StreamHandler
from miniclaw.storage.channels import (
    ChannelIdentityRepository,
    ChannelStateError,
    DeliveryRepository,
    InboundEventKey,
    InboundEventRepository,
    StoredInboundEvent,
)
from miniclaw.storage.conversations import (
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
    ) -> TurnResult:
        """处理一条已经完成 Channel 校验的消息。"""
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

    async def receive(self, message: InboundMessage) -> InboundAcceptance:
        """先持久化消息，再 best-effort 写入有界内存队列。"""
        if message.channel != self._channel or message.account_id != self._account_id:
            raise ChannelStateError("channel_account_mismatch")
        result = self._inbound.record(message)
        if not result.inserted:
            return InboundAcceptance(False, False)
        enqueued = await self._enqueue(result.event.key)
        return InboundAcceptance(True, enqueued)

    async def start(self) -> None:
        """恢复遗留状态并启动 feeder 与有限数量 Worker。"""
        if self._workers or self._feeder is not None:
            return
        self._stopping.clear()
        self._recover_stale()
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
            try:
                result = await self.service.handle_inbound(
                    user_id=self.owner_id,
                    channel=event.key.channel,
                    account_id=event.key.account_id,
                    external_conversation_id=event.external_conversation_id,
                    inbound_event_id=event.external_message_id,
                    text=event.content,
                )
            except asyncio.CancelledError:
                self._fail_running_event(event.key, "channel_turn_interrupted")
                raise
            except Exception:
                self._create_failure_delivery(session.id, event)
                self._inbound.mark_failed(event.key, "channel_turn_failed")
                return

            if result.message_id is not None:
                self._deliveries.create_parts(
                    message_id=result.message_id,
                    channel=event.key.channel,
                    account_id=event.key.account_id,
                    external_conversation_id=event.external_conversation_id,
                    reply_to_message_id=event.reply_to_message_id,
                    kind="message",
                    contents=split_message(
                        result.content,
                        max_chars=self._message_max_chars,
                    ),
                )
            elif result.approval_id is not None:
                notice = self._messages.create_channel_notice(
                    session.id,
                    f"需要审批，编号 #{result.approval_id}。请在 MiniClaw 中处理。",
                )
                self._deliveries.create_parts(
                    message_id=notice.id,
                    channel=event.key.channel,
                    account_id=event.key.account_id,
                    external_conversation_id=event.external_conversation_id,
                    reply_to_message_id=event.reply_to_message_id,
                    kind="approval",
                    contents=split_message(
                        notice.content,
                        max_chars=self._message_max_chars,
                    ),
                )
            self._inbound.mark_completed(event.key)

    def _create_failure_delivery(self, session_id: int, event: StoredInboundEvent) -> None:
        """把任意内部失败压缩为固定、可投递且不含异常正文的提示。"""
        notice = self._messages.create_channel_notice(session_id, _FAILURE_NOTICE)
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
            ),
        )

    def _recover_stale(self) -> None:
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
                self._deliveries.create_parts(
                    message_id=assistant.id,
                    channel=event.key.channel,
                    account_id=event.key.account_id,
                    external_conversation_id=event.external_conversation_id,
                    reply_to_message_id=event.reply_to_message_id,
                    kind="message",
                    contents=split_message(
                        assistant.content,
                        max_chars=self._message_max_chars,
                    ),
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
