# 工程文档索引

这里同时保存 MiniClaw 的当前工程说明、历史阶段快照和后续实施计划。目录按“能力归属”组织，
不等同于 Git 的真实开发顺序；真实交付顺序见[开发与交付时间线](20260809_development-timeline.md)。

## 如何阅读状态

| 标记 | 含义 |
| --- | --- |
| `CURRENT` | 描述当前 `main` 已实现的行为。 |
| `HISTORICAL SNAPSHOT` | 保留某个阶段当时的接口、限制和验证结果；不能代替当前文档。 |
| `IMPLEMENTATION PASS` | 代码与确定性本地门禁通过，不代表真实外部平台验收。 |
| `LIVE VERIFIED` | 对应真实平台场景已有脱敏、可复核证据。 |
| `LIVE PENDING` | 本地实现存在，但真实账号、租户、网络或人工客户端证据尚未闭环。 |
| `PLANNED / NOT IMPLEMENTED` | 只有设计或计划，不能描述为当前能力。 |

> [!IMPORTANT]
> “Phase”表示架构能力阶段；`v0.5.1`、`v0.5.2`、`v0.5.3` 表示 Phase 5 之后的稳定化交付。
> 历史材料中的“Phase 5.1～5.3”保留原文件名，但本索引统一展示为 `v0.5.x Stabilization`，避免与
> 架构 Phase 5（Telegram / Discord）混淆。

## 能力阶段、交付版本与当前证据

| 架构阶段 | 能力边界 | 主要交付 | 当前状态 | 权威入口 |
| --- | --- | --- | --- | --- |
| Phase 0 | 工程骨架、配置、SQLite、Workspace | v0.1.0 前 | `CURRENT` | [Phase 0 计划](../superpowers/plans/2026-08-07-phase-0-foundation.md) |
| Phase 1 | Provider、Context、Agent Loop、Turn | v0.1.0 前 | Core `CURRENT`；旧 CLI 为历史快照 | [Phase 1 设计](../superpowers/specs/2026-08-07-phase-1-cli-agent-design.md) |
| Phase 2 | Tool、Policy、Approval、TUI、安全边界 | v0.1.0～v0.2.0；后续增量见 v0.4.1/v0.5.x | `CURRENT` | [Phase 2 设计](../superpowers/specs/2026-08-07-phase-2-tools-security-design.md) |
| Phase 3 | Memory、Skills、Compaction | v0.3.0、v0.6.0 | Memory Autopilot `IMPLEMENTATION PASS` | [Memory Autopilot 工程实现](phase-5/20260809_memory-autopilot.md) |
| Phase 4 | Feishu Channel | v0.4.0；后续由 v0.5.1～v0.5.3 加固 | `IMPLEMENTATION PASS`；Owner DM verified；15-case pending | [飞书 Channel Core](phase-4/20260808_feishu-channel-core.md) |
| Phase 5 | Telegram / Discord Channel | v0.5.0 | `IMPLEMENTATION PASS / LIVE PENDING` | [Telegram 与 Discord](phase-5/20260808_telegram-discord-channels.md) |
| Phase 6 | Autonomy、Sandbox、Checkpoint、production gate | v0.7.0+ | `IMPLEMENTATION PASS / PRODUCTION SOAK PENDING` | [macOS + 飞书生产验收](phase-6/20260810_macos-feishu-production-acceptance.md) |
| Phase 6.5 | Isolated Browser Agent | v0.6.5 capability record | `IMPLEMENTATION PASS / CONTROLLED LIVE SMOKE PENDING` | [Browser Agent](phase-6/browser-agent.md) |
| Phase 7 | Controlled Evolution | 未发布 | `ENGINEERING PLAN / NOT IMPLEMENTED` | [Controlled Evolution 工程落地方案](phase-7/20260810_controlled-evolution.md) |

当前全仓本地基线是 1005/1005 Python、41/41 TUI TypeScript、14/14 Browser Worker、39/39 Agent、
33/33 Channel、20 轮 660/660 local Channel soak、15/15 Automation，以及 18/18 Browser 和 20 轮
360/360 Browser soak。状态为 **IMPLEMENTATION PASS**；Feishu 为
**TARGETED CALLBACK LIVE VERIFIED / 15-CASE LIVE PENDING**，Telegram、Discord 均为 **LIVE PENDING**，
Docker 为 **LIVE VERIFIED**。Phase 6 production tooling 已实现，状态为 **PRODUCTION SOAK PENDING**；同一 clean
commit 的 Seatbelt 2/2、Feishu 15/15、Automation 10/10、受管 recovery 和连续 24h 尚未共同完成。
Memory 上线前的 v0.5.3 历史基线为 562 Python、30 TypeScript 和 29/29 Agent，不代表当前门禁数字。

> Phase 3 的 legacy Memory/Skills/Compaction 继续兼容；分级自动记忆、跨 Session 检索与周期 Flush 已由
> [Memory Autopilot 工程实现](phase-5/20260809_memory-autopilot.md)替代为当前主路径。

## Phase 0：工程基础

Phase 0 建立 Python 包、配置目录、Workspace、SQLite migration、`init`/`doctor` 和安全文件权限。它早于首个
Eval Release，因此没有独立 `docs/engineering/phase-0/` 快照；设计与逐步施工记录分别见：

- [完整工程设计](../superpowers/specs/2026-08-07-miniclaw-complete-engineering-design.md)
- [Phase 0 Foundation 计划](../superpowers/plans/2026-08-07-phase-0-foundation.md)
- [项目初始化计划](../superpowers/plans/2026-08-07-project-initialization.md)

## Phase 1：Agent Core（旧 CLI 为历史快照）

> Phase 1 的 Provider、Context、Runner 和 Turn 边界仍是当前 Core 的基础，但这些页面记录的是阶段交付时状态。
> `miniclaw chat`、one-shot 和 `input()` REPL 已移除；当前人类入口见[单入口 TUI](phase-2/20260808_single-entry-tui.md)。

| 模块 | 文档 |
| --- | --- |
| 本地环境与凭据 | [20260807_environment.md](phase-1/20260807_environment.md) |
| Provider 稳定契约 | [20260807_provider-contract.md](phase-1/20260807_provider-contract.md) |
| HTTP/SSE Provider | [20260807_openai-compatible-provider.md](phase-1/20260807_openai-compatible-provider.md) |
| ContextBuilder | [20260807_context-builder.md](phase-1/20260807_context-builder.md) |
| AgentRunner | [20260807_agent-runner.md](phase-1/20260807_agent-runner.md) |
| 会话持久化 | [20260807_conversation-storage.md](phase-1/20260807_conversation-storage.md) |
| TurnService | [20260807_turn-service.md](phase-1/20260807_turn-service.md) |
| CLI Chat | [20260807_cli-chat.md](phase-1/20260807_cli-chat.md) |
| 测试与调试 | [testing-and-debugging.md](phase-1/20260807_testing-and-debugging.md) |

## Phase 2：Tool、安全与 TUI

Phase 2 目录按架构责任收纳 Tool、Policy、Approval 和 TUI。P2.3B、pi-tui、Owner Autopilot 等页面交付时间
晚于最初 Phase 2 release；它们的实际顺序以[开发与交付时间线](20260809_development-timeline.md)为准。

| 文档 | 当前定位 |
| --- | --- |
| [Tool Runtime 与 system_info](phase-2/20260807_tool-runtime-and-system-info.md) | Registry、Policy、Executor、ToolRun/Audit 与 Agent Runtime。 |
| [Workspace 只读文件与搜索](phase-2/20260807_workspace-read-tools.md) | `read_file`、`glob`、`grep` 与 Workspace Guard。 |
| [Agent 场景回归](phase-2/20260808_agent-regression-evals.md) | JSONL Schema、29 条 Agent case、baseline/release record。 |
| [安全写边界与原子文件 Tool](phase-2/20260808_filesystem-tools.md) | `write_file`、`edit_file`、有限写根与原子发布。 |
| [参数绑定 Approval](phase-2/20260808_approval-lifecycle.md) | hash、TTL、Owner、续执行、恢复、审计与单次消费。 |
| [单入口 TUI](phase-2/20260808_single-entry-tui.md) | 当前默认 pi-tui、Textual fallback 与入口迁移。 |
| [Python Core + pi-tui Bridge](phase-2/20260808_python-core-pi-tui-bridge.md) | NDJSON v1、Node 子进程、长文本、选择与审批。 |
| [TUI 可观测与分级审批](phase-2/20260808_tui-observability-and-scoped-approvals.md) | 双语、草稿、Token 审计、Session/Always exact scope。 |
| [TUI 回归测试规范](phase-2/20260808_tui-regression-testing.md) | 协议、虚拟终端、权限、审批和跨进程稳定行为。 |
| [exact-argv 命令执行](phase-2/20260808_command-execution.md) | `run_command`、最小环境、超时、输出上限与 Policy。 |
| [Personal Machine 权限](phase-2/20260808_personal-machine-permissions.md) | 多根读写、敏感硬拒绝、用户 CLI 发现与真实只读 lark-cli smoke。 |
| [Autopilot 权限](phase-2/20260808_autopilot-permissions-and-approval-ui.md) | 四档权限、Owner 私聊信任、脱敏审计和紧凑审批 UI。 |
| [Pinned HTTPS 与 SSRF](phase-2/20260808_https-get-and-ssrf.md) | HTTPS、DNS 公网校验、固定 IP/TLS hostname 与重定向重验。 |
| [回归、恢复与调试](phase-2/20260808_testing-and-debugging.md) | 当前全仓 Gate 与 Phase 2 历史 release 证据的分层说明。 |
| [Approvals CLI（历史迁移）](phase-2/20260808_cli-approvals.md) | 已移除旧入口的历史说明。 |

## Phase 3：Memory v1、Skills 与上下文预算

| 文档 | 当前实现 | 明确不包含 |
| --- | --- | --- |
| [Memory、Skills 与上下文压缩](phase-3/20260808_memory-skills-compaction.md) | legacy Markdown Memory、Skill 惰性正文、persistent compaction。 | Memory Autopilot 的当前语义，见下行。 |
| [Memory Autopilot A～E](phase-5/20260809_memory-autopilot.md) | Owner Disclosure、durable Flush、Markdown Truth、FTS5/CJK、治理、对账、迁移与 versioned gate。 | Phase 7 的反思和自我进化。 |

Memory Autopilot A～E 是 Phase 3 之后的基础重构，状态为 `IMPLEMENTATION PASS`。

## Phase 4：Feishu Channel

| 文档 | 当前定位 |
| --- | --- |
| [飞书 Channel 与 Gateway 概览](phase-4/20260808_feishu-channel-core.md) | 模块地图、数据流、Admission、状态机和分层证据。 |
| [飞书生产 Channel 工程落地](phase-4/20260808_feishu-channel.md) | WebSocket SDK、Adapter、Inbox/Outbox、Worker、Delivery、卡片和 Approval。 |
| [运行、测试与故障排查](phase-4/20260808_testing-and-operations.md) | Doctor、离线回归、live smoke、重启/断线/审批验收。 |
| [完成性审计](phase-4/20260808_completion-audit.md) | Requirement → code → automated/live evidence；不把本地 PASS 冒充真实平台 PASS。 |

准确状态是 **IMPLEMENTATION PASS / TARGETED CALLBACK LIVE VERIFIED / 15-CASE LIVE PENDING**。真实 Bot、Scope、
WebSocket ready 与 Owner 私聊 Delivery 已验证；完整 15-case、长期 soak 和全平台 production verified 尚未成立。

## Phase 5：Telegram 与 Discord

| 文档 | 当前定位 |
| --- | --- |
| [Telegram 与 Discord 工程落地](phase-5/20260808_telegram-discord-channels.md) | 单 Runtime/多 Pipeline、官方 Transport、身份、Approval、分片和故障隔离。 |
| [测试与 Live Acceptance](phase-5/20260808_testing-and-live-acceptance.md) | 当前本地 Gate 与三平台真实 evidence 边界。 |
| [故障排查手册](phase-5/20260808_troubleshooting.md) | SDK、限流、权限、重连、Approval、恢复与 Secret scan。 |
| [完成性审计](phase-5/20260808_completion-audit.md) | Phase 5 requirement → code → automated/live evidence。 |
| [Memory Autopilot A～E](phase-5/20260809_memory-autopilot.md) | 跨渠道 Owner Memory、恢复矩阵、治理、迁移和运维入口。 |

架构 Phase 5 与 Memory Autopilot A～E 已达到 **IMPLEMENTATION PASS**；Telegram/Discord 真实账号
验收仍为 **LIVE PENDING**。

## Phase 6：Autonomy、Sandbox 与 Checkpoint

| 文档 | 内容 | 当前状态 |
| --- | --- | --- |
| [Autonomy Runtime](phase-6/20260809_autonomy-runtime.md) | Task/Run Ledger、Scheduler、Runner、Heartbeat、Budget、E-stop、Approval continuation 与主动 Delivery。 | **IMPLEMENTATION PASS** |
| [Sandbox 与 Checkpoint](phase-6/20260809_sandbox-and-checkpoint.md) | immutable Plan、Approval hash、Docker/Seatbelt、Checkpoint CAS 与 Rollback。 | **IMPLEMENTATION PASS / DOCKER LIVE VERIFIED / SEATBELT LIVE PENDING** |
| [macOS + 飞书生产级验收](phase-6/20260810_macos-feishu-production-acceptance.md) | managed Python/LaunchAgent、Seatbelt 2-case、飞书 25-case、recovery、exact 24h、Evidence 与故障处置。 | **IMPLEMENTATION PASS / PRODUCTION SOAK PENDING** |
| [v0.7.0 Release Record](../evals/releases/v0.7.0.md) | 798/798、35/35、39/39、33/33、660/660 与 Automation 15/15 的复现边界。 | CURRENT |
| [已确认设计](../superpowers/specs/2026-08-09-phase-6-autonomy-sandbox-design.md) | 产品语义、安全边界与非目标。 | APPROVED DESIGN / IMPLEMENTED |
| [TDD 实施计划](../superpowers/plans/2026-08-09-phase-6-autonomy-sandbox.md) | 逐项 RED→GREEN 文件、接口与门禁。 | IMPLEMENTED；保留施工记录 |
| [Browser Agent](phase-6/browser-agent.md) | 专用 Profile、Worker 协议、snapshot/ref、Policy/Approval、Artifact 与恢复。 | **IMPLEMENTATION PASS / CONTROLLED LIVE SMOKE PENDING** |
| [v0.6.5 Browser Record](../evals/releases/v0.6.5.md) | 925/925、14/14 Worker、18/18 与 360/360 Browser 的复现边界。 | CURRENT |

Automation、Heartbeat 与 Browser 默认关闭；Heartbeat 当前没有 Owner IM route，Checkpoint 只覆盖主 Workspace，
Rollback 没有 CLI/TUI。生产 Gate 的 monitor/orchestrator 已实现，但没有连续 24h verified aggregate；Browser Agent
已完成本地实现，受控公网 live smoke 仍为 pending。

## Desktop W0/W1：通用 Agent 工作台开发版

`desktop/` 已实现 Electron + React 浅色四界面，并通过固定 Preload API 复用 Python Bridge。当前闭环包括首页、
单 Agent 任务时间线、Tool/审批/取消、最近任务与 interrupted 状态、Automation 只读列表、四档 Permission Mode
和用户触发的 Workspace 重启。Renderer 保持 sandbox、无 Node integration，不直读 SQLite、Secret 或 Workspace。

当前状态为 **W0/W1 DEVELOPMENT BUILD / AUTOMATED GATE PASS / ELECTRON MANUAL PENDING**。真实 Python Bridge 的
Desktop hello 和隔离 Electron 进程 smoke 已通过；安装包、签名、鼠标/键盘视觉验收、真实 Provider LIVE smoke、
Artifact 和 Sub-agent 未完成。

下一条 Desktop 主线已经确认：以 LobsterAI Cowork 为主体，首屏直接提供 Composer、附件、模型、Workspace 和
Agent 选择；吸收 OpenAgents 的 Agent/Thread/Participant/Shared Artifact 信息架构，底层继续只使用 MiniClaw
Core。当前为 `TARGET CONFIRMED / D1-D5 IMPLEMENTATION PENDING`。

| 文档或代码 | 当前定位 |
| --- | --- |
| [通用桌面 Agent 工作台设计](../architecture/20260809_通用桌面Agent工作台设计.md) | W0/W1 已实现历史基线。 |
| [桌面多 Agent 开发需求](../product/20260810_桌面多Agent工作台开发需求.md) | D1～D5 产品范围与完成标准。 |
| [LobsterAI-first 桌面设计](../architecture/20260810_LobsterAI-first桌面多Agent设计.md) | 新信息架构、Core 边界与安全设计。 |
| [D1～D5 分 Phase 落地](desktop/20260810_桌面多Agent分Phase落地.md) | 文档先行、TDD 顺序和退出条件。 |
| [D1 打开即聊设计](../superpowers/specs/2026-08-10-desktop-d1-conversation-shell-design.md) | 首屏 Composer、合并左栏、浅色视觉和状态迁移。 |
| [D1 跟进修复：拖拽/命名/Markdown](desktop/20260810_D1跟进修复-拖拽命名与Markdown渲染.md) | 窗口拖拽区域、对话命名对齐、Markdown 渲染。 |
| [D2 附件/模型/Workspace/Agent 控件设计](../superpowers/specs/2026-08-10-desktop-d2-composer-controls-design.md) | Composer 四控件、Bridge 协议新增、ArtifactStore 附件 admission。`DRAFT FOR REVIEW`。 |
| [W0/W1 实施计划](../superpowers/plans/2026-08-09-desktop-workbench-w0-w1.md) | RED→GREEN 施工与最终门禁。 |
| `desktop/` | Electron Main、固定 Preload、React 四界面和 Desktop 测试。 |

## v0.5.x Stabilization：真实运行与证据收口

这些工作发生在架构 Phase 5 之后，主要加固 Feishu 和 Live Gate，不是新的架构 Phase。

| 交付 | 文档 | 当前状态 |
| --- | --- | --- |
| v0.5.1 | [真实飞书 Bot 与 Live E2E](phase-5/20260808_feishu-live-e2e.md) | Runner implemented；Owner DM verified；15/15 pending。 |
| v0.5.2 | [飞书 Gateway 与 macOS 常驻](phase-5/20260808_feishu-gateway-runtime-and-macos-service.md)、[单卡片与 lark-cli](phase-5/20260809_feishu-single-card-and-lark-cli.md) | Core hardening implemented；严格 live evidence pending。 |
| v0.5.3 Core | [Live Gate 设计](../superpowers/specs/2026-08-09-phase-5-3-feishu-discord-live-gate-design.md)、[实施计划](../superpowers/plans/2026-08-09-phase-5-3-feishu-discord-live-gate.md)、[Release Record](../evals/releases/v0.5.3.md) | SDK 脱敏、lease/provenance、受管 runner 已实现；Feishu/Discord 15/15 pending。 |
| v0.6.0 | [Memory 工程实现](phase-5/20260809_memory-autopilot.md)、[Release Record](../evals/releases/v0.6.0.md) | Memory Autopilot A～E 已实现；真实 IM 结论沿用各平台 gate。 |
| v0.7.0 | [Autonomy Runtime](phase-6/20260809_autonomy-runtime.md)、[Sandbox 与 Checkpoint](phase-6/20260809_sandbox-and-checkpoint.md)、[Release Record](../evals/releases/v0.7.0.md) | Phase 6 本地门禁完成；Live containment pending。 |
| v0.6.5 capability record | [Browser Agent](phase-6/browser-agent.md)、[Release Record](../evals/releases/v0.6.5.md) | 实际晚于 v0.7.0 合并；Browser 本地门禁完成，controlled live smoke pending。 |

## 后续路线（规划）

| 文档 | 规划范围 | 当前事实 |
| --- | --- | --- |
| [OpenClaw / Hermes Gap](../architecture/20260808_OpenClaw-Hermes能力Gap与演进路线.md) | Phase 5.3 收口后到 Phase 9 的优先级和非目标。 | 路线已确认；未交付部分不能写成当前能力。 |
| [LobsterAI-first 桌面多 Agent 设计](../architecture/20260810_LobsterAI-first桌面多Agent设计.md) | 打开即聊、Composer、Artifact 和 depth-1 Multi-Agent。 | 目标已确认；D1～D5 pending。 |
| [能力对齐工程总方案](20260808_openclaw-hermes-alignment-engineering-roadmap.md) | Service、Automation、Sandbox、Browser、Evolution、Memory、Skills、MCP、Provider、Sub-agent、Media。 | `APPROVED ROADMAP`。 |
| [Phase 6.5 计划](../superpowers/plans/2026-08-08-phase-6-5-browser-agent.md) | Browser Profile、snapshot/ref、Policy、Artifact。 | IMPLEMENTED；保留施工记录。 |
| [Phase 7 工程落地方案](phase-7/20260810_controlled-evolution.md) | Feedback、受限 Prompt/Skill/Memory Proposal、Eval、Owner Approval、Apply/Rollback。 | `ENGINEERING PLAN / NOT IMPLEMENTED`；Phase 6 生产验收后施工。 |
| [Phase 7 计划](../superpowers/plans/2026-08-08-phase-7-controlled-evolution-and-memory-v2.md) | Feedback、Proposal、Eval、Apply/Rollback。 | 依赖 Memory A～E 与 Phase 6。 |
| [Phase 8 计划](../superpowers/plans/2026-08-08-phase-8-skills-mcp-provider-resilience.md) | Skill trust、MCP、Provider fallback、预算。 | 依赖 Phase 7。 |
| [Phase 9 计划](../superpowers/plans/2026-08-08-phase-9-subagents-and-multimodal.md) | depth-1 Sub-agent、附件、Vision、可选语音。 | 依赖 Phase 6 与 Phase 8。 |
