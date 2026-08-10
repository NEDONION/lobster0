"""Memory Flush 跨 Coordinator 重启的 checkpoint 恢复测试。"""

from tests.test_memory_flush import NOW, MemoryFlushTest, RecordingFlushHandler


class MemoryRecoveryTest(MemoryFlushTest):
    """复用真实 fixture 验证新 Coordinator 恢复 projection_pending。"""

    async def test_new_coordinator_recovers_projection_pending(self) -> None:
        """进程重建后不重跑 Markdown，只继续 Projection 并结算 buffer。"""
        await self.complete_turn()
        first_handler = RecordingFlushHandler(fail_projection_once=True)
        from lobster0.memory.flush import FlushCoordinator

        first = FlushCoordinator(
            self.database,
            self.buffers,
            self.runs,
            first_handler,
            extractor="test-v1",
            prompt_hash="c" * 64,
        )
        failed = await first.run_once("worker-a", now=NOW)
        replacement = RecordingFlushHandler()
        restarted = FlushCoordinator(
            self.database,
            self.buffers,
            self.runs,
            replacement,
            extractor="test-v1",
            prompt_hash="c" * 64,
        )

        recovered = await restarted.run_once("worker-b", now=NOW)

        self.assertEqual(failed.status, "projection_pending")
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(first_handler.markdown_calls, 1)
        self.assertEqual(replacement.markdown_calls, 0)
        self.assertEqual(replacement.projection_calls, 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
