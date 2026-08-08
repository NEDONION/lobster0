"""Turn 非阻塞 capture 与 FlushCoordinator checkpoint 测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.agent.context import ContextBuilder
from miniclaw.agent.runner import AgentRunner
from miniclaw.agent.turn import TurnService
from miniclaw.bootstrap import initialize_state
from miniclaw.config import WorkspaceConfig
from miniclaw.memory.buffer import MemoryBufferRepository
from miniclaw.memory.flush import FlushCoordinator, MemoryCapture
from miniclaw.memory.repository import MemoryRunRepository
from miniclaw.paths import build_state_paths
from miniclaw.providers.base import ModelResponse
from miniclaw.storage.conversations import MessageRepository, SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from tests.fakes.fake_provider import FakeProvider

NOW = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


class RecordingFlushHandler:
    """记录 Markdown/Projection 阶段并可注入一次 Projection 失败。"""

    def __init__(self, *, fail_projection_once: bool = False) -> None:
        """保存故障开关与调用计数。"""
        self.fail_projection_once = fail_projection_once
        self.markdown_calls = 0
        self.projection_calls = 0

    async def write_markdown(self, run, messages) -> None:
        """模拟幂等 Markdown 提交。"""
        del run, messages
        self.markdown_calls += 1

    async def project(self, run) -> None:
        """模拟可恢复 Projection，并按配置首次失败。"""
        del run
        self.projection_calls += 1
        if self.fail_projection_once and self.projection_calls == 1:
            raise RuntimeError("synthetic projection failure")


class MemoryFlushTest(unittest.IsolatedAsyncioTestCase):
    """验证普通 Turn 不等待提取，checkpoint 后只恢复未完成阶段。"""

    def setUp(self) -> None:
        """创建真实 Turn、buffer/run Repository 和 MemoryCapture。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        self.buffers = MemoryBufferRepository(self.database)
        self.runs = MemoryRunRepository(self.database)
        self.messages = MessageRepository(self.database)
        self.sessions = SessionRepository(self.database)
        self.turns = TurnRepository(self.database)

    async def complete_turn(
        self,
        text: str = "普通消息",
        *,
        capture: MemoryCapture | None = None,
    ) -> None:
        """用真实 TurnService 完成一次本地 Owner Turn。"""
        provider = FakeProvider(
            (
                ModelResponse(
                    "answer",
                    (),
                    "reasoning",
                    "stop",
                    1,
                    1,
                    "req-memory-capture",
                ),
            )
        )
        service = TurnService(
            owner_id=self.owner.id,
            model="test-model",
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            context=ContextBuilder(self.paths),
            runner=AgentRunner(provider),
            state_home=self.paths.home,
            workspace=WorkspaceConfig(path=self.paths.workspace),
            memory_capture=capture or MemoryCapture(self.buffers),
        )
        result = await service.handle(self.owner.id, text, "memory-capture")
        self.assertEqual(result.content, "answer")

    async def test_completed_turn_returns_before_flush_handler_runs(self) -> None:
        """Turn 终态只写 durable receipt，不同步调用 extractor/Markdown。"""
        handler = RecordingFlushHandler()

        await self.complete_turn()

        self.assertEqual(handler.markdown_calls, 0)
        self.assertEqual(self.buffers.pending_count(self.owner.id), 1)

    async def test_flush_completes_assigned_buffers_and_checkpoints(self) -> None:
        """Coordinator 组批、claim、Markdown、Projection 后结算 buffer。"""
        await self.complete_turn()
        handler = RecordingFlushHandler()
        coordinator = FlushCoordinator(
            self.database,
            self.buffers,
            self.runs,
            handler,
            extractor="test-v1",
            prompt_hash="a" * 64,
        )

        outcome = await coordinator.run_once("worker-a", now=NOW)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(handler.markdown_calls, 1)
        self.assertEqual(handler.projection_calls, 1)
        self.assertEqual(self.buffers.pending_count(self.owner.id), 0)
        assert outcome.run_id is not None
        self.assertEqual(self.runs.get(outcome.run_id).status, "completed")

    async def test_projection_failure_resumes_without_rewriting_markdown(self) -> None:
        """markdown_committed 后失败只重跑 Projection，不产生第二次 Unit 写入。"""
        await self.complete_turn()
        handler = RecordingFlushHandler(fail_projection_once=True)
        coordinator = FlushCoordinator(
            self.database,
            self.buffers,
            self.runs,
            handler,
            extractor="test-v1",
            prompt_hash="b" * 64,
        )

        first = await coordinator.run_once("worker-a", now=NOW)
        second = await coordinator.run_once("worker-b", now=NOW)

        self.assertEqual(first.status, "projection_pending")
        self.assertEqual(second.status, "completed")
        self.assertEqual(handler.markdown_calls, 1)
        self.assertEqual(handler.projection_calls, 2)

    async def test_worker_wakes_after_five_turns_but_capture_stays_nonblocking(self) -> None:
        """前四条只落 durable buffer，第五条达到阈值才触发后台 wake。"""
        wakes: list[bool] = []
        capture = MemoryCapture(
            self.buffers,
            wake=lambda: wakes.append(True),
            wake_threshold=5,
        )

        for index in range(5):
            await self.complete_turn(f"普通消息 {index}", capture=capture)

        self.assertEqual(self.buffers.pending_count(self.owner.id), 5)
        self.assertEqual(wakes, [True])


if __name__ == "__main__":
    unittest.main()
