# MiniClaw Phase 5.2 Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已验证的飞书 Owner DM 链路交付成可安装、可诊断、可恢复并有 15/15 与 24 小时证据的常驻服务。

**Architecture:** 保持 `miniclaw gateway` 为唯一 Gateway Runtime，在它外面增加平台无关 Service Controller 和 macOS/Linux adapter。服务状态来自有界 health snapshot，不从进程列表猜测业务健康；真实验收继续复用现有 Feishu Live Harness 和脱敏 Evidence。

**Tech Stack:** Python 3.12、stdlib `plistlib`/`subprocess`/`signal`、SQLite、launchd、systemd user service、Docker Compose、现有 Feishu SDK 与 unittest。

## Global Constraints

- 不新增人类聊天入口；`miniclaw service ...` 只是维护命令。
- Secret 不进入 plist、unit file、命令行、SQLite、日志或 Evidence。
- 系统命令必须使用 exact argv，不经过 Shell。
- 安装、重装和卸载必须幂等且只操作 MiniClaw 自己生成的文件。
- 服务管理失败不能删除已有可用配置。
- `LIVE VERIFIED` 只能由真实平台 Evidence 得出。

---

### Task 1: Service contract and safe status model

**Files:**
- Create: `src/miniclaw/service.py`
- Modify: `src/miniclaw/paths.py`
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: `StatePaths` and an adapter implementing `ServiceAdapter`.
- Produces: `ServiceAction`, `ServiceState`, `ServiceStatus`, `ServiceController.status()` and stable `ServiceError.code`.

- [ ] **Step 1: Write the failing service model tests**

```python
def test_status_payload_never_contains_environment_secret(tmp_path):
    adapter = FakeServiceAdapter(state="running", pid=123)
    controller = ServiceController(adapter, health=FakeHealth(secret="sk-secret"))
    status = controller.status()
    assert status.state is ServiceState.RUNNING
    assert "sk-secret" not in repr(status)

def test_uninstall_refuses_unowned_definition(self, tmp_path):
    adapter = FakeServiceAdapter(owned=False)
    with self.assertRaisesRegex(ServiceError, "service definition is not owned"):
        ServiceController(adapter).uninstall()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run python -m unittest tests.test_service -v`
Expected: import failure for `miniclaw.service`.

- [ ] **Step 3: Implement the minimal service contract**

```python
class ServiceState(StrEnum):
    NOT_INSTALLED = "not_installed"
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"

@dataclass(frozen=True, slots=True)
class ServiceStatus:
    state: ServiceState
    pid: int | None
    started_at: str | None
    health_age_seconds: float | None
    enabled_channels: tuple[str, ...]
    degraded_channels: tuple[str, ...]
```

Add `StatePaths.logs`, `StatePaths.service`, and `StatePaths.health` under the existing private MiniClaw state directory.

- [ ] **Step 4: Run focused and path tests**

Run: `uv run python -m unittest tests.test_service tests.test_paths -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/service.py src/miniclaw/paths.py tests/test_service.py tests/test_paths.py
git commit -m "feat(service): define safe background service contract"
```

### Task 2: launchd adapter with ownership hash

**Files:**
- Create: `src/miniclaw/services/__init__.py`
- Create: `src/miniclaw/services/launchd.py`
- Test: `tests/test_launchd_service.py`

**Interfaces:**
- Consumes: absolute MiniClaw executable, project directory, `StatePaths`, exact `CommandRunner`.
- Produces: `LaunchdServiceAdapter.render() -> bytes`, `install()`, `status()`, `restart()`, `uninstall()`.

- [ ] **Step 1: Write failing plist and ownership tests**

```python
def test_plist_uses_exact_arguments_without_secrets(paths):
    payload = LaunchdServiceAdapter(...).render()
    plist = plistlib.loads(payload)
    assert plist["ProgramArguments"][-1] == "gateway"
    assert "EnvironmentVariables" not in plist
    assert b"MINICLAW_MODEL_API_KEY" not in payload

def test_install_is_idempotent_and_atomic(paths):
    adapter = LaunchdServiceAdapter(...)
    first = adapter.install()
    second = adapter.install()
    assert first.changed is True
    assert second.changed is False
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_launchd_service -v`
Expected: import failure for `miniclaw.services.launchd`.

- [ ] **Step 3: Implement rendering and exact command execution**

Use `plistlib.dumps`, private temporary files, `os.replace`, `fsync`, and exact argv calls:

```python
("launchctl", "bootstrap", f"gui/{uid}", str(plist_path))
("launchctl", "kickstart", "-k", f"gui/{uid}/{label}")
("launchctl", "bootout", f"gui/{uid}", str(plist_path))
```

Store a generator marker and SHA-256 in a sibling manifest. `uninstall()` must reject files whose marker/hash no longer match.

- [ ] **Step 4: Run tests including hostile paths and command failure**

Run: `uv run python -m unittest tests.test_launchd_service tests.test_service -v`
Expected: PASS for spaces in paths, existing foreign plist, failed bootstrap, and repeated uninstall.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/services tests/test_launchd_service.py
git commit -m "feat(service): add owned launchd lifecycle"
```

### Task 3: Health snapshot and Gateway integration

**Files:**
- Create: `src/miniclaw/health.py`
- Modify: `src/miniclaw/gateway.py`
- Modify: `src/miniclaw/channels/supervisor.py`
- Test: `tests/test_health.py`
- Test: `tests/test_gateway.py`

**Interfaces:**
- Consumes: Gateway/channel lifecycle events.
- Produces: atomically written `health.json` with schema version 1 and no user content.

- [ ] **Step 1: Write failing health projection tests**

```python
def test_health_snapshot_contains_states_but_no_message_content(tmp_path):
    writer = HealthWriter(tmp_path / "health.json", clock=fixed_clock)
    writer.ready(enabled=("feishu",), channels={"feishu": "ready"})
    payload = json.loads((tmp_path / "health.json").read_text())
    assert payload["schema_version"] == 1
    assert payload["channels"] == {"feishu": "ready"}
    assert "message" not in payload
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_health tests.test_gateway -v`
Expected: missing `HealthWriter`.

- [ ] **Step 3: Implement atomic health transitions**

Write only: schema version, process id, started/updated timestamps, runtime state, enabled channel names, degraded channel names, last safe error code, and shutdown reason. Use mode `0600`, bounded JSON, temporary file + `os.replace`.

- [ ] **Step 4: Verify startup, degraded channel and shutdown states**

Run: `uv run python -m unittest tests.test_health tests.test_gateway tests.test_channel_supervisor -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/health.py src/miniclaw/gateway.py src/miniclaw/channels/supervisor.py tests/test_health.py tests/test_gateway.py tests/test_channel_supervisor.py
git commit -m "feat(gateway): publish bounded runtime health"
```

### Task 4: Service CLI and doctor checks

**Files:**
- Modify: `src/miniclaw/cli.py`
- Modify: `src/miniclaw/doctor.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `ServiceController` and health snapshot.
- Produces: `miniclaw service install|status|logs|restart|uninstall` with stable exit codes.

- [ ] **Step 1: Write failing CLI behavior tests**

```python
def test_service_status_is_maintenance_command_not_chat_entrypoint():
    result = run_cli(["service", "status"], controller=fake_controller)
    assert result.exit_code == 0
    assert "running" in result.stdout

def test_service_logs_rejects_unbounded_line_count():
    result = run_cli(["service", "logs", "--lines", "10001"])
    assert result.exit_code == 2
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_cli tests.test_doctor -v`
Expected: parser rejects `service`.

- [ ] **Step 3: Add the command group and doctor checks**

Cap logs at 1,000 lines and read only regular files from `StatePaths.logs`. `doctor` checks definition ownership, health freshness, log directory permissions, and service/runtime disagreement without starting or stopping the service.

- [ ] **Step 4: Run focused CLI/doctor tests**

Run: `uv run python -m unittest tests.test_cli tests.test_doctor tests.test_service -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/cli.py src/miniclaw/doctor.py tests/test_cli.py tests/test_doctor.py
git commit -m "feat(cli): manage the MiniClaw background service"
```

### Task 5: Hardened Docker and Compose deployment

**Files:**
- Create: `deploy/Dockerfile`
- Create: `deploy/compose.yaml`
- Create: `deploy/README.md`
- Create: `tests/test_deployment_assets.py`
- Modify: `.dockerignore`

**Interfaces:**
- Consumes: packaged MiniClaw wheel and mounted state/config.
- Produces: non-root runtime with no public port and explicit writable volume.

- [ ] **Step 1: Write failing static deployment assertions**

```python
def test_compose_drops_privileges_and_does_not_mount_home():
    compose = load_yaml(PROJECT_ROOT / "deploy/compose.yaml")
    service = compose["services"]["miniclaw"]
    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert "ALL" in service["cap_drop"]
    assert not service.get("ports")
    assert all("/Users" not in item and "/home" not in item for item in service["volumes"])
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_deployment_assets -v`
Expected: missing deployment files.

- [ ] **Step 3: Add non-root image and compose profile**

Use a fixed unprivileged UID, read-only root filesystem, tmpfs for `/tmp`, state volume only, `restart: unless-stopped`, no Docker socket, and no exposed port. Document how `.env` is provided without copying it into the image.

- [ ] **Step 4: Validate the assets and build**

Run: `uv run python -m unittest tests.test_deployment_assets -v`
Run: `docker compose -f deploy/compose.yaml config --quiet`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add deploy .dockerignore tests/test_deployment_assets.py
git commit -m "build(deploy): add hardened MiniClaw container"
```

### Task 6: Feishu 15/15, soak and release evidence

**Files:**
- Create: `src/miniclaw/evals/soak.py`
- Create: `tests/test_soak_harness.py`
- Modify: `src/miniclaw/evals/feishu_live.py`
- Modify: `docs/engineering/phase-5/20260808_feishu-live-e2e.md`
- Modify: `docs/engineering/phase-5/20260808_feishu-gateway-runtime-and-macos-service.md`
- Create: `docs/evals/releases/v0.5.2.md`
- Modify: `README.md`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Consumes: real Feishu credentials and the existing 15 versioned cases.
- Produces: redacted 15-case report and soak report with commit hash and exact counts.

- [ ] **Step 1: Write failing evidence-schema tests**

```python
def test_soak_report_rejects_secret_and_unknown_fields(tmp_path):
    report = build_soak_report(...)
    assert set(report) == {"schema_version", "commit", "duration_seconds", "counts", "checks", "release_status"}
    assert scan_secret_matches([write_report(tmp_path, report)], secrets) == 0
```

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest tests.test_soak_harness tests.test_feishu_live_e2e -v`
Expected: missing soak harness.

- [ ] **Step 3: Implement bounded soak collection and fault injection**

Collect only counts, durations, safe state names, error codes and commit. Include controlled restart, network interruption, provider transient failure, duplicate event and channel reconnect checks. Never copy message bodies or identifiers.

- [ ] **Step 4: Run deterministic gates**

Run: `uv run python -m unittest discover -s tests -v`
Run: `pnpm --dir tui test && pnpm --dir tui build`
Run: `uv run ruff check . && uv run python scripts/validate_docs.py`
Expected: all PASS.

- [ ] **Step 5: Run the live gates with explicit Owner confirmation**

Run the documented Feishu 15-case command against a clean commit, then run a 24-hour soak. Expected: 15/15, zero secret matches, zero duplicate side effects, zero permanently stuck tasks/deliveries, and a manually observed client experience record.

- [ ] **Step 6: Commit the release facts only after the live evidence exists**

```bash
git add src/miniclaw/evals/soak.py tests/test_soak_harness.py src/miniclaw/evals/feishu_live.py README.md docs
git commit -m "release(v0.5.2): verify persistent Feishu operations"
```

## Final verification

- [ ] `uv run python -m unittest discover -s tests -v`
- [ ] `pnpm --dir tui test`
- [ ] `pnpm --dir tui build`
- [ ] `uv run ruff check .`
- [ ] `uv run python scripts/validate_docs.py`
- [ ] `uv lock --check`
- [ ] `uv build`
- [ ] `git diff --check`
- [ ] Feishu 15/15 real evidence
- [ ] 24-hour service soak
- [ ] Secret scan count = 0
