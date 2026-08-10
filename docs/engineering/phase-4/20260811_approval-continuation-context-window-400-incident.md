# 审批续跑再次触发 DeepSeek 400 事故复盘（2026-08-11）

> 状态：根因已定位，代码修复已落地并通过全部相关测试。
> 受影响会话：Feishu session_id=20（"Lucas 的 Lobster0" Owner DM），turn 284 / 287。
> 前置事故：[20260810_feishu-provider-protocol-400-incident.md](20260810_feishu-provider-protocol-400-incident.md)。
> 这是同一家族的**第二个**漏洞：上一次修的是"Provider-safe 过滤按 Turn ID 误删"，
> 这一次是"上下文窗口截断 + compaction 分支前缀补齐条件不足"，两者独立。

## 1. 症状

Owner 在飞书点了审批卡的批准按钮，卡片先变"正在验证权限并执行"，随后变红：

```
失败阶段：审批续跑
错误码：approval_callback_failed
原因：审批续跑未能完成。
调试编号：Approval #118 · Event ref #be18a24dd997
Tool 状态：执行状态未知；系统不会自动重试，请检查 ToolRun。
```

同一串审批里另有一张绿色卡片显示 `already_decided`（"这条审批已经处理，不会重复执行"）——
那张**不是故障**，是 Core 对重复点击的幂等拦截，只是文案容易被误读为报错。

## 2. 证据

`~/.miniclaw/miniclaw.db`（运行中的 Gateway home）：

| Approval | Tool | status | 续跑 Turn | Turn status |
| --- | --- | --- | --- | --- |
| 115 | propose_memory | consumed | 280 → 281 | waiting_approval |
| 116 | propose_memory | consumed | 281 → 282 | waiting_approval |
| 117 | propose_memory | consumed | 282 → 283 | waiting_approval |
| 118 | run_command | **consumed** | 283 → **284** | **failed** |
| 119 | edit_file | consumed | 286 → **287** | **failed** |

即：**审批本身成功了，Tool 也真的执行了**（`status = consumed`），炸的是批准之后那个
continuation Turn 的模型请求：

```
turn 284 / 287: error_code = provider_protocol
model provider rejected the request with status 400:
{"error":{"message":"Messages with role 'tool' must be a response to a preceding
message with 'tool_calls'","type":"invalid_request_error"}}
```

`feishu_approval.py` 的 `except Exception` 把这个异常整个吞掉、不写日志，所以
`~/.miniclaw/logs/gateway.stderr.log` 里一行痕迹都没有，只能从 `turns.error_message`
反查到 400 正文。（遗留项，见 §6）

### 2.1 复现

用运行中的代码（`.worktrees/phase6-feishu-production-design`）对真实库快照调用
`MessageRepository.list_context(20, limit=20)`，得到的正是非法序列——第一条业务消息就是
裸 `tool`：

```
837  system     （compaction 摘要，覆盖 377–609）
1430 tool  call_00_ReNhhuR2...   ← 它的 Assistant Tool Call 消息 1428 不在上下文里
1431 assistant call_00_WqrOF3k4...
1432 tool      call_00_WqrOF3k4...
...
1458 tool  call_00_9KYq5KXr...
```

而底层数据本身是完整合法的（`list_recent(20, limit=20)` 因为有 user 边界补齐，返回的
44 条里每个 Assistant Tool Call 都配对到了结果）：

```
1415 user
1416 assistant (3 calls) → 1417/1418/1419 tool
1420 assistant (3 calls) → 1422/1423/1424 tool   （1421 是 channel_notice，已过滤）
1425 assistant (1 call)  → 1427 tool
1428 assistant (1 call)  → 1430 tool             ← 跨 Turn：1428 属 turn 282，1430 属 turn 283
...
1456 assistant (1 call)  → 1458 tool
```

**坏历史不是落库出来的，是读取时被切出来的。**

## 3. 根因

### 3.1 `list_context()` 的 compaction 分支只按 `turn_id` 补齐前缀（核心）

`src/lobster0/storage/conversations.py` 的 `list_context()` 有两条路径：

- **无 compaction** → 走 `list_recent()`，它带"窗口首条不是 user 就往前补到最近一条 user
  消息"的补齐逻辑，能把 Assistant Tool Call 拉回上下文。**这条路径是好的。**
- **有 compaction** → 单独一段 SQL，取摘要覆盖范围之后的最近 `limit` 条，补齐时只补
  `turn_id == ordered[0].turn_id` 的更早消息。

而**审批续跑天生会把 `assistant(tool_calls)` 和它的 `tool` 结果拆进两个不同的 Turn**：
`create_continuation()` 会为批准后的执行新建 child Turn，结果消息落在 child 上。本次事故里
`1428` 属于 turn 282，它的结果 `1430` 属于 turn 283。

20 条窗口的边界正好落在这两条之间 → 补齐只在 `turn_id = 283` 里找 `id < 1430` 的消息 →
**一条都没有**（turn 283 的消息就是从 1430 开始的）→ Assistant Tool Call 永久缺席 →
历史第一条是裸 `tool` → DeepSeek 400。

一旦会话被压缩过（session 20 的摘要覆盖到 609），这层保护就退化了；没压缩过的会话反而没事。
这解释了为什么同一进程里别的会话是正常的。

### 3.2 `_provider_safe_context()` 只清理单向 orphan，兜不住

兜底过滤器只处理"Assistant 发起了 Tool Call 但结果缺失"这一个方向，
**没有反向规则**："`tool` 结果找不到对应的 Assistant Tool Call 就必须剔除"。
所以 §3.1 切出来的裸 `tool` 原样发给了 Provider。

### 3.3 部分回答的批次会自己造出裸 `tool`

同一个过滤器还有一个独立缺陷：当一批 Tool Call 只有部分拿到结果时，
`_invalidate_pending()` 会剔除那条 Assistant 消息，但**保留已经配对成功的 `tool` 结果消息**
（它们不在 `invalid_call_ids` 里）。Assistant 被删、结果留下 → 又是裸 `tool`。

这是上一次事故修复留下的边界：当时只保证"不误删无关 Turn 的消息"，没保证"删 Assistant 时
连带删掉它自己的结果"。

## 4. 修复

### 4.1 compaction 分支改为按 user 边界补齐（对齐 `list_recent`）

窗口首条不是 `user` 时，在"compaction 边界之后"的范围里往前找最近一条 `user` 消息，把
`[user, 窗口首条)` 之间的非 system 消息全部补回来；找不到 user 消息时不再强行补全整个
未压缩区间（那可能是几百条），交给 §4.2 的反向规则安全剔除。

补齐后的条数可能略多于 `limit`，与 `list_recent()` 的既有契约一致。

### 4.2 `_provider_safe_context()` 改为按 Assistant 批次结算

- 每条带 `tool_calls` 的 Assistant 消息开启一个批次，记录未回答的 `call_id` 和已回答结果的
  消息 ID。
- 批次结算时若仍有未回答的 `call_id`：剔除 Assistant 消息、**它已配对的全部 `tool` 结果**、
  以及紧邻的触发 User 消息；未回答的 `call_id` 记入 `invalid_call_ids`，用于剔除后续迟到的结果。
- 新增反向规则：`tool` 消息的 `tool_call_id` 不属于当前批次的未回答集合时一律剔除——覆盖
  "Assistant 被窗口/compaction 切掉"、"Assistant 已判定失效"、"同一 call 被重复回答" 三种情况。

## 5. 验证

- `tests/test_conversations.py` 新增两个真实序列回归用例：
  - compaction 之后审批续跑跨 Turn，窗口边界切在 Assistant 与其结果之间；
  - 一批 Tool Call 只有部分结果，Assistant 失效时结果不得残留。
- 既有用例 `test_context_keeps_unrelated_tool_result_when_a_later_batch_is_orphaned`
  （上一次事故的回归）必须继续通过。

## 6. 后续：`command_forbidden` 拒绝信息不可操作，导致 `loop_no_progress`

修复上线并把 `tools.mode` 改成 `yolo` 后，session 20 的 turn 295 出现新症状：Owner 只发了
"你现在在吗"，Agent 却连跑 11 步、最后以 `loop_no_progress` 停止。

### 6.1 这不是改名或本次修复引入的

- `~/.miniclaw/config.toml` 与 `~/.lobster0/config.toml` 逐行 diff，只差环境变量名、workspace
  路径和 `tools.mode`；`[permissions]`、`[tools]` 完全一致，**没有配置在迁移中丢失**。
- `command_forbidden` 最早出现在 `2026-08-07T21:47:01Z`，改名前 3 天就有，旧库累计 45 次。

### 6.2 真实原因

Claw Trail 里 3 个 ✕ 全部是 `policy/command.py` 的**硬禁止**，在 `normalize_command()` 阶段
就 DENY，早于任何权限模式判断，`yolo` 也绕不过：

- `env` 命中 `_FORBIDDEN_PROGRAMS`；
- `python -m …` / `python -c …` 命中 `_is_inline_evaluation`（成功的两步是
  `python tests/test_x.py`，直接执行脚本文件，不带开关）。

这两条规则本身是对的。问题在于回给模型的 `ToolResult.failure(code, decision.reason)` 里，
`reason` 只有 `"program is not allowed"` / `"inline code execution is not allowed"`——
**没有指出是哪个开关触发的，也没有给替代做法**。模型换个写法再试，再撞同一面墙，连续 3 轮
无新成功结果后被 `max_no_progress_iterations` 保护性中断。

### 6.3 修复

把三处 `CommandPolicyError` 的 message 改成"诊断 + 可执行的替代方案"，稳定 `error_code`
不变（渠道文案和审计仍按 `command_forbidden` 归类）：

- 硬禁止程序按类别给替代路径（shell/包装器 → 直接调目标程序；网络下载 → 用 `http_get`；
  文件破坏 → 用 `edit_file`/`write_file`；包管理 → 明确不可用）；
- inline 执行指出命中的具体开关，并要求改为执行脚本文件；
- 被禁 git 子命令指出是哪个子命令。

## 7. 遗留项（本次未修）

1. **审批频率**：`~/.miniclaw/config.toml` 里 `tools.mode = "safe"`，`edit_file` /
   `write_file` / `propose_memory` 这些 MEDIUM Tool 每次调用都要一张卡，`run_command`
   是 HIGH、除 `yolo` 外任何模式都必审（`policy/engine.py`）。一条任务链因此会连出
   #115–#119 五张卡。
2. **权限模式不持久化**：`PermissionState` 是进程内存态，`/permissions` 改完不写回
   config.toml，Gateway 重启即回落。且档位只有 `safe|smart|autopilot|yolo`，自然语言
   （"给你最高权限"）和不存在的档位名（"yellow"）都不会生效，也没有回显提示合法值。
3. **异常被静默吞掉**：`channels/feishu_approval.py` 的 `except Exception` 不记日志，
   400 正文只存在于 `turns.error_message`。
4. **`already_decided` 卡片文案**：幂等拦截显示成"处理失败"式的诊断块，容易被误读为故障。
5. **parent Turn 停在 `waiting_approval`**：审批消费后 parent 状态不推进，会一直作为
   `compaction_candidates` 的保护边界，阻止旧消息被压缩。
