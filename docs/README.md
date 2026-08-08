# MiniClaw 文档中心

这里是 MiniClaw 的产品、架构、运行和开发记录入口。项目简介与当前可执行命令先看仓库根目录的
[README](../README.md)。

## 文件命名约定

- 人类阅读型文档统一使用 `YYYYMMDD_标题.md`，日期取首次进入 Git 的交付日期；
- `README.md`、`AGENTS.md`、`v0.x.y.md` Release Record、已有日期的 plans/specs、HTML 和静态资源保留
  各自工具或目录约定；
- 重命名文档时必须同步仓库内全部链接、校验脚本和测试，不保留失效的旧路径。

## 推荐阅读顺序

1. 打开 [开发进度页](progress/index.html)，查看当前 Phase、验证状态和下一步交付。
2. 阅读 [PRD](product/20260807_产品需求文档.md)，确认 v0.1 范围、非目标和验收标准。
3. 阅读 [OpenClaw / Hermes 能力 Gap 与演进路线](architecture/20260808_OpenClaw-Hermes能力Gap与演进路线.md)，
   用大白话了解当前已经有什么、还缺什么，以及 v0.5.3 Evidence → Memory A～E → Phase 6～9 的顺序。
4. 阅读 [Memory Autopilot 能力 Gap 与重构架构](architecture/20260808_Memory-Autopilot能力Gap与重构架构.md)，
   了解旧方案为什么在新飞书 Session 中像失忆，以及跨渠道分级自动记忆的目标架构。
5. 阅读 [Memory Autopilot 最佳实践与技术选型](engineering/20260808_memory-autopilot-best-practices-and-technology-selection.md)，
   查看 Markdown/SQLite 职责、Flush、检索、隐私和完整测试施工图。
6. 阅读 [Memory Autopilot 工程实现](engineering/phase-5/20260809_memory-autopilot.md)，了解当前跨渠道
   Owner Memory、Markdown Truth、SQLite Control Plane、治理和恢复边界。
7. 用 [Memory Autopilot A～E TDD 实施计划](superpowers/plans/2026-08-09-memory-autopilot.md)追溯从
   Identity/Disclosure 到 Reconcile/Migration 的 RED→GREEN 施工记录。
8. 阅读 [OpenClaw / Hermes 能力对齐工程落地总方案](engineering/20260808_openclaw-hermes-alignment-engineering-roadmap.md)，
   查看自治任务、Sandbox、Browser、自我进化、MCP、Provider、Sub-agent 与多模态的模块和测试边界。
9. 阅读 [完整工程落地设计](superpowers/specs/2026-08-07-miniclaw-complete-engineering-design.md)，确认 v1.0
   的范围、契约、安全边界、数据模型和分阶段交付标准。
10. 阅读 [Phase 2：Tool、权限与安全执行设计](superpowers/specs/2026-08-07-phase-2-tools-security-design.md)，
   了解本机 Tool、Workspace、审批、命令和 SSRF 的落地方案。
11. 阅读 [系统架构](architecture/20260807_系统架构.md)，理解渠道、Agent、工具、安全和数据边界。
12. 按 [本地运行指南](getting-started/20260807_本地运行指南.md)安装项目并验证唯一 TUI。
13. 阅读 [Python Core + pi-tui Bridge](engineering/phase-2/20260808_python-core-pi-tui-bridge.md)，理解版本化协议、
   进程边界、长文本、选择和审批；[单入口 TUI](engineering/phase-2/20260808_single-entry-tui.md)保留 fallback 历史。
14. 阅读 [TUI 回归测试规范](engineering/phase-2/20260808_tui-regression-testing.md)，理解 Trace 可观测契约、
   虚拟终端、跨进程、Textual fallback、PTY smoke 和每版本门禁。
15. 阅读 [exact-argv 命令执行工程文档](engineering/phase-2/20260808_command-execution.md)，理解 `run_command`、硬禁止、
   精确规则、最小环境、超时和 TUI 审批。
16. 阅读 [Personal Machine 权限与 CLI 发现](engineering/phase-2/20260808_personal-machine-permissions.md)，理解
   Workspace/Personal Profile、多根读写、敏感路径、NVM/uv/pnpm CLI 发现和最小子进程环境。
17. 阅读 [Autopilot 权限与紧凑审批 UI](engineering/phase-2/20260808_autopilot-permissions-and-approval-ui.md)，理解
   四档模式、Owner 私聊信任、动态切换、脱敏审计与可滚动审批框。
18. 阅读 [Pinned HTTPS 与 SSRF 工程文档](engineering/phase-2/20260808_https-get-and-ssrf.md)，理解 URL/DNS 校验、
   固定 IP/TLS hostname、重定向、响应预算和不可信内容边界。
19. 阅读 [Agent 回归工程文档](engineering/phase-2/20260808_agent-regression-evals.md)，理解每个版本如何固定 query、
   跑离线 gate 并记录 baseline。
20. 阅读 [飞书 Channel Core](engineering/phase-4/20260808_feishu-channel-core.md)，理解 durable Inbox/Outbox、官方
   WebSocket Transport、Delivery 恢复和卡片审批。
21. 阅读 [真实飞书 Bot 与 Live E2E](engineering/phase-5/20260808_feishu-live-e2e.md)，按图完成 Scope、同应用 Owner
   discovery、15 条真实场景和脱敏 Evidence。
22. 阅读 [飞书单卡片与 lark-cli Skill](engineering/phase-5/20260809_feishu-single-card-and-lark-cli.md)，理解为什么飞书
   只保留 completed card，以及业务 API 为什么直接复用 official `lark-cli`。
23. 开发完成后使用 [Codex 对话沉淀工作流](development/20260807_Codex对话沉淀工作流.md)同步决策。

## 产品与架构

| 文档 | 内容 |
| --- | --- |
| [产品需求文档](product/20260807_产品需求文档.md) | 定位、用户流程、功能范围、图解、验收标准和里程碑 |
| [系统架构](architecture/20260807_系统架构.md) | 运行边界、主链路、计划包结构和安全原则 |
| [OpenClaw / Hermes 能力 Gap](architecture/20260808_OpenClaw-Hermes能力Gap与演进路线.md) | 当前能力、竞品方法映射、优先级、v0.5.3 Evidence→Memory A～E→Phase 6～9 路线与非目标 |
| [开发与交付时间线](engineering/20260809_development-timeline.md) | Capability Phase 与真实 Git/Release 交付顺序的对应关系 |
| [Memory Autopilot 能力 Gap](architecture/20260808_Memory-Autopilot能力Gap与重构架构.md) | 当前失忆根因、EverOS/Tencent/OpenClaw 映射、L0→L3、跨渠道与分级自动治理 |
| [Memory Autopilot 正式设计](superpowers/specs/2026-08-08-memory-autopilot-design.md) | 已确认的产品语义、存储决策、安全边界、失败策略与验收门槛 |
| [Memory Autopilot A～E 实施计划](superpowers/plans/2026-08-09-memory-autopilot.md) | 10 个 TDD 任务、精确文件/接口/命令、Crash Matrix 与完成定义 |
| [Memory Autopilot 工程实现](engineering/phase-5/20260809_memory-autopilot.md) | 已实现的数据流、隐私、恢复、治理、迁移和运维入口 |
| [能力对齐工程落地总方案](engineering/20260808_openclaw-hermes-alignment-engineering-roadmap.md) | 模块、数据模型、配置、错误码、测试矩阵与各 Phase 退出条件 |

## 使用与开发

| 文档 | 内容 |
| --- | --- |
| [本地运行指南](getting-started/20260807_本地运行指南.md) | Python、uv、裸 TUI、审批、测试和当前项目状态 |
| [Codex 对话沉淀工作流](development/20260807_Codex对话沉淀工作流.md) | 每轮开发后如何同步产品、架构和运行文档 |

## Phase 1 模块工程文档

> Phase 1 文档保留当时的实现快照。`miniclaw chat` 已在 Phase 2.2B 移除；涉及当前入口时以
> [Python Core + pi-tui Bridge](engineering/phase-2/20260808_python-core-pi-tui-bridge.md) 为准。

| 模块 | 文档 |
| --- | --- |
| 本地环境与凭据 | [20260807_environment.md](engineering/phase-1/20260807_environment.md) |
| Provider 稳定契约 | [20260807_provider-contract.md](engineering/phase-1/20260807_provider-contract.md) |
| HTTP/SSE Provider | [20260807_openai-compatible-provider.md](engineering/phase-1/20260807_openai-compatible-provider.md) |
| ContextBuilder | [20260807_context-builder.md](engineering/phase-1/20260807_context-builder.md) |
| AgentRunner | [20260807_agent-runner.md](engineering/phase-1/20260807_agent-runner.md) |
| 会话持久化 | [20260807_conversation-storage.md](engineering/phase-1/20260807_conversation-storage.md) |
| TurnService | [20260807_turn-service.md](engineering/phase-1/20260807_turn-service.md) |
| CLI Chat（历史快照） | [20260807_cli-chat.md](engineering/phase-1/20260807_cli-chat.md) |
| 测试与调试 | [testing-and-debugging.md](engineering/phase-1/20260807_testing-and-debugging.md) |

## Phase 2 模块工程文档

完整的已实现模块索引见 [工程文档索引](engineering/README.md)；本表保留从文档中心直接进入当前 Phase 2 文档的入口。

| 模块 | 文档 |
| --- | --- |
| Tool Runtime 与 system_info | [20260807_tool-runtime-and-system-info.md](engineering/phase-2/20260807_tool-runtime-and-system-info.md) |
| Workspace 只读文件与搜索 | [20260807_workspace-read-tools.md](engineering/phase-2/20260807_workspace-read-tools.md) |
| Agent 场景回归与 Benchmark 基线 | [20260808_agent-regression-evals.md](engineering/phase-2/20260808_agent-regression-evals.md) |
| 安全文件写入 | [20260808_filesystem-tools.md](engineering/phase-2/20260808_filesystem-tools.md) |
| Approval 生命周期与续执行 | [20260808_approval-lifecycle.md](engineering/phase-2/20260808_approval-lifecycle.md) |
| Python Core + pi-tui Bridge | [20260808_python-core-pi-tui-bridge.md](engineering/phase-2/20260808_python-core-pi-tui-bridge.md) |
| 单入口与 Textual fallback 历史 | [20260808_single-entry-tui.md](engineering/phase-2/20260808_single-entry-tui.md) |
| TUI 双语、长文本、真实遥测与分级审批 | [20260808_tui-observability-and-scoped-approvals.md](engineering/phase-2/20260808_tui-observability-and-scoped-approvals.md) |
| TUI Trace、虚拟终端与跨进程回归规范 | [20260808_tui-regression-testing.md](engineering/phase-2/20260808_tui-regression-testing.md) |
| Exact-argv 命令执行 | [20260808_command-execution.md](engineering/phase-2/20260808_command-execution.md) |
| Personal Machine 权限与 CLI 发现 | [20260808_personal-machine-permissions.md](engineering/phase-2/20260808_personal-machine-permissions.md) |
| Autopilot 四档权限、Owner 信任与紧凑审批 | [20260808_autopilot-permissions-and-approval-ui.md](engineering/phase-2/20260808_autopilot-permissions-and-approval-ui.md) |
| Pinned HTTPS 与 SSRF 防护 | [20260808_https-get-and-ssrf.md](engineering/phase-2/20260808_https-get-and-ssrf.md) |
| Phase 2 回归、恢复与调试 | [testing-and-debugging.md](engineering/phase-2/20260808_testing-and-debugging.md) |
| Approvals CLI（已迁移） | [20260808_cli-approvals.md](engineering/phase-2/20260808_cli-approvals.md) |

## Phase 3 模块工程文档

> Phase 3 的 legacy 接口继续兼容；Memory Autopilot 已成为当前主路径。

| 模块 | 文档 |
| --- | --- |
| Memory、Skills 与上下文压缩 | [20260808_memory-skills-compaction.md](engineering/phase-3/20260808_memory-skills-compaction.md) |
| Memory Autopilot 最佳实践与技术选型 | [20260808_memory-autopilot-best-practices-and-technology-selection.md](engineering/20260808_memory-autopilot-best-practices-and-technology-selection.md) |
| Memory Autopilot A～E 工程实现 | [20260809_memory-autopilot.md](engineering/phase-5/20260809_memory-autopilot.md) |

## Phase 4 模块工程文档

| 模块 | 文档 |
| --- | --- |
| 飞书 Channel Core、Gateway 与真实 E2E 边界 | [20260808_feishu-channel-core.md](engineering/phase-4/20260808_feishu-channel-core.md) |
| 真实飞书 Bot 创建、Scope、15 条 Live gate 与 Evidence | [20260808_feishu-live-e2e.md](engineering/phase-5/20260808_feishu-live-e2e.md) |
| 飞书单卡片最终回复与 direct lark-cli Skill | [20260809_feishu-single-card-and-lark-cli.md](engineering/phase-5/20260809_feishu-single-card-and-lark-cli.md) |

## 评测与版本证据

| 文档 | 内容 |
| --- | --- |
| [评测记录规范](evals/README.md) | 场景、baseline、release record 与本地 raw result 的边界 |
| [Eval v0.1.0](evals/releases/v0.1.0.md) | 首个 offline-v1 基线：177 tests、10/10 Agent cases |
| [Eval v0.2.0](evals/releases/v0.2.0.md) | Phase 2 历史基线：245 tests、20/20 Agent cases、DeepSeek live smoke |
| [Eval v0.3.0](evals/releases/v0.3.0.md) | Phase 3 基线：296 tests、24/24 Agent cases、Memory/Skills/Compaction |
| [Eval v0.4.1](evals/releases/v0.4.1.md) | Personal Machine 基线：412+27 tests、28+12 cases、lark-cli 只读纵切 |
| [Eval v0.5.1](evals/releases/v0.5.1.md) | Feishu Live Runner、508-test gate 与当时 REAL BOT PENDING 的历史证据 |
| [Eval v0.5.2](evals/releases/v0.5.2.md) | 530+30 tests、29+32 场景、飞书单卡片与 direct lark-cli Skill |
| [Eval v0.5.3 Core](evals/releases/v0.5.3.md) | 562+30 tests、SDK 日志脱敏、Gateway lease/provenance、受管 Live Runner；严格双平台 Live Pending |
| [Eval v0.6.0](evals/releases/v0.6.0.md) | 666+35 tests、39+32 场景、Memory Autopilot A～E 与恢复矩阵 |
| [Agent 回归与 Benchmark 设计](superpowers/specs/2026-08-08-agent-regression-benchmark-design.md) | 对 OpenClaw/ZeroClaw/nanobot/RayClaw/Claw Bench/OpenJarvis 的方法映射 |
| [R1/R2 实施计划](superpowers/plans/2026-08-08-agent-regression-benchmark.md) | TDD 任务、文件、命令与完成定义 |

## 设计与实施记录

- [v0.5.3 Feishu / Discord Live Gate 计划](superpowers/plans/2026-08-09-phase-5-3-feishu-discord-live-gate.md)：
  SDK 日志脱敏、Gateway lease/provenance 与受管 live runner 已实现；两平台严格 15/15 Evidence 待完成。
- [Memory Autopilot A～E TDD 计划](superpowers/plans/2026-08-09-memory-autopilot.md)：
  跨渠道 Identity/Disclosure、Durable Flush、FTS5、自动治理、Reconcile 与 legacy migration。
- [Phase 5.2 生产稳定化计划](superpowers/plans/2026-08-08-phase-5-2-production-hardening.md)：
  系统服务、health、Docker、飞书 15/15 与 24 小时 soak。
- [Phase 6 自治运行与 Sandbox 计划](superpowers/plans/2026-08-08-phase-6-autonomy-runtime-and-sandbox.md)：
  Scheduler、Task Ledger、Heartbeat、主动投递、预算、OS Sandbox 与 Checkpoint。
- [Phase 6.5 Browser Agent 计划](superpowers/plans/2026-08-08-phase-6-5-browser-agent.md)：
  独立 Chromium Profile、snapshot/ref、动作审批、Artifact 与浏览器回归。
- [Phase 7 受控进化与 Memory Reflection 计划](superpowers/plans/2026-08-08-phase-7-controlled-evolution-and-memory-v2.md)：
  Feedback、Reflection、Proposal、扫描、评测、人工应用与回滚。
- [Phase 8 Skills/MCP/Provider 韧性计划](superpowers/plans/2026-08-08-phase-8-skills-mcp-provider-resilience.md)：
  Skill staging/验证、MCP、Provider fallback 与费用预算。
- [Phase 9 Sub-agent 与多模态计划](superpowers/plans/2026-08-08-phase-9-subagents-and-multimodal.md)：
  depth-1 子任务、权限子集、图片附件、Vision 与可选语音。

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
