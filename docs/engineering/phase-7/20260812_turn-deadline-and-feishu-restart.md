# Turn 墙钟预算与飞书 `/restart` 工程方案

> 文档日期：2026-08-12
> 状态：**IMPLEMENTATION IN PROGRESS**
> 触发事件：2026-08-12 一次真实飞书对话中，Agent 为了安装缺失的 `lark-cli`，在
> **108.7 秒、16 个 step、24 次 tool call** 内连续尝试 npm→pip→GitHub 二进制下载，全部失败后才自己放弃。
> 用户侧表现为"机器人卡死且无法干预"。

## 1. 为什么现有预算全都没有拦住它

| 预算 | 位置 | 事故当时的值 | 为什么没触发 |
| --- | --- | --- | --- |
| `max_iterations=32` | `AgentRunner.run` 循环上界 | 16 轮 | 计数式，远未到上限 |
| `hard_max_iterations=64` | 同上 | 16 轮 | 同上 |
| `max_no_progress_iterations=3` | `runner.py` 批次末尾 | 从未累计到 3 | 只统计**连续**无进展轮；每次换一个命令、拿到一个新报错都算"有进展"，计数器被反复清零 |
| `tools.run_command.timeout_seconds=30`（上限 120） | `tools/command.py` | 单条命令未超时 | 只约束**单条命令**，不约束整个 Turn |

结论：仓库里**没有任何"一次 Turn 总共能花多少墙钟时间"的约束**。已核对
`src/lobster0/agent/runner.py` 与 `src/lobster0/channels/base.py`，两者都不含 `deadline` / `elapsed` /
`monotonic` / turn 级 timeout。

## 2. Feature 1：单次 Turn 的墙钟预算

### 2.1 设计要点

- **单调时钟**：`time.monotonic`，通过 `AgentRunner(clock=...)` 注入，NTP 回拨不会让预算失效或提前触发，
  单元测试也不需要真的 sleep。
- **与计数预算并存**：不改动、不放宽 `max_iterations` / `hard_max_iterations` /
  `max_no_progress_iterations`，墙钟预算是**新增的第四条**。
- **复用既有退出路径**：与 no-progress 停止完全一致——`AgentRunner` 抛
  `AgentTurnDeadlineError(AgentError)` → `TurnService` 的
  `except (ContextError, ConversationDataError, AgentError, ProviderError)` 分支把 Turn 标成 `failed`
  并写入稳定错误码 `turn_deadline` → `ChannelManager` 生成诊断消息投递给用户。
  没有新的终止机制，没有静默截断，没有裸崩。
- **默认值 90 秒**：见 §2.3。
- **配置沿用既有约定**：`[agent] max_turn_seconds`（TOML）+ `LOBSTER0_MAX_TURN_SECONDS`（环境变量），
  和 `LOBSTER0_MAX_TOOL_ITERATIONS` / `LOBSTER0_MAX_NO_PROGRESS_ITERATIONS` 走完全相同的
  `config.py` 加载→环境覆盖→`AgentConfig`→`runtime.py` / `evals/runner.py` 注入链路。

### 2.2 检查点：迭代边界 + 批次内工具边界

墙钟预算**不打断已经在执行的 tool call**。原因：

1. 打断执行中的工具意味着要在任意一条 `await` 上取消，工具可能正处在写文件、写数据库、发 HTTP 请求的
   中途，"部分写入 + 已取消"是这套系统最不该引入的状态。
2. 单条工具本来就有自己的上界（`run_command` 默认 30s、最大 120s；`http_get` 20s），
   已经是一层硬约束。

因此检查放在两处，都在"没有任何未完成副作用"的时刻：

- **迭代边界**：每轮进入 Provider 请求之前（第 2 轮起；第 1 轮 elapsed≈0，检查无意义）。
- **批次内工具边界**：一个 assistant 响应可能带**多个** tool call，串行执行。只在迭代边界检查的话，
  一个 10 条命令的批次能把预算超出 10×120s。所以在批次内每次执行下一条工具前也检查一次。

**明确的残留风险**：单条病态工具调用仍然能冲过预算，超出量以该工具自身的超时为界
（`run_command` 最大 120s）。也就是说 90s 的预算，最坏情况下真实耗时约 90+120=210s。这是刻意的取舍。

### 2.2.1 只覆盖交互式 Turn

墙钟预算只在 `budget is None`（即交互式 Channel/CLI Turn）时生效。Automation 的 Turn 带
`AgentRunBudget`，而 `src/lobster0/automation/runner.py:186` 已经用
`asyncio.timeout(snapshot.budget.timeout_seconds)`（默认 **600s**）把整个 Turn 包住，超时落
`task_timeout` / `TIMED_OUT`。如果这里再叠一层 90s，后台任务会被悄悄砍到 90 秒——这是回归，不是修复。
事故发生在交互式飞书 Turn 上，那里**没有任何**墙钟主人，这正是本次要补的缺口。由单元测试直接断言。

### 2.3 默认 90 秒的理由

- 事故耗时 108.7s。**任何 ≥108.7s 的默认值都拦不住这次事故**，等于没做。
- 90s 是能拦住这次事故的最大整十秒值，也是 `run_command` 默认超时（30s）的 3 倍。
- 更长的合法任务（读很多文件、多次 web fetch）确实存在，所以：
  - 预算只在**工具边界**检查，正在进行的长工具不会被腰斩；
  - `[agent] max_turn_seconds` / `LOBSTER0_MAX_TURN_SECONDS` 让重负载用户一行改大；
  - Turn 失败时给出的提示明确写出"如需更长时间请调高这个配置"。
- 从用户感受出发：IM 场景下 90 秒无回应已经是"卡死"，把上界定在体感阈值附近才有意义。

### 2.4 用户看到什么

Turn 被标 `failed`，错误码 `turn_deadline`，飞书/Telegram/Discord 收到既有格式的诊断卡：

```
- 失败阶段：Agent Tool Loop
- 错误码：`turn_deadline`
- 原因：本轮已用 108.7 秒，超过单轮 90 秒的墙钟预算，已在工具边界安全停止。
- 调试编号：Turn #123 · Event #456
- 超时前正在执行：run_command npm · 3 args
- 已完成模型轮次：16
- Tool 状态：已记录 12 个真实 ToolRun；系统不会自动重试，请检查 ToolRun 确认副作用。
- 下一步：请把任务拆小后重试；确实需要更长时间，就调高 agent.max_turn_seconds（或 LOBSTER0_MAX_TURN_SECONDS）。
```

TUI/CLI 侧同一个错误码有中英两条摘要（`本轮超过墙钟时间预算` /
`the turn exceeded its wall-clock budget`），不会退回成裸异常类名。

"超时前正在执行"用的是 `tool_display_summary`，与审批卡、Tool 事件同一份脱敏摘要，不会泄露完整命令行、
URL 路径或凭据。

### 2.5 一致性

预算触发时：

- 迭代边界触发：这一轮还没发起 Provider 请求，本轮没有任何新消息，已完成的批次早已通过
  `on_intermediate` 落库。
- 批次内触发：**先给该批次剩余的每一条 tool call 补一条确定性的 `turn_deadline` 工具结果**，再
  `on_intermediate` 落库，最后才抛异常。这样存下来的历史里，assistant 消息声明的每一个
  `tool_call_id` 都有且只有一条对应的 tool 消息，下一轮重放上下文不会构造出非法请求。
  这一点由单元测试直接断言，不靠推理。

## 3. Feature 2：`/restart`

### 3.1 权限

复用 `ChannelManager._trusted_owner`（外部身份 == 配置的 Owner ID **且** 是 p2p 私聊），
与 `/reset`、`/permissions` 完全同一个判断，不新增授权路径。群聊里 Owner 发 `/restart` 也会被拒绝。

### 3.2 重启到底怎么实现

关键事实（读 `src/lobster0/install/service.py` 得到）：

- systemd unit：`Restart=on-failure` + `RestartSec=5`
- launchd plist：`KeepAlive = {SuccessfulExit: false}`

两者都是**只在非零退出时拉起**。所以"优雅退出让 supervisor 拉起"必须**以非零退出码退出**——
干净地 `exit 0` 会让 systemd/launchd 认为服务正常结束，永远不再拉起。这里选 `75`（`EX_TEMPFAIL`）。

托管检测（不改 `install/`，只读环境变量）：

| 情形 | 判据 |
| --- | --- |
| systemd user service | 进程环境里有 `INVOCATION_ID`（systemd 为每次 unit 调用注入，交互式 shell 没有） |
| macOS LaunchAgent | `XPC_SERVICE_NAME == "io.lobster0.gateway"`（与 plist Label 精确相等） |
| 显式声明（Docker `--restart` 等） | `LOBSTER0_SUPERVISED=1`；`LOBSTER0_SUPERVISED=0` 强制关闭 |

- **有托管**：先回一句确认，3 秒后 set `shutdown_event` → `GatewaySupervisor.shutdown()` 走既有的
  完整反向清理（停止接收 → drain manager → 停 delivery → 断开 transport → 关 runtime），
  `run_gateway` 返回 75，CLI 用它作为进程退出码，supervisor 5 秒后拉起。
- **无托管**：**什么都不做**。前台运行时退出等于把机器人关掉，比不动更糟。只回一句说明当前是前台运行，
  并给出手动重启方式。

### 3.3 诚实的措辞

`/restart` 只能救"进程还活着、还在处理消息、但状态卡住"的 Gateway。进程真死了，这条消息根本收不到，
那是 supervisor 的职责，不是本命令能解决的。回复里直说这一点。

### 3.4 确认时序

先把确认写进 Delivery Outbox（durable），再等 3 秒才触发关停，尽量让用户在重启前就看到确认；
即使 3 秒内没发出去，这条消息也留在 Outbox 里，重启后由 DeliveryWorker 补发——不会静默消失。

## 4. 改动文件

| 文件 | 改动 |
| --- | --- |
| `src/lobster0/agent/runner.py` | `AgentTurnDeadlineError`；`max_turn_seconds` / `clock` 参数；两处检查点；批次补齐工具结果 |
| `src/lobster0/agent/__init__.py` | 导出新异常 |
| `src/lobster0/agent/turn.py` | `_error_code` 增加 `turn_deadline` |
| `src/lobster0/config.py` | `agent.max_turn_seconds` + `LOBSTER0_MAX_TURN_SECONDS` |
| `src/lobster0/bootstrap.py` | 默认配置模板补一行 |
| `src/lobster0/runtime.py`、`src/lobster0/evals/runner.py` | 注入新预算 |
| `src/lobster0/channels/manager.py` | `turn_deadline` 失败画像与诊断行；`/restart` 命令；`attach_restart` |
| `src/lobster0/channels/restart.py`（新增） | 托管检测与 `GatewayRestartController` |
| `src/lobster0/gateway.py` | 装配 controller，`run_gateway` 返回退出码 |
| `src/lobster0/cli.py` | 用 `run_gateway` 的返回值作为退出码 |
