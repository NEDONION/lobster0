"""MiniClaw 的简洁全屏 Textual 应用。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Markdown, Static, TextArea

from miniclaw import __version__
from miniclaw.agent.events import RunEvent
from miniclaw.paths import StatePaths

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

    .assistant, .tool-card {
        height: auto;
        margin: 1 0;
    }

    .tool-card {
        border: round $surface-lighten-2;
        padding: 0 1;
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
        self._assistant_text: dict[int, str] = {}
        self._assistant_widgets: dict[int, Markdown] = {}
        self._tool_cards: dict[str, ToolCard] = {}

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
