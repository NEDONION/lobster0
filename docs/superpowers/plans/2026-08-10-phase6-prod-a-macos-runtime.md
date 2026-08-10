# Phase 6 PROD-A macOS Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付使用 managed CPython 3.12、exact executable chain、Seatbelt 与用户级 LaunchAgent 的可验证 macOS 生产运行时。

**Architecture:** 保留 ExecutionPlan v1 的 canonical JSON/hash 以恢复历史记录，只让新 Seatbelt Plan 使用包含 `ExecutableRef` 的 v2。Core 在审批前冻结 exact chain 和内容 hash，Seatbelt 在执行前复核并只放行 literal executable；生产 Gateway 使用独立 managed runtime 和受管 LaunchAgent，不复用开发 `.venv`。

**Tech Stack:** Python 3.12、stdlib `dataclasses/hashlib/pathlib/plistlib/subprocess`、SQLite、macOS `sandbox-exec`/`launchctl`、现有 `unittest`、Ruff、uv。

## Global Constraints

- 权威设计是 `docs/superpowers/specs/2026-08-10-phase-6-feishu-production-acceptance-design.md`。
- 只实现 macOS LaunchAgent；不顺手实现 systemd、Docker 或远程 Worker。
- 生产 runtime 与仓库开发 `.venv` 分离，Python 必须是 3.12.x。
- ExecutionPlan v1 的 canonical JSON 与 SHA-256 必须逐字兼容；历史 Approval/Receipt 不重写。
- v2 executable chain 只能由 Core 生成，模型和 Tool arguments 没有该字段。
- Seatbelt 不允许 executable `subpath`，backend 不可用或 chain 变化时不回退 Host。
- 不读取、输出或提交 `.env` 值、Home 路径、用户名或平台 ID。
- 每个行为严格 RED→GREEN；公共类/函数使用完整类型标注和中文 docstring。
- 提交标题中英各半；不暂存 `.pnpm-store/`、`.venv/` 或 Live Evidence。

---

### Task A1: ExecutionPlan v2 exact executable model

**Files:**
- Modify: `src/lobster0/sandbox/base.py`
- Modify: `tests/test_sandbox_contract.py`

**Interfaces:**
- Consumes: 现有 `ExecutionPlan.canonical_json`、`sha256`、`from_canonical_json()`。
- Produces: `ExecutableRef(path: Path, sha256: str)`；v2 `ExecutionPlan.executables`；v1/v2 strict round-trip。

- [ ] **Step 1: 写 v1 不变与 v2 executable RED 测试**

```python
def test_v1_canonical_json_remains_byte_compatible(self) -> None:
    plan = self.plan()
    self.assertNotIn('"executables"', plan.canonical_json)
    self.assertEqual(
        ExecutionPlan.from_canonical_json(plan.canonical_json).canonical_json,
        plan.canonical_json,
    )

def test_v2_binds_exact_executable_paths_and_hashes(self) -> None:
    ref = ExecutableRef(Path("/bin/echo"), "a" * 64)
    plan = self.plan(schema_version=2, executables=(ref,))
    restored = ExecutionPlan.from_canonical_json(plan.canonical_json)
    self.assertEqual(restored.executables, (ref,))
    self.assertIn('"executables"', plan.canonical_json)

def test_v2_rejects_empty_duplicate_relative_and_bad_hash_refs(self) -> None:
    with self.assertRaises(SandboxPlanError):
        self.plan(schema_version=2, executables=())
    with self.assertRaises(SandboxPlanError):
        ExecutableRef(Path("relative"), "a" * 64)
    with self.assertRaises(SandboxPlanError):
        ExecutableRef(Path("/bin/echo"), "not-sha256")
```

- [ ] **Step 2: 运行 RED**

Run: `uv run python -m unittest tests.test_sandbox_contract.ExecutionPlanContractTest -v`

Expected: import/constructor failure because `ExecutableRef` and v2 fields do not exist; existing v1 tests remain green.

- [ ] **Step 3: 实现兼容模型**

```python
@dataclass(frozen=True, slots=True)
class ExecutableRef:
    """绑定一个 exact executable path 与审批时内容摘要。"""

    path: Path
    sha256: str

    def __post_init__(self) -> None:
        """拒绝相对路径、控制字符和非标准 SHA-256。"""
        path = _absolute_path(self.path)
        if _has_control(str(path)) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise SandboxPlanError("execution_plan_invalid")
        object.__setattr__(self, "path", path)
```

为 `ExecutionPlan` 增加 `executables: tuple[ExecutableRef, ...] = ()`；v1 必须为空并继续输出旧字段集合，v2 必须包含
1～4 个不重复 ref，并输出按执行顺序保存的 nested JSON。`from_canonical_json()` 根据 `schema_version` 选择严格字段集合，
拒绝未知 key、非 canonical 顺序和 v1/v2 交叉字段。

- [ ] **Step 4: 运行 GREEN 与 repository 回归**

Run: `uv run python -m unittest tests.test_sandbox_contract -v`

Expected: all sandbox contract/repository tests PASS；现有 v1 hash assertion 不变化。

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/sandbox/base.py tests/test_sandbox_contract.py
git commit -m "feat(sandbox): 增加 v2 exact executable binding"
```

---

### Task A2: Freeze and verify executable chains

**Files:**
- Create: `src/lobster0/sandbox/executables.py`
- Create: `tests/test_sandbox_executables.py`
- Modify: `src/lobster0/tools/command.py`
- Modify: `tests/test_run_command.py`

**Interfaces:**
- Consumes: `ExecutableRef`、现有 `normalize_command()` 和 `ExecutableEnvironment.path_value`。
- Produces: `capture_executable_chain(program: Path, *, executable_path: str) -> tuple[ExecutableRef, ...]`；`verify_executable_chain(refs: tuple[ExecutableRef, ...]) -> None`。

- [ ] **Step 1: 写 direct、shebang、env 与 mutation RED 测试**

```python
def test_direct_executable_is_one_hashed_ref(self) -> None:
    chain = capture_executable_chain(self.direct, executable_path=str(self.bin_dir))
    self.assertEqual(tuple(ref.path for ref in chain), (self.direct.resolve(),))
    self.assertRegex(chain[0].sha256, r"^[0-9a-f]{64}$")

def test_env_shebang_freezes_script_env_and_exact_interpreter(self) -> None:
    self.script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    chain = capture_executable_chain(self.script, executable_path=str(self.bin_dir))
    self.assertEqual(
        tuple(ref.path.name for ref in chain),
        (self.script.name, "env", "node"),
    )

def test_changed_executable_fails_verification(self) -> None:
    chain = capture_executable_chain(self.direct, executable_path=str(self.bin_dir))
    self.direct.write_bytes(b"changed")
    with self.assertRaisesRegex(SandboxPlanError, "execution_plan_executable_changed"):
        verify_executable_chain(chain)
```

再覆盖：relative/interpreter missing、超过 4 项、symlink swap、非 regular、不可执行、NUL/control、`env -S` 与带参数
shebang 稳定拒绝，不在异常中回显路径。

- [ ] **Step 2: 运行 RED**

Run: `uv run python -m unittest tests.test_sandbox_executables -v`

Expected: import failure for `lobster0.sandbox.executables`.

- [ ] **Step 3: 实现 no-follow capture 与 verification**

```python
def capture_executable_chain(
    program: Path,
    *,
    executable_path: str,
) -> tuple[ExecutableRef, ...]:
    """冻结 direct/shebang/env executable chain 并返回内容绑定引用。"""
    paths = _resolve_chain(program, executable_path=executable_path)
    if not 1 <= len(paths) <= 4:
        raise SandboxPlanError("execution_plan_exec_chain_invalid")
    return tuple(_capture(path) for path in paths)


def verify_executable_chain(refs: tuple[ExecutableRef, ...]) -> None:
    """在 backend 执行前复核 regular/executable 与内容摘要。"""
    for ref in refs:
        if _capture(ref.path) != ref:
            raise SandboxPlanError("execution_plan_executable_changed")
```

`_capture()` 使用 `os.open(..., O_RDONLY | O_NOFOLLOW)`、`fstat()`、bounded chunk SHA-256；只接受 regular + owner/system
executable。`_resolve_chain()` 最多读取 4096 bytes shebang；`/usr/bin/env NAME` 只通过 `shutil.which(NAME,
path=executable_path)` 冻结，不读取 shell startup file。

- [ ] **Step 4: 让 Seatbelt automation plan 使用 v2**

在 `RunCommandTool.build_execution_plan()` 中，仅当 `backend == "seatbelt"` 时调用 `capture_executable_chain()`，并构造：

```python
return ExecutionPlan(
    argv=(normalized.resolved_program, *normalized.args),
    # existing fields unchanged
    backend="seatbelt",
    executables=capture_executable_chain(
        Path(normalized.resolved_program),
        executable_path=self._executable_path,
    ),
    schema_version=2,
)
```

Docker 继续使用 container program name 和 v1；Host 继续 v1。模型 Tool schema 不新增任何字段。

- [ ] **Step 5: 运行 GREEN**

Run: `uv run python -m unittest tests.test_sandbox_executables tests.test_run_command tests.test_tool_executor -v`

Expected: chain tests PASS；Host/Docker 既有 plan 行为不变；Seatbelt plan hash 包含 refs。

- [ ] **Step 6: Commit**

```bash
git add src/lobster0/sandbox/executables.py src/lobster0/tools/command.py tests/test_sandbox_executables.py tests/test_run_command.py
git commit -m "feat(policy): 冻结 Seatbelt executable chain"
```

---

### Task A3: Seatbelt v2 profile and live probes

**Files:**
- Modify: `src/lobster0/sandbox/seatbelt.py`
- Modify: `scripts/sandbox_live_smoke.py`
- Modify: `tests/test_seatbelt_sandbox.py`
- Modify: `tests/test_sandbox_live_smoke.py`

**Interfaces:**
- Consumes: `ExecutionPlan.executables`、`verify_executable_chain()`。
- Produces: v2 literal-only profile；managed-Python 与 env-shebang live probe。

- [ ] **Step 1: 写 literal chain 与 tamper RED 测试**

```python
def test_v2_profile_allows_only_bound_literal_chain(self) -> None:
    profile = self.backend.build_profile(self.v2_plan)
    for ref in self.v2_plan.executables:
        self.assertIn(f'(allow process-exec (literal "{ref.path}"))', profile)
    self.assertNotIn("process-exec (subpath", profile)

async def test_changed_ref_fails_before_sandbox_exec(self) -> None:
    self.program.write_bytes(b"changed")
    with self.assertRaisesRegex(SandboxPlanError, "execution_plan_executable_changed"):
        await self.backend.execute(self.v2_plan)
```

- [ ] **Step 2: 运行 RED**

Run: `uv run python -m unittest tests.test_seatbelt_sandbox -v`

Expected: profile contains only `argv[0]`; tamper is not detected.

- [ ] **Step 3: 实现 v1/v2 profile**

```python
refs = plan.executables if plan.schema_version == 2 else ()
if refs:
    verify_executable_chain(refs)
    executable_paths = tuple(ref.path for ref in refs)
else:
    executable_paths = (Path(plan.argv[0]),)
for path in executable_paths:
    lines.append(f'(allow process-exec (literal "{_escape(str(path))}"))')
```

执行前和生成 profile 前都验证 v2 refs；v1 保留历史行为。错误只返回稳定码，不输出 path/hash。

- [ ] **Step 4: 扩展 live smoke**

给脚本增加 `--probe {python,node-chain}`，默认 `python`。`node-chain` 在 preflight 找不到 exact `node` 或 fixture script 时
返回 `seatbelt_probe_unavailable`，不会退化为 Python PASS。状态行仍只有：

```text
engine=seatbelt probe=python containment=PASS
engine=seatbelt probe=node-chain containment=PASS
```

- [ ] **Step 5: 运行 GREEN 和真实 managed Python smoke**

Run: `uv run python -m unittest tests.test_seatbelt_sandbox tests.test_sandbox_live_smoke -v`

Run: `uv run python scripts/sandbox_live_smoke.py --backend seatbelt --confirm-live --probe python`

Expected: focused tests PASS；真实状态为 `containment=PASS`，无路径和 Secret。

- [ ] **Step 6: Commit**

```bash
git add src/lobster0/sandbox/seatbelt.py scripts/sandbox_live_smoke.py tests/test_seatbelt_sandbox.py tests/test_sandbox_live_smoke.py
git commit -m "fix(seatbelt): 绑定 exact chain 并完成 live probe"
```

---

### Task A4: Managed macOS LaunchAgent lifecycle

**Files:**
- Create: `src/lobster0/install/service.py`
- Create: `tests/test_install_service.py`
- Modify: `src/lobster0/cli.py`
- Create: `tests/test_cli_service.py`

**Interfaces:**
- Consumes: installed launcher absolute path、State Home、owner-only env/log paths。
- Produces: `ServiceSpec`、`render_launchd_service()`、`LaunchdService.install/status/restart/uninstall()`；`lobster0 service ...`。

- [ ] **Step 1: 写 plist 与 exact lifecycle RED 测试**

```python
def test_launchd_plist_uses_managed_launcher_without_secret(self) -> None:
    spec = render_launchd_service(self.layout)
    value = plistlib.loads(spec.content)
    self.assertEqual(value["Label"], "io.lobster0.gateway")
    self.assertEqual(
        value["ProgramArguments"],
        [str(self.layout.launcher), "gateway", "--home", str(self.layout.state_home)],
    )
    self.assertEqual(value["KeepAlive"], {"SuccessfulExit": False})
    self.assertNotIn(self.secret_sentinel.encode(), spec.content)

def test_restart_uses_exact_gui_domain_and_label(self) -> None:
    service = LaunchdService(self.layout, runner=self.runner, uid=501)
    service.restart()
    self.assertEqual(
        self.runner.argvs,
        [("/bin/launchctl", "kickstart", "-k", "gui/501/io.lobster0.gateway")],
    )
```

覆盖：plist `0600`、logs parent `0700`、`plutil -lint` 先于 replace、bootstrap/print/kickstart/bootout exact argv、重复
install/uninstall、foreign content hash 拒绝、manager 失败回滚、Linux/non-owner fail closed。

- [ ] **Step 2: 运行 RED**

Run: `uv run python -m unittest tests.test_install_service -v`

Expected: import failure for `lobster0.install.service`.

- [ ] **Step 3: 实现最小 LaunchAgent adapter**

```python
@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """保存一个受管 LaunchAgent 的 exact 文件与 ownership hash。"""

    label: str
    path: Path
    content: bytes
    sha256: str


class LaunchdService:
    """只管理 Lobster0 自己拥有的用户级 LaunchAgent。"""

    def install(self) -> None: ...
    def status(self) -> str: ...
    def restart(self) -> None: ...
    def uninstall(self) -> None: ...
```

实现必须用 `plistlib.dumps()`、临时 owner-only 文件、`/usr/bin/plutil -lint`、原子 replace；只有 existing content hash
等于受管 receipt 才可覆盖/删除。subprocess 输出不直接转发，错误映射固定码。

- [ ] **Step 4: 接入 CLI**

新增：

```text
lobster0 service install
lobster0 service status
lobster0 service restart
lobster0 service uninstall
```

CLI 不接受 label、plist path、launchctl path 或任意 argv。`install` 先运行本地 Doctor 并确认只有飞书启用、Secret env names
存在但不读取值；失败不写 plist。

- [ ] **Step 5: 运行 GREEN**

Run: `uv run python -m unittest tests.test_install_service tests.test_cli_service tests.test_cli -v`

Expected: exact lifecycle、幂等、ownership 和 CLI 稳定码全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/lobster0/install/service.py src/lobster0/cli.py tests/test_install_service.py tests/test_cli_service.py
git commit -m "feat(service): 增加 owned LaunchAgent lifecycle"
```

---

### Task A5: PROD-A verification and engineering docs

**Files:**
- Modify: `docs/engineering/phase-6/20260809_sandbox-and-checkpoint.md`
- Modify: `docs/engineering/phase-5/20260808_feishu-gateway-runtime-and-macos-service.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: A1～A4 的公开命令与真实 Seatbelt evidence。
- Produces: 可复现的 managed runtime、Seatbelt 和 LaunchAgent 操作手册。

- [ ] **Step 1: 更新文档中的实际边界**

文档必须明确：managed CPython 3.12、Plan v1/v2、Node chain 限制、LaunchAgent 不含 Secret、Host 不等于 sandbox、真实
`--confirm-live` 命令和只有当前 Mac runtime 被验证的结论。

- [ ] **Step 2: 运行完整 A Gate**

```bash
uv run python -m unittest \
  tests.test_sandbox_contract \
  tests.test_sandbox_executables \
  tests.test_seatbelt_sandbox \
  tests.test_sandbox_live_smoke \
  tests.test_run_command \
  tests.test_tool_executor \
  tests.test_install_service \
  tests.test_cli_service -v
uv run ruff check .
uv run python scripts/validate_docs.py
git diff --check
```

Expected: all PASS；tracked diff 中无 `.env`、绝对 Home、完整 runtime path 或 live output。

- [ ] **Step 3: 运行生产 Mac smoke**

```bash
uv run python scripts/sandbox_live_smoke.py --backend seatbelt --confirm-live --probe python
uv run python scripts/sandbox_live_smoke.py --backend seatbelt --confirm-live --probe node-chain
uv run lobster0 service install
uv run lobster0 service status
uv run lobster0 service restart
uv run lobster0 service status
```

Expected: 两个 containment probe PASS；service 两次 status healthy；若 node chain 暂不可用则 A Gate FAIL，不得跳过。

- [ ] **Step 4: Commit**

```bash
git add README.md docs/engineering/phase-5/20260808_feishu-gateway-runtime-and-macos-service.md docs/engineering/phase-6/20260809_sandbox-and-checkpoint.md
git commit -m "docs(runtime): 记录 macOS Seatbelt production evidence"
```
