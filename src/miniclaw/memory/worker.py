"""有界、可唤醒且不阻塞 Turn 的 Memory background worker。"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol

_LOGGER = logging.getLogger(__name__)


class Coordinator(Protocol):
    """收窄 MemoryWorker 对 FlushCoordinator 的单轮调用。"""

    async def run_once(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> object:
        """执行至多一个可恢复 Flush Run。"""
        ...


class MemoryWorker:
    """在单个 asyncio Task 中周期运行 FlushCoordinator。"""

    def __init__(
        self,
        coordinator: Coordinator,
        *,
        worker_id: str,
        interval: float = 1.0,
    ) -> None:
        """绑定 Coordinator、稳定 Worker ID 与正数轮询间隔。"""
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 120:
            raise ValueError("memory worker_id is invalid")
        if type(interval) not in {int, float} or interval <= 0:
            raise ValueError("memory worker interval must be positive")
        self._coordinator = coordinator
        self._worker_id = worker_id
        self._interval = float(interval)
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """返回 background task 是否仍在运行。"""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """幂等启动 worker，并立即唤醒一次以恢复遗留 checkpoint。"""
        if self.running:
            self._wake.set()
            return
        self._stopping.clear()
        self._wake.set()
        self._task = asyncio.create_task(self._run(), name="miniclaw-memory-worker")

    def notify(self) -> None:
        """从已完成 Turn 非阻塞唤醒 worker，尚未启动时就地创建 task。"""
        if not self.running:
            self._stopping.clear()
            self._task = asyncio.get_running_loop().create_task(
                self._run(),
                name="miniclaw-memory-worker",
            )
        self._wake.set()

    async def stop(self, *, timeout: float = 5.0) -> None:
        """幂等停止 worker，并在 timeout 后取消而不无限阻塞关机。"""
        if type(timeout) not in {int, float} or timeout <= 0:
            raise ValueError("memory worker stop timeout must be positive")
        task = self._task
        if task is None:
            return
        self._stopping.set()
        self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=float(timeout))
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._task = None

    async def _run(self) -> None:
        """每次唤醒执行一轮，异常被局部化后等待下一次重试。"""
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                await self._coordinator.run_once(
                    self._worker_id,
                    now=datetime.now(UTC),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.warning("memory_worker_cycle_failed", exc_info=False)
            if self._stopping.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                continue
