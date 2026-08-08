# Feishu Card Overflow and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让飞书短回答只显示一张 12px 最终卡片，长回答仅把未展示后缀回复到该卡片下方，并让重启与审批路径保持无重复、无丢失。

**Architecture:** `ChannelExperience` 负责计算 completed card 已承载的字符偏移和平台 reply target；`ChannelManager` 只把剩余后缀写入 durable Outbox。启动恢复复用稳定 progress UUID 重建同一终态，而 waiting approval 跳过普通回答卡片。

**Tech Stack:** Python 3.12、`unittest`、SQLite、official lark-channel-sdk、Feishu Card JSON 2.0、Ruff。

## Global Constraints

- 卡片 Markdown 使用 `text_size="small"`（12px），不使用 10px `x-small`。
- 不改变 `message_max_chars` 的平台字符预算。
- 自动化测试不得调用真实飞书、Discord、Telegram 或模型 Provider。
- 失败路径必须保留完整回答 fallback；日志、Audit 和测试输出不得包含 Token 或个人消息。
- 新增/修改 Python 方法必须有类型标注和中文 docstring。

---

### Task 1: Experience overflow contract

**Files:**
- Modify: `tests/test_channel_experience.py`
- Modify: `src/miniclaw/channels/experience.py`

**Interfaces:**
- Consumes: `ExperienceActivity.finish(content: str | None, failed: bool) -> ExperienceOutcome`
- Produces: `ExperienceOutcome.final_delivery_offset: int` and `final_reply_to_message_id: str | None`

- [ ] **Step 1: Write the failing boundary tests**

Add one test for `len(content) == max_visible_chars` and one for `len(content) > max_visible_chars`.
The long case must assert the literal offset `20`, the platform message ID `"progress-message"`, and that the rendered
card contains only the first 20 characters.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m unittest tests.test_channel_experience -v`

Expected: FAIL because the current outcome has no overflow offset/reply target and suppresses all final delivery.

- [ ] **Step 3: Implement the minimal outcome state**

Track completed visible length only after the final update succeeds. Return offset `0` and no card target after create/update
failure; return the full content length for an entirely contained answer; return `max_visible_chars` plus the card ID for an
overflow answer.

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m unittest tests.test_channel_experience -v`

Expected: all Experience tests pass.

### Task 2: Durable suffix and approval-only delivery

**Files:**
- Modify: `tests/test_channel_manager.py`
- Modify: `src/miniclaw/channels/manager.py`

**Interfaces:**
- Consumes: Task 1 `ExperienceOutcome`
- Produces: `_create_result_delivery(..., content_offset: int = 0, reply_to_message_id: str | None = None)`

- [ ] **Step 1: Write failing Manager tests**

Add an oversized Feishu answer fixture and assert SQLite contains only the literal suffix, with
`reply_to_message_id="om_manager_card"`. Extend the waiting-approval test with Experience attached and assert there is exactly
one Approval delivery and no normal progress card.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m unittest tests.test_channel_manager -v`

Expected: FAIL because Manager currently writes the full answer against the original inbound message and completes an approval reply card.

- [ ] **Step 3: Route only the suffix**

Pass outcome offset and reply target into `_create_result_delivery`; slice `result.content[content_offset:]` before
`split_message()`. For waiting approval, call `finish(content=None, failed=True)` and keep Approval Outbox creation unchanged.

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m unittest tests.test_channel_manager -v`

Expected: all Manager tests pass.

### Task 3: Idempotent completed-turn restart recovery

**Files:**
- Modify: `tests/test_channel_manager.py`
- Modify: `src/miniclaw/channels/manager.py`

**Interfaces:**
- Consumes: stable `ExperienceActivity.idempotency_key` and Task 1 outcome
- Produces: `async def _recover_stale(self) -> None`

- [ ] **Step 1: Write the failing restart test**

Persist a completed Turn while Inbox remains running, attach an idempotent fake Feishu Experience, start Manager twice, and
assert one logical card ID, zero duplicate ordinary full-text deliveries, and completed Inbox state.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m unittest tests.test_channel_manager.ChannelManagerTest.test_restart_recovers_completed_turn_through_same_card -v`

Expected: FAIL because current recovery unconditionally creates a message delivery.

- [ ] **Step 3: Recover through Experience**

Await `_recover_stale()` from `start()`. For completed Turns, call a fresh activity's `finish()` with persisted Assistant
content, then create only the outcome-required suffix/fallback before settling Inbox.

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m unittest tests.test_channel_manager -v`

Expected: all Manager recovery tests pass.

### Task 4: 12px Feishu renderer

**Files:**
- Modify: `tests/test_feishu_transport.py`
- Modify: `tests/test_channel_capabilities.py`
- Modify: `src/miniclaw/channels/feishu.py`
- Modify: `src/miniclaw/channels/capabilities.py`

**Interfaces:**
- Produces: both progress Card JSON renderers set Markdown `text_size` to literal `"small"`

- [ ] **Step 1: Add failing payload assertions**

Assert `card["body"]["elements"][0]["text_size"] == "small"` in the real Transport and compatibility lifecycle tests.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m unittest tests.test_feishu_transport tests.test_channel_capabilities -v`

Expected: FAIL with missing `text_size`.

- [ ] **Step 3: Add the supported Card JSON field**

Set `"text_size": "small"` on each Markdown element without changing title, content, template, or status behavior.

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m unittest tests.test_feishu_transport tests.test_channel_capabilities -v`

Expected: both suites pass.

### Task 5: Documentation, release evidence, and final gates

**Files:**
- Modify: `docs/engineering/phase-5/feishu-single-card-and-lark-cli.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/evals/releases/v0.5.2.md`
- Modify: `docs/progress/index.html`
- Modify: `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/miniclaw-progress.html`

**Interfaces:**
- Produces: current behavior, test counts, and live limitations visible from the docs index and progress page

- [ ] **Step 1: Update behavior diagrams and failure matrix**

Document 12px card text, suffix-only card replies, stable UUID recovery, approval-only behavior, and the fact that true live
Feishu confirmation remains pending until a user-authorized test message is sent.

- [ ] **Step 2: Run complete verification**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/miniclaw eval run --suite offline --root evals/scenarios
.venv/bin/miniclaw eval run --suite channel --root evals/scenarios
.venv/bin/miniclaw eval run --suite channel --repeat 20 --json --root evals/scenarios
.venv/bin/python scripts/validate_docs.py
git diff --check
```

Expected: all commands exit 0; counts in docs match fresh output.

- [ ] **Step 3: Review, commit, and push**

Run a focused code review, stage only the intended MiniClaw changes, commit with a mixed Chinese/English title such as
`fix(feishu): 卡片溢出回复与 restart recovery`, then push `main` and verify `HEAD == origin/main`.
