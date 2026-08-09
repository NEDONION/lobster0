# MiniClaw One-line Install and Trusted Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 Linux/macOS 一行安装、完整功能依赖、受管 Python/Node/pi-tui、非 root 服务、原子升级/回滚、安全卸载以及 GitHub Release、PyPI、GHCR 同源发布门禁。

**Architecture:** GitHub Release 的 immutable manifest 是安装事实源；一个最小 POSIX bootstrap 建立 uv/Python 信任根，再把控制权交给 stdlib-only `miniclaw-installer.pyz`。安装器在 versioned Runtime staging 中校验并安装 Python wheel、hash-locked dependencies、managed Node 和 platform TUI bundle，通过新 Runtime smoke、数据库保护和 service health 后才原子切换 `current`；稳定 launcher、用户状态和 Secret 文件不随 Runtime 版本漂移。

**Tech Stack:** Python 3.12、stdlib `dataclasses/json/tarfile/urllib/zipapp/sqlite3/plistlib/subprocess`、uv 0.12.0、Node 24.18.0（默认）与 Node 22.22.3+、pnpm 10.14.0、systemd user service、macOS launchd、GitHub Actions/Attestations、PyPI Trusted Publishing、GHCR、`unittest`、Ruff。

## Global Constraints

- 权威设计是 `docs/superpowers/specs/2026-08-09-one-line-install-and-release-design.md`；本计划一次交付完整方案，不发布缺 TUI、Channel、service、upgrade 或 uninstall 的临时稳定版。
- 生产实现必须从 Phase 6 Task 18 已完成、全部门禁通过且工作区干净的最新 `main` 开始；当前观察到 Phase 6 正在实现 Docker/Seatbelt Sandbox，禁止覆盖或重复其 Sandbox、Checkpoint、Gateway lifecycle 和 Doctor 事实。
- Python 分发名固定为 `miniclaw-agent`；产品、仓库、Python import、CLI 和默认状态根保持 `MiniClaw`、`miniclaw`、`miniclaw`、`miniclaw`、`~/.miniclaw`。
- 首个按本计划发布的目标版本是 `0.7.0`；`src/miniclaw/_version.py` 是版本单一来源，wheel、CLI、tag `v0.7.0`、manifest 和 release record 必须一致。
- Python Runtime 固定 3.12 系列；bootstrap/runtime 使用 uv 0.12.0，四个平台官方 archive SHA-256 固定在 `release/runtime-versions.json`。
- 默认 managed Node 固定 24.18.0；只接受 `22.22.3 <= version < 23.0.0` 或 `24.15.0 <= version < 25.0.0`，拒绝 Node 20/23/25/26。
- Tier 1 是 Ubuntu 22.04/24.04、Debian 12/13、RHEL/Rocky/Alma 9/10 的 x86_64/arm64，以及 macOS 13+ Intel/Apple Silicon；Windows、WSL、Alpine/musl、NixOS、Android、32 位和非 systemd Linux service host 明确不支持。
- 安装器在任何写入前拒绝 unsupported platform、相对/符号链接 prefix、`/`、Home 根、group/world-writable Runtime 目录和已有未受管同名 launcher。
- 所有外部命令使用 exact argv 与 `shell=False`；源 URL 只能来自已验证 manifest 和固定 allowlist；GitHub 下载只允许重定向到 `release-assets.githubusercontent.com` 的 HTTPS asset URL，其他跨 host redirect 全部拒绝；所有组件下载先校验 size 与 SHA-256。
- archive 解包拒绝绝对路径、`..`、symlink、hardlink、device、FIFO、目标逃逸、重复路径、超 entry 数和超解压字节；Node/TUI Release bundle 因而必须是 symlink-free `.tar.gz`。
- 默认用户安装不得请求 sudo；系统包、linger 和 system prefix 分别展示精确 `InstallPlan` 并再次确认，禁止修改 sudoers、自动加入 docker group或以 UID 0 运行 Gateway。
- Secret 只能来自 `/dev/tty` 隐藏输入、owner-only 绝对 Secret 文件或现有进程环境；Token/API Key 不得出现在 installer flags、argv、JSON event、异常、unit/plist、receipt、日志和 Git diff。
- `--dry-run` 在零持久化写入、零组件下载、零 sudo、零 Secret 读取条件下输出完整脱敏计划；bootstrap 只允许把运行 pyz 所需的 uv、临时 Python、manifest 和 pyz 下载到 trap 清理的私有临时目录。stdin 非 TTY 且参数不足时 fail closed。
- 安装、更新、回滚和卸载共用 owner-only install lock；新 Runtime health 未通过前不得替换 `current`，不得删除 N-1 Runtime。
- SQLite schema 回退必须同时恢复升级前数据库备份和旧 Runtime；检测到新写入时返回 `rollback_conflict`，不得覆盖用户升级后的 Memory、Skills 或 Workspace 文件。
- service unit 永远指向稳定 launcher；systemd/launchd 文件只有 label、path 和 ownership hash 都匹配 receipt 时才允许覆盖或删除。
- 单元测试离线、快速、可重复，不执行 sudo、不访问真实网络/Home/service/Provider/Channel；VM/实体 runner 才能产生 Tier 1 service/reboot 的 LIVE PASS。
- 每个生产行为严格 RED→GREEN；先跑 focused tests，再跑全量 unittest、Ruff、docs validator、build、install matrix 与 Secret scan。
- 提交只包含当前 Task 文件，提交标题使用中英混合；不暂存 `.pnpm-store/` 和 Phase 6 的并行工作文件。

---

## File Map

| 文件 | 单一职责 |
| --- | --- |
| `src/miniclaw/_version.py` | Python/CLI/build 的唯一版本常量 |
| `src/miniclaw/setup.py` | fresh-install 交互配置与 owner-only Secret 写入 |
| `src/miniclaw/install/models.py` | strict manifest、artifact、request、plan、event 强类型模型 |
| `src/miniclaw/install/platforms.py` | OS/distro/arch 检测、Node policy 与 system dependency actions |
| `src/miniclaw/install/artifacts.py` | allowlisted HTTPS 下载、hash/size 校验与安全 tar 解包 |
| `src/miniclaw/install/releases.py` | fixed/stable/dev GitHub Release manifest 发现与 bounded API 解析 |
| `src/miniclaw/install/layout.py` | program prefix、state home、Runtime staging 和 stable launcher 路径 |
| `src/miniclaw/install/receipt.py` | install lock、owner-only receipt 与 managed-file ownership hash |
| `src/miniclaw/install/runtime.py` | venv/requirements/wheel/Node/TUI staging、smoke 与 atomic activation |
| `src/miniclaw/install/service.py` | systemd user/LaunchAgent render、validate 与 exact lifecycle argv |
| `src/miniclaw/install/update.py` | DB backup/data-version guard、N-1 rollback 与 retention |
| `src/miniclaw/install/orchestrator.py` | install/update/uninstall state machine 与脱敏 events |
| `src/miniclaw/install/__main__.py` | installer zipapp 与已安装 CLI 共用参数入口 |
| `scripts/build_installer_zipapp.py` | 校验 stdlib import boundary 并构建 deterministic pyz |
| `scripts/build_tui_bundle.py` | pnpm production deploy、symlink materialization、license inventory 和 deterministic tar.gz |
| `scripts/build_node_bundle.py` | 从已校验官方 Node archive 提取 node/LICENSE 并生成 symlink-free tar.gz |
| `scripts/build_release_manifest.py` | 从 clean tag、artifact 和 runtime pins 生成 manifest/checksum |
| `scripts/build_sbom.py` | 合并 Python、Node、TUI 与 image inventory 为 CycloneDX/SPDX SBOM |
| `scripts/render_install_script.py` | 把 release URL、uv/manifest/pyz hashes 固化到最终 `install.sh` |
| `release/install.sh.tmpl` | 最小 POSIX bootstrap 模板，不含安装业务逻辑 |
| `release/runtime-versions.json` | uv/Node/pnpm 固定版本、官方 URL 与四平台 upstream hashes |
| `release/manifest.schema.json` | Release manifest schema v1 的机器可验证副本 |
| `requirements-all.lock` | Core + Feishu/Telegram/Discord 的 exact/hash-required Python 依赖 |
| `deploy/Dockerfile` | 非 root 完整 MiniClaw GHCR Runtime image |
| `.github/workflows/ci.yml` | Python/Node/build/docs/offline installer 门禁 |
| `.github/workflows/release.yml` | tag build、Tier 1 gate、attest、PyPI/GHCR 与 stable promotion |
| `tests/install/` | fake artifacts、fake commands 和离线 crash-window fixtures |
| `tests/install_matrix/` | 容器/VM/实体 runner fresh install、service、upgrade、rollback、uninstall driver |
| `docs/engineering/operations/20260809_install-release-operations.md` | 安装、权限、服务、升级、恢复与发布运维事实 |

`src/miniclaw/cli.py` 只负责 parser/dispatch；安装业务放在 `miniclaw.install`。`StatePaths` 继续描述用户状态，不承载 Runtime layout；`InstallLayout` 单独描述程序安装位置。Execution Preflight 要求 `src/miniclaw/install/`、`deploy/Dockerfile` 和 `deploy/sandbox.Dockerfile` 尚不存在；任一路径已被 Phase 6 占用时停止执行并修订本计划，不能创建第二份实现。

---

## Execution Preflight: Phase 6 clean handoff

- [ ] **Step 1: 确认 Phase 6 已完成且没有并行修改**

Run:

```bash
git status --short
git log -1 --oneline
rg -n "Phase 6.*COMPLETE|v0.7.0" docs/evals/releases docs/engineering/phase-6
```

Expected: 工作区无修改；Phase 6 Task 1～11 与设计完成定义已闭合；release record 不把 PENDING live gate 伪装为 PASS。

- [ ] **Step 2: 重新盘点交叉边界**

Run:

```bash
rg -n "service|install|update|uninstall|Dockerfile|LaunchAgent|systemd" src tests scripts deploy .github docs
find src/miniclaw -maxdepth 2 -type f | sort
```

Expected: 记录 Phase 6 已提供的 Gateway health、Doctor、Sandbox image 和 lifecycle 接口；本计划不保留重复实现。

Phase 6 handoff must also prove its Linux Docker backend can reach a rootless socket without docker-group membership or `/var/run/docker.sock`, and that RHEL-family `/usr/bin/docker` provided by `podman-docker` passes the same containment smoke. If either fact is absent, finish it in the Phase 6 branch before this plan; the installer must not compensate by weakening Sandbox policy.

- [ ] **Step 3: 建立隔离执行工作区并跑基线**

执行时先使用 `superpowers:using-git-worktrees` 创建 `codex/one-line-install`，然后运行：

```bash
uv sync --extra dev
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run python scripts/validate_docs.py
git diff --check
```

Expected: 全部 PASS。任一基线失败先按 `superpowers:systematic-debugging` 归因，不把已有失败带进 Task 1。

---

### Task 0: Phase 6 rootless container-engine handoff

**Files:**
- Modify: `src/miniclaw/config.py`
- Modify: `src/miniclaw/bootstrap.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `src/miniclaw/tools/command.py`
- Modify: `src/miniclaw/sandbox/docker.py`
- Modify: `src/miniclaw/doctor.py`
- Modify: `scripts/sandbox_live_smoke.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_run_command.py`
- Modify: `tests/test_docker_sandbox.py`
- Modify: `tests/test_doctor.py`
- Create: `tests/test_sandbox_live_smoke.py`

**Interfaces:**
- Consumes: Phase 6 `ExecutionPlan`, `DockerSandbox`, `RunCommandTool` and Doctor contracts.
- Produces: strict `sandbox.container_engine = "docker-rootless" | "podman-rootless"`, defaulting to `docker-rootless` for backward-compatible config loading; an explicit rootless client transport that never becomes container `--env`; rootless-aware Doctor/live-smoke evidence.

- [ ] **Step 1: Write rootless transport RED tests**

Cover both engines on Linux with an injected non-zero UID and controlled filesystem facts. Docker must select only `unix:///run/user/<uid>/docker.sock`; Podman must select only `unix:///run/user/<uid>/podman/podman.sock`. Reject UID 0, `/var/run/docker.sock`, symlink/non-socket/foreign-owner sockets, an engine/executable mismatch and an unsafe runtime directory. Verify client-only `HOME`, `XDG_RUNTIME_DIR` and `DOCKER_HOST`/`CONTAINER_HOST` values are not emitted as container `--env` entries and do not enter `ExecutionPlan.canonical_json` or receipts.

Run the focused tests and observe failures caused by the missing config/client-transport behavior, not import errors.

- [ ] **Step 2: Implement a separate rootless client boundary**

Add the strict config field and pass it through Runtime to `RunCommandTool`. Keep the immutable model-owned `ExecutionPlan` unchanged: container environment remains the plan's allowlisted names, while Docker/Podman client connection values are Core-derived immediately before launching the trusted absolute executable. Linux derives `/run/user/<effective uid>` and the engine-specific socket; no config, environment or model input may choose an arbitrary socket. Resolve the trusted executable and require `docker-rootless` to use Docker and `podman-rootless` to resolve to Podman. Both modes reject effective UID 0 and never fall back to `/var/run/docker.sock`, docker-group access, sudo or Host execution.

The host-side client environment may include the already resolved Owner home and rootless runtime directory, but none of those values may be forwarded through Docker `--env` or persisted. Keep module import side-effect free and preserve Seatbelt/interactive Host behavior.

- [ ] **Step 3: Make Doctor and live smoke verify the same boundary**

Doctor remains offline/no-Secret and reports engine-specific rootless readiness without printing UID, Home or socket paths. `scripts/sandbox_live_smoke.py` requires an explicit engine for Docker, uses the production discovery/validation path and reports only stable engine/containment status. Unit tests fake filesystem/process facts; they do not connect to a real daemon.

- [ ] **Step 4: Verify and commit Phase 6 handoff**

Run focused config/Sandbox/command/Doctor/smoke tests, the 15-case Automation suite, full unittest and Ruff. Real Debian Docker and RHEL Podman containment remain explicit Tier 1 release gates; local contract tests must not label them live PASS.

Commit: `fix(sandbox): 闭合 rootless Docker 与 Podman handoff`

---

### Task 1: Distribution identity and one version source

**Files:**
- Create: `src/miniclaw/_version.py`
- Modify: `src/miniclaw/__init__.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/test_package_metadata.py`

**Interfaces:**
- Consumes: clean Phase 6 `main` and existing `miniclaw` console entry point.
- Produces: `miniclaw._version.__version__: str == "0.7.0"`; wheel distribution `miniclaw-agent`; extras `channels` and `all`; unchanged import/CLI names.

- [ ] **Step 1: Write metadata RED tests**

```python
def test_version_has_one_python_source(self) -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    self.assertEqual(metadata["project"]["name"], "miniclaw-agent")
    self.assertEqual(metadata["project"]["dynamic"], ["version"])
    self.assertNotIn("version", metadata["project"])
    self.assertEqual(__version__, "0.7.0")

def test_public_names_and_complete_extras_do_not_change(self) -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    self.assertEqual(project["scripts"]["miniclaw"], "miniclaw.cli:main")
    self.assertEqual(set(project["optional-dependencies"]["all"]), set(project["optional-dependencies"]["channels"]))
```

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_package_metadata -v`

Expected: FAIL because `_version.py`, dynamic metadata and `all` extra are absent.

- [ ] **Step 3: Implement the exact metadata contract**

```python
# src/miniclaw/_version.py
"""MiniClaw 发布版本的唯一源码。"""

__version__ = "0.7.0"
```

`src/miniclaw/__init__.py` only re-exports `from miniclaw._version import __version__`. In `pyproject.toml`, set build requirement to the reviewed `setuptools==80.9.0`, set `name = "miniclaw-agent"`, replace literal version with `dynamic = ["version"]`, add `[tool.setuptools.dynamic] version = {attr = "miniclaw._version.__version__"}`, and define `all` with the same three exact SDK requirements as `channels`.

- [ ] **Step 4: Verify wheel metadata and entry point**

Run:

```bash
uv lock
uv build
uv run python -m unittest tests.test_package_metadata tests.test_cli -v
uv run python -c 'import zipfile,glob; p=glob.glob("dist/*.whl")[0]; z=zipfile.ZipFile(p); n=[x for x in z.namelist() if x.endswith("METADATA")][0]; m=z.read(n).decode(); assert "Name: miniclaw-agent" in m and "Version: 0.7.0" in m'
```

Expected: PASS; wheel filename begins `miniclaw_agent-0.7.0-`; no package named `miniclaw-agent` is imported.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/miniclaw/_version.py src/miniclaw/__init__.py tests/test_package_metadata.py
git commit -m "build(package): 统一 miniclaw-agent version metadata"
```

---

### Task 2: Installed Secret-file resolution without breaking development

**Files:**
- Modify: `src/miniclaw/paths.py`
- Modify: `src/miniclaw/env.py`
- Modify: `src/miniclaw/gateway.py`
- Modify: `src/miniclaw/tui/app.py`
- Modify: `src/miniclaw/cli.py`
- Modify: `tests/test_env.py`
- Modify: `tests/test_gateway.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Consumes: existing owner-only `load_dotenv(path, environ)` and `StatePaths` factory.
- Produces: `StatePaths.secrets_file`; `resolve_dotenv_path(paths, environ, cwd=None) -> Path`; development defaults to `cwd/.env`, installed launcher selects an absolute `MINICLAW_ENV_FILE`.

- [ ] **Step 1: Write installed/development path RED tests**

```python
def test_installed_env_file_must_be_absolute_and_wins_over_cwd(self) -> None:
    private = self.root / "secrets.env"
    self.assertEqual(
        resolve_dotenv_path(self.paths, {"MINICLAW_ENV_FILE": str(private)}, cwd=self.other),
        private,
    )
    with self.assertRaisesRegex(DotEnvError, "must be absolute"):
        resolve_dotenv_path(self.paths, {"MINICLAW_ENV_FILE": "relative.env"}, cwd=self.other)

def test_development_keeps_fixed_cwd_dotenv(self) -> None:
    self.assertEqual(resolve_dotenv_path(self.paths, {}, cwd=self.other), self.other / ".env")
```

Add Gateway/Doctor/TUI tests with a sentinel in `<state>/secrets.env`; set only `MINICLAW_ENV_FILE`; assert the sentinel is never printed and the old cwd `.env` tests still pass.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_env tests.test_gateway tests.test_cli tests.test_tui -v`

Expected: FAIL because `secrets_file` and `resolve_dotenv_path` are absent and runtime call sites still hard-code `Path.cwd() / ".env"`.

- [ ] **Step 3: Implement one resolver and route every runtime through it**

```python
def resolve_dotenv_path(
    paths: StatePaths,
    environ: Mapping[str, str],
    *,
    cwd: Path | None = None,
) -> Path:
    """解析显式安装态 Secret 文件，否则保持 cwd/.env 开发语义。"""
    selected = environ.get("MINICLAW_ENV_FILE", "").strip()
    if not selected:
        return (Path.cwd() if cwd is None else cwd) / ".env"
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        raise DotEnvError("MINICLAW_ENV_FILE must be an absolute path")
    return candidate.resolve(strict=False)
```

Add `secrets_file=resolved_home / "secrets.env"` in `build_state_paths`. Gateway, Doctor dispatch and TUI load exactly `resolve_dotenv_path(paths, environment)` before `load_dotenv`; no call site searches parent directories or Home.

- [ ] **Step 4: Run GREEN and secret scan**

Run:

```bash
uv run python -m unittest tests.test_env tests.test_gateway tests.test_cli tests.test_tui -v
rg -n "Path\.cwd\(\) / \"\.env\"" src/miniclaw
```

Expected: tests PASS; remaining direct cwd usage exists only in explicitly development-only eval/bridge code or is migrated through the same resolver where `StatePaths` is available.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/paths.py src/miniclaw/env.py src/miniclaw/gateway.py src/miniclaw/tui/app.py src/miniclaw/cli.py tests/test_env.py tests/test_gateway.py tests/test_cli.py tests/test_tui.py
git commit -m "feat(env): 支持 installed owner-only Secret file"
```

---

### Task 3: Fresh-install onboarding with no Secret argv

**Files:**
- Create: `src/miniclaw/setup.py`
- Modify: `src/miniclaw/bootstrap.py`
- Modify: `src/miniclaw/cli.py`
- Create: `tests/test_setup.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `StatePaths.secrets_file`, existing strict config loader and `initialize_state(paths)`.
- Produces: `SetupAnswers`, `write_fresh_setup(paths, answers, secrets, sandbox_image) -> InitResult`, interactive `miniclaw setup`; the installer may pass one non-Secret pinned Sandbox image, but no Secret-valued CLI option exists.

- [ ] **Step 1: Write onboarding RED tests**

```python
def test_fresh_setup_writes_private_config_and_secrets_without_echo(self) -> None:
    answers = SetupAnswers(
        enable_feishu=True,
        feishu_owner_open_id="ou_owner",
        enable_telegram=False,
        telegram_owner_user_id=None,
        enable_discord=False,
        discord_owner_user_id=None,
    )
    result = write_fresh_setup(
        self.paths,
        answers,
        {
            "MINICLAW_MODEL_API_KEY": "sentinel-model-key",
            "MINICLAW_FEISHU_APP_ID": "cli_app",
            "MINICLAW_FEISHU_APP_SECRET": "sentinel-app-secret",
        },
        sandbox_image="ghcr.io/nedonion/miniclaw-sandbox@sha256:" + "a" * 64,
    )
    self.assertEqual(stat.S_IMODE(self.paths.config.stat().st_mode), 0o600)
    self.assertEqual(stat.S_IMODE(self.paths.secrets_file.stat().st_mode), 0o600)
    config = load_config(self.paths, {})
    self.assertTrue(config.channels.feishu.enabled)
    self.assertNotIn("sentinel", self.paths.config.read_text(encoding="utf-8"))
    self.assertTrue(result.owner.id > 0)

def test_setup_refuses_overwrite_and_unsafe_secret_text(self) -> None:
    self.paths.config.parent.mkdir(parents=True)
    self.paths.config.write_text("owned", encoding="utf-8")
    with self.assertRaisesRegex(SetupError, "already exists"):
        write_fresh_setup(
            self.paths,
            SetupAnswers.defaults(),
            {"MINICLAW_MODEL_API_KEY": "x"},
            sandbox_image="ghcr.io/nedonion/miniclaw-sandbox@sha256:" + "a" * 64,
        )
    with self.assertRaisesRegex(SetupError, "unsafe secret"):
        validate_secret_value("line1\nline2")
```

Also parse `build_parser()` actions and assert `--api-key`, `--token`, `--app-secret` do not exist anywhere.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_setup tests.test_bootstrap tests.test_cli -v`

Expected: FAIL because setup module and command are absent.

- [ ] **Step 3: Implement typed setup and private writes**

```python
@dataclass(frozen=True, slots=True)
class SetupAnswers:
    """保存 fresh setup 的非 Secret 选择。"""
    enable_feishu: bool
    feishu_owner_open_id: str | None
    enable_telegram: bool
    telegram_owner_user_id: int | None
    enable_discord: bool
    discord_owner_user_id: int | None

def validate_secret_value(value: str) -> str:
    """拒绝 dotenv 无法安全表达的 Secret。"""
    if not value or value != value.strip() or value[0] in "'\"" or any(c in value for c in "\r\n\0"):
        raise SetupError("unsafe secret value")
    return value
```

Make `render_default_config(paths, *, sandbox_image: str | None = None)` public in `bootstrap.py`; when provided, require `ghcr.io/nedonion/miniclaw-sandbox@sha256:` plus 64 lowercase hex characters. `setup.py` first creates/validates `paths.home` as owner-only 0700 without following a symlink, appends only enabled Channel tables with fixed env variable names and validated Owner IDs, writes config and `secrets.env` with `O_CREAT|O_EXCL`, mode `0600`, fsync, then calls `initialize_state`. Interactive reads non-Secrets from `/dev/tty`, reads Secrets with `getpass.getpass`, never prints values, and always permits selecting zero Channels. Existing config or Secret file returns a safe error instead of merging unknown TOML. `miniclaw setup` and `miniclaw init` accept only the non-Secret `--sandbox-image`; installer always supplies the digest from the verified manifest, while source development can omit it and retain the Phase 6 default.

- [ ] **Step 4: Verify CLI behavior**

Run:

```bash
uv run python -m unittest tests.test_setup tests.test_bootstrap tests.test_cli -v
uv run miniclaw setup --help
```

Expected: PASS; help includes `--home` and `--sandbox-image` only and contains no Secret-valued option. `miniclaw init` remains idempotent; `miniclaw setup` is fresh-state only.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/setup.py src/miniclaw/bootstrap.py src/miniclaw/cli.py tests/test_setup.py tests/test_bootstrap.py tests/test_cli.py
git commit -m "feat(setup): 增加 no-argv Secret onboarding"
```

---

### Task 4: Strict Release manifest and installer request models

**Files:**
- Create: `src/miniclaw/install/__init__.py`
- Create: `src/miniclaw/install/models.py`
- Create: `release/manifest.schema.json`
- Create: `tests/test_install_models.py`
- Create: `tests/install/manifest_v1.json`

**Interfaces:**
- Consumes: version `0.7.0`, schema v5 or the final Phase 6 `LATEST_SCHEMA_VERSION` found in preflight.
- Produces: `ReleaseManifest.from_bytes(data)`, `Artifact`, `PlatformKey`, `NodePolicy`, `InstallRequest`, `InstallPlan`, `InstallEvent`; stable `InstallError(code, detail)`.

- [ ] **Step 1: Write strict-schema RED tests**

```python
def test_manifest_accepts_exact_v1_and_selects_one_platform_artifact(self) -> None:
    manifest = ReleaseManifest.from_bytes(self.fixture.read_bytes())
    self.assertEqual(manifest.version, "0.7.0")
    selected = manifest.require_artifact("tui", PlatformKey("linux", "x86_64"))
    self.assertEqual(selected.component_version, "0.7.0")

def test_manifest_rejects_unknown_duplicate_and_untrusted_values(self) -> None:
    for mutation, code in (
        ({"mystery": True}, "manifest_invalid"),
        ({"product": "other"}, "manifest_invalid"),
        ({"version": "v0.7"}, "manifest_invalid"),
    ):
        with self.subTest(mutation=mutation), self.assertRaisesRegex(InstallError, code):
            ReleaseManifest.from_bytes(self.mutate_fixture(mutation))
    with self.assertRaisesRegex(InstallError, "manifest_invalid"):
        ReleaseManifest.from_bytes(self.fixture_with_duplicate_artifact())
```

Add cases for non-40-hex commit, missing/uppercase hash, zero/oversized size, URL credentials/query/fragment, HTTP, wrong owner/repository, unknown feature, unsupported platform, invalid Node ranges, `minimum_readable_schema > database_schema`, bool where int is required, and JSON larger than 1 MiB.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_install_models -v`

Expected: FAIL because `miniclaw.install.models` is absent.

- [ ] **Step 3: Implement exact immutable types**

```python
class InstallError(RuntimeError):
    """表示带稳定代码且 detail 已脱敏/截断的安装失败。"""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail[:500]
        super().__init__(f"{code}: {self.detail}")

@dataclass(frozen=True, slots=True)
class PlatformKey:
    """标识一个 Release artifact 目标。"""
    os: Literal["linux", "macos", "any"]
    arch: Literal["x86_64", "arm64", "any"]

@dataclass(frozen=True, slots=True)
class NodeRange:
    """限制一个受支持 Node major 的最小和排他上界。"""
    minimum: tuple[int, int, int]
    maximum_exclusive: tuple[int, int, int]

@dataclass(frozen=True, slots=True)
class NodePolicy:
    """保存默认 Node 与允许复用的两个 LTS 范围。"""
    default: tuple[int, int, int]
    accepted: tuple[NodeRange, ...]

@dataclass(frozen=True, slots=True)
class Artifact:
    """描述一个有 size/hash/source/license 约束的 Release 文件。"""
    kind: Literal[
        "wheel", "sdist", "requirements", "node", "tui", "sandbox-image",
        "runtime-image", "installer", "sbom",
    ]
    filename: str
    url: str
    sha256: str
    size: int
    media_type: str
    platform: PlatformKey
    component_version: str
    source_repository: str
    license_ref: str
    upstream_sha256: str | None

@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """保存一个已完成 strict schema 校验的同源 Release。"""
    schema_version: Literal[1]
    product: Literal["miniclaw"]
    version: str
    git_commit: str
    python: Literal["3.12"]
    node: NodePolicy
    artifacts: tuple[Artifact, ...]
    supported_platforms: tuple[PlatformKey, ...]
    features: tuple[str, ...]
    database_schema: int
    minimum_readable_schema: int

@dataclass(frozen=True, slots=True)
class InstallRequest:
    """保存 install/update/uninstall 的全部非 Secret 请求参数。"""
    action: Literal["install", "update", "uninstall"]
    version: str | None
    channel: Literal["stable", "dev"]
    prefix: Path | None
    state_home: Path
    system_prefix: bool
    onboard: bool
    config_file: Path | None
    secrets_file: Path | None
    service: bool | None
    allow_system_packages: bool
    dry_run: bool
    json_output: bool
    verbose: bool
    purge_data: bool
    confirm_data_loss: bool

@dataclass(frozen=True, slots=True)
class InstallEvent:
    """保存一个可安全输出的人类/NDJSON 安装事件。"""
    name: str
    status: Literal["start", "ok", "warn", "error"]
    code: str | None
    detail: str

@dataclass(frozen=True, slots=True)
class InstallPlan:
    """保存确认前可完整展示且不含 Secret 的安装计划。"""
    request: InstallRequest
    manifest: ReleaseManifest
    platform: PlatformKey
    distro_id: str
    distro_version: str
    service_manager: Literal["systemd-user", "launchd"]
    program_prefix: Path
    state_home: Path
    artifact_filenames: tuple[str, ...]
    system_argvs: tuple[tuple[str, ...], ...]
    install_service: bool
    run_onboarding: bool

    def safe_summary(self) -> str:
        """返回不含 config/Secret 内容的单行计划摘要。"""
        return (
            f"version={self.manifest.version} platform={self.platform.os}/{self.platform.arch} "
            f"prefix={self.program_prefix} service={self.install_service} "
            f"onboarding={self.run_onboarding}"
        )
```

Use exact-key set comparison before constructing every object; reject bool-as-int; parse semver with one anchored regex; cap artifacts at 128; require unique filenames and unique `(kind, platform, component_version, media_type)` identities; `require_artifact` fails unless its component/platform query returns exactly one row. Use `urllib.parse.urlsplit` and the design allowlist. The JSON schema mirrors these constraints with `additionalProperties: false`.

- [ ] **Step 4: Run GREEN**

Run: `uv run python -m unittest tests.test_install_models -v`

Expected: all strict positive/negative cases PASS and errors contain only stable code plus bounded field name.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/install release/manifest.schema.json tests/test_install_models.py tests/install/manifest_v1.json
git commit -m "feat(installer): 定义 strict Release manifest v1"
```

---

### Task 5: Tier 1 detection, Node policy and explicit privilege plan

**Files:**
- Create: `src/miniclaw/install/platforms.py`
- Create: `release/runtime-versions.json`
- Create: `tests/test_install_platforms.py`

**Interfaces:**
- Consumes: `PlatformKey`, `InstallRequest`, `/etc/os-release` text or `platform.mac_ver()` facts.
- Produces: `DetectedPlatform`, `PrivilegeAction`, `detect_platform(...)`, `build_dependency_actions(platform, facts)`, `node_version_supported(version)`.

- [ ] **Step 1: Write full matrix RED tests**

```python
def test_every_tier1_id_maps_to_one_release_platform(self) -> None:
    for distro, version in (("ubuntu", "22.04"), ("ubuntu", "24.04"),
                            ("debian", "12"), ("debian", "13"),
                            ("rhel", "9.6"), ("rocky", "10.0"), ("almalinux", "9.5")):
        for machine, arch in (("x86_64", "x86_64"), ("aarch64", "arm64")):
            detected = detect_linux(self.os_release(distro, version), machine)
            self.assertEqual(detected.artifact_platform, PlatformKey("linux", arch))

def test_node_policy_rejects_eol_odd_and_unvalidated_major(self) -> None:
    accepted = ((22, 22, 3), (22, 99, 0), (24, 15, 0), (24, 18, 0))
    rejected = ((20, 99, 0), (22, 22, 2), (23, 9, 0), (24, 14, 9), (25, 1, 0), (26, 0, 0))
    self.assertTrue(all(node_version_supported(v) for v in accepted))
    self.assertTrue(all(not node_version_supported(v) for v in rejected))
```

Add unsupported tests for Ubuntu 20.04/26.04, Debian 11, RHEL 8, macOS 12, musl, WSL, 32-bit, unknown ID, non-systemd service request, root invocation without original user, and bool/injected package fact. Assert `--dry-run` plan contains exact argv arrays and no sudo action is marked approved by default.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_install_platforms -v`

Expected: FAIL because detection and runtime pins are absent.

- [ ] **Step 3: Implement deterministic adapters and exact pins**

```python
@dataclass(frozen=True, slots=True)
class DetectedPlatform:
    """保存通过 Tier 1 校验的本机事实。"""
    os: Literal["linux", "macos"]
    distro_id: str
    distro_version: str
    arch: Literal["x86_64", "arm64"]
    service_manager: Literal["systemd-user", "launchd"]
    artifact_platform: PlatformKey
    sandbox_backend: Literal["docker-rootless", "podman-rootless", "seatbelt"]

@dataclass(frozen=True, slots=True)
class PrivilegeAction:
    """保存一条需要单独展示/确认的高权限 exact argv。"""
    category: Literal["system-package", "linger", "system-prefix"]
    argv: tuple[str, ...]
    requires_sudo: bool
    reason: str
```

`release/runtime-versions.json` contains uv 0.12.0 with these upstream hashes:

```text
linux-x86_64  eaf842262aa1c418d8ecc5605f02ee1ebfd369124fa48548e85f9481a47831a9
linux-arm64   2c5d6e3092cc5223b10ff403880cc75121bf64e84644e7a0c69f643b0d89ac95
macos-x86_64  d41593beaefc54bab7d062af0ef6ca093bfb81d001d58ebbef39e44423f9c496
macos-arm64   2b9e582af54f84fa50c115427451a6c13e80f43b52f8282b8af5791077317bbf
```

It also contains Node 24.18.0 official `.tar.gz` upstream hashes:

```text
linux-x86_64  783130984963db7ba9cbd01089eaf2c2efb055c7c1693c943174b967b3050cb8
linux-arm64   6b4484c2190274175df9aa8f28e2d758a819cb1c1fe6ab481e2f95b463ab8508
macos-x86_64  dfd0dbd3e721503434df7b7205e719f61b3a3a31b2bcf9729b8b91fea240f080
macos-arm64   e1a97e14c99c803e96c7339403282ea05a499c32f8d83defe9ef5ec66f979ed1
```

Set pnpm to 10.14.0. Parse `/etc/os-release` without executing it. Normalize `amd64/x86_64 -> x86_64`, `aarch64/arm64 -> arm64`. Package actions are fixed per distro family and never interpolate remote text:

```text
Ubuntu/Debian: sudo apt-get update
Ubuntu/Debian: sudo apt-get install -y docker.io rootlesskit uidmap dbus-user-session slirp4netns fuse-overlayfs
RHEL/Rocky/Alma: sudo dnf install -y podman-docker slirp4netns fuse-overlayfs shadow-utils dbus-daemon
```

On Debian-family hosts, select only an executable regular file from `/usr/bin/dockerd-rootless-setuptool.sh` and `/usr/share/docker.io/contrib/dockerd-rootless-setuptool.sh`, run it with exact argument `install` as the target non-root user, and verify its user socket/context; never start the root daemon or add docker group membership. On RHEL-family hosts, verify `/usr/bin/docker` resolves to the rootless Podman compatibility CLI and passes the Phase 6 containment smoke. macOS uses built-in Seatbelt by default; an existing absolute Homebrew may manage optional Docker/Colima, but installer never installs Homebrew. If the selected Tier 1 backend cannot be established, interactive refusal or noninteractive missing `--allow-system-packages` returns `system_dependency_missing` before activation; a stable full install cannot silently continue without Sandbox.

Headless Linux service may add only `("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger", validated_user)` after a separate linger confirmation. `--system-prefix` never silently re-execs with sudo: a non-root call returns the exact rerun command; the root call requires `SUDO_USER`/`SUDO_UID` resolving to a real non-root account, installs program files under `/usr/local/lib/miniclaw`, and always creates state/onboarding/systemd-user service as that account. Root without an original non-root user returns `privilege_denied`.

- [ ] **Step 4: Run GREEN**

Run: `uv run python -m unittest tests.test_install_platforms -v`

Expected: all Tier 1 facts map deterministically; unsupported cases return `unsupported_platform` before a writer or downloader is called.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/install/platforms.py release/runtime-versions.json tests/test_install_platforms.py
git commit -m "feat(installer): 固化 Tier 1 与 privilege InstallPlan"
```

---

### Task 6: Verified download and bounded archive extraction

**Files:**
- Create: `src/miniclaw/install/artifacts.py`
- Create: `src/miniclaw/install/releases.py`
- Create: `tests/test_install_artifacts.py`
- Create: `tests/test_install_releases.py`
- Create: `tests/install/make_archives.py`

**Interfaces:**
- Consumes: validated `Artifact` and a destination under owner-only staging.
- Produces: `resolve_release_source(channel, version, opener=None) -> ReleaseSource`; `download_artifact(artifact, destination, opener=None) -> Path`; `extract_tar_gz(archive, destination, limits) -> tuple[Path, ...]`; `ExtractionLimits`.

- [ ] **Step 1: Write adversarial RED tests**

```python
def test_download_checks_content_length_stream_size_and_hash_before_replace(self) -> None:
    target = self.root / "artifact.tar.gz"
    opener = FakeOpener(body=b"trusted", content_length=7)
    result = download_artifact(self.artifact(body=b"trusted"), target, opener=opener)
    self.assertEqual(result.read_bytes(), b"trusted")
    self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)

def test_tar_rejects_every_escaping_or_special_member(self) -> None:
    for archive in self.archives("absolute", "dotdot", "symlink", "hardlink", "device",
                                 "fifo", "duplicate", "too_many", "too_large"):
        with self.subTest(archive=archive), self.assertRaisesRegex(InstallError, "manifest_invalid|artifact_hash_mismatch"):
            extract_tar_gz(archive, self.output, ExtractionLimits(32, 4096))
        self.assertEqual(list(self.output.rglob("*")), [])
```

Also test short read, content-length mismatch, hash mismatch, redirect outside allowlist, credentials/query/fragment, timeout, interrupted `.part`, pre-existing target, case-colliding paths on macOS semantics, mode stripping, and no log body leakage.

`tests/test_install_releases.py` covers fixed `v0.7.0`, stable latest redirect, bounded `api.github.com/repos/NEDONION/miniclaw/releases?per_page=20` dev discovery, draft exclusion, prerelease requirement, semver ordering, oversized/malformed API JSON and wrong repository/asset name. Installed downgrade/equal-version refusal is tested in Task 11/13 so a fresh machine may explicitly install an older supported Release. All HTTP responses use fakes.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_install_artifacts tests.test_install_releases -v`

Expected: FAIL because artifact transport and safe extraction are absent.

- [ ] **Step 3: Implement streaming verification and manual extraction**

```python
@dataclass(frozen=True, slots=True)
class ReleaseSource:
    """保存一个已收窄的 manifest 来源和可选嵌入 hash。"""
    channel: Literal["stable", "dev"]
    requested_version: str | None
    manifest_url: str
    expected_sha256: str | None

@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    """限制 archive member 数和实际输出字节。"""
    max_entries: int = 20_000
    max_bytes: int = 1_073_741_824

def _safe_member_path(root: Path, name: str) -> Path:
    """把 POSIX archive name 限制在 root 内。"""
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise InstallError("manifest_invalid", "unsafe archive path")
    target = root.joinpath(*pure.parts)
    if not target.resolve(strict=False).is_relative_to(root.resolve()):
        raise InstallError("manifest_invalid", "archive path escapes destination")
    return target
```

`resolve_release_source` maps explicit versions to `https://github.com/NEDONION/miniclaw/releases/download/v<version>/release-manifest.json`, stable to `https://github.com/NEDONION/miniclaw/releases/latest/download/release-manifest.json`, and dev to the bounded GitHub Releases API response's exact `release-manifest.json` browser-download URL. It rejects stable prereleases, dev drafts and repositories outside `NEDONION/miniclaw`.

Use a redirect handler that revalidates every Location. The initial host allowlist is `github.com`, `api.github.com`, `files.pythonhosted.org`, `nodejs.org` and official Astral endpoints; only a request originating at `github.com/NEDONION/miniclaw/releases/` may redirect to HTTPS `release-assets.githubusercontent.com`. Stream into an `O_EXCL` `.part`, cap bytes before write, fsync, compare exact size/hash with `hmac.compare_digest`, then `os.replace`. For tar, first validate all headers and cumulative declared sizes, then reopen and copy only regular files/directories with a second actual-byte budget; create dirs 0700/files 0600 or executable 0700 only for manifest-declared executable paths; fsync and leave destination empty on failure.

- [ ] **Step 4: Run GREEN**

Run: `uv run python -m unittest tests.test_install_artifacts tests.test_install_releases -v`

Expected: all malicious fixtures fail closed and the valid tar extracts reproducibly without links or special files.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/install/artifacts.py src/miniclaw/install/releases.py tests/test_install_artifacts.py tests/test_install_releases.py tests/install/make_archives.py
git commit -m "feat(installer): 校验 downloads 与 bounded safe extract"
```

---

### Task 7: Install layout, lock, receipt and stable launcher ownership

**Files:**
- Create: `src/miniclaw/install/layout.py`
- Create: `src/miniclaw/install/receipt.py`
- Create: `tests/test_install_layout.py`
- Create: `tests/test_install_receipt.py`

**Interfaces:**
- Consumes: absolute program prefix, state home, version and `PlatformKey`.
- Produces: `InstallLayout.for_request(request, user)`, `InstallLayout.for_plan(plan)`, `InstallLock.acquire(layout)`, `InstallReceipt.load/write`, `render_launcher(layout) -> bytes`, `managed_file_sha256(path)`.

- [ ] **Step 1: Write filesystem safety RED tests**

```python
def test_default_layout_separates_runtime_from_user_state(self) -> None:
    layout = InstallLayout.user(self.home, version="0.7.0")
    self.assertEqual(layout.program_prefix, self.home / ".miniclaw")
    self.assertEqual(layout.state_home, self.home / ".miniclaw")
    self.assertEqual(layout.runtime, layout.program_prefix / "runtimes" / "0.7.0")
    self.assertEqual(layout.secrets_file, layout.state_home / "secrets.env")
    self.assertEqual(layout.command_link, self.home / ".local" / "bin" / "miniclaw")

def test_lock_and_receipt_prevent_concurrent_or_foreign_overwrite(self) -> None:
    first = InstallLock.acquire(self.layout)
    self.addCleanup(first.close)
    with self.assertRaisesRegex(InstallError, "install_locked"):
        InstallLock.acquire(self.layout)
    self.foreign_launcher.write_text("owner data", encoding="utf-8")
    with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
        verify_managed_file(self.foreign_launcher, self.receipt.launcher_sha256)
```

Add `/`, Home root, relative, symlink prefix, group/world writable parent, stale/live PID lock, corrupt/unknown receipt, wrong uid, launcher hash mismatch, atomic receipt crash, prefix containing spaces/quotes, and system-prefix state ownership cases.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_install_layout tests.test_install_receipt -v`

Expected: FAIL because layout and receipt modules are absent.

- [ ] **Step 3: Implement immutable layout and stable launcher**

```python
@dataclass(frozen=True, slots=True)
class InstallLayout:
    """描述受管程序、共享状态与一个 staging Runtime 的路径。"""
    program_prefix: Path
    state_home: Path
    bin_dir: Path
    runtimes_dir: Path
    current: Path
    staging: Path
    runtime: Path
    launcher: Path
    command_link: Path
    receipt: Path
    lock: Path

@dataclass(frozen=True, slots=True)
class InstallReceipt:
    """记录受管程序文件，不记录任何用户数据或 Secret。"""
    schema_version: Literal[1]
    version: str
    git_commit: str
    platform: PlatformKey
    installed_at: str
    managed_files: tuple[tuple[str, str], ...]
    current_runtime: str
    previous_runtime: str | None
    service_label: str | None
    service_file: str | None
    service_file_sha256: str | None

def render_launcher(layout: InstallLayout) -> bytes:
    """生成只跟随 current 且不依赖 shell rc 的 POSIX launcher。"""
    prefix = shlex.quote(str(layout.program_prefix))
    home = shlex.quote(str(layout.state_home))
    return (
        "#!/bin/sh\nset -eu\n"
        f"MINICLAW_PREFIX={prefix}\nMINICLAW_HOME={home}\n"
        'MINICLAW_NODE="$MINICLAW_PREFIX/current/node/bin/node"\n'
        'MINICLAW_TUI_ENTRY="$MINICLAW_PREFIX/current/tui/dist/main.js"\n'
        'MINICLAW_ENV_FILE="$MINICLAW_HOME/secrets.env"\n'
        "export MINICLAW_HOME MINICLAW_NODE MINICLAW_TUI_ENTRY MINICLAW_ENV_FILE\n"
        'exec "$MINICLAW_PREFIX/current/venv/bin/python" -m miniclaw "$@"\n'
    ).encode("utf-8")
```

Lock uses `O_CREAT|O_EXCL` and stores only pid/uid/start UTC; stale removal requires same uid and confirmed dead pid. User mode creates `~/.local/bin/miniclaw` as a relative symlink to the stable launcher, system-prefix mode uses `/usr/local/bin/miniclaw`; either path must be absent or match the prior receipt/hash, and installer never edits shell profiles. Ownership hash is SHA-256 of regular file bytes or `b"symlink\0" + os.readlink(path).encode()` without following the link. Receipt JSON uses exact keys, relative managed paths, hashes and UTC timestamp, mode 0600, temp+fsync+replace. Never hash or record config, Secret, DB, Memory, Skills, Workspace or logs.

- [ ] **Step 4: Run GREEN**

Run: `uv run python -m unittest tests.test_install_layout tests.test_install_receipt -v`

Expected: PASS; executing rendered launcher against a fake versioned Python preserves each argv element and exports only path-valued MiniClaw variables.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/install/layout.py src/miniclaw/install/receipt.py tests/test_install_layout.py tests/test_install_receipt.py
git commit -m "feat(installer): 增加 managed layout lock 与 receipt"
```

---

### Task 8: Managed Python Runtime and atomic activation

**Files:**
- Create: `src/miniclaw/install/runtime.py`
- Create: `requirements-all.lock`
- Create: `tests/test_install_runtime.py`
- Create: `tests/install/fake_uv.py`

**Interfaces:**
- Consumes: `InstallLayout`, verified wheel/requirements/Node/TUI/installer-pyz paths, managed uv executable, bootstrap-verified managed Python root/executable and `ReleaseManifest`.
- Produces: `RuntimeBuilder.build(...) -> RuntimeReceipt`, `RuntimeBuilder.smoke(...)`, `activate_runtime(layout, receipt)`, `retain_current_and_previous(layout)`.

- [ ] **Step 1: Write command/order/crash RED tests**

```python
def test_runtime_installs_hash_locked_dependencies_then_verified_wheel(self) -> None:
    receipt = self.builder.build(self.inputs)
    self.assertEqual(self.runner.argvs[0], (str(self.uv), "venv", "--python", "3.12", str(self.layout.staging / "venv")))
    self.assertIn("--require-hashes", self.runner.argvs[1])
    self.assertEqual(self.runner.argvs[2][-2:], ("--no-deps", str(self.inputs.wheel)))
    self.assertEqual(receipt.version, "0.7.0")

def test_failed_smoke_never_switches_current(self) -> None:
    self.current.symlink_to(self.old_runtime)
    self.runner.fail_when_argument("--runtime-smoke")
    with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
        self.builder.install_and_activate(self.inputs)
    self.assertEqual(self.current.resolve(), self.old_runtime.resolve())
```

Add tests for exact environment (`UV_NO_CONFIG=1`, `UV_NO_ENV_FILE=1`, private cache), wheel metadata mismatch, missing console entry, broken Channel import, wrong Python/Node/TUI version, staging collision, chmod failure, atomic symlink replace, N/N-1 retention and not deleting a runtime referenced by current/previous receipt.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_install_runtime -v`

Expected: FAIL because RuntimeBuilder and lockfile are absent.

- [ ] **Step 3: Generate lock and implement exact build sequence**

```python
@dataclass(frozen=True, slots=True)
class RuntimeReceipt:
    """记录一个尚未激活 Runtime 的可验证构建结果。"""
    version: str
    git_commit: str
    runtime_relative: str
    python_version: str
    node_version: str
    tui_version: str
    wheel_sha256: str
    requirements_sha256: str
    node_sha256: str
    tui_sha256: str
    installer_sha256: str
```

Generate and review:

```bash
uv export --locked --all-extras --no-dev --no-emit-project \
  --format requirements.txt --output-file requirements-all.lock
```

After export, a test parses every non-comment logical requirement and requires an exact pin/direct URL plus at least one `--hash=sha256:`. The universal `uv.lock` remains the resolver fact; installer never resolves again.

Runtime argv sequence is:

```text
uv venv --relocatable --python <staging>/python/bin/python3.12 --no-python-downloads <staging>/venv
uv pip install --python <staging>/venv/bin/python --require-hashes -r requirements-all.lock
uv pip install --python <staging>/venv/bin/python --no-deps <verified-wheel>
<staging>/venv/bin/python -I -m miniclaw --version
<staging>/venv/bin/python -I -m miniclaw install-smoke --json
<staging>/node/bin/node <staging>/tui/dist/main.js --smoke
```

Copy the bootstrap-verified managed Python tree into `staging/python` through descriptor-bound no-follow reads, then build the venv from that explicit internal interpreter with downloads disabled. Copy only verified Node/TUI directories from safe extraction plus verified `miniclaw-installer.pyz`, chmod dirs 0700 and files 0600/required executables 0700, write runtime receipt/manifest 0600 and fsync the tree boundary. Before activation, publish staging as immutable `runtimes/0.7.0`, repair only the venv's interpreter link/config to the known final-internal Python path, and rerun an exact final-path smoke which requires both the resolved executable and `sys.base_prefix` to remain under that Runtime. A bootstrap/system/user Python outside the Runtime is never accepted. Activation creates a relative `current.next` symlink and atomically switches it over `current`; no in-place venv update exists.

- [ ] **Step 4: Run GREEN**

Run:

```bash
uv run python -m unittest tests.test_install_runtime -v
uv run python -m unittest tests.test_package_metadata -v
```

Expected: PASS; fake uv proves exact argv/order; every injected failure leaves old `current` usable and staging removable.

- [ ] **Step 5: Commit**

```bash
git add requirements-all.lock src/miniclaw/install/runtime.py tests/test_install_runtime.py tests/install/fake_uv.py
git commit -m "feat(runtime): 构建 hash-locked atomic Runtime"
```

---

### Task 9: Reproducible managed Node and pi-tui bundles

**Files:**
- Create: `pnpm-workspace.yaml`
- Modify: `tui/package.json`
- Modify: `tui/src/main.ts`
- Create: `tui/test/smoke.test.ts`
- Modify: `src/miniclaw/tui_launcher.py`
- Modify: `tests/test_tui_launcher.py`
- Create: `scripts/build_node_bundle.py`
- Create: `scripts/build_tui_bundle.py`
- Create: `tests/test_release_bundles.py`

**Interfaces:**
- Consumes: runtime pins, pnpm lock, official Node archive already checked against upstream hash.
- Produces: `miniclaw-node-24.18.0-<os>-<arch>.tar.gz`, `miniclaw-tui-0.7.0-<os>-<arch>.tar.gz`, `node_version_supported`, `main.js --smoke`.

- [ ] **Step 1: Write Node/TUI isolation RED tests**

```python
def test_launcher_accepts_only_validated_lts_ranges(self) -> None:
    for version in ((22, 22, 3), (24, 15, 0), (24, 18, 0)):
        self.assertTrue(is_supported_node_version(version))
    for version in ((22, 22, 2), (23, 0, 0), (25, 0, 0), (26, 0, 0)):
        self.assertFalse(is_supported_node_version(version))

def test_release_bundles_have_no_link_dev_cache_or_dev_dependency(self) -> None:
    for bundle in (self.node_bundle, self.tui_bundle):
        with tarfile.open(bundle, "r:gz") as archive:
            self.assertTrue(all(member.isreg() or member.isdir() for member in archive))
            names = {member.name for member in archive}
        self.assertFalse(any(".pnpm-store" in name or "/typescript/" in name for name in names))
    self.assertIn("tui/dist/main.js", names)
    self.assertIn("tui/node_modules/@earendil-works/pi-tui/package.json", names)
```

Node tests verify the executable reports exactly `v24.18.0`. TUI tests unpack into an empty temp directory, clear global Node module paths, set only managed Node/Python/TUI env paths, run `main.js --smoke`, and expect one JSON object with `{"component":"pi-tui","version":"0.7.0","status":"ok"}`.

- [ ] **Step 2: Run RED**

Run:

```bash
uv run python -m unittest tests.test_tui_launcher tests.test_release_bundles -v
corepack pnpm --dir tui test
```

Expected: FAIL because ranges still use `>=22.19`, smoke flag and bundle builders are absent.

- [ ] **Step 3: Implement exact LTS contract and bundle builders**

Set package engine to `>=22.22.3 <23 || >=24.15.0 <25`, keep pnpm 10.14.0, add root workspace with package `tui`, and add `release:deploy` script using pnpm 10 `deploy --prod`. `main.ts` handles `--smoke` before TTY checks and imports `@earendil-works/pi-tui` without spawning Bridge.

`build_node_bundle.py` validates official archive hash from runtime pins, extracts only regular `bin/node` and `LICENSE`, verifies `node --version`, and writes a deterministic gzip tar with uid/gid/mtime zero. `build_tui_bundle.py` runs build/test/deploy, captures `pnpm licenses list --prod --json`, resolves every internal staging symlink only when its final target remains inside staging, replaces it with regular content, rejects cycles/escapes, strips dev files/cache, and writes deterministic tar metadata.

- [ ] **Step 4: Run GREEN at both supported Node floors**

Run:

```bash
corepack prepare pnpm@10.14.0 --activate
corepack pnpm install --frozen-lockfile
corepack pnpm --dir tui test
uv run python -m unittest tests.test_tui_launcher tests.test_release_bundles -v
```

CI repeats TypeScript test/bundle smoke with Node 22.22.3 and 24.18.0. Expected: both PASS; bundle smoke succeeds with repository `node_modules` renamed out of view.

- [ ] **Step 5: Commit**

```bash
git add pnpm-workspace.yaml tui/package.json tui/src/main.ts tui/test/smoke.test.ts src/miniclaw/tui_launcher.py tests/test_tui_launcher.py scripts/build_node_bundle.py scripts/build_tui_bundle.py tests/test_release_bundles.py
git commit -m "build(tui): 生成 symlink-free managed Node/TUI bundles"
```

---

### Task 10: systemd user and LaunchAgent lifecycle

**Files:**
- Create: `src/miniclaw/install/service.py`
- Create: `tests/test_install_service.py`
- Create: `tests/install/fake_systemctl.py`
- Create: `tests/install/fake_launchctl.py`

**Interfaces:**
- Consumes: `InstallLayout.launcher`, `state_home`, logs, receipt ownership and detected service manager.
- Produces: `ServiceSpec`, `render_service_spec(layout, platform)`, `service_install/status/logs/restart/uninstall(spec, runner)`.

- [ ] **Step 1: Write exact-content and lifecycle RED tests**

```python
def test_systemd_unit_uses_stable_launcher_and_no_secret_value(self) -> None:
    spec = render_service_spec(self.layout, ServicePlatform.SYSTEMD_USER)
    self.assertIn(f"ExecStart={self.layout.launcher} gateway --home {self.layout.state_home}", spec.content)
    self.assertIn("Restart=on-failure", spec.content)
    self.assertIn("RestartSec=5", spec.content)
    self.assertIn(f"Environment=MINICLAW_ENV_FILE={self.layout.secrets_file}", spec.content)
    self.assertNotIn(self.sentinel, spec.content)

def test_launchd_uses_program_arguments_and_owner_logs(self) -> None:
    spec = render_service_spec(self.layout, ServicePlatform.LAUNCHD)
    parsed = plistlib.loads(spec.content.encode("utf-8"))
    self.assertEqual(parsed["Label"], "io.miniclaw.gateway")
    self.assertEqual(parsed["ProgramArguments"], [str(self.layout.launcher), "gateway", "--home", str(self.layout.state_home)])
```

Add exact command tests for `systemctl --user daemon-reload/enable --now/is-active/restart/disable --now`, `journalctl --user-unit miniclaw-gateway.service`, `launchctl bootstrap/print/kickstart -k/bootout`, plutil lint before replace, idempotent install, foreign file hash mismatch, failure rollback and no root service user.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_install_service -v`

Expected: FAIL because service renderer/controller are absent.

- [ ] **Step 3: Implement render-validate-replace-register**

```python
class ServicePlatform(StrEnum):
    """列出 Tier 1 支持的两个用户级 service manager。"""
    SYSTEMD_USER = "systemd-user"
    LAUNCHD = "launchd"

@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """保存一个受管 service 文件和 exact manager commands。"""
    platform: ServicePlatform
    label: str
    path: Path
    content: bytes
    install_argvs: tuple[tuple[str, ...], ...]
    status_argv: tuple[str, ...]
    restart_argv: tuple[str, ...]
    uninstall_argvs: tuple[tuple[str, ...], ...]
```

systemd unit uses absolute launcher, explicit `--home`, minimal `/usr/local/bin:/usr/bin:/bin` PATH, `Environment=MINICLAW_ENV_FILE=...`, `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=30`, `UMask=0077`, and no `WorkingDirectory` dependency. LaunchAgent uses plistlib, `KeepAlive.SuccessfulExit=false`, `ProcessType=Background`, owner-only stdout/stderr files. Validate temp unit with `systemd-analyze --user verify` when available and plist with `/usr/bin/plutil -lint`; only then replace and register.

- [ ] **Step 4: Run GREEN**

Run: `uv run python -m unittest tests.test_install_service -v`

Expected: PASS; fake managers prove exact order and cleanup; sentinel is absent from content, argv, event and error.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/install/service.py tests/test_install_service.py tests/install/fake_systemctl.py tests/install/fake_launchctl.py
git commit -m "feat(service): 管理 systemd user 与 LaunchAgent lifecycle"
```

---

### Task 11: Installer orchestrator and stdlib-only zipapp

**Files:**
- Create: `src/miniclaw/install/orchestrator.py`
- Create: `src/miniclaw/install/__main__.py`
- Create: `scripts/build_installer_zipapp.py`
- Create: `tests/test_install_orchestrator.py`
- Create: `tests/test_installer_zipapp.py`

**Interfaces:**
- Consumes: Tasks 4～10 models/platform/releases/artifacts/layout/runtime/service and subprocess invocation of the newly staged `miniclaw setup|init`.
- Produces: `Installer.run(request) -> InstallResult`, `emit_event(event)`, `python miniclaw-installer.pyz ...`, error-code exit mapping 0/2/3.

- [ ] **Step 1: Write state-machine and JSON RED tests**

```python
def test_install_runs_preflight_stage_smoke_activate_service_in_order(self) -> None:
    result = self.installer.run(self.request)
    self.assertEqual(self.recorder.names, [
        "install.preflight", "install.download", "install.staged", "install.smoke",
        "install.activated", "service.installed", "install.complete",
    ])
    self.assertEqual(result.version, "0.7.0")

def test_dry_run_has_zero_side_effect_and_redacted_ndjson(self) -> None:
    result = self.installer.run(replace(self.request, dry_run=True, json_output=True))
    self.assertEqual(self.filesystem.writes, [])
    self.assertEqual(self.downloader.calls, [])
    self.assertEqual(self.runner.argvs, [])
    self.assertNotIn(self.sentinel, "\n".join(event.detail for event in result.events))
```

Inject failures at preflight/download/hash/extract/venv/wheel/TUI/setup/doctor/activation/service and assert stable code, old current, stopped/unchanged service as appropriate, cleaned staging and lock release. Add tests proving initial fixed-version/dev selection verifies and execs exactly one target installer pyz before persistent writes, installed update does the same, and a second installer hop fails closed. Add stdin non-TTY flag conflict tests for version/channel, prefix/system-prefix, service flags, config/secrets files, purge confirmation, JSON stdout purity and bounded verbose stderr.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_install_orchestrator tests.test_installer_zipapp -v`

Expected: FAIL because orchestrator, entry point and zipapp builder are absent.

- [ ] **Step 3: Implement one transaction coordinator**

```python
@dataclass(frozen=True, slots=True)
class InstallResult:
    """保存一次安装动作的安全终态。"""
    action: Literal["install", "update", "uninstall"]
    version: str | None
    changed: bool
    events: tuple[InstallEvent, ...]

class Installer:
    """协调一次 install/update/uninstall，具体副作用委托给受测组件。"""

    def run(self, request: InstallRequest) -> InstallResult:
        platform = self.detect_platform(request)
        plan = self.build_plan(request, platform)
        self.emit(InstallEvent("install.preflight", "ok", None, plan.safe_summary()))
        if request.dry_run:
            return InstallResult(request.action, plan.manifest.version, False, tuple(self.events))
        layout = InstallLayout.for_plan(plan)
        with InstallLock.acquire(layout):
            return self._execute_locked(plan)
```

Initial install accepts only the internal `--manifest-file <absolute-temp-file> --manifest-sha256 <64-hex>` pair supplied by bootstrap and re-verifies it before parsing. If public `--version` names a different version or `--channel dev` selects a prerelease, this verified pyz resolves the target manifest, downloads/verifies that manifest's `installer` pyz and `os.execve`s the managed Python with `<target-installer.pyz>` plus target manifest/hash and the same public flags before any persistent write. `MINICLAW_INSTALLER_HOPS=1` prevents recursion, and the target pyz must match the selected manifest version.

Installed update calls `resolve_release_source`, downloads a bounded manifest, rejects downgrade/equal version, selects and verifies that manifest's `installer` pyz, then execs the target pyz with internal manifest file/hash and action `update`; target-Release code therefore owns its own migration. Install order: validate request/platform/manifest → build/confirm system actions → create private dirs → verified artifacts → Runtime build/smoke → staged `miniclaw setup --sandbox-image <verified-digest>` or `init --sandbox-image <verified-digest>` → local Doctor → activation → launcher/receipt → service install/health → retention. Service installation additionally requires at least one enabled Channel and all of that Channel's required environment names in the owner-only Secret file; selecting zero Channels leaves the TUI fully installed but does not create a Gateway service. `--no-onboard` without a structurally valid imported config, one enabled Channel and complete Secret file forces `install_service=False`; an explicit `--install-service` in that state returns `doctor_blocked` before activation. In system-prefix mode, root performs only verified program-file operations; every state, setup, Doctor and systemd-user command is launched with `/usr/bin/sudo -u <validated-user> --` and a fixed minimal environment, and no Secret is preserved from root or passed in argv. JSON mode prints one compact event per line to stdout; human mode prints status to stderr; no subprocess raw output is forwarded without redaction/cap.

`--config` and `--secrets-file` accept absolute regular non-symlink files only. Config is parsed by the staged Runtime before copy and cannot contain credential values under the strict schema; Secret import requires owner uid and mode 0600, is copied with `O_EXCL` to `StatePaths.secrets_file`, and only its validated environment variable names may enter events. Neither source path nor value enters receipt/log output.

`build_installer_zipapp.py` copies only `src/miniclaw/install`, parses every module AST, permits stdlib roots and relative `miniclaw.install` imports only, fixes source timestamps, and runs `zipapp.create_archive(..., interpreter="/usr/bin/env python3", main="miniclaw.install.__main__:main")`.

- [ ] **Step 4: Verify stdlib isolation**

Run:

```bash
uv run python scripts/build_installer_zipapp.py --output dist/miniclaw-installer.pyz
python3 -I dist/miniclaw-installer.pyz --help
uv run python -m unittest tests.test_install_orchestrator tests.test_installer_zipapp -v
```

Expected: PASS; pyz help works from an empty directory with site packages disabled; AST test rejects a synthetic `import httpx`.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/install/orchestrator.py src/miniclaw/install/__main__.py scripts/build_installer_zipapp.py tests/test_install_orchestrator.py tests/test_installer_zipapp.py
git commit -m "feat(installer): 编排 atomic install 与 stdlib zipapp"
```

---

### Task 12: Minimal pinned POSIX bootstrap

**Files:**
- Create: `release/install.sh.tmpl`
- Create: `scripts/render_install_script.py`
- Create: `tests/test_install_bootstrap.py`
- Create: `tests/install/fake_bootstrap_bin/`

**Interfaces:**
- Consumes: release tag/base URL, uv platform URLs/hashes, manifest/pyz size+hash.
- Produces: final GitHub Release artifact `install.sh`; forwards all supported installer flags unchanged.

- [ ] **Step 1: Write rendered-script RED tests**

```python
def test_rendered_bootstrap_has_no_floating_or_unresolved_value(self) -> None:
    script = render_install_script(self.release_inputs)
    self.assertNotIn("latest", script)
    self.assertNotIn("{{", script)
    self.assertIn("UV_VERSION='0.12.0'", script)
    self.assertIn("RELEASE_TAG='v0.7.0'", script)
    self.assertIn(self.release_inputs.manifest_sha256, script)
    self.assertIn(self.release_inputs.installer_sha256, script)

def test_hash_failure_stops_before_python_or_installer(self) -> None:
    completed = self.run_bootstrap(fake_manifest=b"tampered")
    self.assertNotEqual(completed.returncode, 0)
    self.assertEqual(self.fake_uv.calls, [])
    self.assertFalse((self.home / ".miniclaw").exists())
```

Add `sh -n`, unsupported OS/arch, missing TLS/hash/tar utility, private mktemp mode, interrupted curl, checksum command fallback, flag passthrough, signal cleanup, no shell profile mutation, no Secret read and installer exit-code propagation tests. Fixed-version/dev pyz handoff and recursion are covered in Task 11 because shell never discovers Releases.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_install_bootstrap -v`

Expected: FAIL because template and renderer are absent.

- [ ] **Step 3: Implement the nine bootstrap operations only**

The rendered shell uses `set -eu`, `umask 077`, `trap` cleanup, `mktemp -d`, `uname -s/-m`, exact case mapping, `curl -fL --proto '=https' --tlsv1.2 --retry 3`, `sha256sum` or `shasum -a 256`, and `tar -xzf`. It downloads the fixed uv 0.12.0 archive, verifies its embedded platform hash, extracts the uv executable, downloads exact `v0.7.0/release-manifest.json` and `miniclaw-installer.pyz`, verifies embedded size/hash, sets private `UV_PYTHON_INSTALL_DIR`, runs `uv python install 3.12`, resolves the managed interpreter with `uv python find --managed-python 3.12`, then `exec`s the pyz with internal `--manifest-file`, `--manifest-sha256`, `--managed-python-root`, `--managed-python-executable` and original public flags. The pyz binds both Python paths to the private bootstrap tree before Task 8 copies them into the versioned Runtime; it never resolves or downloads Python again. The shell never parses manifest JSON, edits config, invokes sudo, writes service files or touches `current`.

- [ ] **Step 4: Run GREEN and shell syntax gates**

Run:

```bash
uv run python -m unittest tests.test_install_bootstrap -v
uv run python scripts/render_install_script.py --fixture tests/install/bootstrap-release.json --output /tmp/miniclaw-install.sh
sh -n /tmp/miniclaw-install.sh
bash -n /tmp/miniclaw-install.sh
```

Expected: PASS; fake bootstrap reaches only the fake managed Python/pyz on valid hashes; all failure fixtures leave user state untouched.

- [ ] **Step 5: Commit**

```bash
git add release/install.sh.tmpl scripts/render_install_script.py tests/test_install_bootstrap.py tests/install/fake_bootstrap_bin
git commit -m "feat(bootstrap): 生成 pinned POSIX one-line installer"
```

---

### Task 13: Database-safe update, rollback and retention

**Files:**
- Create: `src/miniclaw/install/update.py`
- Modify: `src/miniclaw/install/orchestrator.py`
- Create: `tests/test_install_update.py`

**Interfaces:**
- Consumes: current/target receipts, manifest `database_schema` and `minimum_readable_schema`, `StatePaths.database`, service controller and Runtime activation.
- Produces: `DatabaseBackup.create`, `DatabaseChangeGuard`, `UpdateCoordinator.update(request)`, automatic N-1 restore or `rollback_conflict`.

- [ ] **Step 1: Write crash-window RED tests**

```python
def test_failed_new_service_restores_database_and_previous_runtime_without_new_write(self) -> None:
    before = self.database.read_bytes()
    self.new_runtime.migrate_to(6)
    self.service.fail_health()
    with self.assertRaisesRegex(InstallError, "activation_failed"):
        self.coordinator.update(self.request)
    self.assertEqual(self.current.resolve(), self.old_runtime.resolve())
    self.assertEqual(self.database.read_bytes(), before)
    self.assertTrue(self.old_service.running)

def test_external_write_after_new_runtime_starts_blocks_destructive_restore(self) -> None:
    self.service.on_start(lambda: self.external_connection.execute("INSERT INTO audit_events ..."))
    self.service.fail_health()
    with self.assertRaisesRegex(InstallError, "rollback_conflict"):
        self.coordinator.update(self.request)
    self.assertTrue(self.backup.exists())
    self.assertFalse(self.old_service.running)
```

Add failures before backup, during sqlite backup, migration, current replace, service refresh, health timeout and receipt commit; compatible schema no-backup shortcut is forbidden. Assert Memory/Skills/Workspace hashes never participate in restore/delete.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_install_update -v`

Expected: FAIL because update guard and coordinator are absent.

- [ ] **Step 3: Implement guarded migration and rollback**

```python
@dataclass(slots=True)
class DatabaseChangeGuard:
    """用持续只读连接检测预期 migration 后的新 commit。"""
    connection: sqlite3.Connection
    expected_data_version: int

    def refresh_after_expected_migration(self) -> None:
        self.expected_data_version = int(self.connection.execute("PRAGMA data_version").fetchone()[0])

    def has_external_commit(self) -> bool:
        current = int(self.connection.execute("PRAGMA data_version").fetchone()[0])
        return current != self.expected_data_version
```

Stop old service; open guard connection; use `sqlite3.Connection.backup` to an owner-only file, fsync, run new Runtime `miniclaw init --home` while service is stopped, refresh guard, switch current, refresh/start service, and wait bounded health. On failure stop new service; if guard unchanged restore DB through temp+fsync+replace and old current/service; if changed retain backup/new state and return `rollback_conflict` with recovery commands. Keep only current and N-1 after success; never remove runtime referenced by receipt/symlink.

- [ ] **Step 4: Run GREEN**

Run: `uv run python -m unittest tests.test_install_update -v`

Expected: every crash window has a deterministic final state; auto rollback occurs only with zero post-migration commits.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/install/update.py src/miniclaw/install/orchestrator.py tests/test_install_update.py
git commit -m "feat(update): 增加 DB-guarded atomic rollback"
```

---

### Task 14: Public service/update/uninstall CLI and install-aware Doctor

**Files:**
- Modify: `src/miniclaw/cli.py`
- Modify: `src/miniclaw/doctor.py`
- Modify: `src/miniclaw/install/orchestrator.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_doctor.py`
- Create: `tests/test_install_uninstall.py`

**Interfaces:**
- Consumes: installed receipt/layout, service/update coordinators and existing 27+ Phase 6 Doctor checks.
- Produces: `miniclaw service install|status|logs|restart|uninstall`, `miniclaw update`, `miniclaw uninstall [--purge-data]`; install facts in Doctor; `miniclaw install-smoke --json` internal gate.

- [ ] **Step 1: Write CLI/Doctor/uninstall RED tests**

```python
def test_service_update_and_uninstall_dispatch_outside_agent_runtime(self) -> None:
    for argv, action in ((["service", "status"], "service.status"),
                         (["update"], "update"), (["uninstall"], "uninstall")):
        with self.subTest(argv=argv), mock.patch("miniclaw.cli.run_install_action", return_value=0) as run:
            self.assertEqual(main(argv), 0)
            self.assertEqual(run.call_args.args[0], action)

def test_default_uninstall_preserves_all_user_data(self) -> None:
    before = self.hash_user_data()
    request = replace(self.install_request, action="uninstall", purge_data=False)
    self.uninstaller.run(request)
    self.assertEqual(self.hash_user_data(), before)
    self.assertFalse(self.layout.runtimes_dir.exists())
```

Add purge TTY double-confirmation, noninteractive `--yes-i-understand-data-loss`, reject `/`/Home/Workspace/symlink/foreign ownership, service removal first, partial failure preservation, self-uninstall pyz handoff before Runtime deletion, installed Runtime/Node/TUI/receipt/service checks, source checkout warning, JSON smoke imports all three Channel SDKs and Phase 6 modules, and Doctor no-network/no-Secret invariants.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_cli tests.test_doctor tests.test_install_uninstall -v`

Expected: FAIL because public lifecycle commands and install facts are absent.

- [ ] **Step 3: Add thin parser dispatch and receipt-bounded deletion**

CLI only parses non-Secret options and passes typed requests to install modules. Before deleting any Runtime, installed uninstall copies the receipt-matching `current/miniclaw-installer.pyz` to a 0700 private temp directory, verifies its hash again and execs it with action `uninstall`; a one-hop environment guard prevents recursion. Default uninstall then verifies hashes, stops/removes service, removes managed launcher/link/receipt/runtimes, preserves config, DB, `secrets.env`, Memory, Skills, Workspace and logs, and prints exact retained root. Purge enumerates explicit state paths, refuses broad/symlink targets, requires both TTY confirmation phrases; noninteractive additionally requires the exact long flag.

Doctor appends checks named `install_method`, `managed_runtime`, `managed_node`, `managed_tui`, `managed_service`, and `release_receipt` when a receipt exists; source mode reports `WARN` rather than failing. `install-smoke --json` validates version, metadata, entry point, Channel SDK imports, Phase 6 Automation/Sandbox/Checkpoint imports, Node policy, TUI entry and local DB schema without Provider/Channel/network calls.

- [ ] **Step 4: Run GREEN**

Run:

```bash
uv run python -m unittest tests.test_cli tests.test_doctor tests.test_install_uninstall -v
uv run miniclaw service --help
uv run miniclaw update --help
uv run miniclaw uninstall --help
```

Expected: PASS; help has no Secret flags; Doctor source mode stays usable; uninstall fixtures prove all personal data survives by default.

- [ ] **Step 5: Commit**

```bash
git add src/miniclaw/cli.py src/miniclaw/doctor.py src/miniclaw/install/orchestrator.py tests/test_cli.py tests/test_doctor.py tests/test_install_uninstall.py
git commit -m "feat(cli): 暴露 service update uninstall lifecycle"
```

---

### Task 15: Reproducible Release artifacts, checksums and manifest

**Files:**
- Create: `scripts/build_release_manifest.py`
- Create: `scripts/build_sbom.py`
- Create: `scripts/verify_release_artifacts.py`
- Create: `tests/test_release_manifest_build.py`
- Create: `release/features.json`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: clean `v0.7.0` commit, wheel/sdist/lock/pyz/Node/TUI/SBOM/image digest files.
- Produces: `release-manifest.json`, `SHA256SUMS`, `release-inputs.json`; fails on dirty tree, version/tag/commit mismatch or missing platform artifact.

- [ ] **Step 1: Write completeness/reproducibility RED tests**

```python
def test_manifest_builder_requires_every_tier1_component(self) -> None:
    with self.assertRaisesRegex(ReleaseBuildError, "missing tui artifact for macos-arm64"):
        build_manifest(self.inputs_without("tui", "macos", "arm64"))

def test_same_inputs_produce_byte_identical_manifest_and_checksums(self) -> None:
    first = build_release_outputs(self.inputs)
    second = build_release_outputs(self.inputs)
    self.assertEqual(first.manifest, second.manifest)
    self.assertEqual(first.checksums, second.checksums)
```

Add exact commit/tag/version/schema/features, unexpected artifact, duplicate filename, wrong wheel metadata, non-hash requirements, Node upstream hash mismatch, archive link scan, SBOM subject mismatch, image missing digest, dirty tree and checksum sort-order tests.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_release_manifest_build -v`

Expected: FAIL because release builders and feature registry are absent.

- [ ] **Step 3: Implement closed-world release assembly**

`release/features.json` lists only verified Phase 0～6 public features and maps each feature to one import/smoke assertion. `build_release_manifest.py manifest` requires `SOURCE_DATE_EPOCH` equal to the tag commit time, reads file metadata, computes hashes/sizes, sorts artifact records by `(kind, os, arch, filename)`, writes canonical JSON with UTF-8/newline, and includes `database_schema` plus `minimum_readable_schema`. Manifest requires universal wheel, sdist, requirements, installer pyz, four Node bundles, four TUI bundles, two SBOM formats and both GHCR digest files. Then `render_install_script.py` embeds the completed manifest/pyz hashes; only after that does `build_release_manifest.py checksums` generate sorted `SHA256SUMS` over manifest, shell and every Release artifact. `install.sh` and `SHA256SUMS` are attested Release trust-root files but are intentionally not manifest artifacts, avoiding a manifest↔shell hash cycle.

`build_sbom.py` consumes `uv export --locked --all-extras --no-dev --no-emit-project --format cyclonedx1.5`, wheel METADATA/RECORD, Node `release-component.json`, TUI `licenses.json` and image digest files. It emits canonical CycloneDX 1.5 JSON plus SPDX 2.3 JSON with package name/version/license/purl, artifact SHA-256 and dependency relationships; tests require every locked Python and production Node package exactly once. `verify_release_artifacts.py` independently reloads manifest/schema/SBOM and rehashes every local artifact.

- [ ] **Step 4: Build local candidate twice**

Run:

```bash
release_epoch=$(git show -s --format=%ct HEAD)
SOURCE_DATE_EPOCH="$release_epoch" uv build --out-dir /tmp/miniclaw-build-a
SOURCE_DATE_EPOCH="$release_epoch" uv build --out-dir /tmp/miniclaw-build-b
cmp /tmp/miniclaw-build-a/miniclaw_agent-0.7.0-py3-none-any.whl /tmp/miniclaw-build-b/miniclaw_agent-0.7.0-py3-none-any.whl
SOURCE_DATE_EPOCH="$release_epoch" uv run python scripts/build_installer_zipapp.py --output /tmp/miniclaw-installer-a.pyz
SOURCE_DATE_EPOCH="$release_epoch" uv run python scripts/build_installer_zipapp.py --output /tmp/miniclaw-installer-b.pyz
cmp /tmp/miniclaw-installer-a.pyz /tmp/miniclaw-installer-b.pyz
uv run python -m unittest tests.test_release_manifest_build -v
```

Expected: PASS; two clean Python wheels and pyz files are byte-identical; unit fixtures prove manifest/SBOM/checksum canonical output. Full native artifact assembly and independent verifier run in Task 17.

- [ ] **Step 5: Commit**

```bash
git add scripts/build_release_manifest.py scripts/build_sbom.py scripts/verify_release_artifacts.py tests/test_release_manifest_build.py release/features.json .gitignore
git commit -m "build(release): 生成 closed-world manifest 与 checksums"
```

---

### Task 16: Non-root GHCR Runtime image

**Files:**
- Create: `deploy/Dockerfile`
- Create: `deploy/sandbox.Dockerfile`
- Create: `deploy/entrypoint.sh`
- Create: `tests/test_deploy_image.py`

**Interfaces:**
- Consumes: verified wheel/requirements, managed TUI bundle and Phase 6 pinned Sandbox image contract.
- Produces: `ghcr.io/nedonion/miniclaw:0.7.0` and `ghcr.io/nedonion/miniclaw-sandbox:0.7.0`, immutable digests, non-root UID, CLI/install smoke and Phase 6 containment smoke.

- [ ] **Step 1: Write Dockerfile contract RED tests**

```python
def test_runtime_image_is_digest_pinned_and_ends_non_root(self) -> None:
    dockerfile = Path("deploy/Dockerfile").read_text(encoding="utf-8")
    self.assertRegex(dockerfile, r"^FROM .+@sha256:[0-9a-f]{64}$")
    self.assertIn("USER 65532:65532", dockerfile)
    self.assertNotIn("curl |", dockerfile)
    self.assertNotIn("latest", dockerfile)
```

Add no Secret ARG/ENV, no package resolver at final image, exact wheel/lock copy, owner-only state volume, read-only rootfs smoke, CLI version, Channel imports and Phase 6 Sandbox configuration tests.

- [ ] **Step 2: Run RED**

Run: `uv run python -m unittest tests.test_deploy_image -v`

Expected: FAIL because deployment files are absent.

- [ ] **Step 3: Implement multi-stage verified image**

Builder stage uses digest-pinned Python 3.12 slim, copies `requirements-all.lock` and verified wheel, installs with hashes into `/opt/miniclaw/venv`, and copies the native TUI bundle. Final stage copies only Runtime output, creates UID/GID 65532, uses `/data` owner-only state, sets `MINICLAW_HOME=/data` and managed Node/TUI paths, exposes no Docker socket, and runs stable entrypoint with exact argv. `deploy/sandbox.Dockerfile` builds the Phase 6 command image with the same UID 65532, no package manager/cache in the final layer, a read-only-root compatible `/tmp`, and no entrypoint that can reinterpret model argv. Do not embed user Secret or default Provider key.

- [ ] **Step 4: Build and run local container gate**

Run:

```bash
docker build --pull=false -f deploy/Dockerfile -t miniclaw:0.7.0 .
docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m --user 65532:65532 miniclaw:0.7.0 --version
docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m --user 65532:65532 miniclaw:0.7.0 install-smoke --json
```

Expected: version and smoke PASS as non-root. If Docker is unavailable, keep this gate PENDING with exact blocker; do not mark image verified.

- [ ] **Step 5: Commit**

```bash
git add deploy tests/test_deploy_image.py
git commit -m "build(container): 发布 non-root complete Runtime image"
```

---

### Task 17: CI, protected Release promotion and Tier 1 install matrix

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/release.yml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/install_matrix/run_install_matrix.py`
- Create: `tests/install_matrix/cases.json`
- Create: `tests/test_install_matrix_contract.py`
- Create: `.github/dependabot.yml`

**Interfaces:**
- Consumes: Tasks 1～16 builds/tests and self-hosted Tier 1 labels.
- Produces: required checks `offline`, `node-22`, `node-24`, `artifact-build`, `tier1-install`, `attestation`, `publish`; signed `release-evidence.json`; stable Release/PyPI/GHCR only after all required evidence.

- [ ] **Step 1: Add the dev-only YAML parser and write workflow policy RED tests**

```python
def test_release_workflow_has_no_floating_action_or_early_publish(self) -> None:
    workflow = yaml.load(
        Path(".github/workflows/release.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[^@]+@[0-9a-f]{40}$")
    self.assertIn("tier1-install", workflow["jobs"]["publish"]["needs"])
    self.assertEqual(workflow["jobs"]["publish"]["permissions"]["id-token"], "write")
```

Add `PyYAML>=6.0.2,<7` to the existing `dev` extra only. Parse with `yaml.BaseLoader` so the YAML 1.1 `on` token is not coerced to bool. Add event/tag, least permissions, immutable artifact handoff, environment protection, no long-lived PyPI/GHCR token, matrix exact coverage, action SHA allowlist and stable-public-URL post-publish smoke tests.

- [ ] **Step 2: Run RED**

Run: `uv lock && uv sync --extra dev && uv run python -m unittest tests.test_install_matrix_contract -v`

Expected: FAIL because workflows and matrix contract are absent.

- [ ] **Step 3: Implement pinned workflows and exact permissions**

Pin these reviewed commits:

```text
actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd
actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405
actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d
pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b
```

CI runs full unittest/Ruff/docs/build, Node 22.22.3 and 24.18.0 tests, installer adversarial tests and artifact reproducibility. Release triggers only `v*` protected tags, validates `v0.7.0 == __version__`, and builds native Node/TUI artifacts on Linux x64/arm64 and macOS Intel/ARM. Assembly order is fixed: Python/Node/TUI/images/pyz → `build_sbom.py` → `build_release_manifest.py manifest` → `render_install_script.py` → `build_release_manifest.py checksums` → `verify_release_artifacts.py`; then attest every subject, create a draft Release and run Tier 1 gates before publication.

`tier1-install` requires self-hosted labels for each supported OS/version/arch and runs all 15 design cases: blank user; no Python/Node/pnpm; no/refused sudo; dry-run; JSON no-onboard; TUI; Channel SDKs; version/Doctor; service lifecycle/reboot; idempotent reinstall; N-1 upgrade; bad hash/network/disk/health; rollback; data-preserving uninstall; Secret scan. Container jobs may supplement file/package tests but cannot satisfy systemd reboot or macOS LaunchAgent checks.

Publish job uses protected `pypi` environment OIDC, `packages: write` for GHCR, `id-token: write` only for attest/publish, promotes draft Release only after PyPI/GHCR digests match manifest, then runs public `latest/download/install.sh` smoke in fresh Tier 1 hosts. It writes canonical `release-evidence.json` with commit, workflow run, case counts, artifact hashes, image digests and PASS/PENDING status, attaches and attests it without any runner/user identifier. Any unavailable self-hosted combination blocks stable promotion. If the post-promotion public smoke fails, a guarded cleanup job immediately returns the GitHub Release to draft, records FAIL evidence and prevents `latest` from remaining on it; the already immutable PyPI version is not deleted or overwritten, and the fix must use `0.7.1`.

- [ ] **Step 4: Run local workflow contract and matrix dry-run**

Run:

```bash
uv run python -m unittest tests.test_install_matrix_contract -v
uv run python tests/install_matrix/run_install_matrix.py --list
uv run python tests/install_matrix/run_install_matrix.py --local-offline
```

Expected: exact cases/platform labels print; offline fake artifact matrix PASS; no workflow has a mutable action ref or publication path that bypasses Tier 1.

- [ ] **Step 5: Commit**

```bash
git add .github pyproject.toml uv.lock tests/install_matrix tests/test_install_matrix_contract.py
git commit -m "ci(release): 门禁 Tier 1 install 与 trusted publishing"
```

---

### Task 18: User documentation, release evidence and final same-commit gate

**Files:**
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Create: `docs/engineering/operations/20260809_install-release-operations.md`
- Modify: `docs/product/20260807_产品需求文档.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Create: `docs/evals/releases/v0.7.0-install.md`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Consumes: public draft/stable URLs, same-commit local/full matrix evidence, PyPI project and GHCR digest.
- Produces: one-line primary install docs, accurate source-install fallback, operations/runbook, v0.7.0 install release record and final completion verdict.

- [ ] **Step 1: Write documentation contract RED tests**

Extend `scripts/validate_docs.py` tests/assertions so README contains the exact one-line URL, `miniclaw-agent`, supported platform table, Node policy, service/update/uninstall commands, data-preserving default and source development path; reject old `name = miniclaw`, Node `>=22.19`, global pnpm requirement and claims that PENDING runners passed.

- [ ] **Step 2: Run RED**

Run: `uv run python scripts/validate_docs.py`

Expected: FAIL because public docs still describe source-first installation and lack release operations.

- [ ] **Step 3: Document only verified behavior**

README primary command is:

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://github.com/NEDONION/miniclaw/releases/latest/download/install.sh | bash
```

Document interactive and `--no-onboard --no-service --json`, exact supported/unsupported matrix, sudo plan, Secret file, rootless/non-root boundary, service commands, update/rollback conflict, uninstall/purge semantics, diagnostics and source development using uv/pnpm. Architecture shows bootstrap → pyz → manifest → staging → smoke → current → service; product marks Gap complete only after evidence. Operations runbook contains draft promotion, PyPI Trusted Publisher, GHCR digest, rollback-conflict recovery and revocation.

- [ ] **Step 4: Validate the documentation candidate**

Run:

```bash
uv run python scripts/validate_docs.py
git diff --check
```

Expected: PASS; `v0.7.0-install.md` is labelled `RELEASE CANDIDATE / PUBLIC GATES PENDING` and defines the exact external `release-evidence.json` required for final PASS.

- [ ] **Step 5: Commit documentation and candidate release record**

```bash
git add README.md README_EN.md docs scripts/validate_docs.py
git commit -m "docs(install): 发布 one-line operations 与 v0.7.0 gate"
```

- [ ] **Step 6: Run the complete same-commit local gate**

Run:

```bash
git status --short
uv sync --extra dev
uv run python -m unittest discover -s tests -v
uv run ruff check .
corepack pnpm --dir tui test
uv build
uv run python scripts/build_installer_zipapp.py --output dist/miniclaw-installer.pyz
uv run python scripts/validate_docs.py
uv run miniclaw eval run --suite channel --repeat 20 --json --root evals/scenarios
git diff --check
git diff --cached --check
```

Expected: all PASS on the committed clean candidate; Channel remains exactly 640/640; worktree/staged diff contains no Secret, private path, `.env`, Runtime, DB, log, cache or build artifact.

- [ ] **Step 7: Run protected live Release gates and attach final facts**

Push a candidate tag only after local gate. GitHub must show every Task 17 required check PASS, all Tier 1 fresh/service/reboot/upgrade/rollback/uninstall cases PASS, PyPI metadata/import/CLI PASS, both GHCR non-root/containment digest smokes PASS, attestation verification PASS, and public latest URL fresh install PASS. The workflow attaches and attests `release-evidence.json`; the tracked `v0.7.0-install.md` defines this artifact as the final non-self-referential evidence source. A missing runner/service reboot/account keeps the draft Release `PENDING` and blocks stable promotion.

- [ ] **Step 8: Final completion audit**

Confirm all design completion items against the tracked candidate record plus attested `release-evidence.json`: PyPI Trusted Publisher exists; GitHub assets are same commit; no machine needs preinstalled Python/Node/pnpm; pi-tui is checkout-independent; all Channel/Phase 6 imports and runtime smokes pass; sudo is explicit; Gateway non-root; service reboot passes; N-1 rollback passes; uninstall preserves data; SBOM/checksum/attestation verify; `origin/main` equals published commit. Only the Release verdict becomes COMPLETE after this check; do not create a post-tag evidence commit that would make `origin/main` differ from the published commit.

---

## Final Review Checklist

- [ ] Every section 1～25 of the approved design maps to at least one Task above.
- [ ] `rg -n 'T[B]D|T[O]DO|implement l[a]ter|fill in d[e]tails|similar t[o]|appropriate error handlin[g]' docs/superpowers/plans/2026-08-09-one-line-install-and-release.md` returns no matches.
- [ ] Interfaces are type-consistent: `PlatformKey`, `Artifact`, `InstallRequest`, `InstallLayout`, `InstallReceipt`, `ServiceSpec`, `Installer`, `DatabaseChangeGuard` are defined before downstream use.
- [ ] Phase 6 implementation files are consumed, not duplicated; the execution preflight updates exact intersections before Task 1.
- [ ] No step asks a unit test to use network, sudo, real Home, real service, Provider or Channel.
- [ ] No stable completion claim is possible without every Tier 1 VM/实体 runner and public post-publish smoke.
- [ ] Default install includes Core, all three Channel SDKs, managed Node/pi-tui and every feature listed in manifest; unavailable external Sandbox capability is reported explicitly rather than hidden.
- [ ] Default uninstall preserves config, Secret, DB, Memory, Skills, Workspace and logs; purge is the only destructive path and has double confirmation.
