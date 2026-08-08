"""在唯一 CLI 入口选择默认 pi-tui 或迁移期 Textual fallback。"""

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from miniclaw.paths import StatePaths
from miniclaw.tui import run_tui

MINIMUM_NODE_VERSION = (22, 19, 0)
_NODE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class TuiLaunchError(RuntimeError):
    """表示默认 TUI 无法按 Owner 明确选择启动。"""


@dataclass(frozen=True, slots=True)
class PiTuiInspection:
    """保存 Node、构建入口和一条可操作问题。"""

    node: Path | None
    node_version: tuple[int, int, int] | None
    entry: Path | None
    problem: str | None

    @property
    def ready(self) -> bool:
        """只有 Node 与入口均满足要求时返回 True。"""
        return self.node is not None and self.entry is not None and self.problem is None


def inspect_pi_tui(environ: Mapping[str, str] | None = None) -> PiTuiInspection:
    """只读检查 pi-tui 所需 Node 版本和编译入口。

    Args:
        environ: 可覆盖 `MINICLAW_NODE`、`MINICLAW_TUI_ENTRY` 和 PATH 的环境。

    Returns:
        包含已解析路径、版本和第一条可操作问题的检查结果。
    """
    source = os.environ if environ is None else environ
    configured_node = source.get("MINICLAW_NODE", "").strip()
    resolved_node = configured_node or shutil.which("node", path=source.get("PATH"))
    if not resolved_node:
        return PiTuiInspection(None, None, None, "没有找到 Node.js；pi-tui 需要 Node.js >= 22.19.0")
    node = Path(resolved_node).expanduser().resolve(strict=False)
    version = _read_node_version(node)
    if version is None:
        return PiTuiInspection(node, None, None, "无法读取 Node.js 版本")
    if version < MINIMUM_NODE_VERSION:
        current = ".".join(str(part) for part in version)
        return PiTuiInspection(
            node,
            version,
            None,
            f"pi-tui 需要 Node.js >= 22.19.0；当前为 {current}",
        )

    configured_entry = source.get("MINICLAW_TUI_ENTRY", "").strip()
    entry = (
        Path(configured_entry).expanduser().resolve(strict=False)
        if configured_entry
        else Path(__file__).resolve().parents[2] / "tui" / "dist" / "main.js"
    )
    if not entry.is_file():
        return PiTuiInspection(
            node,
            version,
            None,
            f"pi-tui 尚未构建：{entry}；请运行 pnpm --dir tui build",
        )
    return PiTuiInspection(node, version, entry, None)


def run_default_tui(
    paths: StatePaths,
    *,
    environ: Mapping[str, str] | None = None,
    stderr: TextIO | None = None,
) -> int:
    """运行 Owner 选择的唯一 TUI，并保持 Textual 为迁移期回退。

    Args:
        paths: 当前 MiniClaw 状态路径。
        environ: UI 模式与 Node 路径来源；默认当前环境。
        stderr: 输出 fallback 诊断的流；默认进程 stderr。

    Returns:
        Node pi-tui 或 Textual App 的退出码。

    Raises:
        TuiLaunchError: UI mode 未知、显式 pi 不可用或 Node 启动失败。
    """
    source = os.environ if environ is None else environ
    error_stream = sys.stderr if stderr is None else stderr
    mode = source.get("MINICLAW_TUI", "auto").strip().lower() or "auto"
    if mode not in {"auto", "pi", "textual"}:
        raise TuiLaunchError("MINICLAW_TUI must be auto, pi, or textual")
    if mode == "textual":
        return run_tui(paths)

    inspection = inspect_pi_tui(source)
    if not inspection.ready:
        assert inspection.problem is not None
        if mode == "pi":
            raise TuiLaunchError(inspection.problem)
        print(f"warning: {inspection.problem}；回退 Textual。", file=error_stream)
        return run_tui(paths)

    assert inspection.node is not None and inspection.entry is not None
    child_env = dict(source)
    child_env.update(
        {
            "MINICLAW_HOME": str(paths.home),
            "MINICLAW_PYTHON": sys.executable,
        }
    )
    try:
        completed = subprocess.run(
            [str(inspection.node), str(inspection.entry)],
            env=child_env,
            check=False,
            shell=False,
        )
    except OSError:
        if mode == "auto":
            print("warning: pi-tui 启动失败；回退 Textual。", file=error_stream)
            return run_tui(paths)
        raise TuiLaunchError("pi-tui process could not be started") from None
    return int(completed.returncode)


def _read_node_version(node: Path) -> tuple[int, int, int] | None:
    """在两秒预算内读取并解析 Node 的三段语义版本。"""
    try:
        completed = subprocess.run(
            [str(node), "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    matched = _NODE_VERSION.fullmatch(completed.stdout.strip())
    if matched is None:
        return None
    return tuple(int(part) for part in matched.groups())
