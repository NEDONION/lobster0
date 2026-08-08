# 工程文档索引

这里记录 MiniClaw 已实现模块的工程说明。设计文档描述目标；工程文档只描述已经进入代码与验证的行为。

## OpenClaw / Hermes 能力对齐（规划）

> 以下文档是经过确认的后续工程路线，状态为 **PLANNED / NOT IMPLEMENTED**。它们不改变本页下方 Phase 2-5
> 的真实完成状态，也不能作为 production evidence。

| 文档 | 规划范围 | 当前事实 |
| --- | --- | --- |
| [能力 Gap 与演进路线](../architecture/20260808_OpenClaw-Hermes能力Gap与演进路线.md) | 对照 OpenClaw、Hermes、nanobot、ZeroClaw、RayClaw，确定 Phase 5.3→9 的优先级与非目标。 | Gap 已确认，代码未实现。 |
| [能力对齐工程落地总方案](openclaw-hermes-alignment-engineering-roadmap.md) | Service、Automation、Sandbox、Browser、Evolution、Memory v2、Skills、MCP、Provider、Sub-agent、Media 的模块/数据/测试设计。 | 工程边界已确认，等待逐 Phase TDD。 |
| [Memory Autopilot 能力 Gap](../architecture/20260808_Memory-Autopilot能力Gap与重构架构.md) | 跨渠道 Owner Memory、L0→L3、自动 Flush、晋升、冲突、遗忘和隐私。 | 设计已确认，代码未实现。 |
| [Memory Autopilot 技术选型](memory-autopilot-best-practices-and-technology-selection.md) | Markdown Truth + SQLite Control Plane、接口、表、Crash Recovery、FTS5/CJK、测试矩阵。 | 工程方案已确认，代码未实现。 |
| [Memory Autopilot 正式设计](../superpowers/specs/2026-08-08-memory-autopilot-design.md) | 产品语义、架构决策、失败策略、迁移和完成定义。 | APPROVED DESIGN / NOT IMPLEMENTED。 |
| [Memory Autopilot A～E 实施计划](../superpowers/plans/2026-08-09-memory-autopilot.md) | Identity/Disclosure、Flush、FTS5、治理、Reconcile 的逐任务 RED→GREEN 计划。 | 计划已落地，代码未实现。 |
| [Phase 5.2 实施计划](../superpowers/plans/2026-08-08-phase-5-2-production-hardening.md) | 系统服务、health、Docker、Feishu 15/15、24h soak。 | Core hardening 已落地，严格 live evidence 未闭环。 |
| [Phase 5.3 Live Gate 设计](../superpowers/specs/2026-08-09-phase-5-3-feishu-discord-live-gate-design.md) | SDK 日志、Gateway lease、受管进程、Feishu/Discord 真实证据边界。 | APPROVED DESIGN / NOT IMPLEMENTED。 |
| [Phase 5.3 实施计划](../superpowers/plans/2026-08-09-phase-5-3-feishu-discord-live-gate.md) | 逐任务修复、15/15 Live Gate、脱敏 evidence 和 release record。 | 当前下一实施计划。 |
| [Phase 6 实施计划](../superpowers/plans/2026-08-08-phase-6-autonomy-runtime-and-sandbox.md) | Scheduler、Task Ledger、Heartbeat、Delivery、Budget、Sandbox、Checkpoint。 | 依赖 Memory A～E。 |
| [Phase 6.5 实施计划](../superpowers/plans/2026-08-08-phase-6-5-browser-agent.md) | 独立浏览器 Profile、snapshot/ref、Browser Policy、Artifact。 | 依赖 Phase 6 Sandbox。 |
| [Phase 7 实施计划](../superpowers/plans/2026-08-08-phase-7-controlled-evolution-and-memory-v2.md) | Feedback、Memory Reflection、Proposal、Eval、Apply/Rollback。 | 依赖 Memory A～E 与 Phase 6 Task/Eval 安全边界。 |
| [Phase 8 实施计划](../superpowers/plans/2026-08-08-phase-8-skills-mcp-provider-resilience.md) | Skill 安装信任、MCP、Provider fallback、费用预算。 | 依赖 Phase 7 版本与审批账本。 |
| [Phase 9 实施计划](../superpowers/plans/2026-08-08-phase-9-subagents-and-multimodal.md) | depth-1 Sub-agent、附件、Vision、可选语音。 | 依赖 Phase 6 Task/Sandbox 与 Phase 8 Provider。 |

## Phase 2：Tool 与安全

| 文档 | 已验证内容 | 不包含 |
| --- | --- | --- |
| [Phase 2.1A：Tool Runtime 与 system_info](phase-2/tool-runtime-and-system-info.md) | Tool Contract、Registry、Policy、Executor、ToolRun/Audit、`system_info` 与 Agent Runtime。 | 文件读写、审批、Shell、飞书。 |
| [Phase 2.1B：Workspace 只读文件与搜索](phase-2/workspace-read-tools.md) | `read_file`、`glob`、`grep`、Workspace Guard、离线 Agent/CLI 装配验证。 | 真实 DeepSeek 文件 smoke、`write_file`、Shell、审批、飞书。 |
| [Phase 2.1C：Agent 场景回归与 Benchmark 基线](phase-2/agent-regression-evals.md) | JSONL Schema、29 条 Claw-like query、真实离线 runner、`miniclaw eval`、baseline/release record。 | 完整 live DeepSeek runner、report/compare、飞书 E2E、自动改 Prompt/Skill。 |
| [Phase 2.2A：安全写边界与原子文件 Tool](phase-2/filesystem-tools.md) | 严格 Tools 配置、`resolve_write`、`write_file`、`edit_file`、原子发布与失败保护。 | Runtime/TUI 接线、Shell、HTTP、飞书。 |
| [Phase 2.2：参数绑定 Approval 与续执行](phase-2/approval-lifecycle.md) | canonical hash、waiting/child Turn、Owner/TTL、approve/deny、重启恢复、单次消费与审计。 | Shell、HTTP、飞书卡片。 |
| [Phase 2：单入口 TUI](phase-2/single-entry-tui.md) | pi-tui 默认入口与 Textual onboarding/fallback 的迁移关系。 | 飞书、历史虚拟化。 |
| [Python Core + pi-tui Bridge](phase-2/python-core-pi-tui-bridge.md) | NDJSON v1、Node 子进程、长文本/选择/审批、安装调试和跨进程回归。 | 发布包内置构建产物、删除 fallback。 |
| [TUI 可观测、长文本与分级审批](phase-2/tui-observability-and-scoped-approvals.md) | 默认中文/可切英文、草稿恢复、真实 Token 审计、Session/Always exact scope。 | 飞书 Channel、规则管理 UI。 |
| [TUI 回归测试规范](phase-2/tui-regression-testing.md) | 33 个稳定行为 ID，覆盖协议、虚拟终端、权限、长审批、跨进程、Textual fallback 和发布门禁。 | live LLM 硬断言。 |
| [Phase 2.3A：exact-argv 命令执行](phase-2/command-execution.md) | `run_command`、硬禁止、精确 argv 规则、最小环境、进程组超时、输出上限与 TUI 审批。 | 任意 Shell、用户 CLI 发现、OS sandbox。 |
| [Phase 2.3B：Personal Machine 权限与 CLI 发现](phase-2/personal-machine-permissions.md) | Workspace/Personal Profile、多根读写、敏感路径硬拒绝、NVM/uv/pnpm CLI 发现、最小子进程环境、Doctor 与四条回归场景。 | 全盘任意写、密码库读取、Shell rc、真实飞书认证/Scope。 |
| [Autopilot 权限与紧凑审批 UI](phase-2/autopilot-permissions-and-approval-ui.md) | 四档权限、Owner 私聊信任、运行时切换、脱敏审计、84×18 可滚动审批框。 | 绕过硬拒绝、群聊 Autopilot、模式自动写回配置。 |
| [Phase 2.4：Pinned HTTPS 与 SSRF 防护](phase-2/https-get-and-ssrf.md) | `http_get`、URL/DNS 公网校验、固定 IP/TLS hostname、每跳重验、文本预算、审批与 crash recovery。 | 浏览器、认证 Header、任意方法、企业代理。 |
| [Phase 2：回归、恢复与调试](phase-2/testing-and-debugging.md) | 当前 531 Python + 30 TypeScript tests、29 Agent + 32 Channel 场景、恢复、Doctor 和发布手册。 | 三平台真实 E2E、自动 Prompt/Skill 演进。 |
| [Phase 2.2：Approvals CLI（历史迁移）](phase-2/cli-approvals.md) | 记录旧入口为何被单入口 TUI 取代。 | 当前可执行命令。 |

## Phase 3：Memory、Skills 与上下文预算

> Phase 3 下表是当前实现。分级自动记忆、跨 Session 检索和周期 Markdown Flush 属于
> [Memory Autopilot 规划](memory-autopilot-best-practices-and-technology-selection.md)，目前尚未进入代码。

| 文档 | 已验证内容 | 不包含 |
| --- | --- | --- |
| [Memory、Skills 与上下文压缩](phase-3/memory-skills-compaction.md) | Markdown Memory、审批写入、凭据过滤、Skill metadata/惰性正文、persistent compaction、runtime snapshot。 | 向量库、Skill 代码执行、自动修改 Prompt/Skill、飞书 Channel。 |

## Phase 4：飞书生产 Channel

| 文档 | 已验证内容 | 不包含 |
| --- | --- | --- |
| [飞书 Channel 与 Gateway 概览](phase-4/feishu-channel-core.md) | 模块地图、数据流、Admission、状态机和当前完成度。 | 真实飞书账号 E2E、部署/soak。 |
| [飞书生产 Channel 工程落地](phase-4/feishu-channel.md) | official WebSocket SDK、严格 Adapter、schema v2 Inbox/Outbox、Worker、Delivery、Typing/进度卡、Approval 与 Gateway。 | Telegram/Discord、多用户、文件消息、真实账号验收。 |
| [运行、测试与故障排查](phase-4/testing-and-operations.md) | 15 项 Doctor、412+27 tests、28+12 回归、local soak、live smoke、重启/断线/审批验收规范。 | 未配置凭据时的真实平台结论。 |
| [完成性审计与证据矩阵](phase-4/completion-audit.md) | Section 4/22 逐项映射到代码、自动化测试和 live gate，明确本地 PASS 与真实平台 PENDING。 | 没有凭据时伪造 production verified。 |

功能主线已完成 Phase 5 implementation（531 Python tests + 30 TypeScript tests + 29/29 Agent + 32/32 Channel）。
P2.3B 已完成 Personal Profile 与本机用户 CLI 的确定性发现。专用飞书 App、Bot、认证、Scope、Owner allowlist、
WebSocket ready 与两条 Owner 私聊回复已经得到真实平台证据；完整 15-case suite、长期 soak 和 Telegram/Discord
仍待验收。准确状态是“Owner DM delivery verified”，不是“全平台 production verified”。

## Phase 5：Telegram 与 Discord

| 文档 | 已验证内容 | 仍待外部证据 |
| --- | --- | --- |
| [Telegram 与 Discord 工程落地说明](phase-5/telegram-discord-channels.md) | 单 Runtime/多 Pipeline、GatewaySupervisor、long polling、Discord Gateway、身份/会话、Typing/Preview、Approval、分片和故障隔离。 | 两个平台真实账号验收。 |
| [真实飞书 Bot 与 Live E2E](phase-5/feishu-live-e2e.md) | App/Bot 已配置、同应用 Owner discovery、15 条版本化场景、Gateway Runner、只读 evidence、Secret scan。 | 完成剩余 15/15。 |
| [飞书 Gateway 运行时与 macOS 常驻](phase-5/feishu-gateway-runtime-and-macos-service.md) | SDK event loop、`connect_until_ready`、`text/post`、Typing、Owner DM evidence、launchd/VPS。 | 自动安装系统服务、24×7 soak。 |
| [飞书单卡片与 lark-cli Skill](phase-5/feishu-single-card-and-lark-cli.md) | 12px completed card、超限后缀回复卡片、restart UUID 恢复、Approval 单卡片、direct lark-cli Skill。 | 修复后真实客户端人工确认、私有文档 live Tool Loop。 |
| [测试与 live acceptance](phase-5/testing-and-live-acceptance.md) | 531 Python、30 TypeScript、29 Agent、32 Channel、640 soak、三平台 live gate。 | 三个平台真实 evidence。 |
| [故障排查手册](phase-5/troubleshooting.md) | SDK/Token、Telegram 409、Discord intents/403、限流、degraded、Approval、恢复和 Secret scan。 | 平台侧实际权限工单。 |
| [完成性审计](phase-5/completion-audit.md) | requirement → code → automated/live evidence 矩阵。 | production verified exit gate。 |

Phase 5 当前是 **IMPLEMENTATION PASS**：531 Python tests、30/30 TypeScript、29/29 Agent、32/32 Channel 与
640/640 local soak。Feishu 是 **FEISHU OWNER-DM DELIVERY VERIFIED / 15-CASE LIVE PENDING**；Telegram/Discord 是
**LIVE PENDING**。详细权威规格见
[Phase 5 Telegram/Discord 工程设计](../superpowers/specs/2026-08-08-phase-5-telegram-discord-design.md)，逐项开发步骤见
[Phase 5 Telegram/Discord Implementation Plan](../superpowers/plans/2026-08-08-phase-5-telegram-discord.md)。

## Phase 1：CLI Agent 闭环（历史实现）

> `miniclaw chat` 与 line REPL 已移除。下表用于理解演进历史；当前入口以 P2.2B 文档为准。

| 模块 | 文档 |
| --- | --- |
| 本地环境与凭据 | [environment.md](phase-1/environment.md) |
| Provider 稳定契约 | [provider-contract.md](phase-1/provider-contract.md) |
| HTTP/SSE Provider | [openai-compatible-provider.md](phase-1/openai-compatible-provider.md) |
| ContextBuilder | [context-builder.md](phase-1/context-builder.md) |
| AgentRunner | [agent-runner.md](phase-1/agent-runner.md) |
| 会话持久化 | [conversation-storage.md](phase-1/conversation-storage.md) |
| TurnService | [turn-service.md](phase-1/turn-service.md) |
| CLI Chat | [cli-chat.md](phase-1/cli-chat.md) |
| 测试与调试 | [testing-and-debugging.md](phase-1/testing-and-debugging.md) |
