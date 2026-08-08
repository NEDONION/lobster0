# Feishu Owner Permission and Card Callback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Owner 私聊默认不产生普通 Tool 审批，并让飞书审批按钮具备可见、持久、幂等的完整续跑闭环。

**Architecture:** Policy 硬边界保持不变；当前 Owner 状态持久切到最高自动化模式。Approval continuation 恢复 Owner 信任，新建飞书回调编排器负责原卡状态更新与 durable final/next-approval Delivery，Gateway 只做依赖装配。

**Tech Stack:** Python 3.12、标准库 `unittest`、SQLite、Feishu Channel SDK、Ruff、macOS launchd。

## Global Constraints

- 不读取、输出或提交真实 API Key、App Secret、Token、对话或完整 Tool 参数。
- Owner 自动化必须在 Workspace、argv、网络和 Tool risk 硬校验之后生效。
- 非 Owner、群聊、敏感路径、SSRF、提权和 Critical Tool 不得扩权。
- 所有外部模型和飞书边界用 fake，单元测试不访问真实网络。

---

### Task 1: Approval continuation 保留 Owner 信任

**Files:**
- Modify: `tests/test_turn.py`
- Modify: `src/miniclaw/agent/turn.py`

**Interfaces:**
- Consumes: `TurnService.continue_approval(user_id, approval_id, decision=...)`
- Produces: continuation 的 `ToolContext.trusted_owner is True`，同时继续携带 Personal roots。

- [ ] 写一个 Autopilot continuation 测试：批准第一个 Tool 后，模型请求第二个中风险 Tool，第二个 Tool 必须直接执行且结果无 `approval_id`。
- [ ] 运行该测试，确认因 child context 不可信而 RED。
- [ ] 在 Owner-bound `_tool_context()` 中显式设置 `trusted_owner=True`。
- [ ] 复跑 `tests.test_turn` 与 `tests.test_permission_modes`，确认 GREEN。

### Task 2: 紧凑审批摘要和状态卡

**Files:**
- Modify: `tests/test_tool_executor.py`
- Modify: `tests/test_channel_approvals.py`
- Modify: `src/miniclaw/tools/executor.py`
- Modify: `src/miniclaw/channels/approvals.py`

**Interfaces:**
- Produces: 有界 `_approval_summary()`；`feishu_approval_status_card(state, visible)`。

- [ ] 测试长 `run_command` argv 不进入 summary，summary 只保留 executable、首个安全子命令与参数数量。
- [ ] 测试 processing/succeeded/denied/failed 分别渲染橙/绿/红卡且不含按钮。
- [ ] 运行 focused tests，确认旧实现 RED。
- [ ] 实现最小有界摘要与状态卡 renderer。
- [ ] 复跑 focused tests，确认 GREEN。

### Task 3: 飞书按钮回调 durable 闭环

**Files:**
- Create: `src/miniclaw/channels/feishu_approval.py`
- Create: `tests/test_feishu_approval.py`
- Modify: `src/miniclaw/gateway.py`

**Interfaces:**
- Produces: `FeishuApprovalActionHandler.__call__(actor_open_id, value, chat_id, message_id) -> None`。
- Consumes: `ChannelApprovalController`、`FeishuTransport`、`MessageRepository`、`DeliveryRepository`。

- [ ] 测试点击后先更新原卡为 processing，最终结果写入 durable message Delivery，再把原卡更新为 succeeded。
- [ ] 测试 continuation 返回新 `approval_id` 时写入 durable approval Delivery。
- [ ] 测试 malformed/non-owner/already-decided/未知异常只更新稳定失败卡，不泄露异常或重复执行。
- [ ] 运行新测试，确认模块缺失或旧 inline handler 行为不足而 RED。
- [ ] 实现编排器并替换 Gateway inline closure。
- [ ] 复跑 Gateway、Channel approvals、Feishu transport 与新测试，确认 GREEN。

### Task 4: 当前 Owner 运行配置与文档

**Files:**
- Modify outside repository: `~/.miniclaw/config.toml`
- Modify: `README.md`
- Modify: `docs/engineering/phase-2/20260808_autopilot-permissions-and-approval-ui.md`

- [ ] 仅把当前私有 `[tools].mode` 持久改为最高自动化模式，不打印其他配置值。
- [ ] 同步 Owner 私聊、回调状态机和 LaunchAgent 保活排障文档。
- [ ] 将 LaunchAgent `KeepAlive` 改为始终保活并重载，不提交 plist。

### Task 5: 完整验证与发布

**Files:** Verify all modified files.

- [ ] 运行 `uv run python -m unittest discover -s tests -v`，要求零失败。
- [ ] 运行 `uv run ruff check .` 与 `uv run python scripts/validate_docs.py`。
- [ ] 运行 `uv run miniclaw eval run --suite channel --repeat 20 --json --root evals/scenarios`，要求全部通过。
- [ ] 运行 `git diff --check`，确认无 Secret、调试输出和意外文件。
- [ ] 合并最新 `main`、推送 `origin/main`，更新 LaunchAgent commit 并验证 Gateway connected/ready。
