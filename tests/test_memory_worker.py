"""Memory background worker 的有界启动、唤醒与关闭测试。"""

import asyncio
import unittest

from miniclaw.memory.worker import MemoryWorker


class FakeCoordinator:
    """记录 worker run_once 调用并通知测试协程。"""

    def __init__(self) -> None:
        """创建调用计数和首轮事件。"""
        self.calls = 0
        self.called = asyncio.Event()

    async def run_once(self, worker_id: str, *, now=None):
        """记录一次有界轮询并返回 idle。"""
        del worker_id, now
        self.calls += 1
        self.called.set()
        return None


class MemoryWorkerTest(unittest.IsolatedAsyncioTestCase):
    """验证 worker 不随 Turn 同步执行且 shutdown 有界。"""

    async def test_notify_starts_one_worker_and_stop_is_idempotent(self) -> None:
        """多次 notify 只保留一个 task，stop 可重复且不遗留任务。"""
        coordinator = FakeCoordinator()
        worker = MemoryWorker(coordinator, worker_id="memory-worker", interval=60)

        worker.notify()
        worker.notify()
        await asyncio.wait_for(coordinator.called.wait(), timeout=1)
        await worker.stop(timeout=1)
        await worker.stop(timeout=1)

        self.assertGreaterEqual(coordinator.calls, 1)
        self.assertFalse(worker.running)


if __name__ == "__main__":
    unittest.main()
