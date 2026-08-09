# MiniClaw Desktop One-Click Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供一个可在 macOS Finder 双击或从终端运行的 `start-desktop.command`，自动准备项目依赖、安全初始化 MiniClaw 并启动 Desktop development build。

**Architecture:** 单个 zsh 入口只编排已有 `uv`、Corepack/pnpm、`miniclaw setup/init` 和 Electron dev 命令，不实现第二套安装器或 Secret 写入逻辑。Python `unittest` 在临时伪仓库中执行真实脚本，用窄 fake executable 隔离系统依赖并断言可观察的命令顺序、环境传递和退出码。

**Tech Stack:** zsh、Python 3.12+ 标准库 `unittest`、uv、Node.js `>=22.19.0`、Corepack/pnpm、Electron Vite。

## Global Constraints

- 设计规格以 `docs/superpowers/specs/2026-08-10-desktop-one-click-launch-design.md` 为准。
- 入口固定为仓库根目录 `start-desktop.command`，文件必须具有 owner executable bit。
- 系统级 `uv`、Node.js 和 Corepack 只检查，不自动安装，不使用 `sudo` 或 `curl | sh`。
- Secret 只由已有 `miniclaw setup` 收集；Shell 不读取、不打印、不写入 Secret 值。
- 所有依赖安装使用已提交 lockfile 和 `--frozen-lockfile`。
- 不新增第三方 Python、Node 或 Shell 依赖。
- 当前产品状态保持 `W0/W1 DEVELOPMENT BUILD`，不宣称 installer/signing 或 W2 已完成。

---

### Task 1: 已初始化环境的一键启动闭环

**Files:**
- Create: `tests/test_desktop_launcher.py`
- Create: `start-desktop.command`

**Interfaces:**
- Consumes: `MINICLAW_HOME`、可选 `MINICLAW_ENV_FILE`、`.venv/bin/python`、`.venv/bin/miniclaw`、`corepack pnpm`。
- Produces: `start-desktop.command`；成功路径依次执行 Core home 解析、`miniclaw init`、TUI build 和 Desktop dev，并返回 Desktop 退出码。

- [ ] **Step 1: 写已初始化状态的失败测试**

在 `tests/test_desktop_launcher.py` 建立临时伪仓库与 fake executable。测试执行脚本而不是检查源码文本：

```python
class DesktopLauncherTest(unittest.TestCase):
    """验证 Desktop 一键入口的可观察编排行为。"""

    def test_existing_state_builds_shared_client_and_starts_desktop_once(self) -> None:
        """已有依赖和配置时只做幂等初始化、TUI build 与一次 Desktop 启动。"""
        sandbox = self._sandbox(initialized=True, dependencies=True)

        result = sandbox.run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            sandbox.log_lines(),
            [
                f"miniclaw init --home {sandbox.state_home}",
                "corepack pnpm --dir tui build",
                (
                    "corepack pnpm --dir desktop dev "
                    f"home={sandbox.state_home} env={sandbox.state_home / 'secrets.env'}"
                ),
            ],
        )
```

`LauncherSandbox` 必须使用 `tempfile.TemporaryDirectory`，在临时根创建 `tui/`、`desktop/`、`.venv/bin/`、fake `uname`、`uv`、`node` 和 `corepack`。fake 命令只把调用写到 `MINICLAW_TEST_LOG`；`subprocess.run` 使用 pipe，因此失败路径不会等待交互输入。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_desktop_launcher.DesktopLauncherTest.test_existing_state_builds_shared_client_and_starts_desktop_once -v
```

Expected: FAIL，因为 `start-desktop.command` 尚不存在，成功退出码和命令日志均不满足断言。

- [ ] **Step 3: 实现最小已初始化启动路径**

创建 `start-desktop.command`：

```zsh
#!/bin/zsh
set -euo pipefail

readonly REPOSITORY_ROOT="${0:A:h}"
cd "$REPOSITORY_ROOT"

command -v uv >/dev/null 2>&1 || exit 2
command -v node >/dev/null 2>&1 || exit 2
command -v corepack >/dev/null 2>&1 || exit 2
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=19)?0:1)'

readonly STATE_HOME="$("$REPOSITORY_ROOT/.venv/bin/python" -c 'from miniclaw.paths import resolve_home; print(resolve_home())')"
export MINICLAW_HOME="$STATE_HOME"
export MINICLAW_PYTHON="$REPOSITORY_ROOT/.venv/bin/python"

"$REPOSITORY_ROOT/.venv/bin/miniclaw" init --home "$STATE_HOME"
if [[ -z "${MINICLAW_ENV_FILE:-}" && -f "$STATE_HOME/secrets.env" ]]; then
  export MINICLAW_ENV_FILE="$STATE_HOME/secrets.env"
fi
corepack pnpm --dir tui build
corepack pnpm --dir desktop dev
```

设置 executable bit：

```bash
chmod 755 start-desktop.command
```

- [ ] **Step 4: 运行测试并确认 GREEN**

Run:

```bash
.venv/bin/python -m unittest tests.test_desktop_launcher.DesktopLauncherTest.test_existing_state_builds_shared_client_and_starts_desktop_once -v
```

Expected: PASS；日志中没有 install/setup，Desktop dev 只出现一次。

- [ ] **Step 5: 提交已初始化启动闭环**

```bash
git add start-desktop.command tests/test_desktop_launcher.py
git commit -m "feat(desktop): 增加 one-click launch 基础入口"
```

---

### Task 2: 缺失依赖与首次 setup

**Files:**
- Modify: `tests/test_desktop_launcher.py`
- Modify: `start-desktop.command`

**Interfaces:**
- Consumes: Task 1 的 `LauncherSandbox.run()`、fake uv/corepack/miniclaw 和启动脚本。
- Produces: 缺失 `.venv` 时执行 `uv sync --extra dev`；缺失 Node 依赖目录时分别安装；无 `config.toml` 时调用 fresh-only setup。

- [ ] **Step 1: 写首次启动的失败测试**

```python
def test_first_run_prepares_missing_dependencies_then_uses_setup(self) -> None:
    """首次启动应补齐依赖并由现有 setup 安全收集 Secret。"""
    sandbox = self._sandbox(initialized=False, dependencies=False)

    result = sandbox.run()

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(
        sandbox.log_lines(),
        [
            "uv sync --extra dev",
            "corepack pnpm --dir tui install --frozen-lockfile",
            "corepack pnpm --dir desktop install --frozen-lockfile",
            f"miniclaw setup --home {sandbox.state_home}",
            "corepack pnpm --dir tui build",
            (
                "corepack pnpm --dir desktop dev "
                f"home={sandbox.state_home} env={sandbox.state_home / 'secrets.env'}"
            ),
        ],
    )
```

fake `uv` 在当前临时根创建 `.venv/bin/python` 与 `.venv/bin/miniclaw`；fake setup 创建 `config.toml` 和 mode `0600` 的 `secrets.env`，但日志只记录路径和 action，不记录 Secret 内容。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_desktop_launcher.DesktopLauncherTest.test_first_run_prepares_missing_dependencies_then_uses_setup -v
```

Expected: FAIL；Task 1 脚本在缺少 `.venv` 时提前失败，未执行依赖准备和 setup。

- [ ] **Step 3: 添加最小 bootstrap 分支**

在系统运行时检查之后、Core home 解析之前加入：

```zsh
if [[ ! -x "$REPOSITORY_ROOT/.venv/bin/python" || ! -x "$REPOSITORY_ROOT/.venv/bin/miniclaw" ]]; then
  uv sync --extra dev
fi
if [[ ! -d "$REPOSITORY_ROOT/tui/node_modules" ]]; then
  corepack pnpm --dir tui install --frozen-lockfile
fi
if [[ ! -d "$REPOSITORY_ROOT/desktop/node_modules" ]]; then
  corepack pnpm --dir desktop install --frozen-lockfile
fi
```

将固定 `init` 改为以下分支，并保持 Secret 文件选择位于该分支之后：

```zsh
if [[ -f "$STATE_HOME/config.toml" ]]; then
  "$REPOSITORY_ROOT/.venv/bin/miniclaw" init --home "$STATE_HOME"
else
  "$REPOSITORY_ROOT/.venv/bin/miniclaw" setup --home "$STATE_HOME"
fi
```

setup/init 必须位于依赖准备之后、TUI build 之前，保持测试规定的稳定顺序。

- [ ] **Step 4: 运行两个行为测试并确认 GREEN**

Run:

```bash
.venv/bin/python -m unittest tests.test_desktop_launcher -v
```

Expected: 两个测试 PASS；已有状态不重复安装，首次状态只调用 setup。

- [ ] **Step 5: 提交自动 bootstrap**

```bash
git add start-desktop.command tests/test_desktop_launcher.py
git commit -m "feat(desktop): 自动准备依赖与 fresh setup"
```

---

### Task 3: 失败提示、运行时边界和退出码

**Files:**
- Modify: `tests/test_desktop_launcher.py`
- Modify: `start-desktop.command`

**Interfaces:**
- Consumes: Task 2 的真实脚本与 fake command harness。
- Produces: macOS/uv/Node/Corepack 前置检查、失败短提示、非交互原始退出码和成功路径不泄露 Secret 的测试证据。

- [ ] **Step 1: 写运行时与中途失败测试**

```python
def test_missing_uv_stops_before_project_commands(self) -> None:
    """缺少 uv 时不得安装依赖、初始化状态或启动 Electron。"""
    sandbox = self._sandbox(initialized=False, dependencies=False, include_uv=False)
    result = sandbox.run()
    self.assertEqual(result.returncode, 2)
    self.assertIn("uv", result.stderr)
    self.assertEqual(sandbox.log_lines(), [])

def test_failed_build_preserves_exit_code_and_never_starts_desktop(self) -> None:
    """TUI build 失败后必须保留退出码并阻止 Desktop 启动。"""
    sandbox = self._sandbox(initialized=True, dependencies=True)
    result = sandbox.run(fail_match="pnpm --dir tui build", fail_code=17)
    self.assertEqual(result.returncode, 17)
    self.assertIn("启动失败", result.stderr)
    self.assertNotIn("pnpm --dir desktop dev", "\n".join(sandbox.log_lines()))

def test_explicit_secret_file_wins_without_exposing_its_contents(self) -> None:
    """显式 Secret 路径应透传，文件内容不得进入 stdout、stderr 或调用日志。"""
    sandbox = self._sandbox(initialized=True, dependencies=True)
    selected = sandbox.root / "private.env"
    selected.write_text("MINICLAW_MODEL_API_KEY=SECRET_SENTINEL\n", encoding="utf-8")
    selected.chmod(0o600)
    result = sandbox.run(extra_env={"MINICLAW_ENV_FILE": str(selected)})
    output = result.stdout + result.stderr + "\n".join(sandbox.log_lines())
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn(f"env={selected}", output)
    self.assertNotIn("SECRET_SENTINEL", output)
```

另加 Node 低于 `22.19.0` 和 fake `uname` 非 Darwin 的同类测试，只断言退出码、可操作关键词和零项目命令。

- [ ] **Step 2: 运行新增测试并确认 RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_desktop_launcher -v
```

Expected: FAIL；脚本当前只裸退出，缺少稳定错误文字和中途失败收口。

- [ ] **Step 3: 实现统一失败边界**

在脚本顶部加入：

```zsh
fail() {
  local message="$1"
  local status="${2:-2}"
  print -u2 -- "MiniClaw Desktop: $message"
  exit "$status"
}

on_error() {
  local status="$?"
  trap - ZERR
  set +e
  print -u2 -- "MiniClaw Desktop 启动失败（exit $status），请查看上方输出。"
  if [[ -t 0 ]]; then
    read -r "?按回车关闭窗口..." _
  fi
  exit "$status"
}
trap on_error ZERR
```

把前置条件改为带文案的 `fail`：

```zsh
[[ "$(uname -s)" == "Darwin" ]] || fail "当前一键入口仅支持 macOS。"
command -v uv >/dev/null 2>&1 || fail "缺少 uv，请先安装 uv。"
command -v node >/dev/null 2>&1 || fail "缺少 Node.js，需要 >=22.19.0。"
command -v corepack >/dev/null 2>&1 || fail "缺少 Corepack，请安装完整 Node.js >=22.19.0。"
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=19)?0:1)' \
  || fail "Node.js 版本过低，需要 >=22.19.0。"
```

不要捕获或重写 setup、uv、pnpm 的 stdout/stderr；统一边界只追加短错误并保留原始退出码。

- [ ] **Step 4: 运行 launcher 全部测试并确认 GREEN**

Run:

```bash
.venv/bin/python -m unittest tests.test_desktop_launcher -v
```

Expected: 全部 PASS；错误路径没有 Desktop dev，Secret sentinel 不出现在任何输出。

- [ ] **Step 5: 提交失败边界**

```bash
git add start-desktop.command tests/test_desktop_launcher.py
git commit -m "fix(desktop): 收紧 launcher runtime 与错误边界"
```

---

### Task 4: 文档、状态与完整门禁

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-10-desktop-one-click-launch-design.md`
- Test: `tests/test_desktop_launcher.py`

**Interfaces:**
- Consumes: Tasks 1～3 的可执行 `start-desktop.command` 和测试证据。
- Produces: 用户可复制的一条启动命令、保留的手工排障流程、准确的 implementation 状态和最终验证记录。

- [ ] **Step 1: 更新 README 的主启动路径**

将 Desktop W0/W1 开发版的首选命令改为：

```bash
./start-desktop.command
```

明确 Finder 可双击；保留 `uv sync`、TUI/Desktop install/build、`miniclaw setup/init` 和环境变量命令作为“脚本失败时的手工排障”，不删除安全说明或 development build 限制。

- [ ] **Step 2: 运行 focused 与产品门禁**

Run:

```bash
.venv/bin/python -m unittest tests.test_desktop_launcher -v
corepack pnpm --dir tui test
MINICLAW_PYTHON=.venv/bin/python corepack pnpm --dir desktop test
corepack pnpm --dir desktop typecheck
corepack pnpm --dir desktop build
.venv/bin/ruff check .
.venv/bin/python scripts/validate_docs.py
git diff --check
```

Expected: 所有命令退出 `0`；TUI、Desktop 和文档门禁无回归。

- [ ] **Step 3: 运行不含真实 Secret 的进程 smoke**

使用临时 Home、fake Provider key 和 fake command harness 执行成功路径，确认只出现一个 Desktop dev 调用；不启动真实模型请求、不读取个人状态或真实 Secret。

Run:

```bash
.venv/bin/python -m unittest tests.test_desktop_launcher.DesktopLauncherTest.test_existing_state_builds_shared_client_and_starts_desktop_once -v
```

Expected: PASS，输出不含任何 Secret 值。

- [ ] **Step 4: 更新设计状态**

只有 Steps 2～3 全绿后，才把设计文档首行状态从：

```text
DESIGN APPROVED / IMPLEMENTATION PENDING
```

改为：

```text
IMPLEMENTATION PASS / ELECTRON MANUAL PENDING
```

保留 installer/signing、真实模型 LIVE smoke 和 W2 不在本次范围的说明。

- [ ] **Step 5: 提交文档和最终证据**

```bash
git add README.md docs/superpowers/specs/2026-08-10-desktop-one-click-launch-design.md
git commit -m "docs(desktop): 发布 one-click launcher 使用说明"
```

## Plan Self-Review

- Spec coverage: 启动入口、系统运行时、依赖准备、setup/init、Secret 路径、失败退出、测试和文档状态分别由 Tasks 1～4 覆盖。
- Scope: 只交付 W0/W1 development launcher；未加入 installer、签名、更新器、Artifact、Sub-agent 或系统运行时安装器。
- Type/name consistency: `LauncherSandbox`、`MINICLAW_TEST_LOG`、`start-desktop.command`、`MINICLAW_HOME` 与 `MINICLAW_ENV_FILE` 在所有任务中保持一致。
- Mutation coverage: 错分 setup/init、跳过某个依赖、重复 Desktop dev、吞掉失败码、忽略显式 Secret 路径或打印 Secret 内容均会让至少一个测试失败。
