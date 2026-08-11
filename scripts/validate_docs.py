#!/usr/bin/env python3
"""校验当前发布文档的内部链接、Mermaid/HTML 结构和全仓门禁事实。"""

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_RELATIVE_DOCS = (
    Path("README.md"),
    Path("README_EN.md"),
    Path("docs/product/20260807_产品需求文档.md"),
    Path("docs/architecture/20260807_系统架构.md"),
    Path("docs/engineering/README.md"),
    Path("docs/engineering/phase-2/20260808_autopilot-permissions-and-approval-ui.md"),
    Path("docs/engineering/phase-5/20260808_telegram-discord-channels.md"),
    Path("docs/engineering/phase-5/20260808_feishu-live-e2e.md"),
    Path("docs/engineering/phase-5/20260808_feishu-gateway-runtime-and-macos-service.md"),
    Path("docs/engineering/phase-5/20260809_feishu-single-card-and-lark-cli.md"),
    Path("docs/engineering/phase-5/20260808_testing-and-live-acceptance.md"),
    Path("docs/engineering/phase-5/20260808_troubleshooting.md"),
    Path("docs/engineering/phase-5/20260808_completion-audit.md"),
    Path("docs/engineering/phase-5/20260809_memory-autopilot.md"),
    Path("docs/engineering/phase-6/20260809_autonomy-runtime.md"),
    Path("docs/engineering/phase-6/20260809_sandbox-and-checkpoint.md"),
    Path("docs/engineering/phase-6/20260810_macos-feishu-production-acceptance.md"),
    Path("docs/engineering/phase-6/browser-agent.md"),
    Path("docs/engineering/phase-7/20260810_controlled-evolution.md"),
    Path("docs/superpowers/specs/2026-08-09-phase-6-autonomy-sandbox-design.md"),
    Path("docs/superpowers/plans/2026-08-09-phase-6-autonomy-sandbox.md"),
    Path("docs/getting-started/20260807_本地运行指南.md"),
    Path("docs/getting-started/20260811_Linux服务器部署指南.md"),
    Path("docs/getting-started/20260811_实机部署踩坑实录.md"),
    Path("docs/evals/README.md"),
    Path("docs/evals/releases/v0.5.0.md"),
    Path("docs/evals/releases/v0.5.1.md"),
    Path("docs/evals/releases/v0.5.2.md"),
    Path("docs/evals/releases/v0.6.0.md"),
    Path("docs/evals/releases/v0.6.1.md"),
    Path("docs/evals/releases/v0.7.0.md"),
    Path("docs/evals/releases/v0.6.5.md"),
    Path("docs/engineering/operations/20260809_install-release-operations.md"),
    Path("docs/engineering/operations/20260812_aliyun-cli-and-ecs-setup-pitfalls.md"),
    Path("docs/evals/releases/v0.7.0-install.md"),
)
FACT_RELATIVE_DOCS = (
    Path("README.md"),
    Path("README_EN.md"),
    Path("docs/product/20260807_产品需求文档.md"),
    Path("docs/architecture/20260807_系统架构.md"),
    Path("docs/engineering/README.md"),
    Path("docs/getting-started/20260807_本地运行指南.md"),
    Path("docs/evals/README.md"),
)
REQUIRED_FACTS = (
    "IMPLEMENTATION PASS",
    "1005",
    "41/41",
    "14/14",
    "39/39",
    "33/33",
    "660/660",
    "15/15",
    "18/18",
    "360/360",
    "CONTROLLED LIVE SMOKE PENDING",
    "TARGETED CALLBACK LIVE VERIFIED",
    "15-CASE LIVE PENDING",
    "PRODUCTION SOAK PENDING",
)
INSTALL_URL = "https://github.com/NEDONION/lobster0/releases/latest/download/install.sh"
INSTALL_CURL = "curl -fsSL --proto '=https' --tlsv1.2"
CANDIDATE_LABEL = "RELEASE CANDIDATE / PUBLIC GATES PENDING"
_README_INSTALL_FACTS = (
    INSTALL_URL,
    INSTALL_CURL,
    CANDIDATE_LABEL,
    "lobster0-agent",
    "Ubuntu",
    "22.04",
    "24.04",
    "Debian",
    "Rocky",
    "Alma",
    "macOS",
    "x86_64",
    "arm64",
    "systemd user",
    "LaunchAgent",
    "unsupported_platform",
    "Windows",
    "WSL",
    "Alpine",
    "22.22.3",
    "24.15.0",
    "24.18.0",
    "lobster0 service install",
    "lobster0 service logs",
    "lobster0 service uninstall",
    "lobster0 uninstall",
    "--purge-data",
    "--yes-i-understand-data-loss",
    "--no-onboard",
    "--no-install-service",
    "--dry-run",
    "--json",
    "update_requires_bootstrap",
    "uv sync",
    "pnpm --dir tui",
)
# 每个入口文档必须自己说清一行安装、支持矩阵、Node 策略、服务/卸载语义与
# 候选状态；缺任何一条都视为文档回归。
INSTALL_REQUIRED_FACTS: tuple[tuple[Path, tuple[str, ...]], ...] = (
    (Path("README.md"), _README_INSTALL_FACTS),
    (Path("README_EN.md"), _README_INSTALL_FACTS),
    (
        Path("docs/getting-started/20260807_本地运行指南.md"),
        (
            INSTALL_URL,
            INSTALL_CURL,
            CANDIDATE_LABEL,
            "lobster0-agent",
            "22.22.3",
            "24.18.0",
            "update_requires_bootstrap",
            "lobster0 service status",
            "--purge-data",
            "--dry-run",
            "uv sync",
        ),
    ),
    (
        Path("docs/engineering/operations/20260809_install-release-operations.md"),
        (
            INSTALL_URL,
            CANDIDATE_LABEL,
            "release-evidence.json",
            "Trusted Publisher",
            "ghcr.io/nedonion/lobster0",
            "LOBSTER0_REQUIRE_NATIVE_BUNDLES=1",
            "rollback_conflict",
            "update_requires_bootstrap",
            "draft",
            "yank",
            "0.7.1",
        ),
    ),
    (
        Path("docs/evals/releases/v0.7.0-install.md"),
        (
            INSTALL_URL,
            CANDIDATE_LABEL,
            "release-evidence.json",
            "actions/runners",
            "tier1-install",
            "update_requires_bootstrap",
            "LOBSTER0_REQUIRE_NATIVE_BUNDLES",
            "lobster0-agent",
        ),
    ),
    (
        Path("docs/product/20260807_产品需求文档.md"),
        (INSTALL_URL, CANDIDATE_LABEL, "lobster0-agent"),
    ),
    (
        Path("docs/architecture/20260807_系统架构.md"),
        (
            "install.sh",
            "lobster0-installer.pyz",
            "release-manifest.json",
            "runtimes/<version>",
            "lobster0-agent",
        ),
    ),
    (
        Path("docs/progress/index.html"),
        (INSTALL_URL, CANDIDATE_LABEL, "lobster0-agent"),
    ),
)
# 一行安装范围内的文档不得复活旧命名、旧 Node 门槛或全局 pnpm 前置条件。
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("legacy_distribution_name", re.compile(r"name\s*=\s*\"?lobster0\"?(?![-\w])")),
    ("legacy_node_floor", re.compile(r"(?:>=|≥)\s*`?22\.19|22\.19(?:\.\d+)?\s*\+")),
    (
        "global_pnpm_requirement",
        re.compile(r"(?:全局(?:安装)?\s*pnpm|globally\s+install(?:ed)?\s+pnpm|global\s+pnpm)"),
    ),
)
# 只要一行同时谈到公共发布门禁主体和 PASS，就必须在同一行说明它仍是 PENDING。
PENDING_SUBJECT = re.compile(
    r"(?i)(?:tier\s*1|tier1-install|self-hosted|PyPI|GHCR|ghcr\.io|attestation|"
    r"release-evidence|Trusted Publisher|install\.sh)"
)
PASS_TOKEN = re.compile(r"(?<![A-Za-z])PASS(?![A-Za-z])")
INSTALL_SCOPE_RELATIVE_DOCS = frozenset(relative for relative, _ in INSTALL_REQUIRED_FACTS)
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
FENCE = re.compile(r"^\s*```([^`]*)$")
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


class _StrictHtmlParser(HTMLParser):
    """用栈保证项目静态 HTML 的开始/结束标签成对。"""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag, attrs

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            raise ValueError("unbalanced HTML tag")
        self.stack.pop()

    def close(self) -> None:
        super().close()
        if self.stack:
            raise ValueError("unclosed HTML tag")

    def error(self, message: str) -> None:  # pragma: no cover - compatibility hook
        raise ValueError(message)


def main(argv: list[str] | None = None) -> int:
    """运行全部校验并只输出相对路径错误。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--html", type=Path, action="append", default=[])
    parser.add_argument("--forbid-draft-markers", action="store_true")
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve(strict=False)
    current_docs = tuple(root / relative for relative in CURRENT_RELATIVE_DOCS)
    fact_docs = tuple(root / relative for relative in FACT_RELATIVE_DOCS)
    failures: list[str] = []
    for path in current_docs:
        if not path.is_file():
            failures.append(f"missing:{_display(root, path)}")
            continue
        content = path.read_text(encoding="utf-8")
        failures.extend(_fence_failures(root, path, content))
        failures.extend(_broken_links(root, path, content))
    for path in fact_docs:
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        for fact in REQUIRED_FACTS:
            if fact not in content:
                failures.append(f"fact:{_display(root, path)}:{fact}")
        if arguments.forbid_draft_markers:
            for marker in ("DESIGN READY", "IMPLEMENTATION PENDING"):
                if marker in content:
                    failures.append(f"draft:{_display(root, path)}:{marker}")
    failures.extend(_install_documentation_failures(root))
    html_paths = [root / "docs/progress/index.html", *arguments.html]
    for html in html_paths:
        failure = _html_failure(html)
        if failure is not None:
            failures.append(f"html:{_display(root, html)}:{failure}")
            continue
        content = html.read_text(encoding="utf-8")
        for fact in REQUIRED_FACTS:
            if fact not in content:
                failures.append(f"html_fact:{_display(root, html)}:{fact}")
    if failures:
        for failure in sorted(set(failures)):
            print(f"Documentation validation: FAIL {failure}", file=sys.stderr)
        return 1
    print("Documentation validation: PASS")
    return 0


def _install_documentation_failures(root: Path) -> list[str]:
    """校验一行安装文档契约：必需事实、被禁表述与 PENDING 门禁的诚实标注。"""
    failures: list[str] = []
    for relative, facts in INSTALL_REQUIRED_FACTS:
        path = root / relative
        if not path.is_file():
            failures.append(f"missing:{relative.as_posix()}")
            continue
        content = path.read_text(encoding="utf-8")
        for fact in facts:
            if fact not in content:
                failures.append(f"install_fact:{relative.as_posix()}:{fact}")
        for label, pattern in FORBIDDEN_PATTERNS:
            if pattern.search(content) is not None:
                failures.append(f"forbidden:{relative.as_posix()}:{label}")
        failures.extend(_pending_claim_failures(relative, content))
    return failures


def _pending_claim_failures(relative: Path, content: str) -> list[str]:
    """拒绝把尚未执行的公共发布门禁写成已经通过。"""
    failures: list[str] = []
    for line_number, line in enumerate(content.splitlines(), 1):
        if PENDING_SUBJECT.search(line) is None or PASS_TOKEN.search(line) is None:
            continue
        if "PENDING" in line:
            continue
        failures.append(f"pending_claimed_pass:{relative.as_posix()}:{line_number}")
    return failures


def _broken_links(root: Path, path: Path, content: str) -> list[str]:
    """只验证本地 Markdown 链接；URL、anchor 和模板值不参与。"""
    failures: list[str] = []
    for target in LINK.findall(content):
        clean = target.strip().split("#", 1)[0]
        if not clean or clean.startswith(("http://", "https://", "mailto:")):
            continue
        if clean.startswith("<") and clean.endswith(">"):
            clean = clean[1:-1]
        resolved = (path.parent / clean).resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            failures.append(f"link_escape:{_display(root, path)}")
            continue
        if not resolved.exists():
            failures.append(
                f"broken_link:{_display(root, path)}:{clean}"
            )
    return failures


def _fence_failures(root: Path, path: Path, content: str) -> list[str]:
    """逐行验证 fenced block；Mermaid 与普通 code fence 都必须闭合。"""
    opened: tuple[int, str] | None = None
    for line_number, line in enumerate(content.splitlines(), 1):
        match = FENCE.fullmatch(line)
        if match is None:
            continue
        if opened is None:
            opened = (line_number, match.group(1).strip())
        elif not match.group(1).strip():
            opened = None
    if opened is None:
        return []
    kind = "mermaid" if opened[1] == "mermaid" else "fence"
    return [f"{kind}:{_display(root, path)}:{opened[0]}"]


def _html_failure(path: Path) -> str | None:
    """读取并严格关闭一个 HTML 文件。"""
    try:
        parser = _StrictHtmlParser()
        parser.feed(path.read_text(encoding="utf-8"))
        parser.close()
    except (OSError, UnicodeError, ValueError) as error:
        return type(error).__name__
    return None


def _display(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root).as_posix()
    except ValueError:
        return path.name


if __name__ == "__main__":
    raise SystemExit(main())
