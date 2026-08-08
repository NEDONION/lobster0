"""Unicode-safe 消息分片与可恢复 DeliveryWorker。"""

import asyncio
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from miniclaw.channels.approvals import (
    ApprovalEnvelope,
    feishu_approval_prompt,
    parse_approval_delivery_payload,
)
from miniclaw.channels.base import (
    ChannelTransport,
    ChannelTransportError,
    OutboundMessage,
)
from miniclaw.channels.observability import ChannelObserver
from miniclaw.storage.channels import DeliveryRepository, StoredDelivery


def split_message(
    content: str,
    *,
    max_chars: int,
    preserve_code_fences: bool = False,
) -> tuple[str, ...]:
    """按 Unicode 边界切分回复，可为跨片 Markdown code fence 补配对标记。"""
    if not isinstance(content, str) or not content:
        raise ValueError("delivery content must not be empty")
    if type(max_chars) is not int or max_chars < 8:
        raise ValueError("max_chars must be at least 8")
    if len(content) <= max_chars:
        return (content,)

    estimated_parts = 2
    chunks: tuple[str, ...] = ()
    for _ in range(12):
        prefix_length = len(f"[{estimated_parts}/{estimated_parts}] ")
        fence_reserve = _fence_reserve(content) if preserve_code_fences else 0
        budget = max_chars - prefix_length - fence_reserve
        if budget <= 0:
            raise ValueError("max_chars is too small for multipart prefix")
        chunks = _split_plain(content, budget)
        actual_parts = len(chunks)
        if actual_parts == estimated_parts:
            break
        estimated_parts = actual_parts
    else:
        raise ValueError("multipart prefix did not stabilize")

    rendered = _balance_code_fences(chunks) if preserve_code_fences else chunks
    total = len(rendered)
    parts = tuple(f"[{index}/{total}] {chunk}" for index, chunk in enumerate(rendered, 1))
    if any(len(part) > max_chars for part in parts):
        raise ValueError("multipart output exceeds max_chars")
    return parts


def _balance_code_fences(chunks: tuple[str, ...]) -> tuple[str, ...]:
    """仅添加 synthetic fence，不删除或重复原始代码正文。"""
    rendered: list[str] = []
    active_opener: str | None = None
    for chunk in chunks:
        started_inside = active_opener
        active_opener = _fence_state_after(chunk, active_opener)
        prefix = f"{started_inside}\n" if started_inside is not None else ""
        suffix = "\n```" if active_opener is not None else ""
        rendered.append(f"{prefix}{chunk}{suffix}")
    return tuple(rendered)


def _fence_reserve(content: str) -> int:
    """预留 reopen + newline + closing fence 的最坏长度。"""
    longest = 3
    position = 0
    while True:
        fence = content.find("```", position)
        if fence < 0:
            break
        line_end = content.find("\n", fence)
        candidate = content[fence : line_end if line_end >= 0 else fence + 3]
        longest = max(longest, min(64, len(candidate)))
        position = fence + 3
    return longest + 5


def _fence_state_after(chunk: str, active: str | None) -> str | None:
    """扫描原始 chunk 中的 fence，返回下一片需要重开的短 opener。"""
    position = 0
    while True:
        fence = chunk.find("```", position)
        if fence < 0:
            return active
        if active is None:
            line_end = chunk.find("\n", fence)
            candidate = chunk[fence : line_end if line_end >= 0 else fence + 3]
            active = candidate if len(candidate) <= 64 else "```"
        else:
            active = None
        position = fence + 3


def _split_plain(content: str, budget: int) -> tuple[str, ...]:
    """在不丢字符的前提下按优先分隔符生成裸分片。"""
    remaining = content
    chunks: list[str] = []
    while len(remaining) > budget:
        cut = _preferred_cut(remaining, budget)
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def _preferred_cut(content: str, budget: int) -> int:
    """优先在段落/行/空格之后断开，否则按 Python Unicode 字符切断。"""
    window = content[:budget]
    for separator in ("\n\n", "\n", " "):
        position = window.rfind(separator)
        if position >= 0:
            return position + len(separator)
    return budget


class DeliveryWorker:
    """从 SQLite claim Delivery，映射错误并更新可恢复终态。"""

    def __init__(
        self,
        *,
        transport: ChannelTransport,
        repository: DeliveryRepository,
        channel: str,
        account_id: str,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        jitter: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
        poll_interval: float = 0.25,
        message_max_chars: int = 30_000,
        observer: ChannelObserver | None = None,
    ) -> None:
        """绑定 Transport、Outbox 和有限重试预算。"""
        if (
            max_attempts <= 0
            or base_delay <= 0
            or max_delay <= 0
            or poll_interval <= 0
            or message_max_chars < 8
        ):
            raise ValueError("DeliveryWorker limits must be positive")
        self._transport = transport
        self._repository = repository
        self._channel = channel
        self._account_id = account_id
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._jitter = jitter or (lambda: random.uniform(0.8, 1.2))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._poll_interval = poll_interval
        self._message_max_chars = message_max_chars
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._observer = observer

    def recover(self) -> int:
        """把遗留 sending 变成 unknown，再用相同 UUID 安全重新排队。"""
        self._repository.recover_sending(self._channel, self._account_id)
        return self._repository.recover_unknown(
            self._channel,
            self._account_id,
            max_attempts=self._max_attempts,
        )

    async def run_once(self) -> bool:
        """处理当前最早可发送 Delivery；没有候选时返回 False。"""
        delivery = self._repository.claim_next(self._channel, self._account_id)
        if delivery is None:
            return False
        started = time.monotonic()
        self._observe_delivery(delivery, started=started)
        try:
            if delivery.delivery_kind == "approval":
                await self._send_approval(delivery)
                return True
            message = OutboundMessage(
                channel=delivery.channel,
                account_id=delivery.account_id,
                external_conversation_id=delivery.external_conversation_id,
                reply_to_message_id=delivery.reply_to_message_id,
                content=delivery.content,
                kind=delivery.delivery_kind,
            )
            try:
                receipt = await self._transport.send(
                    message,
                    idempotency_key=delivery.idempotency_key,
                )
            except asyncio.CancelledError:
                self._repository.mark_unknown(
                    delivery.id,
                    "channel_delivery_unknown",
                )
                raise
            except ChannelTransportError as error:
                self._handle_transport_error(delivery, error)
            except Exception:
                self._repository.mark_failed(delivery.id, "channel_send_failed")
            else:
                if not receipt.platform_message_id:
                    self._repository.mark_unknown(
                        delivery.id,
                        "channel_delivery_unknown",
                    )
                else:
                    self._repository.mark_sent(delivery.id, receipt.platform_message_id)
        finally:
            self._observe_delivery_id(delivery.id, started=started)
        return True

    async def _send_approval(self, delivery: StoredDelivery) -> None:
        """发送 durable Approval card；平台不支持时原子创建 Markdown fallback。"""
        try:
            parsed = parse_approval_delivery_payload(delivery.content)
        except ValueError:
            self._repository.mark_failed(
                delivery.id,
                "channel_approval_payload_invalid",
            )
            return
        fallback_text = parsed.fallback_text
        send_approval = getattr(self._transport, "send_approval", None)
        if isinstance(parsed, ApprovalEnvelope) and callable(send_approval):
            try:
                receipt = await send_approval(
                    conversation_id=delivery.external_conversation_id,
                    reply_to_message_id=delivery.reply_to_message_id,
                    envelope=parsed,
                    idempotency_key=delivery.idempotency_key,
                )
            except asyncio.CancelledError:
                self._repository.mark_unknown(delivery.id, "channel_delivery_unknown")
                raise
            except Exception:
                self._fallback_approval(
                    delivery,
                    fallback_text,
                    "channel_interactive_failed",
                )
                return
            if not receipt.platform_message_id:
                self._repository.mark_unknown(delivery.id, "channel_delivery_unknown")
            else:
                self._repository.mark_sent(delivery.id, receipt.platform_message_id)
            return
        prompt = (
            feishu_approval_prompt(parsed)
            if isinstance(parsed, ApprovalEnvelope)
            else parsed
        )
        send_card = getattr(self._transport, "send_card", None)
        if not callable(send_card):
            self._fallback_approval(
                delivery,
                fallback_text,
                "channel_interactive_unsupported",
            )
            return
        try:
            receipt = await send_card(
                conversation_id=delivery.external_conversation_id,
                reply_to_message_id=delivery.reply_to_message_id,
                card=prompt.card,
                idempotency_key=delivery.idempotency_key,
            )
        except asyncio.CancelledError:
            self._repository.mark_unknown(
                delivery.id,
                "channel_delivery_unknown",
            )
            raise
        except Exception:
            self._fallback_approval(
                delivery,
                fallback_text,
                "channel_interactive_failed",
            )
            return
        if not receipt.platform_message_id:
            self._repository.mark_unknown(
                delivery.id,
                "channel_delivery_unknown",
            )
            return
        self._repository.mark_sent(delivery.id, receipt.platform_message_id)

    def _fallback_approval(
        self,
        delivery: StoredDelivery,
        fallback_text: str,
        error_code: str,
    ) -> None:
        """supersede Card 并在同一内部消息下创建可恢复普通 Delivery。"""
        self._repository.mark_superseded(delivery.id, error_code)
        if delivery.message_id is None:
            return
        self._repository.create_parts(
            message_id=delivery.message_id,
            channel=delivery.channel,
            account_id=delivery.account_id,
            external_conversation_id=delivery.external_conversation_id,
            reply_to_message_id=delivery.reply_to_message_id,
            kind="message",
            contents=split_message(
                fallback_text,
                max_chars=self._message_max_chars,
            ),
        )

    async def start(self) -> None:
        """恢复 Outbox 并启动单一后台发送循环。"""
        if self._task is not None:
            return
        self.recover()
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="delivery-worker")

    async def stop(self) -> None:
        """停止后台循环；发送中的取消会留下 unknown 而不是伪成功。"""
        if self._task is None:
            return
        self._stopping.set()
        self._wake.set()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def notify(self) -> None:
        """提示后台循环立即重新扫描 Outbox。"""
        self._wake.set()

    async def _run(self) -> None:
        """持续发送全部到期候选，空闲时按事件或短周期唤醒。"""
        while not self._stopping.is_set():
            processed = await self.run_once()
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    def _handle_transport_error(
        self,
        delivery: StoredDelivery,
        error: ChannelTransportError,
    ) -> None:
        """按稳定属性把 Transport 错误映射到 unknown/retry/failed。"""
        if error.unknown:
            self._repository.mark_unknown(delivery.id, error.code)
            return
        if error.retryable and delivery.attempts < self._max_attempts:
            exponent = max(0, delivery.attempts - 1)
            if error.retry_after is None:
                delay = min(self._max_delay, self._base_delay * (2**exponent))
                delay *= max(0.0, self._jitter())
            else:
                delay = min(self._max_delay, max(0.0, error.retry_after))
            self._repository.mark_retry_wait(
                delivery.id,
                error.code,
                self._clock() + timedelta(seconds=delay),
            )
            return
        self._repository.mark_failed(delivery.id, error.code)

    def _observe_delivery(
        self,
        delivery: StoredDelivery,
        *,
        started: float,
    ) -> None:
        """记录 claim 或终态；正文、目标和平台消息 ID 永不进入 Observer。"""
        if self._observer is None:
            return
        retry_decisions = {
            "sending": None,
            "sent": "none",
            "retry_wait": "retry",
            "unknown": "unknown",
            "failed": "terminal",
            "superseded": "fallback",
        }
        retry_decision = retry_decisions.get(delivery.status)
        if delivery.status not in retry_decisions:
            return
        try:
            user_id, session_id, turn_id = self._observer.message_context(
                delivery.message_id
            )
            self._observer.delivery(
                channel=delivery.channel,
                account_id=delivery.account_id,
                external_message_id=delivery.reply_to_message_id or delivery.idempotency_key,
                delivery_id=delivery.id,
                status=delivery.status,
                internal_message_id=delivery.message_id,
                delivery_duration_ms=(
                    None
                    if delivery.status == "sending"
                    else max(0, int((time.monotonic() - started) * 1000))
                ),
                attempts=delivery.attempts,
                error_code=delivery.last_error_code,
                retry_decision=retry_decision,
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
            )
        except Exception:
            return

    def _observe_delivery_id(self, delivery_id: int, *, started: float) -> None:
        """读取 Repository 已提交的终态后写入可观测事件。"""
        if self._observer is None:
            return
        try:
            delivery = self._repository.get(delivery_id)
        except Exception:
            return
        self._observe_delivery(delivery, started=started)
