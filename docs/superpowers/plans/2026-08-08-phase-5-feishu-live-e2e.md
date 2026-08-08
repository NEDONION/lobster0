# Phase 5 Feishu Live E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建并接入一个真实飞书企业自建机器人，把“飞书消息 → MiniClaw Agent/Tool/Approval → 飞书回复”变成版本化、可取证、默认安全的 Live E2E 门禁。

**Architecture:** 保留现有 `FeishuTransport -> ChannelManager -> AgentRuntime -> DeliveryWorker` 生产链，不增加测试专用执行链。新 Live 模块只负责严格加载 15 条真实场景、管理真实 Gateway 子进程、只读比较 SQLite checkpoint、收集必要人工确认并输出脱敏 Evidence；平台创建、发布、发消息和审批仍由 Owner 完成。

**Tech Stack:** Python 3.12、标准库 `asyncio/sqlite3/json/subprocess/unittest`、现有 `lark-channel-sdk`、SQLite schema v2、JSONL eval、Mermaid/HTML 文档；不新增第三方依赖。

## Global Constraints

- 当前基线为 Python 483/483、TypeScript 27/27、Agent 28/28、Channel 32/32、20 轮 640/640；新增测试只能上调数字。
- 未提供 `--confirm-live` 时必须在读取 `.env`、状态目录、凭据或联网前退出 2。
- Live Runner 不能写 Inbox/Turn/ToolRun/Approval/Delivery/Audit 来制造成功，只允许只读取证。
- Live Runner 不能自动批准、点击卡片、发送 `/approve`、追加 `--yes` 或扩大白名单。
- App Secret、模型 Key、Token、完整 Open ID/Chat ID/Message ID、正文、Prompt、reasoning 和 Tool 参数不得进入 Git、日志或 Evidence。
- `lark-cli` Owner discovery 必须使用同一 Bot App 的独立命名 profile；不能覆盖默认 profile，也不能把 Secret 放在 argv。
- Gateway 使用现有出站 WebSocket，不增加公网 Webhook、第二套 Transport 或第二个 AgentRuntime。
- 真实群聊只申请 group-at-message 权限，不申请读取群内所有消息的敏感 Scope。
- Gateway 正常停止使用 SIGTERM 和现有两段式清理，Runner 不调用 `kill -9`。
- 每个生产行为先看到对应 unittest RED，再写最小 GREEN。
- 所有新增/修改顶层函数、方法和类使用准确类型标注与中文 docstring。
- 提交标题保留英文 type/工程术语，并用中文说明动作，例如 `test(feishu): 固化 Live scenario 与 evidence contract`。
- 真实飞书写操作只发生在用户的专用 Bot/测试私聊/测试群；任意平台高风险写操作仍需用户再次确认。
- 不读取或覆盖主检出目录中的用户文件；工作只发生在 `feat/feishu-live-e2e` 隔离分支。

---

## File Map

- `src/miniclaw/evals/cases.py`：扩展通用 JSONL 契约，增加严格的 live local/human evidence allowlist。
- `evals/scenarios/feishu-live.v1.jsonl`：15 条真实平台场景，不含凭据、外部 ID 或真实正文。
- `src/miniclaw/evals/feishu_live.py`：Feishu 专用 Live case 选择、SQLite checkpoint/观察、Gateway 生命周期、Evidence 与交互编排。
- `scripts/feishu_live_smoke.py`：保持现有脚本路径，只做薄入口。
- `tests/test_eval_cases.py`：Live JSONL schema RED/GREEN。
- `tests/test_feishu_live_e2e.py`：数据库取证、Gateway 监督、Evidence 和 Runner 契约。
- `tests/test_feishu_evals.py`：仓库固定 15 条 Live matrix 与未确认零副作用。
- `docs/engineering/phase-5/20260808_feishu-live-e2e.md`：真实 Bot 创建、Scope、Owner discovery、运行和验收手册。
- `docs/engineering/phase-5/20260808_testing-and-live-acceptance.md`：三平台状态和 Feishu Live gate。
- `docs/engineering/phase-5/20260808_troubleshooting.md`：同应用 Open ID、Scope、发布、WebSocket、审批和恢复排障。
- `docs/evals/releases/v0.5.1.md`：实现门禁与真实平台 Evidence；若未完成 Live，只能写 pending/partial。
- `README.md`、`docs/engineering/README.md`、`docs/architecture/20260807_系统架构.md`、`docs/progress/index.html`：入口和真实进度。
- `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html`：外部可点击进度页，不进入 Git。
- `scripts/validate_docs.py`：把新工程文档和 v0.5.1 纳入链接、Mermaid、HTML 与事实门禁。

---

### Task 1: Versioned Feishu Live Scenario Contract

**Files:**
- Modify: `src/miniclaw/evals/cases.py`
- Modify: `tests/test_eval_cases.py`
- Modify: `tests/test_feishu_evals.py`
- Create: `evals/scenarios/feishu-live.v1.jsonl`

**Interfaces:**
- Consumes: `load_cases(root: Path) -> tuple[EvalCase, ...]` 现有严格 JSONL loader。
- Produces: `EvalExpectation.live_local_evidence: tuple[str, ...]`、`EvalExpectation.live_human_evidence: tuple[str, ...]`、`load_feishu_live_cases(root: Path) -> tuple[EvalCase, ...]`。
- Live local key 固定为：`gateway_ready`、`inbox_completed`、`turn_completed`、`delivery_sent`、`one_session_three_turns`、`system_info_succeeded`、`read_file_succeeded`、`approval_pending`、`approval_consumed_once`、`approval_denied`、`no_new_turn`、`multiple_parts_sent`、`memory_survived_restart`、`transport_reconnected`、`secret_scan_zero`。
- Live human key 固定为：`reply_visible`、`context_answer_correct`、`system_info_visible`、`sentinel_visible`、`approval_prompt_visible`、`approved_result_visible`、`denial_visible`、`bot_silent`、`group_reply_visible`、`long_content_intact`、`restart_answer_correct`、`reconnect_reply_visible`。

- [ ] **Step 1: Write failing schema tests**

在 `tests/test_eval_cases.py` 增加：

```python
def live_case(case_id: str = "FEISHU-LIVE-001") -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": case_id,
        "title": "gateway ready",
        "status": "active",
        "layers": ["live"],
        "capability": "feishu_e2e",
        "query": "启动 Gateway，等待真实 WebSocket ready。",
        "turns": [],
        "setup": {"files": {}},
        "expected": {
            "live_local_evidence": ["gateway_ready"],
            "live_human_evidence": [],
        },
        "introduced_by": "phase-5.1",
        "tags": ["feishu", "live", "gateway"],
    }
```

覆盖：合法字段被加载；未知 evidence key、非字符串 key、非 `feishu_e2e` capability、active live 无 evidence、live case 携带 `offline`/`channel` fixture 均失败关闭。

- [ ] **Step 2: Run RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_eval_cases.EvalCaseLoaderTest -v
```

Expected: FAIL，因为 `live_local_evidence` 是未知字段，`EvalExpectation` 尚无对应属性。

- [ ] **Step 3: Implement minimal strict parser**

在 `src/miniclaw/evals/cases.py`：

```python
_LIVE_LOCAL_EVIDENCE = frozenset({...})
_LIVE_HUMAN_EVIDENCE = frozenset({...})

@dataclass(frozen=True, slots=True)
class EvalExpectation:
    ...
    live_local_evidence: tuple[str, ...]
    live_human_evidence: tuple[str, ...]

def load_feishu_live_cases(root: Path) -> tuple[EvalCase, ...]:
    """加载并返回严格的 active Feishu Live E2E 场景。"""
    cases = tuple(
        case
        for case in load_cases(root)
        if case.status == "active" and case.capability == "feishu_e2e"
    )
    if len(cases) != 15:
        raise EvalCaseError("feishu live suite must contain exactly 15 active cases")
    return cases
```

`_parse_expectation()` 用 `_strings()` 解析两个字段、拒绝重复 key 和未知枚举；`_parse_case()` 对 `feishu_e2e` 强制 `layers == ("live",)`、没有 offline response、没有 channel fixture、至少一项 evidence。

- [ ] **Step 4: Add exactly fifteen JSONL rows**

按设计规格写入 `FEISHU-LIVE-001` 至 `FEISHU-LIVE-015`。每行只使用合成 Query/nonce 占位符，包含完整 `introduced_by` 和 tags；不包含 `token`、`secret`、`app_id`、`open_id`、`chat_id` 字段。

- [ ] **Step 5: Add repository matrix test**

在 `tests/test_feishu_evals.py` 断言：

```python
live = load_feishu_live_cases(PROJECT_ROOT / "evals" / "scenarios")
self.assertEqual([case.id for case in live], [f"FEISHU-LIVE-{i:03d}" for i in range(1, 16)])
self.assertTrue(all(case.layers == ("live",) for case in live))
self.assertTrue(all(case.expected.live_local_evidence for case in live))
```

- [ ] **Step 6: Run GREEN and repository validation**

```bash
.venv/bin/python -m unittest tests.test_eval_cases tests.test_feishu_evals -v
.venv/bin/python -m miniclaw eval validate --root evals/scenarios
```

Expected: PASS；总 validate case 数比基线上调 15，但 offline 28 与 channel 32 不变。

- [ ] **Step 7: Commit**

```bash
git add src/miniclaw/evals/cases.py tests/test_eval_cases.py tests/test_feishu_evals.py evals/scenarios/feishu-live.v1.jsonl
git commit -m "test(feishu): 固化 Live scenario 与 evidence contract"
```

---

### Task 2: Read-only SQLite Evidence Probe

**Files:**
- Create: `src/miniclaw/evals/feishu_live.py`
- Create: `tests/test_feishu_live_e2e.py`

**Interfaces:**
- Consumes: SQLite schema v2、Task 1 的 `EvalCase.expected.live_local_evidence`。
- Produces:

```python
@dataclass(frozen=True, slots=True)
class DatabaseCheckpoint:
    processed_event_rowid: int
    turn_id: int
    tool_run_id: int
    approval_id: int
    delivery_id: int
    audit_event_id: int

@dataclass(frozen=True, slots=True)
class EvidenceEvaluation:
    passed: tuple[str, ...]
    failed: tuple[str, ...]

def capture_checkpoint(database: Path) -> DatabaseCheckpoint: ...
def evaluate_local_evidence(
    database: Path,
    checkpoint: DatabaseCheckpoint,
    requirements: tuple[str, ...],
) -> EvidenceEvaluation: ...
```

- [ ] **Step 1: Write RED tests for checkpoint and positive evidence**

测试用 `initialize_state()` 建临时 schema，再插入一条真实关联链：feishu session、completed processed event、completed turn、`system_info` succeeded ToolRun、sent Delivery 和 connected Audit。断言 checkpoint 之前的行被忽略，之后的行满足对应 evidence。

- [ ] **Step 2: Write RED tests for negative/sensitive behavior**

覆盖：

- `no_new_turn` 只有在 checkpoint 后没有 Feishu Turn 时通过；
- Telegram/Discord 行不能满足 Feishu evidence；
- `approval_consumed_once` 要求一个 consumed Approval 且同 ToolRun 只执行一次；
- `multiple_parts_sent` 要求同一个内部 message 至少两个连续 part 全为 sent；
- `one_session_three_turns` 要求同一 Feishu session 恰好至少 3 个新 completed Turn；
- `transport_reconnected` 要求 checkpoint 后同时出现 reconnecting 与 connected Audit；
- SQL/异常只返回稳定 `evidence_database_unavailable`，不回显数据库路径、正文或外部 ID。

- [ ] **Step 3: Run RED**

```bash
.venv/bin/python -m unittest tests.test_feishu_live_e2e.FeishuDatabaseProbeTest -v
```

Expected: ImportError/AttributeError，因为 `feishu_live.py` 与接口不存在。

- [ ] **Step 4: Implement minimal read-only probe**

所有查询必须使用 `Database(path).connect_read_only()`。固定 SQL 表名，参数只绑定 `channel="feishu"` 和内部 ID；不把外部 ID 或 content 放进返回对象。对每个 requirement 使用私有函数映射：

```python
_EVIDENCE_CHECKS: dict[str, Callable[[sqlite3.Connection, DatabaseCheckpoint], bool]] = {
    "inbox_completed": _has_completed_inbox,
    "turn_completed": _has_completed_turn,
    ...
}
```

未知 requirement 先于数据库访问抛 `FeishuLiveError("unknown_local_evidence")`。

- [ ] **Step 5: Run GREEN and related storage tests**

```bash
.venv/bin/python -m unittest tests.test_feishu_live_e2e.FeishuDatabaseProbeTest tests.test_channel_storage tests.test_channel_observability -v
```

- [ ] **Step 6: Commit**

```bash
git add src/miniclaw/evals/feishu_live.py tests/test_feishu_live_e2e.py
git commit -m "feat(feishu): 增加 read-only Live evidence probe"
```

---

### Task 3: Bounded Gateway Process Supervisor

**Files:**
- Modify: `src/miniclaw/evals/feishu_live.py`
- Modify: `tests/test_feishu_live_e2e.py`

**Interfaces:**
- Consumes: 当前 Python、项目根、MiniClaw home 和 `.env` 所在 cwd。
- Produces:

```python
class GatewayProcess:
    @classmethod
    async def start(
        cls,
        *,
        project_root: Path,
        home: Path,
        ready_timeout: float,
    ) -> "GatewayProcess": ...

    async def stop(self, *, timeout: float = 10.0) -> int: ...
    @property
    def ready(self) -> bool: ...
    @property
    def bounded_diagnostics(self) -> tuple[str, ...]: ...
```

- [ ] **Step 1: Write fake subprocess fixtures and RED tests**

在临时目录生成只用于测试的 Python 脚本：

```python
print("MiniClaw gateway ready: feishu/default", flush=True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
time.sleep(60)
```

测试通过可注入的 `command` 参数启动 fixture；覆盖 ready、ready 超时、ready 前退出、stderr 大量输出不死锁、SIGTERM 正常退出、第一次超时后第二个 SIGTERM、诊断每行/总行数有界。

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_feishu_live_e2e.GatewayProcessTest -v
```

Expected: FAIL，因为 `GatewayProcess` 尚不存在。

- [ ] **Step 3: Implement asyncio subprocess lifecycle**

使用 `asyncio.create_subprocess_exec(*argv, cwd=project_root, stdout=PIPE, stderr=PIPE, start_new_session=True)`；默认 argv：

```python
(
    sys.executable,
    "-m",
    "miniclaw",
    "--home",
    str(home),
    "gateway",
)
```

两个 background task 同时 drain stdout/stderr；只识别完整精确 ready 行，不做 substring；每行截断至 4096 字符、最多保留 200 条；diagnostics 只供本地错误分类，不写入 Evidence。停止时向进程组发 SIGTERM，等待 timeout，仍存活则再发一次 SIGTERM 并再等待；不调用 SIGKILL。

- [ ] **Step 4: Run GREEN**

```bash
.venv/bin/python -m unittest tests.test_feishu_live_e2e.GatewayProcessTest -v
```

- [ ] **Step 5: Run real CLI lifecycle regression**

```bash
.venv/bin/python -m unittest tests.test_gateway tests.test_cli -v
```

- [ ] **Step 6: Commit**

```bash
git add src/miniclaw/evals/feishu_live.py tests/test_feishu_live_e2e.py
git commit -m "feat(feishu): 管理 bounded Gateway live lifecycle"
```

---

### Task 4: Redacted Evidence Report and Secret Scan

**Files:**
- Modify: `src/miniclaw/evals/feishu_live.py`
- Modify: `tests/test_feishu_live_e2e.py`

**Interfaces:**
- Consumes: case ID、自动 evidence key/status、人工 evidence key/status、commit、匿名计数。
- Produces:

```python
@dataclass(frozen=True, slots=True)
class FeishuCaseResult:
    case_id: str
    status: str
    local_passed: tuple[str, ...]
    local_failed: tuple[str, ...]
    human_statuses: tuple[tuple[str, str], ...]
    error_code: str | None

def build_evidence_report(...) -> dict[str, object]: ...
def write_evidence(path: Path, report: Mapping[str, object]) -> None: ...
def scan_secret_matches(paths: Sequence[Path], secrets: Sequence[str]) -> int: ...
```

- [ ] **Step 1: Write RED tests for exact schema**

断言顶层只能包含：`schema_version`、`channel`、`commit`、`started_at`、`finished_at`、`gateway`、`checks`、`counts`、`release_status`。Check 只能包含 case ID、status、evidence key/status 和 stable error code；JSON 使用 `allow_nan=False`。

- [ ] **Step 2: Write RED tests for privacy**

把 `MODEL_SECRET_SENTINEL`、`CHANNEL_SECRET_SENTINEL`、`ou_private`、`oc_private`、消息正文、绝对 Home 路径放进输入对象或扫描文件，断言：

- Secret 文件命中会把 `FEISHU-LIVE-015` 强制 fail；
- serializer 拒绝额外字段与不允许的 evidence key；
- 报告序列化后不包含 sentinel、`ou_`、`oc_`、`om_` 或绝对用户名路径；
- symlink、超过 1 MiB 文件和第 1001 个文件被安全跳过；
- scan 只返回计数，不返回 secret 或文件路径。

- [ ] **Step 3: Run RED**

```bash
.venv/bin/python -m unittest tests.test_feishu_live_e2e.FeishuEvidenceReportTest -v
```

- [ ] **Step 4: Implement strict report builder**

使用封闭 status：`pass|fail|skip`；封闭 release status：`FEISHU_E2E_VERIFIED|FEISHU_LIVE_PARTIAL|FEISHU_LIVE_FAILED`。`write_evidence()` 以 `os.open(..., O_CREAT|O_EXCL, 0o600)` 创建，拒绝 symlink/已存在文件，UTF-8 写入后 `fsync`。

- [ ] **Step 5: Run GREEN and existing Channel harness tests**

```bash
.venv/bin/python -m unittest tests.test_feishu_live_e2e.FeishuEvidenceReportTest tests.test_channel_live_harness -v
```

- [ ] **Step 6: Commit**

```bash
git add src/miniclaw/evals/feishu_live.py tests/test_feishu_live_e2e.py
git commit -m "feat(feishu): 输出 redacted Live evidence report"
```

---

### Task 5: Interactive Feishu Live Orchestrator

**Files:**
- Modify: `src/miniclaw/evals/feishu_live.py`
- Modify: `scripts/feishu_live_smoke.py`
- Modify: `tests/test_feishu_live_e2e.py`
- Modify: `tests/test_feishu_evals.py`

**Interfaces:**
- Consumes: Task 1 cases、Task 2 probe、Task 3 Gateway、Task 4 report、现有 `run_local_checks()` 与 `validate_gateway_environment()`。
- Produces: `run_feishu_live_harness(argv: Sequence[str] | None = None) -> int`。

- [ ] **Step 1: Write RED tests for pre-confirmation zero side effects**

子进程调用脚本但不带 `--confirm-live`；传入不存在的 `--home`、`--output-dir`、`--root`，断言退出 2，三者均未创建，stdout/stderr 不含 config/secret/ID。Patch `_load_preflight`、`load_feishu_live_cases`、`GatewayProcess.start`，断言全部未调用。

- [ ] **Step 2: Write RED tests for preflight**

覆盖：

- Feishu disabled；
- 同时启用 Telegram/Discord，无法形成隔离 evidence；
- commit unknown；
- dirty worktree；
- Doctor FAIL；
- Gateway credentials/SDK preflight FAIL；
- 存在旧 pending Approval；
- scenario 数不是 15；
- 以上全部在 Gateway start 前退出 2 且不写 Evidence。

- [ ] **Step 3: Write RED tests for orchestration truth**

注入 fake `GatewayProcess`、fake `DatabaseProbe`、fake input/output 和固定 clock。覆盖：

- `FEISHU-LIVE-001` 由 Gateway ready 自动 pass，不要求人工；
- 自动证据失败时，即使人工输入 `p` 也必须 fail；
- 自动证据通过且全部 human evidence 为 `p` 才 pass；
- `s` 返回 1，不能写成 verified；
- 每个 case 先 capture checkpoint，再提示动作，再 evaluate；
- Gateway 总在 finally 中 stop；
- HEAD 在运行中变化会使最终 release fail；
- 15/15 + secret scan 0 才返回 0 和 `FEISHU_E2E_VERIFIED`。

- [ ] **Step 4: Run RED**

```bash
.venv/bin/python -m unittest tests.test_feishu_live_e2e.FeishuLiveHarnessTest -v
```

- [ ] **Step 5: Implement parser and preflight**

参数：

```text
--home PATH
--root PATH                 default ./evals/scenarios
--output-dir PATH           default ./.local/eval-results/feishu
--confirm-live
--gateway-timeout 30        bounded 5..120
--case-timeout 60           bounded 5..300
```

`--confirm-live` 检查必须发生在 `Path.resolve()`、`load_dotenv()`、`resolve_home()` 和 `mkdir()` 之前。确认后才加载 `.env`，要求只有 Feishu enabled，运行 Doctor 和全 Gateway preflight；只在内存收集 secrets。

- [ ] **Step 6: Implement per-case loop**

稳定输出只显示 case ID、title、合成 Query/action 和 evidence key，不显示正文/ID。对于 positive evidence，在用户按 Enter 表示动作完成后按 250ms 轮询，直到满足或 timeout；`no_new_turn` 使用完整 silence window。Human evidence 逐项读取 `p/f/s`；自动失败时直接记录 fail，不询问“强制通过”。

- [ ] **Step 7: Replace script with thin entry**

```python
#!/usr/bin/env python3
"""运行显式确认、可取证的 Feishu Live E2E。"""

from miniclaw.evals.feishu_live import run_feishu_live_harness

if __name__ == "__main__":
    raise SystemExit(run_feishu_live_harness())
```

- [ ] **Step 8: Run GREEN and all live harness tests**

```bash
.venv/bin/python -m unittest tests.test_feishu_live_e2e tests.test_feishu_evals tests.test_channel_live_harness -v
```

- [ ] **Step 9: Commit**

```bash
git add src/miniclaw/evals/feishu_live.py scripts/feishu_live_smoke.py tests/test_feishu_live_e2e.py tests/test_feishu_evals.py
git commit -m "feat(feishu): 编排真实 Bot Live E2E gate"
```

---

### Task 6: Engineering Documentation, Release Truth, and Progress

**Files:**
- Create: `docs/engineering/phase-5/20260808_feishu-live-e2e.md`
- Create: `docs/evals/releases/v0.5.1.md`
- Modify: `README.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/engineering/phase-4/20260808_testing-and-operations.md`
- Modify: `docs/engineering/phase-5/20260808_testing-and-live-acceptance.md`
- Modify: `docs/engineering/phase-5/20260808_troubleshooting.md`
- Modify: `docs/progress/index.html`
- Modify: `scripts/validate_docs.py`
- Modify external: `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html`

**Interfaces:**
- Consumes: 已实现命令、当前新鲜测试数字、真实 Live Evidence（若尚未运行则明确 pending）。
- Produces: 大白话操作手册、可点击进度页、严格发布状态。

- [ ] **Step 1: Write failing documentation validation tests**

在 `scripts/validate_docs.py` 将新工程文档与 v0.5.1 加入 `CURRENT_RELATIVE_DOCS`，并增加 Feishu 状态一致性：仓库中没有 `.local/eval-results`、凭据形状或 `FEISHU E2E VERIFIED` 与 Evidence 不一致。先运行并看到 missing docs RED。

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python scripts/validate_docs.py
```

Expected: FAIL，报告缺少 `phase-5/20260808_feishu-live-e2e.md` 和 `v0.5.1.md`。

- [ ] **Step 3: Write the operation manual**

文档必须覆盖：

1. 创建企业自建应用并启用机器人；
2. 最小 Scope ID：P2P read、group-at read、send-as-bot；
3. 长连接与 `im.message.receive_v1`；
4. 发布测试版本和可用范围；
5. `.env 0600`；
6. `lark-cli --profile miniclaw-e2e` 一次性 Owner discovery，先 schema、等 ready marker、有界退出；
7. 私聊 config → Doctor → E2E Runner；
8. 群聊 allowlist 第二阶段；
9. 15 case 对照表和 Evidence 解释；
10. 每类失败的稳定排障路径；
11. 绝不粘贴 Secret/完整 ID 的说明。

- [ ] **Step 4: Update release and progress truth**

实现代码通过但真实机器人尚未创建时，只能写：

```text
FEISHU E2E HARNESS PASS / REAL BOT PENDING
```

完成真实 15/15 且 Evidence 绑定最终 commit 后才能改成：

```text
FEISHU E2E VERIFIED
```

两份 HTML 同步相同 commit、测试数字和下一动作。External HTML 只链接 GitHub/仓库文件，不写本地 Secret/ID。

- [ ] **Step 5: Run GREEN documentation gates**

```bash
.venv/bin/python scripts/validate_docs.py --html /Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html
git diff --check
```

- [ ] **Step 6: Commit repository docs**

```bash
git add README.md docs scripts/validate_docs.py
git commit -m "docs(feishu): 补齐 Bot onboarding、Live gate 与进度"
```

External HTML 不进入 Git，但记录其校验结果。

---

### Task 7: Full Gates, Real Bot Provisioning, and Live Acceptance

**Files:**
- No source file required before live setup.
- Modify after evidence only: `docs/evals/releases/v0.5.1.md`、`docs/engineering/phase-5/20260808_testing-and-live-acceptance.md`、两份 progress HTML。
- Local secret only: project `.env` and `~/.miniclaw/config.toml`（禁止 `git add`）。
- Local ignored evidence: `.local/eval-results/feishu/*.json`（禁止 `git add`）。

**Interfaces:**
- Consumes: Tasks 1–6 clean commit、飞书开发者后台、Owner 飞书客户端、真实 App ID/App Secret。
- Produces: 真实 Bot、15/15 Evidence、`FEISHU E2E VERIFIED` 或准确的 partial/pending 状态。

- [ ] **Step 1: Run implementation gates before touching platform state**

```bash
.venv/bin/python -m unittest discover -s tests -v
corepack pnpm --dir tui test
.venv/bin/ruff check .
.venv/bin/python -m miniclaw eval validate --root evals/scenarios
.venv/bin/python -m miniclaw eval run --suite all --root evals/scenarios
.venv/bin/python -m miniclaw eval run --suite channel --repeat 20 --json --root evals/scenarios
.venv/bin/python scripts/validate_docs.py --html /Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html
uv lock --check
uv build
git diff --check
git status --short
```

Expected: 全部 PASS、工作区干净、offline 28 与 channel 32 不变、Python 总数上调、无 Secret。

- [ ] **Step 2: Create and publish the real Bot with the user**

在飞书开发者后台执行规格第 6 节：企业自建应用 `MiniClaw E2E Bot`、启用机器人、最小 Scope、长连接、
`im.message.receive_v1`、Owner-only 可用范围、发布测试版本。任何后台权限扩大或发布动作先把具体 Scope/范围展示给用户确认。

- [ ] **Step 3: Store credentials without exposing them**

让用户在本机安全输入 App Secret；只验证变量存在和 `.env` mode `0600`，终端/日志不得回显值。`git status` 与 secret scan 确认 `.env` 未被跟踪。

- [ ] **Step 4: Discover same-app Owner Open ID**

使用命名 profile `miniclaw-e2e`。在普通人类 TTY 中运行下列初始化命令；App ID 可作为参数，App Secret 只从
stdin 读取，不能使用 shell pipe、环境回显或 argv：

```bash
lark-cli config init --app-id cli_xxx --app-secret-stdin --brand feishu --name miniclaw-e2e
```

初始化完成后先查看 schema，再启动有界 consumer：

```bash
lark-cli --profile miniclaw-e2e event schema im.message.receive_v1 --json
lark-cli --profile miniclaw-e2e event consume im.message.receive_v1 --as bot --max-events 1 --timeout 2m
```

必须等 `[event] ready` 后让用户发送一次性 challenge。只把 `sender_id` 写入本地 `owner_open_id` / `allowed_open_ids`；不得把 event JSON、正文或 ID 写进仓库和 Evidence。

- [ ] **Step 5: Run Doctor and P0 private-chat gate**

```bash
.venv/bin/miniclaw doctor
.venv/bin/python scripts/feishu_live_smoke.py --confirm-live
```

先执行 `LIVE-001/002/004/005/006/007` 相关动作；任一失败立即保留脱敏证据并停止扩大到群聊。

- [ ] **Step 6: Enable only the dedicated test group**

Bot 加入 `MiniClaw E2E` 测试群，获取同应用 Chat ID，配置唯一 `allowed_chat_ids` 并开启 `allow_group_mentions`。确认配置 diff 只存在本地 state，不进入 Git。

- [ ] **Step 7: Complete 15/15 and record evidence**

按 Runner 提示完成全部案例。退出码必须为 0；Evidence summary 必须 15 pass、0 fail、0 skip、0 secret match，commit 等于当前 HEAD。

- [ ] **Step 8: Update verified truth only after real evidence**

用 Evidence 中的匿名计数和 commit 更新 v0.5.1、工程文档与两份进度 HTML；不复制任何外部 ID、正文或 Secret。若无法完成真实平台，保持 `REAL BOT PENDING`/`LIVE PARTIAL`，列出具体失败 case。

- [ ] **Step 9: Final verification and commit**

```bash
.venv/bin/python scripts/validate_docs.py --html /Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html
git diff --check
git status --short
git add docs README.md scripts/validate_docs.py
git commit -m "docs(release): 记录 Feishu E2E live evidence 与 release truth"
```

- [ ] **Step 10: Finish branch**

使用 `superpowers:verification-before-completion` 新鲜运行完整门禁，再使用 `superpowers:finishing-a-development-branch` 把分支合并并非强制推送到 `origin/main`；推送后 fetch 验证远端 SHA。不得重写共享历史。

---

## Plan Self-Review

- Spec coverage：机器人创建、最小 Scope、长连接、同应用 Owner discovery、15 条场景、Runner、SQLite 取证、人工证据、Secret scan、文档、真实验收与发布状态均有对应 Task。
- Scope：不包含日历/任务/文档/云盘，也不替换 Telegram/Discord harness。
- Type consistency：Task 1 的 evidence tuple 被 Task 2/5 消费；Task 2 的 `DatabaseCheckpoint/EvidenceEvaluation`、Task 3 的 `GatewayProcess`、Task 4 的 `FeishuCaseResult` 均在 Task 5 明确装配。
- Safety：未确认零读取/零写入；真实平台与凭据只在 Task 7；人工不能覆盖自动失败；不使用 SIGKILL。
- 占位符：未保留待定标记或跨任务省略语；所有代码任务都给出目标接口、RED、GREEN、命令与预期结果。
