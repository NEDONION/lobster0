# Phase 6 PROD-C Recovery, Soak and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用受管 LaunchAgent 完成可恢复的 24 小时 macOS + 飞书生产 soak，聚合 PROD-A/PROD-B Evidence，准确更新发布状态，并产出 Phase 7 Controlled Evolution 的完整工程落地文档。

**Architecture:** 一个只读 monitor 按固定 cadence 读取 launchd/lease/SQLite/文件权限事实，持久化最小 checkpoint，因此 Codex 或终端退出不会中断验收。一个薄 orchestration CLI 顺序校验 A/B Evidence、启动/恢复 soak、生成 release summary；它不接管 Gateway、不复制 Scheduler，也不伪造飞书动作。Phase 7 文档描述下一阶段的受控提案流水线，但本轮不提前创建 Evolution 代码或数据库表。

**Tech Stack:** Python 3.12、stdlib `argparse/dataclasses/datetime/hashlib/json/os/plistlib/sqlite3/subprocess`、macOS `launchctl`、SQLite、现有 Gateway lease/Automation/Delivery repositories、`unittest`、Ruff。

## Global Constraints

- 权威设计是 `docs/superpowers/specs/2026-08-10-phase-6-feishu-production-acceptance-design.md`。
- 24 小时按 UTC 单调经过时间计算，不能缩短、倍速、补写或用 20 轮 local suite 代替。
- 验收期间 Mac 必须保持登录、唤醒且联网；系统睡眠、时钟异常、持续断网或 monitor gap 超界会令本次 soak 失败，重新开始完整 24 小时。
- Gateway 必须由受管 LaunchAgent 启动；monitor 只观察和触发已定义的恢复 case，不 fork 第二个 Gateway。
- process restart 是强制验收；整机 reboot 仅由 Owner 手工选择，未执行时 Evidence 必须明确 `os_reboot=not_run`，不得写 PASS。
- monitor 不读取消息正文、Tool arguments、Provider payload、Secret 或个人 Memory；SQLite 查询只返回 count/status/age/内部一致性。
- 所有 Evidence 保存在 owner-only 私有目录；Git 只记录 schema-validated、脱敏 aggregate 与 hash。
- `--progress-output` 是可选外部路径，只输出 elapsed、heartbeat、case/gate status；路径不写入 config、SQLite、Evidence 或 Git。
- 信号、中断、终端关闭和 Codex task 结束不得把 partial soak 标为 PASS；checkpoint 必须可恢复。
- Phase 7 文档必须清楚区分“工程方案/尚未实现”，禁止把规划写成当前功能。
- 不实现 systemd、Docker/VPS、Telegram/Discord production soak、Prometheus 或 Web 管理后台。
- 每个非平凡行为先 RED 后 GREEN；提交标题中英混合，不提交运行数据库、日志、Evidence 或外部 progress 文件。

---

### Task C1: Read-only production invariant snapshot

**Files:**
- Create: `src/lobster0/evals/phase6_soak.py`
- Create: `tests/test_phase6_soak.py`

**Interfaces:**
- Consumes: LaunchAgent status reader from PROD-A、`GatewayProvenance`/lease、Database repositories and owner-only filesystem facts。
- Produces: `SoakSnapshot`、`SoakViolation`、`collect_soak_snapshot()`、`evaluate_snapshot()`。

- [ ] **Step 1: 写 healthy snapshot RED test**

用注入的 launchd facts、fixed UTC/monotonic clocks 和临时 v5 SQLite，断言 snapshot 只包含：

```python
SoakSnapshot(
    observed_at="2026-08-10T00:00:00.000000Z",
    service_loaded=True,
    service_running=True,
    gateway_lease_fresh=True,
    database_healthy=True,
    running_turns=0,
    stale_task_runs=0,
    pending_deliveries=0,
    failed_deliveries=0,
    pending_approvals=0,
    secret_matches=0,
    owner_only_state=True,
)
```

类型中不能出现 PID、absolute path、platform ID、message content、prompt 或 secret value。

- [ ] **Step 2: 写 violation RED matrix**

分别构造：service unloaded/not running、lease stale/mismatch、SQLite integrity failure、stuck running Turn、expired TaskRun lease、Delivery backlog/failed terminal、orphan Approval、world-readable state/evidence、secret scan hit、clock rollback、query error。每项产生固定 code，多个 violation 稳定排序且不含底层异常文本。

- [ ] **Step 3: 运行 RED**

Run: `uv run python -m unittest tests.test_phase6_soak -v`

Expected: missing module。

- [ ] **Step 4: 实现最小只读 collector**

collector 接收 explicit dependencies，不读取 `.env`。SQL 使用现有 schema 的 count/max-age 查询和 `PRAGMA quick_check`；launchd 通过 PROD-A `service.status()` 的封闭结果读取；路径只检查 mode/owner/symlink，不返回路径。所有 OS/SQLite 错误转换为固定 violation code。

默认阈值必须是代码常量并在 CLI help 中可见：

- sample cadence 60 秒；
- monitor gap 最大 180 秒；
- Gateway lease freshness 使用现有 lease contract，不另定第二套；
- pending Delivery 允许短暂存在，但超过现有 retry deadline 即 violation；
- pending Approval 本身允许，超过 TTL 才 orphan violation；
-任何 Secret match 立即 hard fail。

- [ ] **Step 5: 运行 GREEN**

Run: `uv run python -m unittest tests.test_phase6_soak -v`

Expected: healthy/violation/error matrix PASS；测试不调用真实 launchctl/network/Home。

- [ ] **Step 6: Commit**

```bash
git add src/lobster0/evals/phase6_soak.py tests/test_phase6_soak.py
git commit -m "feat(eval): 增加 Phase 6 production invariant monitor"
```

---

### Task C2: Durable exact-duration soak session

**Files:**
- Modify: `src/lobster0/evals/phase6_soak.py`
- Modify: `tests/test_phase6_soak.py`

**Interfaces:**
- Produces: `SoakSession`、`SoakCheckpoint`、`start_soak()`、`resume_soak()`、`record_snapshot()`、`finish_soak()`。

- [ ] **Step 1: 写 checkpoint/state-machine RED tests**

状态固定为 `running | failed | passed`。测试必须覆盖：

- start 使用 commit、run ID、UTC 与 monotonic origin；
- exclusive owner-only checkpoint create；
-每次 sample atomic replace + file fsync + parent fsync；
-同 commit/run 可以 resume；不同 commit、duration、state home、schema 拒绝；
-重复 sample idempotent，不重复计数；
-gap > 180 秒、clock rollback、sleep jump 或 invariant violation 立即 failed；
-23:59:59 永不 PASS，24:00:00 且全程 healthy 才 PASS；
-failed session 不能 resume 成 passed；passed session 重跑只读返回 passed；
-SIGINT/normal process exit 仅保留 running，不误报失败。

- [ ] **Step 2: 运行 RED**

Run: `uv run python -m unittest tests.test_phase6_soak.Phase6SoakSessionTest -v`

Expected: session APIs missing。

- [ ] **Step 3: 实现 JSON checkpoint，无新数据库表**

Ponytail decision：这是单机单 operator 的发布验收状态，不属于产品 durable truth；使用 owner-only canonical JSON checkpoint，而不是新增 migration/repository。checkpoint 只含：schema、commit、run token hash、started/last/required UTC、elapsed seconds、sample counts、restart case status、aggregate violation codes 和 terminal status。

使用 `time.monotonic()` 计算单进程增量，同时用 UTC 差/last sample 检测跨进程 gap；休眠造成 cadence gap 即 hard fail。checkpoint 本身不包含绝对路径或 PID。

- [ ] **Step 4: 增加 bounded progress output**

`render_progress()` 只输出：

```text
status=running elapsed=04:12:00 required=24:00:00 samples=253 violations=0
```

可选 progress file 使用 atomic owner-only write；不写 commit、paths、IDs、content 或 Secret。写失败只返回 `progress_write_failed`，不能改变 Gateway 状态。

- [ ] **Step 5: 运行 GREEN**

Run: `uv run python -m unittest tests.test_phase6_soak -v`

Expected: exact-duration、resume、permissions、gap/clock/invariant matrix PASS。

- [ ] **Step 6: Commit**

```bash
git add src/lobster0/evals/phase6_soak.py tests/test_phase6_soak.py
git commit -m "feat(eval): 持久化 exact 24-hour soak checkpoint"
```

---

### Task C3: Phase 6 production gate orchestrator

**Files:**
- Create: `src/lobster0/evals/phase6_production.py`
- Create: `scripts/phase6_production_gate.py`
- Create: `tests/test_phase6_production.py`

**Interfaces:**
- Consumes: PROD-A Seatbelt/service status、PROD-B Channel/Automation Evidence、soak session。
- Produces: `preflight | start | resume | status | finalize` CLI and closed aggregate report。

- [ ] **Step 1: 写 no-side-effect parser/preflight RED tests**

不带 subcommand/confirm 必须不读 Secret、不启动 service、不创建 files。`preflight` 只读并检查：darwin、managed CPython 3.12、clean exact commit、LaunchAgent ownership、Seatbelt Evidence PASS、Feishu 15/15 Evidence PASS、Automation 10/10 Evidence PASS、same commit、owner-only state/evidence、Gateway health 与 zero secret matches。

任一 Evidence missing/invalid/symlink/world-readable/hash mismatch/other commit/partial/skip 都返回固定 error code。

- [ ] **Step 2: 写 recovery case RED tests**

强制 process recovery case：

1. 记录健康 snapshot；
2. 使用 PROD-A service API `restart`，不 kill arbitrary PID；
3. 观察 LaunchAgent state transition 与新的 fresh lease；
4. 保证旧 Gateway lease/provenance 不继续 heartbeat；
5. 发送一个唯一飞书 recovery probe 并观察 exactly-one Delivery；
6.记录 `service_restart=pass`。

测试覆盖 restart argv failure、service not owned、ready timeout、same stale lease、duplicate visible Delivery、shutdown during active approval 和 successful recovery。失败不得继续 soak。

- [ ] **Step 3: 写 aggregate report RED tests**

只有以下全部成立才生成 `PHASE6_MACOS_FEISHU_PRODUCTION_VERIFIED`：

- Seatbelt containment PASS；
- LaunchAgent install/start/status/restart PASS；
- Feishu strict 15/15；
- Feishu Automation 10/10；
- DeepSeek normal/Tool/Approval PASS；
- process recovery PASS；
- soak elapsed >= 86400、terminal passed、zero violations；
- all evidence same clean commit、zero Secret matches。

report 只含 schema、commit、UTC range、environment labels、gate totals/status、hashes、`os_reboot=pass|not_run|fail` 和 final status。`os_reboot=not_run` 不阻塞 Mac+Feishu Gate，但不能被显示为 PASS。

- [ ] **Step 4: 运行 RED**

Run: `uv run python -m unittest tests.test_phase6_production -v`

Expected: missing module/script behavior。

- [ ] **Step 5: 实现薄 orchestration CLI**

CLI 示例：

```bash
uv run python scripts/phase6_production_gate.py preflight --confirm-live --evidence-dir <private-dir>
uv run python scripts/phase6_production_gate.py start --confirm-live --evidence-dir <private-dir> --progress-output <external-file>
uv run python scripts/phase6_production_gate.py resume --confirm-live --evidence-dir <private-dir> --progress-output <external-file>
uv run python scripts/phase6_production_gate.py status --evidence-dir <private-dir>
uv run python scripts/phase6_production_gate.py finalize --confirm-live --evidence-dir <private-dir>
```

orchestrator 只串联现有 APIs；不复制 evidence validators、launchctl argv、SQLite schema 或 Feishu SDK。start/restart 这类写操作要求 `--confirm-live`；status 只读且不加载 Secret。

- [ ] **Step 6: 运行 GREEN**

Run: `uv run python -m unittest tests.test_phase6_production tests.test_phase6_soak -v`

Expected: orchestration/recovery/report/side-effect boundary PASS。

- [ ] **Step 7: Commit**

```bash
git add src/lobster0/evals/phase6_production.py scripts/phase6_production_gate.py tests/test_phase6_production.py
git commit -m "feat(release): 编排 macOS Feishu production gate"
```

---

### Task C4: Operations docs and progress truth

**Files:**
- Create: `docs/engineering/phase-6/20260810_macos-feishu-production-acceptance.md`
- Modify: `docs/engineering/phase-6/20260809_autonomy-runtime.md`
- Modify: `docs/engineering/phase-6/20260809_sandbox-and-checkpoint.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/product/20260807_产品需求文档.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `README.md`
- Modify: `docs/progress/index.html`
- Modify: `scripts/validate_docs.py`
- Modify: `tests/test_documentation.py`

- [ ] **Step 1: 写 documentation truth RED tests**

断言 status vocabulary 一致：实施完成但 live 未结束时只能是 `IMPLEMENTATION PASS / PRODUCTION SOAK RUNNING|PENDING`；只有 aggregate verified report 才允许 `MACOS+FEISHU PRODUCTION VERIFIED`。Telegram、Discord、Docker/VPS 和 Browser live 不得随本 Gate 变成 PASS。

- [ ] **Step 2: 写大白话运维文档**

文档至少包含：安装 LaunchAgent、启动/停止/状态、managed Python、Seatbelt probe、飞书 25-case 操作、恢复 case、如何保持 Mac 唤醒、24h start/resume/status/finalize、Evidence 位置/权限、失败重跑、Secret 泄漏处置、卸载服务、已知边界、逐项验收 checklist 和 Mermaid 数据流/故障恢复图。

- [ ] **Step 3: 同步进度页与产品/架构事实**

在真实 24h PASS 前显示 elapsed/status，而不是预写 PASS。进度页不引用本机 `file://` Evidence；只链接 tracked docs/release record。所有图的 Mermaid label 兼容现有 validator。

- [ ] **Step 4: 运行 GREEN**

```bash
uv run python -m unittest tests.test_documentation -v
uv run python scripts/validate_docs.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add README.md docs scripts/validate_docs.py tests/test_documentation.py
git commit -m "docs(phase6): 增加 Mac Feishu production runbook"
```

---

### Task C5: Write the Phase 7 engineering landing document

**Files:**
- Create: `docs/engineering/phase-7/20260810_controlled-evolution.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/progress/index.html`
- Modify: `tests/test_documentation.py`

**Document status:** `ENGINEERING PLAN / NOT IMPLEMENTED`。

- [ ] **Step 1: 对齐现有事实，不提前实现**

盘点当前 Feedback tables、eval runner、Memory/Skill versioning、Approval、Audit、Rollback 是否存在；文档逐项标成 `REUSE / EXTEND / NEW`，不存在的 API 不得写成现状。

- [ ] **Step 2: 写完整工程文档**

文档必须用大白话和图解说明：

1. 用户场景：飞书/CLI `/good`、`/bad <原因>`；
2. 数据流：Feedback → redacted Case → Proposal → baseline+failure Eval → Owner review → Apply → Rollback；
3. proposal 范围：Prompt、Skill、Memory candidate；
4. hard deny：Python Core、Config、Policy、tests、release pipeline、自动部署；
5. SQLite schema/migrations：feedback、proposals、proposal_versions、eval_runs、eval_case_results、active_revision；
6. CLI：`feedback list/show`、`evolve propose/show/eval/apply/rollback`；
7. IM：命令解析、确认卡、proposal summary，不在 IM 展示完整敏感 context；
8. proposal lifecycle：draft/evaluating/rejected/approved/applied/rolled_back/failed；
9. eval gate：全量 versioned suites、失败案例、安全零回归、成本/延迟预算、deterministic vs live；
10. Owner approval 与 hash binding；
11. atomic apply、active pointer、crash windows、rollback；
12. redaction、retention、forget interaction、audit；
13. module/file map、接口、测试金字塔、versioned cases、逐 Task 实施顺序；
14. MVP/非目标、风险、DoD、验收命令。

至少包含五张 Mermaid 图：总体架构、feedback sequence、proposal state machine、apply/rollback crash recovery、数据模型；并给出示例但不包含真实个人数据。

- [ ] **Step 3: 计划边界采用最小安全方案**

首版不让模型生成任意 patch。Prompt proposal 只允许修改 versioned Markdown block；Skill proposal 只允许在 staging 中生成 Markdown/Python Skill 候选并经过现有 Skill validator/Policy；Memory proposal 只生成 review candidate。任何 apply 都由 Core 验 hash、Owner approval 与 eval receipt 后原子切换版本。Agent 无 approve/apply 权限。

- [ ] **Step 4: 校验状态与图**

```bash
uv run python -m unittest tests.test_documentation -v
uv run python scripts/validate_docs.py
git diff --check
```

Expected: links/Mermaid/HTML/status PASS；文档和进度页明确 Phase 7 未实现。

- [ ] **Step 5: Commit**

```bash
git add docs/engineering/phase-7/20260810_controlled-evolution.md docs/engineering/README.md docs/progress/index.html tests/test_documentation.py
git commit -m "docs(phase7): 规划评测驱动的 Controlled Evolution"
```

---

### Task C6: Execute the real 24-hour gate and publish exact status

**Files:**
- Runtime-only: private Evidence/checkpoint/progress files
- Modify after PASS: `docs/evals/releases/20260810_phase6-macos-feishu-production.md`
- Modify after PASS: product/architecture/engineering/progress status files from C4

- [ ] **Step 1: Freeze release candidate and run full deterministic gate**

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run lobster0 eval run --suite channel --repeat 20 --json --root evals/scenarios
uv run lobster0 eval run --suite automation --repeat 20 --json --root evals/scenarios
uv run lobster0 eval run --suite browser --repeat 20 --json --root evals/scenarios
pnpm --dir browser-worker test
uv run python scripts/validate_docs.py
git diff --check
```

- [ ] **Step 2: Complete PROD-A and PROD-B Live Evidence**

Seatbelt Python/Node-chain、LaunchAgent lifecycle、Feishu strict 15 与 Automation 10 必须都绑定此 clean commit。任一失败先修复、回归、形成新 clean commit，再把所有 Live Gate 重新绑定到新 commit。

- [ ] **Step 3: Run mandatory service recovery**

使用 production gate CLI 执行受管 restart 和 exactly-one Feishu recovery probe。若存在 active Tool/Approval，先等稳定终态，不用强杀绕过。

- [ ] **Step 4: Start and sustain full soak**

```bash
uv run python scripts/phase6_production_gate.py start \
  --confirm-live \
  --evidence-dir ~/.lobster0/evidence/phase6-production/<run-id> \
  --progress-output <owner-selected-external-file>
```

保持 Mac 登录、唤醒和联网。若 monitor 进程退出，使用 `resume`；若 sleep/gap/invariant violation 已将 session 标为 failed，则必须新 run ID 从 0 开始，不能继续累计。

- [ ] **Step 5: Finalize after at least 86400 healthy seconds**

```bash
uv run python scripts/phase6_production_gate.py finalize \
  --confirm-live \
  --evidence-dir ~/.lobster0/evidence/phase6-production/<run-id>
```

Expected: `PHASE6_MACOS_FEISHU_PRODUCTION_VERIFIED` and zero secret matches。没有达到精确时长时返回 pending/non-zero，不修改发布文档。

- [ ] **Step 6: Update tracked truth from verified aggregate only**

release record 写 Evidence hashes/totals/time range；产品、架构、README、Phase 6 docs 与 HTML 改成 `MACOS+FEISHU PRODUCTION VERIFIED`。Telegram、Discord、Docker/VPS、Browser controlled live 与 `os_reboot=not_run` 保持各自真实状态。

- [ ] **Step 7: Final verify, merge and push**

重新运行 Step 1 的全部门禁，检查 `git status --short` 与 Secret scan，使用中英混合 commit。按 finishing-development-branch 流程 fast-forward/merge 到最新 main，解决冲突时保留双方有效改动，再在 main 重跑 full gate 并 push `origin/main`。

## PROD-C Done Definition

- [ ] LaunchAgent process recovery 和 exactly-one Feishu Delivery PASS。
- [ ] 连续 >=86400 秒 healthy soak，零 gap、零 invariant violation、零 Secret match。
- [ ] aggregate report 与全部 Live Evidence 绑定同一 clean commit。
- [ ] status 文档、进度页和 release record 只陈述真实通过的组合。
- [ ] Phase 7 工程文档完整、可执行且明确 NOT IMPLEMENTED。
- [ ] full unittest、Ruff、Channel/Automation/Browser gates、Browser worker、docs、diff 全绿。
- [ ] 已合并并推送 `main`；没有提交私有 Evidence、日志、数据库或 Secret。
