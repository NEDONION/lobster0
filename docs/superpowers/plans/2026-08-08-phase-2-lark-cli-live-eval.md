# Lobster0 Phase 2 lark-cli and Live Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 P2.3B/P2.5：让现有 `run_command` 安全发现并执行 NVM 中的 `lark-cli`，增加真实 Provider live eval，并在门禁通过后交付 Phase 3 工程落地文档。

**Architecture:** 保留唯一执行链 `AgentRunner -> ToolExecutor -> PolicyEngine -> RunCommandTool`，在 Command Policy 中增加一个受固定根约束的 lark-cli resolver，并把三条只读 argv 合并为内置 exact rules。live eval 复用生产 Runtime 和临时 SQLite，只输出脱敏结构判定；不会自动审批或执行飞书写操作。

**Tech Stack:** Python 3.12+、标准库 `pathlib/asyncio/unittest`、现有 httpx Provider、SQLite、JSONL eval、Textual；不新增依赖、飞书 Tool、Channel SDK 或 Shell。

## Global Constraints

- 现有 8 个 Tool 数量不变，不新增 `lark_cli` Tool。
- 只接受 `program + args[]`，禁止 Shell、PTY、stdin、重定向与后台任务。
- 只读自动规则必须匹配解析后的 executable 与完整 argv。
- `auth login/logout`、`update`、`config init` 硬拒绝，不能经 Approval 放行。
- 其他 lark-cli 动作继续使用参数绑定、单次消费的 SQLite Approval。
- 不继承 API Key、Token、Secret、代理或完整用户 PATH。
- live eval 必须显式确认，不进入默认 offline gate，不输出 Prompt/reasoning/stdout/身份。
- live smoke 不打开 Lark、不发消息、不修改飞书数据。
- 每个生产行为先看到对应 unittest RED，再写最小 GREEN。
- 保护未跟踪的 `docs/lobster0-tui-stability-and-desktop-design.md` 与 `docs/superpowers/plans/2026-08-08-tui-polish-telemetry-approval.md`。
- 用户已明确授权在 `main` 上继续并最终 push；提交标题中英文各约一半。

---

## File Map

- `src/lobster0/policy/command.py`：可信 lark-cli/NVM 发现、目标识别、Node runtime bin 和硬禁止。
- `src/lobster0/tools/command.py`：为可信 lark-cli 构造最小 PATH，并提供 Provider 可见用法。
- `src/lobster0/runtime.py`：把三条 lark-cli 内置只读规则合并进现有 PolicyEngine。
- `src/lobster0/doctor.py`：增加不读取认证信息的 `lark_cli` PASS/WARN 检查。
- `src/lobster0/evals/cases.py`：解析独立 `live.expected`，不改变 offline responses。
- `src/lobster0/evals/runner.py`：运行真实 Provider、临时 State、结构 verifier 与 samples。
- `src/lobster0/cli.py`：增加显式 `--suite live --confirm-live --samples`。
- `evals/scenarios/phase2.v1.jsonl`：更新打开应用 live expectation，增加只读 lark status case。
- `tests/test_command_policy.py`：可信根、symlink、精确规则与硬拒绝。
- `tests/test_run_command.py`：可信 Node PATH 与环境秘密隔离。
- `tests/test_doctor.py`：lark-cli 可用/缺失且不触碰 auth。
- `tests/test_runtime.py`：生产 Runtime 内置规则装配。
- `tests/test_eval_cases.py`：live schema 与仓库场景契约。
- `tests/test_eval_runner.py`：live runner 隔离、结构判定和脱敏输出模型。
- `tests/test_cli_eval.py`：live opt-in、samples、错误码和输出边界。
- `tests/test_tool_contract.py`：Provider 可见 lark-cli 调用契约。
- `docs/engineering/phase-2/lark-cli-and-live-eval.md`：P2.3B/P2.5 已实现事实。
- `docs/engineering/phase-3/phase-3-engineering-plan.md`：Phase 3 Memory/Skills/Compaction 工程落地文档。
- `README.md`、架构、运行指南、工程索引、eval 文档和发布记录：同步真实门禁。
- `docs/progress/index.html`：仓库进度页。
- `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/lobster0-progress.html`：独立可点击进度页，不进入 Git。

---

### Task 1: Trusted lark-cli Resolver and Runtime PATH

**Files:**
- Modify: `src/lobster0/policy/command.py`
- Modify: `src/lobster0/tools/command.py`
- Modify: `tests/test_command_policy.py`
- Modify: `tests/test_run_command.py`

**Interfaces:**
- Produces: `discover_lark_cli(home: Path | None = None, executable_path: str | None = None) -> Path | None`，返回可信 entrypoint。
- Produces: `is_trusted_lark_cli(resolved_program: str) -> bool`。
- Produces: `lark_cli_runtime_bin(resolved_program: str) -> Path | None`。
- Consumes: 现有 `normalize_command(program, args, workspace) -> NormalizedCommand` 与 `SAFE_EXECUTABLE_PATH`。

- [ ] **Step 1: 写 resolver RED 测试**

在临时 home 创建真实结构：

```python
prefix = home / ".config/nvm/versions/node/v20.19.0"
target = prefix / "lib/node_modules/@larksuite/cli/scripts/run.js"
target.parent.mkdir(parents=True)
target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
target.chmod(0o700)
(prefix / "bin").mkdir()
(prefix / "bin/node").write_text("node", encoding="utf-8")
(prefix / "bin/node").chmod(0o700)
(prefix / "bin/lark-cli").symlink_to("../lib/node_modules/@larksuite/cli/scripts/run.js")
```

断言：可信 NVM 命中、多版本数字排序、可信 PATH 优先；根外 symlink、断链、loop、目录、不可执行 target 和缺少同版本 `bin/node` 都返回 `None`。每个断言直接调用 `discover_lark_cli()`，不 mock 返回值。

- [ ] **Step 2: 运行 RED**

Run:

```bash
uv run python -m unittest tests.test_command_policy -v
```

Expected: `ImportError` 或缺少 `discover_lark_cli` 行为的断言失败。

- [ ] **Step 3: 用 pathlib 实现最小可信发现**

实现固定搜索顺序，不新增配置层：

```python
def discover_lark_cli(
    home: Path | None = None,
    executable_path: str | None = None,
) -> Path | None:
    """从系统 PATH 或固定 NVM 根发现可信 lark-cli entrypoint。"""
```

使用 `Path.resolve(strict=True)`、`is_file()`、`os.access(..., os.X_OK)` 和 `Path.is_relative_to()`；NVM 版本只解析 `v<major>.<minor>.<patch>` 三个整数字段。任何 `OSError`/`RuntimeError` 返回 `None`，错误不包含候选路径。

- [ ] **Step 4: 写 normalize 与硬禁止 RED**

使用同一临时 NVM fixture 断言：

```python
normalized = normalize_command("lark-cli", ("auth", "status", "--json"), workspace)
self.assertEqual(normalized.resolved_program, str(target.resolve()))
```

并断言以下 argv 抛 `CommandPolicyError(code="command_forbidden")`：

```python
("auth", "login", "--scope", "im:message")
("auth", "logout", "--json")
("update",)
("config", "init", "--new")
```

即使模型直接传入可信真实 `run.js` 绝对路径也必须硬拒绝这些 argv。

- [ ] **Step 5: 运行 RED，再接入 normalize**

Run: `uv run python -m unittest tests.test_command_policy -v`

Expected before implementation: `lark-cli` 仍为 `command_not_found`，认证变更没有 lark 专用 deny。

实现：只有 supplied basename 为 `lark-cli` 时调用 resolver；其他程序保留原解析。用解析后 target 形状识别可信 lark-cli，再运行 `_is_forbidden_lark_cli(args)`。

- [ ] **Step 6: 写最小环境 RED**

在 `tests/test_run_command.py` 用临时可信 NVM fixture 和一个检查 PATH/秘密的 `run.js`，断言：

- 执行成功；
- PATH 第一项是同 Node version 的 `bin`；
- PATH 其余部分等于 `SAFE_EXECUTABLE_PATH`；
- `LOBSTER0_TEST_SECRET`、`HTTPS_PROXY`、`LOBSTER0_MODEL_API_KEY` 均不存在；
- 普通 Python command 的 PATH 不包含 NVM bin。

- [ ] **Step 7: 运行 RED，再实现 `_safe_environment(resolved_program)`**

Run: `uv run python -m unittest tests.test_run_command -v`

Expected before implementation: lark shebang 找不到同版本 Node 或 PATH 断言失败。

`RunCommandTool.execute()` 把 `normalized.resolved_program` 传给 `_safe_environment()`；仅当
`lark_cli_runtime_bin()` 返回可信目录时前置该目录。Tool Result 的 `program` 对可信 target 显示
`lark-cli`，不暴露 `run.js` 或完整 NVM 路径。

- [ ] **Step 8: GREEN、Ruff 与提交**

```bash
uv run python -m unittest tests.test_command_policy tests.test_run_command -v
uv run ruff check src/lobster0/policy/command.py src/lobster0/tools/command.py tests/test_command_policy.py tests/test_run_command.py
git add src/lobster0/policy/command.py src/lobster0/tools/command.py tests/test_command_policy.py tests/test_run_command.py
git commit -m "feat(command): 支持 trusted NVM lark-cli 执行"
```

---

### Task 2: Exact Read-only Rules, Doctor, and Provider Contract

**Files:**
- Modify: `src/lobster0/policy/command.py`
- Modify: `src/lobster0/runtime.py`
- Modify: `src/lobster0/doctor.py`
- Modify: `src/lobster0/tools/command.py`
- Modify: `tests/test_command_policy.py`
- Modify: `tests/test_doctor.py`
- Modify: `tests/test_tool_contract.py`

**Interfaces:**
- Produces: `lark_cli_read_only_rules(workspace: Path) -> tuple[NormalizedCommand, ...]`。
- Produces: Doctor `CheckResult(name="lark_cli", status=PASS|WARN, ...)`。
- Consumes: Task 1 resolver 与现有 `PolicyEngine(command_rules=...)`。

- [ ] **Step 1: 写 exact rules 与 Policy RED**

在可信 fixture 中生成 rules，断言恰好三条：

```python
(
    ("--version",),
    ("--help",),
    ("auth", "status", "--json"),
)
```

用真实 `PolicyEngine` 断言三条为 `ALLOW`；额外 `--verify`、换序、`whoami` 和任意业务动作是
`REQUIRE_APPROVAL`；login/logout/update/config init 是 `DENY` 且不依赖 rules。

- [ ] **Step 2: 运行 RED，再实现 rules helper**

Run: `uv run python -m unittest tests.test_command_policy -v`

Expected: 缺少 helper 或 Policy 不会自动允许三条规则。

helper 在 resolver 缺失时返回空 tuple；存在时通过现有 `normalize_command()` 生成真实 executable 绑定规则。

- [ ] **Step 3: 把已测试 helper 合并进 Runtime**

在 `create_runtime()` 中按稳定顺序合并下面这一行装配代码；helper 的允许/拒绝行为已经由 Step 1 的真实
Policy 测试保护，Task 5 再通过生产 Runtime 做真实集成 smoke：

```python
command_rules = tuple(dict.fromkeys((
    *lark_cli_read_only_rules(config.workspace.path),
    *configured_command_rules,
    *rules.command_rules(owner.id),
)))
```

不得覆盖用户已有 exact rules。运行 `tests.test_runtime`，确认 8 Tool 装配和 Provider 生命周期保持原样。

- [ ] **Step 4: 写 Doctor RED**

测试 `_check_lark_cli()`：可信 fixture 为 PASS、缺失为 WARN；消息不含 home/path/version；patch
`subprocess.run` 为抛错并断言结果不受影响，证明 Doctor 没有执行 auth/version 子进程。

- [ ] **Step 5: 运行 RED，再加入第八项 Doctor check**

Run: `uv run python -m unittest tests.test_doctor -v`

`run_local_checks()` 固定返回八项；`lark_cli` 放在 `tools` 后、`database` 前。WARN 不改变 doctor 退出码，FAIL
规则保持原行为。

- [ ] **Step 6: 写 Provider Contract RED，再改 description**

在 `tests/test_tool_contract.py` 读取真实 Registry schema，断言 description 告知：

- `lark-cli auth status --json`；
- direct argv、no shell；
- 其他动作 request approval；
- auth login 在普通 terminal 完成。

先运行并确认 RED，再只修改 `RunCommandTool.definition.description`，不增加 Prompt 路由代码。

- [ ] **Step 7: GREEN、Ruff 与提交**

```bash
uv run python -m unittest tests.test_command_policy tests.test_runtime tests.test_doctor tests.test_tool_contract -v
uv run ruff check src/lobster0/policy/command.py src/lobster0/runtime.py src/lobster0/doctor.py src/lobster0/tools/command.py tests/test_command_policy.py tests/test_doctor.py tests/test_tool_contract.py
git add src/lobster0/policy/command.py src/lobster0/runtime.py src/lobster0/doctor.py src/lobster0/tools/command.py tests/test_command_policy.py tests/test_doctor.py tests/test_tool_contract.py
git commit -m "feat(policy): 增加 lark-cli exact rules 与 Doctor"
```

---

### Task 3: Live Case Schema and Isolated Runner

**Files:**
- Modify: `src/lobster0/evals/cases.py`
- Modify: `src/lobster0/evals/runner.py`
- Modify: `tests/test_eval_cases.py`
- Modify: `tests/test_eval_runner.py`

**Interfaces:**
- Produces: `EvalCase.live_expected: EvalExpectation | None`。
- Produces: `run_live_case(case: EvalCase, environ: Mapping[str, str]) -> EvalCaseResult`。
- Produces: `run_live_suite(cases: tuple[EvalCase, ...], environ: Mapping[str, str], samples: int) -> EvalSuiteResult`。
- Consumes: 生产 `create_runtime()`、临时 `initialize_state()` 和现有结构观察函数。

- [ ] **Step 1: 写 live schema RED**

扩展测试 fixture：

```python
case["layers"] = ["live"]
case.pop("offline")
case["live"] = {"expected": {
    "answer_contains": [],
    "answer_excludes": ["没有权限"],
    "tool_runs": ["run_command"],
    "tool_statuses": {"run_command": "succeeded"},
    "audit_events": ["tool.succeeded"],
    "request_contains": [],
    "max_tool_runs": 1,
    "approval_statuses": [],
    "files": {},
    "absent_files": [],
    "error_code": None,
}}
```

断言：live-only active case 合法；active live 缺 `live.expected` 拒绝；offline layer 仍必须有 scripted responses；
`live` 未知字段和 credential-like 字段拒绝。

- [ ] **Step 2: 运行 RED，再最小扩展 dataclass/parser**

Run: `uv run python -m unittest tests.test_eval_cases -v`

Expected: `unknown field live` 或 active live 缺少契约。

复用现有 `_parse_expectation()`；不复制第二套字段解析。`live.expected` 不允许 `files` 或 approval actions
产生副作用时，由 runner 在执行前拒绝。

- [ ] **Step 3: 写 live runner RED**

使用完整 `FakeLiveProvider`（实现真实 `complete/aclose` 契约）。测试只 patch `create_runtime` 这一真实网络
装配边界，返回由真实 TurnService、Policy、Tools、SQLite 和 FakeLiveProvider 组成的 `AgentRuntime`，断言：

- 每个 sample 得到独立 state/database/workspace；
- exact `tool_runs` 顺序和 status 决定 PASS；
- pending Approval 不消费；
- live case 含 `approval_actions`、预期文件副作用或非 live layer 时返回 `unsafe_live_case`；
- Provider 异常变成 `execution_error`，后续 sample 继续；
- `EvalCaseResult` 不含 answer、request、stdout、reasoning 或路径字段。

fake 只替代真实网络 Provider；TurnService、Policy、Tool、SQLite 与 verifier 保持真实。

- [ ] **Step 4: 运行 RED，再实现 live runner**

Run: `uv run python -m unittest tests.test_eval_runner -v`

Expected: 缺少 `run_live_case/run_live_suite`。

`run_live_case()` 创建 `TemporaryDirectory(prefix="lobster0-live-eval-")`，初始化默认 state，使用传入 environ
加载 config/API key，构建 Runtime，执行一次 query，读取 ToolRun/Audit/Approval，最终 `await runtime.aclose()`。
live verifier 对 `expected.tool_runs` 使用 exact tuple equality；不检查 Provider 内部请求文本。

- [ ] **Step 5: GREEN、Ruff 与提交**

```bash
uv run python -m unittest tests.test_eval_cases tests.test_eval_runner -v
uv run ruff check src/lobster0/evals/cases.py src/lobster0/evals/runner.py tests/test_eval_cases.py tests/test_eval_runner.py
git add src/lobster0/evals/cases.py src/lobster0/evals/runner.py tests/test_eval_cases.py tests/test_eval_runner.py
git commit -m "feat(eval): 增加 isolated live Provider runner"
```

---

### Task 4: Live CLI, Lark Scenarios, and Output Redaction

**Files:**
- Modify: `src/lobster0/cli.py`
- Modify: `evals/scenarios/phase2.v1.jsonl`
- Modify: `evals/README.md`
- Modify: `tests/test_cli_eval.py`
- Modify: `tests/test_eval_cases.py`
- Modify: `tests/test_eval_runner.py`

**Interfaces:**
- Produces: `lobster0 eval run --suite live --samples 1..5 --confirm-live`。
- Produces: live cases `ACTION-OPEN-APP-001` 与 `LARK-STATUS-001`。
- Consumes: Task 3 `run_live_suite()`。

- [ ] **Step 1: 写 CLI RED**

断言：

- parser 接受 `--suite live --samples 3 --confirm-live`；
- 缺 `--confirm-live` 返回 2 且不创建 Runtime；
- samples 为 0/6 返回 argparse exit 2；
- offline 带 live-only 参数返回 2；
- 缺 `.env`/API key 返回 2，错误只包含变量名；
- fake live suite 输出 `PASS LARK-STATUS-001#1 12ms` 与汇总，不含 fake answer/stdout/reasoning/openId/scope；
- live suite failure 返回 1 和稳定短码。

- [ ] **Step 2: 运行 RED，再实现 CLI dispatch**

Run: `uv run python -m unittest tests.test_cli_eval -v`

Expected: `--suite live` 不被 choices 接受。

把 `_run_eval()` 拆成最小 `_run_offline_eval()` / `_run_live_eval()`；list/validate 保持无凭据。live 使用
`load_dotenv(Path.cwd() / ".env")` 与 config 中的 `api_key_env`，只把副本传给 runner，不打印值。

- [ ] **Step 3: 写场景数据 RED**

仓库契约断言：

- active 总数 22，offline gate 仍为 21，live gate 为 2；
- `ACTION-OPEN-APP-001.live_expected` 精确要求 `system_info, run_command`、waiting Approval、max 2；
- `LARK-STATUS-001` 为 live-only，query 明确“只检查，不修改”，精确要求一个 succeeded `run_command` 和零 Approval；
- live case 均无 `approval_actions` 和文件副作用。

- [ ] **Step 4: 运行 RED，再更新 JSONL/README**

Run: `uv run python -m unittest tests.test_eval_cases tests.test_cli_eval -v`

在 `phase2.v1.jsonl` 更新 ACTION 并增加 LARK case；`evals/README.md` 分别记录 21 offline 与 2 live，禁止把
live case 计入默认离线门禁。

- [ ] **Step 5: 运行完整离线门禁，证明 live 没污染默认路径**

```bash
uv run lobster0 eval validate --root evals/scenarios
uv run lobster0 eval run --suite offline --root evals/scenarios
```

Expected: 22 cases validated；Offline eval 21/21 passed。

- [ ] **Step 6: GREEN、Ruff 与提交**

```bash
uv run python -m unittest tests.test_cli_eval tests.test_eval_cases tests.test_eval_runner -v
uv run ruff check src/lobster0/cli.py src/lobster0/evals tests/test_cli_eval.py tests/test_eval_cases.py tests/test_eval_runner.py
git add src/lobster0/cli.py evals/scenarios/phase2.v1.jsonl evals/README.md tests/test_cli_eval.py tests/test_eval_cases.py tests/test_eval_runner.py
git commit -m "feat(eval): 接通 explicit live gate 与 lark 场景"
```

---

### Task 5: Real Local Smoke and Incident Fixes

**Files:**
- Modify only files proven necessary by failing live/unit regressions.
- Update tests before every production fix.

**Interfaces:**
- Consumes: Tasks 1–4 production CLI and current `.env`/lark-cli auth state。
- Produces: 真实 `lark-cli auth status --json` Tool execution and two cases × three samples evidence。

- [ ] **Step 1: 先执行不经过模型的真实只读 Tool smoke**

用生产 `normalize_command()`、PolicyEngine 内置 rules 和 `RunCommandTool.execute()` 调用：

```json
{"program":"lark-cli","args":["auth","status","--json"]}
```

只打印：resolver 是否命中、Policy action、exit code、ToolResult 是否 ok；不得打印 stdout/stderr 或身份。

Expected: resolver trusted，Policy `allow`，ToolResult ok，exit 0。

- [ ] **Step 2: 运行真实 live gate**

```bash
uv run lobster0 eval run --suite live --root evals/scenarios --samples 3 --confirm-live
```

Expected: `ACTION-OPEN-APP-001` 与 `LARK-STATUS-001` 各 3 次，共 6/6。该命令不得消费 pending Approval 或
打开/修改飞书。

- [ ] **Step 3: 若 live 失败，按 incident TDD 修复**

每个失败必须先新增一个最小离线回归，复现具体结构错误（例如错误 executable 名、args 顺序、Provider
tool arguments、lark output 超限），确认 RED 后修改共享根因，再重跑 focused + live。最多五轮；不得放宽
Policy、自动批准、删除断言或把 case 改成接受口头回答。

- [ ] **Step 4: 提交仅由真实 smoke 证明必要的修复**

```bash
git add src/lobster0/policy/command.py src/lobster0/tools/command.py src/lobster0/runtime.py src/lobster0/evals/runner.py src/lobster0/agent/context.py tests/test_command_policy.py tests/test_run_command.py tests/test_runtime.py tests/test_eval_runner.py tests/test_context.py
git commit -m "fix(live): 修复真实 lark-cli Agent 闭环"
```

若没有生产改动，不创建空提交。

---

### Task 6: Phase 2 Engineering Docs and Phase 3 Landing Document

**Files:**
- Create: `docs/engineering/phase-2/lark-cli-and-live-eval.md`
- Create: `docs/engineering/phase-3/phase-3-engineering-plan.md`
- Create: `docs/evals/releases/v0.2.1.md`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/engineering/phase-2/20260808_command-execution.md`
- Modify: `docs/engineering/phase-2/20260808_testing-and-debugging.md`
- Modify: `docs/evals/README.md`

**Interfaces:**
- Consumes: 已验证的真实文件、类、CLI、测试数、offline/live 结果和 commit。
- Produces: Phase 2 完整工程事实与 Phase 3 可直接拆任务的工程落地文档。

- [ ] **Step 1: 编写 Phase 2 模块工程文档**

必须包含：范围/非范围、NVM resolver 图、Tool/Policy/Approval 时序、精确 allow/deny/approval 表、每个源文件
职责、Doctor、live runner、JSONL schema、测试矩阵、错误排查、真实 smoke 复现和安全限制。只记录已经通过
Task 5 的事实。

- [ ] **Step 2: 编写 Phase 3 工程落地文档**

主文档必须用大白话和 Mermaid，覆盖：

- Phase 2 到 Phase 3 的接口图；
- Identity、`MEMORY.md`、daily memory、Skill manifest/body、compaction summary 的文件与 SQLite ownership；
- ContextBuilder 顺序和 token budget；
- `read_memory` / `propose_memory`（不自动写长期记忆）的 Tool/Approval 流；
- 凭据过滤、Prompt injection、Skill hash/version、最多激活 3 个 Skill；
- compaction 触发阈值、保留最近两轮与 pending Approval、原消息不删除；
- 数据 schema/migration、错误码、模块职责、TDD 任务顺序、回归 query、调试、恢复、回滚和完成定义；
- 参考 nanobot、ZeroClaw、RayClaw、openclaw-python 时区分“借鉴思想”和“Lobster0 已实现事实”；
- 明确 Phase 3 不实现 Feishu/Telegram/Discord Channel 与自动改源码。

文档不得包含未决占位符，也不能把 Phase 3 规划写成当前能力。

- [ ] **Step 3: 写 v0.2.1 发布证据与同步索引**

记录真实测试数、21/21 offline、6/6 live、lark-cli version、运行命令、脱敏限制、已知边界和 Phase 3 下一步。
README badge/status 必须从 Phase 2.4 更新为 Phase 2 verified。

- [ ] **Step 4: 文档门禁与提交**

用标准库脚本验证：修改文档的本地链接存在、Mermaid fence 闭合、无未决占位符、历史 v0.2.0 数字未改写。

```bash
git diff --check
git add README.md docs/README.md docs/architecture/20260807_系统架构.md docs/getting-started/20260807_本地运行指南.md docs/engineering/README.md docs/engineering/phase-2 docs/engineering/phase-3 docs/evals
git commit -m "docs(phase3): 完成 Phase 2 事实与 Memory 落地方案"
```

---

### Task 7: Progress HTML, Release Gate, and Push

**Files:**
- Modify: `docs/progress/index.html`
- Modify outside Git: `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/lobster0-progress.html`
- Modify: `docs/superpowers/plans/2026-08-08-phase-2-lark-cli-live-eval.md`（只更新 checkbox/result）

**Interfaces:**
- Consumes: 所有最终门禁的真实数字和 commit hashes。
- Produces: 两个一致、可点击、可解析的进度页面与已推送 `origin/main`。

- [ ] **Step 1: 更新仓库进度页**

页面显示：Phase 2 completed、真实 unittest 数、21/21 offline、6/6 live、8 Tools、lark-cli、最近提交、Phase 3
Memory/Skills/Compaction next。不得再显示 P2.1B/153 tests 或“lark-cli pending”。保留响应式布局、键盘焦点和
`prefers-reduced-motion`。

- [ ] **Step 2: 同步独立 HTML**

把仓库页内容同步到用户指定文件；外部页的文档链接使用 GitHub `NEDONION/lobster0` 地址，保证从
`file://` 打开仍可点击。该文件不执行 `git add`。

- [ ] **Step 3: HTML 与可访问性门禁**

标准库 `HTMLParser` 解析两页；断言只有一个 `h1`、每个 section 的 `aria-labelledby` 可解析、无重复 id、
链接非空、旧基线文本不存在、Phase 2/3 状态正确。用本地浏览器打开独立 HTML 并检查桌面与窄屏布局。

- [ ] **Step 4: 完整发布门禁**

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run lobster0 eval validate --root evals/scenarios
uv run lobster0 eval run --suite offline --root evals/scenarios
uv build
git diff --check
```

再运行：文档链接/Mermaid/HTML gate、真实 lark-cli 只读 smoke、live 6/6、真实 PTY TUI 启停。任何失败先修复并
重跑；不得只报告“应该通过”。

- [ ] **Step 5: Diff 安全审计**

确认 staged diff 不含 `.env`、Key、Token、Secret、openId、用户名、scope、`dist/`、`build/`、临时 SQLite、
Tool stdout 或另外两份未跟踪用户文档。`git status --short` 只允许本任务文件与那两份受保护 untracked。

- [ ] **Step 6: 提交进度与计划结果**

```bash
git add docs/progress/index.html docs/superpowers/plans/2026-08-08-phase-2-lark-cli-live-eval.md
git commit -m "docs(progress): 标记 Phase 2 verified 与 Phase 3 next"
```

- [ ] **Step 7: 在已提交树上复验并推送**

重跑 unittest、offline eval、Ruff；然后：

```bash
git push origin main
git ls-remote origin refs/heads/main
```

远端 hash 必须与本地 HEAD 一致。GitHub 若提示仓库迁移，只记录提示，不擅自改 remote。

---

## Plan Self-Review

- Spec coverage：可信 resolver/Node PATH 为 Task 1；Policy/Doctor/Provider 契约为 Task 2；live schema/runner 为
  Task 3；CLI/场景为 Task 4；真实 smoke 为 Task 5；Phase 2/3 文档为 Task 6；双进度页与发布为 Task 7。
- 安全覆盖：无 Shell、无自动认证、无自动 `--yes`、无 live 自动审批、无凭据输出、无外部写 smoke。
- 类型一致：`discover_lark_cli` 返回 entrypoint；`normalize_command` 仍返回真实 resolved target；Node bin 由
  `lark_cli_runtime_bin(resolved_program)` 推导；live 复用 `EvalExpectation`。
- 默认行为：offline suite、8 Tool Registry、TUI 与现有配置不变；缺 lark-cli 只产生 Doctor WARN。
- Placeholder scan：计划不包含未决占位符、模糊动作或未定义的错误处理步骤。
