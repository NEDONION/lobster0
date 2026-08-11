# Desktop D2a：定时任务从只读变可控（含表单新建）

> 日期：2026-08-11
> 文档类型：Milestone D2a 设计
> 状态：`DRAFT FOR REVIEW / IMPLEMENTATION PENDING`
> 上位规划：[对标 ClawX 能力差距与 Milestone 规划](2026-08-11-desktop-clawx-capability-gap-and-roadmap.md)
> 落地路线：[D1～D5 分 Phase 落地 §4.6](../../engineering/desktop/20260810_桌面多Agent分Phase落地.md)

## 1. 范围确认

上位规划里留了两个待拍板项，用户已明确：**两项都要**。对 D2a 的影响是
「界面表单新建定时任务」**纳入范围**（原规划建议不做）。

这个决定扩大了范围也提高了风险，本文档因此把新建表单单列一节（§6）详细设计安全边界，
而不是当成一个普通按钮。

## 2. 用户结果

- 自动化页有四张统计卡：总数 / 运行中 / 已暂停 / 失败，与 ClawX 对齐；
- 每个任务可**暂停 / 恢复 / 立即运行一次 / 取消**；
- 可查看单个任务的**历史运行记录**（状态、耗时、错误码）；
- 可在界面**新建定时任务**（表单）；
- 全局 E-stop（急停）可启用与解除，界面明确显示当前是否处于急停；
- 所有状态来自 Core 的 durable ledger，前端不造状态。

## 3. 现状（已查证）

| 能力 | Core 实现位置 | 现在能否从桌面端触达 |
| --- | --- | --- |
| 列表 | `automation.list` | ✅ 已有（只读） |
| 暂停 / 恢复 | `automation/repository.py` 的 `pause()` / `resume()` | ❌ |
| 立即运行 / 取消 / 运行历史 | CLI `task run` / `cancel` / `runs` | ❌ |
| 急停 / 解除 | CLI `task halt --reason` / `unhalt` | ❌ |
| 创建 / 更新 | `tools/automation.py` 的 `action: create/update`（**Agent 工具**，CLI 无对应子命令） | ❌ |

`schedule` 的结构已确认（`automation/parser.py:parse_schedule`）：

```python
{"kind": ..., "expression": ..., "timezone": ...}   # 仅这三个字段，多余字段直接报 schedule_fields
```

`kind` 四选一：`once` / `interval` / `cron` / `heartbeat`；`expression` 非空字符串；`timezone` 默认 `UTC`。
错误码稳定（`schedule_kind` / `schedule_expression` / `schedule_misfire` 等），可直接映射成界面提示。

## 4. Bridge 协议变更

`bridge/protocol.py` 的 `_REQUEST_TYPES` 追加，每个都要配 `_validate_payload` 分支：

| 类型 | payload | 对应 Core |
| --- | --- | --- |
| `automation.pause` | `{"taskId": int}` | `repository.pause()` |
| `automation.resume` | `{"taskId": int}` | `repository.resume()` |
| `automation.run` | `{"taskId": int}` | CLI `task run` 同一路径 |
| `automation.cancel` | `{"taskId": int}` | CLI `task cancel` |
| `automation.runs` | `{"taskId": int, "limit": int}` | CLI `task runs` |
| `automation.halt` | `{"reason": str}` | CLI `task halt` |
| `automation.unhalt` | `{}` | CLI `task unhalt` |
| `automation.create` | 见 §6.2 | `tools/automation.py` 的 create 路径 |

`client.hello` 的 capabilities 追加 `"automation_write"`。Desktop 据此决定是否渲染写操作控件——
capability 缺失时退回只读列表，不显示假按钮（沿用「后端没有就不显示」的既定原则）。

**校验一律在 protocol 层做**，不在 server 分支里临时判断：`taskId` 必须是正整数（复用 CLI
`_positive_cli_id` 的同款约束）、`limit` 有上界、`reason` 非空且有长度上限。

## 5. 写操作的安全门禁

| 操作 | 门禁 |
| --- | --- |
| 全部写操作 | `taskBusy`（有回合在跑）时禁用，沿用现有机制 |
| `run`（立即运行） | 会真实触发一次 Agent 回合并消耗预算 → 二次确认 |
| `cancel` | 不可逆 → 二次确认 |
| `halt` | 停掉所有自动化，影响面最大 → 二次确认 + 必填原因（Core 侧 `--reason` 本就必填） |
| `create` | 见 §6.4 |

## 6. 表单新建定时任务（用户要求纳入）

### 6.1 为什么它比其他操作危险

其他操作都是对**已存在**的任务做状态变更；`create` 是让用户从界面直接投喂一段**将被 Agent 反复
自动执行**的 prompt。风险点：

1. prompt 是自由文本，会在无人值守时按计划执行，且可能触发工具调用；
2. `schedule` 写错（比如 `interval` 写成 1 秒）会造成高频空转，烧 token；
3. Core 的创建入口原本只给 Agent 工具用，其参数经过 Agent 的理解与整形，直接暴露给表单意味着
   少了一层"翻译"。

因此表单不是简单地把 `tools/automation.py` 的参数原样搬到界面上。

### 6.2 `automation.create` 的 payload（刻意收窄）

```jsonc
{
  "name": "每日文档摘要",          // 1..64 字符，非空
  "prompt": "...",                 // 1..4000 字符，非空
  "schedule": {
    "kind": "cron",                // once | interval | cron | heartbeat
    "expression": "0 9 * * *",
    "timezone": "Asia/Shanghai"    // 可省略，默认 UTC
  }
}
```

**本次不开放的字段**（Core 支持但表单不给）：

- `skills`：需要先有 Skill 管理界面才谈得上选择，否则用户不知道有哪些可选；
- `delivery`：投递目标涉及 Channel 账号绑定，属于 Channels 功能页范围，默认 `route: "none"`；
- `budget`：默认值即可，暴露出来只会让用户填错。

这三项保持 Core 的默认值。**表单只覆盖「什么时候、跑什么」这两件事**，其余留给后续 milestone
或对话式创建。

### 6.3 界面对四种 schedule 的处理

不让用户裸写 `expression`——直接暴露 cron 表达式对非技术用户是灾难。表单分两层：

| kind | 界面呈现 | 生成的 expression |
| --- | --- | --- |
| `cron` | 预设选项（每天 HH:MM / 每周几 HH:MM / 每小时）+ 「高级」里才允许手写 cron | 由选择项拼接，或高级模式原样透传 |
| `interval` | 数字 + 单位（分/时/天），**下限 5 分钟** | 秒数 |
| `once` | 日期时间选择器 | ISO 时间串 |
| `heartbeat` | 不在表单里提供 | —— |

`heartbeat` 是系统内部用途（心跳），不该由用户从界面创建，表单不提供该选项；但 `automation.list`
仍然要能正确显示已存在的 heartbeat 任务。

**interval 下限 5 分钟是前端与 protocol 双重校验**——这是防止误配置烧钱的关键门禁，只在前端做
等于没做。

### 6.4 创建的安全门禁

- 表单提交前展示一次**确认摘要**：「将在 <人类可读的时间描述> 自动执行：<prompt 前 200 字>」，
  让用户确认自己真的理解这段 prompt 会被无人值守地反复执行；
- `expression` 的最终合法性由 Core 的 `parse_schedule` 判定，前端不复制那套解析逻辑——前端只做
  「明显非法」的即时提示（空值、interval 低于下限），真正的裁决权在 Core，错误码原样映射成中文；
- 创建成功后立即刷新列表，让用户看到 `nextRunAt`，确认时间符合预期。

## 7. Desktop 端设计

- 自动化页顶部四张统计卡：数字由**纯函数**从任务列表算出（`automationStats(tasks)`），可测试；
- 任务卡片：名称、调度描述、状态、下次运行时间、上次运行结果，右侧一组操作按钮；
- 「运行历史」在卡片内展开，而不是跳新页——历史条数有限（`limit` 有上界），无需独立路由；
- 急停状态用页面顶部的醒目条展示，而不是藏在某个按钮的状态里；
- 新建表单用独立面板（非模态对话框），避免在窄窗口下被挤压。

## 8. TDD 起点

**Python**

- protocol：每个新类型的 exact-key；`taskId` 非正整数/浮点/字符串一律拒绝；`limit` 越界拒绝；
  `halt` 缺 `reason` 或空白 `reason` 拒绝；
- `automation.create`：`name`/`prompt` 长度边界；`schedule` 多余字段被拒（Core 已有 `schedule_fields`）；
  `interval` 低于 5 分钟下限被拒；`heartbeat` 从 Bridge 创建被拒；
- server：busy 状态下写操作被拒；不存在的 `taskId` 返回稳定错误码；capability 未启用时不暴露。

**Desktop**

- `automationStats()` 纯函数：空列表、全部同状态、混合状态；
- capability 缺失时不渲染任何写操作控件；
- 破坏性操作（cancel/halt/run）必须经过确认才发请求；
- schedule 表单：四种 kind 各自生成正确的 expression；interval 下限提示；
- 错误码 → 中文提示的映射覆盖 Core 已定义的全部 schedule 错误码。

## 9. 退出条件

1. 四张统计卡显示真实数字；
2. 暂停/恢复/立即运行/取消真实生效，列表刷新后可见；
3. 运行历史可查看；
4. 可从表单新建 cron/interval/once 三类任务，并在列表里看到正确的 `nextRunAt`；
5. `heartbeat` 无法从界面创建；interval 低于下限被前后端双重拒绝；
6. E-stop 可启停，界面有醒目提示；
7. capability 缺失时优雅降级为只读；
8. Python + Desktop 全量门禁通过；真实 Electron + Bridge smoke 至少覆盖一次
   「新建 → 暂停 → 恢复 → 取消」往返。

## 10. 明确不做

- Skill 选择、投递目标、预算配置（§6.2）；
- 编辑已存在任务的 schedule/prompt（`update` 能力先不开放，避免与对话式修改产生两套真相）；
- cron 表达式的可视化编辑器；
- 任务执行日志的实时流式查看（现有 `runs` 是查询式，够用）。
