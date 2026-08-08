"""共享 AgentRuntime 的最小装配测试。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.config import load_config
from miniclaw.paths import build_state_paths
from miniclaw.runtime import create_runtime


class AgentRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """验证 CLI/TUI 共用同一套 Owner、Service 和 Tool Registry。"""

    async def test_create_runtime_exposes_ten_enabled_tools_and_closes(self) -> None:
        """Runtime 应复用真实配置装配十个已实现 Tool，并拥有 Provider 生命周期。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            owner = initialize_state(paths).owner
            config = load_config(paths)

            runtime = create_runtime(config, paths, "test-key")
            try:
                self.assertEqual(runtime.owner_id, owner.id)
                self.assertEqual(runtime.model, "deepseek-v4-pro")
                self.assertEqual(runtime.workspace, paths.workspace)
                self.assertEqual(runtime.ui_language, "zh-CN")
                self.assertEqual(runtime.context_budget_tokens, 32_000)
                self.assertEqual(
                    [definition.name for definition in runtime.tool_definitions],
                    [
                        "edit_file",
                        "glob",
                        "grep",
                        "http_get",
                        "propose_memory",
                        "read_file",
                        "read_memory",
                        "run_command",
                        "system_info",
                        "write_file",
                    ],
                )
                self.assertIsNotNone(runtime.service)
            finally:
                await runtime.aclose()


if __name__ == "__main__":
    unittest.main()
