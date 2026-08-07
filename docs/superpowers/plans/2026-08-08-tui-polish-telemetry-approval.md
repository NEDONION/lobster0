# MiniClaw TUI Polish, Telemetry, and Scoped Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付默认中文、可切换英文的紧凑 TUI，明确区分用户与 Agent，弱化 Provider reasoning，可靠恢复失败草稿，显示真实 Token/上下文/Tool 审计，并支持安全受限的 Allow this session / Always allow。

**Architecture:** 继续使用唯一 `RunEvent → MiniClawApp` 投影，不新增遥测总线。AgentRunner 在每次 Provider 响应后发布真实 usage，TUI 只渲染；Approval 继续消费原有参数绑定记录，成功执行后才把精确 scope 加入当前 PolicyEngine 或既有 `policy_rules`。

**Tech Stack:** Python 3.12+、Textual 8.2.x、Rich（Textual 已安装依赖）、SQLite、argparse、unittest、Ruff；不新增直接依赖。

> **完成记录（2026-08-08）：** Task 0–5 已执行。功能提交为 `0eb6052`、`a8db692`、`e9fa616`、
> `a26efe3`；新鲜门禁为 270/270 tests、20/20 offline Agent cases、Ruff PASS。下方 checkbox 保留原始
> TDD 执行脚本，不作为当前进度来源；当前事实以本完成记录、工程文档与 `docs/progress/index.html` 为准。

## Global Constraints

- 唯一人类对话入口仍是 `uv run miniclaw`；不恢复 `chat`、`tui` 或 plain REPL。
- 默认 UI 语言为 `zh-CN`，只允许 `zh-CN` 和 `en`。
- Reasoning 内容跟随当前用户消息语言，不跟 UI language，也不二次翻译。
- Token 指标只使用 Provider usage；缺失必须显示 `N/A`，不能估算成 0。
- `context_tokens` 是最后一次 Provider 请求的 prompt token；`input/output` 是当前 Turn 累计值。
- `Always allow` 不能放行整个 `run_command`、executable、Shell 或 inline AppleScript。
- Session/Always scope 只能在绑定调用成功后生效；所有硬 DENY 始终优先。
- Session scope 只在当前 AgentRuntime 内存中存在；Always scope 使用既有 `policy_rules` 与脱敏 Audit。
- Approval 数据库状态仍为 pending → approved/denied → consumed，不新增状态。
- 所有不可信文本仍经过 `_terminal_safe`，UI 不显示原始 Prompt、密钥或未脱敏审计参数。
- 每个生产代码切片都先运行一个因缺少行为而失败的测试，再写最小 GREEN。
- 用户已明确授权最终合并并推送 `main`；实现仍在隔离 worktree 完成，门禁后再合并。

---

### Task 0: Create the Isolated Implementation Worktree

**Files:**
- Reference: `docs/superpowers/specs/2026-08-08-tui-polish-telemetry-approval-design.md`
- Reference: this plan

**Interfaces:**
- Consumes: clean `main` containing design and plan commits.
- Produces: feature branch `feat/tui-polish-telemetry` in an isolated worktree.

- [ ] **Step 1: Verify the main baseline**

Run:

```bash
git status --short --branch
git log -3 --oneline
```

Expected: no uncommitted tracked files; design and plan are the newest commits.

- [ ] **Step 2: Create the worktree with the worktree skill**

Use `superpowers:using-git-worktrees`, then create `feat/tui-polish-telemetry` from current `main` in the repository's established worktree location.

- [ ] **Step 3: Verify the baseline suite in the worktree**

Run:

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
```

Expected: 253 tests pass and Ruff exits 0 before feature edits.

---

### Task 1: Add the Default-Chinese UI Configuration Contract

**Files:**
- Modify: `src/miniclaw/config.py`
- Modify: `src/miniclaw/bootstrap.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Produces: `UIConfig(language: str = "zh-CN")`.
- Produces: `AppConfig.ui: UIConfig`.
- Produces: `AgentRuntime.ui_language: str` and `AgentRuntime.context_budget_tokens: int`.
- Consumes later: `MiniClawApp` reads both Runtime fields; no global environment lookup in the Widget layer.

- [ ] **Step 1: Write RED config and bootstrap tests**

Add tests that assert:

```python
self.assertEqual(load_config(self.paths, environ={}).ui.language, "zh-CN")
self.paths.config.write_text('[ui]\nlanguage = "en"\n', encoding="utf-8")
self.assertEqual(load_config(self.paths, environ={}).ui.language, "en")
```

Also assert `[ui] language = "zh-CN"` exists in a newly initialized config and that `language = "fr"` plus unknown `[ui]` keys raise `ConfigError`.

- [ ] **Step 2: Run RED**

Run:

```bash
uv run python -m unittest tests.test_config tests.test_bootstrap -v
```

Expected: FAIL because `AppConfig` has no `ui` and `[ui]` is rejected.

- [ ] **Step 3: Implement the minimal config type**

In `config.py` add:

```python
@dataclass(frozen=True, slots=True)
class UIConfig:
    language: str = "zh-CN"
```

Allow only top-level `ui`, only key `language`, validate with `_enum_string(..., frozenset({"zh-CN", "en"}))`, and return it as `AppConfig.ui`. Add the stable default table to `_render_default_config`.

- [ ] **Step 4: Run GREEN for config**

Run the Step 2 command. Expected: all config/bootstrap tests pass.

- [ ] **Step 5: Write RED Runtime propagation test**

Assert a runtime created from `AppConfig` exposes the configured UI language and context budget without reading `config.toml` again.

- [ ] **Step 6: Run RED, implement, and run GREEN**

Run:

```bash
uv run python -m unittest tests.test_runtime -v
```

Expected RED: `AgentRuntime` lacks the fields. Add both dataclass fields and populate them in `create_runtime`; rerun until GREEN.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/miniclaw/config.py src/miniclaw/bootstrap.py src/miniclaw/runtime.py tests/test_config.py tests/test_bootstrap.py tests/test_runtime.py
git commit -m "feat(config): add TUI language and context settings"
```

---

### Task 2: Publish Real Per-Turn Usage Telemetry

**Files:**
- Modify: `src/miniclaw/agent/events.py`
- Modify: `src/miniclaw/agent/runner.py`
- Modify: `src/miniclaw/agent/turn.py`
- Modify: `tests/test_agent_runner.py`
- Modify: `tests/test_turn.py`
- Modify: `tests/test_run_events.py`

**Interfaces:**
- Produces new event kind: `model_usage`.
- `model_usage.data` contains `iteration`, `context_tokens`, `input_tokens`, `output_tokens`, `tool_calls`, and `provider_request_id`.
- Numeric usage fields are `int | None`; `None` means the Provider omitted usage.
- `turn_finished.data` keeps `status` and `content` and adds the final telemetry snapshot.

- [ ] **Step 1: Write RED AgentRunner telemetry tests**

Use a two-response fake Provider: first response requests one Tool with usage 10/2, second returns text with usage 14/3. Assert two `model_usage` events:

```python
[
    {"iteration": 1, "context_tokens": 10, "input_tokens": 10, "output_tokens": 2, "tool_calls": 1},
    {"iteration": 2, "context_tokens": 14, "input_tokens": 24, "output_tokens": 5, "tool_calls": 1},
]
```

Add a second test where one Provider response omits usage and assert `context_tokens`, cumulative `input_tokens`, and cumulative `output_tokens` are `None`, not 0.

- [ ] **Step 2: Run RED**

```bash
uv run python -m unittest tests.test_agent_runner -v
```

Expected: FAIL because no `model_usage` events exist.

- [ ] **Step 3: Emit one usage event per Provider response**

Track reported cumulative usage separately from the existing persisted integer totals. Once any response omits a side of usage, publish that cumulative side as `None`. Count accepted Tool Call IDs, not successful executions. Emit only trusted scalar fields after validating duplicate call IDs.

- [ ] **Step 4: Run GREEN for AgentRunner**

Run Step 2. Expected: all AgentRunner tests pass.

- [ ] **Step 5: Write RED Turn event tests**

Assert a completed Turn's final event includes the last usage snapshot and that waiting-approval paths still expose `model_usage` before the modal appears. Assert `turn_failed` contains only stable error code plus safe elapsed time, never Prompt content.

- [ ] **Step 6: Run RED, implement final event fields, run GREEN**

```bash
uv run python -m unittest tests.test_turn tests.test_run_events -v
```

Measure Turn duration with `time.monotonic()` in `TurnService`; add `duration_ms` to terminal events. Pass the final safe metrics from `AgentRunResult` into `turn_finished`. Expected GREEN: all selected tests pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/miniclaw/agent/events.py src/miniclaw/agent/runner.py src/miniclaw/agent/turn.py tests/test_agent_runner.py tests/test_turn.py tests/test_run_events.py
git commit -m "feat(agent): publish real turn telemetry"
```

---

### Task 3: Add Scoped Session and Persistent Approval Grants

**Files:**
- Modify: `src/miniclaw/policy/approvals.py`
- Modify: `src/miniclaw/policy/command.py`
- Modify: `src/miniclaw/policy/engine.py`
- Modify: `src/miniclaw/storage/tooling.py`
- Modify: `src/miniclaw/tools/executor.py`
- Modify: `src/miniclaw/agent/runner.py`
- Modify: `src/miniclaw/agent/turn.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `src/miniclaw/evals/runner.py`
- Modify: `tests/test_approvals.py`
- Modify: `tests/test_tool_executor.py`
- Modify: `tests/test_turn.py`
- Modify: `tests/test_eval_runner.py`

**Interfaces:**
- Produces `ApprovalDecision(StrEnum)`: `DENY`, `ONCE`, `SESSION`, `ALWAYS`.
- Changes `TurnService.continue_approval(..., decision: ApprovalDecision, ...)`.
- `approval_required.data["grant_modes"]` is a safe ordered list chosen by Core.
- Produces `PolicyEngine.add_session_command(rule)` and `add_session_network(rule)`.
- Extends `ToolExecution` with `succeeded: bool`, set only from the validated `ToolResult.ok` terminal path.
- `ToolExecutor` receives the existing `PolicyRuleRepository`; it applies grants only after `ToolResult.ok`.

- [ ] **Step 1: Write RED scope-eligibility tests**

Assert:

- `http_get` exposes once/session/always;
- exact safe `lark-cli`-style argv exposes once/session/always;
- `/usr/bin/osascript -e ...` exposes once/session but not always;
- write/edit expose only once;
- forbidden commands remain hard denied and never create an Approval.

- [ ] **Step 2: Run RED**

```bash
uv run python -m unittest tests.test_tool_executor -v
```

Expected: FAIL because `grant_modes` is absent and persistability is undefined.

- [ ] **Step 3: Implement Core-owned grant mode calculation**

Add `command_rule_is_persistable(NormalizedCommand) -> bool` that rejects `osascript` inline evaluation and already-forbidden interpreter patterns. Enforce it both when advertising modes and inside `PolicyRuleRepository.add_command_from_approval`, so a direct repository call fails closed. Add a PolicyEngine helper returning ordered modes for normalized `run_command` and `http_get`; generic tools return once only. Put these modes in the Approval event—never infer them in TUI.

- [ ] **Step 4: Run GREEN for eligibility**

Run Step 2. Expected: selected tests pass.

- [ ] **Step 5: Write RED session grant tests**

Approve an exact command with `ApprovalDecision.SESSION`, let it succeed, then execute the same normalized command again in the same PolicyEngine and assert no second Approval. Create a new PolicyEngine and assert the same command requires Approval again. Add a failed-tool case and assert no session rule is installed.

- [ ] **Step 6: Run RED, implement in-memory exact scopes, run GREEN**

Use mutable sets internal to PolicyEngine but keep all authorization through `authorize`. `_execute_started` returns `ToolExecution(model_text, succeeded=result.ok)`; `ToolExecutor` adds the exact scope only after this is true. Run:

```bash
uv run python -m unittest tests.test_tool_executor tests.test_turn -v
```

- [ ] **Step 7: Write RED persistent grant tests**

For safe exact command and exact HTTPS hostname, choose `ALWAYS`, complete successfully, and assert:

- exactly one enabled `policy_rules` row linked to the source Approval;
- one redacted `policy_rule.created` Audit;
- same current runtime stops asking;
- a freshly built runtime loads the rule;
- URL query/path and command output are absent from `rule_json`/Audit;
- osascript `ALWAYS` is rejected without executing a second time;
- failed execution creates no persistent rule.

- [ ] **Step 8: Run RED, wire existing PolicyRuleRepository, run GREEN**

Pass the existing repository into ToolExecutor. Reuse `add_command_from_approval` and `add_network_from_approval`; do not add a second rules table. After persistence succeeds, add the exact returned scope to current PolicyEngine. Run:

```bash
uv run python -m unittest tests.test_approvals tests.test_tool_executor tests.test_turn tests.test_eval_runner -v
```

- [ ] **Step 9: Update eval approval decisions and commit Task 3**

Map existing eval actions `approve` and `deny` to `ONCE` and `DENY`; add no new scenario syntax in this task.

```bash
git add src/miniclaw/policy src/miniclaw/storage/tooling.py src/miniclaw/tools/executor.py src/miniclaw/agent/runner.py src/miniclaw/agent/turn.py src/miniclaw/runtime.py src/miniclaw/evals/runner.py tests/test_approvals.py tests/test_tool_executor.py tests/test_turn.py tests/test_eval_runner.py
git commit -m "feat(approval): add scoped session and persistent grants"
```

---

### Task 4: Polish the TUI and Make Long Drafts Recoverable

**Files:**
- Modify: `src/miniclaw/tui/app.py`
- Modify: `src/miniclaw/agent/context.py`
- Modify: `tests/test_tui.py`
- Modify: `tests/test_context.py`

**Interfaces:**
- Consumes Runtime `ui_language` and `context_budget_tokens`.
- Consumes `model_usage` and terminal RunEvent telemetry.
- Consumes Core-supplied `grant_modes`; modal returns `ApprovalDecision`.
- Produces `/lang zh`, `/lang en`, localized `/status`, and a visible telemetry strip.

- [ ] **Step 1: Write RED role and compact-reasoning tests**

Assert each user message has a visible localized role label and `user-message` container; each Assistant has `MiniClaw` label, an `assistant-message` container, and the same single Markdown child during streaming. Assert Reasoning is collapsed, localized, has a distinct compact class, and still expands with Ctrl+O.

- [ ] **Step 2: Run RED**

```bash
uv run python -m unittest tests.test_tui.MiniClawAppTest.test_model_deltas_update_one_temporary_message_then_finalize_it tests.test_tui.MiniClawAppTest.test_reasoning_and_tool_traces_remain_visible_and_toggle_together -v
```

Expected: FAIL because the Assistant wrapper/labels and compact title are absent.

- [ ] **Step 3: Implement the minimum visual hierarchy**

Reuse `Vertical`, `Static`, `Markdown`, and existing CSS. User and Agent get different border side/background plus explicit text labels. Reasoning removes the Tool-style round border, uses muted/dim text and zero vertical margin, and remains keyboard-focusable. Do not add avatars, theme objects, or another dependency.

- [ ] **Step 4: Run GREEN for role/reasoning tests**

Run Step 2 and the full `tests.test_tui` module.

- [ ] **Step 5: Write RED language and Prompt tests**

Assert default labels/buttons/help are Chinese, `/lang en` immediately changes status, help, approval copy and future message labels, and `/lang zh` switches back. Assert the System Prompt contains the rule: visible reasoning is concise and follows the current user's primary language unless explicitly overridden.

- [ ] **Step 6: Run RED, add two static copy maps, run GREEN**

No gettext and no language detector. Keep Tool names/error codes unchanged. Run:

```bash
uv run python -m unittest tests.test_context tests.test_tui -v
```

- [ ] **Step 7: Write RED telemetry strip tests**

Feed `model_usage` and terminal events and assert the strip shows real CTX percentage, cumulative in/out, model iterations, Tool count, duration, and `N/A` for omitted usage. At size 80x24 assert the compact form fits and the Composer remains visible. Assert `/status` includes Turn ID and Provider Request ID but not Prompt text.

- [ ] **Step 8: Run RED, implement trusted metric rendering, run GREEN**

Use a single `Static` telemetry bar and Rich/Textual styling already installed. Keep one in-memory telemetry record for the active/last Turn. Update it only from typed scalar event fields and locally measured/received duration.

- [ ] **Step 9: Write RED 250k draft recovery tests**

Paste at least 250,000 characters through `events.Paste`, submit, and assert the Service receives the exact string. Add Provider failure and cancellation cases asserting exact text restoration, enabled Composer, and restored focus. Add success asserting Composer stays empty and Runtime-missing asserting input is not discarded.

- [ ] **Step 10: Run RED, fix the submit lifecycle, run GREEN**

Keep the submitted string in the running coroutine. Clear only after it has been copied; on failure/cancel reload the exact string before focusing. Do not persist drafts or introduce a clipboard package.

- [ ] **Step 11: Write RED localized Approval modal tests**

For each Core `grant_modes` set, assert only permitted buttons appear. Deny remains default focus/Esc behavior. Assert session/always decisions reach `TurnService` as enum values and exact full arguments remain visible.

- [ ] **Step 12: Run RED, implement modal decisions, run GREEN**

```bash
uv run python -m unittest tests.test_tui tests.test_context -v
```

- [ ] **Step 13: Commit Task 4**

```bash
git add src/miniclaw/tui/app.py src/miniclaw/agent/context.py tests/test_tui.py tests/test_context.py
git commit -m "feat(tui): polish chat and expose runtime telemetry"
```

---

### Task 5: Update Engineering Documentation and Release Gates

**Files:**
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/engineering/phase-2/single-entry-tui.md`
- Modify: `docs/engineering/phase-2/tui-regression-testing.md`
- Modify: `docs/engineering/phase-2/approval-lifecycle.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/progress/index.html`
- Modify: `docs/superpowers/specs/2026-08-08-tui-polish-telemetry-approval-design.md`

**Interfaces:**
- Consumes only verified behavior and final test counts.
- Produces user-facing usage, metric semantics, approval safety rules, test matrix, and current progress.

- [ ] **Step 1: Update docs with exact implemented behavior**

Document:

- default Chinese plus `[ui].language` and `/lang`;
- compact role/reasoning layout and terminal font limitation;
- telemetry field definitions and `N/A` semantics;
- 250k paste regression and failure/cancel restoration;
- Decision → scope → persistence table;
- why osascript inline code cannot receive Always;
- how to revoke a persistent `policy_rules` entry using the supported repository/config path, without raw SQLite instructions;
- current exact regression count only after the final run.

- [ ] **Step 2: Run documentation integrity checks**

The repository has no permanent docs checker. Run these exact read-only checks:

```bash
uv run python -c "from html.parser import HTMLParser; from pathlib import Path; p=HTMLParser(); p.feed(Path('docs/progress/index.html').read_text(encoding='utf-8')); print('progress html: OK')"
uv run python -c "from pathlib import Path; files=list(Path('docs').rglob('*.md')); bad=[str(p) for p in files if p.read_text(encoding='utf-8').count(chr(96)*3)%2]; assert not bad, bad; print(f'markdown fences: {len(files)} files OK')"
git diff --check main...HEAD
```

- [ ] **Step 3: Run focused security and TUI gates**

```bash
uv run python -m unittest tests.test_approvals tests.test_tool_executor tests.test_turn tests.test_agent_runner tests.test_tui tests.test_context tests.test_config tests.test_runtime -v
```

Expected: all selected tests pass with no warnings or tracebacks.

- [ ] **Step 4: Run the complete release gate**

```bash
uv run python -m unittest discover -s tests -v
uv run miniclaw eval --suite evals/scenarios/phase2.v1.jsonl --offline evals/fixtures/phase2.v1.responses.jsonl
uv run ruff check src tests
git diff --check main...HEAD
python -m build
```

Expected: all unit tests pass; all 20 offline evals pass; Ruff/build/diff check exit 0.

- [ ] **Step 5: Perform a PTY smoke test**

Launch `uv run miniclaw` in a real PTY and verify at 80x24 and a wider terminal:

- default Chinese UI and compact telemetry are visible;
- long multiline paste remains editable;
- user/Agent/Reasoning hierarchy is distinct;
- Approval focus defaults to Deny;
- `/lang en` and `/lang zh` update immediately;
- exit restores the terminal cleanly.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md docs
git commit -m "docs(tui): document telemetry and scoped approvals"
```

- [ ] **Step 7: Finish, merge, and push**

Use `superpowers:finishing-a-development-branch`. Re-run the complete gate on merged `main`, confirm `git status --short --branch` is clean, then:

```bash
git push origin main
```

Report the final commit, exact test/eval counts, and clickable paths to the design, plan, engineering docs, and progress page.
