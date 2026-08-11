# Desktop D2c：附件与 Composer 控件（修订版）

> 日期：2026-08-11
> 文档类型：Phase D2c 产品、协议与安全设计
> 状态：`PARTIAL`（2026-08-11）——admission 链路完整，但附件未抵达模型，见 §6.2
> 取代：[2026-08-10 D2 附件/模型/Workspace/Agent 控件设计](2026-08-10-desktop-d2-composer-controls-design.md)
> 前置实现：[D2a 定时任务可控](2026-08-11-desktop-d2a-automation-control-design.md)、[D2b 多 Provider 配置](2026-08-11-desktop-d2b-model-provider-config-design.md)（均已 `IMPLEMENTED`）

## 1. 为什么要重写这份设计

原 D2 设计写于 2026-08-10，早于 D2a/D2b。重新核对代码后，原文档有**两条结论已被推翻、两条现状是错的**。
不修订就直接实施，会造出两个没有能力支撑的协议类型，并在附件功能上撞墙。

### 1.1 两条被推翻的结论

| 原设计 | 现在的结论 | 理由 |
| --- | --- | --- |
| 新增 `models.list` 请求类型 | **不做** | D2b 的 `providers.list` 已经返回当前模型名与全部 Provider。Composer 里显示模型名、点击跳设置页即可，再加一个只返回一项的 `models.list` 是重复协议面 |
| 新增 `agents.list` 请求类型 | **不做** | 原文的论据是"由 Python 返回可以拒绝伪造的 agent id"。但 `turn.start` 并不携带 `agentId`（Core 没有多 Agent 概念），**根本没有 id 可以伪造**，这条安全论据不成立。返回一个硬编码常量的请求类型，是没有能力支撑的协议面。"Main Agent" 继续做 Desktop 字面量，等 D4 真有多 Agent 时再引入协议 |

### 1.2 两条现状核查结果（原文档没有发现）

**（a）`ArtifactStore` 只在浏览器启用时才被构造。**

`runtime.py:440-450`：

```python
artifact_store = (
    ArtifactStore(database, owner_id=owner.id, root=paths.artifacts,
                  staging_root=paths.downloads,
                  max_bytes=config.browser.download_max_bytes)
    if browser_client is not None else None
)
```

而 `browser.enabled` 默认是 `False`。原设计假定 Store 随手可用，实际上**默认配置下它根本不存在**，
且没有挂在 `AgentRuntime` 上。如果照原样接线，「浏览器没开的用户不能发附件」——这显然不对：
附件和浏览器是两件无关的事。

**（b）现有的读取路径要求源文件是 owner-only，用户选的文件通常不是。**

`_read_staging`（`store.py:235-273`）除了 `O_NOFOLLOW`、`S_ISREG`、大小上限、TOCTOU 复查之外，
还要求 `before.st_mode & 0o077 == 0`。这是为「Worker 自己在私有 staging 里创建的文件」设计的。
用户从「文稿」里选的文件通常是 `0644`，**会被直接拒绝**。

结论：「把外部文件拷进 staging」这一步不是可选的性能优化，而是**功能能否成立的前提**，并且它需要
一条独立的外部读取实现——保留 symlink/普通文件/大小/TOCTOU 检查，但不要求 owner-only。

## 2. 目标

D2c 完成后：

1. 用户可以从系统 Dialog 选一个本地文件，随下一条消息一起发送；
2. 附件真正经过 `ArtifactStore` 的完整校验（哈希、大小、magic byte、symlink 拒绝、TOCTOU），
   而不是把路径字符串塞进 prompt；
3. Composer 状态栏里的 Workspace 可点击切换（复用既有链路，只加入口）、模型名可点击跳设置页；
4. 附件类型不支持/过大/来源异常时给出明确可操作的提示，不静默失败。

明确不做：多模型路由、图片 Vision、Artifact 预览与「在 Finder 中显示」（D3）、多 Agent（D4）、
扩大 `_MEDIA_EXTENSIONS` 白名单、附件的持久化关联表（用现有 `metadata_json`）。

## 3. Core 侧改造

### 3.1 让 ArtifactStore 脱离浏览器开关

改为**无条件构造**，并挂到 `AgentRuntime.artifact_store`：

- 浏览器启用时，行为与今天完全一致（同一个实例继续传给 `browser_tools`）；
- 浏览器关闭时，Store 依然存在，只是没有浏览器工具去写它；
- `delete_expired()` 从「仅浏览器启用时执行」变成始终执行——这是修复，不是回归：过期 Artifact
  本来就该被回收，不该因为用户关了浏览器就永远留着。

### 3.2 单文件上限：两层，不是一层

Store 实例只有一个 `max_bytes`。附件不该借用 `browser.download_max_bytes` 的语义，但也不能各建
一个 Store（`put()` 要求 `source_path.parent == self._staging_root`，两个实例会互相拒绝对方的
staging 文件）。

采用**两层上限**：Store 的 `max_bytes` 作为外层硬边界，附件在 stage 阶段用自己的、更小的上限：

```
attachments.max_bytes（新配置，默认 10MB） ≤ Store max_bytes（默认 20MB）
```

新增 `[attachments] max_bytes`，加载时校验它不超过 Store 上限，超过就拒绝加载配置——不静默取 min，
因为静默降级会让用户以为自己设的值生效了。

### 3.3 `ArtifactStore.stage_from_external_path()`

新方法，放在 `artifacts/store.py`（**不放 `bridge/server.py`**——协议层不写业务逻辑）：

```python
def stage_from_external_path(self, source: Path, *, max_bytes: int) -> Path:
```

1. `os.open(source, O_RDONLY | O_NOFOLLOW)` —— 拒绝 symlink；
2. `fstat` 校验 `S_ISREG` 与 `st_size <= max_bytes`；**不检查 owner-only**（外部文件本就不是）；
3. 分块读取，读满上限即拒绝（不信任 `st_size`，防止读取过程中文件变大）；
4. 复查 `fstat`：`st_dev`/`st_ino`/`st_size`/`st_mtime_ns` 任一变化即 `artifact_source_changed`；
5. 内容写入 `staging_root` 下的 0600 临时文件，复用现有 `_write_private_atomic` 的模式；
6. 返回该 staging 路径，供调用方接着传给现有 `put(..., source="user_upload")`。

`_SOURCES` 追加 `"user_upload"`。失败时清理已写的 staging 文件，不留垃圾。

**注意第 3 步为什么必要**：`st_size` 是 stage 之前读的，攻击者可以在 open 与 read 之间把文件换大。
按 `max_bytes + 1` 读并在超限时立刻拒绝，才是真正的上限；第 4 步的 TOCTOU 复查是第二道。

### 3.4 Bridge 协议

| 类型 | payload | 说明 |
| --- | --- | --- |
| `attachment.stage` | `{path, declared_media_type}` | 把绝对路径拷进 Store 并校验，返回 artifact 摘要 |
| `turn.start`（改造） | 追加可选 `attachment_ids: list[str]` | 每个 id 必须在「本 session 已 stage 且未使用」集合内 |

`capabilities` 追加 `"attachments"`。

`declared_media_type` 由 Desktop 从扩展名映射，Python 侧照旧用 `_inspect_content` 做 magic byte
二次校验——**不信任 Desktop 声明的类型**，现有的 declared-vs-actual 不一致检测天然覆盖。

`path` 的校验：必须是绝对路径、长度有界、无 NUL。与 Workspace 切换同款薄校验，真正的安全边界在
`stage_from_external_path` 里。协议层不做「路径是否在某个白名单目录下」的判断——文件由用户在系统
Dialog 里亲自选定，这是用户的明示授权；真正要防的是 symlink 逃逸与类型伪造，那两条在 Store 层。

### 3.5 附件与 session 的关联

Bridge 内存态维护 `session_key -> set[artifact_id]`（已 stage、未使用）。进程重启即丢失，可接受
（staging 本就不是持久语义）。`turn.start` 成功后把 `{artifact_id, filename, media_type, size_bytes}`
写进该用户消息的 `metadata_json`——**不新建表、不做 schema 迁移**。

`turn.start` 携带未知 id 时**整体拒绝**（`attachment_unknown`），不部分发送。

## 4. Desktop 侧

### 4.1 新增 IPC

```ts
pickAttachment(): Promise<string | null>;              // Main 进程 Dialog，可取消
stageAttachment(path: string): Promise<AttachmentRef>; // 经 Bridge
// StartTurnInput 追加：attachmentIds?: string[]
```

`pickAttachment` 直接调 `dialog.showOpenDialog`，不经 Bridge（选文件不需要 Core 参与），与
`chooseWorkspace` 同一模式。

### 4.2 Composer

状态栏从一行只读文本改为：`📎` · `模型名（点击跳设置页）` · `Workspace basename（点击切换）` ·
`Main Agent`（只读字面量）· 权限模式。

附件 chip：文件名 + 大小 + 移除按钮，累积在 draft 的 `attachmentIds` 里，发送成功后清空。
stage 中禁用移除按钮。失败态用既有 `composer-error` `role="alert"`。

会话切换时清空，与 D1 现有的 `setHistory(null)` 同一批状态重置。

## 5. TDD 起点

Core：

- `stage_from_external_path`：symlink 拒绝、目录拒绝、超限拒绝（含**声明大小与实际大小不符**）、
  TOCTOU（读取过程中源被替换）、0644 源文件能正常通过（这条正是现有 `_read_staging` 做不到的）、
  staging 文件权限是 0600、失败时不留 staging 垃圾；
- `put(source="user_upload")` 端到端；
- protocol：`attachment.stage` exact-key、相对路径拒绝、NUL 拒绝；`turn.start` 的可选字段不破坏既有校验；
- server：未知 attachment id 整体拒绝且**无副作用**（不建 turn、不追加消息）；
- 配置：`attachments.max_bytes` 超过 Store 上限时拒绝加载。

Desktop：附件 chip 的增删纯函数、IPC 校验、capability 缺失时附件入口不出现。

## 6. 退出条件

1. 文本 + 一个 `.txt` 与一个 `.png` 附件走完整 admission 并发送成功；
2. **浏览器关闭时附件依然可用**（§1.2(a) 的回归防线）；
3. **0644 的普通用户文件能通过**（§1.2(b) 的回归防线）；
4. 不支持类型/过大/symlink 有明确提示，不静默失败；
5. Python + Desktop 全量门禁通过；
6. 真 Bridge 子进程端到端覆盖「stage → turn.start 携带 id」全链路。

## 6.1 落地记录（2026-08-11）

按设计实现，退出条件全部覆盖。实施中多出三件设计时没预料到的事：

**（a）`artifacts.source` 有 SQLite CHECK 约束。** 加 `user_upload` 需要迁移 0009；
SQLite 改不了 CHECK，只能重建表搬数据。设计文档说的「不做 schema 迁移」指的是不为
附件↔消息的关联新建表，那一条仍然成立。

**（b）附件默认上限与用户调低的浏览器上限会冲突。** 最初的规则是「附件上限超过 Store
上限就拒绝加载」，结果一份把 `browser.download_max_bytes` 调到 1MB、根本没写过
`[attachments]` 的配置直接加载失败。规则收敛为：**用户显式写下的值**超界才拒绝，
默认值超界时收敛即可。

**（c）一条 TOCTOU 测试最初断言错了。** 我用 `os.replace` 模拟「读取期间源被换掉」，
但替换换不掉已经打开的 fd，所以 re-fstat 什么也发现不了——这不是实现漏洞，是测试
搞错了防线的位置。re-fstat 真正能发现的是同一 inode 上的**原地改写**，测试已改为
验证后者。

端到端 smoke 走真 Bridge 子进程，同时兑现退出条件 2、3、6，并断言完整路径不出现在
响应里。

## 6.2 已知缺口（2026-08-11 发现）

设计 D3 时重新核查代码，发现 **§3.5 没有实现**：`turn.start` 里 `attachment_ids` 只用于
校验就被丢弃，既没写进消息的 `metadata_json`，也没传给 `TurnService.handle()`。

后果：点附件、选文件、发送全程不报错，文件也确实被安全存进了 ArtifactStore，但**模型完全
不知道用户发了文件**，而且没有任何 Tool 能读取 Artifact 正文。

根因是 §6 的退出条件里没有一条是「Agent 能真正用上这个附件」——6 条全部围绕 admission 的
安全性，所以测试全绿却漏掉了功能本身。

修补并入 [D3](2026-08-11-desktop-d3-artifacts-design.md) §1，因为它需要的「Artifact ↔ 会话
关联」正是 D3 展示产物所需的同一份数据，分两次做会把同一张表改两遍。

## 7. 与原文档的差异汇总

保留：方案 B（真实 Core admission）、附件 chip 交互、`metadata_json` 关联、不扩白名单、Workspace
只加入口不新建逻辑。

删除：`models.list`、`agents.list`（§1.1）。

新增：ArtifactStore 脱离浏览器开关、两层大小上限、外部读取路径的独立实现（§1.2）。
