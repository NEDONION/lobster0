# Phase 5 Telegram and Discord Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不复制 Agent Core 的前提下，同时交付 Telegram 与 Discord 两个生产 Channel，让飞书、Telegram、Discord 在一个进程中共享同一个 `AgentRuntime`、Owner、长期 Memory、Skills、Policy、Approval 和 Tool，同时保持各自的会话、队列、Delivery、故障与平台限流相互隔离。

**Architecture:** `GatewaySupervisor` 先对所有 enabled Channel 做离线、fail-closed 预检，再创建唯一 `AgentRuntime`，随后为每个平台装配独立的 `ChannelRuntime(Transport + DeliveryWorker + ChannelManager + Experience)`。SQLite 继续是 Inbox、Outbox、Identity、Session 和 Approval 的 durable truth；平台 SDK 只负责收发。Telegram 使用 long polling，Discord 使用 Gateway；任一平台运行期断线不能取消其余平台。

**Tech Stack:** Python 3.12+、stdlib `asyncio`/`sqlite3`/`unittest`、`python-telegram-bot>=21,<23`、`discord.py>=2.4,<3`、现有 `httpx`/Textual/Ruff/uv、JSONL versioned eval fixtures。

**Authoritative design:**
`docs/superpowers/specs/2026-08-08-phase-5-telegram-discord-design.md`

**Execution baseline (2026-08-08):** Phase 4 已通过 391 个 Python tests、25 个 TypeScript tests、24/24 Agent cases、12/12 Feishu Channel cases 和 240/240 local Channel soak。Phase 5 必须保留这些结果，并新增 20 个 Channel cases，使本地 Channel gate 至少达到 32/32、20 轮至少达到 640/640。当前没有 Telegram 凭据，因此 Telegram live gate 从一开始就标记 `LIVE PENDING`，不得用 fake 测试冒充真实平台验收。

---

## Global Constraints

1. 严格按 Task 1 → 14 顺序执行；每个生产代码改动前，必须先有 focused test 真实 RED。
2. 每完成一个 Task，就把本文件对应 checkbox 改为 `[x]`，记录实际测试数字和 commit；不能只在聊天中口头报进度。
3. 保留用户已有且不属于 Phase 5 的工作树内容：`docs/README.md` 和两份未跟踪架构草稿不进入任何 Phase 5 commit。
4. Commit 标题中英各半、先写 scope，再用中文说明结果，例如：
   `feat(telegram): 增加 allowlisted long-polling Channel`。
5. Telegram/Discord SDK 必须延迟导入；未启用的平台不要求安装 extra，也不读取对应 Token。
6. Token 只能短暂存在于环境读取结果和 Transport 构造参数，不能写入 `AppConfig`、SQLite、日志、异常、`repr`、eval evidence 或文档。
7. 所有 ID admission 都 fail closed。用户名、昵称、群名、Guild 名不能用于 Owner 绑定。
8. 一个进程只有一个 `AgentRuntime`；严禁为每个 Channel 各建 Provider、MemoryStore 或 ToolRegistry。
9. 每个平台有独立 `ChannelManager`、`DeliveryWorker`、Transport、Experience activity 和恢复边界。
10. 体验能力是 best effort：Typing/preview 失败只记录稳定短码，最终 durable Delivery 仍必须执行。
11. Approval 决策继续以 Core SQLite 状态为权威；平台按钮或文本只能请求 Core continuation，不能直接执行 Tool。
12. 不新增 schema migration，除非 focused test 能证明 SQLite v2 不能表达 Telegram/Discord 的 channel/account/external ID；当前设计明确认为 v2 足够。
13. 所有 fake tests 禁止真实联网；只有显式 `--confirm-live` 的 live harness 可以访问平台。
14. “Implementation PASS”和“Production verified”分开：fake SDK、全量回归和 local soak 全绿只能得到前者。

## Shared completion ledger

| Gate | Baseline | Phase 5 exit |
| --- | ---: | ---: |
| Python unit/integration | 391 | 全量 100%，记录实际总数 |
| TypeScript `lark-cli` | 25 | 25/25 或更高 |
| Agent regression | 24 | 24/24 |
| Channel regression | 12 | ≥32/32 |
| 20-run Channel soak | 240 | ≥640/640 |
| Ruff / build / docs / HTML | PASS | 全部 PASS |
| Telegram live | 无凭据 | `LIVE PENDING`，除非本次取得真实 evidence |
| Discord live | 未验证 | `LIVE PENDING` 或真实 evidence 对应的 PASS |

---

## Task 1: Add typed Telegram/Discord configuration and optional SDK extras

**Purpose:** 先锁住静态配置和 packaging 边界。配置不正确时，Gateway 必须在创建 Provider、打开平台连接、写业务表之前失败。

**Files:**

- Modify: `src/miniclaw/config.py`
- Modify: `src/miniclaw/bootstrap.py`
- Modify: `.env.example`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/test_config.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/test_runtime.py`

### Step 1.1 — Write RED configuration contracts

- [x] 在 `tests/test_config.py` 新增 Telegram/Discord 完整加载、disabled defaults、未知 key、bool-as-int、重复 ID、上下界和组合关系测试。
- [x] 测试以下类型必须存在且冻结：

```python
@dataclass(frozen=True, slots=True)
class TelegramConfig:
    enabled: bool = False
    account_id: str = "default"
    bot_token_env: str = "MINICLAW_TELEGRAM_BOT_TOKEN"
    owner_user_id: int = 0
    allowed_user_ids: tuple[int, ...] = ()
    allowed_chat_ids: tuple[int, ...] = ()
    allow_group_mentions: bool = False
    queue_size: int = 64
    worker_count: int = 2
    message_max_chars: int = 4096
    progress_update_interval: float = 0.8


@dataclass(frozen=True, slots=True)
class DiscordConfig:
    enabled: bool = False
    account_id: str = "default"
    bot_token_env: str = "MINICLAW_DISCORD_BOT_TOKEN"
    owner_user_id: int = 0
    allowed_user_ids: tuple[int, ...] = ()
    allowed_guild_ids: tuple[int, ...] = ()
    allowed_channel_ids: tuple[int, ...] = ()
    allow_guild_mentions: bool = False
    queue_size: int = 64
    worker_count: int = 2
    message_max_chars: int = 2000
    progress_update_interval: float = 1.0
    typing_renew_interval: float = 8.0
```

- [x] Telegram user ID 必须是正整数；chat ID 接受非零 signed 64-bit，拒绝 bool、0 和越界值。
- [x] Discord snowflake 必须在 `1..2**64-1`；拒绝 bool、0、负数和越界值。
- [x] enabled 时 Owner 非零且必须在 allowed user list 中。
- [x] Telegram 开启群 mention 时 `allowed_chat_ids` 非空；Discord 开启 Guild mention 时 Guild 与 Channel allowlist 都非空。
- [x] `queue_size=1..1024`、`worker_count=1..8`、Telegram chars `1000..4096`、Discord chars `1000..2000`、interval `0.1..30.0`。
- [x] `AppConfig` 和 `repr(config)` 只包含 Token 环境变量名，不包含环境变量值。
- [x] Gateway 继续要求 `.env` 是 owner-only regular file；symlink、group/world-readable 文件在读取 Token 前失败。

Run RED:

```bash
uv run python -m unittest \
  tests.test_config.TelegramConfigTest \
  tests.test_config.DiscordConfigTest -v
```

Expected RED: `ChannelConfig` 没有 `telegram`/`discord`，`channels.telegram` 首先被报告为 unknown key。

### Step 1.2 — Implement strict parsing

- [x] 将 `_CHANNELS_KEYS` 扩展为 `feishu/telegram/discord`，分别定义 allowlist key set。
- [x] 新增 `_platform_integer_list`、signed chat ID、snowflake 与 `_bounded_number` helpers；所有 helper 显式排除 `bool`。
- [x] `ChannelConfig` 同时持有三个 typed config；保持 Feishu 默认值不变。
- [x] validation 先做单字段，再做关系校验，错误信息只包含配置路径和规则，不回显 ID list。

目标装配：

```python
channels=ChannelConfig(
    feishu=FeishuConfig(...),
    telegram=TelegramConfig(...),
    discord=DiscordConfig(...),
)
```

### Step 1.3 — Add safe bootstrap and environment examples

- [x] `config.toml` bootstrap template 增加两个注释 section，默认 disabled、Owner 为 0、allowlist 为空。
- [x] `.env.example` 只增加空的 `MINICLAW_TELEGRAM_BOT_TOKEN=` 与 `MINICLAW_DISCORD_BOT_TOKEN=`，不写示例 Token。
- [x] 新增测试保证 bootstrap 结果不包含形似 Telegram bot token 或 Discord token 的值。

### Step 1.4 — Add optional extras and lazy-import tests

- [x] `pyproject.toml` 增加：

```toml
telegram = ["python-telegram-bot>=21,<23"]
discord = ["discord.py>=2.4,<3"]
channels = [
  "lark-channel-sdk>=1.2,<2",
  "python-telegram-bot>=21,<23",
  "discord.py>=2.4,<3",
]
```

- [x] 普通 `import miniclaw.runtime`、TUI 和只启用飞书时不得在 `sys.modules` 中加载 `telegram`/`discord`。
- [x] 更新 lock，安装 dev + channels 以便本地跑全量，但 runtime 代码继续 lazy import。

Run GREEN:

```bash
uv lock
uv sync --extra dev --extra channels
uv run python -m unittest tests.test_config tests.test_bootstrap tests.test_runtime -v
uv run ruff check src/miniclaw/config.py src/miniclaw/bootstrap.py \
  tests/test_config.py tests/test_bootstrap.py tests/test_runtime.py
uv build
```

### Step 1.5 — Commit

```bash
git add .env.example pyproject.toml uv.lock src/miniclaw/config.py \
  src/miniclaw/bootstrap.py tests/test_config.py tests/test_bootstrap.py tests/test_runtime.py
git commit -m "feat(config): 增加 Telegram/Discord typed settings 与 optional extras"
```

---

## Task 2: Extract platform-neutral limits, manager factory and delivery errors

**Purpose:** 删除公共层中写死的 `feishu_*` 错误码，把三个平台真正接到同一套 Manager/Delivery，而不是复制三份业务状态机。

**Files:**

- Modify: `src/miniclaw/channels/base.py`
- Modify: `src/miniclaw/channels/delivery.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `tests/test_channel_contracts.py`
- Modify: `tests/test_delivery.py`
- Modify: `tests/test_runtime.py`

### Step 2.1 — RED for `ChannelLimits`

- [x] 测试 immutable value object：

```python
@dataclass(frozen=True, slots=True)
class ChannelLimits:
    channel: str
    account_id: str
    queue_size: int
    worker_count: int
    message_max_chars: int
    progress_update_interval: float
```

- [x] 构造函数拒绝未知 channel、空 account、bool 数字、非正预算；`repr` 不含 Secret 或平台 ID。
- [x] 添加 `limits_for_channel(config, "feishu" | "telegram" | "discord")` 的稳定映射测试。

Run RED:

```bash
uv run python -m unittest \
  tests.test_channel_contracts.ChannelLimitsTest \
  tests.test_runtime.ChannelManagerFactoryTest -v
```

Expected RED: `ChannelLimits` 和 generic manager factory 不存在。

### Step 2.2 — Generalize manager creation

- [x] 将 `create_channel_manager()` 改为显式接收 `ChannelLimits`，不能在函数内部读取 `config.channels.feishu`。
- [x] 依赖仍只创建一次 repository set；不把 Transport 传入 Core Manager。

目标签名：

```python
def create_channel_manager(
    paths: StatePaths,
    runtime: AgentRuntime,
    limits: ChannelLimits,
    *,
    observer: ChannelObserver | None = None,
) -> ChannelManager:
    database = Database(paths.database)
    return ChannelManager(
        owner_id=runtime.owner_id,
        service=runtime.service,
        sessions=SessionRepository(database),
        messages=MessageRepository(database),
        turns=TurnRepository(database),
        identities=ChannelIdentityRepository(database),
        inbound=InboundEventRepository(database),
        deliveries=DeliveryRepository(database),
        channel=limits.channel,
        account_id=limits.account_id,
        queue_size=limits.queue_size,
        worker_count=limits.worker_count,
        message_max_chars=limits.message_max_chars,
        observer=observer,
    )
```

### Step 2.3 — Make Delivery errors channel-neutral

- [x] RED 覆盖 cancel、unknown receipt、unexpected exception、invalid approval payload、unsupported interactive delivery。
- [x] 将稳定码改为：

| Old hard-coded code | New stable code |
| --- | --- |
| `feishu_delivery_unknown` | `channel_delivery_unknown` |
| `feishu_send_failed` | `channel_send_failed` |
| `feishu_approval_payload_invalid` | `channel_approval_payload_invalid` |
| `feishu_card_unsupported` | `channel_interactive_unsupported` |
| `feishu_card_failed` | `channel_interactive_failed` |

- [x] 保留平台 Transport 自己映射的 `telegram_rate_limited`、`discord_forbidden` 等平台码。
- [x] 旧测试更新为公共语义断言，不通过兼容 alias 隐藏硬编码。

Run GREEN:

```bash
uv run python -m unittest tests.test_channel_contracts tests.test_delivery tests.test_runtime -v
uv run ruff check src/miniclaw/channels/base.py src/miniclaw/channels/delivery.py \
  src/miniclaw/runtime.py tests/test_channel_contracts.py tests/test_delivery.py \
  tests/test_runtime.py
```

### Step 2.4 — Commit

```bash
git add src/miniclaw/channels/base.py src/miniclaw/channels/delivery.py \
  src/miniclaw/runtime.py tests/test_channel_contracts.py tests/test_delivery.py \
  tests/test_runtime.py
git commit -m "refactor(channel): 抽取 shared limits、manager factory 与 delivery errors"
```

---

## Task 3: Introduce Approval v2 neutral envelope with v1 compatibility

**Purpose:** 让 Approval durable payload 描述“审批什么、允许哪些决定”，不再描述 Feishu Card。旧 v1 数据仍可解析，避免升级后已有 pending approval 失效。

**Files:**

- Modify: `src/miniclaw/channels/approvals.py`
- Modify: `src/miniclaw/channels/delivery.py`
- Modify: `tests/test_channel_approvals.py`
- Modify: `tests/test_delivery.py`

### Step 3.1 — Define and RED-test the v2 envelope

- [x] 新增 frozen `ApprovalEnvelope`：

```python
@dataclass(frozen=True, slots=True)
class ApprovalEnvelope:
    version: Literal[2]
    approval_id: int
    tool_name: str
    summary: str
    decisions: tuple[ApprovalDecision, ...]
    expires_at: str
    fallback_text: str
```

- [x] 序列化 JSON 使用固定字段和排序，正文有长度上限；拒绝额外 key、bool-as-int、未知 decision、过期格式、NUL、空 summary。
- [x] `repr` 只显示 version、approval_id、tool_name，不显示 summary/fallback。
- [x] 保留 v1 parser：读取旧 `card + fallback_text`，转换为中立 envelope 或 legacy delivery view；只读兼容，不再创建新 v1。

Run RED:

```bash
uv run python -m unittest \
  tests.test_channel_approvals.ApprovalEnvelopeV2Test \
  tests.test_delivery.ApprovalDeliveryCompatibilityTest -v
```

Expected RED: 当前 writer 只生成 Feishu v1 card payload。

### Step 3.2 — Separate Core decision from rendering

- [x] `ChannelApprovalController` 用 `owner_external_user_id` 代替 `owner_open_id`，`handle_text`/button handler 用 `actor_external_user_id`。
- [x] 文本命令格式保持 Core 统一：`/approve <id> once|session|always` 和 `/deny <id>`。
- [x] 新建平台 renderer 函数只把 envelope 转成平台 payload：Feishu card、Telegram/Discord 纯文本；平台 Transport 可在后续 Task 增加 inline keyboard/view。
- [x] DeliveryWorker 优先调用可选 `send_approval(envelope, ...)`；平台不支持时原子 supersede 并创建 fallback Markdown parts。

### Step 3.3 — Prove old Feishu behavior

- [x] 旧 Feishu approve/deny、非 Owner 拒绝、expired、duplicate decision、card fallback 全通过。
- [x] v1 durable fixture 能在升级后的 DeliveryWorker 中完成或 fallback；不执行第二次 Tool。

Run GREEN:

```bash
uv run python -m unittest tests.test_channel_approvals tests.test_delivery \
  tests.test_feishu_transport -v
uv run ruff check src/miniclaw/channels/approvals.py \
  src/miniclaw/channels/delivery.py tests/test_channel_approvals.py tests/test_delivery.py
```

### Step 3.4 — Commit

```bash
git add src/miniclaw/channels/approvals.py src/miniclaw/channels/delivery.py \
  tests/test_channel_approvals.py tests/test_delivery.py tests/test_feishu_transport.py
git commit -m "feat(approval): 引入 neutral v2 envelope 并兼容 Feishu v1"
```

---

## Task 4: Replace Feishu-specific capabilities with a shared Experience layer

**Purpose:** Typing 和 progress preview 统一表达用户体验意图；具体平台如何 reaction、typing context 或 edit message 由 Transport 决定。

**Files:**

- Create: `src/miniclaw/channels/experience.py`
- Modify: `src/miniclaw/channels/capabilities.py`
- Modify: `src/miniclaw/channels/manager.py`
- Modify: `src/miniclaw/gateway.py`
- Create: `tests/test_channel_experience.py`
- Modify: `tests/test_channel_capabilities.py`
- Modify: `tests/test_channel_manager.py`

### Step 4.1 — RED the public Experience protocol

- [x] 定义：

```python
class ChannelExperienceTransport(Protocol):
    async def start_typing(self, event: StoredInboundEvent) -> str | None: ...
    async def stop_typing(self, token: str | None) -> None: ...
    async def create_progress(
        self,
        event: StoredInboundEvent,
        text: str,
        *,
        idempotency_key: str,
    ) -> SendReceipt: ...
    async def update_progress(
        self,
        platform_message_id: str,
        text: str,
        *,
        incomplete: bool,
        completed: bool,
    ) -> SendReceipt: ...
```

- [x] `ChannelExperience.activity(event)` 只消费公开 `model_text_delta`，不读取 provider hidden reasoning。
- [x] activity 的 typing token、preview message ID、visible text、interval、finished 状态完全是单 Turn 私有状态。
- [x] 所有 Transport 异常被 observer 记录为 `<channel>_typing_failed` / `<channel>_progress_failed`，不向 Agent Turn 抛出。
- [x] finish 幂等，失败时 `incomplete=True, completed=False`，成功时 `completed=True`。

Run RED:

```bash
uv run python -m unittest tests.test_channel_experience -v
```

Expected RED: `miniclaw.channels.experience` 不存在。

### Step 4.2 — Implement shared activity state

- [x] 从当前 `ChannelCapabilities/CapabilityActivity` 移动 bounded append、interval gating、idempotency key 和 finish cleanup。
- [x] `ChannelCapabilities` 暂时成为 Feishu compatibility adapter，公开旧构造形状但内部委托 `ChannelExperience`；下一版本可删除，Phase 5 不做破坏性删改。
- [x] Manager 字段和 attach API 改为 `ChannelExperience`，并保留 `attach_capabilities()` 的 deprecated in-process alias 供旧测试与 Feishu 装配迁移。

### Step 4.3 — Adapt Feishu without behavior change

- [x] 在 `FeishuTransport` 上实现 Experience protocol 的四个方法，内部调用现有 add/remove typing 与 send/update card。
- [x] Feishu final Markdown、streaming card fallback、Observer failure isolation 全部保持 Phase 4 语义。

Run GREEN:

```bash
uv run python -m unittest tests.test_channel_experience \
  tests.test_channel_capabilities tests.test_channel_manager \
  tests.test_feishu_transport tests.test_gateway -v
uv run ruff check src/miniclaw/channels/experience.py \
  src/miniclaw/channels/capabilities.py src/miniclaw/channels/manager.py \
  src/miniclaw/gateway.py tests/test_channel_experience.py
```

### Step 4.4 — Commit

```bash
git add src/miniclaw/channels/experience.py src/miniclaw/channels/capabilities.py \
  src/miniclaw/channels/manager.py src/miniclaw/gateway.py \
  tests/test_channel_experience.py tests/test_channel_capabilities.py \
  tests/test_channel_manager.py tests/test_feishu_transport.py tests/test_gateway.py
git commit -m "refactor(experience): 平台无关化 Typing 与 progress preview"
```

---

## Task 5: Implement the pure Telegram Adapter

**Purpose:** 先把 Telegram Update 转成 MiniClaw `InboundMessage`，不碰网络。Adapter 是 admission/security 边界，不把 SDK object 传进 Core。

**Files:**

- Create: `src/miniclaw/channels/telegram.py`
- Create: `tests/test_telegram_adapter.py`

### Step 5.1 — Define a narrow update view and RED matrix

- [x] 测试使用本地 frozen view，不 import `telegram` SDK：

```python
@dataclass(frozen=True, slots=True)
class TelegramMessageView:
    update_id: int
    message_id: int
    user_id: int
    chat_id: int
    chat_type: str
    text: str | None
    date: datetime
    is_bot: bool = False
    is_service: bool = False
    is_edited: bool = False
    mentioned_bot: bool = False
    replied_to_bot: bool = False
    topic_id: int | None = None
```

- [x] 私聊 Owner admitted；非 allowlisted user ignored `user_not_allowed`。
- [x] 群 chat 和 user 双 allowlist；mention 或 reply-to-bot admitted；否则 `bot_not_addressed`。
- [x] bot/service/edited/non-text/empty/control-only/oversized 分别稳定 ignore。
- [x] event ID 使用 `update:<update_id>`；message key 使用 `chat:<chat_id>:message:<message_id>`。
- [x] forum topic conversation key 为 `chat:<chat_id>:topic:<topic_id>`，普通 conversation 为 `chat:<chat_id>`。
- [x] mention stripping 只移除 bot 自身 entity，不误删用户正文中的普通 `@name`。
- [x] `repr`、异常和 observer fields 不出现 text、完整 user/chat ID 或 raw Update。

Run RED:

```bash
uv run python -m unittest tests.test_telegram_adapter -v
```

Expected RED: `TelegramAdapter` 尚不存在。

### Step 5.2 — Implement a pure normalizer

- [x] `TelegramAdapter(config, bot_user_id)` 构造时冻结 allowlist sets。
- [x] `normalize(view)` 返回 `InboundMessage | IgnoredInbound`；不读取网络、不写 SQLite。
- [x] `chat_type` 只映射 private → `p2p`，group/supergroup → `group`；其他值忽略。
- [x] `received_at` 强制 timezone-aware UTC；非法时间不进入 Core。
- [x] 复用公共 text sanitize helper，限制入站正文预算，禁止 NUL 和危险控制字符。

Run GREEN:

```bash
uv run python -m unittest tests.test_telegram_adapter -v
uv run ruff check src/miniclaw/channels/telegram.py tests/test_telegram_adapter.py
```

### Step 5.3 — Commit

```bash
git add src/miniclaw/channels/telegram.py tests/test_telegram_adapter.py
git commit -m "feat(telegram): 增加 allowlisted inbound Adapter 与 topic identity"
```

---

## Task 6: Implement Telegram long-polling Transport, delivery and Experience

**Purpose:** 使用 official SDK 完成 get_me、polling、reply/send、edit、typing、rate-limit 映射；所有 SDK 细节停在 Transport 内。

**Files:**

- Modify: `src/miniclaw/channels/telegram.py`
- Create: `tests/fakes/fake_telegram.py`
- Create: `tests/test_telegram_transport.py`
- Modify: `tests/test_delivery.py`
- Modify: `tests/test_channel_experience.py`

### Step 6.1 — RED lifecycle and SDK boundary

- [x] Fake SDK 记录 `initialize/start/start_polling/stop/shutdown` 的精确顺序。
- [x] `connect()` 先 `get_me()` 取得 bot ID/username，再注册 handler 并开始 polling；ready 前收到消息不得进入 Manager。
- [x] `stop_receiving()` 立即停止新 handler admission；`disconnect()` 幂等并完整释放 updater/application。
- [x] SDK 缺失只在构造 enabled Telegram runtime 时报告 `telegram_sdk_missing`。
- [x] SDK Update mapper 只复制 narrow fields，禁止把 Update 保存到实例、exception 或 callback queue。

Run RED:

```bash
uv run python -m unittest \
  tests.test_telegram_transport.TelegramLifecycleTest -v
```

Expected RED: Transport 尚无 lifecycle。

### Step 6.2 — RED send, split and error map

- [x] `send()` 使用 `reply_text` 或 `bot.send_message`，默认禁用 link preview 且不用 MarkdownV2 parse mode；idempotency key 只用于本地 ledger，不伪称 Telegram 支持 HTTP 幂等。
- [x] `AllowedUpdates` 只订阅 message；edited/channel post/callback 只在对应能力开启后增加。
- [x] 4096 char 分片保留 Markdown code fence 可读性；任意 part `len <= 4096`，拼接去掉 prefix 后内容不丢失。
- [x] `RetryAfter` → `ChannelTransportError("telegram_rate_limited", retryable=True)`，并把 SDK retry-after 归一到 Delivery retry time。
- [x] `TimedOut/NetworkError` → retryable `telegram_poll_failed`/`telegram_send_failed`；`Forbidden` → non-retryable `telegram_permission_denied`；认证失败 → `telegram_auth_failed`；send 已发但 receipt 不确定 → `unknown=True`。
- [x] 错误对象的 message、Token 和目标 ID 不进入 `str/repr/log`。

### Step 6.3 — RED Experience behavior

- [x] typing 使用 `send_chat_action(TYPING)`；后台 renewal 有界，finish 必须取消 renewal task。
- [x] progress 第一帧发送普通消息，后续用 `edit_message_text`；更新频率不快于 config interval。
- [x] edit 返回 message-not-modified 视为成功；preview 失败后 final durable message 仍通过 DeliveryWorker 发送。
- [x] Telegram 不支持 rich approval 时至少提供文本 fallback；若实现 inline keyboard，callback 必须验证 Owner 和 envelope version。

### Step 6.4 — Implement with injected SDK facade

- [x] 生产构造函数允许传 `application_factory` 仅用于 tests；默认 factory 在函数体内 lazy import official SDK。
- [x] `connect()`/`disconnect()` 和 handler callback 都捕获 SDK 异常并映射成稳定短码。
- [x] on_inbound callback 返回值不回显给平台；queue full 由 durable Inbox feeder 恢复。

Run GREEN:

```bash
uv run python -m unittest tests.test_telegram_transport tests.test_delivery \
  tests.test_channel_experience -v
uv run ruff check src/miniclaw/channels/telegram.py tests/fakes/fake_telegram.py \
  tests/test_telegram_transport.py tests/test_delivery.py tests/test_channel_experience.py
```

### Step 6.5 — Commit

```bash
git add src/miniclaw/channels/telegram.py tests/fakes/fake_telegram.py \
  tests/test_telegram_transport.py tests/test_delivery.py tests/test_channel_experience.py
git commit -m "feat(telegram): 打通 long polling、delivery 与 progress experience"
```

---

## Task 7: Implement the pure Discord Adapter

**Purpose:** 把 Discord Message 映射为同一入站契约，同时严格处理 DM、Guild、Thread、mention、bot/webhook/system 边界。

**Files:**

- Create: `src/miniclaw/channels/discord.py`
- Create: `tests/test_discord_adapter.py`

### Step 7.1 — RED the Discord admission matrix

- [ ] 定义不依赖 SDK 的 narrow view：

```python
@dataclass(frozen=True, slots=True)
class DiscordMessageView:
    message_id: int
    author_id: int
    channel_id: int
    guild_id: int | None
    thread_id: int | None
    content: str
    created_at: datetime
    author_is_bot: bool = False
    webhook_id: int | None = None
    is_system: bool = False
    mentioned_bot: bool = False
    replied_to_bot: bool = False
```

- [x] Owner DM admitted；非 allowlisted author ignored。
- [x] Guild message 同时满足 user/guild/channel allowlist，并且 mention 或 reply-to-bot；Thread 继承 parent channel allowlist，但 conversation key 使用 thread ID。
- [x] bot/webhook/system/empty/control-only/oversized ignored，使用稳定 reason。
- [x] `event_id == message_id == str(snowflake)`；DM conversation `channel:<id>`，Guild channel `guild:<g>:channel:<c>`，Thread 再加 `:thread:<t>`。
- [x] mention stripping 只处理 `<@bot_id>`/`<@!bot_id>`，不删其他用户 mention。
- [x] raw SDK message、正文、完整 snowflake 不进入 repr/log。

Run RED:

```bash
uv run python -m unittest tests.test_discord_adapter -v
```

Expected RED: `DiscordAdapter` 不存在。

### Step 7.2 — Implement pure normalization

- [x] `DiscordAdapter(config, bot_user_id)` 缓存 immutable allowlist。
- [x] 只接受 timezone-aware created_at 和严格 snowflake。
- [x] 统一输出 `chat_type="p2p" | "group"`，不把 Guild 对象传入 Core。

Run GREEN:

```bash
uv run python -m unittest tests.test_discord_adapter -v
uv run ruff check src/miniclaw/channels/discord.py tests/test_discord_adapter.py
```

### Step 7.3 — Commit

```bash
git add src/miniclaw/channels/discord.py tests/test_discord_adapter.py
git commit -m "feat(discord): 增加 DM/Guild/Thread inbound Adapter"
```

---

## Task 8: Implement Discord Gateway Transport, delivery and Experience

**Purpose:** 使用 `discord.py` Gateway 完成连接、resume、发送、edit、typing 和限流错误映射，同时禁止 accidental mentions。

**Files:**

- Modify: `src/miniclaw/channels/discord.py`
- Create: `tests/fakes/fake_discord.py`
- Create: `tests/test_discord_transport.py`
- Modify: `tests/test_delivery.py`
- Modify: `tests/test_channel_experience.py`

### Step 8.1 — RED exact intents and lifecycle

- [x] Intents 只开启 `guilds`、`messages`、`message_content`、`dm_messages` 所需集合；禁用 members、presences 和不必要 privileged intents。
- [x] fake client 验证 login/start、ready、close、resume callback；`connect()` 只有在 ready 后返回。
- [x] `stop_receiving()` 让 on_message fail closed；`disconnect()` 幂等。
- [x] 单次 Gateway disconnect 交给 SDK resume/reconnect，不重建 AgentRuntime。
- [x] fatal close code（invalid token/intent）映射 non-retryable platform state，临时网络 close 标记 degraded/retryable。

Run RED:

```bash
uv run python -m unittest \
  tests.test_discord_transport.DiscordLifecycleTest -v
```

Expected RED: Transport lifecycle 尚未实现。

### Step 8.2 — RED safe send and error mapping

- [x] 所有 send/reply/edit 使用 `AllowedMentions.none()`；MiniClaw 回复不能 ping 用户、角色或 everyone。
- [x] 2000 char 分片每 part 不超预算，emoji/中文/code fence 不丢字符。
- [x] 429/retry-after → retryable `discord_rate_limited`；5xx/network → retryable；403 → `discord_forbidden`；404 → `discord_target_not_found`；不确定 receipt → unknown。
- [x] SDK exception 的正文和 response body 不进入稳定错误。

### Step 8.3 — RED typing/progress

- [x] typing context 在 Turn 开始进入，finish/cancel 一定退出；renew interval 有界。
- [x] create progress 用 channel.send，update 用 Message.edit；完成/失败状态不会覆盖 durable final reply。
- [x] progress 更新失败只写 Observer/Audit，不改变 Inbox terminal state。
- [x] 若实现 Approval View，interaction 必须 defer/ack、验证 Owner、只调用 Controller；否则明确走 neutral fallback text。

### Step 8.4 — Implement injected client facade

- [x] production default 在 factory 内 lazy import `discord`；tests 注入 fake client，不 monkeypatch全局 SDK。
- [x] SDK callback 先构造 narrow view，再调用 Adapter/Manager；Transport 不持久化 SDK object。

Run GREEN:

```bash
uv run python -m unittest tests.test_discord_transport tests.test_delivery \
  tests.test_channel_experience -v
uv run ruff check src/miniclaw/channels/discord.py tests/fakes/fake_discord.py \
  tests/test_discord_transport.py tests/test_delivery.py tests/test_channel_experience.py
```

### Step 8.5 — Commit

```bash
git add src/miniclaw/channels/discord.py tests/fakes/fake_discord.py \
  tests/test_discord_transport.py tests/test_delivery.py tests/test_channel_experience.py
git commit -m "feat(discord): 打通 Gateway、safe delivery 与 typing experience"
```

---

## Task 9: Build `GatewaySupervisor` for one Runtime and multiple isolated pipelines

**Purpose:** 把当前单飞书 `gateway.py` 升级成多平台 supervisor，做到静态错误全有或全无，运行期错误单平台隔离，退出时反向完整清理。

**Files:**

- Create: `src/miniclaw/channels/supervisor.py`
- Modify: `src/miniclaw/gateway.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `tests/test_gateway.py`
- Create: `tests/test_channel_supervisor.py`
- Modify: `tests/test_channel_observability.py`

### Step 9.1 — RED preflight and credential bundle

- [x] `collect_enabled_channels(config)` 保持固定 `feishu, telegram, discord` 顺序；空集合报 `no_channels_enabled`。
- [x] 一次校验 model key、每个 enabled Channel 的 Token/SDK/owner relations；任一失败时 runtime factory、database business write 和 transport factory 调用数都是 0。
- [x] 凭据按 platform 分隔并 `repr=False`：

```python
@dataclass(frozen=True, slots=True, repr=False)
class GatewaySecrets:
    model_api_key: str
    channel_tokens: Mapping[str, str]

    def __repr__(self) -> str:
        names = ",".join(sorted(self.channel_tokens))
        return f"GatewaySecrets(configured={names})"
```

- [x] duplicated `(channel, account_id)` 拒绝；不同 platform 相同 account ID 合法。

Run RED:

```bash
uv run python -m unittest \
  tests.test_channel_supervisor.GatewayPreflightTest -v
```

Expected RED: 现有 validation 只接受 Feishu 三个 secret。

### Step 9.2 — RED runtime bundle and lifecycle

- [x] 定义：

```python
@dataclass(slots=True)
class ChannelRuntime:
    channel: str
    account_id: str
    manager: ManagerComponent
    delivery: DeliveryComponent
    transport: TransportComponent
    state: Literal["created", "starting", "ready", "degraded", "stopping", "stopped"]


@dataclass(slots=True)
class GatewaySupervisor:
    runtime: RuntimeComponent
    channels: tuple[ChannelRuntime, ...]
```

- [x] 断言 runtime factory 正好 1 次；三个 Channel 各自 manager/delivery/transport 正好 1 个。
- [x] 每个 Channel startup：transport connect → delivery start → manager start。
- [x] 全局 shutdown：先全部 stop receiving，再按 reverse channel order 执行 manager stop → delivery stop → transport disconnect，最后 runtime close 一次。
- [x] startup 第 N 个失败时，已启动组件反向清理；未启动平台不调用 stop；runtime 仍关闭。
- [x] 一个 Channel 的运行期 task 异常只把该 runtime 标记 degraded，其他 ready pipeline 可继续 receive/send。
- [x] 一个平台 queue full 不占用其他平台 queue；同 Conversation 仍串行，不同 Channel/Conversation 可并发；Agent 网络等待期间不持有 SQLite transaction。
- [x] 第二信号只取消当前阻塞 cleanup，继续清理其余资源。
- [x] ready 文本只列 `channel/account_id`，不列任何平台 ID。
- [x] Observer 记录 `channel.supervisor.ready/degraded/stopping`，并沿用 hash 后的 message/conversation 字段；observer/audit 自身失败不改变 lifecycle 或业务终态。

### Step 9.3 — Implement platform factories

- [x] `gateway.py` 只负责 `.env`、config、signal 和 supervisor entry；具体 Feishu/Telegram/Discord build 分到小 factory。
- [x] 每个 factory 创建自己的 observer、manager、approval controller、experience、delivery、transport，但复用同一个 `AgentRuntime.service`。
- [x] Feishu factory 用 Task 2/4 新公共接口，功能行为不变。
- [x] supervisor 不循环重建 runtime；平台 reconnect 由 Transport/SDK 负责。

Run GREEN:

```bash
uv run python -m unittest tests.test_channel_supervisor tests.test_gateway \
  tests.test_runtime tests.test_channel_observability -v
uv run ruff check src/miniclaw/channels/supervisor.py src/miniclaw/gateway.py \
  src/miniclaw/runtime.py tests/test_channel_supervisor.py tests/test_gateway.py \
  tests/test_channel_observability.py
```

### Step 9.4 — Commit

```bash
git add src/miniclaw/channels/supervisor.py src/miniclaw/gateway.py \
  src/miniclaw/runtime.py tests/test_channel_supervisor.py tests/test_gateway.py \
  tests/test_channel_observability.py
git commit -m "feat(gateway): 增加 single-runtime multi-pipeline Supervisor"
```

---

## Task 10: Extend offline Doctor for three Channels

**Purpose:** 用户在真正联网前就能区分 disabled、配置错误、SDK 缺失、Token 缺失和“本地已准备好”；Doctor 不冒充平台认证。

**Files:**

- Modify: `src/miniclaw/doctor.py`
- Modify: `tests/test_doctor.py`
- Modify: `src/miniclaw/cli.py`
- Modify: `tests/test_cli.py`

### Step 10.1 — RED the result matrix

- [ ] 新增固定检查：`telegram_config/sdk/runtime`、`discord_config/sdk/runtime`，保留 Feishu checks 和 `channel_database`。
- [ ] disabled：PASS + `not required/not started`；enabled 正确：PASS + `locally ready`；SDK/Token/关系错误：对应 FAIL。
- [ ] 检查不调用 Telegram `get_me()`、Discord login、DNS 或 HTTP；fake function 断言网络调用数 0。
- [ ] 输出不显示 Token value、完整 Owner/Chat/Guild/Channel ID。
- [ ] schema 检查继续验证 v2 表和索引；不无理由升级版本。
- [ ] 所有 enabled Channel 的 `worker_count` 总和超过 8 时 Doctor 给 WARN，不阻止个人用户显式配置；默认三平台总并发为 6。

Run RED:

```bash
uv run python -m unittest tests.test_doctor tests.test_cli -v
```

Expected RED: Doctor 没有 Telegram/Discord checks，旧固定 count 断言失败。

### Step 10.2 — Implement per-channel pure checks

- [ ] 用 channel config + injected `find_spec` + environment mapping 生成结果，不实例化 Transport。
- [ ] CLI help 说明 `gateway` 会启动所有 enabled Channels，Doctor 只证明 local readiness。

Run GREEN:

```bash
uv run python -m unittest tests.test_doctor tests.test_cli -v
uv run ruff check src/miniclaw/doctor.py src/miniclaw/cli.py \
  tests/test_doctor.py tests/test_cli.py
```

### Step 10.3 — Commit

```bash
git add src/miniclaw/doctor.py src/miniclaw/cli.py \
  tests/test_doctor.py tests/test_cli.py
git commit -m "feat(doctor): 增加 Telegram/Discord offline readiness diagnostics"
```

---

## Task 11: Add 20 versioned Channel regression cases and 640-check local soak

**Purpose:** 把每个版本必须通过的 Claw-like 核心场景固化为可重复、无网络、带稳定 evidence 的回归资产。

**Files:**

- Create: `evals/scenarios/telegram-channel.v1.jsonl`
- Create: `evals/scenarios/discord-channel.v1.jsonl`
- Create: `src/miniclaw/evals/multi_channel.py`
- Modify: `src/miniclaw/evals/cases.py`
- Modify: `src/miniclaw/evals/runner.py`
- Create: `tests/test_multi_channel_evals.py`
- Modify: `tests/test_eval_cases.py`
- Modify: `tests/test_eval_runner.py`

### Step 11.1 — RED versioned fixture loading

- [ ] Loader 发现并按 case ID 排序加载新 JSONL；重复 ID、未知 fixture、缺字段、额外字段、错误 version fail closed。
- [ ] 两个文件各精确 10 条，不删除或改写原 12 条 Feishu cases。
- [ ] 固定 ID：

```text
TELEGRAM-DM-001
TELEGRAM-GROUP-001
TELEGRAM-GROUP-002
TELEGRAM-REPLY-001
TELEGRAM-DEDUPE-001
TELEGRAM-TOOL-001
TELEGRAM-APPROVAL-001
TELEGRAM-DELIVERY-001
TELEGRAM-RESTART-001
TELEGRAM-ISOLATION-001

DISCORD-DM-001
DISCORD-GUILD-001
DISCORD-GUILD-002
DISCORD-THREAD-001
DISCORD-DEDUPE-001
DISCORD-TOOL-001
DISCORD-APPROVAL-001
DISCORD-DELIVERY-001
DISCORD-RESTART-001
DISCORD-ISOLATION-001
```

Run RED:

```bash
uv run python -m unittest \
  tests.test_multi_channel_evals.MultiChannelFixtureContractTest -v
```

Expected RED: loader 只认识现有 Feishu channel fixture。

### Step 11.2 — Implement real local vertical fixtures

- [ ] Adapter cases 调用真实 pure adapter。
- [ ] dedupe/restart 调用真实 SQLite repositories 与 Manager recovery。
- [ ] Tool case 调用真实 `ReadFileTool + WorkspaceGuard + Policy`。
- [ ] Approval case 调用真实 neutral v2 parser/controller，验证 once/deny 与非 Owner。
- [ ] Delivery case 调用真实 splitter、DeliveryWorker 和 fake platform error。
- [ ] Isolation case 同时起两个 fake ChannelRuntime：一个失败，另一个完成 durable reply。
- [ ] evidence 只使用稳定短语，不包含正文、路径、Secret 或外部 ID。

### Step 11.3 — Make repeat semantics explicit

- [ ] `--suite channel --repeat 20` 每轮重新创建临时 state，不能让上一轮 SQLite 数据影响下一轮。
- [ ] 汇总必须报告 `cases_per_run=32`、`repeat=20`、`checks=640`；任一 case 失败返回非零。
- [ ] `--json` 输出包含 commit、suite version、case IDs、passed/failed/duration，不包含环境或原始消息。

Run GREEN and soak:

```bash
uv run python -m unittest tests.test_multi_channel_evals \
  tests.test_eval_cases tests.test_eval_runner -v
uv run miniclaw eval run --suite channel --root evals/scenarios
uv run miniclaw eval run --suite channel --repeat 20 --root evals/scenarios
```

Expected GREEN: 至少 `32/32`，soak 至少 `640/640`。

### Step 11.4 — Commit

```bash
git add evals/scenarios/telegram-channel.v1.jsonl \
  evals/scenarios/discord-channel.v1.jsonl src/miniclaw/evals/multi_channel.py \
  src/miniclaw/evals/cases.py src/miniclaw/evals/runner.py \
  tests/test_multi_channel_evals.py tests/test_eval_cases.py tests/test_eval_runner.py
git commit -m "test(channel): 固化 20 个 Telegram/Discord regression scenarios"
```

---

## Task 12: Add truthful, secret-free live harnesses

**Purpose:** 给未来有凭据的维护者一条可复现的真实验收路径；脚本不主动骚扰任何用户/群，也不把跳过写成通过。

**Files:**

- Create: `scripts/telegram_live_smoke.py`
- Create: `scripts/discord_live_smoke.py`
- Create: `tests/test_channel_live_harness.py`
- Modify: `.gitignore`

### Step 12.1 — RED safety contract

- [ ] 不带 `--confirm-live` 必须退出非零且不读取 Token、不联网。
- [ ] enabled config、Doctor/preflight、commit SHA 缺失时 fail closed。
- [ ] 脚本不得调用 send API；只启动/提示人工在另一个客户端发规定消息，读取本地匿名状态计数。
- [ ] evidence 输出固定到 ignored `.local/eval-results/<channel>/`。
- [ ] JSON 只允许：channel、commit、started/finished time、check name、pass/fail/skip、匿名计数。
- [ ] 任一 fail 或 skip 返回非零；Token、完整 ID、username、chat/guild name、message body、截图不落盘。

Run RED:

```bash
uv run python -m unittest tests.test_channel_live_harness -v
```

Expected RED: 两个脚本不存在。

### Step 12.2 — Implement fifteen-step checklist harness

- [ ] 覆盖 design 第 25.7 节的 15 项：auth ready、DM、group/guild mention、reply/thread、memory restart、read Tool、approval/deny、non-owner、dedupe、long text、rate limit、restart recovery、network reconnect、experience fallback、secret scan。
- [ ] 当前无法真实执行的平台在 release record 中明确 `LIVE PENDING`，不生成伪 evidence 文件。

Run GREEN:

```bash
uv run python -m unittest tests.test_channel_live_harness -v
uv run ruff check scripts/telegram_live_smoke.py scripts/discord_live_smoke.py \
  tests/test_channel_live_harness.py
```

### Step 12.3 — Commit

```bash
git add .gitignore scripts/telegram_live_smoke.py scripts/discord_live_smoke.py \
  tests/test_channel_live_harness.py
git commit -m "test(live): 增加 secret-free Telegram/Discord acceptance harness"
```

---

## Task 13: Update all engineering, operations and progress documentation

**Purpose:** 让代码、配置示例、架构图、故障排查、测试数字和进度页面保持同一事实，不要求用户翻 commit 猜当前状态。

**Files:**

- Modify: `README.md`
- Modify: `docs/product/20260807_产品需求文档.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/engineering/phase-5/telegram-discord-channels.md`
- Create: `docs/engineering/phase-5/testing-and-live-acceptance.md`
- Create: `docs/engineering/phase-5/troubleshooting.md`
- Create: `docs/engineering/phase-5/completion-audit.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Modify: `docs/evals/README.md`
- Create: `docs/evals/releases/v0.5.0.md`
- Modify: `docs/progress/index.html`
- Create: `scripts/validate_docs.py`
- Create: `tests/test_documentation.py`
- Modify: `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html`
- Modify: `AGENTS.md`

### Step 13.1 — RED documentation fact scan

- [ ] 先建立检查命令，证明文档仍写 `DESIGN READY / IMPLEMENTATION PENDING`、12 cases、240 checks 或单 Feishu gateway。
- [ ] 记录将被同步的实际总测试数和 commit，不提前写预期数字。

Run RED:

```bash
rg -n "DESIGN READY|IMPLEMENTATION PENDING|12/12|240/240|only Feishu|单飞书" \
  README.md docs --glob '*.md' --glob '*.html'
```

Expected RED: 至少命中 Phase 5 尚未实现的进度文本。

### Step 13.2 — Write plain-language operational docs

- [ ] README 给出最短路径：安装 `channels` extra、配置一个或多个 Channel、Doctor、Gateway。
- [ ] 工程文档用 Mermaid 展示一个 Runtime、三个 pipeline、SQLite durable truth、Experience/Approval 流程和故障隔离。
- [ ] testing 文档解释 unit/integration/eval/soak/live 各自证明什么、不证明什么。
- [ ] troubleshooting 提供 SDK missing、Token missing、Telegram 409 conflict、Discord intents/403、rate limit、degraded pipeline、approval pending、secret scan 的具体排查步骤。
- [ ] 本地运行指南写 Telegram BotFather/Discord Developer Portal 的最小权限、Token 环境变量、allowlist 获取方式、启动和停止；不得放真实 ID 或截图中的 Secret。
- [ ] completion audit 建立“设计章节 → 代码 → 测试 → evidence → 状态”逐项表；`docs/evals/README.md` 增加 v0.5.0 发布入口。
- [ ] `docs/evals/releases/v0.5.0.md` 写实际 commits/gates；无 live evidence 的平台保持 `LIVE PENDING`。
- [ ] 两个 progress HTML 同步相同百分比、Phase、测试数字、下一步和 commit links。
- [ ] `scripts/validate_docs.py` 使用 stdlib 检查 Markdown 相对链接、fence/Mermaid 配对、HTML 基本结构和 required facts；`tests/test_documentation.py` 覆盖坏链接、未闭合 fence 与缺失 HTML closing tag。
- [ ] `AGENTS.md` 保留中英各半 commit 规范，并增加 Phase 5 gate 命令。
- [ ] 不修改用户拥有的 `docs/README.md`；工程索引使用 `docs/engineering/README.md`。

### Step 13.3 — Validate docs mechanically

```bash
uv run python -m unittest tests.test_documentation -v
uv run python scripts/validate_docs.py --root . \
  --html docs/progress/index.html \
  --html /Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html \
  --forbid-draft-markers
```

Expected: link、fence、Mermaid、HTML 和草稿标记检查全 PASS。

### Step 13.4 — Commit repo docs, then external progress separately

```bash
git add README.md docs/product/20260807_产品需求文档.md \
  docs/architecture/20260807_系统架构.md \
  docs/engineering/README.md docs/engineering/phase-5/telegram-discord-channels.md \
  docs/engineering/phase-5/testing-and-live-acceptance.md \
  docs/engineering/phase-5/troubleshooting.md \
  docs/engineering/phase-5/completion-audit.md \
  docs/getting-started/20260807_本地运行指南.md docs/evals/README.md \
  docs/evals/releases/v0.5.0.md \
  docs/progress/index.html scripts/validate_docs.py tests/test_documentation.py AGENTS.md
git commit -m "docs(phase5): 同步 multi-channel 架构、运维与真实进度"
```

外部 progress HTML 不属于 repo，只做 HTML 校验并在最终交付中给出可点击绝对路径。

---

## Task 14: Run complete release gates, audit requirements and publish `main`

**Purpose:** 用新鲜证据逐条关掉需求，不以“代码看起来完成”替代测试、soak、文档和远端核验。

**Files:**

- Modify: `docs/superpowers/plans/2026-08-08-phase-5-telegram-discord.md`
- Modify: `docs/evals/releases/v0.5.0.md`
- Modify: `docs/progress/index.html`
- Modify: `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html`

### Step 14.1 — Focused platform gates

- [ ] Telegram:

```bash
uv run python -m unittest tests.test_telegram_adapter tests.test_telegram_transport -v
```

- [ ] Discord:

```bash
uv run python -m unittest tests.test_discord_adapter tests.test_discord_transport -v
```

- [ ] Shared Core / Feishu compatibility:

```bash
uv run python -m unittest tests.test_channel_contracts tests.test_channel_experience \
  tests.test_channel_approvals tests.test_delivery tests.test_channel_manager \
  tests.test_channel_supervisor tests.test_gateway tests.test_feishu_adapter \
  tests.test_feishu_transport -v
```

### Step 14.2 — Full deterministic gates

- [ ] Python full suite:

```bash
uv run python -m unittest discover -s tests -v
```

- [ ] TypeScript compatibility:

```bash
pnpm --dir tui test
```

- [ ] Agent and Channel regression:

```bash
uv run miniclaw eval run --suite agent --root evals/scenarios
uv run miniclaw eval run --suite channel --root evals/scenarios
uv run miniclaw eval run --suite channel --repeat 20 --root evals/scenarios
```

- [ ] Static/build/docs:

```bash
uv run ruff check .
uv build
uv run python -m unittest tests.test_documentation -v
uv run python scripts/validate_docs.py --root . \
  --html docs/progress/index.html \
  --html /Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html
```

### Step 14.3 — Security and clean-diff gates

- [ ] Secret scan 只报告变量名，禁止打印命中的 value：

```bash
git grep -n -E '(bot[0-9]{6,}:|MINICLAW_(TELEGRAM|DISCORD)_BOT_TOKEN=.+|app_secret=.+)' \
  -- ':!uv.lock'
```

Expected: 无命中。

- [ ] 检查本次 diff 不包含用户文件：

```bash
git diff --check
git status --short
git diff --name-only d0e5031..HEAD
```

- [ ] 确认 `docs/README.md` 和两份架构草稿仍未被 stage/commit。

### Step 14.4 — Requirement-by-requirement audit

- [ ] 配置/extras/Doctor：Task 1、10 的 tests 和文档证据。
- [ ] 公共契约/Approval/Experience：Task 2–4 的 tests。
- [ ] Telegram Adapter/Transport：Task 5–6 的 tests。
- [ ] Discord Adapter/Transport：Task 7–8 的 tests。
- [ ] one Runtime/multi pipeline/failure isolation：Task 9 tests。
- [ ] 20 新 cases、≥32/32、≥640/640：Task 11 output。
- [ ] live harness 与诚实状态：Task 12、release record。
- [ ] 全文档和双 progress：Task 13 mechanical checks。
- [ ] 将每项写入 `docs/evals/releases/v0.5.0.md` 的 exit table，缺证据就不能勾选。

### Step 14.5 — Record final actuals and commit

- [ ] 把本计划所有已完成项改为 `[x]`，填入实际 Python/TS/eval/soak 数字和 commits。
- [ ] 将 progress 页从 `IMPLEMENTATION IN PROGRESS` 更新为真实状态：
  - deterministic gates 全绿：`IMPLEMENTATION PASS`；
  - Telegram/Discord 未做 live：分别保留 `LIVE PENDING`；
  - 只有两平台 15 项真实验收都通过，才允许 `PRODUCTION VERIFIED`。

```bash
git add docs/superpowers/plans/2026-08-08-phase-5-telegram-discord.md \
  docs/evals/releases/v0.5.0.md docs/progress/index.html
git commit -m "chore(phase5): 记录 full gates、soak 与 live status"
```

### Step 14.6 — Push and verify remote truth

```bash
git push origin main
git fetch origin main
git rev-parse HEAD
git rev-parse origin/main
git status --short
```

- [ ] `HEAD == origin/main`。
- [ ] 最终汇报列出实际 commit、测试数字、Channel cases、soak、live pending 原因和可点击文档路径。
- [ ] 如果 push 失败，Implementation 可以是本地 PASS，但必须明确写 `REMOTE PENDING`，不能声称已发布。

---

## Final evidence format

Phase 5 完成汇报统一使用下表，数字必须来自 Task 14 当次运行：

| Requirement | Evidence | Status |
| --- | --- | --- |
| One shared AgentRuntime | supervisor factory call count + integration test | PASS/FAIL |
| Telegram Adapter/Transport | focused test count | PASS/FAIL |
| Discord Adapter/Transport | focused test count | PASS/FAIL |
| Feishu compatibility | focused + old 12 cases | PASS/FAIL |
| Approval v2 + v1 compatibility | parser/delivery/controller tests | PASS/FAIL |
| Experience failure isolation | activity + final delivery tests | PASS/FAIL |
| Full Python/TypeScript | actual totals | PASS/FAIL |
| Agent regression | actual total | PASS/FAIL |
| Channel regression | actual total, must be ≥32 | PASS/FAIL |
| Local soak | actual total, must be ≥640 | PASS/FAIL |
| Build/Ruff/docs/HTML/secret scan | command outputs | PASS/FAIL |
| Telegram live | evidence path or no-credential reason | PASS/PENDING |
| Discord live | evidence path or no-credential reason | PASS/PENDING |
| Remote `main` | equal SHA values | PASS/PENDING |

这份表只允许三种结论：

- `IMPLEMENTATION PASS / LIVE PENDING`：完整代码和本地确定性 gate 通过，但真实平台未验收；
- `LIVE PARTIAL`：只有 Telegram 或 Discord 其中一个有真实 evidence；
- `PRODUCTION VERIFIED`：Telegram 与 Discord 均完成 15 项真实验收，且 Secret scan 为零。
