# Phase 2：Python Core + TypeScript pi-tui 工程落地文档

> 状态：已实现；pi-tui 为默认展示层，Textual 为迁移期 fallback
> 协议：Lobster0 stdio NDJSON protocol v1
> 运行时：Python 3.12、Node.js >= 22.19.0、pnpm、pi-tui 0.84.1
> 时间说明：本文按 Phase 2 的 TUI 交付组织；其中“后续阶段”只表示当时顺序，当前能力以工程索引为准。

## 1. 大白话说明

Lobster0 现在由两个进程配合：Python 是“大脑和安全负责人”，TypeScript 是“终端屏幕和键盘”。你在 TUI 输入
一句话，TypeScript 把它作为一行 JSON 发给 Python；Python 调模型、执行 Tool、做审批、存 SQLite，再把每一步作为
一行行事件发回来。UI 只能展示和提出请求，不能绕开 Python 直接执行命令。

这解决了三个工程问题：

1. UI 可以换壳而不重写 Agent；
2. Core 可以独立回归，不依赖真实终端；
3. 以后飞书、Telegram、Discord 或桌面端可以复用同一套事件语义。

## 2. 目录和职责

| 路径 | 作用 |
|---|---|
| src/lobster0/bridge/protocol.py | Python 权威协议校验和编码 |
| src/lobster0/bridge/server.py | 请求调度、单 Turn、审批续跑、RunEvent 转发 |
| src/lobster0/bridge/__main__.py | python -m lobster0.bridge 进程入口 |
| src/lobster0/tui_launcher.py | 唯一 CLI 的 pi/Textual 选择、Node 检查、argv 启动 |
| tui/src/protocol.ts | TypeScript 帧类型、增量 NDJSON decoder、2 MiB 限制 |
| tui/src/bridge-client.ts | Python 子进程监督、请求关联、事件订阅、稳定错误 |
| tui/src/state.ts | 纯 reducer：消息、Reasoning、Tool、Telemetry、Approval |
| tui/src/app.ts | TUI 生命周期、Editor、ScrollView、Slash command、错误恢复 |
| tui/src/components/conversation.ts | Header、Timeline、Telemetry 的紧凑渲染 |
| tui/src/components/approval.ts | Core grant modes 驱动的审批 Overlay |
| tui/src/main.ts | ProcessTerminal + TuiAltScreen 真实入口 |
| tests/test_bridge_*.py | Python 协议/Server 契约 |
| tui/test/*.test.ts | TypeScript 协议、State、虚拟终端和交互回归 |
| tests/test_pi_tui_integration.py | Node client ↔ Python Bridge 跨进程冒烟 |

## 3. 安装、构建和运行

第一次从源码运行：

~~~bash
uv sync --extra dev
corepack enable
pnpm --dir tui install --frozen-lockfile
pnpm --dir tui build
uv run lobster0 init
uv run lobster0 doctor
uv run lobster0
~~~

仓库根目录的 .node-version 固定最低 Node 版本。使用 fnm、nvm、mise 或 Volta 均可，但最终 node --version 必须
不低于 v22.19.0。

入口策略：

~~~bash
# 默认：优先 pi-tui，不满足要求时回退 Textual
uv run lobster0

# 要求 pi-tui；缺 Node/构建产物时直接失败，适合 CI 和验收
LOBSTER0_TUI=pi uv run lobster0

# 迁移期故障排查
LOBSTER0_TUI=textual uv run lobster0

# 使用不在 PATH 的 Node
LOBSTER0_NODE=/absolute/path/to/node uv run lobster0
~~~

dist 是本地构建产物，不提交 Git。修改 TypeScript 后必须重新运行 pnpm --dir tui build。

## 4. 协议 v1

### 4.1 Envelope

请求必须包含 v/id/type/payload；响应带相同 id；异步事件没有 id：

~~~json
{"v":1,"id":"ui-1","type":"client.hello","payload":{"client_name":"lobster0-pi-tui","client_version":"0.1.0","protocols":[1]}}
{"v":1,"id":"ui-1","type":"response.ok","payload":{"protocol":1,"core_version":"0.1.0"}}
{"v":1,"type":"event.model_text_delta","payload":{"turn_id":42,"text":"你好"}}
~~~

约束：UTF-8、单行、标准 JSON、最大 2 MiB；禁止 NaN/Infinity；request ID 只允许受限 ASCII；payload 必须是 object。

### 4.2 请求表

| Type | 精确 payload | 说明 |
|---|---|---|
| client.hello | client_name/client_version/protocols:[1] | 返回模型、Workspace、工具、能力和上下文预算 |
| turn.start | session_key/text | 接收后立即 ack，执行过程走事件 |
| turn.cancel | 空 object | 取消一个 active Turn；空闲幂等 |
| approval.resolve | approval_id/decision | decision 为 deny/once/session/always |
| session.new | session_key | 运行或审批中拒绝切换 |
| bridge.shutdown | 空 object | 有序取消任务并退出 |

### 4.3 事件表

| Event | UI 行为 |
|---|---|
| turn_started | 标记忙碌、重置本轮 Telemetry |
| model_usage | 更新真实 context/input/output/tool/iteration/request id |
| model_reasoning | 新增默认展开的弱色思考项 |
| model_text_delta | 更新同一 Assistant item，不追加重复组件 |
| tool_requested | 创建一个 Tool activity |
| tool_started | 合并 requested→started |
| tool_finished | 合并终态、耗时、预览 |
| approval_required | 显示 Overlay，冻结新 Turn |
| turn_finished | 固化 Markdown，解除输入锁 |
| turn_failed/cancelled | 安全提示并恢复提交草稿 |
| bridge_error | 未知 Core 异常的最后防线，恢复草稿且不泄露异常正文 |

## 5. 长文本为什么现在不会丢

~~~mermaid
flowchart TD
    PASTE["Bracketed paste 原始文本"] --> EDITOR["pi-tui Editor"]
    EDITOR -->|"Enter"| SNAPSHOT["保存 submittedDraft"]
    SNAPSHOT --> SEND["turn.start"]
    SEND -->|"response.ok"| RUN["等待终态事件"]
    RUN -->|"turn_finished"| CLEAR["清除草稿快照"]
    RUN -->|"failed/cancelled/bridge_error"| RESTORE["原样恢复；与新草稿合并"]
~~~

Assistant 流式内容始终落在同一个 TimelineView/Assistant state item 中；ScrollView 也不重建。鼠标选区由
TuiAltScreen 管理并在释放时写入 OSC52。用户手动上滚后 isFollowingEnd=false，新 delta 不改变 scrollTop；
用户回到底部后自动恢复跟随。

## 6. ProviderProtocolError 修复方法

看到 provider_protocol 时先区分两类：

### 历史消息非法

Approval child Turn 会保存 Tool Result。最近消息窗口如果只按条数切，可能留下没有父 Assistant Tool Call 的孤立
Tool Result。MessageRepository.list_recent 现在会向前回退到最近 User 边界，确保送给 Provider 的历史是一段完整
对话。临时绕过可以 /new，但不需要删除 SQLite。

### 兼容服务参数形态不同

OpenAI 规范要求 function.arguments 是 JSON 字符串；部分 DeepSeek/OpenAI-compatible 网关实际直接返回 object。
Provider 现在接受 string 或 object，先规范化，再统一检查最终结果必须是 JSON object。数组、数字、残缺分片和非法
JSON 仍然 fail closed。

## 7. 审批与权限

UI 只按 Core 的 grant_modes 渲染：

- Deny：拒绝；
- Allow once：仅消费当前 Approval；
- Allow this session：仅当前 Python Runtime 内的精确 scope；
- Always allow：只保存 Core 允许持久化的精确规则。

审批 id、Owner、Tool、参数 hash、TTL 和是否可持久化全部由 Python Core 验证。TypeScript 不能自行给一个文件写入或
inline AppleScript 增加 Always。

## 8. 回归测试方法

TypeScript：

~~~bash
pnpm --dir tui build
pnpm --dir tui test
~~~

主要用例：

- NDJSON 拆包、粘包、UTF-8 边界、超限和错误关联；
- 100 个 delta 仍只有一个 Assistant；
- 250,000 字符中英文多行 bracketed paste 原样提交；
- 请求失败、Provider 失败、取消和未知 Core 错误恢复草稿；
- 手工上滚保持位置，回底部恢复 follow；
- SGR mouse drag 真实生成 OSC52；
- 64/80/120 列渲染不越界；
- Reasoning/Tool/Telemetry/角色层级；
- Approval grant mode、按键和真实 Overlay→Core 决定。

Python 与跨进程：

~~~bash
uv run python -m unittest \
  tests.test_bridge_protocol \
  tests.test_bridge_server \
  tests.test_tui_launcher \
  tests.test_pi_tui_integration -v
~~~

跨进程 smoke 在 Node >=22.19 且 TUI 已构建时启动真实 BridgeClient 和真实 Python Bridge；环境不满足时 Python
测试会明确 skip，而 TypeScript 门禁仍必须在 Node CI job 中执行。

## 9. 调试手册

### doctor 显示 Node 失败

~~~bash
node --version
pnpm --dir tui install --frozen-lockfile
pnpm --dir tui build
uv run lobster0 doctor
~~~

### 强制 pi-tui，避免自动回退掩盖问题

~~~bash
LOBSTER0_TUI=pi uv run lobster0
~~~

### 单独检查 Bridge stdout

Bridge stdout 只能是一行一个 JSON。不要把日志 print 到 stdout；日志应走 stderr。可用测试的临时 home 发送
client.hello 和 bridge.shutdown，参考 tests/test_bridge_server.py。

### TUI 卡在忙碌状态

Core 必须发送 turn_finished/turn_failed/turn_cancelled。未知异常由 Bridge 转成 event.bridge_error；UI 收到后解除输入锁、
恢复草稿并只显示稳定 code。若仍卡住，先跑 tui/test/input.test.ts。

### 真实字号太大

在终端应用设置中调小 Font Size。Lobster0 无法只缩小 Reasoning 字号；当前通过弱色、单行状态和少留白实现高密度。

## 10. 扩展协议的正确步骤

1. 先在设计文档定义新 request/event 的责任边界；
2. Python decode_request 增加精确字段验证和 RED 测试；
3. Bridge 只调用现有 Core service，不把业务写进协议层；
4. TypeScript protocol/state 先写拆包与 reducer RED；
5. UI 只消费事件，不推断审批或 Tool 成功；
6. 增加跨进程 smoke；
7. 若破坏字段语义，升级 protocol major，不能悄悄改变 v1。

## 11. 当前边界

- dist 尚不进入 Git；源码安装后需要一次 pnpm build；
- Textual 尚未删除；
- lark-cli P2.3B 与飞书 Channel 已由后续阶段完成；Telegram、Discord 也已在架构 Phase 5 完成
  implementation，三个外部平台仍按各自 Live Evidence 独立验收；
- 桌面版尚未实现；
- 不公开模型隐藏思维链，只展示 Provider 明确返回的 Reasoning 和 Lobster0 可审计活动。
