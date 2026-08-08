# MiniClaw Python Core + pi-tui Implementation Plan

> 状态：实现完成，等待最终 main 合并与发布证据固化。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 保留 Python Agent Core，并以版本化 NDJSON Bridge 接入默认 TypeScript pi-tui，完整交付稳定流式对话、Codex 风格活动流、多行输入、分级审批和回归门禁。

**Architecture:** `miniclaw` Python launcher 启动 Node pi-tui，Node 再以 argv 启动 `python -m miniclaw.bridge`。Bridge 是唯一跨语言边界，把现有 `TurnService` 请求和 `RunEvent` 转换为协议 v1；Textual 仅保留迁移期 fallback。

**Tech Stack:** Python 3.12、SQLite、unittest、Ruff、Node.js 22.19+、TypeScript 5、pnpm、`@earendil-works/pi-tui 0.84.1`、Node test runner。

## Global Constraints

- 唯一人类入口是裸命令 `miniclaw`；`init/doctor/eval/--version` 继续直接由 Python 执行。
- `MINICLAW_TUI=pi|textual|auto`；默认 `auto` 优先 pi，显式 `pi` 不得静默 fallback。
- 协议固定 `v:1`、UTF-8 NDJSON、2 MiB 单帧上限；stdout 只承载协议。
- Node UI 不能读取 SQLite、模型 Key 或直接执行 Tool；所有授权以 Python Core 为准。
- Provider reasoning 只展示显式 `reasoning_content`，默认展开并跟随用户语言。
- 所有新行为先写一个能捕获真实回归的失败测试，再写最小实现。
- Textual 修复与新 TUI 使用独立提交；最终全量门禁后合并并推送 `main`。

---

### Task 1: Finish Current Provider and Textual Regressions

**Files:**
- Modify: `src/miniclaw/storage/conversations.py`
- Modify: `src/miniclaw/agent/context.py`
- Modify: `src/miniclaw/tui/app.py`
- Modify: `tests/test_conversations.py`
- Modify: `tests/test_context.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Produces: `MessageRepository.list_recent()` returning a history beginning at a User boundary.
- Produces: one stable Textual `Static` Assistant body per Turn and conditional follow-scroll.

- [x] **Step 1: Preserve the observed RED evidence**

Run the parent/child approval history test and Textual selection tests against the pre-fix revision; record the expected orphan `tool` prefix, Widget replacement, and `scroll_y` jump in the test docstrings.

- [x] **Step 2: Run the focused regression suite**

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_conversations tests.test_context \
  tests.test_tui.TuiShellTest.test_model_deltas_update_one_temporary_message_then_finalize_it \
  tests.test_tui.TuiShellTest.test_stream_event_keeps_scroll_position_when_user_left_bottom -v
```

Expected: GREEN only after the current root-cause changes are present.

- [x] **Step 3: Remove stale test imports and add the missing bottom-follow assertion**

Keep one test for manual scroll preservation and one for active bottom following. Assert observable `scroll_y`, Assistant content, and component identity rather than Textual private children.

- [x] **Step 4: Run all Python TUI/history tests and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversations tests.test_context tests.test_tui -v
git add src/miniclaw/storage/conversations.py src/miniclaw/agent/context.py src/miniclaw/tui/app.py tests/test_conversations.py tests/test_context.py tests/test_tui.py
git commit -m "fix(tui): 修复审批历史与流式选区丢失"
```

### Task 2: Implement Protocol v1 Codec

**Files:**
- Create: `src/miniclaw/bridge/__init__.py`
- Create: `src/miniclaw/bridge/protocol.py`
- Create: `tests/test_bridge_protocol.py`

**Interfaces:**
- Produces: `decode_request(line: bytes) -> BridgeRequest`.
- Produces: `encode_frame(frame: BridgeFrame) -> bytes`.
- Produces: `ProtocolError(code: str, message: str)` with stable safe errors.

- [x] **Step 1: Write RED codec tests**

Test a literal valid `turn.start` frame plus wrong version, invalid UTF-8, non-object payload, unknown type, missing id and 2 MiB overflow. Assert stable codes, never JSON parser internals.

- [x] **Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_bridge_protocol -v
```

Expected: import failure because `miniclaw.bridge.protocol` does not exist.

- [x] **Step 3: Implement typed codec and run GREEN**

Use frozen slotted dataclasses and stdlib `json`; call `json.dumps(..., allow_nan=False, ensure_ascii=False, separators=(",", ":"))`. Validate allowed request types and exact scalar limits before returning `BridgeRequest`.

- [x] **Step 4: Commit protocol codec**

```bash
git add src/miniclaw/bridge tests/test_bridge_protocol.py
git commit -m "feat(bridge): 定义 versioned NDJSON protocol v1"
```

### Task 3: Implement the Python Bridge Process

**Files:**
- Create: `src/miniclaw/bridge/__main__.py`
- Create: `src/miniclaw/bridge/server.py`
- Create: `tests/test_bridge_server.py`

**Interfaces:**
- Produces: `BridgeServer(runtime, reader, writer).run() -> int`.
- Consumes: `AgentRuntime.service.handle()` and `continue_approval()`.
- Produces: `event.<RunEvent.kind>` frames and stable `response.ok/error`.

- [x] **Step 1: Write RED server tests with real byte streams**

Feed `client.hello`, `turn.start`, `turn.cancel`, `approval.resolve`, `session.new`, malformed frame and EOF through in-memory async streams. Use a fake TurnService but assert decoded NDJSON lines and event order, not fake call counts alone.

- [x] **Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_bridge_server -v
```

- [x] **Step 3: Implement one-active-Turn server**

Read stdin using a bounded async line reader, start one task per accepted Turn, map `RunEvent` through an async writer lock, validate pending approval ids, cancel on EOF, and never print traceback to stdout.

- [x] **Step 4: Add a subprocess smoke test**

Launch `python -m miniclaw.bridge --home <temp>` with a controlled initialized state, send `client.hello` and `bridge.shutdown`, and assert every stdout line is valid protocol JSON.

- [x] **Step 5: Run GREEN and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_bridge_protocol tests.test_bridge_server -v
git add src/miniclaw/bridge tests/test_bridge_server.py
git commit -m "feat(bridge): 接通 Python Agent Core stdio server"
```

### Task 4: Add the Single-Entry pi-tui Launcher

**Files:**
- Create: `src/miniclaw/tui_launcher.py`
- Modify: `src/miniclaw/cli.py`
- Modify: `src/miniclaw/doctor.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_doctor.py`

**Interfaces:**
- Produces: `run_default_tui(paths, *, environ, stdin, stdout, stderr) -> int`.
- Consumes: `MINICLAW_TUI=auto|pi|textual`, `MINICLAW_NODE`, `MINICLAW_TUI_ENTRY`.

- [x] **Step 1: Write RED launcher tests**

Assert bare `miniclaw` selects pi with compatible Node and built entry, explicit `textual` skips Node, `auto` falls back with one diagnostic, explicit `pi` exits non-zero, and maintenance commands never inspect Node.

- [x] **Step 2: Verify RED**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_cli tests.test_doctor -v
```

- [x] **Step 3: Implement safe argv launcher and doctor checks**

Resolve Node from `MINICLAW_NODE` or PATH, parse `node --version`, require `22.19.0`, locate `tui/dist/main.js`, and call `subprocess.run([node, entry], shell=False, env=child_env)`. Add doctor rows for Node and pi-tui build without changing init/eval behavior.

- [x] **Step 4: Run GREEN and commit**

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_cli tests.test_doctor -v
git add src/miniclaw/tui_launcher.py src/miniclaw/cli.py src/miniclaw/doctor.py pyproject.toml tests/test_cli.py tests/test_doctor.py
git commit -m "feat(cli): 默认启动 pi-tui 并保留 Textual fallback"
```

### Task 5: Scaffold the TypeScript Protocol Client and State Reducer

**Files:**
- Create: `tui/package.json`
- Create: `tui/tsconfig.json`
- Create: `tui/src/protocol.ts`
- Create: `tui/src/bridge-client.ts`
- Create: `tui/src/state.ts`
- Create: `tui/test/protocol.test.ts`
- Create: `tui/test/state.test.ts`
- Create: `pnpm-lock.yaml`

**Interfaces:**
- Produces: `BridgeClient.startTurn/cancel/resolveApproval/shutdown`.
- Produces: pure `reduceEvent(state: AppState, frame: ServerFrame) -> AppState`.

- [x] **Step 1: Add dependencies and write RED tests**

Pin `@earendil-works/pi-tui` to `0.84.1`, use Node 22.19+, and test fragmented stdout, multiple frames per chunk, malformed frame rejection, one Assistant item across 100 deltas, one Tool activity across lifecycle events, telemetry accumulation and pending approval state.

- [x] **Step 2: Verify RED**

```bash
pnpm --dir tui test
```

- [x] **Step 3: Implement the minimal client and pure reducer**

Spawn `[MINICLAW_PYTHON, "-m", "miniclaw.bridge", "--home", MINICLAW_HOME]` with `shell:false`; reserve stdout for frames, buffer by newline, enforce 2 MiB, and publish typed events to the reducer.

- [x] **Step 4: Run GREEN, typecheck and commit**

```bash
pnpm --dir tui test
pnpm --dir tui build
git add tui pnpm-lock.yaml
git commit -m "feat(pi-tui): 建立 Bridge client 与事件状态机"
```

### Task 6: Build the Codex-Style pi-tui Shell

**Files:**
- Create: `tui/src/theme.ts`
- Create: `tui/src/components/activity.ts`
- Create: `tui/src/components/conversation.ts`
- Create: `tui/src/components/status.ts`
- Create: `tui/src/components/approval.ts`
- Create: `tui/src/app.ts`
- Create: `tui/src/main.ts`
- Create: `tui/test/render.test.ts`
- Create: `tui/test/input.test.ts`
- Create: `tui/test/approval.test.ts`

**Interfaces:**
- Produces: `MiniClawTui` accepting an injected `TUI`, `BridgePort`, language and initial metadata.
- Consumes: pi-tui `TuiAltScreen`, `ScrollView`, `Editor`, `Markdown`, `VirtualTerminal`, Overlay API.

- [x] **Step 1: Write RED VirtualTerminal snapshots**

Render literal Chinese fixtures at 80×24 and 120×36. Assert no rendered row exceeds viewport width; roles, reasoning, tool status, telemetry and input hints occupy the expected order without large bordered cards.

- [x] **Step 2: Write RED interaction tests**

Cover Enter submit, Shift/Alt+Enter newline, 250,000-character bracketed paste, failed-turn exact draft restore, 100 streaming deltas, manual-scroll preservation, bottom-follow recovery, `/copy`, `/lang`, `/trace`, Ctrl+O and Esc cancel.

- [x] **Step 3: Implement the compact shell**

Use an Alt Screen root `VStack` containing header, primary `ScrollView`, telemetry and Editor. Keep conversation/activity component instances stable; update their fields and call `requestRender()` instead of rebuilding the transcript.

- [x] **Step 4: Write RED approval tests and implement Overlay**

Assert only Core-supplied grant modes render, `deny/once/session/always` map exactly, Editor submit stays disabled while pending, and continuation events append to the same timeline.

- [x] **Step 5: Run GREEN, build and commit**

```bash
pnpm --dir tui test
pnpm --dir tui build
git add tui
git commit -m "feat(pi-tui): 实现紧凑活动流、多行输入与审批"
```

### Task 7: End-to-End Bridge and Terminal Regression

**Files:**
- Create: `tests/test_pi_tui_integration.py`
- Create: `tui/test/fixtures/fake-bridge.ts`
- Modify: `evals/scenarios/phase2.v1.jsonl`

**Interfaces:**
- Verifies the real Python launcher, Node process and fake offline Core as one system.

- [x] **Step 1: Write the failing cross-process smoke test**

Launch the built TUI with a virtual terminal/fake Bridge, submit Chinese text, stream reasoning/tool/final events, resolve approval and exit. Assert process code, ordered transcript and telemetry snapshot.

- [x] **Step 2: Add long-input and selection regression fixtures**

Use deterministic 250,000-character input and 100 delta events. Verify exact submitted bytes, one Assistant item, retained manual scroll position and complete copied text.

- [x] **Step 3: Update the Chinese eval expectation and run GREEN**

Replace the obsolete English System Prompt substring in `ACTION-OPEN-APP-001` with the stable Chinese tool-use safety phrase, then run the Python integration, TypeScript suite and 21 offline cases.

- [x] **Step 4: Commit integration gate**

```bash
git add tests/test_pi_tui_integration.py tui/test/fixtures evals/scenarios/phase2.v1.jsonl
git commit -m "test(tui): 增加跨进程与长文本回归门禁"
```

### Task 8: Synchronize Engineering Documentation and Progress

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/architecture/20260808_TUI稳定化与桌面版架构设计.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/engineering/phase-2/single-entry-tui.md`
- Modify: `docs/engineering/phase-2/testing-and-debugging.md`
- Modify: `docs/engineering/phase-2/tui-regression-testing.md`
- Create: `docs/engineering/phase-2/python-core-pi-tui-bridge.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Modify: `docs/product/20260807_产品需求文档.md`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Documents only behavior proven by the final test/build output.

- [x] **Step 1: Update architecture and replacement decision**

Replace the earlier “不引入 pi-tui” decision with the approved Python Core + TypeScript pi-tui boundary, protocol diagram, fallback lifecycle and Node requirement.

- [x] **Step 2: Write the module engineering guide**

Document files, frame examples, event mapping, security model, debugging commands, failure recovery, terminal matrix and extension procedure in plain Chinese.

- [x] **Step 3: Update setup, testing and progress pages**

Record exact dependency install/build/run commands and fresh Python/TypeScript/eval counts. Mark Textual as fallback, not deleted.

- [x] **Step 4: Validate docs and commit**

Check Markdown fences, Mermaid syntax, relative links, HTML close tags and stale Textual-default wording before committing.

### Task 9: Full Verification, Review, Merge and Main Sync

**Files:**
- Review: all branch changes against this plan and design spec.

**Interfaces:**
- Produces: green `main`, pushed `origin/main`, and local main at the same commit while preserving user dirty files.

- [x] **Step 1: Run fresh full gates**

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
pnpm --dir tui test
pnpm --dir tui build
uv run miniclaw eval validate
uv run miniclaw eval run --offline
uv build
git diff --check
```

- [x] **Step 2: Run real offline smoke checks**

Use a temporary MiniClaw home to validate Bridge hello/shutdown, explicit Textual fallback, pi launcher with controlled Node path, and built TUI startup/exit. Do not call a real model or飞书。

- [x] **Step 3: Self-review requirements and security**

Confirm every spec invariant has implementation/test evidence; scan for secrets, `shell:true`, raw Provider errors, unbounded protocol reads, stdout logs, stale test counts and generated junk.

- [ ] **Step 4: Integrate the explicitly authorized result**

Fetch without force, merge latest `origin/main` into the feature branch if it moved, rerun affected gates, merge/push `main`, and verify `origin/main` resolves to the delivered commit.

- [ ] **Step 5: Synchronize the original local main safely**

Inspect its dirty paths, back them up to a temporary directory, use a targeted recoverable stash, fast-forward to `origin/main`, reapply the stash, and verify both commit equality and preservation of every pre-existing local modification.
