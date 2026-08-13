# Memory add/update Candidate：为进化提案找到合法的记忆来源

> 文档日期：2026-08-13
> 状态：**PARTIAL（2026-08-13）：update 已实现，add 被迁移器能力挡住，见第 9 节**
> 前置：Phase 7 Task 3 已实现 `build_memory_forget_candidate`；Memory Autopilot A～E 已实现

## 1. 这个缺口是什么

Phase 7 的受控进化允许三类目标：Prompt、Skill、Memory。前两类都能被完整提案，Memory 只做了
`forget`——只能提议**忘掉**一条记忆，不能提议**新增**或**改正**一条。

`20260810_controlled-evolution.md` 第 555 节把原因写清楚了，这里原样引用，因为它至今成立：

> `propose_correction`（add/update）需要真实对话里的 `SourceRef` 和 Owner 明确纠错意图匹配，
> 与 Evolution 由 `/good`/`/bad` 反馈发起的场景不自然吻合；在没有想清楚这种情况下的正确语义前，
> 没有为了"看起来功能齐全"而硬凑一个假 SourceRef，留作后续单独设计的缺口。

留白是对的。本文档的任务就是把"正确语义"想清楚，而不是补一个能编译通过的空壳。

## 2. 为什么不能直接复用现成方法

### 2.1 记忆的来源必须是可核验的真实消息

`SourceRef` 的设计（`src/lobster0/memory/models.py:36`）刻意只接受内部整数 ID：

```python
message_id: int
session_id: int
channel: str
```

`__post_init__` 拒绝零、负数和 bool；`_validate_sources`
（`src/lobster0/memory/repository.py:1209`）再用一次 JOIN 确认这条消息**确实存在**、**属于该 Owner
的会话**、**渠道一致**。三道校验的目的只有一个：模型不能凭空捏造一条记忆的出处。

这意味着任何 add/update candidate 都必须指向一条**已经落库的消息**。

### 2.2 但 `/bad` 的原因文本根本没有落库

`/bad <原因>` 是控制命令，在 `ChannelManager` 里被 `_handle_feedback_command`
（`src/lobster0/channels/manager.py:778`）拦截，**不进模型、也不写 `messages` 表**。
Owner 说的那句话只以一个脱敏字符串留在 `feedback.redacted_reason` 里。

`feedback` 表（`0007_controlled_evolution.sql:5`）唯一的消息外键是：

```sql
message_id INTEGER NOT NULL REFERENCES messages(id),
```

指向的是**被评价的那条助手消息**，不是 Owner 的纠错话。

于是形成一个死结：唯一现成的 message_id 语义是错的，语义对的那句话没有 message_id。

### 2.3 用被评价的助手消息当来源是错的

技术上可行——`_validate_sources` 不校验消息角色，助手消息能通过。但那会让记忆的出处变成
"这条事实来自模型自己答错的那句话"。记忆系统里最不能骗人的字段就是出处；为了让功能"看起来齐全"
而写一个假出处，比缺这个功能坏得多。**否决。**

## 3. 决定：把 Owner 的纠错话落库成真实消息

`/bad 你记错了，我的部署机器是 mac 不是 linux` 里的那句话，是 Owner **真的在那个会话里说过的**。
它现在被丢掉纯属实现遗漏，不是设计意图。补上它，三道校验自然全部通过，出处也真实：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `role` | `user` | 确实是 Owner 说的 |
| `session_id` | 被评价消息所在会话 | 同一场对话 |
| `channel` | 该会话的渠道 | 与 disclosure 一致 |
| `metadata_json` | `{"feedback_reason": true}` | 标明它由 `/bad` 产生，供审计与展示区分 |

落库带来一个副作用：这句话会进入下一轮的对话历史。**这是正确的**——Owner 确实说了，模型本来就该
知道。把 Owner 的话藏起来不给模型看，才是反直觉的那一边。

## 4. 意图门槛照旧生效，且正好合用

`propose_correction` 要求 `_CORRECTION_INTENT` 命中 Owner 原话；`remember_explicit` 要求
`_EXPLICIT_INTENT`。这两道门在这里不是障碍，而是**恰好需要的筛子**：

- `/bad 这个回答太啰嗦了` → 没有纠错意图 → 不产生 Memory candidate，退回 Prompt/Skill 目标；
- `/bad 你记错了，我的部署机器是 mac` → 命中纠错意图 → 产生 Memory update candidate。

也就是说：**不是每个 `/bad` 都该改记忆**，只有读起来确实在纠正一条事实的才该。沿用既有正则，
不为 Evolution 单独放宽——放宽等于给模型开一条绕过 Owner 意图的路。

## 5. add 与 update 的不对称

| | 复用的既有方法 | 已有 review_type | 是否需要新东西 |
| --- | --- | --- | --- |
| update（改正一条已有记忆） | `MemoryReviewService.propose_correction` | `correction` | 否，纯接线 |
| add（新增一条记忆） | `MemoryService.remember_explicit` | 无合适取值 | 是，见下 |

`remember_explicit` 对**普通事实**直接写成 `status='active'`，不创建 review
（`src/lobster0/memory/service.py:122`）——只有行为规则或冲突才进 review。

这与受控进化的根本约束冲突：**Agent 可以提案，但永远不能自己批准或应用**。一条经由进化管线提出的
新记忆，必须无条件停在待审状态，等 Owner 批准。

`memory_reviews.review_type` 的 CHECK 约束（`0003_memory_autopilot.sql:129`）目前只允许
`sensitivity / conflict / behavior / correction / forget / weekly`，没有一个能表达"Owner 通过进化
管线提议新增一条普通事实"。因此 add 需要：

1. 迁移 v14：把 `addition` 加进 `review_type` 的 CHECK 白名单；
2. `MemoryReviewService.propose_addition`：与 `remember_explicit` 共用校验与 Markdown 写入，
   但**无条件**落 `review_required` + 建 review，不走"普通事实直接激活"的分支。

不复用 `behavior` 或 `conflict` 冒充：那会让审批列表里出现语义错误的条目，Owner 看到的理由与
实际发生的事对不上。

## 6. 落地顺序

| 步骤 | 内容 | 依赖 |
| --- | --- | --- |
| 1 | `/bad` 原因落库为 user message（带 `feedback_reason` 元数据），`feedback` 表加 `reason_message_id` | 迁移 v14 |
| 2 | `build_memory_correction_candidate`：接 `propose_correction` | 步骤 1 |
| 3 | 迁移 v14 追加 `addition` review_type + `propose_addition` | — |
| 4 | `build_memory_addition_candidate` | 步骤 3 |
| 5 | Evaluator 与 apply/rollback 接线（复用既有 memory review 分支） | 2、4 |

步骤 1 与 3 落在同一个迁移 v14 里，避免连开两个版本。

## 7. 安全边界（不变）

- 不复制 Memory 正文进 Evolution 的 manifest：manifest 只放 `unit_id` / `review_id` /
  `review_type`，正文留在既有 Memory 表里，与 forget candidate 的做法一致；
- `candidate_hash` 仍然直接取既有 `preview_hash`——它已经绑定了 Unit 内容与请求跳转，
  另造一个哈希只会多一处可能对不上的地方；
- disclosure 规则、promotion、conflict、reconcile 全部继续走 Memory Service 的唯一实现，
  Evolution 不复制其中任何一条判断。

## 9. 实现结果（2026-08-13）

### 已完成：update

| 改动 | 位置 |
| --- | --- |
| 迁移 v14：`feedback.reason_message_id` | `0014_feedback_reason_message.sql` |
| Owner 原话落成 user message | `MessageRepository.create_feedback_reason` |
| `/bad` 落库原话并绑定到反馈 | `channels/feedback_commands.py` |
| update 候选构造器 | `build_memory_correction_candidate` |

与设计的**一处偏离**：`DisclosureContext` 改为由调用方整体传入，而不是在构造器里按
`owner_id` + `channel` 拼。原因是更正可以从飞书发起，而"这是私聊还是群聊"、"身份验没验过"
只有真正处理那条消息的地方知道；在构造器里一律写死 `direct` + `identity_verified=True`，
等于让群聊里的 `/bad` 伪装成私聊，绕过"群聊不得写入记忆"。

### 实际使用时的门槛

既有 `_CORRECTION_INTENT` 只认 `更正 / 纠正 / 改成 / 更新记忆 / correct / update memory`。
因此 `/bad 你记错了，我的部署机是 mac` **不会**产生记忆更正提案，得写成
`/bad 更正：我的部署机是 mac`。

按第 4 节的立场不放宽这道正则——它是 Owner 明确意图的唯一凭证。但这意味着功能默认很少触发，
Owner 需要知道该怎么措辞。**这一点必须写进用户可见的帮助文本**，否则就是个沉默的功能。

### 未完成：add，以及被什么挡住

第 5 节要求为 add 新增 `addition` review_type。SQLite 改 CHECK 约束必须重建表，而
`memory_audit.review_id` 有外键指向 `memory_reviews`；现有迁移器在
`BEGIN IMMEDIATE` 里执行所有语句，事务内 `PRAGMA foreign_keys=OFF` 是静默 no-op
（已实测），于是 `DROP TABLE memory_reviews` 直接被外键拦住。

要做 add，得先让迁移器支持 SQLite 官方的表重建流程（`PRAGMA foreign_keys=OFF` 必须在
`BEGIN` **之前**执行，提交前再跑一次 `PRAGMA foreign_key_check`）。那是对数据完整性
最敏感的一块基础设施，不该作为本任务的顺手改动塞进来——今天已经有两次"顺手改动"
把别人的功能整段冲掉的事故。留作独立任务。

## 8. 明确不做

- 不让模型自行决定"该记住什么"然后走进化管线批量入库——本文档只覆盖 Owner 明确表达纠正/记住
  意图的那条路；
- 不放宽 `_CORRECTION_INTENT` / `_EXPLICIT_INTENT`；
- 不为 Evolution 复制一套 Memory 写入逻辑。
