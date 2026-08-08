# 工程文档索引

这里记录 MiniClaw 已实现模块的工程说明。设计文档描述目标；工程文档只描述已经进入代码与验证的行为。

## Phase 2：Tool 与安全

| 文档 | 已验证内容 | 不包含 |
| --- | --- | --- |
| [Phase 2.1A：Tool Runtime 与 system_info](phase-2/tool-runtime-and-system-info.md) | Tool Contract、Registry、Policy、Executor、ToolRun/Audit、`system_info` 与 Agent Runtime。 | 文件读写、审批、Shell、飞书。 |
| [Phase 2.1B：Workspace 只读文件与搜索](phase-2/workspace-read-tools.md) | `read_file`、`glob`、`grep`、Workspace Guard、离线 Agent/CLI 装配验证。 | 真实 DeepSeek 文件 smoke、`write_file`、Shell、审批、飞书。 |
| [Phase 2.1C：Agent 场景回归与 Benchmark 基线](phase-2/agent-regression-evals.md) | JSONL Schema、24 条 Claw-like query、真实离线 runner、`miniclaw eval`、baseline/release record。 | 完整 live DeepSeek runner、report/compare、飞书 E2E、自动改 Prompt/Skill。 |
| [Phase 2.2A：安全写边界与原子文件 Tool](phase-2/filesystem-tools.md) | 严格 Tools 配置、`resolve_write`、`write_file`、`edit_file`、原子发布与失败保护。 | Runtime/TUI 接线、Shell、HTTP、飞书。 |
| [Phase 2.2：参数绑定 Approval 与续执行](phase-2/approval-lifecycle.md) | canonical hash、waiting/child Turn、Owner/TTL、approve/deny、重启恢复、单次消费与审计。 | Shell、HTTP、飞书卡片。 |
| [Phase 2：单入口 TUI](phase-2/single-entry-tui.md) | pi-tui 默认入口与 Textual onboarding/fallback 的迁移关系。 | 飞书、历史虚拟化。 |
| [Python Core + pi-tui Bridge](phase-2/python-core-pi-tui-bridge.md) | NDJSON v1、Node 子进程、紧凑活动流、长文本/选择/审批、安装调试和跨进程回归。 | 发布包内置构建产物、删除 fallback。 |
| [TUI 可观测、长文本与分级审批](phase-2/tui-observability-and-scoped-approvals.md) | 默认中文/可切英文、草稿恢复、真实 Token 审计、Session/Always exact scope。 | lark-cli、飞书 Channel、规则管理 UI。 |
| [TUI 回归测试规范](phase-2/tui-regression-testing.md) | 25 个 pi-tui 协议/虚拟终端/跨进程用例、Textual fallback 和发布门禁。 | live LLM 硬断言。 |
| [Phase 2.3A：exact-argv 命令执行](phase-2/command-execution.md) | `run_command`、固定 PATH、硬禁止、精确 argv 规则、最小环境、进程组超时、输出上限与 TUI 审批。 | 任意 Shell、真实 `lark-cli`/Node 路径 smoke、OS sandbox。 |
| [Phase 2.4：Pinned HTTPS 与 SSRF 防护](phase-2/https-get-and-ssrf.md) | `http_get`、URL/DNS 公网校验、固定 IP/TLS hostname、每跳重验、文本预算、审批与 crash recovery。 | 浏览器、认证 Header、任意方法、企业代理。 |
| [Phase 2：回归、恢复与调试](phase-2/testing-and-debugging.md) | 当前 369 Python + 25 TypeScript tests、24 场景、stale-run recovery、Doctor、历史 live smoke 和发布手册。 | 飞书 E2E、soak、自动 Prompt/Skill 演进。 |
| [Phase 2.2：Approvals CLI（历史迁移）](phase-2/cli-approvals.md) | 记录旧入口为何被单入口 TUI 取代。 | 当前可执行命令。 |

## Phase 3：Memory、Skills 与上下文预算

| 文档 | 已验证内容 | 不包含 |
| --- | --- | --- |
| [Memory、Skills 与上下文压缩](phase-3/memory-skills-compaction.md) | Markdown Memory、审批写入、凭据过滤、Skill metadata/惰性正文、persistent compaction、runtime snapshot。 | 向量库、Skill 代码执行、自动修改 Prompt/Skill、飞书 Channel。 |

功能主线已验证到 Phase 3（全仓 369 Python + 25 TypeScript tests + 24/24 Agent cases），默认 pi-tui、
Textual fallback、TUI 可观测/分级审批和 P2.4 HTTPS 安全能力继续保留。P2.3B 的真实 `lark-cli` 闭环仍是
明确待办，不阻塞后续主线。

## Phase 4：飞书 Channel

| 文档 | 已验证内容 | 不包含 |
| --- | --- | --- |
| [飞书 Channel Core](phase-4/feishu-channel-core.md) | allowlist、durable Inbox/Outbox、有限 Worker、官方 WebSocket Transport、Unicode 分片、恢复、streaming card 与审批。 | 正式 gateway CLI、真实飞书账号 E2E、部署/soak。 |

Phase 4 Core 已进入代码，但 Gateway 进程入口和真实平台验收还没有完成；质量主线也仍需补当前版本 R3 live
DeepSeek release eval。

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
