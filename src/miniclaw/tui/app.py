"""MiniClaw 的简洁全屏 Textual 应用。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Footer, Markdown, Static, TextArea

from miniclaw import __version__
from miniclaw.agent.events import RunEvent
from miniclaw.bootstrap import BootstrapError, initialize_state
from miniclaw.config import ConfigError
from miniclaw.paths import StatePaths
from miniclaw.storage.database import DatabaseError
from miniclaw.storage.migrations import MigrationError

if TYPE_CHECKING:
    from miniclaw.runtime import AgentRuntime

_DEFAULT_SESSION = "default"
_TOOL_PREVIEW_CHARS = 2_000


class ToolCard(Static):
    """以文字标签展示一次 Tool 调用的当前状态。"""

    def __init__(self, call_id: str, tool_name: str, summary: str) -> None:
        """保存稳定调用标识并显示 requested 初态。"""
        super().__init__(markup=False, classes="tool-card")
        self.call_id = call_id
        self.tool_name = _terminal_safe(tool_name)
        self.summary = _terminal_safe(summary)
        self.status = "requested"
        self.duration_ms: int | None = None
        self.preview = ""
        self._refresh_content()

    def set_status(
        self,
        status: str,
        *,
        duration_ms: int | None = None,
        preview: str = "",
    ) -> None:
        """更新可见状态、耗时和有界结果预览。"""
        self.status = _terminal_safe(status)
        self.duration_ms = duration_ms
        self.preview = _terminal_safe(preview)[:_TOOL_PREVIEW_CHARS]
        self._refresh_content()

    def _refresh_content(self) -> None:
        """用不依赖颜色的稳定标签刷新卡片正文。"""
        lines = [f"Tool: {self.tool_name}", f"Status: {self.status}"]
        if self.summary:
            lines.append(self.summary)
        if self.duration_ms is not None:
            lines.append(f"Duration: {self.duration_ms} ms")
        if self.preview:
            lines.append(self.preview)
        self.update("\n".join(lines))


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

    .assistant, .tool-card, .local-message {
        height: auto;
        margin: 1 0;
    }

    .tool-card {
        border: round $surface-lighten-2;
        padding: 0 1;
    }

    #onboarding {
        align: center middle;
        padding: 2 4;
    }

    #onboarding Button {
        width: 24;
        margin-top: 1;
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
        self._state_ready = runtime is not None or _is_initialized(paths)
        self._assistant_text: dict[int, str] = {}
        self._assistant_widgets: dict[int, Markdown] = {}
        self._tool_cards: dict[str, ToolCard] = {}

    def compose(self) -> ComposeResult:
        """生成状态栏、可滚动记录、输入区和快捷键页脚。"""
        if not self._state_ready:
            yield Vertical(
                Static("Initialize MiniClaw", classes="role", markup=False),
                Static(
                    f"State directory: {self.paths.home}",
                    id="onboarding-path",
                    markup=False,
                ),
                Static("", id="onboarding-error", markup=False),
                Button("Initialize", id="initialize", variant="primary"),
                Button("Exit", id="onboarding-exit"),
                id="onboarding",
            )
            return
        yield Static(self._status_text(), id="status", markup=False)
        yield VerticalScroll(id="transcript")
        yield TextArea(id="composer")
        yield Footer()

    def on_mount(self) -> None:
        """启动后把键盘焦点放进唯一输入框。"""
        composer = self.query("#composer")
        if composer:
            composer.first(TextArea).focus()

    def _status_text(self) -> str:
        """返回不暴露本机绝对路径的单行运行状态。"""
        model = self.runtime.model if self.runtime is not None else "not-configured"
        workspace = self.runtime.workspace if self.runtime is not None else self.paths.workspace
        return _terminal_safe(
            f"MiniClaw {__version__} · {model} · "
            f"session:{self.session_id} · workspace:{workspace.name}"
        )

    async def on_run_event(self, event: RunEvent) -> None:
        """把 Core 事件投影到当前 Turn 的临时消息或 Tool 卡片。"""
        if event.kind == "model_text_delta":
            await self._append_model_delta(event)
        elif event.kind == "turn_finished":
            await self._finish_assistant(event)
        elif event.kind == "tool_requested":
            await self._request_tool(event)
        elif event.kind in {"tool_started", "tool_finished"}:
            await self._update_tool(event)
        self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    async def _append_model_delta(self, event: RunEvent) -> None:
        """把一个安全文本增量追加到该 Turn 的唯一临时消息。"""
        value = event.data.get("text")
        if not isinstance(value, str):
            return
        text = self._assistant_text.get(event.turn_id, "") + _terminal_safe(value)
        self._assistant_text[event.turn_id] = text
        message = self._assistant_widgets.get(event.turn_id)
        if message is None:
            message = Markdown(
                text,
                id=f"assistant-{event.turn_id}",
                classes="assistant temporary",
                open_links=False,
            )
            self._assistant_widgets[event.turn_id] = message
            await self.query_one("#transcript", VerticalScroll).mount(message)
        else:
            await message.update(text)

    async def _finish_assistant(self, event: RunEvent) -> None:
        """用最终正文固化临时消息并移除 temporary 状态。"""
        value = event.data.get("content")
        content = (
            _terminal_safe(value)
            if isinstance(value, str)
            else self._assistant_text.get(event.turn_id, "")
        )
        message = self._assistant_widgets.get(event.turn_id)
        if message is None:
            if not content:
                return
            message = Markdown(
                content,
                id=f"assistant-{event.turn_id}",
                classes="assistant",
                open_links=False,
            )
            self._assistant_widgets[event.turn_id] = message
            await self.query_one("#transcript", VerticalScroll).mount(message)
        else:
            await message.update(content)
            message.remove_class("temporary")
        self._assistant_text[event.turn_id] = content

    async def _request_tool(self, event: RunEvent) -> None:
        """为新的 call ID 创建一张无动态 CSS ID 的 Tool 卡片。"""
        call_id = event.data.get("call_id")
        tool_name = event.data.get("tool_name")
        summary = event.data.get("summary", "")
        if (
            not isinstance(call_id, str)
            or not isinstance(tool_name, str)
            or not isinstance(summary, str)
            or call_id in self._tool_cards
        ):
            return
        card = ToolCard(call_id, tool_name, summary)
        self._tool_cards[call_id] = card
        await self.query_one("#transcript", VerticalScroll).mount(card)

    async def _update_tool(self, event: RunEvent) -> None:
        """按 call ID 更新已有卡片，不从不可信 ID 构造选择器。"""
        call_id = event.data.get("call_id")
        if not isinstance(call_id, str):
            return
        card = self._tool_cards.get(call_id)
        if card is None:
            return
        status_value = event.data.get("status")
        status = (
            status_value
            if isinstance(status_value, str)
            else "running"
            if event.kind == "tool_started"
            else "failed"
        )
        duration_value = event.data.get("duration_ms")
        duration_ms = duration_value if type(duration_value) is int else None
        preview_value = event.data.get("preview")
        preview = preview_value if isinstance(preview_value, str) else ""
        card.set_status(status, duration_ms=duration_ms, preview=preview)

    async def handle_local_command(self, text: str) -> bool:
        """处理固定 Slash Command；普通消息返回 False 交给 Agent。"""
        command = text.strip()
        if not command.startswith("/"):
            return False
        match command:
            case "/help":
                await self._append_local_message(
                    "/help · /status · /tools · /new · /exit · /quit\n"
                    "Enter sends · Shift+Enter inserts a line · Esc cancels"
                )
            case "/status":
                model = self.runtime.model if self.runtime is not None else "not-configured"
                await self._append_local_message(
                    f"model: {model}\nsession: {self.session_id}\nstate: idle"
                )
            case "/tools":
                definitions = (
                    self.runtime.tool_definitions if self.runtime is not None else ()
                )
                await self._append_local_message(
                    "\n".join(
                        f"{definition.name} ({definition.risk.value})"
                        for definition in definitions
                    )
                    or "No tools are available."
                )
            case "/new":
                await self._new_session()
            case "/exit" | "/quit":
                self.exit(0)
            case _:
                await self._append_local_message(f"Unknown command: {command}")
        return True

    async def _append_local_message(self, content: str) -> None:
        """向 transcript 添加一条不解释 markup 的本地状态消息。"""
        message = Static(
            _terminal_safe(content),
            markup=False,
            classes="local-message",
        )
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(message)
        transcript.scroll_end(animate=False)

    async def _new_session(self) -> None:
        """切换随机本地 Session，并清空当前界面投影。"""
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        self._assistant_text.clear()
        self._assistant_widgets.clear()
        self._tool_cards.clear()
        self.session_id = f"session-{uuid4().hex[:8]}"
        self.query_one("#status", Static).update(self._status_text())

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """处理同 App 初始化与退出按钮。"""
        if event.button.id == "onboarding-exit":
            self.exit(0)
            return
        if event.button.id != "initialize":
            return
        try:
            initialize_state(self.paths)
        except (BootstrapError, ConfigError, DatabaseError, MigrationError, OSError) as error:
            self.query_one("#onboarding-error", Static).update(
                _terminal_safe(f"Initialization failed: {error}")
            )
            return
        self._state_ready = True
        await self.recompose()
        self.query_one("#composer", TextArea).focus()


def _terminal_safe(value: str) -> str:
    """移除能改变终端状态的控制字符，同时保留文本布局。"""
    return "".join(
        character
        for character in value
        if character in "\n\t"
        or ord(character) >= 0x20
        and not 0x7F <= ord(character) <= 0x9F
    )


def _is_initialized(paths: StatePaths) -> bool:
    """判断完整非符号链接状态是否已存在，不创建任何文件。"""
    required = (paths.config, paths.database, paths.soul, paths.user)
    return (
        paths.home.is_dir()
        and not paths.home.is_symlink()
        and all(path.is_file() and not path.is_symlink() for path in required)
    )


def run_tui(paths: StatePaths) -> int:
    """运行 MiniClaw 全屏应用并返回稳定进程退出码。"""
    result = MiniClawApp(paths).run()
    return 0 if result is None else result
