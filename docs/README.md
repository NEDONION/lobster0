# MiniClaw 文档中心

这里是 MiniClaw 的产品、架构、运行和开发记录入口。项目简介与当前可执行命令先看仓库根目录的
[README](../README.md)。

## 推荐阅读顺序

1. 打开 [开发进度页](progress/index.html)，查看当前 Phase、验证状态和下一步交付。
2. 阅读 [PRD](product/20260807_产品需求文档.md)，确认 v0.1 范围、非目标和验收标准。
3. 阅读 [完整工程落地设计](superpowers/specs/2026-08-07-miniclaw-complete-engineering-design.md)，确认 v1.0
   的范围、契约、安全边界、数据模型和分阶段交付标准。
4. 阅读 [Phase 2：Tool、权限与安全执行设计](superpowers/specs/2026-08-07-phase-2-tools-security-design.md)，
   了解本机 Tool、Workspace、审批、命令和 SSRF 的落地方案。
5. 阅读 [系统架构](architecture/20260807_系统架构.md)，理解渠道、Agent、工具、安全和数据边界。
6. 按 [本地运行指南](getting-started/20260807_本地运行指南.md)安装项目并验证唯一 TUI。
7. 阅读 [单入口 Textual TUI 工程文档](engineering/phase-2/single-entry-tui.md)，理解 Runtime、RunEvent、
   Worker、Tool 卡与审批 Modal。
8. 阅读 [Agent 回归工程文档](engineering/phase-2/agent-regression-evals.md)，理解每个版本如何固定 query、
   跑离线 gate 并记录 baseline。
9. 开发完成后使用 [Codex 对话沉淀工作流](development/20260807_Codex对话沉淀工作流.md)同步决策。

## 产品与架构

| 文档 | 内容 |
| --- | --- |
| [产品需求文档](product/20260807_产品需求文档.md) | 定位、用户流程、功能范围、图解、验收标准和里程碑 |
| [系统架构](architecture/20260807_系统架构.md) | 运行边界、主链路、计划包结构和安全原则 |

## 使用与开发

| 文档 | 内容 |
| --- | --- |
| [本地运行指南](getting-started/20260807_本地运行指南.md) | Python、uv、裸 TUI、审批、测试和当前项目状态 |
| [Codex 对话沉淀工作流](development/20260807_Codex对话沉淀工作流.md) | 每轮开发后如何同步产品、架构和运行文档 |

## Phase 1 模块工程文档

> Phase 1 文档保留当时的实现快照。`miniclaw chat` 已在 Phase 2.2B 移除；涉及当前入口时以
> [单入口 Textual TUI 工程文档](engineering/phase-2/single-entry-tui.md) 为准。

| 模块 | 文档 |
| --- | --- |
| 本地环境与凭据 | [environment.md](engineering/phase-1/environment.md) |
| Provider 稳定契约 | [provider-contract.md](engineering/phase-1/provider-contract.md) |
| HTTP/SSE Provider | [openai-compatible-provider.md](engineering/phase-1/openai-compatible-provider.md) |
| ContextBuilder | [context-builder.md](engineering/phase-1/context-builder.md) |
| AgentRunner | [agent-runner.md](engineering/phase-1/agent-runner.md) |
| 会话持久化 | [conversation-storage.md](engineering/phase-1/conversation-storage.md) |
| TurnService | [turn-service.md](engineering/phase-1/turn-service.md) |
| CLI Chat（历史快照） | [cli-chat.md](engineering/phase-1/cli-chat.md) |
| 测试与调试 | [testing-and-debugging.md](engineering/phase-1/testing-and-debugging.md) |

## Phase 2 模块工程文档

完整的已实现模块索引见 [工程文档索引](engineering/README.md)；本表保留从文档中心直接进入当前 Phase 2 文档的入口。

| 模块 | 文档 |
| --- | --- |
| Tool Runtime 与 system_info | [tool-runtime-and-system-info.md](engineering/phase-2/tool-runtime-and-system-info.md) |
| Workspace 只读文件与搜索 | [workspace-read-tools.md](engineering/phase-2/workspace-read-tools.md) |
| Agent 场景回归与 Benchmark 基线 | [agent-regression-evals.md](engineering/phase-2/agent-regression-evals.md) |
| 安全文件写入 | [filesystem-tools.md](engineering/phase-2/filesystem-tools.md) |
| Approval 生命周期与续执行 | [approval-lifecycle.md](engineering/phase-2/approval-lifecycle.md) |
| 单入口 Textual TUI、Runtime 与审批 Modal | [single-entry-tui.md](engineering/phase-2/single-entry-tui.md) |
| Approvals CLI（已迁移） | [cli-approvals.md](engineering/phase-2/cli-approvals.md) |

## 评测与版本证据

| 文档 | 内容 |
| --- | --- |
| [评测记录规范](evals/README.md) | 场景、baseline、release record 与本地 raw result 的边界 |
| [Eval v0.1.0](evals/releases/v0.1.0.md) | 首个 offline-v1 基线：177 tests、10/10 Agent cases |
| [Agent 回归与 Benchmark 设计](superpowers/specs/2026-08-08-agent-regression-benchmark-design.md) | 对 OpenClaw/ZeroClaw/nanobot/RayClaw/Claw Bench/OpenJarvis 的方法映射 |
| [R1/R2 实施计划](superpowers/plans/2026-08-08-agent-regression-benchmark.md) | TDD 任务、文件、命令与完成定义 |

## 设计与实施记录

- [Phase 2：Tool、权限与安全执行设计](superpowers/specs/2026-08-07-phase-2-tools-security-design.md)：
  `system_info`、文件与搜索 Tool、参数绑定审批、受限命令、SSRF、防逃逸和测试矩阵。
- [Phase 1：CLI Agent 闭环设计](superpowers/specs/2026-08-07-phase-1-cli-agent-design.md)：DeepSeek
  V4 Pro、Provider、Runner、Turn 持久化、`.env` 安全边界和验收标准。
- [完整工程落地设计](superpowers/specs/2026-08-07-miniclaw-complete-engineering-design.md)：Python
  版个人 Claw v1.0 的工程规格、参考来源、模块契约、部署、测试和交付阶段。
- [Agent 回归与 Benchmark 设计](superpowers/specs/2026-08-08-agent-regression-benchmark-design.md)：
  Claw-like query、四层测试、版本记录、live 采样与安全 gate 方法论。
- [Gemini 风格单入口 TUI 设计](superpowers/specs/2026-08-08-gemini-style-tui-and-lark-cli-design.md)：
  OpenClaw/pi-tui、Gemini CLI、OpenCode 与 Python 方案对比，以及 Textual 选型。
- [`superpowers/specs/`](superpowers/specs/)：经确认的设计规格。
- [`superpowers/plans/`](superpowers/plans/)：可执行的实施计划与验证命令。

正式使用说明以上述分类文档为准；实施记录用于解释某次变更如何落地。
