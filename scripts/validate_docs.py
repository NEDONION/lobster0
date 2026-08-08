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
    Path("docs/product/20260807_产品需求文档.md"),
    Path("docs/architecture/20260807_系统架构.md"),
    Path("docs/engineering/README.md"),
    Path("docs/engineering/phase-2/autopilot-permissions-and-approval-ui.md"),
    Path("docs/engineering/phase-5/telegram-discord-channels.md"),
    Path("docs/engineering/phase-5/feishu-live-e2e.md"),
    Path("docs/engineering/phase-5/feishu-gateway-runtime-and-macos-service.md"),
    Path("docs/engineering/phase-5/feishu-single-card-and-lark-cli.md"),
    Path("docs/engineering/phase-5/testing-and-live-acceptance.md"),
    Path("docs/engineering/phase-5/troubleshooting.md"),
    Path("docs/engineering/phase-5/completion-audit.md"),
    Path("docs/getting-started/20260807_本地运行指南.md"),
    Path("docs/evals/README.md"),
    Path("docs/evals/releases/v0.5.0.md"),
    Path("docs/evals/releases/v0.5.1.md"),
    Path("docs/evals/releases/v0.5.2.md"),
    Path("docs/evals/releases/v0.5.3.md"),
)
FACT_RELATIVE_DOCS = (
    Path("README.md"),
    Path("docs/architecture/20260807_系统架构.md"),
    Path("docs/engineering/README.md"),
    Path("docs/engineering/phase-2/autopilot-permissions-and-approval-ui.md"),
    Path("docs/engineering/phase-5/telegram-discord-channels.md"),
    Path("docs/getting-started/20260807_本地运行指南.md"),
    Path("docs/engineering/phase-5/testing-and-live-acceptance.md"),
    Path("docs/engineering/phase-5/feishu-live-e2e.md"),
    Path("docs/engineering/phase-5/feishu-gateway-runtime-and-macos-service.md"),
)
REQUIRED_FACTS = (
    "IMPLEMENTATION PASS",
    "556",
    "30",
    "29/29",
    "32/32",
    "640/640",
    "TARGETED CALLBACK LIVE VERIFIED",
    "15-CASE LIVE PENDING",
)
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
