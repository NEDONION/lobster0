# Feishu 卡片溢出回复与重启恢复设计

## 1. 目标

MiniClaw 在飞书中的正常成功回答以一张最终卡片为主。卡片正文采用飞书 Card JSON 2.0 支持的
`small`（12px）字号；当完整回答超过配置的 `message_max_chars` 时，卡片保存前缀，只有尚未展示的后缀进入
durable Outbox，并回复到机器人自己的卡片消息下方。

本设计同时关闭三个已知缺口：

1. 长回答不能因为 preview 上限而永久丢失尾部；
2. completed Turn 在进程重启恢复时不能追加一条重复的普通文本；
3. waiting approval 只能发布 Approval card，不能再创建普通回答卡片。

## 2. 范围与非目标

实现范围：

- Feishu progress/final card 使用 12px `small` 字号；
- 短回答只保留一张 completed card；
- 长回答把未进入卡片的后缀分片，并以 completed card 的平台消息 ID 作为 `reply_to`；
- final card 创建或更新失败时，完整正文仍回复原用户消息；
- restart recovery 使用既有稳定 progress UUID 重新取得同一张卡片，再幂等完成卡片和必要的后缀 Outbox；
- waiting approval 只创建 durable Approval delivery；
- 离线回归、工程文档、release record 和进度页同步。

非目标：

- 不提高飞书 API 的实际字符上限；字号只改变视觉密度；
- 不把完整正文复制到卡片和后续文本两处；
- 不新增飞书私有 API wrapper；
- 不在自动化测试中调用真实飞书或真实模型。

## 3. 回答状态与契约

`ExperienceOutcome` 继续告诉 `ChannelManager` 是否需要 durable delivery，并增加两个平台中立字段：

- `final_delivery_offset: int`：最终正文中尚未在 completed progress 内展示的起始字符偏移；
- `final_reply_to_message_id: str | None`：后续内容应回复的 progress 平台消息 ID。

状态矩阵：

| 终态 | 卡片 | Outbox 内容 | Outbox reply target |
| --- | --- | --- | --- |
| 成功且全文可容纳 | 完整 completed card | 无 | 无 |
| 成功但全文超限 | 前缀 completed card | 仅后缀 | card message ID |
| card create/update 失败 | 不可信或 incomplete | 完整正文 | 原用户 message ID |
| Provider/Turn 失败 | incomplete preview（若已创建） | 稳定失败提示 | 原用户 message ID |
| waiting approval | 无普通回答卡片 | Approval card | 原用户 message ID |

偏移只按 Python 字符切分，不按 UTF-8 字节切分，因此不会拆开 Unicode code point。Outbox 后缀继续复用
`split_message()`，保持飞书单条消息上限。

## 4. 重启恢复协议

`ChannelManager.start()` 将恢复流程改为异步。发现对应 Turn 已 completed 时：

1. 读取 SQLite 中完整 Assistant message；
2. 用原 Inbox key 派生与首次运行相同的 progress UUID；
3. 调用 `ExperienceActivity.finish()`；飞书官方发送请求的 `uuid` 负责幂等取得同一张卡片；
4. 再次更新同一卡片为 completed；
5. 根据 outcome 创建零条或仅后缀的 durable Delivery；
6. 最后把遗留 running Inbox 结算为 completed。

因此即使进程在远端卡片成功后、Inbox 结算前崩溃，下一次恢复也不会改发重复普通全文。若幂等卡片恢复失败，系统
退化为完整文本 Outbox，优先保证回答不丢失。

## 5. 飞书卡片样式

Feishu Transport 与旧 `ChannelCapabilities` compatibility renderer 都把 Markdown element 设置为：

```json
{
  "tag": "markdown",
  "content": "...",
  "text_size": "small"
}
```

标题、状态色和 Markdown 内容保持不变。`small` 是飞书 Card JSON 2.0 的 12px 枚举；不采用 10px
`x-small`，避免桌面端和移动端可读性显著下降。

## 6. 审批边界

真实 AgentRunner 在 tool-call/approval 轮不会发布最终 `model_text_delta`。Manager 看到
`TurnResult.approval_id` 时不得用 `result.content` 完成普通 progress；它只清理 typing，并让既有 durable Approval
delivery 成为唯一用户可见终态。测试 fake 也必须遵循这一生产契约。

## 7. 验证

回归测试至少证明：

- 刚好等于上限时只有 completed card；
- 超过上限时 outcome 返回正确 offset 和 card reply target，尾部不丢、不重复；
- card failure 时 offset 回到 0，完整正文 fallback；
- completed Turn 重启恢复不创建普通全文，并复用稳定 card UUID；
- waiting approval 不创建普通 progress card；
- 两个飞书 Card renderer 都输出 `text_size=small`；
- Python 全量、Ruff、29 条 Agent、32 条 Channel、20 轮 soak、文档校验和构建全部通过。

