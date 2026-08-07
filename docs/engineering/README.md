# 工程文档索引

这里记录 MiniClaw 已实现模块的工程说明。设计文档描述目标；工程文档只描述已经进入代码与验证的行为。

## Phase 2：Tool 与安全

| 文档 | 已验证内容 | 不包含 |
| --- | --- | --- |
| [Phase 2.1A：Tool Runtime 与 system_info](phase-2/tool-runtime-and-system-info.md) | Tool Contract、Registry、Policy、Executor、ToolRun/Audit、`system_info` 与 Agent Runtime。 | 文件读写、审批、Shell、飞书。 |
| [Phase 2.1B：Workspace 只读文件与搜索](phase-2/workspace-read-tools.md) | `read_file`、`glob`、`grep`、Workspace Guard、离线 Agent/CLI 装配验证。 | 真实 DeepSeek 文件 smoke、`write_file`、Shell、审批、飞书。 |
| [Phase 2.1C：Agent 场景回归与 Benchmark 基线](phase-2/agent-regression-evals.md) | JSONL Schema、21 条 Claw-like query、真实离线 runner、`miniclaw eval`、baseline/release record。 | 完整 live DeepSeek runner、report/compare、飞书 E2E、自动改 Prompt/Skill。 |
| [Phase 2.2A：安全写边界与原子文件 Tool](phase-2/filesystem-tools.md) | 严格 Tools 配置、`resolve_write`、`write_file`、`edit_file`、原子发布与失败保护。 | Runtime/TUI 接线、Shell、HTTP、飞书。 |
| [Phase 2.2：参数绑定 Approval 与续执行](phase-2/approval-lifecycle.md) | canonical hash、waiting/child Turn、Owner/TTL、approve/deny、重启恢复、单次消费与审计。 | Shell、HTTP、飞书卡片。 |
| [Phase 2.2B：单入口 Textual TUI](phase-2/single-entry-tui.md) | Textual 选型、唯一入口、AgentRuntime、RunEvent、可展开 Trace 与 Allow once/Deny Modal。 | 飞书、永久审批规则。 |
| [TUI 回归测试规范](phase-2/tui-regression-testing.md) | 18 个稳定用例、Textual Pilot、事件/持久化契约、PTY smoke 和发布门禁。 | 全屏快照主门禁、live LLM 硬断言。 |
| [Phase 2.3A：exact-argv 命令执行](phase-2/command-execution.md) | `run_command`、固定 PATH、硬禁止、精确 argv 规则、最小环境、进程组超时、输出上限与 TUI 审批。 | 任意 Shell、真实 `lark-cli`/Node 路径 smoke、OS sandbox。 |
| [Phase 2.4：Pinned HTTPS 与 SSRF 防护](phase-2/https-get-and-ssrf.md) | `http_get`、URL/DNS 公网校验、固定 IP/TLS hostname、每跳重验、文本预算、审批与 crash recovery。 | 浏览器、认证 Header、任意方法、企业代理。 |
| [Phase 2：回归、恢复与调试](phase-2/testing-and-debugging.md) | 258 tests、21 场景、stale-run recovery、七项 Doctor、历史 live smoke 和发布手册。 | 飞书 E2E、soak、自动 Prompt/Skill 演进。 |
| [Phase 2.2：Approvals CLI（历史迁移）](phase-2/cli-approvals.md) | 记录旧入口为何被单入口 TUI 取代。 | 当前可执行命令。 |

功能主线已验证到 P2.4（全仓 258 tests + 21/21 Agent cases），但 P2.3B 的真实 `lark-cli` 闭环仍明确
未完成；下一步处理 NVM/Node 运行时路径、doctor 检查，并用本机 `lark-cli auth status` 跑通第一条受控命令。
质量主线下一步仍是 R3 live DeepSeek release eval。

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
