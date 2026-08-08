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

当前全仓本地基线是 651/651 Python、35/35 TypeScript、39/39 Agent、32/32 Channel 和 20 轮
640/640 local soak。Feishu 为 **OWNER-DM DELIVERY VERIFIED / 15-CASE LIVE PENDING**；Telegram、Discord
均为 **LIVE PENDING**。v0.5.3 的 SDK 日志脱敏、Gateway lease/provenance、受管 Live Runner 与异常
Tool 历史恢复以及 Memory Autopilot A～E 已经合并；真实 Feishu/Discord 15/15 仍未完成。
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

准确状态是 **IMPLEMENTATION PASS / OWNER-DM DELIVERY VERIFIED / 15-CASE LIVE PENDING**。真实 Bot、Scope、
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

## v0.5.x Stabilization：真实运行与证据收口

这些工作发生在架构 Phase 5 之后，主要加固 Feishu 和 Live Gate，不是新的架构 Phase。

| 交付 | 文档 | 当前状态 |
| --- | --- | --- |
| v0.5.1 | [真实飞书 Bot 与 Live E2E](phase-5/20260808_feishu-live-e2e.md) | Runner implemented；Owner DM verified；15/15 pending。 |
| v0.5.2 | [飞书 Gateway 与 macOS 常驻](phase-5/20260808_feishu-gateway-runtime-and-macos-service.md)、[单卡片与 lark-cli](phase-5/20260809_feishu-single-card-and-lark-cli.md) | Core hardening implemented；严格 live evidence pending。 |
| v0.5.3 Core | [Live Gate 设计](../superpowers/specs/2026-08-09-phase-5-3-feishu-discord-live-gate-design.md)、[实施计划](../superpowers/plans/2026-08-09-phase-5-3-feishu-discord-live-gate.md)、[Release Record](../evals/releases/v0.5.3.md) | SDK 脱敏、lease/provenance、受管 runner 已实现；Feishu/Discord 15/15 pending。 |
| v0.6.0 | [Memory 工程实现](phase-5/20260809_memory-autopilot.md)、[Release Record](../evals/releases/v0.6.0.md) | Memory Autopilot A～E 已实现；真实 IM 结论沿用各平台 gate。 |

## 后续路线（规划）

| 文档 | 规划范围 | 当前事实 |
| --- | --- | --- |
| [OpenClaw / Hermes Gap](../architecture/20260808_OpenClaw-Hermes能力Gap与演进路线.md) | Phase 5.3 收口后到 Phase 9 的优先级和非目标。 | 路线已确认；未交付部分不能写成当前能力。 |
| [能力对齐工程总方案](20260808_openclaw-hermes-alignment-engineering-roadmap.md) | Service、Automation、Sandbox、Browser、Evolution、Memory、Skills、MCP、Provider、Sub-agent、Media。 | `APPROVED ROADMAP`。 |
| [Phase 6 计划](../superpowers/plans/2026-08-08-phase-6-autonomy-runtime-and-sandbox.md) | Scheduler、Task Ledger、Heartbeat、Budget、Sandbox、Checkpoint。 | 依赖 Memory A～E。 |
| [Phase 6.5 计划](../superpowers/plans/2026-08-08-phase-6-5-browser-agent.md) | Browser Profile、snapshot/ref、Policy、Artifact。 | 依赖 Phase 6 Sandbox。 |
| [Phase 7 计划](../superpowers/plans/2026-08-08-phase-7-controlled-evolution-and-memory-v2.md) | Feedback、Proposal、Eval、Apply/Rollback。 | 依赖 Memory A～E 与 Phase 6。 |
| [Phase 8 计划](../superpowers/plans/2026-08-08-phase-8-skills-mcp-provider-resilience.md) | Skill trust、MCP、Provider fallback、预算。 | 依赖 Phase 7。 |
| [Phase 9 计划](../superpowers/plans/2026-08-08-phase-9-subagents-and-multimodal.md) | depth-1 Sub-agent、附件、Vision、可选语音。 | 依赖 Phase 6 与 Phase 8。 |
