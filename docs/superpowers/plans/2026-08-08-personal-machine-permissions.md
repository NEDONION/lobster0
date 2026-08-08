# Personal Machine Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 MiniClaw 在显式 `personal` Profile 下安全读取普通个人文件、经单次审批写入配置目录，并稳定发现 NVM/uv/pnpm 中的本机 CLI。

**Architecture:** 新增强类型 `PermissionConfig`，由纯函数把 Profile 和显式 Roots 解析为 `ToolContext` 中不可由模型伪造的 Read/Write/Executable 边界。现有 `WorkspaceGuard` 保留入口但扩展为多根 Guard；新增 `ExecutableDiscovery` 生成稳定搜索路径，Policy 与 `RunCommandTool` 共用同一个解析结果和最小环境。旧配置缺少 `[permissions]` 时继续 workspace-only。

**Tech Stack:** Python 3.12、标准库 `pathlib/glob/shutil/asyncio`、TOML、SQLite Approval、TypeScript pi-tui、`unittest`、Ruff。

## Global Constraints

- 不使用 `root`、`sudo`、登录 Shell、Shell 字符串、管道、重定向或任意环境继承。
- 缺少 `[permissions]` 的旧配置必须继续 workspace-only，不得在升级时静默扩大权限。
- 敏感路径继续硬拒绝；普通文件写入只提供 Allow once。
- 不硬编码用户名、NVM 版本或开发机上的 `lark-cli` 绝对路径。
- 所有测试离线，使用临时 Home、临时 executable 和 fake Provider。
- 每个生产改动先有能因缺失行为而失败的测试，再写最小实现。
- 不修改或提交主工作树中的用户文档。

---

### Task 1: PermissionConfig 与 Profile Root 解析

**Files:**
- Modify: `src/miniclaw/config.py`
- Modify: `src/miniclaw/bootstrap.py`
- Test: `tests/test_config.py`
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Produces: `PermissionConfig(profile, read_roots, write_roots, executable_roots, discover_user_executables)`。
- Produces: `AppConfig.permissions: PermissionConfig`。
- Produces: `resolve_permission_roots(config, workspace, home, platform) -> ResolvedPermissionRoots`，包含 `owner_home` 与去重后的 `read_roots`、`write_roots`。

- [ ] **Step 1: 写缺失 `[permissions]` 保持 workspace-only 的失败测试**

在 `tests/test_config.py` 断言旧配置得到：

```python
self.assertEqual(config.permissions.profile, "workspace")
self.assertEqual(config.permissions.read_roots, ())
self.assertEqual(config.permissions.write_roots, ())
self.assertFalse(config.permissions.discover_user_executables)
```

再写 `personal` 配置测试，使用临时绝对目录并断言五个字段完整保留；未知字段、相对路径和重复 Roots 必须抛 `ConfigError`。

- [ ] **Step 2: 运行 RED**

```bash
uv run python -m unittest tests.test_config.ConfigTest -v
```

预期：因 `AppConfig` 尚无 `permissions` 或顶层拒绝该 section 而失败。

- [ ] **Step 3: 最小实现强类型配置**

在 `config.py` 增加：

```python
@dataclass(frozen=True, slots=True)
class PermissionConfig:
    """保存 Owner 明确选择的本机能力 Profile 与附加 Roots。"""

    profile: str = "workspace"
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    executable_roots: tuple[Path, ...] = ()
    discover_user_executables: bool = False
```

把 `permissions` 加入 `_TOP_LEVEL_KEYS`，严格解析五个字段；新增 `_existing_root_list`，拒绝重复、相对、缺失、非目录和 symlink root。`personal` Profile 才允许 `discover_user_executables=true`。

- [ ] **Step 4: 写并通过 Root 解析测试**

增加纯数据类型：

```python
@dataclass(frozen=True, slots=True)
class ResolvedPermissionRoots:
    owner_home: Path | None
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
```

测试 macOS `personal` 在目录真实存在时按稳定顺序包含 Home、`/Applications`、`/opt/homebrew`、`/usr/local`，写根包含存在的 Desktop/Documents/Downloads/PycharmProjects/WebstormProjects；非 macOS 只使用显式 Roots；Workspace 不重复进入附加 Roots。

- [ ] **Step 5: 更新新配置模板并验证不覆盖旧配置**

`_render_default_config()` 新增：

```toml
[permissions]
profile = "personal"
read_roots = []
write_roots = []
executable_roots = []
discover_user_executables = true
```

Bootstrap 测试断言新实例生成 personal，重复 init 不覆盖已有 workspace Profile。

- [ ] **Step 6: 运行 GREEN 并提交**

```bash
uv run python -m unittest tests.test_config tests.test_bootstrap -v
uv run ruff check src/miniclaw/config.py src/miniclaw/bootstrap.py tests/test_config.py tests/test_bootstrap.py
git add src/miniclaw/config.py src/miniclaw/bootstrap.py tests/test_config.py tests/test_bootstrap.py
git commit -m "feat(permission): 增加 Personal Profile 强类型配置"
```

---

### Task 2: 多根文件读取与受控外部写入

**Files:**
- Modify: `src/miniclaw/tools/base.py`
- Modify: `src/miniclaw/policy/workspace.py`
- Modify: `src/miniclaw/tools/filesystem.py`
- Modify: `src/miniclaw/tools/search.py`
- Modify: `src/miniclaw/agent/turn.py`
- Test: `tests/test_workspace_policy.py`
- Test: `tests/test_file_tools.py`
- Test: `tests/test_search_tools.py`
- Test: `tests/test_tool_executor.py`

**Interfaces:**
- Modifies: `WorkspaceConfig` 新增仅供 Runtime 注入的 `write_roots: tuple[Path, ...] = ()` 与 `owner_home: Path | None = None`。
- Modifies: `ToolContext` 新增 `write_roots: tuple[Path, ...] = ()` 与 `owner_home: Path | None = None`，现有位置参数测试仍可工作。
- Produces: `WorkspaceGuard.read_root(context, path) -> tuple[str, Path]`。
- Preserves: `resolve_read`、`resolve_write`、`display` 公共入口。

- [ ] **Step 1: 写 Personal Home 读取和外部拒绝 RED**

临时创建 `home/Documents/note.md`、Workspace 与一个真正外部目录。断言 Home 被列入 `read_only_roots` 时可以读取，外部路径返回 `path_outside_roots`，`~/.ssh/id_ed25519`、`.env.local`、Keychains、Chrome `Cookies` 仍为 `sensitive_path`。

- [ ] **Step 2: 写 Workspace 外写入 RED**

构造 `ToolContext(..., read_only_roots=(home,), write_roots=(home / "Documents",))`：

```python
target = home / "Documents" / "approved.md"
self.assertEqual(guard.resolve_write(context, str(target)), target.resolve())
with self.assertRaisesRegex(WorkspaceAccessError, "configured roots"):
    guard.resolve_write(context, str(home / "Library" / "outside.md"))
```

同时断言 symlink、敏感目标和不存在父目录继续拒绝。

- [ ] **Step 3: 运行 RED**

```bash
uv run python -m unittest tests.test_workspace_policy tests.test_file_tools -v
```

预期：`ToolContext` 不接受 `write_roots`，Workspace 外写入仍返回旧 `workspace_escape`。

- [ ] **Step 4: 最小实现多根 Guard**

`resolve_read` 使用 `(workspace, *read_only_roots)`；不命中时返回 `path_outside_roots`。`resolve_write` 使用
`(workspace, *write_roots)`，逐根执行 lexical containment、symlink 拒绝和真实路径复验。`read_only_roots` 不再天然意味着写禁止；只有未出现在 `write_roots` 的目录才不可写。

`display()` 通过 `read_root()` 生成 `workspace/...`、`home/...`、`applications/...` 或 `root-N/...`；`owner_home` 只来自不可伪造的 Context，绝不输出 Home 绝对前缀。文件与搜索 Tool 删除自己的 `next(root for root...)` 重复逻辑。

- [ ] **Step 5: 扩展敏感路径回归**

加入明确路径模式：Keychains、Chrome/Chromium/Firefox/Safari 的 Cookies/Login Data、1Password、已知即时通讯数据、MiniClaw state、socket 和非普通文件。测试逻辑路径与 symlink 真实目标两条路径都拒绝。

- [ ] **Step 6: 接入 TurnService 与 Approval**

Runtime 用 `dataclasses.replace(config.workspace, owner_home=..., read_only_roots=..., write_roots=...)` 构造 effective `WorkspaceConfig`；`TurnService` 再向 `ToolContext` 注入 `owner_home/read_roots/write_roots`。测试真实 `write_file` 请求在外部 Write Root 进入 `waiting_approval`，批准前文件不存在，Allow once 后原子创建；敏感外部路径不得生成 Approval。

- [ ] **Step 7: 运行 GREEN 并提交**

```bash
uv run python -m unittest tests.test_workspace_policy tests.test_file_tools tests.test_search_tools tests.test_tool_executor tests.test_turn -v
uv run ruff check src/miniclaw/tools/base.py src/miniclaw/policy/workspace.py src/miniclaw/tools/filesystem.py src/miniclaw/tools/search.py src/miniclaw/agent/turn.py tests/test_workspace_policy.py tests/test_file_tools.py tests/test_search_tools.py tests/test_tool_executor.py
git add src/miniclaw/tools/base.py src/miniclaw/policy/workspace.py src/miniclaw/tools/filesystem.py src/miniclaw/tools/search.py src/miniclaw/agent/turn.py tests/test_workspace_policy.py tests/test_file_tools.py tests/test_search_tools.py tests/test_tool_executor.py tests/test_turn.py
git commit -m "feat(files): 支持 Personal 多根读取与受控外部写入"
```

---

### Task 3: 可信 ExecutableDiscovery

**Files:**
- Create: `src/miniclaw/policy/executables.py`
- Modify: `src/miniclaw/policy/command.py`
- Modify: `src/miniclaw/policy/engine.py`
- Test: `tests/test_executable_discovery.py`
- Test: `tests/test_command_policy.py`

**Interfaces:**
- Produces: `ExecutableEnvironment(search_roots, path_value, home)`。
- Produces: `discover_executables(profile, home, explicit_roots, discover_user, platform) -> ExecutableEnvironment`。
- Modifies: `normalize_command(program, args, workspace, *, executable_path=SAFE_EXECUTABLE_PATH)`。

- [ ] **Step 1: 写 NVM/uv/pnpm 发现 RED**

在临时 Home 建立：

```text
.config/nvm/versions/node/v20.19.0/bin/lark-cli
.nvm/versions/node/v22.19.0/bin/node
.local/share/uv/tools/demo/bin/demo
.local/share/pnpm/pnpm
.local/bin/local-tool
```

只给真实目录/文件 executable bit。断言 stable root order、去重、不包含 symlink root、相对显式 root 被拒绝、发现过程不执行 `.zshrc`。

- [ ] **Step 2: 运行 RED**

```bash
uv run python -m unittest tests.test_executable_discovery -v
```

预期：模块不存在。

- [ ] **Step 3: 最小实现发现器**

使用 `Path.glob("versions/node/*/bin")` 和 `Path.glob("tools/*/bin")`，只接受存在的真实目录；系统根优先、显式根其次、用户发现根最后；`path_value` 使用 `os.pathsep.join`。workspace Profile 的 `home=None`，personal Profile 保存真实 Home 供子进程最小环境使用。

- [ ] **Step 4: 让 Command Policy 共用发现结果**

`normalize_command` 的裸程序使用 `shutil.which(program, path=executable_path)`；绝对程序规则保持不变。`PolicyEngine` 保存
`executable_path` 并用于每次归一化。测试 `lark-cli` 能从临时 NVM root 解析，额外 argv 仍重新审批，Shell/删除/提权红线不回归。

- [ ] **Step 5: 运行 GREEN 并提交**

```bash
uv run python -m unittest tests.test_executable_discovery tests.test_command_policy -v
uv run ruff check src/miniclaw/policy/executables.py src/miniclaw/policy/command.py src/miniclaw/policy/engine.py tests/test_executable_discovery.py tests/test_command_policy.py
git add src/miniclaw/policy/executables.py src/miniclaw/policy/command.py src/miniclaw/policy/engine.py tests/test_executable_discovery.py tests/test_command_policy.py
git commit -m "feat(command): 确定性发现 NVM 与用户 CLI"
```

---

### Task 4: 最小子进程环境、Runtime 与 Doctor

**Files:**
- Modify: `src/miniclaw/tools/command.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `src/miniclaw/doctor.py`
- Test: `tests/test_run_command.py`
- Test: `tests/test_runtime.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Modifies: `RunCommandTool(..., executable_path: str, owner_home: Path | None)`。
- Produces: Doctor checks `personal_permissions`、`executables`。

- [ ] **Step 1: 写最小环境 RED**

真实临时 executable 输出选择后的环境键。personal 模式断言只有 `PATH/HOME/LANG/LC_ALL` 和两个
`LARKSUITE_CLI_NO_*_NOTIFIER`；输入环境中的 `MINICLAW_MODEL_API_KEY`、`HTTP_PROXY`、`PYTHONPATH`、`COOKIE` 不得出现。workspace 模式不得包含 HOME。

- [ ] **Step 2: 写真实 wrapper 启动 RED**

临时 NVM `bin/lark-cli` 使用 `#!/usr/bin/env node`，同目录 fake `node` 转发到一个可执行 fixture；通过 discovered PATH 执行 `lark-cli --version` 并断言 stdout。测试不依赖开发机 Node/npm。

- [ ] **Step 3: 运行 RED**

```bash
uv run python -m unittest tests.test_run_command -v
```

预期：Tool 仍固定 `SAFE_EXECUTABLE_PATH`，不接受发现结果或 HOME。

- [ ] **Step 4: 实现并接入 Runtime**

`create_runtime()` 用 `resolve_permission_roots` 和 `discover_executables` 构造唯一边界：

```python
permissions = resolve_permission_roots(config, home=Path.home(), platform=sys.platform)
executables = discover_executables(...)
```

同一 `executables.path_value` 同时传给配置 command rule、`PolicyEngine`、`RunCommandTool`；避免 Policy 允许的程序在 Tool 中再次解析成另一位置。TurnService 获得解析后的 WorkspaceConfig/roots。

- [ ] **Step 5: Doctor 安全展示**

Doctor 固定增加两项，消息只包含 Profile、root 数量、发现的 basename 数量和 `lark-cli available/unavailable`；测试断言输出不含临时 Home、完整 PATH、Token 或认证内容。Doctor 可以执行本地 `--version` 时仍不得联网；首版仅检查 executable 存在和可执行位，不运行它。

- [ ] **Step 6: 运行 GREEN 并提交**

```bash
uv run python -m unittest tests.test_run_command tests.test_runtime tests.test_doctor tests.test_cli -v
uv run ruff check src/miniclaw/tools/command.py src/miniclaw/runtime.py src/miniclaw/doctor.py tests/test_run_command.py tests/test_runtime.py tests/test_doctor.py
git add src/miniclaw/tools/command.py src/miniclaw/runtime.py src/miniclaw/doctor.py tests/test_run_command.py tests/test_runtime.py tests/test_doctor.py tests/test_cli.py
git commit -m "feat(runtime): 接线 Personal CLI 环境与 Doctor"
```

---

### Task 5: TUI 审批说明与版本化回归场景

**Files:**
- Modify: `tui/src/components/approval.ts`
- Modify: `tui/test/approval.test.ts`
- Modify: `evals/scenarios/claw_like_v1.jsonl`
- Modify: `tests/test_eval_cases.py`
- Modify: `src/miniclaw/agent/context.py`
- Test: `tests/test_context.py`

**Interfaces:**
- Preserves: NDJSON 协议与 Approval decisions。
- Adds: run_command 审批中的中文/英文 OS 权限提示。

- [ ] **Step 1: 写 TUI 提示 RED**

当 `toolName === "run_command"` 时，Approval 文本必须包含：

```text
该程序将以当前用户身份运行，并可能读取当前用户可访问的文件。
```

英文模式显示对应英文。文件 Approval 不显示该提示。虚拟终端宽度断言仍不溢出。

- [ ] **Step 2: 运行 RED 并实现最小渲染**

```bash
corepack pnpm --dir tui test
```

在 `ApprovalDialog.render()` 的 summary 与 choices 之间增加一条可换行的 muted warning，不改变按键和 grant mode。

- [ ] **Step 3: 增加模型防绕过规则测试**

`ContextBuilder` 的 Tool 使用规则明确：敏感路径硬拒绝后不得改用 `cat/python/run_command` 绕过；普通 Workspace 外读取应使用文件 Tool；本机 CLI 应直接请求 `run_command` 审批，不要用全盘 `find` 猜测。测试断言最终 system message 包含行为语义，不绑定整段文案。

- [ ] **Step 4: 增加四个离线场景**

加入 `FILES-PERSONAL-READ-001`、`FILES-PERSONAL-WRITE-APPROVAL-001`、`CLI-DISCOVERY-LARK-001`、`CLI-SENSITIVE-DENY-001`。Fake fixtures 只使用临时 root；断言 Tool 名、waiting approval、稳定错误码和零安全违规。

- [ ] **Step 5: 运行 GREEN 并提交**

```bash
corepack pnpm --dir tui test
uv run python -m unittest tests.test_context tests.test_eval_cases -v
uv run miniclaw eval validate --root evals/scenarios
uv run miniclaw eval run --suite offline --root evals/scenarios
git add tui/src/components/approval.ts tui/test/approval.test.ts src/miniclaw/agent/context.py tests/test_context.py evals/scenarios/claw_like_v1.jsonl tests/test_eval_cases.py
git commit -m "feat(tui): 解释命令权限并增加 Personal 回归场景"
```

---

### Task 6: 工程文档、架构与进度同步

**Files:**
- Create: `docs/engineering/phase-2/20260808_personal-machine-permissions.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/product/20260807_产品需求文档.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Documents only verified behavior and exact commands from Tasks 1-5.

- [ ] **Step 1: 写 Phase 2.3B 工程文档**

文档包含大白话说明、Profile 表、Read/Write/Executable Roots、敏感路径、Approval、NVM `lark-cli` 发现、Doctor、错误码、Mermaid 数据流、测试证据和已知边界。真实本机 `auth status` 明确标记为 P2.3C 未验证。

- [ ] **Step 2: 同步产品与使用文档**

README/PRD/架构把“文件只能 Workspace”改为 Profile 语义；本地指南给出 owner-only `config.toml` 示例和迁移步骤。进度页显示 Phase 2.3B 当前 commit、测试计数和下一步 P2.3C live `lark-cli`。

- [ ] **Step 3: 文档事实扫描并提交**

```bash
rg -n "只能访问 Workspace|workspace-only|P2.3B 尚未|391/391" README.md docs
sed -n '1,260p' docs/engineering/phase-2/20260808_personal-machine-permissions.md
git diff --check
git add README.md docs
git commit -m "docs(phase2.3): 记录 Personal 权限工程边界与进度"
```

---

### Task 7: 全量验证与本机只读 Smoke

**Files:**
- Modify only if a failing regression proves an implementation defect.

**Interfaces:**
- Produces release evidence for the feature branch; does not modify `~/.miniclaw` or execute authenticated Lark operations.

- [ ] **Step 1: 完整离线门禁**

```bash
uv run python -m unittest discover -s tests -v
corepack pnpm --dir tui test
uv run miniclaw eval run --suite offline --root evals/scenarios
uv run miniclaw eval run --suite channel --root evals/scenarios
uv run ruff check .
git diff --check
```

- [ ] **Step 2: 本机只读发现 Smoke**

通过纯发现 API（不读取 auth、不联网）断言当前开发机能解析 `lark-cli`，并直接调用解析出的 native/wrapper 执行
`--version`。输出只记录 `available=true`、basename 和版本，不记录 Home 绝对路径。

- [ ] **Step 3: 审查工作树与安全差异**

```bash
git status --short --branch
git diff --check main...HEAD
git log --oneline --decorate main..HEAD
```

确认没有 `.env`、数据库、日志、个人路径 fixture、Node modules 或真实飞书输出。

- [ ] **Step 4: 如验证修复产生新改动则提交**

```bash
git add -u
git commit -m "fix(permission): 修正全量门禁发现的问题"
```

没有新改动时不创建空 commit。
