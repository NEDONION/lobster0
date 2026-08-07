"""MiniClaw 的简洁全屏 Textual 应用。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static, TextArea

from miniclaw import __version__
from miniclaw.paths import StatePaths

if TYPE_CHECKING:
    from miniclaw.runtime import AgentRuntime

_DEFAULT_SESSION = "default"


class MiniClawApp(App[int]):
    """展示状态、对话记录和唯一输入框的本地 Agent 界面。"""

    CSS = """
    Screen {
        layout: vertical;
    }

    #status {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }

    #transcript {
        height: 1fr;
        padding: 0 1;
    }

    #composer {
        height: 5;
        border: round $accent;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel_turn", "Cancel", show=True),
        Binding("ctrl+o", "toggle_tools", "Tool details", show=True),
        Binding("ctrl+d", "exit_if_idle", "Exit", show=True),
    ]

    def __init__(
        self,
        paths: StatePaths,
        *,
        runtime: AgentRuntime | None = None,
    ) -> None:
        """绑定安全状态路径和可选的已装配 Agent Runtime。"""
        super().__init__()
        self.paths = paths
        self.runtime = runtime
        self.session_id = _DEFAULT_SESSION

    def compose(self) -> ComposeResult:
        """生成状态栏、可滚动记录、输入区和快捷键页脚。"""
        yield Static(self._status_text(), id="status", markup=False)
        yield VerticalScroll(id="transcript")
        yield TextArea(id="composer")
        yield Footer()

    def on_mount(self) -> None:
        """启动后把键盘焦点放进唯一输入框。"""
        self.query_one("#composer", TextArea).focus()

    def _status_text(self) -> str:
        """返回不暴露本机绝对路径的单行运行状态。"""
        model = self.runtime.model if self.runtime is not None else "not-configured"
        workspace = self.runtime.workspace if self.runtime is not None else self.paths.workspace
        return _terminal_safe(
            f"MiniClaw {__version__} · {model} · "
            f"session:{self.session_id} · workspace:{workspace.name}"
        )


def _terminal_safe(value: str) -> str:
    """移除能改变终端状态的控制字符，同时保留文本布局。"""
    return "".join(
        character
        for character in value
        if character in "\n\t"
        or ord(character) >= 0x20
        and not 0x7F <= ord(character) <= 0x9F
    )


def run_tui(paths: StatePaths) -> int:
    """运行 MiniClaw 全屏应用并返回稳定进程退出码。"""
    result = MiniClawApp(paths).run()
    return 0 if result is None else result
