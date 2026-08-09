"""MiniClaw 的简洁全屏 Textual 应用。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING
from uuid import uuid4

from rich.markdown import Markdown as RichMarkdown
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Static, TextArea
from textual.worker import Worker

from miniclaw import __version__
from miniclaw.agent.events import RunEvent
from miniclaw.bootstrap import BootstrapError, initialize_state
from miniclaw.config import ConfigError, load_config
from miniclaw.env import DotEnvError, load_dotenv, resolve_dotenv_path
from miniclaw.paths import StatePaths
from miniclaw.policy.approvals import ApprovalDecision
from miniclaw.runtime import create_runtime
from miniclaw.storage.database import DatabaseError
from miniclaw.storage.migrations import MigrationError

if TYPE_CHECKING:
    from miniclaw.runtime import AgentRuntime

_DEFAULT_SESSION = "default"
_TOOL_PREVIEW_CHARS = 2_000
_TRACE_DETAIL_CHARS = 8_000
_ERROR_SUMMARIES = {
    "zh-CN": {
        "provider_authentication": "模型服务认证失败",
        "provider_rate_limit": "模型服务请求过于频繁",
        "provider_timeout": "模型服务请求超时",
        "provider_protocol": "模型请求或响应不符合协议",
        "provider_server": "模型服务暂时不可用",
        "empty_response": "模型返回了空响应",
        "loop_no_progress": "Agent 连续多轮没有新的成功工具结果",
        "loop_limit": "Agent 达到工具循环上限",
        "conversation_data": "会话历史数据不完整",
        "context": "构造模型上下文失败",
        "provider": "模型服务调用失败",
        "agent": "Agent 执行失败",
    },
    "en": {
        "provider_authentication": "model provider authentication failed",
        "provider_rate_limit": "model provider rate limit exceeded",
        "provider_timeout": "model provider request timed out",
        "provider_protocol": "model request or response violated the protocol",
        "provider_server": "model provider is temporarily unavailable",
        "empty_response": "model provider returned an empty response",
        "loop_no_progress": "Agent made no new successful tool progress",
        "loop_limit": "Agent reached the tool iteration limit",
        "conversation_data": "conversation history is incomplete",
        "context": "model context construction failed",
        "provider": "model provider request failed",
        "agent": "Agent execution failed",
    },
}
_TEXT = {
    "zh-CN": {
        "session": "会话",
        "workspace": "工作区",
        "context": "上下文",
        "input": "输入",
        "output": "输出",
        "tools": "工具",
        "iterations": "迭代",
        "duration": "耗时",
        "user": "你",
        "agent": "MiniClaw",
        "tool": "工具",
        "status": "状态",
        "request": "请求",
        "arguments": "参数",
        "lifecycle": "流程",
        "execution": "执行",
        "result_preview": "结果预览",
        "reasoning": "思考（模型）· 第 {turn_id} 轮",
        "approval": "审批 #{approval_id}",
        "expires": "过期时间",
        "deny": "拒绝",
        "once": "仅允许一次",
        "session_grant": "本次运行允许",
        "always": "始终允许",
        "shortcuts": "Enter 发送 · Shift+Enter 换行 · Esc 取消 · Ctrl+O 展开详情",
        "help": (
            "/help · /status · /tools · /new · /lang zh|en · /exit · /quit\n"
            "Enter 发送 · Shift+Enter 换行 · Esc 取消 · Ctrl+O 展开详情"
        ),
        "model": "模型",
        "state": "状态",
        "idle": "空闲",
        "no_tools": "没有可用工具。",
        "unknown": "未知命令",
        "language_changed": "界面语言已切换为中文。",
        "invalid_language": "用法：/lang zh|en",
        "cancelled": "本轮已取消，原输入已恢复。",
        "failed": "本轮失败：{error}。原输入已恢复。",
        "runtime_missing": "Agent 运行环境不可用。",
        "approval_invalid": "审批事件无效。",
        "initialize": "初始化 MiniClaw",
        "state_directory": "状态目录",
        "initialize_action": "初始化",
        "exit": "退出",
        "initialization_failed": "初始化失败",
    },
    "en": {
        "session": "session",
        "workspace": "workspace",
        "context": "context",
        "input": "input",
        "output": "output",
        "tools": "tools",
        "iterations": "iterations",
        "duration": "duration",
        "user": "You",
        "agent": "MiniClaw",
        "tool": "Tool",
        "status": "Status",
        "request": "Request",
        "arguments": "Arguments",
        "lifecycle": "Lifecycle",
        "execution": "Execution",
        "result_preview": "Result preview",
        "reasoning": "Reasoning (provider) · Turn {turn_id}",
        "approval": "Approval #{approval_id}",
        "expires": "Expires",
        "deny": "Deny",
        "once": "Allow once",
        "session_grant": "Allow this session",
        "always": "Always allow",
        "shortcuts": "Enter send · Shift+Enter newline · Esc cancel · Ctrl+O details",
        "help": (
            "/help · /status · /tools · /new · /lang zh|en · /exit · /quit\n"
            "Enter sends · Shift+Enter inserts a line · Esc cancels · Ctrl+O details"
        ),
        "model": "model",
        "state": "state",
        "idle": "idle",
        "no_tools": "No tools are available.",
        "unknown": "Unknown command",
        "language_changed": "UI language changed to English.",
        "invalid_language": "Usage: /lang zh|en",
        "cancelled": "Turn cancelled. The original input was restored.",
        "failed": "Turn failed: {error}. The original input was restored.",
        "runtime_missing": "Agent runtime is not available.",
        "approval_invalid": "Approval event is invalid.",
        "initialize": "Initialize MiniClaw",
        "state_directory": "State directory",
        "initialize_action": "Initialize",
        "exit": "Exit",
        "initialization_failed": "Initialization failed",
    },
}


def _t(language: str, key: str, **values: object) -> str:
    """返回两个受支持 UI 语言之一的固定文案。"""
    return _TEXT[language][key].format(**values)


def _failure_label(language: str, code: str | None, error: Exception) -> str:
    """把 Core 错误码映射为安全摘要，未知异常只显示类型。"""
    summary = _ERROR_SUMMARIES[language].get(code or "")
    return f"{code} · {summary}" if summary is not None else type(error).__name__


class ConversationMessage(Vertical):
    """用稳定的角色标签和视觉边界包装一条对话消息。"""

    def __init__(self, role: str, body: Static, *, classes: str) -> None:
        super().__init__(
            Static(role, markup=False, classes="role"),
            body,
            classes=f"conversation-message {classes}",
        )


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
        *,
        language: str = "zh-CN",
    ) -> None:
        """保存稳定调用标识并显示 requested 初态。"""
        self._detail = Static("", markup=False, classes="trace-detail")
        super().__init__(
            self._detail,
            collapsed=True,
            classes="trace-card tool-card",
        )
        self.call_id = call_id
        self.language = language
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
        self.title = (
            f"{_t(self.language, 'tool')}: {self.tool_name} · "
            f"{_t(self.language, 'status')}: {self.status}"
        )
        lines: list[str] = []
        if self.summary:
            lines.extend((_t(self.language, "request"), self.summary))
        lines.extend(
            (
                _t(self.language, "arguments"),
                self.arguments,
                _t(self.language, "lifecycle"),
                " -> ".join(self.status_history),
            )
        )
        if self.duration_ms is not None:
            lines.extend(
                (
                    _t(self.language, "execution"),
                    f"{_t(self.language, 'duration')}: {self.duration_ms} ms",
                )
            )
        if self.preview:
            lines.extend((_t(self.language, "result_preview"), self.preview))
        self._detail.update("\n".join(lines))


class ReasoningCard(Collapsible):
    """展示 Provider 明确返回的有界 reasoning，不代表内部思维链。"""

    def __init__(self, turn_id: int, text: str, *, language: str = "zh-CN") -> None:
        """默认展开弱化详情，并始终保留可聚焦概要。"""
        detail = _terminal_safe(text)[:_TRACE_DETAIL_CHARS]
        super().__init__(
            Static(detail, markup=False, classes="trace-detail"),
            title=_t(language, "reasoning", turn_id=turn_id),
            collapsed=False,
            classes="trace-card reasoning-card",
        )


class ApprovalModal(ModalScreen[ApprovalDecision]):
    """展示完整绑定参数，并只提供 Core 明确允许的授权范围。"""

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
        height: auto;
        align-horizontal: right;
    }

    #approval-actions Button {
        width: auto;
        min-width: 12;
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "deny", show=False, priority=True)]

    def __init__(self, event: RunEvent, *, language: str = "zh-CN") -> None:
        """从已提交的 approval_required 事件读取可见字段。"""
        super().__init__()
        self.language = language
        approval_id = event.data.get("approval_id")
        tool_name = event.data.get("tool_name")
        summary = event.data.get("summary")
        arguments = event.data.get("arguments")
        expires_at = event.data.get("expires_at")
        grant_modes = event.data.get("grant_modes", [ApprovalDecision.ONCE.value])
        if (
            type(approval_id) is not int
            or not isinstance(tool_name, str)
            or not isinstance(summary, str)
            or not isinstance(arguments, dict)
            or not isinstance(expires_at, str)
            or not isinstance(grant_modes, list)
        ):
            raise ValueError("invalid approval event")
        try:
            self.grant_modes = tuple(ApprovalDecision(value) for value in grant_modes)
        except (TypeError, ValueError):
            raise ValueError("invalid approval event") from None
        if ApprovalDecision.ONCE not in self.grant_modes:
            raise ValueError("invalid approval event")
        self.approval_id = approval_id
        self.tool_name = _terminal_safe(tool_name)
        self.summary = _terminal_safe(summary)
        self.arguments = _terminal_safe(
            json.dumps(arguments, ensure_ascii=False, indent=2, sort_keys=True)
        )
        self.expires_at = _terminal_safe(expires_at)

    def compose(self) -> ComposeResult:
        """生成带完整参数和 Core 授权按钮的弹窗。"""
        buttons = [
            Button(_t(self.language, "deny"), id="approval-deny", variant="error")
        ]
        labels = {
            ApprovalDecision.ONCE: _t(self.language, "once"),
            ApprovalDecision.SESSION: _t(self.language, "session_grant"),
            ApprovalDecision.ALWAYS: _t(self.language, "always"),
        }
        buttons.extend(
            Button(
                labels[decision],
                id=f"approval-{decision.value}",
                variant="warning",
            )
            for decision in self.grant_modes
        )
        yield Vertical(
            Static(
                _t(self.language, "approval", approval_id=self.approval_id),
                markup=False,
            ),
            Static(f"{_t(self.language, 'tool')}: {self.tool_name}", markup=False),
            Static(self.summary, markup=False),
            VerticalScroll(
                Static(self.arguments, markup=False),
                id="approval-body",
            ),
            Static(f"{_t(self.language, 'expires')}: {self.expires_at}", markup=False),
            Horizontal(
                *buttons,
                id="approval-actions",
            ),
            id="approval-dialog",
        )

    def on_mount(self) -> None:
        """危险操作默认聚焦 Deny。"""
        self.query_one("#approval-deny", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """关闭弹窗并返回显式人工决定。"""
        event.stop()
        value = event.button.id.removeprefix("approval-")
        self.dismiss(
            ApprovalDecision.DENY
            if value == "deny"
            else ApprovalDecision(value)
        )

    def action_deny(self) -> None:
        """Esc 与显式 Deny 使用同一安全决定。"""
        self.dismiss(ApprovalDecision.DENY)


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

    #telemetry, #shortcuts {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }

    #composer {
        height: 5;
        border: round $accent;
    }

    .conversation-message, .trace-card, .local-message {
        height: auto;
        margin: 1 0;
    }

    .conversation-message {
        padding: 0 1 1 1;
    }

    .conversation-message .role {
        height: 1;
        text-style: bold;
    }

    .conversation-message .message-body {
        height: auto;
        padding: 0 1;
    }

    .user-message {
        background: $surface;
        border-left: thick $primary;
    }

    .assistant-message {
        background: $boost;
        border-left: thick $success;
    }

    .trace-card {
        border: round $surface-lighten-2;
    }

    .reasoning-card {
        margin: 0 3;
        padding: 0;
        border: none;
        color: $text-muted;
        background: $surface-darken-1;
    }

    .reasoning-card .trace-detail {
        padding: 0 1;
        text-style: dim;
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
        configured_language = getattr(runtime, "ui_language", "zh-CN")
        self._language = configured_language if configured_language in _TEXT else "zh-CN"
        self.session_id = _DEFAULT_SESSION
        self._state_ready = runtime is not None
        self._assistant_text: dict[int, str] = {}
        self._assistant_widgets: dict[int, Static] = {}
        self._tool_cards: dict[str, ToolCard] = {}
        self._active_worker: Worker[None] | None = None
        self._pending_approval: RunEvent | None = None
        self._context_tokens: int | None = None
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._tool_calls = 0
        self._iterations = 0
        self._duration_ms: int | None = None
        self._provider_request_id: str | None = None
        self._last_error_code: str | None = None

    def compose(self) -> ComposeResult:
        """生成状态栏、可滚动记录、输入区和快捷键页脚。"""
        if not self._state_ready:
            yield Vertical(
                Static(_t(self._language, "initialize"), classes="role", markup=False),
                Static(
                    f"{_t(self._language, 'state_directory')}: {self.paths.home}",
                    id="onboarding-path",
                    markup=False,
                ),
                Static("", id="onboarding-error", markup=False),
                Button(
                    _t(self._language, "initialize_action"),
                    id="initialize",
                    variant="primary",
                ),
                Button(_t(self._language, "exit"), id="onboarding-exit"),
                id="onboarding",
            )
            return
        yield Static(self._status_text(), id="status", markup=False)
        yield VerticalScroll(id="transcript")
        yield Static(self._telemetry_text(), id="telemetry", markup=False)
        yield Composer(id="composer")
        yield Static(_t(self._language, "shortcuts"), id="shortcuts", markup=False)

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
            f"{_t(self._language, 'session')}:{self.session_id} · "
            f"{_t(self._language, 'workspace')}:{workspace.name}"
        )

    def _telemetry_text(self) -> str:
        """返回只展示 Provider 真实上报值的紧凑审计栏。"""
        budget = getattr(self.runtime, "context_budget_tokens", None)
        context = _metric(self._context_tokens)
        if type(budget) is int and budget > 0:
            context += f"/{_metric(budget)}"
        duration = (
            f"{self._duration_ms} ms" if self._duration_ms is not None else "N/A"
        )
        return _terminal_safe(
            f"{_t(self._language, 'context')} {context} · "
            f"{_t(self._language, 'input')} {_metric(self._input_tokens)} · "
            f"{_t(self._language, 'output')} {_metric(self._output_tokens)} · "
            f"{_t(self._language, 'tools')} {self._tool_calls} · "
            f"{_t(self._language, 'iterations')} {self._iterations} · "
            f"{_t(self._language, 'duration')} {duration}"
        )

    async def on_run_event(self, event: RunEvent) -> None:
        """把 Core 事件投影到当前 Turn 的临时消息或 Tool 卡片。"""
        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        if event.kind in {
            "turn_started",
            "model_usage",
            "turn_finished",
            "turn_failed",
            "turn_cancelled",
        }:
            self._update_telemetry(event)
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
        if follow:
            transcript.scroll_end(animate=False)

    def _update_telemetry(self, event: RunEvent) -> None:
        """用事件中的可信整数更新当前 Turn 指标。"""
        if event.kind == "turn_started":
            self._context_tokens = None
            self._input_tokens = None
            self._output_tokens = None
            self._tool_calls = 0
            self._iterations = 0
            self._duration_ms = None
            self._provider_request_id = None
            self._last_error_code = None
        else:
            mappings = (
                ("context_tokens", "_context_tokens"),
                ("input_tokens", "_input_tokens"),
                ("output_tokens", "_output_tokens"),
                ("tool_calls", "_tool_calls"),
                ("iteration", "_iterations"),
                ("iterations", "_iterations"),
                ("duration_ms", "_duration_ms"),
            )
            for key, attribute in mappings:
                value = event.data.get(key)
                if type(value) is int and value >= 0:
                    setattr(self, attribute, value)
            request_id = event.data.get("provider_request_id")
            if isinstance(request_id, str) and request_id:
                self._provider_request_id = request_id
            error_code = event.data.get("error_code")
            if isinstance(error_code, str) and error_code in _ERROR_SUMMARIES["en"]:
                self._last_error_code = error_code
        widgets = self.query("#telemetry")
        if widgets:
            widgets.first(Static).update(self._telemetry_text())

    async def _append_reasoning(self, event: RunEvent) -> None:
        """为每次 Provider reasoning 事件保留一张可展开卡片。"""
        value = event.data.get("text")
        if not isinstance(value, str) or not value.strip():
            return
        await self.query_one("#transcript", VerticalScroll).mount(
            ReasoningCard(event.turn_id, value, language=self._language)
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
            message = Static(
                text,
                id=f"assistant-{event.turn_id}",
                classes="message-body temporary",
                markup=False,
            )
            self._assistant_widgets[event.turn_id] = message
            await self.query_one("#transcript", VerticalScroll).mount(
                ConversationMessage(
                    _t(self._language, "agent"),
                    message,
                    classes="assistant-message",
                )
            )
        else:
            message.update(text)

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
            message = Static(
                RichMarkdown(content, hyperlinks=False),
                id=f"assistant-{event.turn_id}",
                classes="message-body",
            )
            self._assistant_widgets[event.turn_id] = message
            await self.query_one("#transcript", VerticalScroll).mount(
                ConversationMessage(
                    _t(self._language, "agent"),
                    message,
                    classes="assistant-message",
                )
            )
        else:
            message.update(RichMarkdown(content, hyperlinks=False))
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
        card = ToolCard(
            call_id,
            tool_name,
            summary,
            arguments,
            language=self._language,
        )
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
        if command.startswith("/lang"):
            parts = command.split()
            if len(parts) != 2 or parts[1] not in {"zh", "zh-CN", "en"}:
                await self._append_local_message(_t(self._language, "invalid_language"))
                return True
            self._language = "zh-CN" if parts[1] in {"zh", "zh-CN"} else "en"
            self._refresh_chrome()
            await self._append_local_message(_t(self._language, "language_changed"))
            return True
        match command:
            case "/help":
                await self._append_local_message(_t(self._language, "help"))
            case "/status":
                model = self.runtime.model if self.runtime is not None else "not-configured"
                await self._append_local_message(
                    f"{_t(self._language, 'model')}: {model}\n"
                    f"{_t(self._language, 'session')}: {self.session_id}\n"
                    f"{_t(self._language, 'state')}: {_t(self._language, 'idle')}\n"
                    f"provider_request_id: {self._provider_request_id or 'N/A'}\n"
                    f"{self._telemetry_text()}"
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
                    or _t(self._language, "no_tools")
                )
            case "/new":
                await self._new_session()
            case "/exit" | "/quit":
                self.exit(0)
            case _:
                await self._append_local_message(
                    f"{_t(self._language, 'unknown')}: {command}"
                )
        return True

    def _refresh_chrome(self) -> None:
        """语言切换后原地刷新固定状态条。"""
        for selector, content in (
            ("#status", self._status_text()),
            ("#telemetry", self._telemetry_text()),
            ("#shortcuts", _t(self._language, "shortcuts")),
        ):
            widgets = self.query(selector)
            if widgets:
                widgets.first(Static).update(content)

    async def on_composer_submitted(self, event: Composer.Submitted) -> None:
        """清空输入并用一个独占 Worker 执行普通消息。"""
        composer = self.query_one("#composer", Composer)
        text = event.text
        composer.clear()
        if await self.handle_local_command(text):
            return
        if self.runtime is None:
            composer.load_text(text)
            await self._append_local_message(_t(self._language, "runtime_missing"))
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
            self._restore_draft(text)
            await self._append_local_message(_t(self._language, "cancelled"))
            raise
        except Exception as error:
            self._restore_draft(text)
            await self._append_local_message(
                _t(
                    self._language,
                    "failed",
                    error=_failure_label(self._language, self._last_error_code, error),
                )
            )
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
            modal = ApprovalModal(event, language=self._language)
        except ValueError:
            self.run_worker(
                self._append_local_message(_t(self._language, "approval_invalid")),
                exit_on_error=False,
            )
            return
        self.push_screen(
            modal,
            lambda decision: self._start_approval(modal.approval_id, decision),
        )

    def _start_approval(
        self,
        approval_id: int,
        decision: ApprovalDecision | None,
    ) -> None:
        """从 Modal 回调启动唯一 continuation Worker。"""
        if decision is None or self.runtime is None:
            return
        composer = self.query_one("#composer", Composer)
        composer.disabled = True
        self._active_worker = self.run_worker(
            self._continue_approval(approval_id, decision),
            group="turn",
            exclusive=True,
            exit_on_error=False,
        )

    async def _continue_approval(
        self,
        approval_id: int,
        decision: ApprovalDecision,
    ) -> None:
        """只经 TurnService 继续已绑定动作，并恢复输入框。"""
        assert self.runtime is not None
        completed = False
        try:
            await self.runtime.service.continue_approval(
                self.runtime.owner_id,
                approval_id,
                decision=decision,
                on_event=self.on_run_event,
            )
            completed = True
        except asyncio.CancelledError:
            await self._append_local_message(
                "审批续跑已取消。"
                if self._language == "zh-CN"
                else "Approval continuation cancelled."
            )
            raise
        except Exception as error:
            await self._append_local_message(
                f"审批续跑失败：{type(error).__name__}"
                if self._language == "zh-CN"
                else f"Approval continuation failed: {type(error).__name__}"
            )
        finally:
            composer = self.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()
            if completed:
                self._show_pending_approval()

    async def _append_user_message(self, content: str) -> None:
        """把原始用户文本安全显示在 transcript。"""
        message = ConversationMessage(
            _t(self._language, "user"),
            Static(
                _terminal_safe(content),
                markup=False,
                classes="message-body",
            ),
            classes="user-message",
        )
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(message)
        transcript.scroll_end(animate=False)

    def _restore_draft(self, text: str) -> None:
        """失败或取消时逐字恢复已提交文本和末尾光标。"""
        composer = self.query_one("#composer", Composer)
        composer.load_text(text)
        lines = text.split("\n")
        composer.cursor_location = (len(lines) - 1, len(lines[-1]))

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
        self._context_tokens = None
        self._input_tokens = None
        self._output_tokens = None
        self._tool_calls = 0
        self._iterations = 0
        self._duration_ms = None
        self._provider_request_id = None
        self.session_id = f"session-{uuid4().hex[:8]}"
        self._refresh_chrome()

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
                _terminal_safe(
                    f"{_t(self._language, 'initialization_failed')}: {error}"
                )
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


def _metric(value: int | None) -> str:
    """用紧凑十进制单位显示真实计数；缺失值明确为 N/A。"""
    if value is None:
        return "N/A"
    for divisor, suffix in ((1_000_000, "m"), (1_000, "k")):
        if value >= divisor:
            return f"{value / divisor:.1f}".removesuffix(".0") + suffix
    return str(value)


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
    """从统一 Secret 路径和已初始化状态装配唯一 Runtime。"""
    load_dotenv(resolve_dotenv_path(paths, os.environ))
    config = load_config(paths)
    api_key = os.environ.get(config.provider.api_key_env, "").strip()
    if not api_key:
        raise ConfigError(f"{config.provider.api_key_env} is not configured")
    return create_runtime(config, paths, api_key)
