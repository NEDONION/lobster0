# Feishu Agent Progress Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Feishu's answer-only card with one live, recoverable Agent progress card that shows safe step summaries, Tool activity, viewed targets, status, timing, and the final answer.

**Architecture:** Project `RunEvent` values into a bounded, immutable `AgentProgress` snapshot before any platform renderer sees them. Feishu renders the structured snapshot as a Claw Trail card, while Telegram and Discord derive compact text. Persist only the final redacted trace in existing Assistant Message metadata so completed-turn recovery can rebuild the same card without a schema migration.

**Tech Stack:** Python 3.12, standard-library dataclasses/JSON/urllib, Feishu Card JSON 2.0, SQLite, `unittest`, Ruff.

## Global Constraints

- Never expose `model_reasoning`, hidden chain-of-thought, prompts, raw Tool arguments, raw Tool output, credentials, or sensitive file content.
- Do not make an extra model call for progress titles or summaries.
- Keep a single Feishu answer card for the normal Owner Autopilot path; Approval before the first Tool retains the existing durable Approval card.
- Keep Telegram and Discord behavior compatible through compact text rendering.
- Maximum 16 visible steps, 240 characters per display field, 16 KiB trace metadata, and 20 KiB Feishu card payload.
- Preserve current card fallback, idempotency, restart recovery, and three-platform failure isolation.
- New or modified public functions, methods, classes, attributes, and return values require accurate Python 3.12 type annotations and Chinese docstrings.
- Do not modify or stage unrelated user-owned working-tree files.

---

### Task 1: Build the bounded RunEvent projector

**Files:**
- Create: `src/lobster0/channels/progress.py`
- Create: `tests/test_channel_progress.py`

**Interfaces:**
- Consumes: `RunEvent(kind: RunEventKind, turn_id: int, data: dict[str, JsonValue])`.
- Produces: `ProgressProjector.apply(event: RunEvent) -> bool`, `ProgressProjector.finish(content: str | None, failed: bool) -> AgentProgress`, `progress_to_metadata(progress: AgentProgress) -> dict[str, JsonValue]`, and `progress_from_metadata(value: dict[str, JsonValue], final_answer: str) -> AgentProgress | None`.

- [ ] **Step 1: Write projector tests that name the observable breaks**

Cover a real sequence and literal expectations:

```python
projector = ProgressProjector(clock=clock)
projector.apply(RunEvent("turn_started", 7, {"session_id": 1}))
projector.apply(RunEvent("model_reasoning", 7, {"text": "private chain"}))
projector.apply(RunEvent("model_usage", 7, {"iteration": 1, "input_tokens": 20, "output_tokens": 4, "tool_calls": 1}))
projector.apply(RunEvent("tool_requested", 7, {
    "call_id": "call_1",
    "tool_name": "run_command",
    "summary": "run_command",
    "arguments": {
        "program": "/Users/owner/.local/bin/lark-cli",
        "args": ["drive", "+search", "--token", "secret-value", "--page-size", "100"],
    },
}))
projector.apply(RunEvent("tool_started", 7, {"call_id": "call_1", "tool_name": "run_command"}))
projector.apply(RunEvent("tool_finished", 7, {
    "call_id": "call_1",
    "tool_name": "run_command",
    "status": "succeeded",
    "duration_ms": 428,
    "preview": "private tool output",
}))
progress = projector.finish("你有 327 个飞书文档。", failed=False)

self.assertEqual(progress.status, "completed")
self.assertEqual(progress.summary, "任务已完成")
self.assertEqual(progress.steps[1].title, "查询飞书云空间")
self.assertIn("lark-cli drive +search", progress.steps[1].detail)
self.assertNotIn("secret-value", repr(progress))
self.assertNotIn("private chain", repr(progress))
self.assertNotIn("private tool output", repr(progress))
```

Add table-driven tests for file/search/HTTP/Memory/unknown Tool summaries, 17-step folding, control-character removal, metadata round-trip, malformed metadata, and metadata omitting the final answer.

- [ ] **Step 2: Run the focused projector test and verify RED**

```bash
uv run python -m unittest tests.test_channel_progress -v
```

Expected: import failure for `lobster0.channels.progress`.

- [ ] **Step 3: Implement the minimal typed projector and redaction policy**

Use immutable snapshots and private mutable state:

```python
type ProgressStatus = Literal["running", "completed", "incomplete", "waiting"]
type StepStatus = Literal["pending", "running", "succeeded", "failed", "waiting", "incomplete"]

@dataclass(frozen=True, slots=True)
class ProgressStep:
    """保存一条已经脱敏且可跨平台展示的 Agent 步骤。"""

    index: int
    call_id: str | None
    title: str
    detail: str
    status: StepStatus
    duration_ms: int | None = None

@dataclass(frozen=True, slots=True, repr=False)
class AgentProgress:
    """保存一次 Turn 的有界公开进度快照。"""

    status: ProgressStatus
    summary: str
    steps: tuple[ProgressStep, ...]
    public_text: str
    final_answer: str
    iterations: int
    tool_calls: int
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
```

`model_reasoning` must return `False` without retaining its data. Tool helpers must allowlist fields by exact Tool name; unknown Tools retain no arguments or preview. `run_command` shows the executable basename and up to four non-sensitive business arguments, replacing secret flag values with `[redacted]`.

- [ ] **Step 4: Run projector tests and verify GREEN**

```bash
uv run python -m unittest tests.test_channel_progress -v
```

Expected: all projector tests pass with no warnings.

- [ ] **Step 5: Commit the projector**

```bash
git add src/lobster0/channels/progress.py tests/test_channel_progress.py
git commit -m "feat(channel): 增加 safe Agent progress projector"
```

### Task 2: Render the Feishu Claw Trail card

**Files:**
- Create: `src/lobster0/channels/feishu_cards.py`
- Create: `tests/test_feishu_agent_card.py`

**Interfaces:**
- Consumes: `AgentProgress` only; never consumes `RunEvent` or raw Tool data.
- Produces: `RenderedProgressCard(card: dict[str, JsonValue], visible_answer_chars: int)`, `render_agent_progress_card(progress: AgentProgress) -> RenderedProgressCard`, and `render_compact_progress(progress: AgentProgress) -> str`.

- [ ] **Step 1: Write literal Card JSON behavior tests**

Use a hand-built `AgentProgress` fixture and assert:

```python
rendered_card = render_agent_progress_card(progress)
card = rendered_card.card

self.assertEqual(card["schema"], "2.0")
self.assertEqual(card["header"]["template"], "green")
self.assertEqual(card["header"]["title"]["content"], "Lobster0 · 已完成")
rendered = json.dumps(card, ensure_ascii=False)
self.assertIn("Claw Trail", rendered)
self.assertIn("查询飞书云空间", rendered)
self.assertIn("你有 327 个飞书文档", rendered)
self.assertIn("2 步 · 1 个工具 · 2 轮模型", rendered)
self.assertNotIn("private", rendered)
self.assertLessEqual(len(rendered.encode("utf-8")), 20 * 1024)
self.assertEqual(rendered_card.visible_answer_chars, len("你有 327 个飞书文档。"))
```

Add status cases mapping `running/completed/incomplete/waiting` to `blue/green/red/orange`, an empty-tool final answer, Markdown escaping for backticks, and payload trimming that preserves the final answer before optional detail.

- [ ] **Step 2: Run renderer tests and verify RED**

```bash
uv run python -m unittest tests.test_feishu_agent_card -v
```

Expected: import failure for `lobster0.channels.feishu_cards`.

- [ ] **Step 3: Implement the Feishu and compact renderers**

Build Card JSON from separate Markdown elements:

```python
{
    "schema": "2.0",
    "config": {"wide_screen_mode": True},
    "header": {
        "title": {"tag": "plain_text", "content": title},
        "template": template,
    },
    "body": {
        "elements": [
            {"tag": "markdown", "content": summary, "text_size": "small"},
            {"tag": "hr"},
            {"tag": "markdown", "content": trail, "text_size": "small"},
            {"tag": "hr"},
            {"tag": "markdown", "content": answer, "text_size": "small"},
            {"tag": "markdown", "content": metrics, "text_size": "small"},
        ],
    },
}
```

Omit the answer divider/element while running. Remove optional step detail from oldest entries until the encoded payload fits, then trim the final answer only at a Unicode character boundary. Return the exact visible answer character count so Experience can create a tail Delivery without duplication or loss.

- [ ] **Step 4: Run renderer tests and verify GREEN**

```bash
uv run python -m unittest tests.test_feishu_agent_card -v
```

- [ ] **Step 5: Commit the renderer**

```bash
git add src/lobster0/channels/feishu_cards.py tests/test_feishu_agent_card.py
git commit -m "feat(feishu): 渲染 Claw Trail rich card"
```

### Task 3: Feed structured progress through ChannelExperience

**Files:**
- Modify: `src/lobster0/channels/experience.py`
- Modify: `src/lobster0/channels/feishu.py`
- Modify: `src/lobster0/channels/telegram.py`
- Modify: `src/lobster0/channels/discord.py`
- Modify: `src/lobster0/channels/capabilities.py`
- Modify: `tests/test_channel_experience.py`
- Modify: `tests/test_channel_capabilities.py`
- Modify: `tests/test_feishu_transport.py`
- Modify: `tests/test_telegram_transport.py`
- Modify: `tests/test_discord_transport.py`

**Interfaces:**
- Consumes: `ProgressProjector` and the renderers from Tasks 1-2.
- Produces: `ProgressReceipt(platform_message_id: str, visible_answer_chars: int)`, `ChannelExperienceTransport.create_progress(event, progress, idempotency_key) -> ProgressReceipt`, and `update_progress(platform_message_id, progress) -> ProgressReceipt`; `ExperienceActivity.finalize(content, failed) -> AgentProgress` provides the exact snapshot used for persistence and delivery.

- [ ] **Step 1: Change Experience tests first**

Replace the old “Tool trace is ignored” expectation with safe structured behavior:

```python
await activity.on_event(tool_requested)
self.assertEqual(transport.created, [])
await activity.on_event(tool_started)
self.assertEqual(len(transport.created), 1)
self.assertEqual(transport.created[0][1].steps[-1].status, "running")
await activity.on_event(tool_finished)
progress = activity.finalize(content="final answer", failed=False)
outcome = await activity.finish(content="final answer", failed=False, progress=progress)
self.assertEqual(transport.updated[-1][1].steps[-1].status, "succeeded")
```

Keep explicit assertions that `model_reasoning` and raw secret arguments are absent. Add a no-Tool terminal-card case, throttled coalescing, pre-Tool Approval creates no progress card, and repeated `finish` is idempotent.

- [ ] **Step 2: Run Experience and transport tests and verify RED**

```bash
uv run python -m unittest tests.test_channel_experience tests.test_channel_capabilities tests.test_feishu_transport tests.test_telegram_transport tests.test_discord_transport -v
```

Expected: signature and assertion failures because transports still receive strings.

- [ ] **Step 3: Update the protocol and Experience lifecycle**

Apply every event to the projector. For `progress_is_final=True`, create the first card only on `tool_started`; otherwise preserve public-text preview creation. Structural updates use the existing interval to coalesce bursts, while `finish` always performs one terminal update.

`finalize()` must be synchronous and idempotent. `finish()` accepts an optional prepared `AgentProgress` so the Manager can persist exactly what the transport receives.

- [ ] **Step 4: Adapt all transports**

- Feishu calls `render_agent_progress_card(progress)` and returns its exact `visible_answer_chars` in `ProgressReceipt`.
- Telegram/Discord call `render_compact_progress(progress)`, return `visible_answer_chars=0`, and retain existing mention suppression, length bounds, editing, and completed-message wording.
- The Phase 4 compatibility adapter delegates the same structured snapshot and deletes its duplicate `_progress_card` renderer.

- [ ] **Step 5: Run the focused Experience/transport suite and verify GREEN**

```bash
uv run python -m unittest tests.test_channel_experience tests.test_channel_capabilities tests.test_feishu_transport tests.test_telegram_transport tests.test_discord_transport -v
```

- [ ] **Step 6: Commit the structured Experience integration**

```bash
git add src/lobster0/channels/experience.py src/lobster0/channels/feishu.py src/lobster0/channels/telegram.py src/lobster0/channels/discord.py src/lobster0/channels/capabilities.py tests/test_channel_experience.py tests/test_channel_capabilities.py tests/test_feishu_transport.py tests/test_telegram_transport.py tests/test_discord_transport.py
git commit -m "feat(channel): stream structured Agent progress"
```

### Task 4: Persist the redacted trace and recover completed cards

**Files:**
- Modify: `src/lobster0/storage/conversations.py`
- Modify: `src/lobster0/channels/manager.py`
- Modify: `tests/test_conversations.py`
- Modify: `tests/test_channel_manager.py`

**Interfaces:**
- Consumes: `progress_to_metadata()` and `progress_from_metadata()`.
- Produces: `MessageRepository.save_experience_trace(message_id: int, trace: dict[str, JsonValue]) -> None` and `MessageRepository.experience_trace(message_id: int) -> dict[str, JsonValue] | None`.

- [ ] **Step 1: Write metadata and recovery tests first**

Test an Assistant message round-trip, rejecting non-object/oversized values, preserving existing `provider_request_id`, and never storing `final_answer`.

Extend the completed-card restart test so the first process persists a trace and the recovered process receives an `AgentProgress` containing the same Claw Trail steps.

- [ ] **Step 2: Run storage and Manager tests and verify RED**

```bash
uv run python -m unittest tests.test_conversations tests.test_channel_manager -v
```

Expected: missing repository method failures.

- [ ] **Step 3: Implement bounded metadata merge/read**

Within one SQLite transaction, require an existing Assistant message, parse current metadata, set `experience_trace`, encode with the existing strict JSON helper, and reject encoded traces over 16 KiB. The read path returns `None` for missing or malformed trace without exposing other metadata.

- [ ] **Step 4: Persist before final remote update and restore during recovery**

In normal completion:

```python
progress = activity.finalize(content=result.content, failed=False)
if result.message_id is not None:
    self._messages.save_experience_trace(
        result.message_id,
        progress_to_metadata(progress),
    )
outcome = await activity.finish(
    content=result.content,
    failed=False,
    progress=progress,
)
```

In `_recover_stale`, load the trace from the final Assistant Message, rebuild it with `assistant.content`, and pass it to `finish`; malformed/missing metadata falls back to an answer-only completed card.

- [ ] **Step 5: Run storage and Manager tests and verify GREEN**

```bash
uv run python -m unittest tests.test_conversations tests.test_channel_manager -v
```

- [ ] **Step 6: Commit durability support**

```bash
git add src/lobster0/storage/conversations.py src/lobster0/channels/manager.py tests/test_conversations.py tests/test_channel_manager.py
git commit -m "feat(feishu): 持久化并恢复 progress trace"
```

### Task 5: Synchronize product documentation and verification gates

**Files:**
- Modify: `README.md`
- Modify: `docs/product/20260807_产品需求文档.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/engineering/phase-5/20260809_feishu-single-card-and-lark-cli.md`
- Modify: `docs/evals/releases/v0.5.2.md`

**Interfaces:**
- Consumes: completed behavior from Tasks 1-4.
- Produces: current documentation that describes only implemented, verified rich-card behavior.

- [ ] **Step 1: Update current behavior and boundaries**

Document the Claw Trail layout, safe event projection, raw-reasoning exclusion, 16-step/20-KiB budgets, final trace metadata, single-card normal path, Approval exception, and Markdown fallback. Keep live status as pending unless a real Feishu environment is exercised.

- [ ] **Step 2: Run documentation validation**

```bash
uv run python scripts/validate_docs.py
```

Expected: `Documentation validation: PASS`.

- [ ] **Step 3: Commit documentation**

```bash
git add README.md docs/product/20260807_产品需求文档.md docs/architecture/20260807_系统架构.md docs/engineering/phase-5/20260809_feishu-single-card-and-lark-cli.md docs/evals/releases/v0.5.2.md
git commit -m "docs(feishu): 记录 Agent progress rich card"
```

### Task 6: Run release-level verification

**Files:**
- Verify only: all modified files and existing versioned scenarios.

**Interfaces:**
- Consumes: committed Tasks 1-5 plus the Owner Autopilot default plan.
- Produces: fresh completion evidence.

- [ ] **Step 1: Run all Python tests**

```bash
uv run python -m unittest discover -s tests -v
```

Expected: exit 0 with zero failures and errors.

- [ ] **Step 2: Run Ruff and docs**

```bash
uv run ruff check .
uv run python scripts/validate_docs.py
```

Expected: Ruff and documentation validation pass.

- [ ] **Step 3: Run Channel versioned and stability gates**

```bash
uv run lobster0 eval run --suite channel --root evals/scenarios
uv run lobster0 eval run --suite channel --repeat 20 --json --root evals/scenarios
```

Expected: all versioned Channel cases and all 640 repeated checks pass. These remain `IMPLEMENTATION PASS`, not live Feishu evidence.

- [ ] **Step 4: Run repository hygiene checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors, secrets, generated artifacts, or unrelated staged files.
