# Phase 9：depth-1 子 Agent 后端设计

> 日期：2026-08-12
> 文档类型：Phase 9 后端设计（D4 的前置）
> 状态：`DRAFT FOR REVIEW / IMPLEMENTATION NOT STARTED`
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

## 8. 明确不做

- 子 Agent 再 spawn（depth ≥ 2）；
- 并发 fan-out 与结果聚合；
- Agent 注册表 / 市场 / 动态安装；
- 子 Agent 自己的 Memory 空间（读父 Memory 的只读子集即可）。
