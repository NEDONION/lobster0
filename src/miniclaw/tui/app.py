"""MiniClaw 的简洁全屏 Textual 应用。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Footer, Markdown, Static, TextArea
from textual.worker import Worker

from miniclaw import __version__
from miniclaw.agent.events import RunEvent
from miniclaw.bootstrap import BootstrapError, initialize_state
from miniclaw.config import ConfigError, load_config
from miniclaw.env import DotEnvError, load_dotenv
from miniclaw.paths import StatePaths
from miniclaw.runtime import create_runtime
from miniclaw.storage.database import DatabaseError
from miniclaw.storage.migrations import MigrationError

if TYPE_CHECKING:
    from miniclaw.runtime import AgentRuntime

_DEFAULT_SESSION = "default"
_TOOL_PREVIEW_CHARS = 2_000
_TRACE_DETAIL_CHARS = 8_000


class Composer(TextArea):
    """支持 Enter 发送、Shift+Enter 换行的唯一输入框。"""

    BINDINGS = [
        Binding("enter", "submit", show=False, priority=True),
        Binding("shift+enter", "insert_newline", show=False),
    ]

    class Submitted(Message):
        """携带用户提交时的完整输入。"""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def action_submit(self) -> None:
        """提交非空文本，不让 TextArea 同时插入换行。"""
        if self.text.strip():
            self.post_message(self.Submitted(self.text))

    def action_insert_newline(self) -> None:
        """在当前光标位置插入一行。"""
        self.insert("\n")


class ToolCard(Collapsible):
    """始终展示 Tool 状态，按需展开参数和结果。"""

    def __init__(
        self,
        call_id: str,
        tool_name: str,
        summary: str,
        arguments: dict[str, object],
    ) -> None:
        """保存稳定调用标识并显示 requested 初态。"""
        self._detail = Static("", markup=False, classes="trace-detail")
        super().__init__(
            self._detail,
            collapsed=True,
            classes="trace-card tool-card",
        )
        self.call_id = call_id
        self.tool_name = _terminal_safe(tool_name)
        self.summary = _terminal_safe(summary)
        self.arguments = _terminal_safe(
            json.dumps(arguments, ensure_ascii=False, indent=2, sort_keys=True)
        )[:_TRACE_DETAIL_CHARS]
        self.status = "requested"
        self.status_history = [self.status]
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
        if self.status != self.status_history[-1]:
            self.status_history.append(self.status)
        self.duration_ms = duration_ms
        self.preview = _terminal_safe(preview)[:_TOOL_PREVIEW_CHARS]
        self._refresh_content()

    def _refresh_content(self) -> None:
        """用不依赖颜色的标签刷新概要和详情。"""
        self.title = f"Tool: {self.tool_name} · Status: {self.status}"
        lines: list[str] = []
        if self.summary:
            lines.extend(("Request", self.summary))
        lines.extend(
            (
                "Arguments",
                self.arguments,
                "Lifecycle",
                " -> ".join(self.status_history),
            )
        )
        if self.duration_ms is not None:
            lines.extend(("Execution", f"Duration: {self.duration_ms} ms"))
        if self.preview:
            lines.extend(("Result preview", self.preview))
        self._detail.update("\n".join(lines))


class ReasoningCard(Collapsible):
    """展示 Provider 明确返回的有界 reasoning，不代表内部思维链。"""

    def __init__(self, turn_id: int, text: str) -> None:
        """默认折叠详情，但始终保留可聚焦概要。"""
        detail = _terminal_safe(text)[:_TRACE_DETAIL_CHARS]
        super().__init__(
            Static(detail, markup=False, classes="trace-detail"),
            title=f"Reasoning (provider) · Turn {turn_id}",
            collapsed=True,
            classes="trace-card reasoning-card",
        )


class ApprovalModal(ModalScreen[bool]):
    """展示完整绑定参数，并只提供一次允许或拒绝。"""

    CSS = """
    ApprovalModal {
        align: center middle;
        background: $background 70%;
    }

    #approval-dialog {
        width: 80%;
        max-width: 100;
        height: 80%;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }

    #approval-body {
        height: 1fr;
        margin: 1 0;
    }

    #approval-actions {
        height: 3;
        align-horizontal: right;
    }

    #approval-actions Button {
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "deny", show=False, priority=True)]

    def __init__(self, event: RunEvent) -> None:
        """从已提交的 approval_required 事件读取可见字段。"""
        super().__init__()
        approval_id = event.data.get("approval_id")
        tool_name = event.data.get("tool_name")
        summary = event.data.get("summary")
        arguments = event.data.get("arguments")
        expires_at = event.data.get("expires_at")
        if (
            type(approval_id) is not int
            or not isinstance(tool_name, str)
            or not isinstance(summary, str)
            or not isinstance(arguments, dict)
            or not isinstance(expires_at, str)
        ):
            raise ValueError("invalid approval event")
        self.approval_id = approval_id
        self.tool_name = _terminal_safe(tool_name)
        self.summary = _terminal_safe(summary)
        self.arguments = _terminal_safe(
            json.dumps(arguments, ensure_ascii=False, indent=2, sort_keys=True)
        )
        self.expires_at = _terminal_safe(expires_at)

    def compose(self) -> ComposeResult:
        """生成带文字标签、滚动参数区和两个决定按钮的弹窗。"""
        yield Vertical(
            Static(f"Approval #{self.approval_id}", markup=False),
            Static(f"Tool: {self.tool_name}", markup=False),
            Static(self.summary, markup=False),
            VerticalScroll(
                Static(self.arguments, markup=False),
                id="approval-body",
            ),
            Static(f"Expires: {self.expires_at}", markup=False),
            Horizontal(
                Button("Deny", id="approval-deny", variant="error"),
                Button("Allow once", id="approval-allow-once", variant="warning"),
                id="approval-actions",
            ),
            id="approval-dialog",
        )

    def on_mount(self) -> None:
        """危险操作默认聚焦 Deny。"""
        self.query_one("#approval-deny", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """关闭弹窗并返回唯一一次人工决定。"""
        event.stop()
        self.dismiss(event.button.id == "approval-allow-once")

    def action_deny(self) -> None:
        """Esc 与显式 Deny 使用同一安全决定。"""
        self.dismiss(False)


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

    .assistant, .trace-card, .local-message {
        height: auto;
        margin: 1 0;
    }

    .trace-card {
        border: round $surface-lighten-2;
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
        Binding("ctrl+o", "toggle_traces", "Trace details", show=True),
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
        self._state_ready = runtime is not None
        self._assistant_text: dict[int, str] = {}
        self._assistant_widgets: dict[int, Markdown] = {}
        self._tool_cards: dict[str, ToolCard] = {}
        self._active_worker: Worker[None] | None = None
        self._pending_approval: RunEvent | None = None

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
        yield Composer(id="composer")
        yield Footer()

    def on_mount(self) -> None:
        """启动后把键盘焦点放进唯一输入框。"""
        composer = self.query("#composer")
        if composer:
            composer.first(TextArea).focus()

    async def on_unmount(self) -> None:
        """在 Textual 事件循环关闭前释放唯一 Provider。"""
        close = getattr(self.runtime, "aclose", None)
        if close is not None:
            await close()

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
        elif event.kind == "model_reasoning":
            await self._append_reasoning(event)
        elif event.kind == "turn_finished":
            await self._finish_assistant(event)
        elif event.kind == "tool_requested":
            await self._request_tool(event)
        elif event.kind == "approval_required":
            self._pending_approval = event
            await self._mark_tool_waiting(event)
        elif event.kind in {"tool_started", "tool_finished"}:
            await self._update_tool(event)
        self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    async def _append_reasoning(self, event: RunEvent) -> None:
        """为每次 Provider reasoning 事件保留一张可展开卡片。"""
        value = event.data.get("text")
        if not isinstance(value, str) or not value.strip():
            return
        await self.query_one("#transcript", VerticalScroll).mount(
            ReasoningCard(event.turn_id, value)
        )

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
        arguments = event.data.get("arguments", {})
        if (
            not isinstance(call_id, str)
            or not isinstance(tool_name, str)
            or not isinstance(summary, str)
            or not isinstance(arguments, dict)
            or call_id in self._tool_cards
        ):
            return
        card = ToolCard(call_id, tool_name, summary, arguments)
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

    async def _mark_tool_waiting(self, event: RunEvent) -> None:
        """把审批对应的现有 Tool 卡片标为等待人工决定。"""
        call_id = event.data.get("call_id")
        if isinstance(call_id, str) and call_id in self._tool_cards:
            self._tool_cards[call_id].set_status("waiting_approval")

    async def handle_local_command(self, text: str) -> bool:
        """处理固定 Slash Command；普通消息返回 False 交给 Agent。"""
        command = text.strip()
        if not command.startswith("/"):
            return False
        match command:
            case "/help":
                await self._append_local_message(
                    "/help · /status · /tools · /new · /exit · /quit\n"
                    "Enter sends · Shift+Enter inserts a line · Esc cancels · "
                    "Ctrl+O toggles trace details"
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

    async def on_composer_submitted(self, event: Composer.Submitted) -> None:
        """清空输入并用一个独占 Worker 执行普通消息。"""
        composer = self.query_one("#composer", Composer)
        text = event.text
        composer.clear()
        if await self.handle_local_command(text):
            return
        if self.runtime is None:
            await self._append_local_message("Agent runtime is not available.")
            return
        await self._append_user_message(text)
        composer.disabled = True
        self._active_worker = self.run_worker(
            self._run_turn(text),
            group="turn",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_turn(self, text: str) -> None:
        """在后台执行一次 Turn，并始终恢复唯一输入框。"""
        assert self.runtime is not None
        completed = False
        try:
            await self.runtime.service.handle(
                self.runtime.owner_id,
                text,
                self.session_id,
                on_event=self.on_run_event,
            )
            completed = True
        except asyncio.CancelledError:
            await self._append_local_message("Turn cancelled.")
            raise
        except Exception as error:
            await self._append_local_message(f"Turn failed: {type(error).__name__}")
        finally:
            composer = self.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()
            if completed:
                self._show_pending_approval()

    def _show_pending_approval(self) -> None:
        """在原 Turn 已安全返回后展示一个待审批弹窗。"""
        event = self._pending_approval
        self._pending_approval = None
        if event is None:
            return
        try:
            modal = ApprovalModal(event)
        except ValueError:
            self.run_worker(
                self._append_local_message("Approval event is invalid."),
                exit_on_error=False,
            )
            return
        self.push_screen(
            modal,
            lambda approved: self._start_approval(modal.approval_id, approved),
        )

    def _start_approval(self, approval_id: int, approved: bool | None) -> None:
        """从 Modal 回调启动唯一 continuation Worker。"""
        if approved is None or self.runtime is None:
            return
        composer = self.query_one("#composer", Composer)
        composer.disabled = True
        self._active_worker = self.run_worker(
            self._continue_approval(approval_id, approved),
            group="turn",
            exclusive=True,
            exit_on_error=False,
        )

    async def _continue_approval(self, approval_id: int, approved: bool) -> None:
        """只经 TurnService 继续已绑定动作，并恢复输入框。"""
        assert self.runtime is not None
        completed = False
        try:
            await self.runtime.service.continue_approval(
                self.runtime.owner_id,
                approval_id,
                approved=approved,
                on_event=self.on_run_event,
            )
            completed = True
        except asyncio.CancelledError:
            await self._append_local_message("Turn cancelled.")
            raise
        except Exception as error:
            await self._append_local_message(f"Turn failed: {type(error).__name__}")
        finally:
            composer = self.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()
            if completed:
                self._show_pending_approval()

    async def _append_user_message(self, content: str) -> None:
        """把原始用户文本安全显示在 transcript。"""
        message = Static(
            _terminal_safe(f"You\n{content}"),
            markup=False,
            classes="user-message",
        )
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(message)
        transcript.scroll_end(animate=False)

    async def action_cancel_turn(self) -> None:
        """取消当前后台 Turn；空闲时不做任何事。"""
        if self._active_worker is not None and not self._active_worker.is_finished:
            self._active_worker.cancel()

    def action_exit_if_idle(self) -> None:
        """仅在没有运行中 Turn 时退出。"""
        if self._active_worker is None or self._active_worker.is_finished:
            self.exit(0)

    def action_toggle_traces(self) -> None:
        """展开全部折叠卡；已全展开时收起全部详情。"""
        cards = list(self.query(Collapsible))
        expand = any(card.collapsed for card in cards)
        for card in cards:
            card.collapsed = not expand

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
            self.runtime = _load_runtime(self.paths)
        except (
            BootstrapError,
            ConfigError,
            DatabaseError,
            DotEnvError,
            MigrationError,
            OSError,
        ) as error:
            self.query_one("#onboarding-error", Static).update(
                _terminal_safe(f"Initialization failed: {error}")
            )
            return
        self._state_ready = True
        await self.recompose()
        self.query_one("#composer", Composer).focus()


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
    runtime = _load_runtime(paths) if _is_initialized(paths) else None
    result = MiniClawApp(paths, runtime=runtime).run()
    return 0 if result is None else result


def _load_runtime(paths: StatePaths) -> AgentRuntime:
    """从当前 `.env` 和已初始化状态装配唯一 Runtime。"""
    load_dotenv(Path.cwd() / ".env")
    config = load_config(paths)
    api_key = os.environ.get(config.provider.api_key_env, "").strip()
    if not api_key:
        raise ConfigError(f"{config.provider.api_key_env} is not configured")
    return create_runtime(config, paths, api_key)
