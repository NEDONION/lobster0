# Lobster0 v0.5.3 Stabilization Feishu / Discord Live Gate Implementation Plan

> 编号说明：历史路线称“Phase 5.3”；它是架构 Phase 5 之后的稳定化交付版本。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> 执行状态（2026-08-09）：Tasks 1～4 的 Core hardening 已由 `f856679` 合并，并在 `54db7b0` 固定
> 562-test 基线；Tasks 5～7 的 Feishu/Discord 严格 Live Evidence 仍待完成；Task 8 已同步当前事实但不能
> 预填 Live PASS；Task 9 只有完成真实 Gate 后才能关闭。下方未勾选框保留原始 TDD 操作清单，不作为当前
> 状态权威来源。

**Goal:** 修复 Feishu SDK 日志泄露与旧 Gateway 运行来源不明的问题，在真实飞书和隔离 Discord Server 上分别完成严格 15/15 Live Gate，并把可复核、脱敏的结果同步到发布文档与进度页。

**Architecture:** 保留现有共享 `AgentRuntime` 与三条独立 Channel pipeline；新增一个只作用于上游 SDK handler 的安全日志 Filter、一个状态目录级单实例 lease，以及一个供 Live Runner 复用的受管 Gateway 子进程。Feishu 继续使用 versioned `FEISHU-LIVE-001..015`，Discord 继续使用现有 15-check 人工 Harness；两者只增加运行来源证明和 fail-closed evidence，不重写 Transport 或 Agent Loop。

**Tech Stack:** Python 3.12+、标准库 `logging`/`fcntl`/`asyncio`/`subprocess`/`json`、SQLite、`unittest`、Ruff、official `lark-channel-sdk`、`discord.py`、现有 Lobster0 Eval/Channel 基础设施。

## Global Constraints

- 当前设计基线是 `docs/superpowers/specs/2026-08-09-phase-5-3-feishu-discord-live-gate-design.md`；范围变化必须先改设计。
- 先修 P0 SDK 日志脱敏，再启动任何新的真实飞书验收。
- 测试只能使用 sentinel Secret，不能读取、打印或提交真实 `.env`、Token、Ticket、平台 ID 或消息正文。
- `Feishu LIVE PASS` 和 `Discord LIVE PASS` 只能来自各自真实 15 项全部通过；fake SDK、offline case 和 640 soak 只能写 `IMPLEMENTATION PASS`。
- Live Runner 必须绑定 clean 40 位 commit，并由自己持有 Gateway 子进程；旧进程、重复进程、dirty worktree、skip 或 secret match 均 fail closed。
- Discord 只使用私有 `Lobster0 Test` Server、Owner 和 Bot；不授予 Administrator，不使用真实社区或工作群。
- Discord Bot Token 只能由用户在本机安全写入 `.env`；计划与终端输出只检查“是否存在”，不读取或回显值。
- Telegram 本阶段保持 `IMPLEMENTATION PASS / LIVE PENDING`。
- 每个代码任务执行 RED→GREEN，公共函数/类带准确类型和中文 docstring。
- 提交标题保留英文工程术语并用中文说明目的，不使用 100% 纯英文标题。

---

## File Map

| 文件 | 职责 |
| --- | --- |
| `src/lobster0/channels/sdk_logging.py` | 复制并脱敏交给 Feishu SDK handler 的 `LogRecord` |
| `tests/test_channel_sdk_logging.py` | Sentinel URL、参数、异常、幂等安装与原记录不变回归 |
| `src/lobster0/gateway_lease.py` | 状态目录级 non-blocking 单实例锁与本地运行来源元数据 |
| `tests/test_gateway_lease.py` | 重复进程、stale 文件、权限、元数据与释放回归 |
| `src/lobster0/gateway.py` | 启动时安装 SDK Filter、获取 lease，并在所有退出路径释放 |
| `src/lobster0/evals/gateway_process.py` | 平台无关的受管 Gateway 子进程、精确 ready、输出排空与有界 SIGTERM |
| `src/lobster0/evals/feishu_live.py` | 复用受管进程并把 provenance 纳入严格 evidence |
| `src/lobster0/evals/live.py` | Discord/Telegram Harness 自己管理 Gateway、验证 clean commit 与证据 schema |
| `tests/test_feishu_live_e2e.py` | Feishu 进程迁移、provenance、report 与失败关闭测试 |
| `tests/test_channel_live_harness.py` | Discord 受管 Gateway、dirty/peer/skip/secret 与 report 测试 |
| `docs/evals/releases/v0.5.3.md` | 只写已实际通过的 release gate 事实 |
| Phase 5 docs / README / progress HTML | 同步运行、验收、故障定位和里程碑状态 |

---

### Task 1: Feishu SDK LogRecord 脱敏边界

**Files:**
- Create: `src/lobster0/channels/sdk_logging.py`
- Modify: `src/lobster0/gateway.py`
- Create: `tests/test_channel_sdk_logging.py`
- Modify: `tests/test_gateway.py`

**Interfaces:**
- Consumes: 已由 `lark_channel` 创建的 `logging.getLogger("Lark")` handlers。
- Produces: `redact_sdk_text(value: object) -> str`、`SafeSdkLogFilter.filter(record: logging.LogRecord) -> logging.LogRecord`、`install_feishu_sdk_log_filter(logger: logging.Logger | None = None) -> int`。
- Invariant: Filter 返回副本，不修改原始 `LogRecord`，不改变 SDK 请求 URL，也不吞掉安全的连接/重连诊断。

- [ ] **Step 1: 写 URL、键值和异常的失败测试**

```python
def test_filter_redacts_websocket_query_and_exception_without_mutating_source() -> None:
    logger = logging.Logger("Lark.test")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    install_feishu_sdk_log_filter(logger)

    source = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        "connecting %s",
        ("wss://example.invalid/ws/v2?access_key=ACCESS_SENTINEL&ticket=TICKET_SENTINEL&device_id=DEVICE_SENTINEL",),
        None,
    )
    logger.handle(source)

    rendered = stream.getvalue()
    self.assertIn("wss://example.invalid/ws/v2?<redacted>", rendered)
    for sentinel in ("ACCESS_SENTINEL", "TICKET_SENTINEL", "DEVICE_SENTINEL"):
        self.assertNotIn(sentinel, rendered)
    self.assertIn("ACCESS_SENTINEL", source.getMessage())
```

再覆盖：`access_key=...`/`ticket=...`/`token=...`/`device_id=...` 不在 URL 中、`Authorization: Bearer ...`、`exc_info`、`stack_info`、`__str__` 抛异常的对象。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run python -m unittest tests.test_channel_sdk_logging -v`

Expected: `ModuleNotFoundError: lobster0.channels.sdk_logging`。

- [ ] **Step 3: 实现最小 Filter**

```python
class SafeSdkLogFilter(logging.Filter):
    """为单个 handler 返回已脱敏的 LogRecord 副本。"""

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        clone = copy.copy(record)
        try:
            message = record.getMessage()
        except Exception:
            message = f"<unprintable:{type(record.msg).__name__}>"
        if record.exc_info is not None:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
        clone.msg = redact_sdk_text(message)
        clone.args = ()
        clone.exc_info = None
        clone.exc_text = None
        clone.stack_info = redact_sdk_text(record.stack_info) if record.stack_info else None
        return clone
```

`redact_sdk_text()` 先把 `wss?://...?...` 的 query 整体替换为 `?<redacted>`，再处理独立敏感键和值以及 Bearer；所有路径均返回有限字符串且不抛异常。`install_feishu_sdk_log_filter()` 只给当前 handlers 增加一次带 `_lobster0_feishu_safe_log` 标记的 Filter，并返回新安装数量。

- [ ] **Step 4: 在 Gateway 中按正确顺序安装**

`run_gateway()` 中保持：加载 `.env`/配置 → preflight → `_configure_channel_logging()` → `install_feishu_sdk_log_filter()` → 构造 Supervisor/连接。`prepare_gateway_sdk_runtime()` 已在 CLI 的 `asyncio.run` 前导入 SDK，因此 handler 在 Filter 安装时已经存在。

在 `tests/test_gateway.py` patch installer，断言创建 Supervisor 前精确调用一次；preflight 失败时不安装、不联网。

- [ ] **Step 5: GREEN 与回归**

Run: `uv run python -m unittest tests.test_channel_sdk_logging tests.test_gateway tests.test_feishu_transport -v`

Expected: 全部 PASS；captured stream 中 sentinel 命中数为 0。

- [ ] **Step 6: Commit**

```bash
git add src/lobster0/channels/sdk_logging.py src/lobster0/gateway.py tests/test_channel_sdk_logging.py tests/test_gateway.py
git commit -m "fix(logging): 脱敏 Feishu SDK connection query"
```

### Task 2: 单 Gateway Lease 与本地运行来源

**Files:**
- Create: `src/lobster0/gateway_lease.py`
- Modify: `src/lobster0/gateway.py`
- Create: `tests/test_gateway_lease.py`
- Modify: `tests/test_gateway.py`

**Interfaces:**
- Consumes: `paths.run / "gateway.lock"`、环境中的非秘密 `LOBSTER0_GATEWAY_COMMIT`。
- Produces: `GatewayProvenance(pid: int, started_at: str, commit: str)`、`GatewayLease.acquire(path: Path, *, commit: str) -> GatewayLease`、`GatewayLease.close() -> None`。
- Invariant: 同一 `LOBSTER0_HOME` 只有一个持锁 Gateway；lock 文件残留不等于锁仍被占用。

- [ ] **Step 1: 写重复实例和 stale 文件 RED**

```python
def test_second_lease_fails_closed_until_first_releases(self) -> None:
    first = GatewayLease.acquire(self.path, commit="a" * 40)
    with self.assertRaises(GatewayLeaseError) as raised:
        GatewayLease.acquire(self.path, commit="a" * 40)
    self.assertEqual(raised.exception.code, "gateway_already_running")
    first.close()
    second = GatewayLease.acquire(self.path, commit="b" * 40)
    second.close()
```

再断言：文件 mode 为 `0600`；JSON 只有 `schema_version/pid/started_at/commit`；未知 commit 写 `unknown`；symlink、非普通文件、过宽权限与不可写目录 fail closed；`close()` 幂等。

- [ ] **Step 2: 确认 RED**

Run: `uv run python -m unittest tests.test_gateway_lease -v`

Expected: 缺少 `lobster0.gateway_lease`。

- [ ] **Step 3: 用 `fcntl.flock` 实现 lease**

```python
descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
```

获取锁后 `ftruncate`，写入 `allow_nan=False` 的有界 JSON，`fsync`；异常只抛 `GatewayLeaseError` 的稳定 code，不包含路径、PID 或原异常。目标平台是当前已支持的 macOS/Linux 和 Linux container，不新增第三方 lock 依赖。

- [ ] **Step 4: 接到生产生命周期**

`run_gateway()` 在所有 preflight 成功后、构造任何 Transport 前获取 lease；在 `finally` 先完成 Supervisor 关闭，再释放 lease。commit 只接受 `[0-9a-f]{40}`，普通手工启动缺失时记录 `unknown`，Live Runner 会显式注入精确值。

- [ ] **Step 5: GREEN 与生命周期回归**

Run: `uv run python -m unittest tests.test_gateway_lease tests.test_gateway tests.test_channel_supervisor -v`

Expected: 重复实例在网络连接前得到 `gateway_already_running`；正常和异常退出均可再次获取 lease。

`run_gateway()` 捕获 `GatewayLeaseError` 后只把 `error.code` 转成 `GatewayConfigError`，CLI 返回配置类退出码且不显示
lock path、PID 或原始 `OSError`。

- [ ] **Step 6: Commit**

```bash
git add src/lobster0/gateway_lease.py src/lobster0/gateway.py tests/test_gateway_lease.py tests/test_gateway.py
git commit -m "feat(gateway): 增加 single-instance lease 与 provenance"
```

### Task 3: 平台无关 Managed Gateway 与 Live Evidence

**Files:**
- Create: `src/lobster0/evals/gateway_process.py`
- Modify: `src/lobster0/evals/feishu_live.py`
- Modify: `src/lobster0/evals/live.py`
- Modify: `tests/test_feishu_live_e2e.py`
- Modify: `tests/test_channel_live_harness.py`

**Interfaces:**
- Consumes: clean 40 位 commit、绝对 `project_root/home`、精确 ready line。
- Produces: `ManagedGateway.start(...) -> ManagedGateway`，属性 `ready`、`provenance`、`bounded_diagnostics`，以及 `stop(timeout=10.0) -> int`。
- `GatewayProcess` 从 `feishu_live.py` 移除；Feishu 和 Discord 复用同一实现。

- [ ] **Step 1: 写受管进程 RED**

```python
gateway = await ManagedGateway.start(
    project_root=PROJECT_ROOT,
    home=self.home,
    ready_line="Lobster0 gateway ready: discord/default",
    commit="c" * 40,
    ready_timeout=1.0,
    command=(sys.executable, str(fake_gateway)),
)
self.assertTrue(gateway.ready)
self.assertEqual(gateway.provenance.commit, "c" * 40)
self.assertGreater(gateway.provenance.pid, 0)
self.assertRegex(gateway.provenance.started_at, UTC_PATTERN)
self.assertEqual(await gateway.stop(), 0)
```

覆盖 exact marker、substring 不算 ready、提前退出、timeout、双 SIGTERM 后仍不退出、stdout/stderr 持续排空、每行/总诊断上界，以及子进程环境只增加 `LOBSTER0_GATEWAY_COMMIT` 而不序列化 Secret。

- [ ] **Step 2: 确认 RED**

Run: `uv run python -m unittest tests.test_feishu_live_e2e.ManagedGatewayProcessTest -v`

Expected: `ManagedGateway` 尚不存在。

- [ ] **Step 3: 提取并实现 `ManagedGateway`**

沿用现有 Feishu `GatewayProcess` 的双 drain task、精确整行 marker、process group SIGTERM 与有界 diagnostics；ready line 改成构造参数。`asyncio.create_subprocess_exec` 使用 exact argv、`cwd=project_root`，环境为 `dict(os.environ)` 加 40 位 commit。

- [ ] **Step 4: Feishu Runner 迁移并扩展 report**

`LiveExecution` 增加 `gateway: GatewayProvenance | None`；`build_evidence_report()` 的 `gateway` 固定字段改为：

```json
{
  "ready": true,
  "graceful_exit": true,
  "pid": 123,
  "started_at": "2026-08-09T00:00:00.000000Z",
  "commit": "cccccccccccccccccccccccccccccccccccccccc"
}
```

PID/时间只存在 ignored Evidence，不进入 release 文档。commit 与顶层 commit 不同、PID 非正整数、时间非法时 report 拒绝。现有 15-case、Secret scan 与 restart 语义保持不变。

- [ ] **Step 5: Discord Harness 改为自己管理 Gateway**

`run_live_harness("discord")` 的执行顺序改为：确认门 → 配置/Doctor/preflight → 选择平台已启用且 peer channels 关闭 → 40 位 commit + clean worktree → 启动 `ManagedGateway` → 15 项 p/f/s → Secret scan/DB 聚合 → finally 优雅停止 → 写 evidence。

ready line 从 typed config 生成：

```python
ready_line = f"Lobster0 gateway ready: {channel}/{selected.account_id}"
```

Evidence 新增与 Feishu 同结构的 `gateway`；任意 skip/fail、Gateway 非优雅退出、commit mismatch 或 repository 变化均返回 1。确认门前仍不能读取 state、Secret 或创建目录。

- [ ] **Step 6: 跑聚焦 GREEN**

Run: `uv run python -m unittest tests.test_feishu_live_e2e tests.test_channel_live_harness -v`

Expected: 全部 PASS，两个 report schema 都拒绝正文、完整平台 ID、绝对路径和未知字段。

- [ ] **Step 7: Commit**

```bash
git add src/lobster0/evals/gateway_process.py src/lobster0/evals/feishu_live.py src/lobster0/evals/live.py tests/test_feishu_live_e2e.py tests/test_channel_live_harness.py
git commit -m "test(live): 绑定 managed Gateway commit 与运行来源"
```

### Task 4: 本地发布门禁与 SDK Secret 扫描

**Files:**
- Modify only if a regression requires it: files from Tasks 1–3

**Interfaces:**
- Consumes: Tasks 1–3 的代码。
- Produces: 可进入真实平台的 clean candidate commit；不生成 LIVE PASS。

- [ ] **Step 1: 聚焦安全扫描**

Run: `uv run python -m unittest tests.test_channel_sdk_logging tests.test_gateway_lease tests.test_gateway tests.test_feishu_transport tests.test_feishu_live_e2e tests.test_channel_live_harness -v`

Expected: 0 failures；所有 sentinel 在捕获日志与 report 中命中 0。

- [ ] **Step 2: 全量 Python 与 Ruff**

Run: `uv run python -m unittest discover -s tests -v`

Run: `uv run ruff check .`

Expected: 0 failures，Ruff `All checks passed!`。

- [ ] **Step 3: Agent / Channel regression**

Run: `uv run lobster0 eval run --suite offline --root evals/scenarios`

Run: `uv run lobster0 eval run --suite channel --root evals/scenarios`

Run: `uv run lobster0 eval run --suite channel --repeat 20 --json --root evals/scenarios`

Expected: offline 29/29、versioned Channel 32/32、soak 640/640；若当前 versioned 场景数因已提交变更增加，以报告中的全部 case 为准且不得 skip。

- [ ] **Step 4: TUI、docs、lock、build**

Run: `pnpm --dir tui test`

Run: `pnpm --dir tui build`

Run: `uv run python scripts/validate_docs.py`

Run: `uv lock --check`

Run: `uv build`

Run: `git diff --check`

Expected: 全部 exit 0。

- [ ] **Step 5: 创建 candidate commit（仅在修复了门禁问题时）**

若没有门禁修复，不创建 commit。若修复落在 Tasks 1–3 的既定文件，逐个写出实际路径执行 `git add`，并使用：

```bash
git commit -m "fix(gate): 修复 Phase 5.3 release regression"
```

不得创建空 commit，也不得用删断言、skip 或降低安全条件解决失败。

### Task 5: Feishu Strict 15-case Live Gate

**Files:**
- Local ignored evidence: `.local/eval-results/feishu/YYYYMMDDTHHMMSSZ.json`
- No tracked file until Task 8 records verified facts

**Interfaces:**
- Consumes: clean candidate commit、现有 Owner DM/专用测试群、`evals/scenarios/feishu-live.v1.jsonl`。
- Produces: `FEISHU_E2E_VERIFIED` report，15/15、Gateway graceful、Secret scan 0。

- [ ] **Step 1: 停止旧 Gateway 并核验配置存在性**

优雅停止当前 Lobster0 Gateway；不要 `kill -9`。只运行布尔检查确认模型 Key、Feishu App ID/Secret 已配置，不输出值。运行 `uv run lobster0 doctor`，确认 Feishu locally ready。

- [ ] **Step 2: 在 clean commit 启动 strict Runner**

Run: `uv run python scripts/feishu_live_smoke.py --confirm-live`

Expected: Runner 自己启动唯一 Gateway，显示 typed config 中 `account_id` 对应的 exact Feishu ready line，不依赖外部旧进程。

- [ ] **Step 3: 执行 15 条 versioned Query**

严格按 Runner 逐项显示的目标、固定 Query 和发送身份执行；Owner DM/测试群每次只发当前 action。对客户端可见项输入 p/f/s 前核对：短回复单卡；长回复仅 suffix 跟在卡片下；Approval 只一张卡；restart 不重复；Unicode/emoji/代码块无损；未 mention/非 Owner 静默。

- [ ] **Step 4: 验证 Evidence**

报告必须满足：`cases_total=15`、`cases_passed=15`、failed/skipped/secret_matches 全为 0、`gateway.ready=true`、`gateway.graceful_exit=true`、gateway commit 等于顶层 commit。只读取字段名和匿名计数，不输出 query、reply 或平台 ID。

- [ ] **Step 5: 失败处理**

任一失败保持 `Feishu LIVE PENDING`。记录稳定 error code，回到相应 RED→GREEN；不得编辑 JSON 把 fail 改 pass。修复后提交新的 candidate，并从完整 15-case 重跑。

### Task 6: Discord App、Bot 与私有 Test Server

**Files:**
- Local only: `.env`
- Local only: selected `LOBSTER0_HOME/config.toml`
- No Secret or numeric platform ID in tracked files

**Interfaces:**
- Consumes: 用户已登录的 Discord Developer Portal。
- Produces: Application/Bot `Lucas 的 Lobster0`、私有 `Lobster0 Test`、`#lobster0-live`、`#lobster0-thread-lab` 和 `validation-thread`。

- [ ] **Step 1: 创建或复用专用 Application/Bot**

在 Developer Portal 核对名称。只打开 Guilds、Guild Messages、Direct Messages 和 Message Content；关闭 Presence 与 Server Members privileged intent。若 Token 曾显示在日志/对话/截图中，先 Reset。

- [ ] **Step 2: 用户本机安全保存 Token**

由用户把 Token 写入 `.env` 的 `LOBSTER0_DISCORD_BOT_TOKEN`。Codex 只运行不回显值的布尔/长度下限检查；不读取 `.env` 文本，不把 Token 放进 argv、patch、clipboard 输出或 Evidence。

- [ ] **Step 3: 创建隔离 Server 与频道**

创建私有 `Lobster0 Test`，再创建 `#lobster0-live`、`#lobster0-thread-lab` 和 `validation-thread`。Server 初始只有 Owner 与 Bot。

- [ ] **Step 4: 最小 OAuth2 权限**

只授予 View Channel、Send Messages、Read Message History、Embed Links、Send Messages in Threads；只有 Bot 必须创建 thread 时才加 Create Public Threads。确认 Administrator、Manage Server、Manage Roles、Kick/Ban 均关闭。

- [ ] **Step 5: 写入本地 typed config**

只在本地 `config.toml` 填 `bot_token_env`、Owner/allowed user、guild/channel snowflake 与 `allow_guild_mentions=true`；真实数字不进入 Git 或聊天。严格 Discord Gate 时暂时关闭 Feishu/Telegram，完成后再按 isolation smoke 恢复 Feishu。

- [ ] **Step 6: Preflight**

Run: `uv run lobster0 doctor`

Expected: Discord config/runtime locally ready；缺 Token、Owner 不在 allowlist、guild mention 无 guild/channel allowlist 或 SDK 缺失必须 FAIL。

### Task 7: Discord 15-check 与双平台 Isolation Smoke

**Files:**
- Local ignored evidence: `.local/eval-results/discord/YYYYMMDDTHHMMSSZ.json`
- No tracked file until Task 8

**Interfaces:**
- Consumes: Task 6 的 private Server 和 clean candidate commit。
- Produces: Discord 15/15、Secret scan 0，以及 Feishu+Discord 同时在线 isolation 记录。

- [ ] **Step 1: 启动受管 Discord Harness**

Run: `uv run python scripts/discord_live_smoke.py --confirm-live`

Expected: Harness 自己启动并持有唯一 Gateway，exact ready 后才显示第一项；不再要求另一个终端手工启动未知进程。

- [ ] **Step 2: 完成 15 项而不跳过**

按 `auth_ready` 到 `secret_scan_zero` 的顺序在 Owner DM、`#lobster0-live` 与 `validation-thread` 操作。`dm_twenty_rounds` 必须真实 20 轮；Approval 的 allow once 只执行一次，deny 不执行；未寻址 guild 消息静默；long text 分片拼接无损；restart/reconnect 不重复 Turn。

- [ ] **Step 3: 非 Owner 场景**

仅在 `non_owner_denied` 时邀请一个明确测试账号进入测试频道，确认它不能触发 trusted automation 或决定 Approval；完成后移除。若没有安全测试账号，本项必须 skip，整个 Discord Gate 保持 LIVE PENDING，不能把 Owner 的第二会话冒充非 Owner。

- [ ] **Step 4: 验证 Discord Evidence**

报告必须是 pass=15、fail=0、skip=0、secret_matches=0；gateway ready/graceful/commit 一致；JSON 不含 Server 名、用户名、snowflake、消息正文、Token 或路径。

- [ ] **Step 5: 双平台 isolation smoke**

恢复 Feishu + Discord、保持 Telegram disabled，启动一个 production Gateway。两个平台各发送一个不含私人数据的唯一 nonce Owner DM；确认各自一个 Turn/回复。临时让 Discord pipeline reconnect，确认 Feishu 仍完成一轮；恢复 Discord 后观察 ready。最后优雅 SIGTERM，确认两个 pipeline 反序清理且 Runtime 只关闭一次。

- [ ] **Step 6: 失败处理**

任一 fail/skip、非 Owner 越权、重复回复、Secret 命中或单平台故障拖垮另一平台，都保持 Discord LIVE PENDING。修复必须新增 offline regression，再重跑本地门禁和完整 15 项。

### Task 8: Release Record、工程文档与两份进度页

**Files:**
- Create: `docs/evals/releases/v0.5.3.md`
- Modify: `README.md`
- Modify: `docs/product/20260807_产品需求文档.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/engineering/phase-5/20260808_testing-and-live-acceptance.md`
- Modify: `docs/engineering/phase-5/20260808_feishu-live-e2e.md`
- Modify: `docs/engineering/phase-5/20260808_feishu-gateway-runtime-and-macos-service.md`
- Modify: `docs/engineering/phase-5/20260808_telegram-discord-channels.md`
- Modify: `docs/engineering/phase-5/20260808_troubleshooting.md`
- Modify: `docs/engineering/phase-5/20260808_completion-audit.md`
- Modify: `docs/progress/index.html`
- Modify outside repo with explicit approval: `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/lobster0-progress.html`

**Interfaces:**
- Consumes: Tasks 4–7 的真实命令输出和 ignored evidence 匿名结论。
- Produces: 与实际 commit 和门禁一致的 Phase 5.3 release facts；不复制 private evidence。

- [ ] **Step 1: 先写事实矩阵，不预填 PASS**

Release Record 固定列：Gate、Command、Commit、Result、Evidence schema、Privacy result。只有已有 exit 0/evidence 的项写 PASS；其余写 PENDING 或 FAIL。PID、平台 ID、消息正文、用户名和本地路径不进入文档。

- [ ] **Step 2: 更新架构与工程数据流**

增加 SDK LogRecord Filter、Gateway Lease、Managed Live Process 与 evidence 流程图；明确 Filter 不改 SDK 请求、lease 按 `LOBSTER0_HOME` 隔离、Live Runner 自己持有进程、Telegram 仍 pending。

- [ ] **Step 3: 更新 README / PRD / Completion Audit**

功能矩阵必须精确区分：Feishu/Discord `LIVE PASS`（仅在真实 15/15 时）与 Telegram `IMPLEMENTATION PASS / LIVE PENDING`。若 Discord 因非 Owner 账号等原因不能 15/15，README 不得写 Discord 已接通完成。

- [ ] **Step 4: 更新进度 HTML**

仓库页和外部页显示同一 commit、测试数字、Live Gate 数和下一步。外部文件写入前请求一次明确文件系统批准；不从仓库页复制 Secret 或私有 evidence。

- [ ] **Step 5: 文档与内容安全校验**

Run: `uv run python scripts/validate_docs.py`

Run: `git diff --check`

Run: `rg -n "ACCESS_SENTINEL|TICKET_SENTINEL|DEVICE_SENTINEL|LOBSTER0_DISCORD_BOT_TOKEN=" README.md docs src tests`

Expected: docs validation PASS、diff check 0；最后一条只允许测试中的 sentinel 和文档中的空变量名示例，不允许真实值或 private ID。

- [ ] **Step 6: Commit**

```bash
git add README.md docs
git commit -m "docs(phase5): 同步 Feishu/Discord Live evidence 与进度"
```

### Task 9: 最终 Gate、Review、Push `main`

**Files:**
- No new files unless review identifies a tested defect

**Interfaces:**
- Consumes: 全部 Phase 5.3 commits。
- Produces: local `main == origin/main`，或明确列出未通过项而不宣称完成。

- [ ] **Step 1: 重跑完整发布门禁**

```bash
uv run python -m unittest discover -s tests -v
pnpm --dir tui test
pnpm --dir tui build
uv run lobster0 eval run --suite offline --root evals/scenarios
uv run lobster0 eval run --suite channel --root evals/scenarios
uv run lobster0 eval run --suite channel --repeat 20 --json --root evals/scenarios
uv run ruff check .
uv run python scripts/validate_docs.py
uv lock --check
uv build
git diff --check
```

Expected: 每条 exit 0；任何失败都必须报告真实状态。

- [ ] **Step 2: 需求逐条审查**

逐项对照 design spec 第 15 节的 13 个完成条件，读取 tracked diff、匿名 evidence 计数与当前 commit。检查没有第二个 Card/text 回复、没有 Approval 双卡、没有 SDK query、没有 Discord 管理员权限、没有 Telegram 假 LIVE PASS。

- [ ] **Step 3: 独立 code review**

重点复审：LogRecord 是否存在 handler 旁路；lease 是否在所有异常路径释放；Managed Gateway 是否会留下子进程；evidence 是否能被人工 p 覆盖自动失败；长回复/重启回归是否仍在；文档是否把计划写成事实。

- [ ] **Step 4: Push**

Run: `git push origin main`

Expected: push 成功后 `git rev-parse HEAD` 等于 `git rev-parse origin/main`。

- [ ] **Step 5: 最终汇报**

只报告 fresh verification：commit、测试通过数、Feishu/Discord 各自真实 pass/total、Telegram pending、文档路径、仍在运行的 Gateway 状态。未满足 13 条完成条件时写“Phase 5.3 未完成”并列出最小 blocker。

## Final Definition of Done

- [ ] P0 Feishu SDK connection log sentinel 0 matches；
- [ ] 同一 state home 的第二个 Gateway 在网络前拒绝；
- [ ] Feishu ignored evidence 为 15/15、gateway graceful、secret 0；
- [ ] Discord ignored evidence 为 15/15、gateway graceful、secret 0；
- [ ] 双平台 isolation smoke 通过；
- [ ] 全量 Python/TUI/Eval/Channel soak/Ruff/docs/lock/build 全绿；
- [ ] Release Record 和两份 progress page 与同一 commit 一致；
- [ ] tracked tree 不含 Secret、private evidence、平台 ID 或消息正文；
- [ ] `origin/main` 与本地交付 commit 一致。
