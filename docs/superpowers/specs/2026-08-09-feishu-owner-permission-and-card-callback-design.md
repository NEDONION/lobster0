# 飞书 Owner 权限与审批卡回调修复设计

> 状态：用户已确认执行并要求合并推送 `main`
> 日期：2026-08-09

## 1. 现场根因

- 最近消息均来自配置 Owner 的 `p2p` 会话，身份识别正确；私有配置仍显式设置
  `tools.mode = "safe"`，所以 `write_file`、`run_command` 正常返回 `require_approval`。
- 审批 #100～#102 的 ToolRun 均记录为 `require_approval`；审计中没有 card-action 处理记录。
- 审批期间 Gateway 被外部 live-smoke 进程停止，LaunchAgent 把优雅 SIGTERM 视为成功退出且不再拉起，
  导致按钮点击时没有进程接收飞书长连接回调。
- Approval continuation 创建的 ToolContext 没有保留 `trusted_owner=true`，后续 Tool 仍可能再次审批。
- `run_command` 审批摘要复制完整 argv，长文档正文会撑满飞书卡片。

## 2. 目标行为

- 当前 Owner 状态目录持久使用最高自动化模式；Owner 私聊中通过硬校验的 Tool 直接执行，不创建审批卡。
- 敏感路径、Workspace 逃逸、危险命令、提权、SSRF、凭据和 Critical Tool 继续硬拒绝，权限模式不能绕过。
- 非 Owner、群聊或用户显式切回 `safe`/`smart` 时仍保留审批。
- 飞书审批按钮点击后立即把原卡更新为“处理中”，然后更新为成功、拒绝或失败终态。
- 回调 continuation 的最终回复进入 durable Delivery；若 continuation 又产生审批，则可靠创建下一张审批卡。
- 重复点击由 Core Approval 条件更新保持幂等，不重复执行 Tool。
- 审批摘要只展示 Tool、可识别目标和有界参数计数，不展示完整长正文或凭据值。
- macOS LaunchAgent 始终保活；被 smoke/重启停止后自动恢复回调接收。

## 3. 组件边界

- `policy/engine.py` 不放宽硬校验；当前实例只修改私有 `config.toml` 的启动模式。
- `agent/turn.py` 修正 Owner-bound Approval continuation 的可信上下文。
- `channels/approvals.py` 负责严格 payload、紧凑审批/状态卡渲染，不执行 Tool。
- `channels/feishu_approval.py` 负责按钮回调编排：处理中卡、Controller、durable final/next approval、终态卡。
- `gateway.py` 只装配依赖，不再内联业务回调。
- LaunchAgent 是本机运行配置，不纳入仓库，不包含任何 Secret。

## 4. 失败与幂等

- 非 Owner、畸形 payload、过期、已处理和 hash mismatch 都只显示稳定短提示。
- 卡片更新失败不回滚已经由 Core 完成的审批；最终回复/下一审批先写 SQLite Delivery 再异步发送。
- callback 未知异常在 Feishu 边界收口，原始异常、argv、Token 和文件内容不进入状态卡。
- 同一按钮重投由 SDK action dedup 与 Core Approval 状态机双重防重。

## 5. 验证

- TDD 覆盖 continuation 保留 trusted Owner、紧凑摘要、处理中到终态、下一审批 Delivery、重复/失败提示。
- 运行全部 Python unittest、Ruff、文档校验、`git diff --check` 和 Channel 20 轮稳定性门禁。
- 本机验证 `config.toml` 非秘密模式值、LaunchAgent KeepAlive、Gateway lease commit 与飞书 connected/ready。
