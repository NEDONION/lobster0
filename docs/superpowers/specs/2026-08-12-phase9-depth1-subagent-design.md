# Phase 9：depth-1 子 Agent 后端设计

> 日期：2026-08-12
> 文档类型：Phase 9 后端设计（D4 的前置）
> 状态：`BACKEND DONE`（2026-08-12）——后端 1～4 块已实现，剩 Bridge/UI（即 D4），见 §9
> 上位规划：[D1～D5 分 Phase 落地文档 §7](../../engineering/desktop/20260810_桌面多Agent分Phase落地.md)

## 1. 为什么先写这份

桌面版 D4（真实多 Agent）的落地文档第一句就是「必须先完成 Phase 9 depth-1 Sub-agent
后端，再开放 UI」。这份文档只解决一个问题：**Phase 9 到底要做多少，值不值得开工**。

## 2. 现状核查：四块基础已经存在

开工前先核对代码，结论比预期乐观——Phase 9 不是从零开始：

| 能力 | 现状 | 结论 |
| --- | --- | --- |
| 父子回合链 | `turns.parent_turn_id` 列**已存在**，`TurnRepository` 已支持 | 不需要新列 |
| 工具收窄 | `ToolContext.allowed_tool_names: frozenset \| None` **已存在且在用**（runtime、automation eval 都在传） | 子 Agent 的工具子集直接复用 |
| 预算模型 | `TaskBudget`（timeout / turns / tool_calls / in+out tokens / cost）**已成型**，自动化在用 | 子 Agent 预算直接复用，只需「不得放大」的校验 |
| 隔离执行 | 自动化已有 `source="automation"` 的独立 Session + durable TaskRun + lease + 重启恢复 | 子 Agent 是同一套机制的另一种触发方式 |

**真正缺的只有三样**：子 Agent 的注册与声明、父子 Run 的关联与结算、以及一个让主
Agent 发起子任务的工具。

## 3. 范围与不可放宽的边界

沿用落地文档 §7.3，一条不放宽：

- **max depth = 1**。子 Agent 不能再 spawn 子 Agent——这是防止指数级放大的唯一硬保证；
- 子 Agent **不能**外发消息、创建定时任务、安装 Skill、改 Policy、处理审批；
- 子 Agent 的工具、权限、Workspace、预算**只能是父的子集**，不能等于更不能放大；
- 默认**隔离上下文**：只传递父 Agent 明确写下的子目标，不复制父会话历史。

第 4 条是与「共享上下文」的取舍：共享看起来更聪明，但会把父会话里的敏感内容
无差别灌进子任务，且让上下文预算不可控。**明确选择隔离**，需要的信息由父 Agent
显式写进子目标。

## 4. 数据结构

### 4.1 子 Agent 声明

不做「Agent 注册表」这种大概念——那是 v2 的多 Agent 平台。depth-1 只需要
**一组受限的执行档**：

```toml
[[subagents]]
id = "researcher"
description = "只读检索与汇总，不改文件"
tools = ["read_file", "glob", "grep", "http_get", "read_artifact"]
max_turns = 4
timeout_seconds = 300
```

`tools` 必须是父 Agent 已启用工具的子集，加载时校验；越界即拒绝加载配置，不静默取交集
（静默降级会让用户以为自己配的生效了——D2b 的同款教训）。

### 4.2 父子 Run 关联

复用 `task_runs`，新增两列（一次迁移）：

```sql
ALTER TABLE task_runs ADD COLUMN parent_run_id INTEGER REFERENCES task_runs(id);
ALTER TABLE task_runs ADD COLUMN subagent_id TEXT;
```

不新建表：子 Run 的生命周期、lease、重启恢复与普通 Run 完全一致，复制一张表只会让
恢复逻辑分叉。

`depth` 不存列——**由 `parent_run_id IS NOT NULL` 推导**。存一个可以被写错的深度字段，
不如让它无法表达非法状态。

## 5. 主 Agent 如何发起子任务

新增工具 `delegate_task(subagent_id, goal, timeout_seconds?)`：

- `subagent_id` 必须在配置声明的列表内；
- **在子 Run 的 `ToolContext` 里，`allowed_tool_names` 取声明的工具集与父可用集的交集**，
  且强制去掉 `delegate_task` 自身——这是 max depth = 1 的实现点，比任何计数器都可靠；
- 预算取「声明值」与「父剩余预算」的**逐项最小值**；
- 同步等待结果（有超时），不引入并发 fan-out：并发要处理部分失败、预算分摊与取消传播，
  那是把 v2 的复杂度提前搬进来。

## 6. TDD 起点

- 子 Agent 的工具集越界（配置里写了父没启用的工具）→ 拒绝加载；
- 子 Run 的 `allowed_tool_names` 不含 `delegate_task`（depth-1 的硬保证）；
- 预算逐项不大于父剩余；
- 父 Run 取消时子 Run 一并取消，且不留 running 悬挂；
- 进程重启后父子 Run 的状态都能恢复；
- 子 Agent 产生的 Artifact 关联到**父会话**（否则右栏看不到），`origin` 记为 `agent_output`；
- 子任务失败不让父回合直接失败——父 Agent 应当拿到失败原因并自行决定下一步。

## 7. 工作量估计

| 部分 | 规模 |
| --- | --- |
| 配置与声明校验 | 小（复用 D2b 的配置校验模式） |
| 迁移 + Repository 父子字段 | 小 |
| `delegate_task` 工具与上下文收窄 | 中（安全边界集中在这里） |
| 取消传播与重启恢复 | 中（自动化那套 lease 机制可复用） |
| Bridge/UI（即 D4） | 中 |

比落地文档原先描述的「8 项前置」小，因为其中 4 项已经存在。**主要风险不在实现量，
而在边界**：depth-1、工具/预算只能收窄、上下文隔离，这三条一旦松动，后面很难收回。

## 9. 实施进度（2026-08-12）

| 块 | 状态 | 落点 |
| --- | --- | --- |
| 1. 子 Agent 声明与校验 | 已实现 | `config.py` 的 `[[subagents]]` 与 `_subagents()` |
| 2. 父子 Run 关联 | 已实现 | 迁移 0013 + `TaskRunRepository.enqueue_child/list_children` |
| 3. `delegate_task` 与收窄 | 已实现 | `tools/delegate.py` |
| 4. 取消传播与重启恢复 | 已实现 | `cancel_children` + `finish` 内联清理 |
| 5. Bridge / UI（即 D4） | 未开始 | |

### max depth = 1 最终落成三道，全部是结构性的

设计时只写了「靠 `allowed_tool_names` 去掉 `delegate_task`」一道。实施中发现单靠
它不够，补成三道：

1. **配置层**：`[[subagents]].tools` 里写 `delegate_task` 直接拒绝加载；
2. **工具集计算**：`subagent_tool_names()` 无论声明与父集写了什么，都把它从交集里去掉；
3. **运行期**：`delegate_task` 执行时复核 `context.allowed_tool_names`，已经在子 Agent
   里就拒绝——这一道防的是「工具集算对了但上下文被别处构造错」。

另有**持久层**的第四道：`enqueue_child` 在同一事务里检查父 Run 是否本身就是子 Run，
是则 `subagent_depth_exceeded`。它防的是绕过工具层直接调仓库。

四道都不是计数器。计数器要靠正确读写才生效，而「看不到工具」与「数据库拒绝」是
结构性的。

### 取消传播分两种，因为事实不同

- **没开跑的**（queued/claimed）→ `cancelled`，它们没有产生任何副作用；
- **已经在跑的**（running）→ `interrupted` 并附 `parent_run_cancelled`。它可能已经写过
  文件、发过请求，标成 cancelled 会让「它到底做过什么」这个问题永远没有答案。

清理**挂在 `finish` 上并与终态写在同一个事务里**，而不是靠调用方多调一次。`finish` 是
所有终态的唯一入口；放在调用方，早晚会有一条路径忘记调，留下永远跑不完的子 Run。

**重启恢复不需要新代码**：子 Run 与普通 Run 同表同机制，`recover_stale` 天然覆盖——
这正是 §4.2「不新建表」那个决定的回报。

### 两处实施中才确定的判断

**（a）工具集取交集，不是直接用声明。** 父自己可能已被上一层收窄（自动化档就会），
只有交集才是真实可用集。交集为空直接拒绝，而不是派一个什么都做不了的子 Agent。

**（b）`delegate_task` 合法但不默认启用。** 没声明 `[[subagents]]` 时它无处可派，
默认开着只是给模型多一个空工具。为此把「合法工具名」与「默认启用集」拆成
`KNOWN_TOOL_NAMES` 与 `BUILTIN_TOOL_NAMES`。

## 8. 明确不做

- 子 Agent 再 spawn（depth ≥ 2）；
- 并发 fan-out 与结果聚合；
- Agent 注册表 / 市场 / 动态安装；
- 子 Agent 自己的 Memory 空间（读父 Memory 的只读子集即可）。
