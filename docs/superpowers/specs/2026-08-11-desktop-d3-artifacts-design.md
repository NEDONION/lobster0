# Desktop D3：共享产物（含 D2c 附件缺口修补）

> 日期：2026-08-11
> 文档类型：Phase D3 产品、协议与安全设计
> 状态：`IMPLEMENTED`（2026-08-12）
> 大纲来源：[分 Phase 落地文档 §6](../../engineering/desktop/20260810_桌面多Agent分Phase落地.md)
> 前置实现：[D2c 附件](2026-08-11-desktop-d2c-attachments-design.md)（`IMPLEMENTED`，但存在本文 §1 记录的缺口）

## 1. 必须先修的 D2c 缺口

设计 D3 前重新核查代码，发现 **D2c 的附件只走完了一半**：

`bridge/server.py` 的 `turn.start` 分支里，`attachment_ids` 被用于校验，然后就被丢弃：

```python
for item in attachment_ids:
    self._staged_attachments.pop(item, None)   # 只是消费掉
...
self._runtime.service.handle(owner_id, text, session_key, on_event=...)  # 没有附件
```

后果有三层，一层比一层严重：

| 层 | 现状 | 后果 |
| --- | --- | --- |
| 持久化 | 未写入消息的 `metadata_json`（D2c 设计 §3.5 明确要求） | 重新打开会话看不出这条消息带过文件 |
| 模型可见性 | `TurnService.handle()` 没有附件参数 | **模型完全不知道用户发了文件** |
| 内容可达性 | 没有任何 Tool 能读取 Artifact 正文 | 即使模型知道有文件，也读不到内容 |

也就是说：今天点附件、选文件、发送，全程不报错，但 Agent 那边什么都收不到。

**这是退出条件写法的问题，不只是实现遗漏。** D2c 的 6 条退出条件里没有一条是「Agent 能真正
用上这个附件」，所以测试全绿却漏掉了功能本身。本文档的退出条件必须显式包含端到端可用性。

修补放在 D3 而不是单独补一次 D2c，因为它需要的「Artifact ↔ 会话关联」正是 D3 展示产物所需的
同一份数据——分两次做会把同一张表改两遍。

## 2. 现状核查（决定工程量）

| 能力 | 现状 | 结论 |
| --- | --- | --- |
| `ArtifactStore` 查询 | 只有 `read_metadata(artifact_id)`，**没有 `list()`** | 需要新增有界列表查询 |
| `artifacts` 表关联 | 只有 `owner_id`，**没有 session/turn 列** | 需要迁移；见 §3.1 的取舍 |
| Artifact 正文读取 | 只有内部 `_read_staging`；对外只有 `to_tool_payload()`（仅 id/hash/类型/大小） | 预览需要新增受控读取 |
| `TurnService.handle()` | `(user_id, text, conversation_id, on_text, *, on_event)` | 需要新增可选附件参数 |
| 消息 `metadata_json` | 列已存在，`experience_trace` 在用 | 附件引用可复用，不需要新列 |
| Store 可用性 | D2c 已改为无条件构造并挂在 `AgentRuntime.artifact_store` | D3 直接可用 |

## 3. 数据关联

### 3.1 Artifact 归属哪个会话

「切换任务时右栏只展示对应任务的产物」要求 Artifact 能按会话过滤。两条路：

**方案 A：给 `artifacts` 加 `session_id` 列。** 直接，但 Artifact 是 content-addressed 且
**跨会话去重**的——同一份文件在两个会话里发，`artifact_id` 相同，一行记录塞不下两个归属。
把 `session_id` 放进去等于破坏去重语义，或者被迫放弃去重。不采用。

**方案 B（采用）：新增关联表 `artifact_links`。** 一条 Artifact 可以属于多个会话：

```sql
CREATE TABLE artifact_links (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    message_id INTEGER REFERENCES messages(id),
    origin TEXT NOT NULL CHECK(origin IN ('user_upload', 'agent_output')),
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, session_id, message_id)
);
```

D2c 的设计说「不新建表」，那是在只考虑附件、且假定 `metadata_json` 够用的前提下写的。
D3 要按会话列出**全部**产物（包括浏览器截图、下载），靠扫描每条消息的 `metadata_json`
做不到有界查询——那需要读完整个会话的消息才能列出产物。所以这里推翻 D2c 的结论，建表。

`metadata_json` 仍然写入（渲染历史消息的附件徽标时不必再查一次表），但它不再是唯一真相。

### 3.2 附件如何抵达模型

`TurnService.handle()` 新增可选参数 `attachments: tuple[AttachmentRef, ...] = ()`：

- 为空时行为与今天**逐字节相同**（这是回归防线，必须有测试）；
- 非空时，在用户消息正文后追加一段**结构固定、不可被正文伪造**的附件清单：

```
[附件]
- art_<hash> · note.txt · text/plain · 1.2 KB
```

清单由 Core 生成而不是 Desktop 传入的字符串——Desktop 只传 id，其余字段从 Store 读。
这样用户没法通过在正文里手写一段假的「[附件]」骗过模型（正文里的同名段落不会被特殊对待，
真实清单永远追加在最后）。

### 3.3 模型如何读取内容

新增 Tool `read_artifact(artifact_id, max_bytes?)`：

- 只能读**属于当前 owner 且已 link 到当前会话**的 Artifact（越权与伪造 id 都拒绝）；
- 文本类（`text/plain`、`text/csv`、`application/json`）返回有界 UTF-8 正文，超限截断并**明确
  声明被截断**（不静默截断——模型会把截断当成文件的全部）；
- 二进制类（png/jpeg/pdf/zip）**不返回正文**，只返回元数据并说明「该类型需用户在界面预览」。
  D3 不做 Vision，也不把 base64 塞进上下文。

## 4. Bridge 协议

| 类型 | payload | 说明 |
| --- | --- | --- |
| `artifacts.list` | `{session_key, limit}` | 列出该会话的产物摘要（不含正文） |
| `artifacts.preview` | `{artifact_id, max_bytes}` | 有界预览：文本返回 UTF-8，图片返回 data URI |
| `artifacts.reveal` | `{artifact_id}` | 在访达中显示；Core 只回真实路径，**打开动作由 Main 执行** |

`capabilities` 追加 `"artifacts_read"`。

**`artifacts.open`（用系统默认应用打开）本次不做。** 「用系统应用打开任意文件」等于把一个
本地执行入口交给渲染进程，而 `.zip`/`.pdf` 之类的类型在不同系统上关联到什么程序不可控。
`reveal`（只定位到访达）风险低得多，且已经满足「我要拿到这个文件」的真实需求。要做 `open`
需要单独的威胁模型讨论，不塞进 D3。

## 5. 预览的安全边界

| 类型 | D3 行为 |
| --- | --- |
| `text/plain`、`text/csv`、`application/json` | 有界 UTF-8（默认 64 KB），**按文本渲染，绝不注入 HTML**；非法 UTF-8 拒绝而不是替换字符 |
| `image/png`、`image/jpeg` | 读出后转 data URI，受 Store 已有的尺寸/像素上限约束 |
| `application/pdf`、`application/zip` | 只显示元数据 + 「在访达中显示」，不内嵌解析器 |

三条硬约束：

1. **预览走 Bridge 读取，不给 Renderer 文件路径。** Renderer 永远拿不到 `file://` 路径，
   也就无从构造任意本地读取；
2. **读取前复查**：Artifact 可能已过期被 `delete_expired()` 删除，或磁盘上的文件被替换过。
   复用 `_matches_private_target` 的哈希校验，不匹配即拒绝；
3. **CSP 不放宽。** 图片用 data URI 而不是自定义协议，避免为预览新增一个 scheme。

## 6. Desktop 侧

右栏在「当前会话有产物」时才出现（沿用 D1 的按需布局，不给空面板留位置）：

- 每条产物一行：文件名/类型、大小、来源（我上传 / Agent 产生）、时间；
- 点击展开预览（文本/图片），或「在访达中显示」；
- 会话切换即重新拉取，失败只影响右栏，不阻塞对话（沿用 D1 §9 的局部失败模式）。

## 7. TDD 起点

Core：

- `artifact_links`：同一 Artifact 关联两个会话、按会话有界列出、越权 owner 拒绝；
- `read_artifact` Tool：伪造 id、跨会话、已删除、已过期、哈希不匹配、文本截断标记、二进制不返回正文；
- `TurnService.handle()` 的空附件路径与今天**逐字节一致**（回归防线）；
- 附件清单由 Core 生成：正文里手写假 `[附件]` 段落不影响真实清单；
- protocol：三个新类型的 exact-key、`max_bytes` 边界；
- **端到端：stage 附件 → turn.start → 消息 metadata_json 有记录 → artifacts.list 能列出**
  （这条正是 D2c 缺失的那条）。

Desktop：右栏按需出现、预览失败隔离、capability 缺失时退化、文本预览不渲染 HTML。

## 8. 退出条件

1. **发一个 .txt 附件后，模型的输入里能看到该附件，且能用 `read_artifact` 读到正文**（D2c 缺口修补）；
2. 该消息的 `metadata_json` 有附件记录，重开会话仍能看到附件徽标；
3. `artifacts.list` 能按会话列出用户上传与 Agent 产生的产物，跨会话不串；
4. 文本与图片预览可用；HTML 内容按文本显示，不被渲染；
5. 伪造 id / 跨 owner / 已删除 / 哈希不匹配全部被拒且提示明确；
6. 不带附件的 `turn.start` 行为与 D3 之前完全一致；
7. Python + Desktop 全量门禁通过；
8. 真 Bridge 子进程端到端覆盖第 1、3 条。

## 8.1 落地记录（2026-08-12）

退出条件全部兑现。D2c 的缺口（§1）已补：附件抵达模型、`read_artifact` 可读正文、
`artifact_links` 按会话关联，真 Bridge 子进程端到端验证过。

### 实施中真正花时间的地方

**（a）文件名不能放在 `artifacts` 表上。** 那张表内容寻址且跨会话去重——同一份内容
用两个名字上传是同一行记录。文件名属于「每次出现」，所以放 `artifact_links`，为此
多加了迁移 0012。

**（b）`ToolContext.session_id` 本来就存在。** 我给 `read_artifact` 设计了一个
「当前会话回调」，写完才发现框架早已提供这个不可伪造的边界，把自己发明的那套删了。

**（c）SQLite 的 `UNIQUE` 里 NULL 互不相等。** `UNIQUE(artifact_id, session_id,
message_id)` 管不住 `message_id IS NULL` 的重复行，重复关联会在右栏显示两次。用一个
`WHERE message_id IS NULL` 的部分唯一索引补上。

**（d）一条测试在写的过程中被证伪。** 本想验证「非法 UTF-8 不被替换字符掩盖」，但
Store 的 magic byte 检查在入库时就拒绝了它，这个前提根本构造不出来。测试改为如实
验证真正的那道防线。

### 真实使用暴露出的四个连带问题

D3 之外，把功能真正跑起来后暴露了四处「数据早就在库里，只是从没往界面发」：

1. **运行摘要只有时间/状态/错误码**。一次跑了 55 秒、22518 token、写出完整答案的
   运行，用户只看到一个英文错误码。`_run_summary` 补上结果摘要、用量、起止时间与
   `turn_id`。
2. **任务摘要不含 `prompt`**，卡片上看不出这个任务要让 AI 干什么。D2a 时出于「不泄露
   敏感内容」收敛掉了，现在看这个尺度收得太紧——prompt 是用户自己写的。
3. **历史只回放 user/assistant**。`role == "tool"` 直接抛协议错，正文为空的 assistant
   被整条丢弃，而那一轮往往正是「只调了工具」的关键一步。
4. **automation 渠道的会话打不开**。`history` 只认 `cli`，而定时任务的会话在
   `automation`——偏偏它最需要回放，因为没有实时事件流。

还有一处是 Core 行为而非展示：**失败的运行不记录产出与用量**。只有判定成功的分支写
`result_preview` 与 `usage`，于是上面第 1 条补完之后，失败运行的那几个字段依然是空。
改为失败也保留（Provider 直接抛错时无正文可留，不凭空构造）。

### 一个「做了等于没做」

「查看完整过程」按钮做完并提交后，`app.tsx` 没传 `onOpenRun`，而按钮的渲染条件是
`run.sessionKey && onOpenRun`——回调缺席时它永远不出现。本地 172 项测试全绿也没发现，
因为没有测试覆盖这条接线。**组件测得再全，也测不出组件没被接上。**

## 9. 明确不做

- `artifacts.open`（系统默认应用打开）——见 §4 的理由；
- 图片 Vision（把图片喂给模型）；
- PDF / Office 内置渲染；
- 表格化的 CSV / JSON 编辑器（只做有界文本预览）；
- Artifact 的手动删除与重命名。
