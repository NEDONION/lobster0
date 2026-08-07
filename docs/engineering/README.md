# 工程文档索引

这里记录 MiniClaw 已实现模块的工程说明。设计文档描述目标；工程文档只描述已经进入代码与验证的行为。

## Phase 2：Tool 与安全

| 文档 | 已验证内容 | 不包含 |
| --- | --- | --- |
| [Phase 2.1A：Tool Runtime 与 system_info](phase-2/tool-runtime-and-system-info.md) | Tool Contract、Registry、Policy、Executor、ToolRun/Audit、`system_info` 与 Agent Runtime。 | 文件读写、审批、Shell、飞书。 |
| [Phase 2.1B：Workspace 只读文件与搜索](phase-2/workspace-read-tools.md) | `read_file`、`glob`、`grep`、Workspace Guard、离线 Agent/CLI 装配验证。 | 真实 DeepSeek 文件 smoke、`write_file`、Shell、审批、飞书。 |
| [Phase 2.1C：Agent 场景回归与 Benchmark 基线](phase-2/agent-regression-evals.md) | JSONL Schema、10 条 Claw-like query、真实离线 runner、`miniclaw eval`、baseline/release record。 | live DeepSeek、report/compare、飞书 E2E、自动改 Prompt/Skill。 |

功能主线下一步是 P2.2 写入与参数绑定审批；质量主线下一步是 R3 live DeepSeek release eval。两者仍是规划，
不应被当作当前 CLI 能力。

## Phase 1：CLI Agent 闭环

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
