# Direct Application Launch Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让真实 Provider 在 Owner 要求打开本机应用时使用现有 `run_command` 和 Approval，而不是口头拒绝或生成 Shell 命令。

**Architecture:** 不新增 Tool；只收紧 `ContextBuilder` 的系统规则和 `RunCommandTool` 的 Provider 可见 description。现有 Policy 继续把 `open -a <App>` 归一化为 exact argv，并在没有规则时创建 waiting Approval。

**Tech Stack:** Python 3.12、标准库 `unittest`、现有 OpenAI-compatible Provider、SQLite、JSONL eval、Textual TUI。

## Global Constraints

- 不硬编码“飞书”意图路由，不新增 `open_application` Tool。
- 不允许 Shell、管道、内联代码或自动批准。
- 单元测试离线；真实 DeepSeek 只做不执行 Tool 的 planning probe。
- 提交标题保持中英文各约一半。

---

### Task 1: 收紧 Provider 可见的本机动作与 exact-argv 契约

**Files:**
- Modify: `tests/test_context.py`
- Modify: `tests/test_tool_contract.py`
- Modify: `src/miniclaw/agent/context.py`
- Modify: `src/miniclaw/tools/command.py`

**Interfaces:**
- Consumes: `ContextBuilder.build(...)` 和 `RunCommandTool.definition`。
- Produces: 明确告诉 Provider 使用 Tool 请求 Approval、直接执行 executable、禁止 Shell，并给出 macOS `open -a` 的通用模式。

- [ ] **Step 1: 写 Context 失败回归测试**

在 `test_build_includes_available_tool_schemas_and_tool_usage_rule` 中增加行为契约断言：系统消息必须说明本机动作要先尝试 Tool，Approval 不是 Tool 不可用，并且不能用口头说明代替可执行动作。

- [ ] **Step 2: 写 run_command Schema 失败回归测试**

新增 `test_run_command_schema_teaches_direct_execution_and_macos_app_launch`，从真实 Registry Schema 读取 description，断言它包含 direct executable、no shell、approval 和 `open -a` 四项语义。

- [ ] **Step 3: 运行聚焦测试并确认 RED**

Run:

```bash
uv run python -m unittest tests.test_context tests.test_tool_contract -v
```

Expected: 新断言因当前系统规则和 Tool description 缺少动作/Approval/`open -a` 契约而失败。

- [ ] **Step 4: 最小修改两个 Provider 可见字符串**

修改 `_SYSTEM_PREAMBLE`：Owner 要求本机动作时优先尝试已列出的 Tool；需要 Approval 不是无权限；禁止用说明性文本替代可执行的 Tool Call。

修改 `RunCommandTool.definition.description`：只允许一个 executable 和独立 argv；禁止 Shell/管道/内联代码；未命中规则仍应调用以请求 Approval；macOS 打开应用使用 `program=open, args=[-a, Application]`。

- [ ] **Step 5: 运行聚焦测试并确认 GREEN**

Run:

```bash
uv run python -m unittest tests.test_context tests.test_tool_contract -v
```

Expected: PASS。

- [ ] **Step 6: 提交契约修复**

```bash
git add tests/test_context.py tests/test_tool_contract.py src/miniclaw/agent/context.py src/miniclaw/tools/command.py
git commit -m "fix(agent): 引导 direct Tool action 与安全审批"
```

### Task 2: 固化打开应用事故回归与发布证据

**Files:**
- Modify: `evals/scenarios/phase2.v1.jsonl`
- Modify: `tests/test_cli_eval.py`
- Modify: `tests/test_eval_cases.py`
- Modify: `README.md`
- Modify: `docs/engineering/phase-2/agent-regression-evals.md`
- Modify: `docs/engineering/phase-2/command-execution.md`
- Modify: `docs/engineering/phase-2/testing-and-debugging.md`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Consumes: JSONL schema 1、offline eval runner、现有 `run_command` Approval 流程。
- Produces: `ACTION-OPEN-APP-001`，当前基线 21/21，以及不执行 Tool 的真实 DeepSeek planning gate。

- [ ] **Step 1: 新增失败的数量和事故场景测试**

把 CLI/eval 数量期望提升到 21，并要求 active case 集合包含 `ACTION-OPEN-APP-001` 的 `offline`、`live`、`incident`、`app-launch` 元数据。

- [ ] **Step 2: 运行聚焦测试并确认 RED**

Run:

```bash
uv run python -m unittest tests.test_eval_cases tests.test_cli_eval -v
```

Expected: 当前只有 20 个场景，缺少 `ACTION-OPEN-APP-001`。

- [ ] **Step 3: 新增最小 JSONL 场景**

在 `phase2.v1.jsonl` 新增 active case：query 为“你能帮我打开飞书吗”；offline Provider 发出 `run_command`，参数为 `program=open`、`args=[-a, Feishu]`；不批准，断言 `waiting_approval`、`approval.created`、pending Approval 和零外部副作用。

- [ ] **Step 4: 运行聚焦测试与 offline suite 并确认 GREEN**

Run:

```bash
uv run python -m unittest tests.test_eval_cases tests.test_cli_eval -v
uv run miniclaw eval validate --root evals/scenarios
uv run miniclaw eval run --suite offline --root evals/scenarios
```

Expected: 21 cases validated，21/21 passed。

- [ ] **Step 5: 同步当前文档基线**

把当前门禁从 20/20 更新为 21/21，并在 README、Agent eval、Command、调试手册和 progress HTML 说明本次事故、期望 Approval 流程与 live planning 限制；历史 `v0.2.0` 记录保持 20/20，不改写历史证据。

- [ ] **Step 6: 运行三次真实 planning probe**

用当前 `.env` 和真实 Tool Schema 请求 DeepSeek；允许可选 `system_info`，但后续必须产生 `run_command(open, [-a, Feishu])`。probe 不经过 Executor、不批准、不启动飞书；记录三次采样结果，不记录 API Key 或 reasoning 原文。

- [ ] **Step 7: 运行完整发布门禁**

Run:

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv build
git diff --check
```

Expected: 全部通过，无密钥、构建产物或无关文件进入 diff。

- [ ] **Step 8: 提交事故回归与文档**

```bash
git add evals/scenarios/phase2.v1.jsonl tests/test_cli_eval.py tests/test_eval_cases.py README.md docs/engineering/phase-2/agent-regression-evals.md docs/engineering/phase-2/command-execution.md docs/engineering/phase-2/testing-and-debugging.md docs/progress/index.html docs/superpowers/plans/2026-08-08-direct-application-launch-fix.md
git commit -m "test(agent): 固化 app launch regression 与 live gate"
```
