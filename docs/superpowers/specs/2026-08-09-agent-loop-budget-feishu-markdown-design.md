# Agent 自适应循环预算与飞书 Markdown 卡片设计

## 目标

修复两类已经真实复现的问题：

- 复杂任务在第 8 次模型调用仍需继续使用 Tool 时，被固定 `loop_limit` 突然终止。
- 飞书最终回答被逐行改成 bullet，导致标题、段落、引用、代码块和链接失去原有结构。

本轮不移除安全上限，也不改变 Tool Policy、Owner 权限或审批语义。

## 已确认行为

### 自适应循环预算

- `max_tool_iterations` 保留为向后兼容的软预算，默认从 8 调整为 32。
- 新增硬预算，默认 64；配置必须满足硬预算不小于软预算。
- 软预算到达时，如果最近仍有新颖且成功的 Tool 结果，Runner 自动继续到硬预算。
- Tool 名与规范化参数完全相同的重复调用不算新进展；连续三轮没有新颖成功结果时，提前以稳定错误码停止。
- 硬预算的最后一次模型请求不再提供 Tool schema，并注入不持久化的收口指令，要求模型只根据已有证据给出最终回答并明确未完成事项。
- Approval waiting、Provider failure、取消和 Policy deny 继续沿用现有终态，不被自动延长或重试。

这样既允许文档梳理、创建和多步查询使用 32～64 轮，也不会把重复 `help/inspect` 变成无界循环。

### 飞书 Markdown 渲染

最终回答不再统一添加 bullet。飞书 Card 2.0 的 Markdown 元素保留：

- 一级至六级标题和普通段落；
- 有序、无序与任务列表；
- 引用、分隔线、粗体、斜体和删除线；
- Markdown 链接、行内代码与 fenced code block。

Markdown 表格是唯一主动降级的块：二维键值表转换为 `**字段**：值` bullet，多列表格按每行一条
bullet 展开。表格识别不得进入 fenced code block，避免修改代码示例中的管道符。

最终回答不再转义反引号和反斜线；原始 HTML 会转义为文本，防止模型或用户正文生成飞书专用 HTML / mention。

## 数据流

1. `AgentRunner` 维护软预算、硬预算、成功 Tool 指纹集合与连续无进展计数。
2. 每次 Tool 完成后，根据 Tool 名、规范化参数和执行结果更新进展状态。
3. 到达软预算后，仅在持续取得有效进展时进入扩展区间；到达硬预算前切换为无 Tool 的最终请求。
4. `ProgressProjector` 继续消费相同的公开 RunEvent，不展示原始 Tool 参数或结果。
5. `feishu_cards` 对最终答案做块级 Markdown 规范化；轨迹和内部摘要仍使用严格转义。
6. 卡片超过 20 KiB 时，只在换行或 Markdown 块边界截断；若截在 code fence 内则补闭合 fence，返回的
   `visible_answer_chars` 仍对应原答案的精确前缀，剩余内容沿现有 durable tail 路径发送。

## 错误与诊断

- 连续无进展使用新的稳定错误码，并在终态卡显示停止阶段、连续无进展轮数、模型轮次、已执行 Tool 数和
  Turn/Event 调试编号；不显示参数、结果、Provider 原文或凭据。
- 硬预算前的最终收口若仍返回空内容，沿用 `empty_response`；Provider 异常沿用现有 Provider 分类。
- 软预算扩展与最终收口提示只存在于当次 Provider 请求副本，不写入对话历史。

## 配置与兼容性

- 默认配置、`init` 模板、环境变量、运行指南和 Agent Runner 工程文档同步为 32/64/3。
- 旧配置只包含 `max_tool_iterations` 时，该值作为软预算，硬预算取不小于软预算的默认值。
- 当前本机配置在代码合入后升级到新默认并重启 Gateway；配置文件不进入 Git。

## 测试

- Runner：32 轮前不扩展、有效进展可进入第 33～64 轮、重复指纹三轮提前停止、硬预算最后一轮无 Tool、
  Approval/取消不受影响。
- 配置：默认值、旧配置兼容、环境变量覆盖以及软硬预算关系校验。
- 飞书卡片：标题/段落/列表/引用/链接/inline code/fence 保留，表格只在 fence 外转 bullet，HTML 被转义。
- Overflow：不切坏 Unicode、列表前缀或 code fence，`visible_answer_chars` 与 tail 精确无重叠。
- 完整运行单元测试、Ruff、文档检查、Channel 20 轮 640-case soak 和 `git diff --check`。

## 非目标

- 不实现完整 CommonMark AST 或为所有 Channel 重写统一渲染器。
- 不支持无界模型循环。
- 不把模型私有推理、完整 Tool 参数或 Tool 原始输出放进飞书卡片。
