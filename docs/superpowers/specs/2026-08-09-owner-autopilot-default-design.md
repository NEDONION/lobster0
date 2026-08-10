# Lobster0 Owner Autopilot 默认值设计

> 状态：已确认，等待规格复核
> 日期：2026-08-09
> 关联：`2026-08-08-autopilot-permissions-and-approval-ui-design.md`

## 1. 问题与根因

当前新安装配置会显式生成 `[tools].mode = "autopilot"`，但配置加载器对缺少
`tools.mode` 的旧配置仍回退到 `safe`。因此旧状态目录升级后，即使消息来自已验证的飞书
Owner 私聊，安全的 `run_command` 仍会创建 pending Approval，并发送“Lobster0 审批”卡片。

现场只读核对确认：审批 #97 来自 `chat_type=p2p` 且发送者与已绑定 Owner 身份匹配；问题不在
飞书身份识别，而在旧配置的默认模式。

## 2. 目标行为

- 缺少 `tools.mode` 的旧配置按 `autopilot` 加载，与新安装模板保持一致。
- 显式配置 `safe`、`smart`、`autopilot` 或 `yolo` 时继续严格尊重配置值。
- 经过验证的 Owner 私聊在 `autopilot` 下直接执行通过硬校验的命令，不创建 Approval、
  `waiting_approval` ToolRun 或飞书审批卡片。
- 群聊、其他白名单用户和非 Owner 私聊继续视为不可信入口，不获得自动执行权限。
- 敏感路径、危险命令、Workspace 逃逸、SSRF、超时和结果预算等硬拒绝规则保持不变。
- 当前 Owner 的 `~/.lobster0/config.toml` 显式补入 `mode = "autopilot"`，使重启后的意图可见且稳定。

## 3. 实现边界

配置层是唯一需要改变默认语义的位置：`ToolConfig.mode` 与 `load_config()` 缺省读取统一为
`autopilot`。Policy、ToolExecutor 和 ChannelManager 继续复用现有可信入口与硬校验逻辑。

飞书审批卡片基础设施不删除。用户显式选择 `safe`/`smart`，或请求来自不可信入口时，现有审批
生命周期仍然有效。这样不会通过“隐藏卡片”留下永远无法续跑的 pending Turn，也不会让群聊获得
Owner 权限。

不在本次范围：自动审批历史 pending Approval、为群聊扩权、自动改写任意用户配置、删除 TUI 或
其他 Channel 的审批能力。

## 4. 数据流

1. Runtime 加载配置；缺少 `tools.mode` 时得到 `autopilot`。
2. ChannelManager 仅在发送者等于配置 Owner 且 `chat_type=p2p` 时传入
   `trusted_owner=True`。
3. PolicyEngine 先完成路径、argv、网络和风险硬校验，再对可信 Owner 的安全动作返回 `ALLOW`。
4. ToolExecutor 直接创建并执行普通 ToolRun；因为没有 `approval_id`，ChannelManager 不创建
   Approval Delivery，飞书只更新正常回答卡片。

## 5. 测试与文档

- 配置回归：旧配置缺少 `mode` 时加载为 `autopilot`；显式 `safe` 仍为 `safe`。
- Channel 回归：Owner 飞书私聊在默认模式下执行安全命令时不产生 Approval Delivery；现有群聊与
  非 Owner 审批测试继续通过。
- Policy/Executor 既有 hard-deny 测试继续通过，证明默认值变化没有绕过安全边界。
- 更新 README、权限工程文档和产品/架构中关于“旧配置缺少 mode 回退 safe”的陈述。
- 运行全量 `unittest`、Ruff、文档校验和 `git diff --check`；Channel 语义变化额外运行 20 轮
  versioned Channel gate。
