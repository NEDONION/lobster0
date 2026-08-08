# Autopilot Permissions and Compact Approval UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 为 MiniClaw 增加 safe/smart/autopilot/yolo 四档权限、可信 Owner 入口传播、动态切换与紧凑可滚动审批框。

**Architecture:** Python Core 持有唯一 `PermissionState`，PolicyEngine 在硬路径/命令/网络校验之后结合 `ToolContext.trusted_owner` 决定自动执行或审批。Bridge 与 Channel 控制命令只负责经过身份校验地切换这份状态；pi-tui 从握手读取状态并渲染紧凑 Header 和高度有界的 ApprovalDialog。

**Tech Stack:** Python 3.12、SQLite、标准库 `unittest`、TypeScript 5.9、`@earendil-works/pi-tui` 0.84.1、Node.js 22.19、pnpm 10。

> **完成记录（2026-08-08）：** 实现已快进合并到本地 `main`。合并后门禁为 Python 492/492、TypeScript 30/30、
> offline Agent 28/28、Channel 32/32、20 轮 local soak 640/640、Ruff 与文档校验 PASS；本机 Autopilot 配置权限保持
> 0600，Doctor 22/22 PASS。下列复选项均保留为逐步交付证据。

## Global Constraints

- 只对本地 TUI 与三平台 Owner 私聊启用 Autopilot；群聊与其他白名单成员 fail-closed。
- 所有模式保留敏感路径、Workspace 逃逸、命令和网络硬禁止边界。
- Python 公共类型与方法使用准确类型标注和中文 docstring。
- 不新增第三方依赖，不修改 protocol 版本号，不读取或提交真实凭据。
- 每个生产行为必须先有会因该行为缺失而失败的回归测试。
- 完成时运行 Python 全量、pi-tui 全量、Ruff、docs validator、Channel 32-case gate 与 20 轮 soak。

---

### Task 1: PermissionMode、配置与 Policy 状态表

**Files:**
- Create: `src/miniclaw/policy/modes.py`
- Modify: `src/miniclaw/config.py`
- Modify: `src/miniclaw/bootstrap.py`
- Modify: `src/miniclaw/tools/base.py`
- Modify: `src/miniclaw/policy/engine.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `src/miniclaw/storage/tooling.py`
- Test: `tests/test_config.py`
- Test: `tests/test_bootstrap.py`
- Test: `tests/test_command_policy.py`
- Test: `tests/test_network_policy.py`
- Test: `tests/test_tool_executor.py`

**Interfaces:**
- Produces: `PermissionMode(StrEnum)`, `PermissionState.mode`, `PermissionState.set_mode(mode, *, user_id, source)`。
- Produces: `ToolContext.trusted_owner: bool = False`。
- Produces: `ToolConfig.mode: str = "safe"` 与 `AgentRuntime.permission_state`。
- Consumes: 现有 `security × ask` 仅作为 safe 模式 command/http 兼容策略。

- [x] **Step 1: 写配置与状态表失败测试**

  在 `tests/test_config.py` 断言 `[tools] mode = "autopilot"` 可加载、未知 mode 失败；在 `tests/test_bootstrap.py` 断言新 Personal 模板显式写入 Autopilot；在 Policy 测试中用字面量状态表断言 trusted Owner 的四档行为和 untrusted 降级。

- [x] **Step 2: 运行 RED**

  Run: `.venv/bin/python -m unittest tests.test_config tests.test_bootstrap tests.test_command_policy tests.test_network_policy tests.test_tool_executor -v`

  Expected: FAIL because `tools.mode`、`PermissionMode` 和 `trusted_owner` 尚不存在。

- [x] **Step 3: 实现最小 PermissionState 与 Policy**

  `modes.py` 使用：

  ```python
  class PermissionMode(StrEnum):
      SAFE = "safe"
      SMART = "smart"
      AUTOPILOT = "autopilot"
      YOLO = "yolo"
  ```

  `PermissionState` 保存当前模式；`PolicyEngine.authorize()` 必须先完成现有 normalize/WorkspaceGuard/HTTPS 校验，再按设计文档状态表决策。`CRITICAL` 与所有 hard deny 不可被模式覆盖。

- [x] **Step 4: 实现配置、Runtime 和脱敏审计**

  `config.py` 严格解析 `tools.mode`；`bootstrap.py` 的新配置显式写 `autopilot`；`runtime.py` 创建并公开唯一状态。`PermissionModeAuditRepository.record()` 写 `policy.mode_changed`，metadata 只含 `previous_mode/current_mode/source`。

- [x] **Step 5: 运行 GREEN 与相关全量**

  Run: `.venv/bin/python -m unittest tests.test_config tests.test_bootstrap tests.test_command_policy tests.test_network_policy tests.test_tool_executor -v`

  Expected: PASS。

- [x] **Step 6: 提交 Task 1**

  ```bash
  git add src/miniclaw/policy/modes.py src/miniclaw/config.py src/miniclaw/bootstrap.py src/miniclaw/tools/base.py src/miniclaw/policy/engine.py src/miniclaw/runtime.py src/miniclaw/storage/tooling.py tests/test_config.py tests/test_bootstrap.py tests/test_command_policy.py tests/test_network_policy.py tests/test_tool_executor.py
  git commit -m "feat(policy): 增加 Autopilot 四档权限状态"
  ```

### Task 2: Owner 私聊信任传播与 Channel 控制命令

**Files:**
- Modify: `src/miniclaw/agent/turn.py`
- Modify: `src/miniclaw/channels/manager.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `src/miniclaw/gateway.py`
- Test: `tests/test_turn.py`
- Test: `tests/test_channel_manager.py`
- Test: `tests/test_gateway.py`
- Test: `tests/test_channel_approvals.py`

**Interfaces:**
- Consumes: `PermissionState`、`ToolContext.trusted_owner`。
- Produces: `TurnService.handle_inbound(..., trusted_owner: bool = False)`。
- Produces: `ChannelManager(..., owner_external_user_id: str, permission_state: PermissionState)`。

- [x] **Step 1: 写信任传播失败测试**

  测试 CLI 总是构造 trusted context；Channel Owner 私聊为 trusted；Owner 群聊、其他白名单成员为 untrusted，并且 untrusted context 不包含 Personal 额外 read/write roots。

- [x] **Step 2: 写 `/permissions` Channel 失败测试**

  使用真实 Manager + fake TurnHandler 断言 Owner 私聊 `/permissions autopilot` 切换共享状态且不调用模型；Owner 群聊和非 Owner 返回拒绝 notice；`/permissions` 返回当前状态。

- [x] **Step 3: 运行 RED**

  Run: `.venv/bin/python -m unittest tests.test_turn tests.test_channel_manager tests.test_gateway tests.test_channel_approvals -v`

  Expected: FAIL because Manager 尚未传播 Owner 私聊信任，也没有权限控制命令。

- [x] **Step 4: 实现最小信任传播与控制命令**

  Manager 只在 `event.external_user_id == owner_external_user_id` 且 `event.chat_type == "p2p"` 时传 `trusted_owner=True`。解析器只接受 `/permissions` 或 `/permissions <safe|smart|autopilot|yolo>` 的精确形态，控制命令永不进入 Provider。

- [x] **Step 5: 运行 GREEN 和 Channel 相关测试**

  Run: `.venv/bin/python -m unittest tests.test_turn tests.test_channel_manager tests.test_gateway tests.test_channel_approvals -v`

  Expected: PASS。

- [x] **Step 6: 提交 Task 2**

  ```bash
  git add src/miniclaw/agent/turn.py src/miniclaw/channels/manager.py src/miniclaw/runtime.py src/miniclaw/gateway.py tests/test_turn.py tests/test_channel_manager.py tests/test_gateway.py tests/test_channel_approvals.py
  git commit -m "feat(channels): 仅为 Owner 私聊传播 Autopilot 信任"
  ```

### Task 3: Bridge 动态切换与 TUI 权限状态

**Files:**
- Modify: `src/miniclaw/bridge/protocol.py`
- Modify: `src/miniclaw/bridge/server.py`
- Modify: `tui/src/protocol.ts`
- Modify: `tui/src/bridge-client.ts`
- Modify: `tui/src/app.ts`
- Modify: `tui/src/components/conversation.ts`
- Test: `tests/test_bridge_protocol.py`
- Test: `tests/test_bridge_server.py`
- Test: `tui/test/protocol.test.ts`
- Test: `tui/test/bridge-client.test.ts`
- Test: `tui/test/input.test.ts`
- Test: `tui/test/render.test.ts`

**Interfaces:**
- Produces: protocol request `permissions.set` with exact `mode` payload。
- Produces: `BridgePort.setPermissionMode(mode)`。
- Produces: `HeaderLine.setPermissionMode(mode)`。

- [x] **Step 1: 写 Bridge 协议与 busy 失败测试**

  断言合法模式可解码；未知值、额外字段失败；握手返回当前模式；空闲时切换成功并审计；active Turn 或 pending Approval 时返回稳定 `permissions_busy`。

- [x] **Step 2: 运行 Python Bridge RED**

  Run: `.venv/bin/python -m unittest tests.test_bridge_protocol tests.test_bridge_server -v`

  Expected: FAIL because protocol 和 Server 不认识 `permissions.set`。

- [x] **Step 3: 实现 Bridge GREEN**

  Server 只在空闲且无 pending Approval 时调用 `PermissionState.set_mode()`，响应包含当前模式；`client.hello` 同样返回 `permission_mode`。

- [x] **Step 4: 写 TypeScript 状态与命令 RED**

  断言 `/permissions` 展示当前模式，`/permissions yolo` 发一条 Bridge 请求并更新 Header，`/help` 和 `/status` 包含模式；四档 Header 始终一行且窄屏截断。

- [x] **Step 5: 运行 TypeScript RED**

  Run: `corepack pnpm --dir tui test`

  Expected: FAIL because TS protocol、BridgePort 和 Header 尚无权限模式。

- [x] **Step 6: 实现 TypeScript GREEN**

  扩展 RequestType、BridgeClient 和 MiniClawTui；Header 对 autopilot/yolo 使用永久可见的高对比徽标，其他信息保持单行。

- [x] **Step 7: 运行两端 GREEN**

  Run: `.venv/bin/python -m unittest tests.test_bridge_protocol tests.test_bridge_server -v`

  Run: `corepack pnpm --dir tui test`

  Expected: PASS。

- [x] **Step 8: 提交 Task 3**

  ```bash
  git add src/miniclaw/bridge/protocol.py src/miniclaw/bridge/server.py tui/src/protocol.ts tui/src/bridge-client.ts tui/src/app.ts tui/src/components/conversation.ts tests/test_bridge_protocol.py tests/test_bridge_server.py tui/test/protocol.test.ts tui/test/bridge-client.test.ts tui/test/input.test.ts tui/test/render.test.ts
  git commit -m "feat(tui): 支持动态权限模式与常驻状态徽标"
  ```

### Task 4: 高度有界的紧凑 ApprovalDialog

**Files:**
- Modify: `tui/src/components/approval.ts`
- Modify: `tui/src/app.ts`
- Test: `tui/test/approval.test.ts`
- Test: `tui/test/input.test.ts`

**Interfaces:**
- Produces: `ApprovalDialog(..., maxRows: () => number)`，render 结果永远不超过 maxRows。
- Consumes: Core 提供的 `grantModes`，不得自行增加按钮。

- [x] **Step 1: 写长参数与小终端 RED**

  构造 100 行中文 JSON 参数，断言 `render(76)` 在 maxRows=14 时不超过 14 行，第一屏和滚动后的每一屏都包含 title、位置指示、风险提示和底部选择；Home/End/PageUp/PageDown 改变参数窗口。

- [x] **Step 2: 运行 RED**

  Run: `corepack pnpm --dir tui test`

  Expected: FAIL because 当前 Dialog 无限展开且没有滚动状态。

- [x] **Step 3: 实现固定头尾和参数滚动窗口**

  先生成安全 wrap 后的 detail lines，再根据 `maxRows()` 扣除 border/title/summary/indicator/warning/footer，切出参数窗口；所有 offset 在内容变化和 resize 后 clamp。Overlay 改为最大 84 列、18 行，小终端取 `rows - 2`。

- [x] **Step 4: 运行 GREEN 与虚拟终端交互测试**

  Run: `corepack pnpm --dir tui test`

  Expected: PASS，包含既有粘贴、选择、streaming 和审批测试。

- [x] **Step 5: 提交 Task 4**

  ```bash
  git add tui/src/components/approval.ts tui/src/app.ts tui/test/approval.test.ts tui/test/input.test.ts
  git commit -m "fix(tui): 让长审批参数可滚动且按钮常驻"
  ```

### Task 5: 工程文档、默认配置与发布门禁

**Files:**
- Create: `docs/engineering/phase-2/20260808_autopilot-permissions-and-approval-ui.md`
- Modify: `README.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/progress/index.html`
- Modify: `docs/README.md`

**Interfaces:**
- Documents: 四档状态表、Owner 私聊信任边界、命令、TUI 操作、审计查询和回滚方式。

- [x] **Step 1: 写已验证事实文档**

  工程文档包含数据流、状态表、配置、TUI 键位、三平台信任判断、测试矩阵和故障排查。README/运行指南/架构/进度页只描述已经通过测试的行为。

- [x] **Step 2: 运行文档与静态校验**

  Run: `.venv/bin/python scripts/validate_docs.py`

  Run: `.venv/bin/ruff check .`

  Expected: PASS。

- [x] **Step 3: 运行完整发布门禁**

  Run: `.venv/bin/python -m unittest discover -s tests -v`

  Run: `corepack pnpm --dir tui test`

  Run: `.venv/bin/miniclaw eval run --suite channel --root evals/scenarios`

  Run: `.venv/bin/miniclaw eval run --suite channel --repeat 20 --json --root evals/scenarios`

  Run: `git diff --check main...HEAD`

  Expected: 全部 PASS，Channel case 32/32，20 轮 soak 640/640。

- [x] **Step 4: 提交 Task 5**

  ```bash
  git add docs README.md
  git commit -m "docs(permissions): 记录 Autopilot 与紧凑审批规范"
  ```

- [x] **Step 5: 更新本机 Owner 配置并做真实 doctor smoke**

  在不打印配置正文和密钥的前提下，把 `~/.miniclaw/config.toml` 的 `[tools].mode` 设为 `autopilot`，保持 0600；运行 `uv run miniclaw doctor` 和本地 pi-tui 启动检查。

  Expected: doctor 全部 PASS，Header 显示 `⚡ AUTOPILOT`。

- [x] **Step 6: 完成分支交付**

  按 `superpowers:finishing-a-development-branch` 复验、合并并推送；最后保证 `/Users/nedonion/PycharmProjects/miniclaw` 位于最新 `main` 且与 `origin/main` 一致。
