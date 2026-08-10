# Lobster0 Memory Autopilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 TUI、飞书、Telegram 和 Discord 之间交付同一个 Owner Memory Space，支持非阻塞自动捕获、明确“记住”即时持久化、可恢复 Flush、FTS5 召回、治理审批、纠错遗忘和 Markdown 直接维护。

**Architecture:** SQLite 保存 L0 消息、队列、lease、候选、状态、来源、审计和可重建 FTS5 Projection；已接受的语义 Memory Unit 以 owner-only Markdown 为真相源。`MemoryService` 是 Turn、Context、Tool 和维护命令的唯一入口，所有读取先经过 typed `DisclosureContext`，所有写入先经过 Core 的 Secret/Trust/Conflict Policy。

**Tech Stack:** Python 3.12、SQLite/FTS5、标准库 `asyncio`/`hashlib`/`json`/`os`/`pathlib`/`unicodedata`、现有 OpenAI-compatible Provider、现有 Approval/Tool/Channel 基础设施、`unittest`、Ruff。

## Global Constraints

- 实施基线是 [正式设计 Spec](../specs/2026-08-08-memory-autopilot-design.md)；产品语义变化先改 Spec。
- 一个本地 Owner 只有一个 `owner_id`；Channel、账号、Session 和群聊 ID 只作为来源，不创建第二份私人画像。
- 本地 TUI 与已验证 Owner 私聊可以读取私人 Memory；群聊、非 Owner、身份不确定和映射冲突均 fail closed。
- 普通 Turn 的 capture/flush/extract 失败不能让用户回复失败；明确“记住”必须在原子落盘完成后才报告成功。
- 密码、API Key、Token、Authorization、OTP、验证码和私钥在进入候选队列、日志或 Markdown 前拒绝；错误不得回显原值。
- 普通低风险事实可自动进入 `short_term`；重复且高置信的低风险事实可晋升 `active`。
- 敏感、冲突、权限规则、行为规则和会扩大 Agent 行为的内容必须进入 Review，不能由模型自行批准。
- Memory 是上下文，不是权限；任何 Unit 都不能绕过 Tool Policy、Approval、Workspace 或 Sandbox。
- Markdown commit 先于 Projection checkpoint；索引失败可重建，Markdown 写失败不得推进 source range。
- Unit 必须至少有一条可验证 L0 Source；Provider 只能返回候选文本和 source message id，不能决定 owner、scope、status、id、时间或 hash。
- 单元测试离线、确定、无真实模型和 IM；时间、Provider、文件系统故障和 lease 均用最小 fake。
- 每个代码任务执行 RED → GREEN；公共函数、类、方法和返回值有准确类型标注与中文 docstring。

## Delivery Map

| 阶段 | 交付 | 对应任务 |
| --- | --- | --- |
| Memory A | Owner Identity + Disclosure | Task 1–2 |
| Memory B | Durable Buffer + Markdown Flush + Explicit Remember | Task 3–5 |
| Memory C | FTS5 Recall + Context/Tool surface | Task 6 |
| Memory D | Extraction + Promotion + Review/Conflict/Forget | Task 7–8 |
| Memory E | Reconcile + Legacy Migration + Maintenance | Task 9 |
| Release gate | Versioned cases + docs + recovery evidence | Task 10 |

---

### Task 1: Memory A — typed identity and disclosure policy

**Files:**
- Create: `src/lobster0/memory/models.py`
- Create: `src/lobster0/memory/policy.py`
- Modify: `src/lobster0/memory/__init__.py`
- Test: `tests/test_memory_disclosure.py`

**Interfaces:**
- Produces: `DisclosureContext`, `DisclosureDecision`, `MemoryScope`, `MemoryStatus`, `SourceRef` and fail-closed `MemoryDisclosurePolicy.decide()`.

- [ ] **Step 1: Write the disclosure matrix as failing tests**

```python
def test_private_memory_is_only_visible_to_verified_owner_private_context() -> None:
    allowed = disclosure(local_context(owner_id=1))
    denied = disclosure(group_context(owner_id=1, requester_user_id=1))
    unknown = disclosure(unknown_context(owner_id=1))
    self.assertEqual(allowed.private_access, "full")
    self.assertEqual(denied.private_access, "deny")
    self.assertEqual(unknown.reason_code, "identity_unverified")

def test_model_cannot_select_another_owner() -> None:
    with self.assertRaisesRegex(MemoryPolicyError, "owner mismatch"):
        disclosure(owner_dm_context(owner_id=1, requester_user_id=2))
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_memory_disclosure -v`

Expected: `lobster0.memory.models` and `lobster0.memory.policy` do not exist.

- [ ] **Step 3: Implement immutable contracts and one decision table**

```python
@dataclass(frozen=True, slots=True)
class DisclosureContext:
    owner_id: int
    requester_user_id: int | None
    channel: str
    conversation_kind: Literal["local", "direct", "group", "unknown"]
    identity_verified: bool

@dataclass(frozen=True, slots=True)
class DisclosureDecision:
    private_access: Literal["full", "deny"]
    capture_scope: Literal["private", "public", "none"]
    reason_code: str
```

Reject non-positive ids, unknown channels/kinds, unverified local impersonation and mismatched requester/owner. No model-controlled dictionary reaches the policy.

- [ ] **Step 4: Run focused tests**

Run: `uv run python -m unittest tests.test_memory_disclosure -v`

Expected: all local/direct/group/non-owner/unknown cases PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/memory tests/test_memory_disclosure.py
git commit -m "feat(memory): 建立 Owner Disclosure fail-closed 边界"
```

### Task 2: Memory A — propagate disclosure through Turn and Channel

**Files:**
- Modify: `src/lobster0/agent/turn.py`
- Modify: `src/lobster0/agent/context.py`
- Modify: `src/lobster0/channels/manager.py`
- Modify: `src/lobster0/runtime.py`
- Modify: `src/lobster0/tools/base.py`
- Test: `tests/test_turn_service.py`
- Test: `tests/test_context.py`
- Test: `tests/test_channel_manager.py`
- Test: `tests/test_multi_channel_evals.py`

**Interfaces:**
- Consumes: local TUI identity or `InboundMessage.chat_type` plus Channel owner mapping.
- Produces: one Core-created `DisclosureContext` on `ContextBuilder.build()` and `ToolContext`; adapters cannot widen it.

- [ ] **Step 1: Write failing cross-channel and group isolation tests**

```python
async def test_same_owner_dm_uses_one_memory_space_across_channels() -> None:
    feishu = await handle_owner_dm("feishu", "记住我喜欢中文")
    discord = await handle_owner_dm("discord", "我喜欢什么语言？")
    self.assertEqual(feishu.owner_id, discord.owner_id)

async def test_owner_group_message_still_denies_private_recall() -> None:
    request = await capture_provider_request(chat_type="group", external_user=OWNER)
    self.assertNotIn("private-memory-sentinel", request.messages[0].content)
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_turn_service tests.test_context tests.test_channel_manager tests.test_multi_channel_evals -v`

Expected: existing APIs only carry `trusted_owner: bool` and cannot distinguish direct/group.

- [ ] **Step 3: Replace the boolean at the Memory boundary**

Keep `trusted_owner` temporarily for file/command permissions, but construct a separate `DisclosureContext` from trusted Core fields. `ChannelManager` passes `conversation_kind="direct"` for `p2p` and `"group"` for group/guild messages. TUI always uses verified `local`.

- [ ] **Step 4: Verify no private bytes reach Provider, Tool result or event log**

Run: `uv run python -m unittest tests.test_turn_service tests.test_context tests.test_channel_manager tests.test_multi_channel_evals -v`

Expected: four entry points share owner 1; group/non-owner/unknown receive empty private Memory and a stable reason code.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/agent src/lobster0/channels/manager.py src/lobster0/runtime.py src/lobster0/tools/base.py tests
git commit -m "feat(memory): 贯通跨渠道 DisclosureContext"
```

### Task 3: Memory B — migration, repository state machines and durable buffer

**Files:**
- Create: `src/lobster0/storage/migrations/0003_memory_autopilot.sql`
- Modify: `src/lobster0/storage/migrations.py`
- Create: `src/lobster0/memory/repository.py`
- Create: `src/lobster0/memory/buffer.py`
- Test: `tests/test_memory_repository.py`
- Test: `tests/test_memory_buffer.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `MemoryBufferRepository`, `MemoryRunRepository`, `MemoryUnitRepository`, `MemoryReviewRepository` and transactional lease/checkpoint methods.

- [ ] **Step 1: Write failing migration, idempotency and lease tests**

```python
def test_same_source_range_has_one_flush_run() -> None:
    first = runs.enqueue(owner_id=1, first_message_id=10, last_message_id=20, extractor="v1", prompt_hash=HASH)
    second = runs.enqueue(owner_id=1, first_message_id=10, last_message_id=20, extractor="v1", prompt_hash=HASH)
    self.assertEqual(first.id, second.id)

def test_expired_lease_is_reclaimed_once() -> None:
    claimed = runs.claim_next("worker-a", now=NOW)
    recovered = runs.claim_next("worker-b", now=NOW + LEASE_TIMEOUT)
    self.assertEqual(claimed.id, recovered.id)
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_memory_repository tests.test_memory_buffer tests.test_storage -v`

Expected: migration version remains 2 and repositories are missing.

- [ ] **Step 3: Add v3 schema and transactional repositories**

The migration creates `memory_buffers`, `memory_flush_runs`, `memory_candidates`, `memory_units`, `memory_sources`, `memory_conflicts`, `memory_reviews`, `memory_manifests` and `memory_audit`. Enforce unique idempotency key, unique owner/unit id, legal status checks, source foreign keys and terminal-state immutability. Claims use `BEGIN IMMEDIATE` and UTC lease timestamps.

- [ ] **Step 4: Cover crash windows and invalid transitions**

Run: `uv run python -m unittest tests.test_memory_repository tests.test_memory_buffer tests.test_storage -v`

Expected: queued/running/retry/projection-pending/completed/dead-letter transitions, double claim, stale recovery and malformed JSON all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/storage/migrations.py src/lobster0/storage/migrations/0003_memory_autopilot.sql src/lobster0/memory tests/test_memory_repository.py tests/test_memory_buffer.py tests/test_storage.py
git commit -m "feat(memory): 持久化 buffer、lease 与审计状态机"
```

### Task 4: Memory B — atomic Markdown store and explicit remember

**Files:**
- Create: `src/lobster0/memory/markdown_store.py`
- Create: `src/lobster0/memory/service.py`
- Create: `src/lobster0/tools/memory_v2.py`
- Modify: `src/lobster0/memory/store.py`
- Modify: `src/lobster0/runtime.py`
- Test: `tests/test_memory_markdown_store.py`
- Test: `tests/test_memory_service.py`
- Test: `tests/test_memory_tools_v2.py`

**Interfaces:**
- Produces: `MemoryService.remember_explicit()`, owner-scoped atomic Unit append, `memory_remember` Tool and compatibility adapters for `read_memory`/`propose_memory`.

- [ ] **Step 1: Write failing explicit-intent, secret and atomicity tests**

```python
async def test_explicit_owner_remember_persists_without_second_approval() -> None:
    result = await service.remember_explicit(explicit_request("记住我偏好中文"))
    self.assertEqual(result.status, "active")
    self.assertEqual(approvals.count(), 0)
    self.assertIn(result.unit_id, markdown_text())

async def test_secret_and_rule_claims_never_auto_persist() -> None:
    with self.assertRaisesRegex(MemoryError, "was not stored"):
        await service.remember_explicit(explicit_request("记住密码: sentinel-secret"))
    self.assertNotIn("sentinel-secret", all_state_bytes())
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_memory_markdown_store tests.test_memory_service tests.test_memory_tools_v2 -v`

Expected: Markdown v2 Store, facade and Tool are missing.

- [ ] **Step 3: Implement stable Unit blocks and atomic replace**

Write owner files below `memory/owners/<owner_id>/`. Hold an owner/path lock, compare manifest hash, write the full UTF-8 document to a same-directory temp file, flush + `fsync`, `os.replace`, fsync the directory, then commit `markdown_committed`. Core generates Unit id, UTC time and SHA-256; source message must belong to the current Turn and verified Owner.

`memory_remember` is only allowed when the bound latest user message contains explicit remember intent. Ordinary facts become `active`; sensitive/conflicting/action-changing facts become `review_required`; credentials are rejected. The Tool cannot accept owner id, status or scope arguments.

- [ ] **Step 4: Run write, symlink, crash and compatibility regression**

Run: `uv run python -m unittest tests.test_memory_markdown_store tests.test_memory_service tests.test_memory_tools_v2 tests.test_memory_store tests.test_memory_tools -v`

Expected: temp-write/fsync/replace failure leaves the old truth intact; duplicate explicit requests are idempotent; old Tools still work and emit deprecation audit.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/memory src/lobster0/tools/memory_v2.py src/lobster0/runtime.py tests/test_memory_markdown_store.py tests/test_memory_service.py tests/test_memory_tools_v2.py tests/test_memory_store.py tests/test_memory_tools.py
git commit -m "feat(memory): 原子持久化明确 remember 请求"
```

### Task 5: Memory B — non-blocking capture, flush worker and recovery

**Files:**
- Create: `src/lobster0/memory/flush.py`
- Create: `src/lobster0/memory/worker.py`
- Modify: `src/lobster0/agent/turn.py`
- Modify: `src/lobster0/agent/compaction.py`
- Modify: `src/lobster0/runtime.py`
- Modify: `src/lobster0/gateway.py`
- Test: `tests/test_memory_flush.py`
- Test: `tests/test_memory_worker.py`
- Test: `tests/test_memory_recovery.py`

**Interfaces:**
- Produces: durable capture receipt, idempotent `FlushCoordinator.run_once()`, bounded background worker and startup/shutdown recovery.

- [ ] **Step 1: Write failing latency and crash-window tests**

```python
async def test_completed_turn_returns_before_extractor_runs() -> None:
    result = await turn_service.handle(OWNER, "普通消息", "demo")
    self.assertEqual(result.content, "answer")
    self.assertEqual(extractor.calls, 0)
    self.assertEqual(buffers.pending_count(), 1)

def test_markdown_committed_projection_failed_resumes_without_duplicate_unit() -> None:
    crash_after_markdown_commit()
    recover_startup()
    self.assertEqual(markdown_unit_count(), 1)
    self.assertEqual(units.active_count(), 1)
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_memory_flush tests.test_memory_worker tests.test_memory_recovery -v`

Expected: capture and worker hooks are missing.

- [ ] **Step 3: Implement triggers and bounded lifecycle**

Capture only completed trusted private Turns. Trigger after 5 Turns, 10-minute idle, `/new`/`/reset`, pre-compaction, `/memory flush`, bounded shutdown and startup recovery. A worker owns no more than one lease per owner; Provider/extractor failures move to exponential retry without failing the Turn.

- [ ] **Step 4: Verify all critical crash points**

Run: `uv run python -m unittest tests.test_memory_flush tests.test_memory_worker tests.test_memory_recovery tests.test_compaction tests.test_gateway -v`

Expected: before/after Provider, temp write, replace, manifest update, projection and checkpoint crashes produce no lost source range or duplicate Unit.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/memory src/lobster0/agent src/lobster0/runtime.py src/lobster0/gateway.py tests/test_memory_flush.py tests/test_memory_worker.py tests/test_memory_recovery.py tests/test_compaction.py tests/test_gateway.py
git commit -m "feat(memory): 自动 flush 并恢复中断运行"
```

### Task 6: Memory C — FTS5/CJK retrieval, Context budget and user surface

**Files:**
- Create: `src/lobster0/memory/retrieval.py`
- Create: `src/lobster0/memory/context.py`
- Modify: `src/lobster0/agent/context.py`
- Modify: `src/lobster0/tools/memory_v2.py`
- Modify: `src/lobster0/bridge/protocol.py`
- Modify: `src/lobster0/channels/experience.py`
- Modify: `tui/src/`
- Test: `tests/test_memory_retrieval.py`
- Test: `tests/test_memory_context.py`
- Test: `tests/test_memory_tools_v2.py`
- Modify: `tui/test/`

**Interfaces:**
- Produces: owner-scoped FTS5 Projection, normalized CJK n-gram shadow text, deterministic ranking, `memory_search/get/list/flush` and `/memory status|list|search|why|flush`.

- [ ] **Step 1: Write failing retrieval and disclosure tests**

```python
def test_chinese_recall_returns_complete_units_with_sources() -> None:
    result = retrieval.search(owner_request("默认回复语言"), limit=5)
    self.assertEqual(result.items[0].text, "用户偏好使用中文回复")
    self.assertTrue(result.items[0].source_ids)

def test_group_and_other_owner_search_return_no_private_hits() -> None:
    self.assertEqual(retrieval.search(group_request("中文"), limit=5).items, ())
    self.assertEqual(retrieval.search(other_owner_request("中文"), limit=5).items, ())
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_memory_retrieval tests.test_memory_context tests.test_memory_tools_v2 -v`

Expected: no Projection/retrieval/context layer.

- [ ] **Step 3: Implement disposable Projection and deterministic budget**

Create `memory_fts` as external-content FTS5 backed by `memory_units`. Normalize Unicode and generate bounded Chinese bigram/trigram shadow text in Python. Filter owner/scope/status/validity/sensitivity before ranking. Context chooses complete Units only, with `min(provider_window * 0.08, 2200)` token budget and stable rank/id tie-break.

- [ ] **Step 4: Wire Tools, slash commands and UI events**

Model Tools never accept owner id. `memory_get` returns source metadata but not unrelated raw conversations. TUI/IM show redacted buffered/flushed/recalled events. Run Python tests and `pnpm --dir tui test`; add a fixed Chinese retrieval fixture and require Recall@5 ≥ 90%.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/memory src/lobster0/agent/context.py src/lobster0/tools/memory_v2.py src/lobster0/bridge src/lobster0/channels/experience.py tui tests
git commit -m "feat(memory): 增加 owner-scoped FTS5 召回"
```

### Task 7: Memory D — extraction, validation and automatic promotion

**Files:**
- Create: `src/lobster0/memory/extractor.py`
- Create: `src/lobster0/memory/validator.py`
- Create: `src/lobster0/memory/promotion.py`
- Modify: `src/lobster0/memory/flush.py`
- Test: `tests/test_memory_extractor.py`
- Test: `tests/test_memory_validator.py`
- Test: `tests/test_memory_promotion.py`

**Interfaces:**
- Produces: strict candidate schema, source validation, secret/trust/conflict classification and deterministic short-term/promotion decisions.

- [ ] **Step 1: Write failing fabricated-source and promotion tests**

```python
def test_candidate_with_unknown_source_is_rejected() -> None:
    candidate = provider_candidate(text="偏好中文", source_ids=["message-999"])
    self.assertEqual(validator.validate(candidate).decision, "rejected")

def test_repeated_low_risk_fact_promotes_but_behavior_rule_requires_review() -> None:
    self.assertEqual(promote(repeated_preference()).status, "active")
    self.assertEqual(promote(permission_rule()).status, "review_required")
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_memory_extractor tests.test_memory_validator tests.test_memory_promotion -v`

Expected: extractor/validator/promotion modules missing.

- [ ] **Step 3: Implement strict Provider candidate boundary**

Extractor input contains only the claimed source range. Parse strict JSON with a bounded candidate count and text size. Core loads every source id from SQLite and verifies owner/session/range. Apply exact hash dedupe, secret scan, prompt-injection-as-data handling, sensitivity and behavior-impact rules before any Markdown write.

Promotion requires repeated independent sources or explicit owner confirmation plus configured confidence; confidence alone never changes permission.

- [ ] **Step 4: Run adversarial and retry tests**

Run: `uv run python -m unittest tests.test_memory_extractor tests.test_memory_validator tests.test_memory_promotion tests.test_memory_flush -v`

Expected: malformed JSON, oversized output, fabricated source, duplicate, secret, conflict and Provider retry paths PASS with no sensitive logs.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/memory tests/test_memory_extractor.py tests/test_memory_validator.py tests/test_memory_promotion.py tests/test_memory_flush.py
git commit -m "feat(memory): 自动提取并治理低风险事实"
```

### Task 8: Memory D — review, conflict, correction and forget

**Files:**
- Create: `src/lobster0/memory/review.py`
- Modify: `src/lobster0/memory/service.py`
- Modify: `src/lobster0/tools/memory_v2.py`
- Modify: `src/lobster0/bridge/protocol.py`
- Modify: `src/lobster0/channels/experience.py`
- Modify: `tui/src/`
- Test: `tests/test_memory_review.py`
- Test: `tests/test_memory_forget.py`
- Modify: `tui/test/`

**Interfaces:**
- Produces: Owner review projection, approve/reject/correct, conflict supersede and preview-bound forget.

- [ ] **Step 1: Write failing conflict and forget-binding tests**

```python
def test_conflicting_active_fact_requires_owner_resolution() -> None:
    review = service.remember_explicit(request("记住我现在偏好英文"))
    self.assertEqual(review.status, "review_required")
    self.assertEqual(catalog.active("preference.language"), OLD_UNIT)

def test_forget_approval_is_bound_to_preview_hash() -> None:
    preview = service.preview_forget(owner_request(UNIT_ID))
    mutate_unit(UNIT_ID)
    with self.assertRaisesRegex(MemoryError, "forget target changed"):
        service.apply_forget(preview.hash)
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_memory_review tests.test_memory_forget -v`

Expected: review and forget services missing.

- [ ] **Step 3: Implement legal transitions and source-preserving supersede**

Approval binds candidate hash, source set and intended transition. Correction creates a new Unit and supersedes the old one; it never edits history in place. Forget archives the Unit and removes it from Profile/Recall while preserving redacted audit and source chain. Model Tools can propose/preview but cannot approve themselves.

- [ ] **Step 4: Run Core and UI regression**

Run Python tests plus `pnpm --dir tui test`. Verify stale buttons, replay, cross-owner id, concurrent correction, long diff/card pagination and no private text in generic logs.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/memory src/lobster0/tools/memory_v2.py src/lobster0/bridge src/lobster0/channels/experience.py tui tests
git commit -m "feat(memory): 支持 Review、纠错与可审计遗忘"
```

### Task 9: Memory E — direct-edit reconcile, legacy migration and maintenance

**Files:**
- Create: `src/lobster0/memory/reconcile.py`
- Create: `src/lobster0/memory/migration.py`
- Create: `src/lobster0/memory/maintenance.py`
- Modify: `src/lobster0/bootstrap.py`
- Modify: `src/lobster0/doctor.py`
- Test: `tests/test_memory_reconcile.py`
- Test: `tests/test_memory_migration.py`
- Test: `tests/test_memory_maintenance.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Produces: manifest lint/rebuild, read-only legacy importer, daily expiry and weekly profile review.

- [ ] **Step 1: Write failing direct-edit and legacy idempotency tests**

```python
def test_valid_manual_edit_rebuilds_projection_with_audit() -> None:
    edit_markdown_unit(UNIT_ID, "用户偏好简洁回答")
    result = reconcile.scan(owner_id=1)
    self.assertEqual(result.updated, (UNIT_ID,))
    self.assertEqual(search("简洁").items[0].unit_id, UNIT_ID)

def test_legacy_import_is_hash_idempotent_and_never_deletes_source() -> None:
    first = migrate_legacy()
    second = migrate_legacy()
    self.assertEqual(first.unit_ids, second.unit_ids)
    self.assertTrue(paths.memory_file.exists())
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_memory_reconcile tests.test_memory_migration tests.test_memory_maintenance tests.test_doctor -v`

Expected: reconcile/migration/maintenance modules missing.

- [ ] **Step 3: Implement fail-closed reconcile and compatibility migration**

Compare manifest hash/mtime on startup, schedule and `/memory rebuild`. Valid edits generate `manual_edit` audit and rebuild Projection. Duplicate ids, malformed frontmatter, illegal status/source or parse errors preserve the file, isolate the error with path/line and keep the last valid Projection. Import current `MEMORY.md` and daily files as `legacy_manual` Units using source file hash; never rewrite/delete originals in this release.

- [ ] **Step 4: Add maintenance and Doctor checks**

Expire TTL Units daily, produce a weekly review candidate instead of silent profile rewrites, reclaim leases and report projection drift, parser errors, retry/dead-letter counts and migration state without content.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/memory src/lobster0/bootstrap.py src/lobster0/doctor.py tests/test_memory_reconcile.py tests/test_memory_migration.py tests/test_memory_maintenance.py tests/test_doctor.py
git commit -m "feat(memory): 对账 Markdown 并迁移 legacy memory"
```

### Task 10: Versioned Memory gate, documentation and release evidence

**Files:**
- Create: `evals/scenarios/memory.v1.jsonl`
- Create: `docs/engineering/phase-5/20260809_memory-autopilot.md`
- Create: `docs/evals/releases/v0.6.0.md`
- Modify: `src/lobster0/evals/runner.py`
- Modify: `tests/test_eval_runner.py`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `docs/product/20260807_产品需求文档.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/progress/index.html`
- Modify: `tests/test_documentation.py`

**Interfaces:**
- Produces: deterministic Memory cases through the existing offline suite and an evidence-backed release record.

- [ ] **Step 1: Add active cases for every invariant**

Cover four-channel same-Owner recall; group/non-Owner denial; explicit remember and forget across restart; secret rejection; ordinary short-term capture; repeat promotion; sensitive/rule review; conflict/supersede; Provider failure; every critical crash window; direct edit/rebuild; legacy migration; Chinese Recall@5; zero fabricated sources and duplicate Units.

- [ ] **Step 2: Run complete deterministic verification**

```bash
uv run python -m unittest discover -s tests -v
pnpm --dir tui test
pnpm --dir tui build
uv run ruff check .
uv run lobster0 eval validate --root evals/scenarios
uv run lobster0 eval run --suite offline --root evals/scenarios
uv run lobster0 eval run --suite channel --repeat 20 --json --root evals/scenarios
uv run python scripts/validate_docs.py
uv lock --check
uv build
git diff --check
```

- [ ] **Step 3: Perform a sanitized restart smoke**

With a temporary home and fake Provider: remember one ordinary fact in TUI, recall it through each private Channel fixture, restart Core, forget it, rebuild Projection, and verify all four private contexts stop recalling it. Store only case ids, counts, hashes and stable status codes.

- [ ] **Step 4: Update status only from fresh evidence**

Before completion docs must continue to say `PLANNED / NOT IMPLEMENTED`. After all gates pass, record exact commit, test counts, scenario version, Recall@5 and crash matrix; never upgrade fake/local Channel evidence to live PASS.

- [ ] **Step 5: Commit**

```bash
git add evals src/lobster0/evals docs README.md README_EN.md tests/test_eval_runner.py tests/test_documentation.py
git commit -m "release(memory): 验证 Memory Autopilot A-E"
```

## Final Verification

- [ ] TUI、Feishu、Telegram、Discord 的已验证 Owner 私聊使用同一 `owner_id` 和 Memory Space。
- [ ] 群聊、非 Owner、未知或冲突身份无法读取私人 Profile、Recall、Tool 结果或来源正文。
- [ ] 普通 Turn 不等待 extractor；明确“记住”只有原子持久化成功才返回成功。
- [ ] 密码、Token、OTP、Authorization 和私钥不进入 buffer、candidate、Markdown、FTS、日志或错误。
- [ ] 敏感、冲突、权限/行为规则必须 Review；模型不能自批。
- [ ] Markdown 是已接受语义记忆真相源；SQLite Projection 可以从 Markdown 完整重建。
- [ ] 任一 crash window 不丢 source range、不产生重复 Unit，startup 能回收过期 lease。
- [ ] Recall 严格 owner/scope/status/validity 过滤，按完整 Unit 和固定预算注入 Context。
- [ ] Direct edit、纠错、遗忘、legacy migration 和 weekly review 都有来源与审计。
- [ ] 现有 Python、TypeScript、Agent、Channel、Ruff、docs、build 和 diff gates 全部通过。
