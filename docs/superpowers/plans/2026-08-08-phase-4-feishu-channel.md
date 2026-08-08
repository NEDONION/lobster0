# Phase 4 Feishu Channel Implementation Plan

> **For MiniClaw maintainers:** Execute this plan sequentially with test-driven development. Every
> production edit must be preceded by a focused failing test. Do not stage unrelated worktree files.

**Goal:** Deliver the complete Phase 4 Feishu production Channel: private chat, allowlisted group
mentions, persistent idempotent inbox, shared Agent Core, recoverable delivery, Typing, streaming-card
fallback, approval continuation, reconnect, Gateway CLI, Doctor diagnostics, regression evidence and
up-to-date documentation.

**Architecture:** The official `lark-channel-sdk` owns Feishu transport behavior. MiniClaw owns the
durable application state: normalized inbound rows, session serialization, Agent Turns, Approval,
Delivery Outbox and audit. SQLite is the source of truth; `asyncio.Queue` is only a bounded wake-up
mechanism. CLI and Feishu enter the same `TurnService`.

**Tech Stack:** Python 3.12+, stdlib `asyncio`/`sqlite3`/`unittest`, `lark-channel-sdk` 1.2.x, existing
`httpx`, Textual, Ruff, uv.

**Authoritative design:**
`docs/superpowers/specs/2026-08-08-phase-4-feishu-channel-design.md`

**Execution status (2026-08-08):** Tasks 1–14 implementation and deterministic gates are complete.
The repository passes 382 Python tests, 25 TypeScript tests, 24/24 Agent cases and 12/12 Channel cases. Task 15 live acceptance is
pending because the local project has no Feishu App ID/App Secret and the local state has not enabled the
Channel. Task 16 may publish the implementation and honest pending status, but must not label it
production-verified until Task 15 is completed.

---

## Execution rules

1. Keep the existing unrelated `docs/README.md` and two untracked architecture documents out of every
   Phase 4 commit unless their owning task finishes and explicitly hands them over.
2. Use mixed Chinese/English commit subjects, for example
   `feat(channel): 增加 durable Feishu inbox`.
3. Run the focused RED test and record the expected failure before editing production code.
4. Use fake transport in deterministic tests. Only the explicit live-smoke step may access Feishu.
5. Never print App Secret, access token, `.env`, complete Open ID, Chat ID or raw event JSON.
6. Do not replay stale running Tools or Turns after restart.
7. Do not claim Phase 4 production-verified without the real Feishu exit gate.

## Task 1: Pin the optional official SDK and validate packaging

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `docs/superpowers/specs/2026-08-08-phase-4-feishu-channel-design.md`
- Test: `tests/test_runtime.py`

**Step 1: Write the failing packaging/import contract**

Add a test that parses `pyproject.toml` and asserts:

- `project.optional-dependencies.feishu` exists;
- it pins `lark-channel-sdk>=1.2,<2`;
- importing ordinary MiniClaw modules does not import `lark_channel` eagerly.

Run:

```bash
uv run python -m unittest tests.test_runtime.RuntimeTest.test_feishu_sdk_is_optional -v
```

Expected RED: the `feishu` extra does not exist.

**Step 2: Add the optional dependency**

Add the extra and update the design from the placeholder version to the verified PyPI range.

**Step 3: Lock and install the extra**

Run:

```bash
uv lock
uv sync --extra dev --extra feishu
```

**Step 4: Run focused GREEN and build**

```bash
uv run python -m unittest tests.test_runtime.RuntimeTest.test_feishu_sdk_is_optional -v
uv build
```

**Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/test_runtime.py \
  docs/superpowers/specs/2026-08-08-phase-4-feishu-channel-design.md
git commit -m "build(feishu): 增加 official Channel SDK optional extra"
```

## Task 2: Extend strong configuration for Feishu

**Files:**

- Modify: `src/miniclaw/config.py`
- Modify: `src/miniclaw/bootstrap.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_bootstrap.py`

**Step 1: Add RED configuration cases**

Cover:

- defaults are disabled and contain no secret value;
- the complete `[channels.feishu]` section loads;
- unknown channel/Feishu keys fail closed;
- invalid `account_id`, empty owner allowlist, group-without-chat-allowlist, queue/worker/message limits fail;
- App ID/Secret environment variable *names* validate but values do not enter `AppConfig`;
- bootstrap template contains a commented, safe Feishu example and no real credentials.

Run:

```bash
uv run python -m unittest tests.test_config tests.test_bootstrap -v
```

Expected RED: `channels` is rejected as an unknown top-level section.

**Step 2: Implement dataclasses and parsing**

Add:

- `FeishuConfig`;
- `ChannelConfig`;
- `AppConfig.channels`;
- bounded integer, ID-list and domain validation helpers.

Keep secrets outside the dataclasses. Runtime resolves the configured environment variable names only
when starting Gateway.

**Step 3: Run GREEN and Ruff**

```bash
uv run python -m unittest tests.test_config tests.test_bootstrap -v
uv run ruff check src/miniclaw/config.py src/miniclaw/bootstrap.py tests/test_config.py tests/test_bootstrap.py
```

**Step 4: Commit**

```bash
git add src/miniclaw/config.py src/miniclaw/bootstrap.py tests/test_config.py tests/test_bootstrap.py
git commit -m "feat(config): 增加 Feishu Channel typed settings"
```

## Task 3: Replace one-shot migration logic with ordered migrations

**Files:**

- Create: `src/miniclaw/storage/migrations/0002_feishu_channel.sql`
- Modify: `src/miniclaw/storage/migrations.py`
- Modify: `src/miniclaw/storage/schema.sql`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/test_doctor.py`

**Step 1: Build a real v1 fixture and write RED tests**

Assert:

- a new database applies versions `(1, 2)`;
- an existing v1 database applies only `(2,)`;
- second apply returns `()`;
- v1 data survives migration;
- new columns, constraints and indexes exist;
- a malformed v2 transaction rolls back;
- schema version 3 is rejected by this binary.

Run:

```bash
uv run python -m unittest tests.test_storage tests.test_bootstrap tests.test_doctor -v
```

Expected RED: `LATEST_SCHEMA_VERSION` is 1 and v2 columns are absent.

**Step 2: Implement ordered resources**

- Preserve `schema.sql` as migration 1.
- Load `0002_feishu_channel.sql` as migration 2.
- Apply each missing version in its own explicit `BEGIN IMMEDIATE` transaction.
- Record its version only after all statements succeed.
- Raise a version-specific, secret-free `MigrationError`.

SQLite v2 must rebuild tables where CHECK constraints change. Copy all v1 rows transactionally; do not
drop source tables until the copy succeeds.

**Step 3: Run GREEN**

```bash
uv run python -m unittest tests.test_storage tests.test_bootstrap tests.test_doctor -v
```

**Step 4: Commit**

```bash
git add src/miniclaw/storage/migrations.py src/miniclaw/storage/schema.sql \
  src/miniclaw/storage/migrations/0002_feishu_channel.sql \
  tests/test_storage.py tests/test_bootstrap.py tests/test_doctor.py
git commit -m "feat(storage): 增加 schema v2 durable Channel state"
```

## Task 4: Add Channel contracts and Feishu normalization

**Files:**

- Create: `src/miniclaw/channels/__init__.py`
- Create: `src/miniclaw/channels/base.py`
- Create: `src/miniclaw/channels/feishu.py`
- Create: `tests/fakes/fake_channel.py`
- Create: `tests/test_channel_contracts.py`
- Create: `tests/test_feishu_adapter.py`

**Step 1: Write RED contract tests**

Define tests for:

- immutable `InboundMessage` and `OutboundMessage`;
- transport protocol lifecycle and send result;
- P2P text normalization;
- allowlisted group mention normalization/removal;
- group without mention ignored;
- bot/self message ignored;
- unsupported input ignored with stable reason;
- Open ID and Chat ID denial;
- NUL/control stripping, whitespace rejection and input length limit;
- raw event and secret never appear in `repr` or error.

Run:

```bash
uv run python -m unittest tests.test_channel_contracts tests.test_feishu_adapter -v
```

Expected RED: `miniclaw.channels` does not exist.

**Step 2: Implement pure contracts and adapter**

Keep SDK objects behind a narrow mapper. The normalizer accepts a protocol-shaped view so tests do not
need `lark_channel` internals.

**Step 3: Run GREEN and Ruff**

```bash
uv run python -m unittest tests.test_channel_contracts tests.test_feishu_adapter -v
uv run ruff check src/miniclaw/channels tests/fakes/fake_channel.py \
  tests/test_channel_contracts.py tests/test_feishu_adapter.py
```

**Step 4: Commit**

```bash
git add src/miniclaw/channels tests/fakes/fake_channel.py \
  tests/test_channel_contracts.py tests/test_feishu_adapter.py
git commit -m "feat(channel): 建立 normalized Feishu message contract"
```

## Task 5: Implement identity, inbox and delivery repositories

**Files:**

- Create: `src/miniclaw/storage/channels.py`
- Create: `tests/test_channel_storage.py`
- Modify: `src/miniclaw/storage/__init__.py`

**Step 1: Write RED repository tests**

Cover:

- owner Channel identity get-or-create and conflict handling;
- first inbound insert vs duplicate `message_id`;
- different event IDs with same message ID remain duplicate;
- event ID reuse with a different message fails closed;
- ignored rows contain no full unauthorized ID;
- FIFO list/claim and conditional state transitions;
- attempt count and stable errors;
- queued and stale-running recovery;
- completed Turn lookup;
- outbox create-once, ordered part claim, retry_wait, unknown, sent and failed;
- identical idempotency key across retries;
- concurrent connections cannot claim the same row.

Run:

```bash
uv run python -m unittest tests.test_channel_storage -v
```

Expected RED: repositories do not exist.

**Step 2: Implement repositories**

Public methods use typed dataclasses and Chinese docstrings. Every state transition is a conditional SQL
update checked by `rowcount`; no read-then-write race.

**Step 3: Run GREEN**

```bash
uv run python -m unittest tests.test_channel_storage -v
```

**Step 4: Commit**

```bash
git add src/miniclaw/storage/channels.py src/miniclaw/storage/__init__.py \
  tests/test_channel_storage.py
git commit -m "feat(storage): 实现 idempotent inbox 与 Delivery outbox"
```

## Task 6: Generalize SessionRepository and TurnService

**Files:**

- Modify: `src/miniclaw/storage/conversations.py`
- Modify: `src/miniclaw/agent/turn.py`
- Modify: `tests/test_conversations.py`
- Modify: `tests/test_turn.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_tui.py`

**Step 1: Write RED generic-channel tests**

Assert:

- `SessionRepository.get_or_create()` supports arbitrary validated channel/account/conversation;
- CLI wrapper keeps the exact old behavior;
- Feishu stable inbound ID creates one Turn;
- duplicate stable ID returns/rejects deterministically without a second Provider call;
- `TurnResult` contains the persisted Assistant message ID;
- waiting approval includes approval/message identity needed by Channel;
- CLI and TUI regression behavior does not change.

Run:

```bash
uv run python -m unittest tests.test_conversations tests.test_turn tests.test_cli tests.test_tui -v
```

Expected RED: only `get_or_create_cli()` and CLI-hardcoded `handle()` exist.

**Step 2: Add generic methods and compatibility wrappers**

Do not add Feishu imports to Agent or storage conversation modules.

**Step 3: Run GREEN**

```bash
uv run python -m unittest tests.test_conversations tests.test_turn tests.test_cli tests.test_tui -v
```

**Step 4: Commit**

```bash
git add src/miniclaw/storage/conversations.py src/miniclaw/agent/turn.py \
  tests/test_conversations.py tests/test_turn.py tests/test_cli.py tests/test_tui.py
git commit -m "refactor(agent): 统一 CLI 与 Channel Turn entry"
```

## Task 7: Build the durable inbound manager and worker

**Files:**

- Create: `src/miniclaw/channels/manager.py`
- Create: `tests/test_channel_manager.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `tests/test_runtime.py`

**Step 1: Write asynchronous RED tests**

Using `unittest.IsolatedAsyncioTestCase`, cover:

- callback persists before enqueue;
- duplicate callback does not enqueue twice;
- full queue returns quickly and feeder later recovers the row;
- same conversation is serialized;
- different conversations execute up to worker limit;
- Worker uses the shared `TurnService` and creates a Delivery;
- Provider failure creates a safe failure reply;
- queued rows recover after manager restart;
- stale running Turn is interrupted and not re-executed;
- cancellation leaves unclaimed work queued.

Run:

```bash
uv run python -m unittest tests.test_channel_manager -v
```

Expected RED: `ChannelManager` does not exist.

**Step 2: Implement manager lifecycle**

The in-memory queue contains integer event row IDs. Use a bounded map of conversation locks with cleanup,
not a permanent lock per historical chat.

**Step 3: Run GREEN**

```bash
uv run python -m unittest tests.test_channel_manager tests.test_runtime -v
```

**Step 4: Commit**

```bash
git add src/miniclaw/channels/manager.py src/miniclaw/runtime.py \
  tests/test_channel_manager.py tests/test_runtime.py
git commit -m "feat(gateway): 增加 durable inbound queue 与 Worker recovery"
```

## Task 8: Implement Unicode-safe delivery, retry and recovery

**Files:**

- Create: `src/miniclaw/channels/delivery.py`
- Create: `tests/test_delivery.py`
- Modify: `tests/fakes/fake_channel.py`

**Step 1: Write RED delivery tests**

Cover:

- paragraph/newline/Unicode splitting under the configured limit;
- `[i/n]` prefix included in the budget;
- emoji and CJK are never corrupted;
- all parts persist before first network send;
- parts send sequentially;
- a middle permanent failure blocks later parts;
- retryable connection/429/5xx uses bounded backoff;
- permission/invalid request fails permanently;
- timeout becomes unknown;
- same idempotency UUID is reused;
- restart recovers queued/retry_wait/sending rows correctly;
- safe error detail is bounded and redacted.

Run:

```bash
uv run python -m unittest tests.test_delivery -v
```

Expected RED: delivery module does not exist.

**Step 2: Implement splitter, classifier and DeliveryWorker**

Inject clock and sleep functions for deterministic tests. No real sleep in unit tests.

**Step 3: Run GREEN**

```bash
uv run python -m unittest tests.test_delivery tests.test_channel_storage -v
```

**Step 4: Commit**

```bash
git add src/miniclaw/channels/delivery.py tests/test_delivery.py tests/fakes/fake_channel.py
git commit -m "feat(delivery): 实现 Feishu split retry 与 outbox recovery"
```

## Task 9: Integrate the official Feishu transport

**Files:**

- Modify: `src/miniclaw/channels/feishu.py`
- Create: `tests/test_feishu_transport.py`
- Modify: `tests/fakes/fake_channel.py`

**Step 1: Write RED SDK boundary tests**

Patch only the SDK constructor/facade and assert:

- credentials are passed but never retained in public repr/log objects;
- strict security config, DM allowlist and group policy are explicit;
- connect-until-ready, callbacks and disconnect lifecycle;
- normalized inbound mapping;
- reply uses `reply_to` and stable UUID;
- Typing add/remove;
- card create/update; and
- SDK exceptions map to stable MiniClaw error codes.

Run:

```bash
uv run python -m unittest tests.test_feishu_transport -v
```

Expected RED: adapter has no official transport.

**Step 2: Implement lazy SDK facade**

Import `lark_channel` only inside the transport constructor/factory so non-Feishu commands still work
without the extra installed.

**Step 3: Run GREEN against installed SDK**

```bash
uv run python -m unittest tests.test_feishu_transport tests.test_feishu_adapter -v
```

**Step 4: Commit**

```bash
git add src/miniclaw/channels/feishu.py tests/test_feishu_transport.py \
  tests/fakes/fake_channel.py
git commit -m "feat(feishu): 接入 official WebSocket transport"
```

## Task 10: Add Typing and streaming-card fallback

**Files:**

- Create: `src/miniclaw/channels/capabilities.py`
- Modify: `src/miniclaw/channels/manager.py`
- Modify: `src/miniclaw/channels/delivery.py`
- Create: `tests/test_channel_capabilities.py`
- Modify: `tests/test_channel_manager.py`

**Step 1: Write RED capability tests**

Cover:

- Typing begins after claim and always ends best-effort;
- Typing error does not fail the Turn;
- visible text deltas coalesce and respect update interval;
- reasoning and unsafe trace details never enter the card;
- card create/update failure supersedes card Delivery and sends final Markdown;
- disabled streaming sends only one final message;
- partial Provider failure visibly marks incomplete content.

Run:

```bash
uv run python -m unittest tests.test_channel_capabilities tests.test_channel_manager -v
```

Expected RED: capability orchestrator does not exist.

**Step 2: Implement capability policy**

Keep platform capabilities outside AgentRunner. Consume only existing public `RunEvent` data.

**Step 3: Run GREEN**

```bash
uv run python -m unittest tests.test_channel_capabilities tests.test_channel_manager \
  tests.test_delivery -v
```

**Step 4: Commit**

```bash
git add src/miniclaw/channels/capabilities.py src/miniclaw/channels/manager.py \
  src/miniclaw/channels/delivery.py tests/test_channel_capabilities.py \
  tests/test_channel_manager.py
git commit -m "feat(feishu): 增加 Typing 与 streaming card fallback"
```

## Task 11: Complete Approval cards and text fallback

**Files:**

- Create: `src/miniclaw/channels/approvals.py`
- Modify: `src/miniclaw/channels/manager.py`
- Modify: `src/miniclaw/channels/feishu.py`
- Create: `tests/test_channel_approvals.py`
- Modify: `tests/test_approvals.py`

**Step 1: Write RED approval-channel tests**

Cover:

- waiting Turn builds a redacted card from Core fields;
- only Core-provided grant modes become buttons;
- only Owner Open ID can decide;
- approve once/session/always and deny;
- file write never shows session/always;
- changed argument hash fails closed;
- expired and repeated action is idempotent;
- approved action creates exactly one continuation Turn;
- `/approve <id> <mode>` and `/deny <id>` bypass the model;
- malformed commands show safe usage without Provider call;
- card callback failure leaves text fallback usable.

Run:

```bash
uv run python -m unittest tests.test_channel_approvals -v
```

Expected RED: Channel approval handler does not exist.

**Step 2: Implement a thin presentation/controller layer**

Call existing `ApprovalRepository` / `TurnService`; do not duplicate approval state transitions.

**Step 3: Run GREEN**

```bash
uv run python -m unittest tests.test_channel_approvals tests.test_approvals \
  tests.test_turn -v
```

**Step 4: Commit**

```bash
git add src/miniclaw/channels/approvals.py src/miniclaw/channels/manager.py \
  src/miniclaw/channels/feishu.py tests/test_channel_approvals.py tests/test_approvals.py
git commit -m "feat(approval): 打通 Feishu card 与 continuation flow"
```

## Task 12: Add Gateway CLI, signals and Doctor checks

**Files:**

- Create: `src/miniclaw/gateway.py`
- Modify: `src/miniclaw/cli.py`
- Modify: `src/miniclaw/doctor.py`
- Create: `tests/test_gateway.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_doctor.py`
- Modify: `tests/test_env.py`

**Step 1: Write RED command/lifecycle tests**

Assert:

- parser exposes `miniclaw gateway`;
- disabled/misconfigured/missing SDK/missing credential fails before network;
- credential values do not appear in exceptions or captured output;
- successful startup uses one AgentRuntime and prints a redacted ready line;
- SIGINT/SIGTERM calls stop/drain/disconnect in order;
- second signal forces bounded cancellation, not `kill -9`;
- Doctor reports `feishu_config`, `feishu_sdk`, `feishu_database`, `feishu_runtime`;
- ordinary Doctor does no Feishu network call;
- exit codes are stable.

Run:

```bash
uv run python -m unittest tests.test_gateway tests.test_cli tests.test_doctor tests.test_env -v
```

Expected RED: Gateway command/module is absent.

**Step 2: Implement command and lifecycle**

Use `asyncio.Runner` or `asyncio.run`; register signals only on the main thread and provide a testable
shutdown event injection path.

**Step 3: Run GREEN and local CLI checks**

```bash
uv run python -m unittest tests.test_gateway tests.test_cli tests.test_doctor tests.test_env -v
uv run miniclaw gateway --help
uv run miniclaw doctor
```

**Step 4: Commit**

```bash
git add src/miniclaw/gateway.py src/miniclaw/cli.py src/miniclaw/doctor.py \
  tests/test_gateway.py tests/test_cli.py tests/test_doctor.py tests/test_env.py
git commit -m "feat(cli): 增加 miniclaw gateway 与 Feishu doctor"
```

## Task 13: Add R4 regression suite and live-smoke harness

**Files:**

- Modify: `src/miniclaw/evals/cases.py`
- Modify: `src/miniclaw/evals/runner.py`
- Create: `evals/cases/feishu-channel.jsonl`
- Create: `scripts/feishu_live_smoke.py`
- Create: `tests/test_feishu_evals.py`
- Modify: `tests/test_eval_cases.py`
- Modify: `tests/test_eval_runner.py`

**Step 1: Write RED R4 loader/runner tests**

Add the twelve Channel cases from the design and deterministic assertions over inbox, Turn, ToolRun,
Approval, Delivery and Audit evidence.

Run:

```bash
uv run python -m unittest tests.test_feishu_evals tests.test_eval_cases tests.test_eval_runner -v
```

Expected RED: Channel case schema/runner support and cases are absent.

**Step 2: Implement deterministic runner and safe live harness**

The live harness must:

- require an explicit `--confirm-live` flag;
- never print secrets or full IDs;
- record commit and case results;
- support a human sending inbound DM/group messages;
- produce JSON evidence under an ignored local results directory;
- not send arbitrary contacts messages by default.

**Step 3: Run GREEN**

```bash
uv run python -m unittest tests.test_feishu_evals tests.test_eval_cases tests.test_eval_runner -v
uv run miniclaw eval --suite all
```

**Step 4: Commit**

```bash
git add src/miniclaw/evals evals/cases/feishu-channel.jsonl \
  scripts/feishu_live_smoke.py tests/test_feishu_evals.py \
  tests/test_eval_cases.py tests/test_eval_runner.py
git commit -m "test(feishu): 增加 R4 regression 与 live smoke harness"
```

## Task 14: Complete engineering, operation and user documentation

**Files:**

- Create: `docs/engineering/phase-4/feishu-channel.md`
- Create: `docs/engineering/phase-4/testing-and-operations.md`
- Modify: `docs/product/20260807_产品需求文档.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Modify: `README.md`
- Modify: `.env.example` if present
- Modify: `docs/progress/index.html`
- Modify outside repo: `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html`

**Step 1: Write a documentation truth checklist**

Before editing, derive actual test counts, commands, config names, schema version and capabilities from the
current code. Do not copy planned values.

**Step 2: Write the two Phase 4 engineering documents**

Use plain Chinese, Mermaid architecture/sequence/state diagrams, code paths, normal flow, failure flow,
debug commands and common-error tables. Clearly separate deterministic evidence from real-live evidence.

**Step 3: Synchronize every status surface**

Update SDK name, `message_id` idempotency, Phase status, test counts, version/commit and next phase. Preserve
unrelated documentation edits; if the same index file overlaps, merge only the Phase 4 additions.

**Step 4: Validate docs**

Run the repository’s link/Mermaid/HTML checks. If no script exists, add a small deterministic verifier under
`tests/` and test relative file links, Mermaid fence balance and required progress facts.

**Step 5: Commit**

```bash
git add README.md .env.example docs/product docs/architecture/20260807_系统架构.md \
  docs/engineering docs/getting-started docs/progress/index.html
git commit -m "docs(phase4): 同步 Feishu operations 与项目进度"
```

The external progress HTML is not part of the Git commit; verify it separately and report its absolute path.

## Task 15: Real Feishu acceptance

**Preconditions:**

- Bot App ID / App Secret are present in local `.env`;
- app enables `im.message.receive_v1` and required bot scopes;
- Owner Open ID and test Chat ID are configured;
- user explicitly runs the live harness and sends the instructed messages.

**Step 1: Static and auth diagnosis**

```bash
uv run miniclaw doctor
lark-cli auth status --json --verify
lark-cli event schema im.message.receive_v1 --json
```

Capture only redacted status. Never copy auth JSON verbatim into docs.

**Step 2: Start the real Gateway**

```bash
uv run miniclaw gateway
```

Wait for a redacted ready marker.

**Step 3: Execute the live matrix**

Run private 20-turn, group mention/non-mention, read Tool, approval approve/deny, duplicate event,
long-message split, card fallback, restart memory/inbox and transport reconnect cases.

**Step 4: Record release evidence**

Create `docs/evals/releases/v0.4.0.md` with commit, timestamp, case counts, safe environment summary,
failures/workarounds and reviewer. No personal IDs or secrets.

**Step 5: Commit the evidence**

```bash
git add docs/evals/releases/v0.4.0.md
git commit -m "test(release): 记录 Phase4 Feishu live acceptance"
```

## Task 16: Full completion audit, merge and push

**Step 1: Run fresh full gates**

```bash
uv sync --extra dev --extra feishu
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run miniclaw eval --suite all
uv run miniclaw doctor
uv build
git diff --check origin/main...HEAD
```

Also run docs link/Mermaid/HTML validation, secret scan and a real PTY Gateway start/stop smoke.

**Step 2: Audit every Phase 4 requirement**

Create a requirement-to-evidence table covering all design Section 4 and Section 22 items. “No failing test”
is not evidence for an untested capability.

**Step 3: Inspect Git scope**

```bash
git status --short
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Confirm unrelated user/other-agent files are neither staged nor committed.

**Step 4: Merge if a feature branch was used**

Use a non-destructive merge with a mixed-language subject. Do not rewrite unrelated history.

**Step 5: Push and verify remote**

```bash
git push origin main
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

The two hashes must match before reporting completion.
