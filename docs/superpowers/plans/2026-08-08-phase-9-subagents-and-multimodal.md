# Lobster0 Phase 9 Sub-agents and Multimodal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 depth-1、权限/预算受限、可恢复和可观察的后台 Sub-agent，并让 TUI/IM 安全接收图片与可选语音。

**Architecture:** `spawn_subtask` 创建 durable TaskRun 和独立 child Session；默认使用明确 brief，不复制父对话；子 Tool/权限/预算是父任务的严格子集；完成结果通过内部 announce 回到父 Session。附件进入统一 Artifact Store，经 MIME/magic/hash/size 校验后按模型和 Channel capability 路由。

**Tech Stack:** Python 3.12、现有 Automation Runtime/SQLite/Artifact Store、asyncio、Provider Router、TUI NDJSON、Feishu/Telegram/Discord official SDK、可选 STT/TTS provider。

## Global Constraints

- Sub-agent 最大深度为 1，不能再创建子 Agent。
- 子 Agent 不能创建 Cron、发外部消息、改 Policy、安装 Skill/MCP 或批准 Proposal。
- 子权限和 Tool allowlist 必须是父权限的子集。
- 默认 `isolated` context；`fork` 必须显式请求并受上下文预算限制。
- 子任务有独立 Token、Tool、时间和并发预算。
- Child 输出只是数据，不能覆盖父会话指令。
- 附件默认不自动 OCR/转写，只有用户请求或明确能力路由才处理。
- Voice 是可选依赖，不阻塞纯文本安装。
- Artifact 有私有权限、TTL、来源和清理 Audit。

---

### Task 1: Subtask schema and strict configuration

**Files:**
- Modify: `src/lobster0/config.py`
- Create: `src/lobster0/storage/migrations/0009_subtasks.sql`
- Modify: `src/lobster0/storage/migrations.py`
- Create: `src/lobster0/subagents/__init__.py`
- Create: `src/lobster0/subagents/models.py`
- Test: `tests/test_subagent_config.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `SubagentConfig`, `Subtask`, parent/child links, lifecycle and announce idempotency.

- [ ] **Step 1: Write failing defaults and schema tests**

```python
def test_subagents_are_disabled_and_depth_one(config):
    assert config.subagents.enabled is False
    assert config.subagents.max_depth == 1

def test_subtask_has_parent_session_and_unique_announce(database):
    child = repository.create(parent_session_id=1, parent_turn_id=2, task_name="research", ...)
    assert child.parent_session_id == 1
    assert repository.create_announce(child.id).id == repository.create_announce(child.id).id
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_subagent_config tests.test_storage -v`
Expected: config/migration missing.

- [ ] **Step 3: Add config and durable schema**

```python
@dataclass(frozen=True, slots=True)
class SubagentConfig:
    enabled: bool = False
    max_depth: int = 1
    max_concurrent: int = 2
    default_context: str = "isolated"
    max_context_tokens: int = 16_000
```

Persist parent task/session/turn, child session/task run, context mode/hash, allowed Tools, budget, state, result artifact and announce state.

- [ ] **Step 4: Run config/storage tests and commit**

```bash
git add src/lobster0/config.py src/lobster0/storage/migrations.py src/lobster0/storage/migrations/0009_subtasks.sql src/lobster0/subagents tests/test_subagent_config.py tests/test_storage.py
git commit -m "feat(subagents): persist bounded child tasks"
```

### Task 2: Permission-subset calculator

**Files:**
- Create: `src/lobster0/subagents/policy.py`
- Test: `tests/test_subagent_policy.py`

**Interfaces:**
- Produces: `derive_child_policy(parent, requested) -> ChildPolicy`; never widens permissions.

- [ ] **Step 1: Write failing non-escalation tests**

```python
def test_child_toolset_is_intersection_with_parent():
    child = derive_child_policy(parent(tools={"read_file"}), requested(tools={"read_file", "run_command"}))
    assert child.tools == frozenset({"read_file"})

def test_child_cannot_upgrade_safe_parent_to_autopilot(self):
    with self.assertRaisesRegex(SubagentPolicyError, "subtask permission escalation"):
        derive_child_policy(parent(mode="safe"), requested(mode="autopilot"))
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_subagent_policy -v`
Expected: module missing.

- [ ] **Step 3: Implement subset rules**

Intersect tools, read/write roots, command/network rules, MCP servers, browser access, sandbox and numeric budgets. Always remove messaging, task management, approval decisions, evolution apply/rollback, Skill install and subtask spawn from child Tools.

- [ ] **Step 4: Run policy matrix tests and commit**

```bash
git add src/lobster0/subagents/policy.py tests/test_subagent_policy.py
git commit -m "feat(subagents): prevent child permission escalation"
```

### Task 3: Isolated and bounded fork context

**Files:**
- Create: `src/lobster0/subagents/context.py`
- Modify: `src/lobster0/agent/context.py`
- Test: `tests/test_subagent_context.py`

**Interfaces:**
- Produces: child bootstrap request from explicit brief; optional safety-filtered fork snapshot.

- [ ] **Step 1: Write failing context tests**

```python
def test_isolated_child_receives_brief_not_parent_transcript(builder):
    request = builder.build(mode="isolated", brief="Research project A", parent_session_id=1)
    assert "Research project A" in request.messages[-1].content
    assert "parent private message" not in serialize(request)

def test_fork_excludes_hidden_reasoning_and_secret_tool_arguments(builder):
    request = builder.build(mode="fork", ...)
    assert "hidden reasoning" not in serialize(request)
    assert "secret argument" not in serialize(request)
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_subagent_context -v`
Expected: module missing.

- [ ] **Step 3: Implement context modes**

`isolated` includes identity/safety, selected Skills/Memory and brief. `fork` additionally includes a bounded, sanitized transcript snapshot with provenance and hash; no pending Approval, raw Tool arguments, hidden reasoning or unrelated Channel messages.

- [ ] **Step 4: Run context tests and commit**

```bash
git add src/lobster0/subagents/context.py src/lobster0/agent/context.py tests/test_subagent_context.py
git commit -m "feat(subagents): build isolated and bounded fork context"
```

### Task 4: Subagent runner and push-based completion

**Files:**
- Create: `src/lobster0/subagents/runner.py`
- Create: `src/lobster0/subagents/announce.py`
- Modify: `src/lobster0/automation/runner.py`
- Modify: `src/lobster0/agent/turn.py`
- Test: `tests/test_subagent_runner.py`
- Test: `tests/test_subagent_announce.py`

**Interfaces:**
- Produces: `SubagentRunner.run_once()`, internal idempotent `CompletionAnnouncer`, no polling loop.

- [ ] **Step 1: Write failing isolation/recovery/announce tests**

```python
async def test_child_uses_independent_session_and_budget():
    result = await runner.run_once()
    assert result.child_session_id != result.parent_session_id
    assert result.usage.max_tokens <= parent_budget.max_tokens

async def test_completion_is_announced_once_after_restart():
    await runner.complete(child)
    await CompletionAnnouncer(reopened_db).flush()
    await CompletionAnnouncer(reopened_db).flush()
    assert parent_messages.count(kind="subtask_completion") == 1
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_subagent_runner tests.test_subagent_announce -v`
Expected: modules missing.

- [ ] **Step 3: Implement execution through existing TaskRunner/TurnService**

Child receives a derived immutable Runtime snapshot. Completion stores a bounded summary and Artifact references, enqueues an internal parent-session event and wakes the parent without busy polling. Child failure/timeout/cancel also announces one safe terminal result.

- [ ] **Step 4: Run focused tests and commit**

```bash
git add src/lobster0/subagents/runner.py src/lobster0/subagents/announce.py src/lobster0/automation/runner.py src/lobster0/agent/turn.py tests/test_subagent_runner.py tests/test_subagent_announce.py
git commit -m "feat(subagents): execute and announce isolated child tasks"
```

### Task 5: spawn_subtask Tool and activity visibility

**Files:**
- Create: `src/lobster0/tools/subagents.py`
- Modify: `src/lobster0/runtime.py`
- Modify: `src/lobster0/agent/events.py`
- Modify: `src/lobster0/bridge/protocol.py`
- Modify: `tui/src/`
- Test: `tests/test_subagent_tool.py`
- Test: `tests/test_pi_tui_integration.py`
- Modify: `tui/test/`

**Interfaces:**
- Produces: `spawn_subtask(brief, task_name, context_mode, requested_tools, budget)` and read-only `list_subtasks`.

- [ ] **Step 1: Write failing depth/tool/budget tests**

Verify empty/ambiguous brief rejected, `task_name` normalized, depth-1 child has no spawn Tool, requested Tools are subset, budget capped, and TUI shows child state/tool count/usage without full hidden context.

- [ ] **Step 2: Implement Tool and activity events**

Spawn returns immediately with subtask id. Parent can continue or wait through runtime event delivery; Agent prompt forbids polling loops. TUI exposes queued/running/completed/failed/cancelled and safe result preview.

- [ ] **Step 3: Run Tool/TUI tests and commit**

```bash
git add src/lobster0/tools/subagents.py src/lobster0/runtime.py src/lobster0/agent/events.py src/lobster0/bridge/protocol.py tui tests/test_subagent_tool.py tests/test_pi_tui_integration.py
git commit -m "feat(subagents): spawn and inspect bounded child work"
```

### Task 6: Unified inbound Attachment contract

**Files:**
- Modify: `src/lobster0/channels/base.py`
- Modify: `src/lobster0/artifacts/store.py`
- Create: `src/lobster0/media/__init__.py`
- Create: `src/lobster0/media/models.py`
- Create: `src/lobster0/media/ingest.py`
- Test: `tests/test_media_ingest.py`
- Test: `tests/test_channel_contracts.py`

**Interfaces:**
- Produces: `Attachment`, `InboundMessage.attachments`, validated Artifact ids.

- [ ] **Step 1: Write failing media validation tests**

```python
def test_declared_png_with_executable_magic_is_rejected(self, ingest):
    with self.assertRaisesRegex(MediaError, "artifact type mismatch"):
        ingest.bytes(executable_bytes, declared_type="image/png", source="telegram")

def test_attachment_repr_and_audit_hide_local_path(ingest):
    attachment = ingest.bytes(valid_png, declared_type="image/png", source="feishu")
    assert str(attachment.local_path) not in repr(attachment)
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_media_ingest tests.test_channel_contracts -v`
Expected: media contract missing.

- [ ] **Step 3: Implement bounded streaming ingestion**

Stream to private temporary file with byte limit, hash while writing, validate magic/MIME/dimensions/duration, then move into Artifact Store. Preserve source/channel/platform attachment id only as hash or protected metadata.

- [ ] **Step 4: Run media/contract tests and commit**

```bash
git add src/lobster0/channels/base.py src/lobster0/artifacts/store.py src/lobster0/media tests/test_media_ingest.py tests/test_channel_contracts.py
git commit -m "feat(media): validate unified inbound attachments"
```

### Task 7: Feishu, Telegram and Discord attachment adapters

**Files:**
- Modify: `src/lobster0/channels/feishu.py`
- Modify: `src/lobster0/channels/telegram.py`
- Modify: `src/lobster0/channels/discord.py`
- Test: `tests/test_feishu_transport.py`
- Test: `tests/test_telegram_transport.py`
- Test: `tests/test_discord_transport.py`

**Interfaces:**
- Each adapter downloads through official SDK into `MediaIngest`; no adapter invents its own file store.

- [ ] **Step 1: Write failing adapter tests**

Cover image/file metadata, oversized early abort, interrupted download, duplicate attachment, unsupported sticker/embed, platform filename traversal, authentication errors and retry mapping.

- [ ] **Step 2: Implement capability-gated downloads**

DM/group admission and allowlists run before bytes download. Adapters emit typing/ack before bounded download where supported. Unsupported media produces a safe message, not a crash or empty response.

- [ ] **Step 3: Run all channel adapter/transport tests and commit**

```bash
git add src/lobster0/channels/feishu.py src/lobster0/channels/telegram.py src/lobster0/channels/discord.py tests/test_feishu_transport.py tests/test_telegram_transport.py tests/test_discord_transport.py
git commit -m "feat(channels): ingest safe cross-platform attachments"
```

### Task 8: Vision request support

**Files:**
- Modify: `src/lobster0/providers/base.py`
- Modify: `src/lobster0/providers/openai_compatible.py`
- Create: `src/lobster0/media/router.py`
- Modify: `src/lobster0/agent/context.py`
- Test: `tests/test_vision_provider.py`
- Test: `tests/test_media_router.py`

**Interfaces:**
- Produces: provider-neutral content parts and capability-aware model route.

- [ ] **Step 1: Write failing capability and serialization tests**

```python
def test_image_is_sent_only_to_vision_capable_model(router):
    route = router.resolve((image_attachment,), requested="analyze")
    assert route.capabilities.vision is True

def test_unrequested_attachment_is_not_automatically_sent(context):
    request = context.build(user_text="谢谢", attachments=(image,))
    assert image.artifact_id not in serialize_provider_request(request)
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_vision_provider tests.test_media_router -v`
Expected: content-part/capability support missing.

- [ ] **Step 3: Add provider-neutral image parts**

Read bounded image bytes only at Provider boundary, verify artifact hash again, serialize for compatible providers, and avoid logging/base64 persistence. Fail clearly when selected model lacks vision; do not silently send to another paid model unless configured route permits it.

- [ ] **Step 4: Run Provider/media tests and commit**

```bash
git add src/lobster0/providers/base.py src/lobster0/providers/openai_compatible.py src/lobster0/media/router.py src/lobster0/agent/context.py tests/test_vision_provider.py tests/test_media_router.py
git commit -m "feat(media): route verified images to vision models"
```

### Task 9: Optional transcription and TTS providers

**Files:**
- Create: `src/lobster0/media/speech.py`
- Modify: `src/lobster0/config.py`
- Modify: `pyproject.toml`
- Test: `tests/test_speech_providers.py`

**Interfaces:**
- Produces: `SpeechToTextProvider`, `TextToSpeechProvider`, disabled-by-default config and Artifact outputs.

- [ ] **Step 1: Write failing opt-in, timeout and Secret tests**

Verify pure text install imports without speech extras; disabled speech never sends audio; provider receives only its API key; transcript/TTS size and duration are bounded; cancellation closes stream; logs contain no audio bytes or transcript body.

- [ ] **Step 2: Implement optional protocols and one OpenAI-compatible adapter**

Keep Core interfaces independent of vendor. STT transcript is `untrusted_media_content`; TTS only speaks final user-facing text and creates an Artifact for Channel delivery.

- [ ] **Step 3: Run speech tests and commit**

```bash
git add src/lobster0/media/speech.py src/lobster0/config.py pyproject.toml uv.lock tests/test_speech_providers.py
git commit -m "feat(media): add optional bounded speech providers"
```

### Task 10: Cleanup, scenarios and v0.9.0

**Files:**
- Modify: `src/lobster0/gateway.py`
- Modify: `src/lobster0/doctor.py`
- Create: `evals/scenarios/subagents.v1.jsonl`
- Create: `evals/scenarios/media.v1.jsonl`
- Create: `docs/engineering/phase-9/subagents-and-multimodal.md`
- Create: `docs/evals/releases/v0.9.0.md`
- Modify: `README.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/progress/index.html`
- Test: `tests/test_documentation.py`

**Interfaces:**
- Gateway supervises Subagent workers and Artifact cleanup; Doctor reports safe capability status.

- [ ] **Step 1: Add active regression cases**

Sub-agent: isolated/fork, permission subset, depth rejection, budget, cancel, restart, push completion, no external messaging. Media: image/file validation, three channels, model capability, unrequested attachment, oversized/invalid content, optional speech and TTL cleanup.

- [ ] **Step 2: Run complete deterministic gates**

```bash
uv run python -m unittest discover -s tests -v
pnpm --dir tui test
pnpm --dir tui build
uv run ruff check .
uv run lobster0 eval validate --root evals/scenarios
uv run lobster0 eval run --suite offline --root evals/scenarios
uv run lobster0 eval run --suite subagents --root evals/scenarios
uv run lobster0 eval run --suite media --root evals/scenarios
uv run python scripts/validate_docs.py
uv lock --check
uv build
git diff --check
```

- [ ] **Step 3: Run live samples**

Spawn two bounded research subtasks and verify one-time parent completion; send one image through the configured real Channel and receive a vision answer; if speech is configured, transcribe and reply to one short voice message. Evidence contains ids/hashes/counts/status only.

- [ ] **Step 4: Commit verified facts**

```bash
git add src/lobster0/gateway.py src/lobster0/doctor.py evals docs README.md tests/test_documentation.py
git commit -m "release(v0.9.0): verify bounded subagents and media"
```

## Final verification

- [ ] Child Tool/permission/budget is always a parent subset.
- [ ] Depth-1 child cannot spawn, message, schedule, install or self-approve.
- [ ] Completion is push-based and idempotent across restart.
- [ ] Attachment admission happens before download.
- [ ] MIME/magic/size/hash and TTL are enforced.
- [ ] Images go only to configured vision-capable providers.
- [ ] Speech remains optional and disabled by default.
- [ ] Raw media/base64/Secret never enters logs, SQLite message text or Evidence.
