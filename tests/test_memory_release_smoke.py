"""Memory v0.6.0 跨入口与重启遗忘的脱敏发布 smoke。"""

import tempfile
import unittest
from pathlib import Path

from lobster0.evals.memory import run_memory_fixture


class MemoryReleaseSmokeTest(unittest.IsolatedAsyncioTestCase):
    """验证发布 smoke 只产生固定 evidence key，不返回私人正文。"""

    async def test_cross_channel_restart_forget_smoke_is_sanitized(self) -> None:
        """四个私人入口跨重启召回，遗忘重建后同时停止召回。"""
        with tempfile.TemporaryDirectory(prefix="lobster0-memory-smoke-") as directory:
            root = Path(directory)
            disclosure = await run_memory_fixture(
                "cross_channel_disclosure",
                root / "disclosure",
            )
            restart = await run_memory_fixture(
                "explicit_restart_forget",
                root / "restart",
            )

        self.assertEqual(
            disclosure.evidence,
            ("owner_space_shared", "group_denied", "non_owner_denied"),
        )
        self.assertEqual(
            restart.evidence,
            ("explicit_persisted", "restart_recalled", "forget_archived", "rebuild_absent"),
        )
        serialized = repr((disclosure.evidence, restart.evidence))
        self.assertNotIn("用户偏好", serialized)
        self.assertNotIn("memory.md", serialized)


if __name__ == "__main__":
    unittest.main()
