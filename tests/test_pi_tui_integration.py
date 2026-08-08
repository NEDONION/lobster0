"""真实 Node 客户端与 Python Core Bridge 的跨进程冒烟测试。"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths


class PiTuiIntegrationTest(unittest.TestCase):
    """验证版本化 NDJSON 契约可跨语言完成握手和关闭。"""

    def test_node_client_handshakes_with_real_python_bridge(self) -> None:
        """Node 22.19+ 和已构建 TUI 可用时必须通过真实子进程往返。"""
        project = Path(__file__).resolve().parent.parent
        node = os.environ.get("MINICLAW_NODE") or shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        version = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
        major_minor = tuple(int(part) for part in version.removeprefix("v").split(".")[:2])
        if major_minor < (22, 19):
            self.skipTest("pi-tui integration requires Node.js >= 22.19.0")
        if not (project / "tui" / "dist" / "bridge-client.js").is_file():
            self.skipTest("pi-tui must be built before the cross-language smoke test")

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            initialize_state(build_state_paths(home))
            environment = {
                **os.environ,
                "MINICLAW_NODE": node,
                "MINICLAW_PYTHON": os.sys.executable,
                "MINICLAW_BRIDGE_SMOKE_HOME": str(home),
                "MINICLAW_MODEL_API_KEY": "offline-smoke-key",
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
