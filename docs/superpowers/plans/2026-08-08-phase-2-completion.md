# Lobster0 Phase 2 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已完成 P2.1A/P2.1B/P2.1C 的基础上，交付参数绑定审批、安全写文件、受限命令、HTTPS/SSRF 防护和 Phase 2 完整交付门禁。

**Architecture:** 保留唯一执行链 `AgentRunner -> ToolExecutor -> PolicyEngine -> Tool`。有副作用的调用由 `ToolExecutor` 先持久化为 waiting Approval，当前 Turn 结束；CLI 决策后创建 child Turn，仅消费绑定 ToolRun 一次，再把 Tool Result 交给模型继续。命令和网络共用 Approval 生命周期，但各自拥有最小的参数规范化与硬禁止策略。

**Tech Stack:** Python 3.12+、标准库、现有 `httpx`、SQLite、`argparse`、`unittest`、Ruff；不新增依赖、ORM、插件框架、通用 Policy DSL 或 Shell 字符串执行。

## Global Constraints

- 默认 `allowlist + on-miss`：安全只读自动放行，写入、命令和网络默认审批。
- Approval 使用 canonical JSON + SHA-256，TTL 默认 600 秒，只能原子消费一次。
- 文件写入仅允许 Workspace；敏感路径、read-only roots、symlink 逃逸必须硬拒绝。
- `run_command` 只接受 `program + args`，固定 Workspace cwd，`shell=False`，不继承 Key/Token/Secret。
- `http_get` 只支持 HTTPS GET；每次解析、连接和重定向都拒绝非公网地址。
- 任何有副作用的生产逻辑必须先看到对应测试 RED，再写最小 GREEN。
- 每个切片更新工程事实文档、README、`docs/progress/index.html` 和回归记录。
- Commit subject 约一半中文、一半 English，例如 `feat(approval): 完成 parameter-bound 审批续执行`。
- 不修改或提交现有未跟踪文件 `docs/superpowers/specs/2026-08-08-gemini-style-tui-and-lark-cli-design.md`。

---

## File Map

- `src/lobster0/config.py`：严格解析 `[tools]`、command/http 子配置。
- `src/lobster0/policy/workspace.py`：新增写路径解析，保持 read/write 授权分离。
- `src/lobster0/policy/engine.py`：硬禁止、内置低风险、exact rule、默认审批的唯一决策入口。
- `src/lobster0/policy/approvals.py`：Approval 业务状态、参数绑定、CLI 决策与续执行编排。
- `src/lobster0/policy/command.py`：程序解析、禁止项和 exact argv 规范化。
- `src/lobster0/policy/network.py`：HTTPS URL、DNS/IP、redirect 目标校验。
- `src/lobster0/tools/filesystem.py`：`write_file`、`edit_file` 原子写入。
- `src/lobster0/tools/command.py`：无 Shell 的受限子进程。
- `src/lobster0/tools/web.py`：固定 DNS/peer 的有限 HTTPS 文本读取。
- `src/lobster0/tools/executor.py`：创建 waiting Approval 或执行已允许/已消费调用。
- `src/lobster0/storage/tooling.py`：ToolRun、Approval、PolicyRule、Audit 的条件更新事务。
- `src/lobster0/storage/conversations.py`：waiting Turn 与 child continuation Turn。
- `src/lobster0/agent/runner.py`：遇到 Approval 后停止当前 loop，并返回业务状态。
- `src/lobster0/agent/turn.py`：保存 waiting Turn；批准/拒绝后恢复模型上下文。
- `src/lobster0/cli.py`：注册 8 个工具和 `approvals` CLI。
- `src/lobster0/doctor.py`：只读检查 Tool 配置和 pending Approval。
- `tests/test_*`：按可观察行为覆盖安全矩阵。
- `evals/scenarios/*.jsonl`：新增 write/approval/command/http 离线场景。
- `docs/engineering/phase-2/*.md`、`README.md`、`docs/progress/index.html`：只记录测试证明过的事实。

---

### Task 1: P2.2 Write Boundary and Strict Tool Configuration

**Files:**
- Modify: `src/lobster0/config.py`
- Modify: `src/lobster0/bootstrap.py`
- Modify: `src/lobster0/policy/workspace.py`
- Test: `tests/test_config.py`
- Test: `tests/test_workspace_policy.py`

**Interfaces:**
- Produces: `ToolConfig`, `RunCommandConfig`, `HttpGetConfig` on `AppConfig.tools`.
- Produces: `WorkspaceGuard.resolve_write(context: ToolContext, raw_path: str) -> Path`.
- Consumes: existing `WorkspaceGuard.resolve_read()` sensitive-path and display rules.

- [x] **Step 1: Add failing strict-config tests**

```python
def test_tools_defaults_are_supervised(self) -> None:
    config = load_config(self.paths, {}, {})
    self.assertEqual(config.tools.security, "allowlist")
    self.assertEqual(config.tools.ask, "on-miss")
    self.assertEqual(config.tools.approval_ttl_seconds, 600)
    self.assertIn("write_file", config.tools.enabled)

def test_unknown_nested_tools_key_is_rejected(self) -> None:
    self.paths.config.write_text("[tools.run_command]\nunknown = true\n")
    with self.assertRaisesRegex(ConfigError, "tools.run_command.unknown"):
        load_config(self.paths, {}, {})
```

- [x] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_config -v`

Expected: FAIL because `AppConfig` has no `tools` field and `[tools]` is unknown.

- [x] **Step 3: Implement the minimum dataclasses and strict TOML parser**

```python
@dataclass(frozen=True, slots=True)
class ToolConfig:
    enabled: tuple[str, ...] = BUILTIN_TOOL_NAMES
    security: str = "allowlist"
    ask: str = "on-miss"
    approval_ttl_seconds: int = 600
    run_command: RunCommandConfig = RunCommandConfig()
    http_get: HttpGetConfig = HttpGetConfig()
```

Reject duplicate/unknown tool names, invalid enums, booleans as integers, and timeout values beyond their fixed maxima.

- [x] **Step 4: Add failing write-boundary tests**

```python
def test_resolve_write_only_allows_workspace(self) -> None:
    self.assertEqual(
        WorkspaceGuard().resolve_write(self.context, "notes/today.md"),
        self.workspace / "notes" / "today.md",
    )
    with self.assertRaisesRegex(WorkspaceAccessError, "read-only"):
        WorkspaceGuard().resolve_write(self.context, str(self.read_only / "x.md"))

def test_resolve_write_rejects_parent_symlink_and_sensitive_target(self) -> None:
    (self.workspace / "link").symlink_to(self.outside, target_is_directory=True)
    for value in ("link/out.txt", ".env", "credentials.json"):
        with self.subTest(value=value), self.assertRaises(WorkspaceAccessError):
            WorkspaceGuard().resolve_write(self.context, value)
```

- [x] **Step 5: Run RED, then implement `resolve_write` using existing guard helpers**

Run: `uv run python -m unittest tests.test_workspace_policy -v`

Expected before implementation: FAIL because `resolve_write` does not exist.

Implementation rules: relative paths anchor to Workspace; absolute paths must remain under Workspace; target/parent symlinks and missing parent directories fail; do not reuse read-only-root authorization.

- [x] **Step 6: Verify and commit**

Run: `uv run python -m unittest tests.test_config tests.test_workspace_policy -v && uv run ruff check src/lobster0/config.py src/lobster0/policy/workspace.py tests/test_config.py tests/test_workspace_policy.py`

Commit: `feat(config): 增加 supervised tools 配置与 write boundary`

---

### Task 2: P2.2 Atomic `write_file` and `edit_file`

**Files:**
- Modify: `src/lobster0/tools/filesystem.py`
- Modify: `src/lobster0/policy/engine.py`
- Modify: `src/lobster0/cli.py`
- Modify: `tests/test_file_tools.py`
- Modify: `tests/test_tool_contract.py`

**Interfaces:**
- Produces: `WriteFileTool`, `EditFileTool` implementing the existing `Tool` protocol.
- Produces: error codes `file_exists`, `text_not_found`, `text_not_unique`, `file_too_large`, `binary_file`, `write_failed`.
- Consumes: `WorkspaceGuard.resolve_write()` from Task 1.

- [x] **Step 1: Write RED contract and behavior tests**

```python
async def test_write_file_creates_utf8_file_without_overwrite(self) -> None:
    result = await WriteFileTool().execute(
        self.context, {"path": "notes.txt", "content": "你好\n", "overwrite": False}
    )
    self.assertTrue(result.ok)
    self.assertEqual((self.workspace / "notes.txt").read_text(), "你好\n")

async def test_edit_file_requires_one_exact_match_and_preserves_mode(self) -> None:
    target = self.workspace / "notes.txt"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o640)
    result = await EditFileTool().execute(
        self.context, {"path": "notes.txt", "old_text": "old", "new_text": "new"}
    )
    self.assertTrue(result.ok)
    self.assertEqual(target.read_text(), "new\n")
    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
```

Also assert: content >256 KiB rejected during validation; edit result >1 MiB rejected; NUL/invalid UTF-8 rejected; existing file with `overwrite=false` unchanged; missing parent leaves no file; temp files are removed after an injected `os.replace` failure.

- [x] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_file_tools -v`

Expected: ImportError for the missing tools.

- [x] **Step 3: Implement one private atomic replace helper**

Use `tempfile.NamedTemporaryFile(dir=target.parent, delete=False)`, UTF-8 bytes, `flush()`, `os.fsync()`, mode `0o600` for new files, existing mode for edit/overwrite, `os.replace()`, and unconditional temp cleanup. Re-resolve the target immediately before replace; never create parent directories.

- [x] **Step 4: Expose both contracts and enforce write Policy normalization**

`PolicyEngine.authorize()` must canonicalize write paths before returning `REQUIRE_APPROVAL`; hard-denied paths never create ToolRun/Approval. Contract tests include both tools; production CLI registration waits for Task 4 so the model never sees an approval flow that cannot yet be completed.

- [x] **Step 5: Run GREEN and commit**

Run: `uv run python -m unittest tests.test_file_tools tests.test_tool_contract tests.test_tool_executor -v && uv run ruff check .`

Commit: `feat(files): 实现 atomic write_file 与 exact edit_file`

---

### Task 3: P2.2 Parameter-Bound Approval Storage

**Files:**
- Modify: `src/lobster0/storage/tooling.py`
- Create: `src/lobster0/policy/approvals.py`
- Modify: `src/lobster0/tools/executor.py`
- Create: `tests/test_approvals.py`
- Modify: `tests/test_tool_executor.py`

**Interfaces:**
- Produces: `StoredApproval`, `ApprovalRepository.list/get/create_waiting/decide/consume`.
- Produces: `ToolExecution(model_text: str, approval_id: int | None)`.
- Produces: `ApprovalError` with stable codes `not_found`, `not_owner`, `expired`, `already_decided`, `hash_mismatch`.
- Consumes: existing `_arguments_json()` and `_arguments_hash()` canonical encoder.

- [x] **Step 1: Write RED hash/state tests**

```python
def test_argument_hash_is_order_independent_and_tool_bound(self) -> None:
    left = canonical_arguments_hash("write_file", {"path": "x", "content": "a"})
    right = canonical_arguments_hash("write_file", {"content": "a", "path": "x"})
    self.assertEqual(left, right)
    self.assertNotEqual(left, canonical_arguments_hash("edit_file", {"content": "a", "path": "x"}))

def test_concurrent_consume_has_one_winner(self) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: self.try_consume(), range(2)))
    self.assertEqual(outcomes.count("consumed"), 1)
    self.assertEqual(outcomes.count("conflict"), 1)
```

Also test owner mismatch, TTL expiry, changed stored arguments, restart with a new repository instance, deny without execution, and no raw content in audit metadata.

- [x] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_approvals -v`

Expected: ImportError for `lobster0.policy.approvals`.

- [x] **Step 3: Implement SQLite conditional transitions**

Use `BEGIN IMMEDIATE` for consume. `create_waiting()` inserts a waiting ToolRun, pending Approval and `approval.created` audit atomically. `consume()` recomputes the tool-name-bound hash from `arguments_json` and performs `approved -> consumed` plus `waiting_approval -> running` only when owner and unexpired hash match.

- [x] **Step 4: Make Executor return a typed outcome**

```python
@dataclass(frozen=True, slots=True)
class ToolExecution:
    model_text: str
    approval_id: int | None = None
```

Normal allow/deny returns `approval_id=None`. `REQUIRE_APPROVAL` calls `create_waiting()` and returns a deterministic approval-required JSON result carrying only Approval ID, tool name and redacted summary.

- [x] **Step 5: Verify and commit**

Run: `uv run python -m unittest tests.test_approvals tests.test_tool_executor -v && uv run ruff check .`

Commit: `feat(approval): 增加 parameter-bound SQLite lifecycle`

---

### Task 4: P2.2 Waiting Turn, Continuation, and Approval CLI

**Files:**
- Modify: `src/lobster0/agent/runner.py`
- Modify: `src/lobster0/agent/turn.py`
- Modify: `src/lobster0/storage/conversations.py`
- Modify: `src/lobster0/cli.py`
- Modify: `tests/test_agent_runner.py`
- Modify: `tests/test_turn.py`
- Create: `tests/test_cli_approvals.py`

**Interfaces:**
- Produces: `AgentRunStatus.COMPLETED|WAITING_APPROVAL` and `AgentRunResult.approval_id`.
- Produces: `TurnRepository.wait_for_approval()` and `create_continuation()`.
- Produces CLI: `approvals list|show|approve|deny [--json]`; `approve` supports `--always` only for exact command/hostname rules.
- Consumes: `ApprovalRepository` and `ToolExecutor.execute_approved()` from Task 3.

- [x] **Step 1: Write Runner RED test**

```python
async def test_first_pending_call_ends_loop_and_skips_later_calls(self) -> None:
    result = await runner.run(request, tool_context=self.context)
    self.assertEqual(result.status, AgentRunStatus.WAITING_APPROVAL)
    self.assertIsNotNone(result.approval_id)
    self.assertEqual(provider.request_count, 1)
    self.assertEqual(later_tool.executions, 0)
```

The persisted batch contains the Assistant Tool Call but no fabricated successful Tool Result.

- [x] **Step 2: Run RED, implement Runner/Turn waiting state, run GREEN**

Run: `uv run python -m unittest tests.test_agent_runner tests.test_turn -v`

`TurnService.handle()` calls `wait_for_approval()` instead of completing the Turn and returns deterministic content such as `Approval 42 required for write_file.` without another provider call.

- [x] **Step 3: Write continuation RED test**

```python
async def test_approve_creates_child_turn_executes_once_and_model_finishes(self) -> None:
    result = await service.continue_approval(self.owner.id, approval.id, approved=True)
    self.assertEqual(self.target.read_text(), "approved")
    self.assertEqual(result.content, "写入完成")
    self.assertEqual(self.turns.get(result.turn_id).parent_turn_id, original_turn.id)
    with self.assertRaises(ApprovalError):
        await service.continue_approval(self.owner.id, approval.id, approved=True)
```

Also test deny produces a tool error and never invokes the Tool, hash mismatch never writes, and restart reconstructs context from SQLite.

- [x] **Step 4: Implement continuation with no fake user message**

Create `inbound_event_id="approval:<id>"`, append the approved/denied Tool Message under the child Turn, then call the existing Runner with session history. A consumed ToolRun is never auto-replayed after interruption.

- [x] **Step 5: Write CLI RED tests and implement commands**

Assert stable table output, JSON output, owner/expiry/conflict exit code 2, local I/O exit code 5, and no API key requirement for list/show. `approve`/`deny` load the model key only when continuation must contact the provider.

- [x] **Step 6: Verify P2.2 and commit**

Run: `uv run python -m unittest tests.test_cli_approvals tests.test_agent_runner tests.test_turn tests.test_approvals tests.test_file_tools -v && uv run ruff check .`

Commit: `feat(cli): 完成 approval continuation 与 list/show/approve/deny`

---

### Task 5: P2.3 Exact-Argv Command Execution

**Files:**
- Create: `src/lobster0/policy/command.py`
- Create: `src/lobster0/tools/command.py`
- Modify: `src/lobster0/policy/engine.py`
- Modify: `src/lobster0/storage/tooling.py`
- Modify: `src/lobster0/cli.py`
- Create: `tests/test_command_policy.py`
- Create: `tests/test_run_command.py`
- Modify: `tests/test_cli_approvals.py`

**Interfaces:**
- Produces: `normalize_command(program: str, args: tuple[str, ...], workspace: Path) -> NormalizedCommand`.
- Produces: `RunCommandTool` and exact rule JSON `{type, resolved_program, args}`.
- Consumes: Task 3/4 Approval for on-miss and `--always`.

- [x] **Step 1: Write command-policy RED tests**

```python
def test_shell_eval_delete_upload_privilege_and_git_push_are_hard_denied(self) -> None:
    cases = [("bash", ["-lc", "id"]), ("python", ["-c", "print(1)"]),
             ("rm", ["x"]), ("ssh", ["host"]), ("sudo", ["id"]),
             ("git", ["push"])]
    for program, args in cases:
        with self.subTest(program=program), self.assertRaises(CommandPolicyError):
            normalize_command(program, tuple(args), self.workspace)
```

Also assert no `command` parameter exists, unknown executable fails closed, exact argv does not match extra args, and cwd cannot be supplied by the model.

- [x] **Step 2: Run RED and implement normalization with stdlib**

Run: `uv run python -m unittest tests.test_command_policy -v`

Use `shutil.which()` with a fixed minimal PATH; reject control/NUL characters, shell wrappers, inline-eval flags, and forbidden program/argv combinations.

- [x] **Step 3: Write execution RED tests**

```python
async def test_runs_exact_argv_in_workspace_without_secret_environment(self) -> None:
    result = await self.tool.execute(
        self.context, {"program": sys.executable, "args": [str(self.helper)]}
    )
    self.assertTrue(result.ok)
    self.assertEqual(result.data["cwd"], str(self.workspace))
    self.assertNotIn("super-secret", result.data["stdout"])
```

Also assert separated bounded stdout/stderr, timeout termination of the process group, no stdin/PTY/background, and timeout <=120.

- [x] **Step 4: Implement `asyncio.create_subprocess_exec` with `shell=False`**

Build a minimal environment from safe PATH/locale/platform variables. Start a new process group; on timeout TERM then KILL after two seconds. Bound each captured stream to 1 MiB and return `tool_timeout` without traceback.

- [x] **Step 5: Wire exact persistent rules and verify**

Allowlist hit may execute without approval; miss creates Approval. `approve --always` stores only the normalized executable plus exact argv, sourced from that Approval.

Run: `uv run python -m unittest tests.test_command_policy tests.test_run_command tests.test_cli_approvals -v && uv run ruff check .`

Commit: `feat(command): 加入 exact argv policy 与 safe subprocess`

---

### Task 6: P2.4 HTTPS GET and SSRF Defense

**Files:**
- Create: `src/lobster0/policy/network.py`
- Create: `src/lobster0/tools/web.py`
- Modify: `src/lobster0/policy/engine.py`
- Modify: `src/lobster0/cli.py`
- Create: `tests/test_network_policy.py`
- Create: `tests/test_http_get.py`
- Modify: `tests/test_cli_approvals.py`

**Interfaces:**
- Produces: `validate_https_target(url: str, resolver: Resolver) -> NetworkTarget`.
- Produces: `HttpGetTool`, exact hostname allow rule, `untrusted=true` metadata.
- Consumes: existing `httpx` only if it can preserve pinned peer verification; otherwise stdlib `http.client`/`ssl` with an injected resolver in tests.

- [x] **Step 1: Write network-policy RED tests**

```python
def test_non_https_credentials_and_non_public_addresses_are_denied(self) -> None:
    for url in ("http://example.com", "https://user:pass@example.com",
                "https://127.0.0.1", "https://[::1]", "https://169.254.169.254"):
        with self.subTest(url=url), self.assertRaises(NetworkPolicyError):
            validate_https_target(url, self.resolver)
```

Cover RFC1918, link-local, multicast, unspecified, reserved, mixed public/private DNS answers, control characters, ambiguous hostname encoding, and non-443 ports without an exact rule.

- [x] **Step 2: Run RED and implement URL/DNS validation**

Run: `uv run python -m unittest tests.test_network_policy -v`

Use `urllib.parse`, `socket.getaddrinfo` and `ipaddress`; every resolved address must be globally routable.

- [x] **Step 3: Write HTTP RED tests**

Use a fake connection factory to prove: the validated IP is the connected peer, TLS hostname remains the original hostname, every redirect is revalidated, redirect #4 fails, redirect to private IP fails, response >2 MiB aborts, binary content type fails, and returned metadata contains `untrusted=true`.

- [x] **Step 4: Implement bounded pinned HTTPS transport**

Support GET only, no custom/auth headers, body or fallback service. Stream at most configured bytes; accept text/JSON/XML/HTML media types; strip fragment from requests and redact query from audit summaries.

- [x] **Step 5: Wire hostname exact rules and verify**

`approve --always` stores only lower-cased exact hostname (and explicit allowed port when non-443). Redirect targets must match Policy independently and never inherit the first host's trust.

Run: `uv run python -m unittest tests.test_network_policy tests.test_http_get tests.test_cli_approvals -v && uv run ruff check .`

Commit: `feat(http): 实现 pinned HTTPS 与 SSRF redirect 防护`

---

### Task 7: P2.5 Recovery, Doctor, Regression Gate, and Engineering Docs

**Files:**
- Modify: `src/lobster0/doctor.py`
- Modify: `src/lobster0/storage/tooling.py`
- Modify: `tests/test_doctor.py`
- Modify: `tests/test_eval_cases.py`
- Modify: `tests/test_eval_runner.py`
- Modify/Create: `evals/scenarios/*.jsonl`
- Create: `docs/engineering/phase-2/20260808_filesystem-tools.md`
- Create: `docs/engineering/phase-2/20260808_approval-lifecycle.md`
- Create: `docs/engineering/phase-2/20260808_cli-approvals.md`
- Create: `docs/engineering/phase-2/20260808_command-execution.md`
- Create: `docs/engineering/phase-2/http-and-ssrf.md`
- Create: `docs/engineering/phase-2/20260808_testing-and-debugging.md`
- Modify: `docs/engineering/README.md`
- Modify: `README.md`
- Modify: `docs/progress/index.html`
- Create: `docs/evals/releases/v0.2.0.md`

**Interfaces:**
- Produces: lazy expiry on list/show/consume and startup interruption of stale `running` ToolRuns without replay.
- Produces: Doctor checks for Tool config, writable Workspace, resolvable configured command rules and pending Approval count; checks are read-only.
- Produces: versioned P2.2-P2.4 offline regression cases and v0.2.0 release record.

- [x] **Step 1: Write recovery/doctor RED tests**

```python
def test_doctor_reports_pending_approvals_without_executing_them(self) -> None:
    results = run_local_checks(self.paths)
    item = next(result for result in results if result.name == "approvals")
    self.assertIn("1 pending", item.message)
    self.assertFalse(self.side_effect_path.exists())

def test_stale_running_tool_is_interrupted_not_replayed(self) -> None:
    recovered = repository.interrupt_stale_runs()
    self.assertEqual(recovered, (self.run_id,))
    self.assertEqual(self.status(self.run_id), "interrupted")
```

- [x] **Step 2: Implement minimal recovery and Doctor checks**

No background scheduler: expiry occurs on read/decision; stale runs are marked interrupted during initialized runtime assembly. Doctor never performs DNS requests, HTTP calls, commands or writes beyond its existing safe database access.

- [x] **Step 3: Add deterministic Agent regression cases**

Add active queries for: new file approval, overwrite approval, exact edit, deny-no-write, hash-change rejection, exact command approval, forbidden shell, HTTPS approval, private-address rejection, and approval replay rejection. Assertions use ToolRun/Audit/messages/files, not an LLM judge.

- [x] **Step 4: Run the complete offline exit gate**

Run:

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run lobster0 eval validate
uv run lobster0 eval run --suite offline
git diff --check
```

Expected: zero failures; every active offline case passes.

- [x] **Step 5: Run three explicit live DeepSeek smoke cases**

```bash
uv run lobster0 chat --message "帮我看看我的电脑是什么配置"
uv run lobster0 chat --message "读一下 workspace 里的 README.md 并总结"
uv run lobster0 chat --message "在 workspace 里运行 git status --short"
uv run lobster0 approvals list --status pending
uv run lobster0 approvals approve <ID>
```

Record timestamp, model, commit, sanitized outcome, Approval ID/status and any provider limitation in `docs/evals/releases/v0.2.0.md`; never record the API key or raw secret-bearing environment.

- [x] **Step 6: Write fact-only engineering docs and progress page**

Each document must include: scope/non-scope, plain-language flow, Mermaid state/sequence diagram, public contract, limits, stable errors, SQLite/audit behavior, local commands, test matrix and known ceilings. README/progress counts must be taken from the fresh gate output, not predicted.

- [x] **Step 7: Final verification and commit**

Re-run the exact Step 4 commands after documentation changes. Verify `rg -n "尚未完成|规划中|P2.1C R2" README.md docs/progress/index.html docs/engineering/README.md` has no stale Phase 2 status claim.

Commit: `docs(phase2): 发布 v0.2.0 complete gate 与工程手册`

---

## Plan Self-Review

- Spec coverage: P2.2 is Tasks 1-4; P2.3 Task 5; P2.4 Task 6; P2.5 Task 7. Existing P2.1 remains protected by the full regression gate.
- Security coverage: owner/TTL/hash/concurrency/replay, write escape/atomicity, command hard-deny/env/timeout, HTTPS/DNS/redirect/size are each named in a RED test step.
- Deferred by the approved Phase 2 spec: arbitrary Shell, delete/move tools, OS sandbox, PTY/background jobs, Memory, IM approval cards, multi-user RBAC.
- Dependency check: no new dependency is planned; stdlib and installed `httpx` are sufficient.
- Placeholder scan: no `TBD`, `TODO`, “similar to”, or unspecified error-handling step remains.
- Type consistency: `ToolExecution.approval_id` flows Executor -> Runner -> Turn; `ApprovalRepository.consume` flows CLI/Turn -> Executor; command/hostname exact rules are the only `--always` products.
