# 工程文档索引

这里记录 MiniClaw 已实现模块的工程说明。设计文档描述目标；工程文档只描述已经进入代码与验证的行为。

## Phase 2：Tool 与安全

| 文档 | 已验证内容 | 不包含 |
| --- | --- | --- |
| [Phase 2.1A：Tool Runtime 与 system_info](phase-2/tool-runtime-and-system-info.md) | Tool Contract、Registry、Policy、Executor、ToolRun/Audit、`system_info` 与 Agent Runtime。 | 文件读写、审批、Shell、飞书。 |
| [Phase 2.1B：Workspace 只读文件与搜索](phase-2/workspace-read-tools.md) | `read_file`、`glob`、`grep`、Workspace Guard、离线 Agent/CLI 装配验证。 | 真实 DeepSeek 文件 smoke、`write_file`、Shell、审批、飞书。 |
| [Phase 2.1C：Agent 场景回归与 Benchmark 基线](phase-2/agent-regression-evals.md) | JSONL Schema、28 条 Claw-like query、真实离线 runner、`miniclaw eval`、baseline/release record。 | 完整 live DeepSeek runner、report/compare、飞书 E2E、自动改 Prompt/Skill。 |
| [Phase 2.2A：安全写边界与原子文件 Tool](phase-2/filesystem-tools.md) | 严格 Tools 配置、`resolve_write`、`write_file`、`edit_file`、原子发布与失败保护。 | Runtime/TUI 接线、Shell、HTTP、飞书。 |
| [Phase 2.2：参数绑定 Approval 与续执行](phase-2/approval-lifecycle.md) | canonical hash、waiting/child Turn、Owner/TTL、approve/deny、重启恢复、单次消费与审计。 | Shell、HTTP、飞书卡片。 |
| [Phase 2：单入口 TUI](phase-2/single-entry-tui.md) | pi-tui 默认入口与 Textual onboarding/fallback 的迁移关系。 | 飞书、历史虚拟化。 |
| [Python Core + pi-tui Bridge](phase-2/python-core-pi-tui-bridge.md) | NDJSON v1、Node 子进程、长文本/选择/审批、安装调试和跨进程回归。 | 发布包内置构建产物、删除 fallback。 |
| [TUI 可观测、长文本与分级审批](phase-2/tui-observability-and-scoped-approvals.md) | 默认中文/可切英文、草稿恢复、真实 Token 审计、Session/Always exact scope。 | 飞书 Channel、规则管理 UI。 |
| [TUI 回归测试规范](phase-2/tui-regression-testing.md) | 25 个 pi-tui 协议/虚拟终端/跨进程用例、Textual fallback 和发布门禁。 | live LLM 硬断言。 |
| [Phase 2.3A：exact-argv 命令执行](phase-2/command-execution.md) | `run_command`、硬禁止、精确 argv 规则、最小环境、进程组超时、输出上限与 TUI 审批。 | 任意 Shell、用户 CLI 发现、OS sandbox。 |
| [Phase 2.3B：Personal Machine 权限与 CLI 发现](phase-2/personal-machine-permissions.md) | Workspace/Personal Profile、多根读写、敏感路径硬拒绝、NVM/uv/pnpm CLI 发现、最小子进程环境、Doctor 与四条回归场景。 | 全盘任意写、密码库读取、Shell rc、真实飞书认证/Scope。 |
| [Phase 2.4：Pinned HTTPS 与 SSRF 防护](phase-2/https-get-and-ssrf.md) | `http_get`、URL/DNS 公网校验、固定 IP/TLS hostname、每跳重验、文本预算、审批与 crash recovery。 | 浏览器、认证 Header、任意方法、企业代理。 |
| [Phase 2：回归、恢复与调试](phase-2/testing-and-debugging.md) | 当前 483 Python + 27 TypeScript tests、28 Agent + 32 Channel 场景、恢复、Doctor 和发布手册。 | 三平台真实 E2E、自动 Prompt/Skill 演进。 |
| [Phase 2.2：Approvals CLI（历史迁移）](phase-2/cli-approvals.md) | 记录旧入口为何被单入口 TUI 取代。 | 当前可执行命令。 |

## Phase 3：Memory、Skills 与上下文预算

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

功能主线已完成 Phase 5 implementation（483 Python + 27 TypeScript tests + 28/28 Agent + 32/32 Channel）。
P2.3B 已完成 Personal Profile 与本机用户 CLI 的确定性发现；`lark-cli --version` 只验证发现/启动，不代表认证。
本机尚未配置飞书 App ID/App Secret，因此 production live acceptance、`lark-cli auth status` 与当前版本 live
DeepSeek release eval 仍是独立待办。准确状态是“代码与离线门禁完成”，不是“真实飞书已验证”。

## Phase 5：Telegram 与 Discord

| 文档 | 已验证内容 | 仍待外部证据 |
| --- | --- | --- |
| [Telegram 与 Discord 工程落地说明](phase-5/telegram-discord-channels.md) | 单 Runtime/多 Pipeline、GatewaySupervisor、long polling、Discord Gateway、身份/会话、Typing/Preview、Approval、分片和故障隔离。 | 两个平台真实账号验收。 |
| [测试与 live acceptance](phase-5/testing-and-live-acceptance.md) | 483 Python、27 TypeScript、28 Agent、32 Channel、640 soak、15 项安全 live harness。 | Telegram/Discord 15/15 evidence。 |
| [故障排查手册](phase-5/troubleshooting.md) | SDK/Token、Telegram 409、Discord intents/403、限流、degraded、Approval、恢复和 Secret scan。 | 平台侧实际权限工单。 |
| [完成性审计](phase-5/completion-audit.md) | requirement → code → automated/live evidence 矩阵。 | production verified exit gate。 |

Phase 5 当前是 **IMPLEMENTATION PASS / LIVE PENDING**：483/483 Python tests、27/27 TypeScript、
28/28 Agent、32/32 Channel 与 640/640 local soak 已通过。详细权威规格见
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
