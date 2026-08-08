"""非阻塞 Turn capture、Flush 编排和 crash checkpoint 恢复。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from miniclaw.memory.buffer import (
    MemoryBuffer,
    MemoryBufferRepository,
    MemoryBufferStateError,
)
from miniclaw.memory.models import DisclosureContext
from miniclaw.memory.policy import MemoryDisclosurePolicy, MemoryPolicyError
from miniclaw.memory.repository import (
    MemoryFlushRun,
    MemoryRunRepository,
    MemoryStateError,
)
from miniclaw.storage.database import Database


@dataclass(frozen=True, slots=True)
class FlushSourceMessage:
    """提供给提取器的 Owner-scoped 有界消息，不携带平台外部 ID。"""

    id: int
    session_id: int
    channel: str
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class FlushOutcome:
    """描述一轮 Flush 的稳定 checkpoint 结果。"""

    status: str
    run_id: int | None
    error_code: str | None = None


class FlushHandler(Protocol):
    """定义提取/Markdown 与 disposable Projection 的两阶段处理边界。"""

    async def write_markdown(
        self,
        run: MemoryFlushRun,
        messages: tuple[FlushSourceMessage, ...],
    ) -> None:
        """幂等提取并提交 Markdown 真相。"""
        ...

    async def project(self, run: MemoryFlushRun) -> None:
        """从已提交 Markdown 幂等刷新 SQLite Projection。"""
        ...


class MemoryCapture:
    """把已完成 trusted private Turn 转成 durable source-range receipt。"""

    def __init__(
        self,
        buffers: MemoryBufferRepository,
        *,
        wake: Callable[[], None] | None = None,
        policy: MemoryDisclosurePolicy | None = None,
        wake_threshold: int = 5,
    ) -> None:
        """绑定 buffer、可选 worker wake-up 和 Disclosure Policy。"""
        if type(wake_threshold) is not int or not 1 <= wake_threshold <= 100:
            raise ValueError("memory wake_threshold must be between 1 and 100")
        self._buffers = buffers
        self._wake = wake
        self._policy = policy or MemoryDisclosurePolicy()
        self._wake_threshold = wake_threshold

    def capture_completed(
        self,
        *,
        owner_id: int,
        session_id: int,
        turn_id: int,
        disclosure: DisclosureContext,
    ) -> MemoryBuffer | None:
        """只捕获 Policy 判定为 private 的 completed Turn。"""
        try:
            decision = self._policy.decide(disclosure)
        except MemoryPolicyError:
            return None
        if decision.capture_scope != "private" or disclosure.owner_id != owner_id:
            return None
        receipt = self._buffers.capture_completed_turn(
            owner_id=owner_id,
            session_id=session_id,
            turn_id=turn_id,
            capture_scope="private",
        )
        if (
            self._wake is not None
            and self._buffers.pending_count(owner_id) >= self._wake_threshold
        ):
            self._wake()
        return receipt

    def flush(self) -> None:
        """为 `/new`、pre-compaction、显式 flush 和 shutdown 强制唤醒 Worker。"""
        if self._wake is not None:
            self._wake()


class FlushCoordinator:
    """组批、claim、checkpoint 并恢复中断的 Memory Flush。"""

    def __init__(
        self,
        database: Database,
        buffers: MemoryBufferRepository,
        runs: MemoryRunRepository,
        handler: FlushHandler,
        *,
        extractor: str,
        prompt_hash: str,
        batch_size: int = 5,
        lease_seconds: int = 60,
    ) -> None:
        """绑定 durable Repository、两阶段 Handler 和有界批次/lease。"""
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("memory batch_size must be between 1 and 100")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("memory lease_seconds must be between 1 and 3600")
        self._database = database
        self._buffers = buffers
        self._runs = runs
        self._handler = handler
        self._extractor = extractor
        self._prompt_hash = prompt_hash
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds

    async def run_once(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> FlushOutcome:
        """优先恢复 Projection，再处理至多一个新/到期 Run。"""
        current = now or datetime.now(UTC)
        pending = self._runs.next_projection_pending()
        if pending is not None:
            return await self._resume_projection(pending, current)
        self._prepare_one_batch(current)
        run = self._runs.claim_next(
            worker_id,
            now=current,
            lease_seconds=self._lease_seconds,
        )
        if run is None:
            return FlushOutcome("idle", None)
        try:
            messages = self._source_messages(run)
            await self._handler.write_markdown(run, messages)
        except Exception:
            return self._retry(run, worker_id, current)
        checkpoint = self._runs.mark_markdown_committed(
            run.id,
            worker_id,
            now=current,
        )
        return await self._resume_projection(checkpoint, current)

    def _prepare_one_batch(self, now: datetime) -> MemoryFlushRun | None:
        """把最早 Owner 的 pending buffers 幂等绑定到一个 queued Run。"""
        owners = self._buffers.pending_owner_ids()
        if not owners:
            return None
        owner_id = owners[0]
        buffers = self._buffers.list_pending(owner_id, limit=self._batch_size)
        if not buffers:
            return None
        run = self._runs.enqueue(
            owner_id=owner_id,
            first_message_id=min(item.first_message_id for item in buffers),
            last_message_id=max(item.last_message_id for item in buffers),
            extractor=self._extractor,
            prompt_hash=self._prompt_hash,
            now=now,
        )
        try:
            self._buffers.assign(owner_id, tuple(item.id for item in buffers), run.id)
        except MemoryBufferStateError:
            return run
        return run

    def _retry(
        self,
        run: MemoryFlushRun,
        worker_id: str,
        now: datetime,
    ) -> FlushOutcome:
        """把 Markdown 前的任意安全失败转成指数退避，而不影响用户 Turn。"""
        delay = min(600, 2 ** min(run.attempts, 9))
        try:
            retried = self._runs.mark_retry(
                run.id,
                worker_id,
                error_code="memory_flush_failed",
                next_attempt_at=now + timedelta(seconds=delay),
                now=now,
            )
        except MemoryStateError:
            return FlushOutcome("retry", run.id, "memory_checkpoint_changed")
        return FlushOutcome(retried.status, retried.id, "memory_flush_failed")

    async def _resume_projection(
        self,
        run: MemoryFlushRun,
        now: datetime,
    ) -> FlushOutcome:
        """只执行 disposable Projection，并原子结算 Run + buffers。"""
        try:
            await self._handler.project(run)
        except Exception:
            return FlushOutcome("projection_pending", run.id, "memory_projection_failed")
        completed = self._runs.complete_projection_and_buffers(run.id, now=now)
        return FlushOutcome(completed.status, completed.id)

    def _source_messages(
        self,
        run: MemoryFlushRun,
    ) -> tuple[FlushSourceMessage, ...]:
        """只读取 Run Owner 范围内的 user/assistant 消息并保持 ID 顺序。"""
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT messages.id, messages.session_id, sessions.channel,
                    messages.role, messages.content
                FROM messages
                JOIN sessions ON sessions.id = messages.session_id
                WHERE sessions.user_id = ? AND messages.id BETWEEN ? AND ?
                    AND messages.role IN ('user', 'assistant')
                ORDER BY messages.id
                """,
                (run.owner_id, run.first_message_id, run.last_message_id),
            ).fetchall()
        if not rows:
            raise MemoryStateError("memory flush source range is empty")
        return tuple(
            FlushSourceMessage(
                id=int(row["id"]),
                session_id=int(row["session_id"]),
                channel=str(row["channel"]),
                role=str(row["role"]),
                content=str(row["content"]),
            )
            for row in rows
        )
