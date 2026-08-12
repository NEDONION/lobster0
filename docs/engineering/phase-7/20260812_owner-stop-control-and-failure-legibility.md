# Owner 停止控制、预算放开与失败可读性工程方案

> 文档日期：2026-08-12
> 状态：**IMPLEMENTED**
> 触发事件：同一天上线的 `turn_deadline`（见
> `20260812_turn-deadline-and-feishu-restart.md`）在真实使用中立刻误杀了一次**正常**的长任务：
>
> ```
> 错误码: turn_deadline
> 原因: 本轮已用 98.1 秒，超过单轮 90 秒的墙钟预算，已在工具边界安全停止。
> 超时前正在执行: run_command python3 · 1 arg
> ```
>
> 紧接着又撞上第二种失败：
>
> ```
> 2. ✗ 访问公开网页  www.google.com
> 3. ✗ 访问公开网页  html.duckduckgo.com   · 20.0 s
> 4. ✗ 访问公开网页  www.bing.com          · 104 ms
> 错误码: loop_no_progress
> 原因: 连续多轮没有新的成功 Tool 结果，已停止重复执行。
> ```
>
> 这台 Gateway 跑在中国大陆云主机上，根本连不上 Google/DuckDuckGo/Bing。Agent 的行为是**合理的**
> ——换了三个搜索引擎——却被 `max_no_progress_iterations=3` 掐断。
>
> 第三张卡片是决定性的：
>
> ```
> 2. ✓ 访问公开网页 · 1.1 s    api.github.com
> 3. ✗ 访问公开网页 · 22.3 s   github.com
> 4. ✗ 执行命令   · 120.1 s   python3
> 错误码: turn_deadline
> 原因: 本轮已用 197.5 秒，超过单轮 90 秒的墙钟预算，已在工具边界安全停止。
> ```
>
> **90 秒的预算放任这一轮跑了 197.5 秒。** 第 4 步单独烧掉 120.1 秒——正好是
> `run_command` 的完整超时——而墙钟预算只在工具边界检查，插不进去。也就是说这条预算
> 同时做到了两件坏事：**杀掉正在正常推进的工作**（第 2 步是成功的），
> 又**根本没有真的约束住任何东西**（2.2 倍于它自己的预算）。
> 既有害又无效的限制不应该默认存在。

## 0. Owner 的原话与它的含义

> 我不希望你限制它的长程任务的时间和工具调用次数 都给我放开。只要让我能够随时停止即可，
> 我能不能在它执行的时候让它停止？？通过强控制

这句话把设计前提改了：

- **时间不是"人想停"的代理变量。** 一个真正的 Agent 应该被允许长时间工作；
  该不该停，由人判断，不由秒表判断。
- **人必须真的握着开关。** "停止"必须在 Turn **执行中**生效，事后登记等于没有。

于是本次改动分成三件事：放开预算（§1）、给 Owner 一个真正的停止命令（§2）、
让失败卡片说清楚**为什么**失败（§3）。另有一个独立 Bug：`/good`、`/bad` 找不到目标（§4）。

## 1. 预算：哪些是安全护栏，哪些只是任意上限

前一版把四条预算并列成"都要保留"。这次逐条重估，判据只有一条：
**关掉时间限制以后，还剩什么能保证一个卡死的 Turn 一定会结束？**

| 预算 | 旧默认 | 新默认 | 性质 | 理由 |
| --- | --- | --- | --- | --- |
| `agent.max_turn_seconds` | `90` | `0`（= 不限时） | **任意上限** | 墙钟时间与"任务是否在正常推进"无关。机制保留，运维想封顶就设正整数。 |
| `agent.max_iterations` | `32` | `200` | **软收口点** | 到点时发一轮"不带工具、请给最终答案"的收口请求；32 轮对长任务太少。 |
| `agent.hard_max_iterations` | `64` | `400` | **安全护栏（保留有限）** | `for iteration in range(1, loop_limit + 1)` 是 `AgentRunner.run` 会终止的**唯一结构性保证**。绝不能设成无限。 |
| `agent.max_no_progress_iterations` | `3` | `12` | **安全护栏（放宽）** | 见 §1.1。 |

### 1.1 `max_no_progress_iterations`：为什么是"调大"而不是"变聪明"

事故里 3 次不同搜索引擎失败 = 3 轮无进展 = 直接终止。3 显然太小：瞬时网络故障是常态。

考虑过的替代形状是"区分**同一个调用以同样方式反复失败**与**不同调用以不同方式失败**"，
只对前者计数。**否决**，理由是它会打掉终止保证：只要模型能不断产出**新的**失败方式
（换一个 host、换一个参数），计数器就永远清零，这个循环就再也不会因为无进展而停下来。
在墙钟预算已经关掉的前提下，这等于把 `hard_max_iterations` 变成唯一的刹车——正是不能接受的那种
"真正停不下来的 Turn"。

同时注意：**字面重复已经被另一条机制覆盖**。`AgentRunner` 的 `attempted_tool_fingerprints`
对 `(工具名, 规范化参数)` 完全相同的调用直接返回 `duplicate_tool_call` 而不执行，
所以"同一个调用原样重试"本来就不会真的重复产生副作用。

结论：保留"只有真正成功的 Tool 结果才清零"的严格语义（终止性由此保证：
无进展轮数单调递增，至多 12 轮必然抛出），只把常数从 3 调到 12。
12 轮连续零成功是很强的卡死信号，同时给合理的重试与探索留出足够空间。

### 1.2 关掉时间限制后，还剩什么在保护系统

1. `hard_max_iterations=400`——循环上界，结构性终止保证。
2. `max_no_progress_iterations=12`——无成功结果时的单调计数终止。
3. **单条工具自身的超时**——`run_command` 默认 30s / 上限 120s，`http_get` 20s。
   任何一条工具都不可能永久挂起。
4. **Owner 的 `/stop`**（§2）——现在这才是"人想停"的正确表达。
5. Automation 路径不受影响：后台任务仍由 `automation/runner.py` 的
   `asyncio.timeout(budget.timeout_seconds)`（默认 600s）外层封顶，
   且 `loop_limit = min(hard_max_iterations, budget.max_turns)`。

**残留风险（必须写下来）**：一个 Turn 现在最长可以跑
`400 轮 × 每轮 Provider 时间 + 工具时间`。实践中会先撞上上下文窗口——单个 Turn 内
`messages` 只增不减，几百轮后 Provider 会返回上下文超限错误，那也是一条终止路径，
但它表现为 `provider_*` 失败而不是干净的预算收口。这是本次取舍的已知代价。

### 1.3 `max_turn_seconds = 0` 的语义

`0` = 不限时，是**关闭开关**而不是"零秒"。选 `0` 而不是 `None`：
TOML 和环境变量都表达不了 `None`，而 `0` 在两边都是同一个字面量，
`LOBSTER0_MAX_TURN_SECONDS=0` 和 `[agent] max_turn_seconds = 0` 含义一致。
负数仍然拒绝。`AgentRunner` 里 `deadline_active = budget is None and self._max_turn_seconds > 0`，
**机制一行未删**：运维设成 `300` 就还是原来那套工具边界检查。

## 2. Owner `/stop`：在 Turn 执行中真的停下来

### 2.1 复用已有的取消链路，不新建第二套

取消机制已经存在，本次只是给飞书接上一个入口：

```
/stop  →  ChannelManager._request_stop(conversation)  →  turn_task.cancel()
       →  asyncio.CancelledError 从 await 抛出
       →  TurnService.handle_inbound 的 except CancelledError:  self._turns.cancel(turn.id)
                                                               emit("turn_cancelled")
       →  ChannelManager._process 的 except CancelledError:  投递"已停止"诊断
```

`TurnService` 那两个 `except asyncio.CancelledError` 分支（`turn.py` 的 `handle_automation`
与 `handle_inbound`）是既有代码，一行没改。桌面端 `cancelTurn()` →
`desktop:task:cancel` → `turn.cancel` → `BridgeServer._cancel_active()` → `task.cancel()`
走的是同一个终点。

### 2.2 关键：`/stop` 必须绕开 Conversation Lock

`ChannelManager._process` 的第一件事就是 `async with self._conversation_lock(...)`。
如果 `/stop` 走正常的 Worker→`_process` 路径，它会**排在正在跑的那个 Turn 后面等锁**
——等到它拿到锁，要停的东西早就跑完了。这正是"事后登记等于没有"的那个陷阱。

因此取消动作放在 `ChannelManager.receive()` 里，也就是 Transport 收到 webhook 的那一刻：

- 不占 Worker，不等锁，不受队列长度或 `worker_count` 影响
  （`worker_count=1` 时 Worker 正卡在那个 Turn 里，`/stop` 照样立即生效）。
- 入站事件照常持久化（审计与去重不变），随后仍然进入队列，
  由 `_control_notice` 生成**确认消息**。确认因此天然排在"Turn 真的停下来"之后，
  Owner 看到的顺序是：Turn 停 → "已停止本轮任务"。

### 2.3 权限：不新增授权路径

`_stop_notice` 与 `/reset`、`/permissions`、`/restart` 用**同一个** `_trusted_owner(event)`：
外部身份等于配置的 Owner **且** `chat_type == "p2p"`。群里任何人喊 `/stop` 都不会取消任何东西。
`receive()` 里的即时取消分支同样先过 `_trusted_owner`，非 Owner 的 `/stop`
只会在正常路径上拿到一句"只有 Owner 私聊可以停止当前任务。"。

### 2.4 Worker 必须活下来

`_worker` 现有的 `except asyncio.CancelledError: raise` 是给关停用的。
如果 Owner 的 `/stop` 让 `CancelledError` 一路穿到 `_worker`，整个 Worker 就死了。
所以 `_process` 用 `self._stop_requested`（由 `_request_stop` 显式置位）区分两种取消：

- **Owner 停止**：吞掉 `CancelledError`，投递诊断，`return`。Worker 继续消费队列。
- **关停/重启**：标志位没置，按原路 `raise`，行为与之前完全一致。

被取消的是内层的 `turn_task`（`asyncio.ensure_future` 包出来的独立 Task），
Worker Task 本身从未被 cancel，因此吞掉是安全的，不需要 `uncancel()`。

### 2.5 其他界面要不要也加

**结论：不需要，飞书是唯一缺开关的界面。**

- 桌面 / Web 控制台：都经由 Bridge，`turn.cancel` 早就存在（`BridgeServer._cancel_active`）。
  Web 控制台是同一个 Bridge 协议的 Node 前端，直接继承。
- TUI：`_run_turn` 在后台 Task 里跑，界面 cancel 该 Task 即走同一条 `CancelledError` 路径。
- CLI：单 Turn 前台执行，`Ctrl-C` 已经把 `KeyboardInterrupt`/`CancelledError` 送进同一条路径。

## 3. 失败卡片必须说清楚"为什么"

事故卡片给了三个 ✗ 和一句"连续多轮没有新的成功 Tool 结果"，用户无法区分
**网络被墙** / **工具坏了** / **模型在打转**。信息其实**存在**却被丢掉了：
`ToolExecutor` 每次失败都有 `result.error_code`（本例是 `http_failed`），
但 `tool_finished` 事件只带了 `preview`，没带 `error_code`，
`ProgressProjector` 于是只能把所有失败画成一个 ✗。

改动两处：

1. `ToolExecutor` 的 `tool_finished` 事件补上 `error_code`（失败时才有）。
   这是**已有的稳定 snake_case 码**，不是异常正文。
2. `ProgressProjector._set_tool_status` 把失败的 `error_code` 映射成一句中文原因，
   写进该步骤的 `detail`。

脱敏标准与 `tool_display_summary` 一致：只公开稳定错误码派生的固定短语，
不含凭据、不含超出卡片已展示范围的 URL、不含任何响应正文。未知码退化为
"调用失败（`<code>`）"，`code` 先过与 `_safe_error_code` 同级的字符白名单。

事故卡片改动后读作：

```
2. ✗ 访问公开网页  www.google.com        · 网络请求失败，可能是超时或出站被阻断
3. ✗ 访问公开网页  html.duckduckgo.com   · 20.0 s · 网络请求失败，可能是超时或出站被阻断
4. ✗ 访问公开网页  www.bing.com          · 104 ms · 网络请求失败，可能是超时或出站被阻断
```

三条同因 + 20.0s（超时）与 104ms（瞬时拒绝）的对比，足以让人判断这是出站访问问题。

## 4. Bug：`/good`、`/bad` 报"没有找到这条回答"

### 4.1 根因（不是猜测，是代码路径）

`ChannelFeedbackController` 靠
`deliveries.find_sent_by_platform_message_id(kind="card")` 把 Owner 回复的那张卡片
反查回内部 assistant message。写入这条映射的是
`ChannelManager._record_card_delivery`，而它**只在 `_create_result_delivery` 里被调用**
——也就是**只有成功路径**。

失败路径（`_process` 的 `except Exception` 分支）调的是 `_create_failure_delivery`，
它只 `create_channel_notice` + `create_parts(kind="message")`，
**从不登记 `kind='card'` 的映射**。于是 Owner 在 IM 里看到的那张失败卡片，
在 `deliveries` 表里根本没有对应行，反查必然 `None` → `target_not_found`。

`experience.py:295` 那条"真实事故"注释修的是同一类问题的成功分支；失败分支被漏掉了。

同一个缺口也存在于控制命令提示与反馈提示走的 `_create_notice_delivery`。

### 4.2 修法

让失败卡片**可被反馈**，而不是让命令解释自己为什么不行——对失败的回合打 `/bad`
恰恰是最有价值的反馈，不该被拒绝。

- `_create_failure_delivery` 接受 `progress_message_id`，创建内部通知消息后
  调用同一个 `_record_card_delivery` 建立映射。
- 失败路径即使 `final_delivery_required=False`（卡片已完整展示、无需再发文本），
  也仍然创建内部通知消息并登记映射，否则又会退回"卡片无处可查"。
- `target_not_found` 的兜底文案同时改写：明确列出"这条不是 Lobster0 的回答 /
  是更早版本发出的卡片"两种可能，不再留死胡同。

`create_channel_notice` 写入的 role 本来就是 `assistant`，
反馈控制器的 `role == "assistant"` 校验无需放宽。

## 5. 测试策略

全部离线、确定性、注入时钟，不 sleep：

- **预算**：断言新默认值；断言 `max_turn_seconds=0` 时超长 Turn **不**被杀；
  断言设成正整数时机制仍然按原样触发。
- **`/stop` 在执行中生效**：Fake Provider 在第一次 `complete` 里
  `await` 一个测试持有的 `asyncio.Event`，Turn 因此真的停在半途；
  测试从另一侧调 `receive()` 投递 `/stop`；断言 Turn 被取消。
- **持久化一致性**：停止后**读回数据库**，断言 Turn 状态为 `cancelled`、
  已完成的 Tool 批次仍在、每个 `tool_call_id` 都有配对的 tool 消息。
- **Worker 存活**：停止后再投递一条普通消息，断言它被正常处理。
- **非 Owner**：群消息 `/stop` 与非 Owner 私聊 `/stop` 都不得取消任何 Turn。
- **失败卡片可反馈**：失败路径跑完后，用失败卡片的 platform message id 发 `/bad`，
  断言反馈被记录。
- **失败原因可读**：`tool_finished(error_code="http_failed")` 后，
  断言对应步骤 detail 含网络失败原因且不含原始正文。
