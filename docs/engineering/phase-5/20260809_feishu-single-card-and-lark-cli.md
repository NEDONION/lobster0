# v0.5.2 Stabilization：飞书单卡片回复与 lark-cli Skill 工程落地

> 文档性质：`HISTORICAL SNAPSHOT`。本文记录 v0.5.2 Stabilization 在 2026-08-09 发布时的实现与验证事实，
> 不作为当前运行时规则。

> 编号说明：历史材料曾称“Phase 5.2”；它是稳定化交付版本，不是新的架构 Phase。

> 状态：代码与离线回归已完成；修复后 Owner DM 单卡已确认，完整 15-case 仍为 LIVE PENDING
> 当前基线：671/671 Python tests、35/35 TypeScript tests、39/39 offline Agent cases、32/32 Channel cases、640/640 local soak
> 适用版本：v0.5.2 Stabilization

## 1. 这次到底修了什么

这轮处理的是三个在真实飞书 Bot 上才暴露出来的问题：

1. Gateway 使用官方 Channel SDK 时可能报 `This event loop is already running`，或者 WebSocket 已连接但
   `lobster0 gateway` 一直等不到 ready；
2. 一次正常回答同时出现绿色 `Lobster0 回复` 卡片和一条内容完全相同的普通文本；
3. 用户问“最近修改的飞书文档”时，Agent 口头回答“没有飞书 API 工具”，没有调用已经安装并登录的官方
   `lark-cli`。

在 v0.5.2 发布时，修复后的用户体验是：

- 飞书卡片正文使用 Card JSON 2.0 的 `small`（12px）字号；
- 消息到达后立即创建一张蓝色 Loading 卡，执行步骤持续更新同一 `message_id`；
- 正常回答能完整放入时，原卡片转为绿色并展示完整轨迹和答案；
- Provider 或 Tool Loop 失败时，原卡片转为红色并展示固定安全提示，不另发灰色失败消息；
- 卡片内最终回答统一渲染为 bullet points；Markdown 表格转换为键值条目，不依赖模型遵守排版提示；
- 回答超过 `message_max_chars` 时，卡片保存前缀，只有未展示后缀回复到机器人自己的卡片下方；
- 卡片创建或最终更新失败时才把完整正文回复原用户消息；
- completed Turn 重启恢复时复用稳定 progress UUID，不追加重复普通全文；
- waiting approval 保留 durable Approval card，运行卡进入未完成终态；
- Telegram / Discord 继续使用 preview + durable final text，不受飞书策略影响；
- 飞书业务查询不新增一套 API Tool，而是由 `feishu-lark-cli` Skill 教 Agent 使用现有
  `run_command(program="lark-cli", args=[...])`；
- Tool Call 流式分片中的空名称续片不会覆盖已经确认的合法 Tool 名。

`small` 只提高屏幕上的视觉密度，不改变飞书 API 的字符上限；真正超过上限的内容仍必须走“卡片前缀 + 后缀回复”。

### 当前行为补充（非 v0.5.2 快照）

当前实现已演进：最终回答保留 CommonMark 的标题、段落、有序/无序/任务列表、引用、分隔线、链接、强调、
行内代码和 fenced code block；只有 fence 外的真实 Markdown 表格会降级为可读 bullet 条目。当前单卡状态和
配置说明见 [README](../../../README.md) 与[本地运行指南](../../getting-started/20260807_本地运行指南.md)。

## 2. 两条“飞书能力”不要混为一谈

Lobster0 里的飞书有两层：

| 层 | 解决的问题 | 当前实现 |
| --- | --- | --- |
| Channel | 从飞书收到消息、把回答送回飞书 | official Channel SDK + WebSocket + Card |
| Business API | 查询文档、消息、日历、任务等真实业务数据 | Skill + `run_command` + official `lark-cli` |

只接通 Channel，不代表 Agent 自动知道每个业务 API 的命令。旧实现能够收发飞书消息，也能发现并启动
`lark-cli`，但缺少自然语言到 CLI argv 的说明，因此 Provider 选择了口头拒绝。

```mermaid
flowchart LR
    U["Owner 在飞书提问"] --> C["Feishu Channel"]
    C --> A["共享 AgentRuntime"]
    A --> S["激活 feishu-lark-cli Skill"]
    S --> R["run_command"]
    R --> P["Policy / Approval"]
    P --> L["official lark-cli"]
    L --> F["Feishu OpenAPI"]
    F --> L
    L --> A
    A --> C
    C --> U
```

这条链路刻意不增加 `feishu_search_documents`、`feishu_calendar` 等重复包装 Tool。以后扩展飞书业务域时，优先
更新 Skill 的命令映射；执行、安全、审批、超时、日志和 ToolRun 仍复用同一个 `run_command`。

## 3. 为什么之前会同时发卡片和文本

旧链路把进度卡和最终文本看作两次独立投递：

```mermaid
sequenceDiagram
    participant P as Provider
    participant E as Experience
    participant M as ChannelManager
    participant O as Delivery Outbox
    participant F as Feishu
    P-->>E: model_text_delta
    E->>F: create / update progress card
    P-->>M: final content
    E->>F: update card to completed
    M->>O: create final text delivery
    O->>F: send the same content as text
```

这个设计对 Telegram / Discord 是合理的：它们的 preview 完成后会提示“最终内容见下一条消息”。飞书卡片本身却已经
能够完整承载最终 Markdown，所以再发文本就是重复。

## 4. 新的单卡片状态机

`ChannelExperience` 新增平台装配参数 `progress_is_final`，默认是 `false`。只有飞书装配时设为 `true`。

```mermaid
flowchart TD
    EVENT["Inbound claimed"] --> CREATE["Create one blue Claw Trail card"]
    CREATE --> PROJECT["Safe AgentProgress projector"]
    PROJECT --> REASON["Drop model_reasoning and raw Tool data"]
    CREATE --> UPDATE["Coalesce safe step updates"]
    PROJECT --> UPDATE
    UPDATE --> RESULT{"Turn terminal state?"}
    RESULT -->|waiting approval| APPROVAL["Finish run card and send durable Approval card"]
    RESULT -->|failed| FAILED["Turn same card red with safe notice"]
    RESULT -->|completed| FINAL["Turn same card green and add final answer"]
    FINAL --> FIT{"20 KiB payload fits?"}
    FIT -->|yes| CARD["Single final card"]
    FIT -->|no| TAIL["Only unseen answer suffix to Outbox"]
```

关键语义：

- `model_reasoning`、原始 Tool 参数/输出、凭据和文件内容不会被保留或渲染；“过程摘要”只来自公开 preamble 与
  确定性状态文案，不代表隐藏思维链；
- 卡片最多展示 16 步，每字段最多 240 字符；trace metadata 最大 16 KiB，Card JSON 最大 20 KiB；
- `AgentProgress.final_answer` 保留原始正文的精确字符前缀，渲染层只改变视觉格式，不改变续发偏移；
- `ExperienceOutcome.final_delivery_required=false` 只会在最终卡片成功承载完整正文后出现；
- 超限但最终卡片成功时，Outcome 返回 `final_delivery_offset` 和 `final_reply_to_message_id`，Manager 只切出
  `content[offset:]`，不会重复卡片内前缀；
- `progress_is_final=true` 时，`start()` 在模型推理前立即建立蓝色 Agent 卡；没有 Tool 或流式 delta 时也复用该卡；
- Provider 在终态前失败时更新同一卡片为红色未完成态，并把固定安全提示写入卡内；
- 卡片创建失败或最终更新失败后，普通文本 fallback 仍存在；
- 即使 tool-call 响应同时带可见 content，waiting approval 也丢弃 preview，因此唯一终态是 durable Approval card；
- `ChannelManager` 只跳过普通 message delivery，不改变 Inbox、Turn、Assistant Message 和 Audit 的结算。

### 4.1 平台行为矩阵

| 场景 | 飞书 | Telegram | Discord |
| --- | --- | --- | --- |
| 正常回答 | 一张 Claw Trail completed card | compact trail + final text | compact trail + final text |
| 回答超过卡片上限 | 12px card 前缀 + 回复卡片的后缀分片 | preview + final text | preview + final text |
| 无流式 delta | 蓝色卡原地转为绿色 completed card | final text | final text |
| progress 失败 | text fallback | final text | final text |
| Provider 失败 | 同一卡片转为红色并展示安全提示 | incomplete preview + failure text | incomplete preview + failure text |
| waiting approval | 运行卡未完成 + durable Approval card | Approval text | Approval text / platform展示 |

### 4.2 当前持久性边界

Assistant 完整正文始终持久化在 SQLite `messages`，最终脱敏步骤写入同一消息的
`metadata_json.experience_trace`；Inbound/Turn/Audit 也会结算。短回答的成功卡片不额外创建普通 Outbox；长回答只把
卡片未展示的后缀写进 Outbox。重启恢复从 trace 重建同样的 Claw Trail，缺失或损坏的旧 trace 退化为答案单卡。

`ChannelManager.start()` 的恢复流程现在是异步的。发现遗留 running Inbox 对应 completed Turn 时，它读取完整
Assistant Message，用原 Inbox key 派生和首次运行相同的 progress UUID，再次 `create/update` 同一卡片。官方发送
请求的 UUID 与现有 Delivery retry 使用同一幂等原则：若远端卡片已经成功，恢复取得同一 message ID；若尚未发送，恢复
创建并完成它。之后才按 Outcome 幂等创建零条或仅后缀 Delivery，最后结算 Inbox。因此“远端卡片已成功、进程在
`mark_completed()` 前退出”不会再补发重复全文。若卡片恢复失败，系统仍以完整文本 fallback，优先保证正文不丢失。

## 5. Gateway lifecycle 修复

官方 `lark-channel-sdk 1.2.x` 在导入阶段会准备自己的 event loop。旧入口在 `asyncio.run()` 内第一次导入 SDK，导致
SDK 尝试管理已经运行的 loop。

当前 CLI 在进入 `asyncio.run(run_gateway(...))` 前调用 `_prime_gateway_channel_sdks()`。可选 SDK 不存在时不影响
本地 TUI；SDK 自身依赖损坏时返回稳定 `GatewayConfigError`，不输出 App Secret。

第二个问题是 SDK 的 `connect()` 属于前台阻塞入口；Lobster0 需要“连接 ready 后返回，继续启动 Manager / Delivery”。
`FeishuTransport.connect()` 现在优先调用公开的 `connect_until_ready()`，老 SDK 没有该方法时才兼容回退
`connect()`。

```mermaid
sequenceDiagram
    participant CLI
    participant SDK
    participant G as GatewaySupervisor
    CLI->>SDK: import before asyncio.run
    CLI->>G: asyncio.run(run_gateway)
    G->>SDK: connect_until_ready
    SDK-->>G: WebSocket ready
    G->>G: start Delivery + Manager
    G-->>CLI: Lobster0 gateway ready
```

## 6. `feishu-lark-cli` Skill

`lobster0 init` 现在会在文件不存在时创建：

```text
~/.lobster0/skills/feishu-lark-cli/SKILL.md
```

重复 `init` 不覆盖用户已经修改过的 Skill。`SkillLoader` 只读 frontmatter 参与匹配；当前 Query 命中后才加载正文，
并把 Skill 名、版本和内容 hash 写进 runtime snapshot。

对于事故 Query：

```text
你帮我看看我最近更改的飞书文档是哪两个
```

Skill 给 Provider 的确定映射是：

```json
{
  "program": "lark-cli",
  "args": [
    "drive", "+search", "--as", "user",
    "--query", "",
    "--edited-since", "30d",
    "--sort", "edit_time",
    "--page-size", "2",
    "--format", "json"
  ]
}
```

这里的安全边界没有变化：

- 子进程使用固定 Workspace、最小 PATH、Owner Home、stdin EOF、超时和 1 MiB 双流上限；
- 不经过 Shell，不接受管道、重定向或拼接字符串；
- 个人云数据使用 `--as user`，认证由 `lark-cli` 管理，Lobster0 不复制凭据；
- `safe` / `smart` 下未命中 exact rule 会生成参数绑定 Approval；
- 可信 Owner 的 `autopilot` / `yolo` 可在硬校验后自动运行；
- 群聊或非 Owner 即使进程处于 autopilot 也会降级到审批；
- 高风险 CLI 动作没有 Owner 的明确批准时不能追加 `--yes`。

## 7. Provider Tool Call 兼容修复

部分 OpenAI-compatible 流式端点会这样发送 Tool Call：

1. 第一片：`name="glob"`，arguments 只有前半段；
2. 第二片：`name=""`，arguments 继续传后半段。

空字符串不是新的 Tool 名，它只是兼容网关的续片占位。Provider 解析器现在只在 `name` 非空时更新聚合器；最终仍要求
必须存在合法 Tool 名和 JSON object 参数。数组、数字、非法 JSON 或从未提供名称仍然 fail closed。

## 8. 回归与验收

新增永久事故场景：

| ID | Query | 必须发生 | 禁止发生 |
| --- | --- | --- | --- |
| `FEISHU-LARK-DOCS-001` | 最近更改的两个飞书文档 | `run_command → lark-cli drive +search` | 声称没有 API、搜索本地 Workspace |

相关自动化覆盖：

- `tests/test_channel_experience.py`：立即 Loading、同 ID 更新、completed card、刚好上限、overflow offset/card target、失败收口与 fallback、无 delta；
- `tests/test_channel_manager.py`：成功/失败无卡外普通文本、后缀回复卡片、waiting approval、completed restart UUID 恢复；
- `tests/test_channel_capabilities.py` + `tests/test_feishu_transport.py`：两个 Card renderer 的 12px `small` payload；
- `tests/test_feishu_transport.py`：`connect_until_ready()`；
- `tests/test_cli.py`：SDK 在主 loop 前导入；
- `tests/test_context.py`：飞书 Query 激活 Skill；
- `tests/test_openai_compatible_provider.py`：空 Tool 名续片；
- `tests/test_eval_cases.py` + `personal.v1.jsonl`：直接 lark-cli argv 契约。

发布门禁：

```bash
uv run python -m unittest discover -s tests -q -b
pnpm --dir tui test
uv run lobster0 eval run --suite offline --root evals/scenarios
uv run lobster0 eval run --suite channel --root evals/scenarios
uv run lobster0 eval run --suite channel --repeat 20 --json --root evals/scenarios
uv run ruff check .
uv run python scripts/validate_docs.py
uv build
git diff --check
```

真实验收需要 Owner 单独确认两类外部动作：

1. 通过真实 Bot 再发送短回答与超长回答，人工确认短回答只有一张 12px 卡片，超长回答只在卡片下补充后缀；
2. 通过真实 DeepSeek 运行飞书文档查询，因为标题和 URL 会作为 Tool Result 发送给模型 Provider。

没有明确授权时，自动化门禁不能代替这两条 privacy-sensitive live evidence。
