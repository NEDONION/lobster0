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

from lobster0.paths import StatePaths

_SUPPORTED_NODE_MESSAGE = "Node.js 22.22.3～<23 或 24.15.0～<25"
_NODE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_NODE_INJECTION_VARIABLES = ("NODE_OPTIONS", "NODE_PATH")


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


def is_supported_node_version(version: tuple[int, int, int]) -> bool:
    """判断 Node 三段版本是否落入已验证的 22/24 LTS 区间。

    Args:
        version: Node 的 major、minor、patch 三段非负整数。

    Returns:
        仅当版本属于 22.22.3～22.x 或 24.15.0～24.x 时返回 True。
    """
    if (
        type(version) is not tuple
        or len(version) != 3
        or any(type(part) is not int or part < 0 for part in version)
    ):
        return False
    return (22, 22, 3) <= version < (23, 0, 0) or (24, 15, 0) <= version < (25, 0, 0)


def inspect_pi_tui(environ: Mapping[str, str] | None = None) -> PiTuiInspection:
    """只读检查 pi-tui 所需 Node 版本和编译入口。

    Args:
        environ: 可覆盖 `LOBSTER0_NODE`、`LOBSTER0_TUI_ENTRY` 和 PATH 的环境。

    Returns:
        包含已解析路径、版本和第一条可操作问题的检查结果。
    """
    source = os.environ if environ is None else environ
    node_environment = _sanitized_node_environment(source)
    configured_node = source.get("LOBSTER0_NODE", "").strip()
    resolved_node = configured_node or shutil.which("node", path=source.get("PATH"))
    if not resolved_node:
        return PiTuiInspection(
            None,
            None,
            None,
            f"没有找到 Node.js；pi-tui 需要 {_SUPPORTED_NODE_MESSAGE}",
        )
    node = Path(resolved_node).expanduser().resolve(strict=False)
    version = _read_node_version(node, node_environment)
    if version is None:
        return PiTuiInspection(node, None, None, "无法读取 Node.js 版本")
    if not is_supported_node_version(version):
        current = ".".join(str(part) for part in version)
        return PiTuiInspection(
            node,
            version,
            None,
            f"pi-tui 需要 {_SUPPORTED_NODE_MESSAGE}；当前为 {current}",
        )

    configured_entry = source.get("LOBSTER0_TUI_ENTRY", "").strip()
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
        paths: 当前 Lobster0 状态路径。
        environ: UI 模式与 Node 路径来源；默认当前环境。
        stderr: 输出 fallback 诊断的流；默认进程 stderr。

    Returns:
        Node pi-tui 或 Textual App 的退出码。

    Raises:
        TuiLaunchError: UI mode 未知、显式 pi 不可用或 Node 启动失败。
    """
    # Textual 栈（rich 等）只在回退路径才需要。放在函数内 import，模块顶层就
    # 只依赖 stdlib 与 lobster0.paths——release 的 bundle 脚本正是靠这一点，
    # 才能只为 is_supported_node_version 而 import 本模块，无需安装运行期依赖。
    from lobster0.tui import run_tui

    source = os.environ if environ is None else environ
    error_stream = sys.stderr if stderr is None else stderr
    mode = source.get("LOBSTER0_TUI", "auto").strip().lower() or "auto"
    if mode not in {"auto", "pi", "textual"}:
        raise TuiLaunchError("LOBSTER0_TUI must be auto, pi, or textual")
    if mode == "textual":
        return run_tui(paths)
    if not paths.config.is_file():
        if mode == "pi":
            raise TuiLaunchError("pi-tui requires initialized state; run lobster0 init first")
        print("warning: 状态尚未初始化；使用 Textual onboarding。", file=error_stream)
        return run_tui(paths)

    inspection = inspect_pi_tui(source)
    if not inspection.ready:
        assert inspection.problem is not None
        if mode == "pi":
            raise TuiLaunchError(inspection.problem)
        print(f"warning: {inspection.problem}；回退 Textual。", file=error_stream)
        return run_tui(paths)

    assert inspection.node is not None and inspection.entry is not None
    child_env = _sanitized_node_environment(source)
    child_env.update(
        {
            "LOBSTER0_HOME": str(paths.home),
            "LOBSTER0_NODE": str(inspection.node),
            "LOBSTER0_PYTHON": sys.executable,
            "LOBSTER0_TUI_ENTRY": str(inspection.entry),
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


def _sanitized_node_environment(source: Mapping[str, str]) -> dict[str, str]:
    """复制调用方环境并移除可向 Node 注入代码或全局模块的变量。

    Args:
        source: 调用方明确提供的环境；不会隐式合并进程环境。

    Returns:
        不含 Node 注入变量的独立环境字典。
    """
    environment = dict(source)
    for name in _NODE_INJECTION_VARIABLES:
        environment.pop(name, None)
    return environment


def _read_node_version(
    node: Path,
    environment: Mapping[str, str],
) -> tuple[int, int, int] | None:
    """在两秒预算内用调用方封闭环境读取 Node 的三段语义版本。

    Args:
        node: 要探测的显式 Node executable。
        environment: 已由调用方清洗且不会隐式扩展的环境。

    Returns:
        有效的三段版本；启动、超时、退出或格式异常时返回 None。
    """
    try:
        completed = subprocess.run(
            [str(node), "--version"],
            env=dict(environment),
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
