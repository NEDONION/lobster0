"""Phase 6.5 当前事实、文档链接与进度页面的一致性门禁。"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.validate_docs import _broken_links, _fence_failures, _html_failure

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Phase6DocumentationTest(unittest.TestCase):
    """防止 Phase 6.5 完成后文档仍停留在规划阶段或写成伪 Live PASS。"""

    def test_primary_status_documents_share_the_same_verified_facts(self) -> None:
        """用户入口必须统一显示 implementation PASS 与当前真实门禁数字。"""
        paths = (
            "README.md",
            "README_EN.md",
            "docs/product/20260807_产品需求文档.md",
            "docs/architecture/20260807_系统架构.md",
            "docs/engineering/README.md",
            "docs/getting-started/20260807_本地运行指南.md",
            "docs/evals/README.md",
        )
        for relative in paths:
            with self.subTest(path=relative):
                content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("IMPLEMENTATION PASS", content)
                self.assertIn("925", content)
                self.assertIn("36/36", content)
                self.assertIn("14/14", content)
                self.assertIn("39/39", content)
                self.assertIn("33/33", content)
                self.assertIn("660/660", content)
                self.assertIn("15/15", content)
                self.assertIn("18/18", content)
                self.assertIn("360/360", content)
                self.assertIn("CONTROLLED LIVE SMOKE PENDING", content)
                self.assertIn("TARGETED CALLBACK LIVE VERIFIED", content)
                self.assertIn("15-CASE LIVE PENDING", content)

    def test_operational_documents_are_present_and_linked(self) -> None:
        """Phase 5 运维和 Phase 6 自治安全文档必须各有独立入口。"""
        engineering = (PROJECT_ROOT / "docs/engineering/README.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "20260808_telegram-discord-channels.md",
            "20260808_feishu-live-e2e.md",
            "20260808_feishu-gateway-runtime-and-macos-service.md",
            "20260809_feishu-single-card-and-lark-cli.md",
            "20260808_testing-and-live-acceptance.md",
            "20260808_troubleshooting.md",
            "20260808_completion-audit.md",
        ):
            self.assertTrue((PROJECT_ROOT / "docs/engineering/phase-5" / name).is_file())
            self.assertIn(name, engineering)
        for name in (
            "20260809_autonomy-runtime.md",
            "20260809_sandbox-and-checkpoint.md",
            "browser-agent.md",
        ):
            self.assertTrue((PROJECT_ROOT / "docs/engineering/phase-6" / name).is_file())
            self.assertIn(name, engineering)
        self.assertIn("releases/v0.7.0.md", engineering)
        self.assertIn("releases/v0.6.5.md", engineering)

    def test_memory_autopilot_status_and_release_evidence_are_consistent(self) -> None:
        """Memory A～E 用户入口必须统一为已实现，并引用十条 versioned case。"""
        paths = (
            "README.md",
            "README_EN.md",
            "docs/product/20260807_产品需求文档.md",
            "docs/architecture/20260807_系统架构.md",
            "docs/engineering/README.md",
            "docs/engineering/phase-5/20260809_memory-autopilot.md",
            "docs/evals/releases/v0.6.0.md",
            "docs/progress/index.html",
        )
        for relative in paths:
            with self.subTest(path=relative):
                content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("Memory Autopilot", content)
                self.assertNotIn("Memory Autopilot（规划）", content)
                self.assertNotIn("Memory Autopilot (planned)", content)
        release = (PROJECT_ROOT / "docs/evals/releases/v0.6.0.md").read_text(
            encoding="utf-8"
        )
        for fact in ("666/666", "35/35", "39/39", "MEM-AUTO-001", "MEM-AUTO-010"):
            self.assertIn(fact, release)

    def test_openclaw_hermes_gap_and_phase_plans_are_linked_as_planned(self) -> None:
        """后续能力路线必须完整可达，同时明确不能冒充当前实现。"""
        gap = PROJECT_ROOT / (
            "docs/architecture/20260808_OpenClaw-Hermes能力Gap与演进路线.md"
        )
        engineering = PROJECT_ROOT / (
            "docs/engineering/20260808_openclaw-hermes-alignment-engineering-roadmap.md"
        )
        plans = (
            "2026-08-09-memory-autopilot.md",
            "2026-08-08-phase-5-2-production-hardening.md",
            "2026-08-08-phase-6-autonomy-runtime-and-sandbox.md",
            "2026-08-08-phase-6-5-browser-agent.md",
            "2026-08-08-phase-7-controlled-evolution-and-memory-v2.md",
            "2026-08-08-phase-8-skills-mcp-provider-resilience.md",
            "2026-08-08-phase-9-subagents-and-multimodal.md",
        )

        self.assertTrue(gap.is_file())
        self.assertTrue(engineering.is_file())
        engineering_text = engineering.read_text(encoding="utf-8")
        self.assertIn("APPROVED ROADMAP / NOT IMPLEMENTED", engineering_text)
        for name in plans:
            with self.subTest(plan=name):
                path = PROJECT_ROOT / "docs/superpowers/plans" / name
                self.assertTrue(path.is_file())
                content = path.read_text(encoding="utf-8")
                self.assertIn("Implementation Plan", content)
                self.assertIn("## Global Constraints", content)
                self.assertIn("- [ ]", content)

        for relative in (
            "README.md",
            "README_EN.md",
            "docs/README.md",
            "docs/engineering/README.md",
        ):
            content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("20260808_OpenClaw-Hermes能力Gap与演进路线.md", content)
            self.assertIn("20260808_openclaw-hermes-alignment-engineering-roadmap.md", content)

        for relative in ("README.md", "README_EN.md", "docs/README.md"):
            content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("2026-08-09-memory-autopilot.md", content)

    def test_progress_page_exposes_gate_and_live_truth(self) -> None:
        """可视化进度页必须能一眼区分代码完成与真实平台待验收。"""
        content = (PROJECT_ROOT / "docs/progress/index.html").read_text(encoding="utf-8")
        for needle in (
            "Phase 6.5 implementation pass",
            "925 Python",
            "36/36 TUI",
            "14/14 Worker",
            "39/39",
            "IMPLEMENTATION PASS",
            "33/33",
            "660/660",
            "15/15 Automation",
            "18/18",
            "360/360",
            "CONTROLLED LIVE SMOKE PENDING",
            "TARGETED CALLBACK LIVE VERIFIED",
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
        self.assertIn("--suite automation --repeat 20", content)
        self.assertIn("--suite browser --repeat 20", content)
        self.assertIn("scripts/validate_docs.py", content)

    def test_phase7_landing_is_detailed_and_explicitly_not_implemented(self) -> None:
        """Phase 7 入口必须可达、可施工，并且不能冒充当前功能。"""
        relative = "phase-7/20260810_controlled-evolution.md"
        path = PROJECT_ROOT / "docs/engineering" / relative
        self.assertTrue(path.is_file())
        content = path.read_text(encoding="utf-8")
        self.assertIn("ENGINEERING PLAN / NOT IMPLEMENTED", content)
        self.assertGreaterEqual(content.count("```mermaid"), 5)
        for heading in (
            "SQLite v7 设计",
            "Proposal 状态机",
            "Eval Gate",
            "Atomic Apply、Crash Recovery 与 Rollback",
            "分 Task 实施顺序",
            "Definition of Done",
        ):
            self.assertIn(heading, content)
        engineering = (PROJECT_ROOT / "docs/engineering/README.md").read_text(
            encoding="utf-8"
        )
        progress = (PROJECT_ROOT / "docs/progress/index.html").read_text(encoding="utf-8")
        self.assertIn(relative, engineering)
        self.assertIn("../engineering/phase-7/20260810_controlled-evolution.md", progress)
        self.assertIn("ENGINEERING PLAN · NOT IMPLEMENTED", progress)

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
