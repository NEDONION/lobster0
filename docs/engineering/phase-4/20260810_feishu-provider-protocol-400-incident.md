# 飞书会话 `provider_protocol` / DeepSeek 400 事故复盘（2026-08-10）

> 状态：根因已定位，代码修复已落地并通过全部相关测试；已用真实 DeepSeek 请求重放验证修复有效；
> 修复已同步进当前运行的 Gateway 进程（`.worktrees/phase6-feishu-production-design`）并重启生效。
> 受影响会话：Feishu session_id=20（"Lucas 的 Lobster0" Owner DM）。

## 1. 症状

用户在飞书里发消息，收到卡片：

```
失败阶段：模型响应校验
错误码：provider_protocol
原因：模型生成的工具参数格式错误，已安全停止。
调试编号：Turn #266 · Event #99
```

且从 `2026-08-10T07:32:24Z`（turn 263）开始，同一个飞书会话里**之后每一条消息都必现同样的报错**，
Gateway 进程本身没有卡死——同一时间段其它会话（如 session 47）仍能正常完成对话。

## 2. 根因

### 2.1 触发序列

1. `07:32:06Z` 助手在 turn 262 里一次性发起 3 个 Tool Call（其中一个 `run_command` 需要人工审批），
   Feishu 卡片提示 `/approve 109`。
2. 审批还没处理完时，用户在 `07:32:24Z` 又发了一条新消息（"你为什么还需要审批"）。Channel 层
   （`src/lobster0/channels/manager.py` → `TurnService.handle_inbound`）**没有检查同一 Session 是否已有
   Turn 处于 `waiting_approval`**，直接把这条新消息作为新 Turn 的 User Message 写入了会话历史。
3. `07:32:28Z` 审批处理完成，`create_continuation` 生成的续跑 Turn（264）把 3 个 Tool Call 里
   1 个真实执行结果和 2 个 `not_executed` 占位结果一起落库——但落库时间比第 2 步的用户消息晚，
   所以按消息 ID 顺序，会话历史里出现了：

   ```
   assistant(发起 3 个 tool_calls)  →  user("你为什么还需要审批")  →  tool → tool → tool
   ```

   这不符合 OpenAI/DeepSeek 协议：Tool 结果消息必须紧跟在触发它的 Assistant `tool_calls`
   消息后面，中间不能插入别的角色消息。

### 2.2 真正炸掉请求的 Bug：Provider-safe 过滤按 Turn ID 判定，误删无关消息

`src/lobster0/storage/conversations.py` 里的 `_provider_safe_context()` 本来就是为了兜住上面这种
情况设计的——它会扫描消息序列，一旦发现某个 Turn 的 Tool Call 批次没能在下一条非 Tool 消息前
完整拿到所有 Tool 结果，就把这个"孤儿"批次从发给模型的历史里剔除。这一层过滤器*应该*已经能够
避免协议错误。

但它的失效范围判定用的是 **Turn ID**，而不是具体消息：一旦某个 Turn 被判定为"孤儿"，凡是
`message.turn_id` 等于这个 Turn ID 的消息全部剔除。问题在于：**同一个 Assistant Tool Call 批次的
执行结果，可能因为审批 continuation 被落库到跟触发它的 Assistant 消息不同的 Turn 里**——这是
`create_continuation` 的正常行为，不是数据损坏。

真实数据（`/Users/nedonion/.lobster0/lobster0.db`, session 20）复现了这个碰撞：

| message id | turn_id | role | 说明 |
| --- | --- | --- | --- |
| 1316 | 261 | assistant | 发起 `call_00`(glob)、`call_01`(run_command --version) |
| 1317 | 261 | tool | `call_00` 结果 |
| 1319 | **262** | tool | `call_01` 结果——**落到了 continuation Turn 262，而不是 261** |
| 1320 | 262 | assistant | 新一批 3 个 tool_calls（其中一个后来变成孤儿） |
| 1322 | 263 | user | 用户在审批完成前插入的新消息 |
| 1323–1325 | 264 | tool | 1320 那批 tool_calls 的迟到结果 |

Turn 262 因为 1320 的孤儿批次被判定失效，旧算法把 `turn_id == 262` 的消息**全部**剔除——这误删了
`message 1319`，而 1319 其实是 **Turn 261 自己完整、无关的 Tool 结果**，只是恰好和后来变成孤儿的
Turn 262 共享了 turn_id。删掉 1319 之后，message 1316 里的 `call_01` 就变成了没有结果的悬空
Tool Call，于是发给 DeepSeek 的请求里出现了"Assistant 发起 tool_calls，但只有部分被回答"的非法结构，
DeepSeek 直接以 `HTTP 400` 拒绝整个请求——且因为这段坏历史已经落库，**之后这个 Session 的每一次
新请求都会带着这段坏历史，永远 400，直到人工修复**。

### 2.3 卡片文案本身也是错的

`channels/manager.py` 的 `_failure_profile()` 对**所有** `ProviderProtocolError` 都固定显示
"模型生成的工具参数格式错误"，不区分"DeepSeek 直接拒绝了请求（HTTP 400）"和"模型自己生成的
Tool 参数格式不对"这两种完全不同的情况，也没有保留 DeepSeek 返回的真实错误正文（`_status_error`
之前直接丢弃了 HTTP 响应体）。这导致排查时只能看到一句通用文案，看不到真正原因。

## 3. 修复

### 3.1 `_provider_safe_context()` 改为按单条消息判定，而不是按 Turn（核心修复）

文件：[`src/lobster0/storage/conversations.py`](../../../src/lobster0/storage/conversations.py)

- 失效集合从 `set[Turn ID]` 改为 `set[Message ID]` + `set[Tool Call ID]`。
- 孤儿 Tool Call 批次只连带**它紧邻的前一条 User 消息**一起剔除（那是它唯一的触发者），不再
  牵连任何共享 turn_id、但实际上属于更早、已经配对完整的 Tool Call/Tool Result。
- 新增回归测试
  [`test_context_keeps_unrelated_tool_result_when_a_later_batch_is_orphaned`](../../../tests/test_conversations.py)，
  完整复现上表的跨 Turn 落库场景，断言旧的、无关的 Tool 结果必须被保留。
- 原有两个相关测试（`test_recent_limit_never_orphans_tool_result_from_approval_child`、
  `test_context_drops_orphaned_waiting_tool_turn_before_new_user`）保持通过，说明修复没有改变
  既有的、已被测试锁定的行为（完整审批 continuation 保留；纯孤儿批次连同触发它的 User 消息一起丢弃）。

### 3.2 Provider 400 等被拒绝请求，保留响应正文用于诊断

文件：[`src/lobster0/providers/openai_compatible.py`](../../../src/lobster0/providers/openai_compatible.py)

- `_status_error()` 改为 `async`，对非重试状态码（如 400）读取并截断（≤500 字符）HTTP 响应正文，
  拼进 `ProviderProtocolError` 的异常信息。
- 这段信息只会写进 `turns.error_message`（数据库）和后端日志，**不会**进入 Feishu 卡片文案——
  `_failure_diagnostics()` 的既有设计原则是卡片上不回显原始异常正文，这次修复延续了这个边界。
- 效果：下次再发生类似 400，直接查 `turns.error_message` 就能看到 DeepSeek 真实返回的错误信息，
  不需要再像这次一样逆向工程一整套消息序列。

### 3.3 卡片文案区分"服务商直接拒绝"和"工具参数格式错误"

文件：[`src/lobster0/channels/manager.py`](../../../src/lobster0/channels/manager.py)

`_failure_profile()` 里 `ProviderProtocolError` 分支新增判断：如果异常信息以
`"model provider rejected the request with status"` 开头（即 3.2 里新加的这类），卡片文案改成：

> 模型服务直接拒绝了这次请求（协议或参数校验未通过），未生成任何工具调用。
> 请重试；若持续失败，请检查 Turn 的 error_message 获取服务商返回的具体原因。

否则保留原来的"模型生成的工具参数格式错误"文案（这类确实是模型自己生成的 Tool 参数不合法，
文案本身没错）。

### 3.4 曾经尝试但撤销的方案：禁止在审批 pending 时开新 Turn

最初还实现了第三个修复：在 `TurnService.handle_inbound()` 里，如果 Session 已有 Turn 处于
`waiting_approval`，直接拒绝新 User 消息开新 Turn（避免 2.1 描述的插入本身发生）。

**这个方案被撤销了**，因为它跟已有测试 `test_new_turn_omits_orphaned_approval_call_from_provider_history`
（[`tests/test_turn.py`](../../../tests/test_turn.py)）冲突：该测试验证 CLI 用户可以**主动放弃**一个
还没决议的旧审批、直接开始处理新请求，且系统应该保证发给模型的历史仍然是 protocol-safe 的。
3.1 的修复已经让"发生插入"这件事本身不再危险——不管什么原因导致 Assistant `tool_calls` 和它的
User 触发消息被打断，`_provider_safe_context()` 现在都能精确剔除孤儿批次而不伤及无辜。因此没有必要
再额外禁止这个已经被测试认可的产品行为。

## 4. 验证

1. **单元测试**：`uv run python -m unittest tests.test_conversations tests.test_channel_manager
   tests.test_turn tests.test_openai_compatible_provider tests.test_compaction -v` → 82/82 通过
   （含 1 个新增回归测试）。
2. **全仓门禁**：`uv run python -m unittest discover -s tests -q` → 1291 个测试，除 83 个与本次改动
   完全无关、且在改动前就已经因为本机环境缺失（managed Python 3.12 二进制、`pnpm` 不可用）而失败的
   `test_install_runtime` / `test_release_bundles` 用例外，全部通过。Ruff 对本次改动的 4 个文件
   `All checks passed`。
3. **真实 DeepSeek 请求重放（决定性证据）**：用修复后的 `_provider_safe_context()` 重新读取
   session 20 的真实历史，构造出和生产环境完全一致的请求 payload，直接 POST 到
   `https://api.deepseek.com/chat/completions`：
   - 修复前的历史（含悬空 `call_01`）→ 生产环境已验证为 `HTTP 400`。
   - 修复后的历史（`call_01` 结果被保留）→ 本次验证返回 **`HTTP 200`**，`prompt_tokens: 116929`，
     模型正常生成了响应。

   这证明本次修复直接解决的就是这次事故的真实根因，而不是停留在"结构看起来更合理"的猜测层面。

## 5. 部署

- 修复代码已应用到主仓库 `main` 工作区，并同步复制进当前实际运行 Feishu Gateway 的
  `.worktrees/phase6-feishu-production-design`（两边此前四个文件字节级一致，直接覆盖同步）。
- 已优雅重启 Gateway 进程（旧 PID 3558 → 新 PID 30955），Feishu WebSocket 已重新连接并进入
  `supervisor_state=ready`（`2026-08-10T11:43:02Z`）。
- 尚未提交 git commit——改动目前只在工作区；是否提交、是否需要把同样的修复 merge 回
  `codex/phase6-feishu-production-design` 分支，由用户决定。

## 6. 遗留问题 / 后续建议

1. **Session 20 历史里仍然保留着当时被判定为孤儿的 Turn 262/264 内容**（`_provider_safe_context()`
   只在读取时过滤，不改写数据库）。这是预期行为、无需清理，但如果要人工审计当时具体发生了什么，
   要去 `turns`/`messages` 表按 `turn_id IN (262, 264)` 查，而不是从 Feishu 聊天记录里找。
2. 3.2 的响应正文截断长度（500 字符）是经验值，如果未来 DeepSeek 或其它 OpenAI-compatible
   服务商的错误正文明显更长导致关键信息被截断，可以按需调整 `_MAX_ERROR_BODY_CHARS`。
3. 本次没有改动"审批决定后 Turn 状态未必立即变化"这个既有设计（`ApprovalRepository.deny()` 不会
   自动把关联 Turn 转出 `waiting_approval`，需要走 `create_continuation`）；`test_turn.py` 里的用例
   显示这是已知且被测试接受的行为，不在本次事故范围内，如果后续觉得这个语义容易引起混淆，值得
   单独立项讨论。
