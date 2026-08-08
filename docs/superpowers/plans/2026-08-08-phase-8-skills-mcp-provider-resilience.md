# MiniClaw Phase 8 Skills, MCP and Provider Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 安全接入标准 Skills 和 MCP Tool，并让 Provider 在明确的瞬时故障下按可解释策略切换，同时保持 Tool、Secret、费用和审计边界。

**Architecture:** Skill 来源先进入私有 staging，解析 manifest、扫描、生成 Skill Card、Owner 批准后按 hash 安装；MCP Server 由静态配置启动，其 Tool 被适配成现有 `Tool` 并继续经过 Policy/Executor；ProviderRouter 包装多个 `ModelProvider`，只对允许的错误类型前进并在 Session 内保持粘性。

**Tech Stack:** Python 3.12、AgentSkills 风格 `SKILL.md`、MCP Python SDK、stdio/loopback HTTP、现有 Tool Contract/Policy、SQLite、OpenAI-compatible Provider。

## Global Constraints

- 模型不能安装 Skill、添加 MCP Server 或修改 Provider route。
- Skill/MCP 不能绕过 `ToolExecutor` 和 `PolicyEngine`。
- 第三方 Skill 默认不可信，安装和权限变化都要 Owner 批准。
- MCP 子进程只收到显式 Secret allowlist，不继承完整环境。
- 不连接模型生成的任意 URL 或执行模型生成的安装命令。
- Provider fallback 不隐藏认证、协议、Tool schema 或安全错误。
- 显式用户模型选择默认严格，不被自动 fallback 替换。
- Token/费用缺失显示 unknown，不伪造精确数字。

---

### Task 1: Skill manifest v2 and compatibility loader

**Files:**
- Create: `src/miniclaw/skills/manifest.py`
- Modify: `src/miniclaw/skills/loader.py`
- Test: `tests/test_skill_manifest.py`
- Test: `tests/test_skills.py`

**Interfaces:**
- Produces: `SkillManifest`, legacy-v1 compatibility, permission/binary/env declarations and content hash.

- [ ] **Step 1: Write failing manifest tests**

```python
def test_manifest_declares_names_not_secret_values():
    manifest = parse_manifest(valid_skill)
    assert manifest.required_env == ("LARK_APP_ID",)
    assert "secret-value" not in repr(manifest)

def test_unknown_tool_or_permission_is_rejected(self):
    with self.assertRaisesRegex(SkillError, "unknown required tool"):
        parse_manifest(skill_with_tool("disable_policy"))
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_skill_manifest tests.test_skills -v`
Expected: manifest v2 missing.

- [ ] **Step 3: Implement strict manifest parsing**

Fields: name, description, version, license, homepage, required_tools, required_binaries, required_env names, supported_platforms, model_invocable, user_invocable. Reject unknown security-relevant fields and preserve existing minimal Skills as legacy v1.

- [ ] **Step 4: Run loader compatibility tests**

Expected: current summarize Skill and Phase 3 cases remain green; symlink/size/path rules still apply.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/skills/manifest.py src/miniclaw/skills/loader.py tests/test_skill_manifest.py tests/test_skills.py
git commit -m "feat(skills): parse permission-aware skill manifests"
```

### Task 2: Content-addressed Skill staging and trust approval

**Files:**
- Create: `src/miniclaw/skills/install.py`
- Create: `src/miniclaw/skills/scanner.py`
- Create: `src/miniclaw/skills/catalog.py`
- Create: `src/miniclaw/storage/migrations/0007_skill_catalog.sql`
- Modify: `src/miniclaw/storage/migrations.py`
- Test: `tests/test_skill_install.py`
- Test: `tests/test_skill_scanner.py`

**Interfaces:**
- Produces: `SkillInstaller.stage/approve/install/update/revoke/verify`, origin record, scan report and Skill Card.

- [ ] **Step 1: Write failing trust-envelope tests**

```python
def test_stage_has_no_runtime_effect_until_approved(installer, loader):
    staged = installer.stage(local_source)
    assert staged.status == "staged"
    assert staged.name not in {item.name for item in loader.catalog()}

def test_update_with_new_tool_permission_requires_new_approval(installer):
    installed = install_v1(required_tools=("read_file",))
    update = installer.stage(v2(required_tools=("read_file", "run_command")))
    assert update.permission_expanded is True
    assert update.status == "waiting_approval"
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_skill_install tests.test_skill_scanner -v`
Expected: modules/migration missing.

- [ ] **Step 3: Implement staging and scans**

Copy only regular files below bounded count/size into a private O_EXCL staging directory; resolve no symlink targets. Scan manifest, secrets, hidden Unicode, executable files, path escape, suspicious instructions, external URLs and requested permission changes. Store source kind/ref, license, content hash, scan version and approval id.

- [ ] **Step 4: Implement atomic install and verify**

Install into content-addressed version directory and switch one active pointer atomically. `verify` recomputes every hash and fails if local bytes drift. Revoke disables activation but preserves version history.

- [ ] **Step 5: Run install/scanner tests and commit**

```bash
git add src/miniclaw/skills src/miniclaw/storage/migrations.py src/miniclaw/storage/migrations/0007_skill_catalog.sql tests/test_skill_install.py tests/test_skill_scanner.py
git commit -m "feat(skills): stage scan and verify installed skills"
```

### Task 3: Skill maintenance CLI and review UX

**Files:**
- Modify: `src/miniclaw/cli.py`
- Modify: `src/miniclaw/doctor.py`
- Modify: `src/miniclaw/bridge/protocol.py`
- Modify: `tui/src/`
- Test: `tests/test_cli.py`
- Test: `tests/test_doctor.py`
- Modify: `tui/test/`

**Interfaces:**
- Produces: `miniclaw skills list|inspect|stage|approve|install|verify|revoke` maintenance commands and TUI Skill Card.

- [ ] **Step 1: Write failing CLI and redaction tests**

Verify inspect shows source/license/hash/tool/env names but no env values; approve binds staged hash; `install` without approval fails; verify detects drift; command input paths remain WorkspaceGuard-safe.

- [ ] **Step 2: Implement maintenance-only surface**

These commands do not create a second chat UI. TUI may show the same Core-generated Skill Card; neither TUI nor model can widen permissions.

- [ ] **Step 3: Run CLI/TUI/Doctor tests and commit**

```bash
git add src/miniclaw/cli.py src/miniclaw/doctor.py src/miniclaw/bridge/protocol.py tui tests/test_cli.py tests/test_doctor.py
git commit -m "feat(skills): review and manage trusted skills"
```

### Task 4: Strict MCP configuration and process lifecycle

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/miniclaw/config.py`
- Create: `src/miniclaw/mcp/__init__.py`
- Create: `src/miniclaw/mcp/models.py`
- Create: `src/miniclaw/mcp/manager.py`
- Test: `tests/test_mcp_config.py`
- Test: `tests/test_mcp_manager.py`

**Interfaces:**
- Produces: named static `McpServerConfig`, `McpManager.start/stop/status`, safe lifecycle states.

- [ ] **Step 1: Write failing config and environment tests**

```python
def test_mcp_stdio_requires_exact_argv_and_named_env(self, config_loader):
    with self.assertRaises(ConfigError):
        load_mcp({"command": "server | sh"})

async def test_mcp_process_gets_only_explicit_environment(manager):
    await manager.start("files")
    assert fake_process.env == {"PATH": safe_path, "ALLOWED_TOKEN": "injected"}
    assert "MINICLAW_MODEL_API_KEY" not in fake_process.env
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_mcp_config tests.test_mcp_manager -v`
Expected: MCP missing.

- [ ] **Step 3: Implement stdio and loopback HTTP lifecycle**

Add MCP SDK dependency. stdio config uses program/args/cwd/env_names/tool_allowlist; loopback HTTP accepts only `http://127.0.0.1`/`::1` with explicit port and bearer env name. Bound startup/call/shutdown time and stderr bytes; kill process group on timeout.

- [ ] **Step 4: Run lifecycle tests and commit**

```bash
git add pyproject.toml uv.lock src/miniclaw/config.py src/miniclaw/mcp tests/test_mcp_config.py tests/test_mcp_manager.py
git commit -m "feat(mcp): manage static bounded MCP servers"
```

### Task 5: MCP Tool adaptation through Core Policy

**Files:**
- Create: `src/miniclaw/mcp/tools.py`
- Modify: `src/miniclaw/tools/registry.py`
- Modify: `src/miniclaw/runtime.py`
- Test: `tests/test_mcp_tools.py`
- Test: `tests/test_tool_executor.py`

**Interfaces:**
- Produces: namespaced Tool names `mcp_<server>_<tool>`, schema hash cache and standard `ToolResult`.

- [ ] **Step 1: Write failing allowlist/schema-change tests**

```python
def test_only_allowlisted_mcp_tools_enter_registry(runtime):
    assert tool_names(runtime) == {"mcp_files_search"}

async def test_schema_change_disables_tool_until_review(manager):
    manager.refresh(schema_with_changed_hash)
    assert manager.status("files").state == "schema_changed"
    assert "mcp_files_search" not in tool_names(runtime)
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_mcp_tools tests.test_tool_executor -v`
Expected: adaptation missing.

- [ ] **Step 3: Implement adapter and risk mapping**

Normalize MCP JSON Schema to the supported subset, reject recursive/ambiguous schemas, namespace names, map configured risk per Tool and send invocation through `ToolExecutor`. Bound MCP result and mark it `untrusted_mcp_content`.

- [ ] **Step 4: Run Tool/Policy tests and commit**

```bash
git add src/miniclaw/mcp/tools.py src/miniclaw/tools/registry.py src/miniclaw/runtime.py tests/test_mcp_tools.py tests/test_tool_executor.py
git commit -m "feat(mcp): route MCP tools through core policy"
```

### Task 6: MCP approvals, audit and Doctor

**Files:**
- Modify: `src/miniclaw/policy/engine.py`
- Modify: `src/miniclaw/doctor.py`
- Modify: `src/miniclaw/channels/observability.py`
- Test: `tests/test_mcp_policy.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- MCP approval binds server id, tool name, schema hash and canonical arguments hash.

- [ ] **Step 1: Write failing hash/redaction tests**

Verify approval becomes stale after schema change, logs show server/tool/status/duration only, MCP stderr and Secret args never appear, and Autopilot still obeys configured risk.

- [ ] **Step 2: Implement policy/audit and offline Doctor probe**

Doctor validates dependency, config, executable/loopback endpoint, schema cache age and server status without invoking a Tool. Fail-closed startup for enabled required servers; optional servers may be degraded.

- [ ] **Step 3: Run focused tests and commit**

```bash
git add src/miniclaw/policy/engine.py src/miniclaw/doctor.py src/miniclaw/channels/observability.py tests/test_mcp_policy.py tests/test_doctor.py
git commit -m "feat(mcp): bind approvals and audit MCP calls"
```

### Task 7: Provider routing contract and strict error policy

**Files:**
- Modify: `src/miniclaw/config.py`
- Create: `src/miniclaw/providers/router.py`
- Create: `src/miniclaw/providers/routing.py`
- Test: `tests/test_provider_router.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ProviderCandidate`, `ProviderRoute`, `ProviderRouter.complete()` implementing `ModelProvider`.

- [ ] **Step 1: Write failing fallback classification tests**

```python
async def test_transient_errors_advance_to_next_candidate(self):
    for error in (ProviderRateLimitError(), ProviderTimeoutError(), ProviderServerError()):
        with self.subTest(error=type(error).__name__):
            primary.complete.side_effect = error
            response = await router.complete(request)
            self.assertEqual(response, fallback_response)

async def test_auth_and_protocol_errors_fail_closed(self):
    for error in (ProviderAuthenticationError(), ProviderProtocolError()):
        with self.subTest(error=type(error).__name__):
            primary.complete.side_effect = error
            with self.assertRaises(type(error)):
                await router.complete(request)
            fallback.complete.assert_not_called()
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_provider_router tests.test_config -v`
Expected: router missing.

- [ ] **Step 3: Implement candidate chain and session stickiness**

Config defines provider id/model/auth profile env name/max attempts. Router stores only non-secret runtime profile ids, keeps a successful candidate sticky per session, applies exponential cooldown for transient failures, and respects explicit strict session selection.

- [ ] **Step 4: Run router tests and commit**

```bash
git add src/miniclaw/config.py src/miniclaw/providers/router.py src/miniclaw/providers/routing.py tests/test_provider_router.py tests/test_config.py
git commit -m "feat(provider): route explicit model fallback chains"
```

### Task 8: Provider usage and cost budgets

**Files:**
- Create: `src/miniclaw/providers/budget.py`
- Create: `src/miniclaw/storage/migrations/0008_provider_usage.sql`
- Modify: `src/miniclaw/storage/migrations.py`
- Modify: `src/miniclaw/agent/runner.py`
- Modify: `src/miniclaw/automation/runner.py`
- Test: `tests/test_provider_budget.py`

**Interfaces:**
- Produces: persisted request usage, `BudgetDecision`, per-Turn/Task/day checks and safe `provider_budget_exhausted`.

- [ ] **Step 1: Write failing known/unknown usage tests**

```python
def test_unknown_provider_usage_never_becomes_fake_zero(budget):
    budget.record(input_tokens=None, output_tokens=None, cost=None)
    assert budget.snapshot().input_tokens is None

def test_task_stops_before_request_that_would_exceed_known_budget(budget):
    budget.record(input_tokens=900, output_tokens=100, cost=1000)
    assert budget.authorize(max_tokens=1000).allowed is False
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_provider_budget -v`
Expected: budget module missing.

- [ ] **Step 3: Implement conservative budgets**

Use provider-reported usage only for tokens/cost. Always enforce request count, Tool count and wall-clock. Price tables are versioned config metadata; unknown model price yields unknown cost and can be governed by request count rather than guessed money.

- [ ] **Step 4: Run Agent/Task budget tests and commit**

```bash
git add src/miniclaw/providers/budget.py src/miniclaw/storage/migrations.py src/miniclaw/storage/migrations/0008_provider_usage.sql src/miniclaw/agent/runner.py src/miniclaw/automation/runner.py tests/test_provider_budget.py
git commit -m "feat(provider): enforce observable usage budgets"
```

### Task 9: TUI observability, scenarios and v0.8.0

**Files:**
- Modify: `src/miniclaw/agent/events.py`
- Modify: `src/miniclaw/bridge/protocol.py`
- Modify: `tui/src/`
- Create: `evals/scenarios/ecosystem.v1.jsonl`
- Create: `evals/scenarios/provider-routing.v1.jsonl`
- Create: `docs/engineering/phase-8/skills-mcp-provider-resilience.md`
- Create: `docs/evals/releases/v0.8.0.md`
- Modify: `README.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/progress/index.html`
- Test: `tests/test_pi_tui_integration.py`

**Interfaces:**
- TUI shows Skill/MCP/Provider route status without secrets and marks fallback visibly.

- [ ] **Step 1: Add UI and scenario regressions**

Cover Skill stage/scan/permission expansion/drift/revoke, MCP allowlist/schema change/timeout/secret env/result injection, provider transient fallback/auth fail-closed/stickiness/cooldown/unknown usage/budget exhaustion.

- [ ] **Step 2: Run complete deterministic gates**

```bash
uv run python -m unittest discover -s tests -v
pnpm --dir tui test
pnpm --dir tui build
uv run ruff check .
uv run miniclaw eval validate --root evals/scenarios
uv run miniclaw eval run --suite offline --root evals/scenarios
uv run miniclaw eval run --suite ecosystem --root evals/scenarios
uv run miniclaw eval run --suite provider --root evals/scenarios
uv run python scripts/validate_docs.py
uv lock --check
uv build
git diff --check
```

- [ ] **Step 3: Run release-only live samples**

Install one audited local Skill, call one controlled MCP server, trigger one real transient Provider fallback and verify a strict auth/protocol failure does not fallback. Evidence contains only ids, hashes, error classes and usage.

- [ ] **Step 4: Commit verified facts**

```bash
git add src/miniclaw/agent/events.py src/miniclaw/bridge/protocol.py tui evals docs README.md tests/test_pi_tui_integration.py
git commit -m "release(v0.8.0): verify trusted extensions and provider routing"
```

## Final verification

- [ ] Third-party Skill has source, license, hash, scan and approval evidence.
- [ ] Skill permission expansion requires fresh approval.
- [ ] MCP gets only explicit environment and allowlisted Tools.
- [ ] MCP Tools use the same Policy/Executor/Audit path.
- [ ] Provider fallback occurs only for allowed transient errors.
- [ ] Explicit model selection and protocol/auth errors remain strict.
- [ ] Unknown usage remains unknown; request/time budgets still enforce a limit.
- [ ] TUI and logs expose route decisions without exposing credentials.
