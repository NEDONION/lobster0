# Phase 6 PROD-B Feishu Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在同一 clean commit、同一个真实飞书 Bot 与 DeepSeek Provider 上，完成严格 Channel 15/15、Phase 6 Automation 10/10，并生成不含正文、外部 ID、路径和 Secret 的 Owner-only Evidence。

**Architecture:** 不复制现有 Channel harness。`feishu_live.py` 继续负责严格 15-case；新增的 Automation live runner 复用 `ManagedGateway`、SQLite repositories、真实 Scheduler/Runner/Delivery 事实，并用独立 versioned JSONL 固定 10 个生产场景。人工只做飞书中可观察的发送、审批和可见性确认；case 结论由 SQLite durable truth 与有限人工确认共同决定。

**Tech Stack:** Python 3.12、stdlib `argparse/asyncio/dataclasses/json/sqlite3`、现有 Feishu SDK、DeepSeek OpenAI-compatible Provider、SQLite、`unittest`、Ruff。

## Global Constraints

- 权威设计是 `docs/superpowers/specs/2026-08-10-phase-6-feishu-production-acceptance-design.md`。
- 现有 `evals/scenarios/feishu-live.v1.jsonl` 的 15 条 ID、schema 和判定不得弱化。
- 新增数据集固定 `FEISHU-AUTO-001..010`；不能用本地 fake Automation 15-case 冒充 Live PASS。
- 两套 Live gate 必须绑定同一个 clean commit、单一飞书 channel、safe permission mode 和零 pending Approval 起点。
- runner 不读取或输出 `.env` 值；日志、Evidence 和异常不包含消息正文、chat/open/user ID、Home、Workspace 绝对路径或 Secret。
- Evidence 目录必须 owner-only、拒绝 symlink，文件使用 0600、exclusive create 与 fsync。
- 真实飞书发送必须经过现有 durable Delivery/Outbox，不允许测试脚本直接调用 SDK 绕过产品路径。
- Automation dangerous Tool 必须走现有参数绑定 Approval continuation；测试脚本不能代替 Owner 自动批准。
- 每个 case 使用唯一内部 run slot 与 idempotency key；未知投递状态只允许恢复同一 key，不能重新创建可见回复。
- DeepSeek 的正常、Tool、Approval 三条 live 请求必须各有稳定终态；Provider 原始 payload 不能进入 Evidence。
- 单元测试离线、快速、可重复；网络与飞书动作用 fake boundary，真实 PASS 只能由 `--confirm-live` 产生。
- 提交标题中英混合；不暂存 `.env`、`.local/`、Evidence、数据库、日志或平台截图。

---

### Task B1: Versioned Feishu Automation live cases

**Files:**
- Create: `evals/scenarios/feishu-automation-live.v1.jsonl`
- Modify: `src/lobster0/evals/cases.py`
- Modify: `tests/test_eval_cases.py`

**Interfaces:**
- Consumes: `EvalCase`、现有 strict JSONL parser 与 Automation expectation fields。
- Produces: `load_feishu_automation_live_cases(root: Path) -> tuple[EvalCase, ...]`。

- [ ] **Step 1: 写 closed schema 与 exact-ID RED 测试**

测试必须断言：

```python
cases = load_feishu_automation_live_cases(SCENARIO_ROOT)
self.assertEqual(tuple(case.id for case in cases), tuple(
    f"FEISHU-AUTO-{index:03d}" for index in range(1, 11)
))
self.assertTrue(all(case.capability == "feishu_automation_e2e" for case in cases))
```

再用临时 JSONL 覆盖：缺号、重复 ID、未知 fixture、空 evidence、Secret 字段、绝对路径、`offline.responses`、未知 key 和非 active case，全部 fail closed。

- [ ] **Step 2: 运行 RED**

Run: `uv run python -m unittest tests.test_eval_cases.EvalCaseLoadingTest -v`

Expected: import/attribute failure because loader and dataset do not exist；已有 suite 保持 green。

- [ ] **Step 3: 增加最小 loader 与固定 10 条数据**

loader 只过滤 `status == "active"` 和 `capability == "feishu_automation_e2e"`，然后严格比较 exact ID tuple；通用 parser 继续负责 credentials/path/script safety。只向既有 `_AUTOMATION_FIXTURES` 增加十个 live fixture 名，不创建第二套 case model。

固定用例：

| ID | fixture | 公开结果 | 禁止结果 |
| --- | --- | --- | --- |
| 001 | `live_one_shot_delivery` | 单次 Task/Run/Delivery 成功 | 同 slot 重复 |
| 002 | `live_interval_two_slots` | 两个不同 slot 各成功一次 | 漏 slot/重复 slot |
| 003 | `live_gateway_restart` | 重启后原 Task 延续 | 重建 Task/重发历史 |
| 004 | `live_interrupted_recovery` | interrupted/stable terminal，lease 释放 | 永久 running |
| 005 | `live_waiting_approval` | dangerous Tool 等待 Approval | 未批准副作用 |
| 006 | `live_approval_continuation` | Owner 批准后 child turn 与唯一 Delivery | 原 Turn 重放 |
| 007 | `live_structured_silence` | 成功但零 Delivery | 空白/占位消息 |
| 008 | `live_durable_estop` | halt 后零 claim/enqueue | 模型解除 E-stop |
| 009 | `live_budget_stop` | budget 在下一副作用前终止 | 超预算 Tool/Delivery |
| 010 | `live_delivery_unknown_recovery` | 同 idempotency key 恢复，唯一可见回复 | duplicate Delivery |

- [ ] **Step 4: 运行 GREEN**

Run: `uv run python -m unittest tests.test_eval_cases -v`

Expected: 所有通用、Feishu、Automation、Browser loader 测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add evals/scenarios/feishu-automation-live.v1.jsonl src/lobster0/evals/cases.py tests/test_eval_cases.py
git commit -m "test(feishu): 固定 Phase 6 Automation live cases"
```

---

### Task B2: Redacted production Evidence contract

**Files:**
- Create: `src/lobster0/evals/production_evidence.py`
- Create: `tests/test_production_evidence.py`
- Modify: `src/lobster0/evals/feishu_live.py`
- Modify: `tests/test_feishu_live_e2e.py`

**Interfaces:**
- Consumes: existing Feishu evidence report/write/secret-scan behavior。
- Produces: shared `write_private_json()`、`validate_commit()`、`utc_timestamp()` 与 bounded `scan_secret_matches()`；Feishu report bytes stay schema-compatible。

- [ ] **Step 1: 写权限、redaction 与 compatibility RED 测试**

覆盖：owner-only directory、0600 file、O_EXCL、no-follow、fsync failure cleanup、NaN/unknown type rejection、exact Secret match count、bounded file count/bytes、symlink/large/non-regular skip，以及 payload 中 path/platform IDs/message content key 被拒绝。

保存现有 Feishu `build_evidence_report()` 的 canonical fixture，重构前后 JSON object 与 release status 不变。

- [ ] **Step 2: 运行 RED**

Run: `uv run python -m unittest tests.test_production_evidence -v`

Expected: missing module；现有 Feishu evidence tests remain green。

- [ ] **Step 3: 提取最小共享 primitive**

只提取 B/C 都会使用的文件安全、timestamp、commit validator 和 secret scan。业务 schema、case 计数与 release status 仍留在各自 runner，避免通用 report framework。`feishu_live.py` 调用新 primitive，但其公开函数名和错误码保持兼容。

- [ ] **Step 4: 运行 GREEN 与 byte-level 回归**

Run: `uv run python -m unittest tests.test_production_evidence tests.test_feishu_live_e2e -v`

Expected: permissions/redaction PASS；15-case Evidence schema、status 与 existing CLI behavior 不变化。

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/evals/production_evidence.py src/lobster0/evals/feishu_live.py tests/test_production_evidence.py tests/test_feishu_live_e2e.py
git commit -m "refactor(eval): 复用 private production evidence boundary"
```

---

### Task B3: Feishu Automation live evaluator

**Files:**
- Create: `src/lobster0/evals/feishu_automation_live.py`
- Create: `tests/test_feishu_automation_live.py`

**Interfaces:**
- Consumes: `DatabaseCheckpoint` pattern、Automation repositories、DeliveryRepository、versioned live cases。
- Produces: `AutomationLiveCheckpoint`、`AutomationLiveCaseResult`、`evaluate_automation_case()`、`build_automation_evidence_report()`。

- [ ] **Step 1: 写 durable-fact RED tests**

每个 fixture 用临时真实 SQLite 写入最小合法 Task/Run/Approval/Delivery/Audit facts，再断言 evaluator 只读数据库即可给出：

- expected Task identity、slot 与 Run terminal；
- Delivery 数量、status、idempotency relation；
- waiting Approval 未发生 Tool side effect；
- continuation 绑定 parent/child/approval；
- E-stop 后无新 claim；
- interrupted lease 已释放；
- budget terminal 前没有额外 ToolRun；
- unknown delivery recovery 沿用同 key。

再覆盖缺表事实、重复 Delivery、错误 target、pending leak、stale running、unknown status、case/checkpoint mismatch 与 clock rollback，全部返回稳定失败码，不输出 row data。

- [ ] **Step 2: 运行 RED**

Run: `uv run python -m unittest tests.test_feishu_automation_live -v`

Expected: missing evaluator module。

- [ ] **Step 3: 实现只读 evaluator 与 closed report**

只复用现有 repositories 或 parameterized SELECT；不复制 Scheduler/Runner 逻辑。checkpoint 在人工动作前保存相关事实表最大内部 ID 与已存在 pending IDs，case 只评价 checkpoint 之后的 rows。

report 顶层固定：

```json
{
  "schema_version": 1,
  "suite": "feishu-automation",
  "commit": "<40-hex>",
  "started_at": "<UTC>",
  "finished_at": "<UTC>",
  "checks": [],
  "counts": {},
  "secret_matches": 0,
  "release_status": "FEISHU_AUTOMATION_VERIFIED"
}
```

`checks` 只能含 case ID、pass/fail/skip、有限 evidence key、稳定 error code 和人工 status；禁止原始文本、时间以外的外部标识、path、prompt、tool arguments 与 Provider payload。exact 10/10、零 skip/fail/secret 才 VERIFIED。

- [ ] **Step 4: 运行 GREEN**

Run: `uv run python -m unittest tests.test_feishu_automation_live -v`

Expected: 10 条 positive fixture 和所有 corruption fixture PASS。

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/evals/feishu_automation_live.py tests/test_feishu_automation_live.py
git commit -m "feat(eval): 增加 Feishu Automation durable evaluator"
```

---

### Task B4: Confirmed live harness and DeepSeek paths

**Files:**
- Modify: `src/lobster0/evals/feishu_automation_live.py`
- Create: `scripts/feishu_automation_live.py`
- Modify: `tests/test_feishu_automation_live.py`
- Modify: `tests/test_channel_live_harness.py`

**Interfaces:**
- Consumes: `ManagedGateway`、AppConfig/Gateway preflight、10-case evaluator、existing stdin prompt pattern。
- Produces: `run_feishu_automation_live_harness(argv) -> int` 和 thin script entry point。

- [ ] **Step 1: 写 zero-side-effect preflight RED tests**

不带 `--confirm-live` 必须在读取 config/secret/state、建立网络、创建目录前返回 2。确认后严格拒绝：dirty/detached commit、非单 Feishu channel、unsafe permission mode、Automation disabled、非 Seatbelt backend、pending Approval、active duplicate Gateway、数据库 schema 不健康、错误 provider 或 Evidence 路径不安全。

- [ ] **Step 2: 写 managed lifecycle 与 interruption RED tests**

用 fake ManagedGateway/TTY boundary 覆盖：exact ready、unexpected exit、SIGINT、resume checkpoint、人工 pass/fail/skip、审批超时、重复 resume、case failure 后继续收集、最终 graceful shutdown。只有确认 flag 之后才允许启动子进程。

- [ ] **Step 3: 实现最小 orchestration loop**

runner 依次展示 case ID、标题和不含 Secret/平台 ID 的操作说明；人工输入只接受固定 token。每条 case 前 checkpoint，动作后轮询 bounded SQLite evidence，再记录人工可见性。003 通过受管 Gateway stop/start，不触碰 launchd；005/006 要求 Owner 在飞书卡片中审批。010 注入受控 transport unknown boundary 时必须经产品 Delivery recovery，不直接写成功状态。

DeepSeek live minimum：

1. normal response：001；
2. read-only Tool call：002 或 003 中的状态读取；
3. dangerous Tool + Approval：005/006。

三类必须记录 stable terminal/error code 和 Provider request existence bit，不记录 request/response content 或 ID。

- [ ] **Step 4: 运行离线 GREEN**

Run: `uv run python -m unittest tests.test_feishu_automation_live tests.test_channel_live_harness -v`

Expected: no-network harness contract PASS；中断可恢复且 Evidence 不泄露。

- [ ] **Step 5: Commit**

```bash
git add src/lobster0/evals/feishu_automation_live.py scripts/feishu_automation_live.py tests/test_feishu_automation_live.py tests/test_channel_live_harness.py
git commit -m "feat(feishu): 打通 confirmed Automation live harness"
```

---

### Task B5: Execute both real Feishu gates

**Files:**
- Runtime-only: ignored Evidence under `~/.lobster0/evidence/phase6-production/<run-id>/`
- Modify after PASS: `docs/evals/releases/20260810_phase6-macos-feishu-production.md`

- [ ] **Step 1: 跑 deterministic pre-gates**

```bash
uv run python -m unittest tests.test_eval_cases tests.test_feishu_live_e2e tests.test_feishu_automation_live -v
uv run lobster0 eval run --suite channel --repeat 20 --json --root evals/scenarios
uv run lobster0 eval run --suite automation --repeat 20 --json --root evals/scenarios
uv run ruff check .
git diff --check
```

- [ ] **Step 2: 在同一 clean commit 跑严格 15-case**

```bash
uv run python scripts/feishu_live_smoke.py \
  --confirm-live \
  --output-dir ~/.lobster0/evidence/phase6-production/<run-id>/feishu-channel
```

Expected: `FEISHU_E2E_VERIFIED`, 15/15 PASS, zero Secret matches；任何 skip 都不通过生产 Gate。

- [ ] **Step 3: 在同一 commit 跑 Automation 10-case**

```bash
uv run python scripts/feishu_automation_live.py \
  --confirm-live \
  --output-dir ~/.lobster0/evidence/phase6-production/<run-id>/feishu-automation
```

Expected: `FEISHU_AUTOMATION_VERIFIED`, 10/10 PASS, zero duplicates/pending leaks/Secret matches。

- [ ] **Step 4: 写 tracked redacted summary**

release record 只记录 commit、UTC 区间、suite totals、release status、evidence SHA-256 和环境类别 `macos+feishu`；不提交 Evidence 文件、绝对路径、PID、平台 ID、正文或 Secret。

- [ ] **Step 5: Commit**

```bash
git add docs/evals/releases/20260810_phase6-macos-feishu-production.md
git commit -m "docs(eval): 记录 Feishu 25-case production evidence"
```

## PROD-B Done Definition

- [ ] Strict Channel Live 15/15 PASS on one clean commit。
- [ ] Phase 6 Automation Live 10/10 PASS on the same commit/Bot/Provider。
- [ ] DeepSeek normal/Tool/Approval paths have stable terminal evidence。
- [ ] No duplicate Feishu reply, stuck run/lease, unbound Approval or leaked Secret。
- [ ] Private Evidence is owner-only and tracked release record is redacted。
- [ ] 本 Gate 只写 Feishu VERIFIED；Telegram、Discord 继续 PENDING。
