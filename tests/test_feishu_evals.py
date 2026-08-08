"""Phase 4 十二条 Feishu Channel 场景门禁。"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from miniclaw.evals.cases import load_cases
from miniclaw.evals.channel import run_channel_suite

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FeishuChannelEvalTest(unittest.IsolatedAsyncioTestCase):
    """保证 R4 数据集严格、无网络且全部通过真实有限纵切。"""

    async def test_repository_has_exact_required_channel_matrix(self) -> None:
        """十二个稳定 ID、fixture 与 live 标记必须完整。"""
        cases = tuple(
            case
            for case in load_cases(PROJECT_ROOT / "evals" / "scenarios")
            if case.id.startswith("FEISHU-")
            and case.status == "active"
            and "channel" in case.layers
        )

        self.assertEqual(
            {case.id for case in cases},
            {
                "FEISHU-DM-001",
                "FEISHU-GROUP-001",
                "FEISHU-GROUP-002",
                "FEISHU-DEDUPE-001",
                "FEISHU-TOOL-001",
                "FEISHU-APPROVAL-001",
                "FEISHU-APPROVAL-002",
                "FEISHU-RESTART-001",
                "FEISHU-RESTART-002",
                "FEISHU-DELIVERY-001",
                "FEISHU-CARD-001",
                "FEISHU-RECONNECT-001",
            },
        )
        self.assertEqual(len(cases), 12)
        self.assertTrue(all("live" in case.layers for case in cases))
        self.assertTrue(all(case.channel_fixture for case in cases))
        self.assertTrue(all(case.expected.channel_evidence for case in cases))

    async def test_all_channel_cases_pass_deterministic_gate(self) -> None:
        """每个版本都必须得到 12/12，且失败只用稳定短码。"""
        cases = tuple(
            case
            for case in load_cases(PROJECT_ROOT / "evals" / "scenarios")
            if case.id.startswith("FEISHU-")
            and case.status == "active"
            and "channel" in case.layers
        )

        suite = await run_channel_suite(cases)

        self.assertEqual(suite.total, 12)
        self.assertEqual(suite.passed, 12, suite.cases)
        self.assertEqual(suite.failed, 0)
        self.assertTrue(all(result.failures == () for result in suite.cases))

    async def test_live_harness_requires_explicit_confirmation(self) -> None:
        """未传 --confirm-live 时脚本必须在任何诊断/网络/写文件前退出 2。"""
        script = PROJECT_ROOT / "scripts" / "feishu_live_smoke.py"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(script), "--home", directory],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--confirm-live is required", result.stderr)
        self.assertNotIn("app_secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
