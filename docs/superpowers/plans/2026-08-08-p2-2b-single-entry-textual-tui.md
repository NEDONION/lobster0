# MiniClaw P2.2B Single-Entry Textual TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 交付唯一的人类对话入口 uv run miniclaw：它在正常 TTY 中进入简洁的 Textual 全屏 TUI，复用同一 TurnService、AgentRunner、ToolExecutor 和 SQLite；支持流式回答、Tool 状态、取消、持久化审批 Allow once / Deny、少量 Slash Commands 与同界面初始化。

**Architecture:** 在现有 Core 上只增加一个进程内 RunEvent 回调，不引入 Event Bus。TUI 通过一个共享 AgentRuntime 调用 TurnService，不直接调用 Provider、Tool 或 SQLite。裸 miniclaw 启动 TUI；init、doctor、eval 保持非对话维护命令。原 P2.2 Approval Core 负责参数绑定、过期和单次消费，TUI 只展示并提交决定。

**Tech Stack:** Python 3.12+、Textual 8.x、现有 httpx、SQLite、argparse、unittest、Ruff；只新增 textual>=8.2,<9 一个直接运行依赖。

## Global Constraints

- 本计划依赖 docs/superpowers/plans/2026-08-08-phase-2-completion.md 的 P2.2 Approval Core 已完成并全绿。
- 本计划覆盖旧 Phase 2 总计划中面向人的 approvals list/show/approve/deny CLI。审批仍保留 Python Service API，但用户只在同一个 TUI 中 Allow once / Deny。
- 不存在 miniclaw chat、miniclaw tui、--plain、chat --message 或 input()/print() REPL。
- init、doctor、eval 是维护和 CI 命令，不是第二套 Agent 对话入口。
- TUI 不直接调用 Provider、Tool、Policy、Repository；所有动作仍走 TurnService → AgentRunner → ToolExecutor。
- 不新增 Rich、prompt_toolkit、Click、Typer、前端运行时、WebSocket、Gateway、Event Bus 或通用 Presenter 框架。
- RunEvent 只承载当前进程实时展示；SQLite 仍是 Message、Turn、ToolRun、Approval、Audit 的事实来源。
- 默认只允许一个 active Turn。运行中再次提交无效，用户先取消再发送。
- Approval MVP 只有 Allow once 和 Deny，不创建永久规则。
- 所有模型文本、Tool 摘要和 Tool 输出在进入终端 Widget 前清除控制字符，不能解释不可信 ANSI。
- 每个非平凡切片先写可观察行为测试并看到 RED，再写最小 GREEN。
- 开发前必须等待当前共享工作区的 P2.2A 修改完成；随后使用独立 worktree，不能覆盖现有未提交改动。
- 规划文档可以描述目标；README、架构、工程文档和进度页只在实现通过门禁后改成“已完成”。

---

## Dependency Contract

开始 Task 1 前，P2.2A 必须至少提供以下公开语义；实际名称若在已合并实现中略有不同，只允许机械对齐，不改变语义：

| Interface | Required fields / behavior |
|---|---|
| ToolExecution | model_text: str；approval_id: int or None |
| AgentRunResult | status: completed or waiting_approval；content；approval_id；existing usage/iteration fields |
| TurnResult | status；turn_id；session_id；content；approval_id；existing usage/request fields |
| TurnService.handle | existing user_id/text/conversation_id/on_text parameters；returns TurnResult |
| TurnService.continue_approval | user_id、approval_id、approved、on_text；returns child TurnResult |
| ApprovalService | owner-checked get/display、Allow once、Deny；never exposes a direct execute bypass |

Approval 必须已经满足：

- canonical JSON + tool-name-bound SHA-256；
- pending → approved/denied/expired，approved → consumed；
- Owner、Turn、ToolRun、arguments hash 和 TTL 绑定；
- 原 Turn waiting_approval，决定后创建 child continuation Turn；
- 重启后可从 SQLite 恢复；
- 重复批准只有一个执行者；
- Audit 不保存原始文件内容、命令参数正文或凭据。

若这些条件未满足，不得在 TUI 中用挂起协程、内存 Future 或临时布尔值补一套审批。

---

## File Map

- Create: src/miniclaw/agent/events.py — 最小 RunEvent 与异步回调类型。
- Modify: src/miniclaw/agent/runner.py — 模型增量、Tool 请求和审批事件。
- Modify: src/miniclaw/agent/turn.py — Turn 开始、完成、失败、取消事件；把回调传入 continuation。
- Modify: src/miniclaw/tools/executor.py — Tool started/finished 事件，不改变 Policy/持久化语义。
- Create: src/miniclaw/runtime.py — 现有运行期装配和资源关闭的唯一位置。
- Create: src/miniclaw/tui/__init__.py — 导出 run_tui。
- Create: src/miniclaw/tui/app.py — App、聊天 Widget、ToolCard、ApprovalModal、Onboarding。
- Modify: src/miniclaw/cli.py — 裸命令进入 TUI，删除旧 chat/REPL。
- Modify: pyproject.toml — 添加 Textual 8.x。
- Modify: uv.lock — 由 uv 同步锁文件。
- Create: tests/test_run_events.py — Core 事件顺序与兼容性。
- Create: tests/test_runtime.py — 共享装配与关闭。
- Create: tests/test_tui.py — Textual run_test()/Pilot 无头交互。
- Modify: tests/test_cli.py — 单入口、TTY、维护命令。
- Delete: tests/test_cli_chat.py — 有价值的 Provider/持久化断言迁移到 runtime/TUI 测试。
- Modify: tests/test_turn.py、tests/test_agent_runner.py、tests/test_tool_executor.py — 事件与审批等待回归。
- Modify: README.md、docs/getting-started/20260807_本地运行指南.md。
- Modify: docs/architecture/20260807_系统架构.md。
- Create: docs/engineering/phase-2/textual-tui.md。
- Modify: docs/engineering/README.md、docs/README.md、docs/progress/index.html。
- Modify: docs/superpowers/specs/2026-08-08-gemini-style-tui-and-lark-cli-design.md — 状态改为已实现，仅在最终门禁后。

---

### Task 0: Reconcile the Phase 2 Plan and Isolate the Work

**Files:**

- Modify: docs/superpowers/plans/2026-08-08-phase-2-completion.md
- Reference: docs/superpowers/specs/2026-08-08-gemini-style-tui-and-lark-cli-design.md

**Purpose:** 防止旧计划继续实现第二套 approvals CLI，并确保 TUI 从包含 Approval Core 的新基线开始。

- [ ] **Step 1: Confirm P2.2A is complete**

Run:

~~~bash
git status --short
git log --oneline -8
uv run python -m unittest tests.test_approvals tests.test_tool_executor tests.test_agent_runner tests.test_turn tests.test_file_tools -v
~~~

Expected:

- Approval、waiting Turn、continuation 和文件写入测试存在且全部通过；
- 没有其他 agent 正在修改这些文件；
- 不把当前用户或主线程的未提交文件带进 TUI 分支。

- [ ] **Step 2: Mark the old human approval CLI steps as superseded**

在 Phase 2 completion 计划 Task 4 中保留 Approval Service、Turn continuation 和测试，删除或划掉：

- approvals list/show/approve/deny；
- --always；
- PolicyRule 人机管理入口；
- tests/test_cli_approvals.py 中只验证 CLI 命令树的断言。

增加一行：

~~~markdown
Superseded by P2.2B: human approval decisions are exposed only through the single Textual TUI; the Python Approval Service remains the stable Core API.
~~~

- [ ] **Step 3: Create an isolated worktree**

先使用 superpowers:using-git-worktrees。基线必须是包含 P2.2A 的最新提交，不能从带未提交修改的共享目录复制状态。

- [ ] **Step 4: Commit the plan reconciliation**

Commit:

~~~text
docs(plan): 统一 Approval 与 single-entry TUI 路线
~~~

---

### Task 1: Add the Minimal In-Process RunEvent Boundary

**Files:**

- Create: src/miniclaw/agent/events.py
- Modify: src/miniclaw/agent/runner.py
- Modify: src/miniclaw/agent/turn.py
- Modify: src/miniclaw/tools/executor.py
- Create: tests/test_run_events.py
- Modify: tests/test_agent_runner.py
- Modify: tests/test_tool_executor.py
- Modify: tests/test_turn.py

**Interfaces:**

- Produces: RunEvent(kind, turn_id, data).
- Produces: RunEventHandler = Callable[[RunEvent], Awaitable[None]].
- Adds optional on_event to TurnService.handle(), TurnService.continue_approval(), AgentRunner.run() and ToolExecutor.execute().
- Existing callers that omit on_event keep identical behavior and persistence.

- [ ] **Step 1: Write RED tests for event order and backward compatibility**

Add tests that assert:

1. plain answer emits turn_started → one or more model_text_delta → turn_finished;
2. Tool path emits tool_requested → tool_started → tool_finished before final turn_finished;
3. Approval path emits approval_required and ends in waiting_approval without a fake successful Tool Message;
4. cancellation emits turn_cancelled and leaves existing cancelled/interrupted database states;
5. provider failure emits turn_failed with a stable error code and no traceback/secret;
6. omitting on_event leaves all existing return values and message history unchanged;
7. a handler exception is isolated from Core persistence/results and logged without raw payload.

Example:

~~~python
async def test_tool_events_follow_the_real_execution_order(self) -> None:
    events: list[RunEvent] = []

    async def capture(event: RunEvent) -> None:
        events.append(event)

    result = await service.handle(
        owner.id,
        "读取 README",
        "events",
        on_event=capture,
    )

    self.assertEqual(result.status, "completed")
    self.assertEqual(
        [event.kind for event in events],
        [
            "turn_started",
            "tool_requested",
            "tool_started",
            "tool_finished",
            "model_text_delta",
            "turn_finished",
        ],
    )
~~~

- [ ] **Step 2: Run RED**

Run:

~~~bash
uv run python -m unittest tests.test_run_events tests.test_agent_runner tests.test_tool_executor tests.test_turn -v
~~~

Expected: ImportError for miniclaw.agent.events or unexpected keyword on_event.

- [ ] **Step 3: Add one event dataclass, not an event framework**

Create:

~~~python
"""Agent Runtime 到本地交互层的进程内事件。"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from miniclaw.providers.base import JsonValue

logger = logging.getLogger(__name__)

type RunEventKind = Literal[
    "turn_started",
    "model_text_delta",
    "tool_requested",
    "tool_started",
    "tool_finished",
    "approval_required",
    "turn_finished",
    "turn_failed",
    "turn_cancelled",
]


@dataclass(frozen=True, slots=True)
class RunEvent:
    """描述当前进程内一次可展示的 Agent 运行变化。"""

    kind: RunEventKind
    turn_id: int
    data: dict[str, JsonValue]


type RunEventHandler = Callable[[RunEvent], Awaitable[None]]


async def emit(
    handler: RunEventHandler | None,
    event: RunEvent,
) -> None:
    """按顺序交付事件，同时隔离展示层普通异常。"""
    if handler is None:
        return
    try:
        await handler(event)
    except Exception:
        logger.error("RunEvent handler failed: %s", event.kind)
~~~

不增加 EventEmitter、订阅列表、后台队列、重试器或持久化表。
CancelledError 不属于这里要吞掉的普通展示异常，必须继续传播并进入现有取消路径。

- [ ] **Step 4: Wire events to real state transitions**

Rules:

- turn_started 只在 Turn 已成功 mark_running 后发出；
- model_text_delta 在 Provider SSE delta 到达时发出，但 SQLite 仍只保存完整合法消息；
- tool_requested 在参数校验前发出，只带 call ID、Tool 名和脱敏 summary，不带敏感原文；
- tool_started 只在 ToolRun 已进入 running 后发出；
- tool_finished 只在 ToolRun 已进入终态后发出，data 包含 status、duration_ms、bounded preview；
- approval_required 只在 pending Approval 已提交 SQLite 后发出，data 只带 approval_id、tool_name、summary、expires_at；
- turn_finished/failed/cancelled 只在对应数据库更新成功后发出。

Runner 中的增量处理保持原有最终文本语义：

~~~python
async def capture_text(chunk: str) -> None:
    round_chunks.append(chunk)
    await emit(
        on_event,
        RunEvent("model_text_delta", tool_context.turn_id, {"text": chunk}),
    )
~~~

on_text 仍只在确认该轮没有 Tool Call 后回放最终文本，避免破坏 Channel 的旧契约；TUI 使用 on_event 获得临时流式段落。

- [ ] **Step 5: Run GREEN and regression**

Run:

~~~bash
uv run python -m unittest tests.test_run_events tests.test_agent_runner tests.test_tool_executor tests.test_turn -v
uv run ruff check src/miniclaw/agent/events.py src/miniclaw/agent/runner.py src/miniclaw/agent/turn.py src/miniclaw/tools/executor.py tests/test_run_events.py
git diff --check
~~~

- [ ] **Step 6: Commit**

Commit:

~~~text
feat(runtime): 增加 in-process RunEvent 边界
~~~

---

### Task 2: Extract the Shared AgentRuntime Assembly

**Files:**

- Create: src/miniclaw/runtime.py
- Modify: src/miniclaw/cli.py
- Create: tests/test_runtime.py
- Modify: tests/test_turn.py

**Interfaces:**

- Produces: AgentRuntime(owner_id, service, approval_service, tool_definitions, provider).
- Produces: open_runtime(config, paths, api_key) -> AgentRuntime.
- Produces: AgentRuntime.aclose().
- Consumes: exactly the same repositories, Provider, Registry, Policy and ToolExecutor already assembled in cli.py.

- [ ] **Step 1: Write RED runtime assembly tests**

Tests must use a FakeProvider or patched OpenAICompatibleProvider and assert:

- tool schemas are registered once in stable order;
- runtime service writes to the same SQLite database;
- owner ID is the initialized local Owner;
- aclose closes Provider exactly once;
- no API key appears in repr(runtime), exception text, Tool schema or database;
- importing miniclaw.runtime performs no I/O.

Example:

~~~python
async def test_runtime_owns_one_provider_and_one_turn_service(self) -> None:
    with mock.patch(
        "miniclaw.runtime.OpenAICompatibleProvider",
        return_value=provider,
    ):
        runtime = open_runtime(config, paths, "private-key")
        result = await runtime.service.handle(runtime.owner_id, "hello", "default")
        await runtime.aclose()

    self.assertEqual(result.content, "world")
    provider.aclose.assert_awaited_once()
    self.assertNotIn("private-key", repr(runtime))
~~~

- [ ] **Step 2: Run RED**

Run:

~~~bash
uv run python -m unittest tests.test_runtime -v
~~~

Expected: ImportError for miniclaw.runtime.

- [ ] **Step 3: Move existing assembly without redesign**

Use one concrete dataclass:

~~~python
@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """保存一个本地进程共用的 Agent 运行期。"""

    owner_id: int
    service: TurnService
    approval_service: ApprovalService
    tool_definitions: tuple[ToolDefinition, ...]
    provider: OpenAICompatibleProvider = field(repr=False)

    async def aclose(self) -> None:
        """关闭运行期唯一 Provider。"""
        await self.provider.aclose()
~~~

open_runtime() 只移动 cli.py 已有装配：

1. apply_migrations；
2. OwnerRepository.get_or_create；
3. Provider；
4. 当前阶段实际已实现的 Tool；
5. ToolRegistry、PolicyEngine、ToolRun/Approval Repository；
6. AgentRunner；
7. TurnService。

不要创建 Runtime Protocol、Builder、Container、Factory class 或 DI framework。

Tool 启用规则：

- 只注册 config.tools.enabled 中当前版本确实实现的 Tool；
- 配置显式点名一个当前构建不存在的 Tool 时稳定 ConfigError；
- P2.3 的 run_command 和 P2.4 的 http_get 以后只在这一处增加；
- 不在 TUI 里重新组装 Registry。

- [ ] **Step 4: Keep maintenance CLI behavior unchanged**

此任务只让旧聊天装配临时调用 open_runtime；真正删除旧 chat 在 Task 6。init、doctor、eval 不导入或启动 Provider。

- [ ] **Step 5: Run GREEN and commit**

Run:

~~~bash
uv run python -m unittest tests.test_runtime tests.test_turn tests.test_cli_chat -v
uv run ruff check src/miniclaw/runtime.py src/miniclaw/cli.py tests/test_runtime.py
git diff --check
~~~

Commit:

~~~text
refactor(runtime): 复用唯一 AgentRuntime 装配
~~~

---

### Task 3: Add Textual and Build the Minimal TUI Shell

**Files:**

- Modify: pyproject.toml
- Modify: uv.lock
- Create: src/miniclaw/tui/__init__.py
- Create: src/miniclaw/tui/app.py
- Create: tests/test_tui.py

**Interfaces:**

- Produces: MiniClawApp(paths, runtime=None).
- Produces: run_tui(paths) -> int.
- Internal only: MessageCard, ToolCard, ApprovalModal, Onboarding.

- [ ] **Step 1: Add Textual 8.x**

Run:

~~~bash
uv add "textual>=8.2,<9"
uv sync --extra dev
~~~

Verify:

~~~bash
uv run python -c "import textual; print(textual.__version__)"
~~~

Expected: an 8.x version. Textual is the only new direct dependency. Rich may appear transitively; do not add it directly.

- [ ] **Step 2: Write RED shell tests with Textual run_test**

Tests use unittest.IsolatedAsyncioTestCase and Textual Pilot:

~~~python
async def test_app_starts_at_80_by_24_with_focused_composer(self) -> None:
    app = MiniClawApp(self.paths, runtime=self.runtime)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        self.assertEqual(app.focused.id, "composer")
        self.assertIn("deepseek-v4-pro", app.query_one("#status").renderable)
        self.assertEqual(len(app.query("#composer")), 1)
~~~

Also assert:

- exactly one status area, transcript and composer;
- labels You and MiniClaw exist, so state is not color-only;
- 80×24 keeps composer visible;
- no network or real personal state is touched;
- exit restores app lifecycle without leaked worker.

- [ ] **Step 3: Run RED**

Run:

~~~bash
uv run python -m unittest tests.test_tui -v
~~~

Expected: ImportError for miniclaw.tui.

- [ ] **Step 4: Implement one app module**

Use built-in Textual widgets only:

- Header or Static for status;
- VerticalScroll for transcript;
- Markdown for Assistant content;
- Static for User and Tool cards;
- TextArea for multiline composer;
- ModalScreen for Approval;
- Button for onboarding and approval decisions.

Initial layout:

~~~python
class MiniClawApp(App[int]):
    """运行 MiniClaw 唯一的本地全屏对话界面。"""

    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; }
    #transcript { height: 1fr; padding: 0 1; }
    #composer { height: 5; border: round $accent; }
    .role { text-style: bold; }
    .tool-card { border: round $surface-lighten-2; margin: 1 0; }
    """

    BINDINGS = [
        Binding("escape", "cancel_turn", "Cancel", show=True),
        Binding("ctrl+o", "toggle_tools", "Tool details", show=True),
        Binding("ctrl+d", "exit_if_idle", "Exit", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield VerticalScroll(id="transcript")
        yield TextArea(id="composer")
        yield Footer()
~~~

Keep all first-version widgets in app.py. Split widgets.py only if app.py exceeds Ruff/readability limits because two independent widgets need substantial logic; do not pre-split.

- [ ] **Step 5: Add terminal-safe rendering at the single boundary**

Before any untrusted text reaches Markdown or Static, remove ESC, C0 and C1 controls except newline and tab:

~~~python
def _terminal_safe(value: str) -> str:
    """移除可改变终端状态的控制字符。"""
    return "".join(
        character
        for character in value
        if character in "\n\t"
        or ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F
    )
~~~

Test ANSI title changes, cursor movement, OSC hyperlinks, C0/C1, normal Chinese, Markdown, newline and tab.

- [ ] **Step 6: Run GREEN and commit**

Run:

~~~bash
uv run python -m unittest tests.test_tui -v
uv run ruff check src/miniclaw/tui tests/test_tui.py
git diff --check
~~~

Commit:

~~~text
feat(tui): 建立 Textual full-screen shell
~~~

---

### Task 4: Wire Chat, Streaming, Tool Cards, and Cancellation

**Files:**

- Modify: src/miniclaw/tui/app.py
- Modify: tests/test_tui.py
- Modify: tests/test_run_events.py

**Behavior:**

- Enter sends non-empty content.
- Shift+Enter inserts a newline.
- One exclusive Textual Worker executes TurnService.handle().
- model_text_delta updates one temporary Assistant card.
- final completion converts the temporary card to one final answer.
- Tool events update one ToolCard by call_id.
- Esc/Control+C cancels the active Worker and restores input.

- [ ] **Step 1: Write RED tests TUI-002 through TUI-005**

Tests:

1. fake deltas “你” + “好” render “你好” in one temporary card and one final card;
2. a model round that later requests Tool collapses its temporary text and does not persist it as final Assistant output;
3. Tool card moves requested → running → succeeded and shows bounded safe preview;
4. Tool failure shows stable code, not traceback/private text;
5. Esc cancels Turn, database states become cancelled/interrupted, composer is enabled and focused;
6. a second Enter while active does not create a second Turn;
7. long output remains bounded and transcript scrolls to the newest card;
8. Shift+Enter changes composer text without calling TurnService.

Do not assert full-screen snapshots. Assert Widget IDs, text, state classes and database results.

- [ ] **Step 2: Run RED**

Run:

~~~bash
uv run python -m unittest tests.test_tui -v
~~~

Expected: send/action/event handlers missing.

- [ ] **Step 3: Run TurnService in one exclusive Worker**

Minimal shape:

~~~python
def _submit(self, text: str) -> None:
    if self._active_worker is not None or not text.strip():
        return
    self._active_worker = self.run_worker(
        self._run_turn(text),
        name="active-turn",
        group="turn",
        exclusive=True,
    )


async def _run_turn(self, text: str) -> None:
    self._lock_composer()
    try:
        result = await self.runtime.service.handle(
            self.runtime.owner_id,
            text,
            self.session_id,
            on_event=self._on_run_event,
        )
        await self._finish_result(result)
    finally:
        self._active_worker = None
        self._unlock_composer()
~~~

Use Textual Worker cancellation; do not create a second asyncio task around it.

- [ ] **Step 4: Render events by stable IDs**

- One temporary Assistant widget ID per active Turn.
- One ToolCard per tool_call_id.
- tool_requested creates it.
- tool_started changes label/class to running.
- tool_finished changes label/class to terminal and stores bounded preview.
- Ctrl+O toggles details for the focused/latest ToolCard.
- turn_finished replaces/finalizes temporary Assistant.
- turn_failed/cancelled removes incomplete temporary content and appends a safe status card.

Do not query SQLite on every delta.

- [ ] **Step 5: Verify cancellation and GREEN**

Run:

~~~bash
uv run python -m unittest tests.test_tui tests.test_run_events tests.test_turn tests.test_tool_executor -v
uv run ruff check src/miniclaw/tui/app.py tests/test_tui.py
git diff --check
~~~

- [ ] **Step 6: Commit**

Commit:

~~~text
feat(tui): 接通 streaming Tool cards 与 cancel
~~~

---

### Task 5: Add Approval Modal and the Five Slash Commands

**Files:**

- Modify: src/miniclaw/tui/app.py
- Modify: tests/test_tui.py
- Modify: tests/test_turn.py
- Modify: tests/test_approvals.py

**Behavior:**

- waiting_approval opens a modal only after SQLite commit.
- Modal shows Tool name, exact normalized target/parameters, risk, expiry.
- Transcript keeps only redacted summary.
- Allow once and Deny call the existing continuation API.
- No permanent rule, no --always, no human approvals CLI.
- Slash commands: /help, /status, /tools, /new, /exit and /quit.

- [ ] **Step 1: Write RED tests TUI-006 and TUI-007**

Allow test:

~~~python
async def test_allow_once_executes_bound_action_only_once(self) -> None:
    app = MiniClawApp(self.paths, runtime=self.runtime)

    async with app.run_test() as pilot:
        await self.send(pilot, "创建 note.txt")
        modal = app.screen
        self.assertIsInstance(modal, ApprovalModal)
        self.assertIn("write_file", modal.query_one("#approval-tool").render())
        self.assertIn("note.txt", modal.query_one("#approval-arguments").render())
        await pilot.click("#allow-once")
        await pilot.pause()

    self.assertEqual((self.workspace / "note.txt").read_text(), "approved")
    self.assertEqual(self.tool.executions, 1)
~~~

Also assert:

- modal opens after Approval row exists;
- changed Tool name/arguments/hash fails and writes nothing;
- expired Approval cannot be allowed;
- repeated double-click executes once;
- Deny never runs Tool and continuation lets model produce a final response;
- closing/crashing modal leaves Approval pending, never approved;
- exact arguments are visible only in modal, not audit or transcript;
- keyboard focus starts on Deny or a neutral element, not Allow once;
- Tab/Shift+Tab and Enter operate both choices; status does not rely on color.

- [ ] **Step 2: Run RED**

Run:

~~~bash
uv run python -m unittest tests.test_tui tests.test_approvals tests.test_turn -v
~~~

- [ ] **Step 3: Implement ApprovalModal as a thin view**

Modal receives a StoredApproval already owner-checked by Approval Service. It does not mutate Repository directly:

~~~python
class ApprovalModal(ModalScreen[bool]):
    """展示一次参数绑定审批并返回 Allow once 或 Deny。"""

    def compose(self) -> ComposeResult:
        yield Static("Approval required", id="approval-title")
        yield Static(_terminal_safe(self.approval.tool_name), id="approval-tool")
        yield Static(_terminal_safe(self.approval.display_arguments), id="approval-arguments")
        yield Static(_terminal_safe(self.approval.risk_summary), id="approval-risk")
        yield Static(self.approval.expires_at.isoformat(), id="approval-expiry")
        yield Button("Deny", id="deny", variant="error")
        yield Button("Allow once", id="allow-once", variant="warning")
~~~

The App receives bool, then calls:

~~~python
await self.runtime.service.continue_approval(
    self.runtime.owner_id,
    approval_id,
    approved=allowed,
    on_event=self._on_run_event,
)
~~~

No callback may execute Tool directly.

- [ ] **Step 4: Add only the approved Slash Commands**

Parse only when stripped input starts with /:

- /help — append static help card;
- /status — model, session, workspace, active/idle;
- /tools — runtime.tool_definitions name + risk;
- /new — set session_id to a local UUID, clear only visible transcript, keep SQLite history;
- /exit and /quit — exit only when no active Turn/Modal.

Unknown command produces a local safe message and never contacts Provider.

Do not implement command registry/plugin/auto-completion framework. A small match statement is enough:

~~~python
match command:
    case "/help":
        self._show_help()
    case "/status":
        self._show_status()
    case "/tools":
        self._show_tools()
    case "/new":
        self._new_session()
    case "/exit" | "/quit":
        self.exit(0)
    case _:
        self._append_status(f"Unknown command: {command}")
~~~

- [ ] **Step 5: Run GREEN and commit**

Run:

~~~bash
uv run python -m unittest tests.test_tui tests.test_approvals tests.test_turn -v
uv run ruff check src/miniclaw/tui/app.py tests/test_tui.py
git diff --check
~~~

Commit:

~~~text
feat(tui): 完成 bound Approval 与 slash commands
~~~

---

### Task 6: Make Bare miniclaw the Only Human Chat Entry

**Files:**

- Modify: src/miniclaw/cli.py
- Modify: tests/test_cli.py
- Delete: tests/test_cli_chat.py
- Modify: tests/test_runtime.py
- Modify: tests/test_tui.py

**CLI Contract:**

~~~text
uv run miniclaw              -> Textual TUI
uv run miniclaw init         -> initialize only
uv run miniclaw doctor       -> diagnostics only
uv run miniclaw eval list|validate|run -> regression only
uv run miniclaw --version    -> version only
~~~

Rejected:

~~~text
miniclaw chat
miniclaw tui
miniclaw chat --message "TEXT"
miniclaw --plain
~~~

- [ ] **Step 1: Write RED single-entry and non-TTY tests**

Tests patch run_tui rather than opening a real terminal:

~~~python
def test_bare_command_starts_the_only_tui(self) -> None:
    with (
        mock.patch.object(sys.stdin, "isatty", return_value=True),
        mock.patch.object(sys.stdout, "isatty", return_value=True),
        mock.patch.dict(os.environ, {"TERM": "xterm-256color"}),
        mock.patch("miniclaw.cli.run_tui", return_value=0) as run_tui,
    ):
        self.assertEqual(main([]), 0)

    run_tui.assert_called_once()
~~~

Also assert:

- stdin non-TTY returns 2 with concise PTY guidance;
- stdout non-TTY returns 2;
- TERM missing or dumb returns 2;
- parser help does not list chat/tui/plain;
- main(["chat"]) and main(["tui"]) raise argparse SystemExit(2);
- init/doctor/eval do not call run_tui/open_runtime;
- no hidden fallback calls input().

- [ ] **Step 2: Run RED**

Run:

~~~bash
uv run python -m unittest tests.test_cli -v
~~~

Expected: bare command still prints help and chat still exists.

- [ ] **Step 3: Remove the old chat parser and REPL**

Delete from cli.py:

- chat subparser;
- --session and --message chat options;
- _run_chat;
- _chat;
- _interactive_chat;
- chat-only error mapping/imports.

Bare command:

~~~python
if arguments.command is None:
    if (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or os.environ.get("TERM", "").casefold() in {"", "dumb"}
    ):
        print(
            "error: MiniClaw requires an interactive terminal; allocate a PTY",
            file=sys.stderr,
        )
        return 2
    return run_tui(build_state_paths(resolve_home(None)))
~~~

Do not catch Textual internals broadly. run_tui maps only known config/state/provider errors to existing stable exit categories and always restores the terminal through Textual lifecycle.

- [ ] **Step 4: Migrate useful legacy chat tests before deleting the file**

Move:

- real SSE Provider request/response assertions → tests/test_runtime.py;
- Turn/Message/ToolRun persistence assertions → tests/test_turn.py;
- interactive input/exit tests → tests/test_tui.py;
- API key missing and initialization behavior → tests/test_tui.py.

Delete assertions tied only to:

- --message;
- line prompt text;
- print-based one-shot output;
- non-TTY one-shot chat.

Then delete tests/test_cli_chat.py.

- [ ] **Step 5: Add same-app onboarding**

When state is missing, MiniClawApp shows Onboarding in the same App:

- resolved state path;
- what files will be created;
- Initialize and Exit buttons;
- Initialize calls existing initialize_state(paths);
- success replaces Onboarding with the normal chat screen without launching a second process/app;
- failure shows a safe error and allows retry/exit;
- if the model key is still missing, show the exact environment variable name and setup guidance, never its value.

Tests assert initialization creates the normal files, does not follow unsafe symlinks, stays in one App instance and focuses composer after success.

- [ ] **Step 6: Run GREEN and commit**

Run:

~~~bash
uv run python -m unittest tests.test_cli tests.test_tui tests.test_runtime tests.test_turn -v
uv run ruff check src/miniclaw/cli.py src/miniclaw/tui src/miniclaw/runtime.py tests/test_cli.py tests/test_tui.py tests/test_runtime.py
git diff --check
~~~

Commit:

~~~text
feat(cli): 裸 miniclaw 统一进入 single TUI
~~~

---

### Task 7: Regression Gate, Documentation, and Release Evidence

**Files:**

- Modify: README.md
- Modify: docs/getting-started/20260807_本地运行指南.md
- Modify: docs/architecture/20260807_系统架构.md
- Create: docs/engineering/phase-2/textual-tui.md
- Modify: docs/engineering/README.md
- Modify: docs/README.md
- Modify: docs/progress/index.html
- Modify: docs/superpowers/specs/2026-08-08-gemini-style-tui-and-lark-cli-design.md
- Modify: eval release record selected by the repository benchmark policy

- [ ] **Step 1: Run the focused product acceptance matrix**

Map the design cases:

- TUI-001 startup/focus;
- TUI-002 streaming plain answer;
- TUI-003 Tool lifecycle;
- TUI-004 Tool failure redaction;
- TUI-005 cancellation;
- TUI-006 approval allow once;
- TUI-007 approval deny;
- TUI-008 80×24 layout;
- TUI-009 single entry;
- TUI-010 onboarding;
- TUI-011 non-TTY rejection;
- TUI-012 keyboard/text-label accessibility.

Run:

~~~bash
uv run python -m unittest tests.test_tui tests.test_cli tests.test_runtime tests.test_run_events -v
~~~

Expected: every named case maps to at least one passing assertion.

- [ ] **Step 2: Run Agent regression suites**

Run:

~~~bash
uv run miniclaw eval validate --root evals/scenarios
uv run miniclaw eval run --suite offline --root evals/scenarios
~~~

Record:

- suite version;
- total/passed/failed;
- safety failures;
- duration;
- commit SHA;
- Textual version.

Do not add a live model or live lark-cli call to the default gate.

- [ ] **Step 3: Run full repository gates**

Run:

~~~bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
git diff --check
git status --short
~~~

Then scan for removed alternate chat entry outside historical planning records:

~~~bash
rg -n "miniclaw (chat|tui)|chat --message|--plain|input\\(" README.md docs/getting-started docs/engineering src/miniclaw tests
~~~

Expected: no production/user-guide alternate chat entry and no input() REPL.

- [ ] **Step 4: Write engineering documentation from verified behavior**

textual-tui.md must explain in plain language:

1. why argparse remains and Textual was added;
2. one-entry command map;
3. Widget tree;
4. RunEvent sequence;
5. temporary streaming paragraph semantics;
6. Tool card state mapping;
7. persisted Approval and child Turn flow;
8. cancellation;
9. onboarding;
10. non-TTY behavior;
11. test commands and latest counts;
12. known limits: no screen-reader guarantee, no second plain UI, one active Turn, no permanent approval.

Include these diagrams:

~~~mermaid
flowchart LR
    CLI["bare miniclaw"] --> TUI["Textual MiniClawApp"]
    TUI --> RUNTIME["AgentRuntime"]
    RUNTIME --> TURN["TurnService"]
    TURN --> RUNNER["AgentRunner"]
    RUNNER --> EXECUTOR["ToolExecutor"]
    EXECUTOR --> POLICY["Policy / Approval"]
    POLICY --> DB["SQLite"]
~~~

~~~mermaid
sequenceDiagram
    actor U as User
    participant T as TUI
    participant A as Agent Core
    participant D as SQLite
    U->>T: request side effect
    T->>A: handle
    A->>D: pending Approval + waiting Turn
    A-->>T: approval_required
    T-->>U: Allow once / Deny
    U->>T: Allow once
    T->>A: continue_approval
    A->>D: atomic consume + child Turn
    A-->>T: final answer
~~~

- [ ] **Step 5: Update user-facing docs and progress**

Only after all gates pass:

- README quick start uses uv run miniclaw for chat;
- local guide removes chat --message and line REPL examples;
- architecture shows TUI as Channel/view over one Runtime;
- progress HTML marks P2.2B complete with exact test/eval counts and commit;
- design status becomes Implemented and links to the engineering document;
- docs indexes link the new document.

- [ ] **Step 6: Manual smoke in a disposable home**

Use a temporary state directory and fake/local Provider. Verify:

- terminal enters and exits cleanly;
- Chinese input and Markdown render;
- resize to 80×24 remains usable;
- Esc cancellation restores input;
- Tool card updates;
- Allow once and Deny both work;
- terminal title, cursor and echo state are restored after exit.

Do not read the real ~/.miniclaw or execute a real external side effect for this smoke.

- [ ] **Step 7: Final commit**

Commit:

~~~text
docs(tui): 同步 single-entry 使用与 regression evidence
~~~

---

## Final Acceptance Checklist

- [ ] Bare uv run miniclaw enters the Textual TUI in a supported TTY.
- [ ] miniclaw chat, miniclaw tui, --plain and chat --message do not exist.
- [ ] init, doctor, eval and --version remain non-chat commands.
- [ ] Missing local state is initialized inside the same App.
- [ ] Plain model answers visibly stream and persist once.
- [ ] Tool requested/running/terminal states are visible and text-labelled.
- [ ] Untrusted ANSI/control characters cannot alter the terminal.
- [ ] Esc cancels active model/Tool work and leaves no running database records.
- [ ] Approval exact arguments are visible in the modal, while transcript/Audit remain redacted.
- [ ] Allow once executes exactly one bound action; Deny executes none.
- [ ] Approval survives restart and never depends on a suspended coroutine.
- [ ] Only one active Turn is possible.
- [ ] 80×24, keyboard focus and non-color status tests pass.
- [ ] Default offline regression suite passes with recorded evidence.
- [ ] Full unittest, Ruff and diff checks pass.
- [ ] README, architecture, engineering docs, local guide and progress HTML match verified behavior.

## Explicitly Deferred

- local !command shortcut;
- second plain/one-shot chat protocol;
- permanent approval rules and --always;
- model/theme/MCP selectors;
- queueing or parallel Turns;
- Gateway/remote TUI;
- TypeScript/pi-tui/OpenTUI frontend;
- snapshot/golden terminal screenshots;
- live lark-cli or real Feishu mutation in default CI.

These are added only after a concrete user need or measured limitation. P2.3 run_command and lark-cli must reuse the Runtime, RunEvent and Approval boundaries delivered here.
