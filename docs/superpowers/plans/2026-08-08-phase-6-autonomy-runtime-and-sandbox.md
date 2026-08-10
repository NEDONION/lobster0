# Lobster0 Phase 6 Autonomy Runtime and Sandbox Implementation Plan

> **SUPERSEDED / 历史计划：** 当前实施以
> [`2026-08-09-phase-6-autonomy-sandbox.md`](2026-08-09-phase-6-autonomy-sandbox.md) 为准。
> 本文保留作设计演进记录，不得按其中旧 schema 编号、旧接口或旧完成条件开发。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付重启可恢复的定时/后台任务、Heartbeat、主动投递、任务预算、OS Sandbox 和工作区 Checkpoint，使 Lobster0 能在无人盯屏时安全工作。

**Architecture:** Scheduler 只负责确定到期时间并原子创建 `task_runs`；TaskRunner 使用现有 `TurnService` 执行隔离 Session；所有 Tool 继续经过 `PolicyEngine` 和 `ToolExecutor`。命令副作用先生成 canonical `ExecutionPlan`，审批绑定 plan hash，再由 host/docker/seatbelt backend 执行并产生 receipt。

**Tech Stack:** Python 3.12、SQLite、stdlib `zoneinfo`、`croniter`（仅解析 cron）、asyncio、Docker CLI、macOS Seatbelt、现有 Channel Delivery 和 unittest。

## Global Constraints

- Scheduler 不直接调用 Provider 或 Tool。
- 同一个 `task_id + scheduled_for` 最多产生一个 `task_run`。
- Cron/Heartbeat 不能自动批准等待中的危险动作。
- 自动任务的权限不得高于创建者和配置 Profile 的交集。
- 所有任务都有 wall-clock、Tool、Turn、Token 和并发上限。
- Sandbox 不替代 Core Policy；`yolo` 也不能关闭资源和敏感路径硬边界。
- 任务正文、网页正文和用户消息不写入普通日志。
- 新配置默认关闭，现有安装升级后行为不变。

---

### Task 1: Strongly typed automation configuration

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/lobster0/config.py`
- Modify: `src/lobster0/bootstrap.py`
- Test: `tests/test_config.py`
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Produces: `AutomationConfig`, `HeartbeatConfig`, `SandboxConfig`, `CheckpointConfig` on `AppConfig`.

- [ ] **Step 1: Write failing strict-config tests**

```python
def test_automation_defaults_off_and_rejects_unknown_keys(self, paths):
    config = load_config(paths, env())
    assert config.automation.enabled is False
    write_config(paths, "[automation]\nenabled=false\nmystery=true\n")
    with self.assertRaisesRegex(ConfigError, "unknown configuration key"):
        load_config(paths, env())

def test_sandbox_limits_are_bounded(self, paths):
    write_config(paths, "[sandbox]\nmemory_mib=0\n")
    with self.assertRaisesRegex(ConfigError, "sandbox.memory_mib"):
        load_config(paths, env())
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_config tests.test_bootstrap -v`
Expected: `AppConfig` has no `automation`/`sandbox` fields.

- [ ] **Step 3: Implement config dataclasses and parser**

```python
@dataclass(frozen=True, slots=True)
class AutomationConfig:
    enabled: bool = False
    max_active_tasks: int = 50
    max_concurrent_runs: int = 2
    misfire_grace_seconds: int = 300

@dataclass(frozen=True, slots=True)
class SandboxConfig:
    backend: str = "host"
    network: str = "none"
    memory_mib: int = 512
    cpu_seconds: int = 60
```

Add `croniter>=6,<7` and keep timezone parsing in stdlib `zoneinfo`. The bootstrap template documents values but leaves automation disabled.

- [ ] **Step 4: Run config/bootstrap tests**

Run: `uv run python -m unittest tests.test_config tests.test_bootstrap -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/lobster0/config.py src/lobster0/bootstrap.py tests/test_config.py tests/test_bootstrap.py
git commit -m "feat(automation): define strict autonomy configuration"
```

### Task 2: Task schema and repository state machine

**Files:**
- Create: `src/lobster0/storage/migrations/0004_autonomy.sql`
- Modify: `src/lobster0/storage/migrations.py`
- Create: `src/lobster0/automation/__init__.py`
- Create: `src/lobster0/automation/models.py`
- Create: `src/lobster0/automation/repository.py`
- Test: `tests/test_automation_repository.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `ScheduledTaskRepository.create/list/pause/resume/cancel`, `TaskRunRepository.claim_due/mark_*`, immutable `ScheduledTask` and `TaskRun`.

- [ ] **Step 1: Write failing migration and single-claim tests**

```python
def test_same_schedule_slot_creates_one_run(database):
    task = tasks.create(valid_task())
    first = runs.enqueue(task, scheduled_for=fixed_time)
    second = runs.enqueue(task, scheduled_for=fixed_time)
    assert first.id == second.id

def test_two_workers_cannot_claim_same_run(database):
    run = runs.enqueue(task, scheduled_for=fixed_time)
    assert runs.claim_next("worker-a").id == run.id
    assert runs.claim_next("worker-b") is None
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_automation_repository tests.test_storage -v`
Expected: migration version remains 2 and repository imports fail.

- [ ] **Step 3: Add migration and transactional repositories**

Use the exact `scheduled_tasks` and `task_runs` schema from the engineering roadmap. Claim with `BEGIN IMMEDIATE`, immutable run snapshot JSON, a lease timestamp, and a unique idempotency key derived from canonical task id and UTC scheduled time.

- [ ] **Step 4: Cover all legal and illegal transitions**

Run: `uv run python -m unittest tests.test_automation_repository tests.test_storage -v`
Expected: PASS for pause/resume/cancel, stale lease, interrupted recovery, waiting approval, terminal immutability, malformed JSON and concurrent claim.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/storage/migrations.py src/lobster0/storage/migrations/0004_autonomy.sql src/lobster0/automation tests/test_automation_repository.py tests/test_storage.py
git commit -m "feat(automation): persist scheduled tasks and runs"
```

### Task 3: Schedule parser with timezone and misfire rules

**Files:**
- Create: `src/lobster0/automation/parser.py`
- Test: `tests/test_schedule_parser.py`

**Interfaces:**
- Produces: `parse_schedule(raw, now) -> ScheduleSpec` and `next_occurrence(spec, after) -> datetime | None`.

- [ ] **Step 1: Write failing deterministic parser tests**

```python
def test_interval_and_cron_use_explicit_timezone():
    spec = parse_schedule({"kind": "cron", "expression": "0 9 * * 1-5", "timezone": "Asia/Shanghai"}, now)
    assert next_occurrence(spec, friday_utc) == monday_9am_shanghai_utc

def test_dst_gap_has_one_documented_result():
    spec = parse_schedule({"kind": "cron", "expression": "30 2 * * *", "timezone": "America/New_York"}, before_dst)
    assert next_occurrence(spec, before_dst) == expected_after_gap
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_schedule_parser -v`
Expected: module missing.

- [ ] **Step 3: Implement only four schedule kinds**

Support `once`, `interval`, five-field `cron`, and internal `heartbeat`. Reject seconds fields, year fields, aliases with ambiguous locale, non-IANA timezone, intervals below 60 seconds, and timestamps in the past outside the misfire grace.

- [ ] **Step 4: Run parser tests**

Run: `uv run python -m unittest tests.test_schedule_parser -v`
Expected: PASS for leap day, DST, invalid zones, huge intervals and deterministic UTC normalization.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/automation/parser.py tests/test_schedule_parser.py
git commit -m "feat(automation): parse bounded schedules and timezones"
```

### Task 4: Scheduler and restart recovery

**Files:**
- Create: `src/lobster0/automation/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: task repositories, monotonic/UTC clocks and wake event.
- Produces: due `task_runs`; never calls Provider, Channel or Tool.

- [ ] **Step 1: Write failing due/misfire tests**

```python
async def test_scheduler_enqueues_due_run_once():
    await scheduler.tick(now=fixed_now)
    await scheduler.tick(now=fixed_now)
    assert runs.count() == 1

async def test_restart_skips_unbounded_backlog():
    await scheduler.tick(now=one_year_later)
    assert runs.count_for(task.id) == 1
    assert tasks.get(task.id).next_run_at > one_year_later
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_scheduler -v`
Expected: missing scheduler.

- [ ] **Step 3: Implement bounded tick and wake loop**

Tick claims no work itself. It scans a limited due window, inserts at most one catch-up run per task, advances `next_run_at` transactionally, and sleeps until the nearest due time or config wake event.

- [ ] **Step 4: Verify recovery and cancellation races**

Run: `uv run python -m unittest tests.test_scheduler tests.test_automation_repository -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/automation/scheduler.py tests/test_scheduler.py
git commit -m "feat(automation): enqueue durable due runs"
```

### Task 5: TaskRunner through existing TurnService

**Files:**
- Create: `src/lobster0/automation/runner.py`
- Modify: `src/lobster0/agent/turn.py`
- Modify: `src/lobster0/runtime.py`
- Test: `tests/test_task_runner.py`
- Test: `tests/test_turn.py`

**Interfaces:**
- Produces: `TurnService.start_background_turn(...)` with explicit session, source, runtime snapshot and budget; `TaskRunner.run_once()`.

- [ ] **Step 1: Write failing runtime-reuse tests**

```python
async def test_task_runner_uses_same_policy_and_tool_executor(runtime):
    result = await task_runner.run_once()
    assert result.turn_id is not None
    assert tool_executor.calls[0].context.trusted_owner is True
    assert runs.get(result.run_id).status == "succeeded"

async def test_waiting_approval_stays_waiting_after_restart():
    result = await task_runner.run_once()
    assert result.status == "waiting_approval"
    recovered = TaskRunner(...).recover()
    assert recovered.status == "waiting_approval"
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_task_runner tests.test_turn -v`
Expected: no background-turn interface.

- [ ] **Step 3: Implement immutable run execution**

Create an isolated `automation` session keyed by task/run, append one user-equivalent system-owned instruction with provenance, and use existing `TurnService`. Count Tool calls from persisted `tool_runs`; enforce wall-clock cancellation; write safe usage and error codes to `task_runs`.

- [ ] **Step 4: Run focused runtime tests**

Run: `uv run python -m unittest tests.test_task_runner tests.test_turn tests.test_runtime tests.test_tool_executor -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/automation/runner.py src/lobster0/agent/turn.py src/lobster0/runtime.py tests/test_task_runner.py tests/test_turn.py
git commit -m "feat(automation): execute tasks through the core runtime"
```

### Task 6: Durable proactive delivery and silence contract

**Files:**
- Modify: `src/lobster0/channels/delivery.py`
- Modify: `src/lobster0/storage/channels.py`
- Create: `src/lobster0/automation/delivery.py`
- Test: `tests/test_task_delivery.py`
- Test: `tests/test_delivery.py`

**Interfaces:**
- Consumes: terminal TaskRun and validated `DeliveryTarget`.
- Produces: existing `deliveries` rows with deterministic task delivery idempotency.

- [ ] **Step 1: Write failing delivery tests**

```python
def test_notify_false_creates_no_delivery_but_keeps_run_result():
    projector.project(run, response={"notify": False})
    assert deliveries.count() == 0
    assert runs.get(run.id).result_preview

def test_recovery_does_not_send_task_result_twice():
    projector.project(run, response={"notify": True, "text": "done"})
    projector.project(run, response={"notify": True, "text": "done"})
    assert deliveries.count() == 1
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_task_delivery tests.test_delivery -v`
Expected: missing projector.

- [ ] **Step 3: Implement structured delivery projection**

Do not parse magic silence strings from ordinary model prose. The background runtime requests a structured `notify` decision; malformed responses default to a safe failure notification, not silence. Reuse Channel message splitting and DeliveryWorker.

- [ ] **Step 4: Verify all three channel projections**

Run: `uv run python -m unittest tests.test_task_delivery tests.test_delivery tests.test_channel_contracts -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/automation/delivery.py src/lobster0/channels/delivery.py src/lobster0/storage/channels.py tests/test_task_delivery.py tests/test_delivery.py
git commit -m "feat(automation): deliver background results durably"
```

### Task 7: Heartbeat and manage_task Tool

**Files:**
- Create: `src/lobster0/automation/heartbeat.py`
- Create: `src/lobster0/tools/automation.py`
- Modify: `src/lobster0/runtime.py`
- Test: `tests/test_heartbeat.py`
- Test: `tests/test_automation_tool.py`
- Modify: `evals/scenarios/personal.v1.jsonl`

**Interfaces:**
- Produces: internal heartbeat task reconciliation and public `manage_task` action-style Tool.

- [ ] **Step 1: Write failing Tool and active-hours tests**

```python
def test_manage_task_create_requires_complete_self_contained_prompt(self):
    with self.assertRaisesRegex(ToolValidationError, "prompt"):
        tool.validate({"action": "create", "schedule": valid_schedule, "prompt": "查一下那个事情"})

def test_heartbeat_outside_active_hours_does_not_create_run():
    heartbeat.reconcile(now=midnight)
    assert runs.count() == 0
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_heartbeat tests.test_automation_tool -v`
Expected: missing modules/tool.

- [ ] **Step 3: Implement validation, Policy risk and reconciliation**

`list` is low risk; create/update/pause/resume/cancel require policy evaluation; tasks requesting write/command/browser abilities cannot exceed configured profile. Heartbeat is a system-owned scheduled task and is reconciled from config, not edited as a normal user task.

- [ ] **Step 4: Add Agent eval cases**

Add active cases for “明早九点提醒我”、recurring task creation, cancellation, ambiguous timezone clarification, and refusing an over-privileged unattended task. Run the offline suite and require all existing cases to remain green.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/automation/heartbeat.py src/lobster0/tools/automation.py src/lobster0/runtime.py tests/test_heartbeat.py tests/test_automation_tool.py evals/scenarios/personal.v1.jsonl
git commit -m "feat(automation): expose scheduled tasks and heartbeats"
```

### Task 8: Canonical execution plan and sandbox protocol

**Files:**
- Create: `src/lobster0/sandbox/__init__.py`
- Create: `src/lobster0/sandbox/base.py`
- Create: `src/lobster0/sandbox/host.py`
- Modify: `src/lobster0/tools/command.py`
- Modify: `src/lobster0/tools/executor.py`
- Test: `tests/test_sandbox_contract.py`
- Test: `tests/test_run_command.py`

**Interfaces:**
- Produces: `ExecutionPlan`, `ExecutionReceipt`, `SandboxBackend.execute(plan)`; Approval binds `ExecutionPlan.sha256`.

- [ ] **Step 1: Write failing plan binding tests**

```python
def test_plan_hash_changes_when_cwd_or_environment_changes():
    assert plan(argv=("git", "status"), cwd=a).sha256 != plan(argv=("git", "status"), cwd=b).sha256

async def test_executor_uses_approved_plan_not_mutated_arguments():
    approved = build_plan(("tool", "safe"))
    await executor.resume(approval, supplied_arguments={"args": ["dangerous"]})
    assert backend.seen_plan == approved
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_sandbox_contract tests.test_run_command tests.test_tool_executor -v`
Expected: missing sandbox contract.

- [ ] **Step 3: Adapt existing host execution behind the protocol**

Move subprocess behavior without changing existing exact-argv, PATH, environment, output and timeout semantics. Persist the plan hash before approval; persist a bounded receipt after execution.

- [ ] **Step 4: Run command/policy/approval regression**

Run: `uv run python -m unittest tests.test_sandbox_contract tests.test_run_command tests.test_tool_executor tests.test_approvals -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/sandbox src/lobster0/tools/command.py src/lobster0/tools/executor.py tests/test_sandbox_contract.py tests/test_run_command.py tests/test_tool_executor.py
git commit -m "refactor(sandbox): bind command approval to execution plans"
```

### Task 9: Docker and Seatbelt backends

**Files:**
- Create: `src/lobster0/sandbox/docker.py`
- Create: `src/lobster0/sandbox/seatbelt.py`
- Modify: `src/lobster0/doctor.py`
- Test: `tests/test_docker_sandbox.py`
- Test: `tests/test_seatbelt_sandbox.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: canonical `ExecutionPlan`.
- Produces: `ExecutionReceipt`; unavailable backend fails before side effects.

- [ ] **Step 1: Write failing exact-argv backend tests**

```python
async def test_docker_backend_mounts_only_declared_roots():
    await backend.execute(plan(read_roots=(read_root,), write_roots=(write_root,)))
    argv = runner.calls[0]
    assert str(read_root) in argv and f"{read_root}:ro" in argv
    assert str(Path.home()) not in argv

async def test_seatbelt_profile_denies_network_when_configured():
    await backend.execute(plan(network_mode="none"))
    assert "network*" in generated_profile
    assert "deny" in generated_profile
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_docker_sandbox tests.test_seatbelt_sandbox -v`
Expected: missing backends.

- [ ] **Step 3: Implement deterministic adapters**

Docker uses `--network none`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, fixed non-root uid, pids/memory/cpu limits and explicit mounts. Seatbelt generates a private profile with literal escaped paths and runs `sandbox-exec` via exact argv. Unsupported platforms fail closed.

- [ ] **Step 4: Run contract tests and optional live containment smoke**

Run deterministic fake-runner tests in CI. On release candidates, execute real attempts to read a denied file, write outside the root, open a denied network socket and exceed CPU/time limits; all must fail without host damage.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/sandbox/docker.py src/lobster0/sandbox/seatbelt.py src/lobster0/doctor.py tests/test_docker_sandbox.py tests/test_seatbelt_sandbox.py tests/test_doctor.py
git commit -m "feat(sandbox): add docker and seatbelt execution backends"
```

### Task 10: Checkpoint and conflict-aware rollback

**Files:**
- Create: `src/lobster0/checkpoints/__init__.py`
- Create: `src/lobster0/checkpoints/store.py`
- Create: `src/lobster0/checkpoints/rollback.py`
- Modify: `src/lobster0/tools/filesystem.py`
- Test: `tests/test_checkpoints.py`
- Test: `tests/test_file_tools.py`

**Interfaces:**
- Produces: `CheckpointStore.capture(paths, source_turn_id)`, `RollbackService.preview(id)`, `apply(id, expected_preview_hash)`.

- [ ] **Step 1: Write failing capture and conflict tests**

```python
def test_checkpoint_captures_only_files_about_to_change(tmp_path):
    checkpoint = store.capture((target,), source_turn_id=1)
    assert checkpoint.entries == (entry_for(target),)

def test_rollback_refuses_user_changes_after_tool(self, tmp_path):
    checkpoint = store.capture((target,), source_turn_id=1)
    target.write_text("user edit")
    with self.assertRaisesRegex(CheckpointError, "checkpoint conflict"):
        rollback.apply(checkpoint.id, expected_preview_hash=rollback.preview(checkpoint.id).hash)
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_checkpoints tests.test_file_tools -v`
Expected: missing checkpoint modules.

- [ ] **Step 3: Implement bounded content-addressed snapshots**

Capture only regular files under write roots; reject symlinks; exclude `.git`, state, DB, socket, logs and credentials; cap entry count and total bytes. Save manifest mode `0600`; rollback uses preview hash, current-file hash checks and atomic replace.

- [ ] **Step 4: Integrate before write/edit and verify**

Run: `uv run python -m unittest tests.test_checkpoints tests.test_file_tools tests.test_workspace_policy -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/checkpoints src/lobster0/tools/filesystem.py tests/test_checkpoints.py tests/test_file_tools.py
git commit -m "feat(checkpoint): snapshot and safely roll back file edits"
```

### Task 11: Gateway lifecycle, scenarios and Phase 6 release record

**Files:**
- Modify: `src/lobster0/gateway.py`
- Modify: `src/lobster0/doctor.py`
- Modify: `evals/scenarios/personal.v1.jsonl`
- Create: `evals/scenarios/automation.v1.jsonl`
- Create: `docs/engineering/phase-6/autonomy-runtime-and-sandbox.md`
- Create: `docs/evals/releases/v0.6.0.md`
- Modify: `README.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Gateway owns scheduler/task workers and stops them before Channel/Runtime shutdown.

- [ ] **Step 1: Write failing lifecycle tests**

Verify start order `runtime → delivery → task workers → scheduler → transports`, and stop order `stop triggers → drain tasks → delivery → runtime`. Second signal cancels bounded cleanup without corrupting claimed rows.

- [ ] **Step 2: Implement lifecycle wiring and Doctor checks**

Doctor reports schema, enabled state, due backlog, stale claim count, selected sandbox backend and dependency availability without triggering jobs.

- [ ] **Step 3: Add scenario gates**

Cover create/list/pause/resume/cancel, once/interval/cron, timezone clarification, duplicate tick, restart, waiting approval, silent heartbeat, proactive Feishu delivery, budget exhaustion, denied sandbox path/network and rollback conflict.

- [ ] **Step 4: Run full deterministic verification**

```bash
uv run python -m unittest discover -s tests -v
pnpm --dir tui test
pnpm --dir tui build
uv run ruff check .
uv run lobster0 eval validate --root evals/scenarios
uv run lobster0 eval run --suite offline --root evals/scenarios
uv run lobster0 eval run --suite channel --root evals/scenarios
uv run python scripts/validate_docs.py
uv lock --check
uv build
git diff --check
```

- [ ] **Step 5: Run release-only live gates**

Run one one-shot and one recurring real task through Feishu, restart during a read-only task, exercise a waiting Approval, verify a silent Heartbeat, and execute Docker/Seatbelt containment probes. Store only redacted evidence.

- [ ] **Step 6: Commit verified facts**

```bash
git add src/lobster0/gateway.py src/lobster0/doctor.py evals docs README.md
git commit -m "release(v0.6.0): deliver durable autonomous tasks"
```

## Final verification

- [ ] All prior Phase 0-5 tests and evals remain green.
- [ ] Scheduler duplicate/misfire/restart tests pass.
- [ ] Task waiting Approval survives restart without auto-approval.
- [ ] Proactive Delivery is durable and idempotent.
- [ ] Real Sandbox containment smoke passes.
- [ ] Checkpoint rollback refuses concurrent user edits.
- [ ] No Secret appears in Task, Receipt, logs or Evidence.
