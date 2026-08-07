# MiniClaw TUI 回归测试规范

> 状态：已落地。本规范与 `tests/test_tui.py`、`tests/test_cli.py`、`tests/test_turn.py` 和
> `tests/test_agent_runner.py` 共同构成版本门禁。当前全仓基线：270/270 tests、20/20 offline
> Agent cases、Ruff PASS。

## 1. 为什么 TUI 必须单独回归

TUI 不是“好看的 `print()`”。它同时承载五类约束：

1. 用户输入不能被误提交或丢失；
2. 模型、Tool 和审批不能卡住界面事件循环；
3. Tool 参数、执行状态和结果必须可见；
4. 默认 Deny、Esc 取消、Owner 绑定等安全语义不能被 UI 绕过；
5. ANSI、控制字符、超长输出和动态 call ID 不能破坏终端。
6. Provider usage 缺失不能被 UI 猜成 0；长粘贴失败不能丢草稿。
7. Session/Always 按钮不能扩大 Core 给出的审批 scope。

因此，“单元测试通过”不等于“TUI 可以发布”。每个版本要同时验证事件契约、无头交互、
真实终端启动和 Agent 场景回归。

## 2. 测试分层

```mermaid
flowchart TB
    L0["L0 Pure contracts<br/>sanitizer / RunEvent / projection data"]
    L1["L1 Headless Textual pilot<br/>keys / focus / widgets / modal / 80x24"]
    L2["L2 Runtime integration<br/>TurnService / ToolExecutor / SQLite states"]
    L3["L3 Real PTY smoke<br/>bare command / terminal lifecycle / exit"]
    L4["L4 Offline Agent scenarios<br/>Claw-like query / policy / audit / final answer"]
    L0 --> L1 --> L2 --> L3 --> L4
```

| 层 | 用什么跑 | 主要防什么 | 是否访问真实模型 |
| --- | --- | --- | --- |
| L0 纯契约 | `unittest` | 事件丢失、异常泄漏、控制字符 | 否 |
| L1 无头 TUI | Textual `run_test()` + Pilot | 焦点、按键、Widget、Modal、小终端回归 | 否 |
| L2 真实 Core 集成 | Fake Provider + 真实 SQLite/Policy/Tool | UI 看到未落库状态、审批绕过 | 否 |
| L3 PTY smoke | 伪终端启动 `miniclaw` | TTY guard、入口、启停、终端兼容 | 否 |
| L4 Agent gate | `miniclaw eval run --suite offline` | query 穿过 Agent/Tool/Policy/DB 后行为漂移 | 否 |

Live DeepSeek 是发布抽样，不作为每次提交的硬门禁：它有费用、网络和模型随机性。

## 3. 可观测 Trace 的验收契约

界面展示的不是一堆临时日志，而是从 Core 事件投影出的稳定状态：

```mermaid
sequenceDiagram
    participant Provider
    participant Runner as AgentRunner
    participant Exec as ToolExecutor
    participant DB as SQLite
    participant TUI

    Provider-->>Runner: reasoning_content + tool call
    Provider-->>Runner: reported usage or missing usage
    Runner-->>TUI: model_usage
    Runner-->>TUI: model_reasoning
    Runner-->>TUI: tool_requested + raw arguments
    Exec->>DB: ToolRun = running
    Exec-->>TUI: tool_started
    Exec->>DB: ToolRun = terminal status
    Exec-->>TUI: tool_finished + duration + preview
```

必须始终成立的断言：

- 每个 call ID 只有一张 Tool 卡；
- Tool 标题始终可见，并显示当前状态；展开后保留 `requested -> running -> 终态` 完整路径；
- 展开后能看到模型请求参数、执行耗时和结果预览；
- Provider 返回非空 `reasoning_content` 时，生成紧凑、弱色、默认折叠的 reasoning 卡；
- Provider 没返回 reasoning 时，不伪造卡片；
- 单卡可用键盘 Enter 或鼠标展开；
- `Ctrl+O` 只展开/收起全部详情，不隐藏概要；
- ANSI/C0/C1 在进入 Widget 前被移除；
- Tool 预览不超过 2,000 字符，折叠详情不超过 8,000 字符；
- 审批弹窗显示的是 Policy 归一化后参数，不是 Tool 卡中的原始模型参数。
- 审计栏只显示 Provider 真实 usage；缺失显示 `N/A`；
- 用户/Agent 始终有文字角色标签和不同结构；
- 失败或取消后 Composer 逐字恢复本轮提交文本；
- TUI 只显示 Core `grant_modes` 给出的 Once/Session/Always。

## 4. 稳定用例集

ID 固定，行为变更时更新原 ID；只有新增独立能力时才新增 ID。

| ID | 场景 | 必须断言 | 当前位置 |
| --- | --- | --- | --- |
| `TUI-001` | 80x24 启动 | 状态、transcript、composer 均可见，composer 获焦 | `test_tui` |
| `TUI-002` | Enter 提交 | 只调用一次 Turn，输入清空，焦点恢复 | `test_tui` |
| `TUI-003` | Shift+Enter | 只换行，不调用 Agent | `test_tui` |
| `TUI-004` | Esc 取消 | Worker 收到 `CancelledError`，composer 恢复 | `test_tui` |
| `TUI-005` | 流式文本 | 同 Turn 只有一张临时 Assistant，完成后固化 | `test_tui` |
| `TUI-006` | Tool 状态链 | 同 call ID 从 requested 更新至 running/终态 | `test_tui` |
| `TUI-007` | Tool 单卡展开 | 概要不消失，Enter 显示参数/耗时/预览 | `test_tui` |
| `TUI-008` | Provider reasoning | 只展示真实返回字段，详情有界且安全 | `test_tui` |
| `TUI-009` | Ctrl+O | Tool/Reasoning 详情全展开后全收起，卡片不隐藏 | `test_tui` |
| `TUI-010` | 控制字符 | ANSI、OSC、C0/C1 不能改写终端 | `test_tui` |
| `TUI-011` | 危险动作审批 | 完整归一化参数、Deny 默认焦点、文件只 Allow once | `test_tui` |
| `TUI-012` | Esc 关闭审批 | 等价 Deny，不执行动作 | `test_tui` |
| `TUI-013` | Slash command | 不访问模型，在同一 transcript 显示 | `test_tui` |
| `TUI-014` | `/new` | 清空投影、更换 session ID、不创建第二 Runtime | `test_tui` |
| `TUI-015` | 缺少状态 | 在同一 App onboarding，初始化后原地进入 composer | `test_tui` |
| `TUI-016` | 裸命令 TTY guard | 非 TTY 和 `TERM=dumb` 明确失败 | `test_cli` |
| `TUI-017` | 持久化事件顺序 | Tool/Turn 事件发送前数据库已进入对应状态 | `test_turn` |
| `TUI-018` | 裸命令 PTY smoke | 真实启动、可提交、可正常退出 | 发布前 smoke |
| `TUI-019` | 对话角色层级 | “你”/“MiniClaw”文字标签、不同容器、Assistant 仍单 Widget 流式更新 | `test_tui` |
| `TUI-020` | 长文本失败恢复 | 65 KiB+ 中文原文逐字回填，焦点恢复 | `test_tui` |
| `TUI-021` | 真实遥测 | context/input/output/tool/iteration/duration 正确投影；缺失为 N/A | `test_tui` / `test_agent_runner` |
| `TUI-022` | UI 双语 | 默认中文，`/lang zh|en` 原地切换且不调用 Agent | `test_tui` / `test_config` |
| `TUI-023` | 分级审批 | 只显示 Core grant_modes；Session/Always 精确 scope 且失败不产生规则 | `test_tui` / `test_tool_executor` |

## 5. 为什么不做全屏快照主门禁

全屏字符快照对 Textual 版本、终端尺寸、Unicode 宽度和主题非常敏感，很容易出现“行为没变，
快照全红”。MiniClaw 当前优先断言：

- Widget 数量与类型；
- reactive 状态，例如 `collapsed`、`status`、`disabled`；
- 焦点所在 Widget；
- 用户按键产生的业务调用；
- 不可绕过的数据库/安全终态。

只有出现需要长期固定的复杂视觉布局时，再增加少量指定尺寸快照，不替代行为断言。

## 6. Fake 和真实边界

```mermaid
flowchart LR
    FAKE["Fake TurnService / Fake Provider"] --> DETERMINISTIC["确定的按键、事件、失败注入"]
    REAL["Real Policy / ToolExecutor / SQLite"] --> SAFETY["真实审批、状态、审计语义"]
    DETERMINISTIC --> GATE["可重复 CI gate"]
    SAFETY --> GATE
```

Fake 只用在外部不确定边界：模型输出和用于聚焦 UI 的 TurnService。Policy、ToolExecutor、SQLite、
Approval Repository 和 Workspace Guard 在集成测试中使用真实实现。这样既不依赖网络，也不会把
最重要的安全边界 mock 掉。

## 7. 版本必跑门禁

本地与 CI 执行同一组命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check --no-cache .
uv run miniclaw eval validate --root evals/scenarios
uv run miniclaw eval run --suite offline --root evals/scenarios
git diff --check
```

发布或修改 CLI/Textual 依赖时，再加一次真实 PTY smoke：

```text
1. 在伪终端启动裸 miniclaw。
2. 验证屏幕出现状态栏、transcript 和唯一 composer。
3. 提交一条可预测输入或 `/help`。
4. 验证界面仍可交互。
5. 使用 `/quit` 或空闲 Ctrl+D 退出，进程返回 0。
```

## 8. 新增 TUI 能力的测试模板

每个新能力至少要先写一条会失败的行为测试：

1. 用 Fake 只产生本能力需要的最小 `RunEvent`；
2. 用 `app.run_test(size=(80, 24))` 进入无头 App；
3. 通过 Pilot 按键或点击，不直接调用 Widget 内部方法；
4. 断言用户可见文字、焦点和业务副作用；
5. 安全相关能力再加一条真实 Policy/SQLite 集成断言；
6. 确认新测试在实现前为 RED，实现后为 GREEN；
7. 更新本文的用例表和全仓基线数字。

## 9. 当前不做的事

- 不用真实 DeepSeek 输出做每次提交的硬断言；
- 不因为主题或 Unicode 宽度的细微变化让整张全屏快照失败；
- 不展示或测试未由 Provider 返回的隐藏思维链；
- 不为了测试 TUI 再造第二套 Agent Runtime；
- 不在 TUI 测试中放宽 Workspace、命令、HTTP 或 Approval 安全边界。

如果未来增加多面板、主题或复杂响应式布局，再增加少量视觉快照和尺寸矩阵；当前行为契约已覆盖
唯一入口、交互、Trace、审批和安全渲染。

本轮实现细节与 scope 表见
[TUI 可观测、长文本与分级审批](tui-observability-and-scoped-approvals.md)。
