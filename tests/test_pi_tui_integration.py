"""真实 Node 客户端与 Python Core Bridge 的跨进程冒烟测试。"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lobster0.bootstrap import initialize_state
from lobster0.paths import build_state_paths
from lobster0.tui_launcher import is_supported_node_version


class PiTuiIntegrationTest(unittest.TestCase):
    """验证版本化 NDJSON 契约可跨语言完成握手和关闭。"""

    def test_node_client_handshakes_with_real_python_bridge(self) -> None:
        """受支持 Node LTS 和已构建 TUI 可用时必须通过真实子进程往返。"""
        project = Path(__file__).resolve().parent.parent
        node = os.environ.get("LOBSTER0_NODE") or shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        version = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
        parts = version.removeprefix("v").split(".")
        if len(parts) != 3:
            self.skipTest("pi-tui integration requires a supported Node.js LTS")
        try:
            parsed = (int(parts[0]), int(parts[1]), int(parts[2]))
        except (IndexError, ValueError):
            self.skipTest("pi-tui integration requires a supported Node.js LTS")
        if not is_supported_node_version(parsed):
            self.skipTest("pi-tui integration requires Node.js 22.22.3–<23 or 24.15.0–<25")
        if not (project / "tui" / "dist" / "bridge-client.js").is_file():
            self.skipTest("pi-tui must be built before the cross-language smoke test")

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            initialize_state(build_state_paths(home))
            environment = {
                **os.environ,
                "LOBSTER0_NODE": node,
                "LOBSTER0_PYTHON": os.sys.executable,
                "LOBSTER0_BRIDGE_SMOKE_HOME": str(home),
                "LOBSTER0_MODEL_API_KEY": "offline-smoke-key",
                "PYTHONPATH": str(project / "src"),
            }
            completed = subprocess.run(
                [node, "--test", "tui/test/python-bridge.test.ts"],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("real TypeScript client handshakes", completed.stdout)


if __name__ == "__main__":
    unittest.main()
