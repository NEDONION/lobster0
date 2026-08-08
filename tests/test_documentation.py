"""Phase 5 当前事实、文档链接与进度页面的一致性门禁。"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.validate_docs import _broken_links, _fence_failures, _html_failure

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Phase5DocumentationTest(unittest.TestCase):
    """防止代码完成后文档仍停留在设计阶段或写成伪 live PASS。"""

    def test_primary_status_documents_share_the_same_verified_facts(self) -> None:
        """用户入口必须统一显示 implementation PASS 与当前真实门禁数字。"""
        paths = (
            "README.md",
            "docs/architecture/20260807_系统架构.md",
            "docs/engineering/README.md",
            "docs/engineering/phase-5/telegram-discord-channels.md",
            "docs/getting-started/20260807_本地运行指南.md",
            "docs/engineering/phase-2/autopilot-permissions-and-approval-ui.md",
            "docs/engineering/phase-5/testing-and-live-acceptance.md",
            "docs/engineering/phase-5/feishu-live-e2e.md",
            "docs/engineering/phase-5/feishu-gateway-runtime-and-macos-service.md",
        )
        for relative in paths:
            with self.subTest(path=relative):
                content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("IMPLEMENTATION PASS", content)
                self.assertIn("519", content)
                self.assertIn("30", content)
                self.assertIn("32/32", content)
                self.assertIn("640/640", content)
                self.assertIn("OWNER-DM DELIVERY VERIFIED", content)
                self.assertIn("15-CASE LIVE PENDING", content)

    def test_phase5_operational_documents_are_present_and_linked(self) -> None:
        """实现、测试、排障和完成审计必须各有独立入口。"""
        engineering = (PROJECT_ROOT / "docs/engineering/README.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "telegram-discord-channels.md",
            "feishu-live-e2e.md",
            "feishu-gateway-runtime-and-macos-service.md",
            "testing-and-live-acceptance.md",
            "troubleshooting.md",
            "completion-audit.md",
        ):
            self.assertTrue((PROJECT_ROOT / "docs/engineering/phase-5" / name).is_file())
            self.assertIn(name, engineering)

    def test_progress_page_exposes_gate_and_live_truth(self) -> None:
        """可视化进度页必须能一眼区分代码完成与真实平台待验收。"""
        content = (PROJECT_ROOT / "docs/progress/index.html").read_text(encoding="utf-8")
        for needle in (
            "Phase 5 implementation pass",
            "519 Python",
            "30 TypeScript",
            "IMPLEMENTATION PASS",
            "32/32",
            "640/640",
            "OWNER-DM DELIVERY VERIFIED",
            "15-CASE LIVE PENDING",
            "Telegram live pending",
            "Discord live pending",
        ):
            self.assertIn(needle, content)

    def test_agents_file_contains_mixed_commit_and_phase5_gate(self) -> None:
        """后续 Agent 必须知道中英混合提交规范与发布命令。"""
        content = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("大约一半中文、一半英文", content)
        self.assertIn("--suite channel --repeat 20", content)
        self.assertIn("scripts/validate_docs.py", content)

    def test_documentation_validator_passes_repository(self) -> None:
        """内部链接、Mermaid fence、HTML 与事实扫描由一个可复现脚本校验。"""
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "validate_docs.py")],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Documentation validation: PASS", result.stdout)

    def test_validator_rejects_broken_link_unclosed_mermaid_and_html(self) -> None:
        """三个最常见的机械文档错误都必须被 focused helper 拒绝。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markdown = root / "broken.md"
            markdown.write_text(
                "[missing](missing.md)\n\n```mermaid\nflowchart LR\nA --> B\n",
                encoding="utf-8",
            )
            html = root / "broken.html"
            html.write_text("<html><body><main></body></html>", encoding="utf-8")
            content = markdown.read_text(encoding="utf-8")

            self.assertTrue(_broken_links(root, markdown, content))
            self.assertTrue(_fence_failures(root, markdown, content))
            self.assertIsNotNone(_html_failure(html))


if __name__ == "__main__":
    unittest.main()
