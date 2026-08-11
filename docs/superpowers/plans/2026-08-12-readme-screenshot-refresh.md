# README Screenshot Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 2～5 张最新、干净、代表性强的产品截图和更口语化的中文文案，把 README 改成面向第一次访问者的产品首页，并将篇幅缩短约 30%。

**Architecture:** 不修改产品代码。截图由当前仓库构建、隔离的 Lobster0 Home、演示 Workspace 和本地固定 Provider 产生；能确认真实登录状态时才使用飞书实际界面。README 保留关键技术事实，把工程细节下沉到现有文档链接。

**Tech Stack:** Markdown、Electron/React Desktop、pi-tui、Feishu/Lark、macOS window capture、`scripts/validate_docs.py`

## Global Constraints

- 只修改 `README.md`、`docs/assets/` 下本轮采用的截图，以及本计划文档。
- README 基线为 490 行、28,684 字节；目标为 330～360 行、20～21 KiB。
- 最终使用 2～5 张截图；图片不得包含真实姓名、私人聊天、Token、Secret 或无关通知。
- 固定 Provider 只能证明真实本地 UI/Core 路径，不得写成真实模型或飞书 Live PASS。
- 未完成的 Release、Live Gate、controlled smoke 与 production soak 必须继续标为 pending。

---

### Task 1: 生成隔离、可重复的演示状态

**Files:**
- Create temporarily: `/tmp/lobster0-readme-demo-*`
- Create: `docs/assets/lobster0-desktop-weekly-brief.png`
- Create: `docs/assets/lobster0-tui-repository-check.png`
- Create when live state is available: `docs/assets/lobster0-feishu-claw-trail.png`
- Create when visually distinct: `docs/assets/lobster0-approval.png`

**Interfaces:**
- Consumes: `uv run lobster0 init`、`pnpm --dir tui build`、`pnpm --dir desktop build`、现有 Electron smoke 与 macOS 截图脚本。
- Produces: 2～5 张可直接被 README 引用的 PNG。

- [ ] **Step 1: 确认工作区和构建基线**

Run:

```bash
git status --short
pnpm --dir tui build
pnpm --dir desktop build
```

Expected: Git 只包含本计划的预期改动；两个构建退出码均为 0。

- [ ] **Step 2: 创建隔离演示 Home 与 Workspace**

Set `LOBSTER0_DEMO_ROOT` to `mktemp -d /tmp/lobster0-readme-demo.XXXXXX`, initialize it with `uv run lobster0 --home "$LOBSTER0_DEMO_ROOT" init`, and create `workspace/本周会议记录.md` through `apply_patch`. The fixture contains three fictional decisions, two risks and next-week actions, and only uses role names“产品负责人”“开发”“运营”。

Expected: `config.toml`、SQLite 和演示文件全部位于临时目录，仓库与个人 `~/.lobster0` 未被读取或修改。

- [ ] **Step 3: 启动固定 loopback Provider**

Use `/tmp/lobster0-readme-demo-provider.py`, created through `apply_patch`, to start a standard-library `ThreadingHTTPServer` on `127.0.0.1:8765` and return OpenAI-compatible SSE. For the Desktop prompt it first emits a `read_file` Tool Call for `本周会议记录.md`, then returns a concise weekly brief; for the TUI prompt it emits `run_command` with exact argv for `git status --short --branch`, then summarizes the result. Point only the isolated process at it with `LOBSTER0_MODEL_BASE_URL=http://127.0.0.1:8765` and `LOBSTER0_MODEL_API_KEY=demo-only`.

Expected: no external network request, no real Provider credential, and the real Bridge/TurnService/Policy/ToolExecutor path handles the turns.

- [ ] **Step 4: 截取 Desktop 主图**

Start the built Electron app against the isolated Home, submit“读取本周会议记录，整理成一份周报：给出本周进展、风险和下周待办。”through the real renderer, wait for the completed assistant response, and capture only the 1280×820 application window to `docs/assets/lobster0-desktop-weekly-brief.png`.

Expected: image contains Lobster0 branding, the representative prompt, the completed answer, and no setup/debug window or personal path.

- [ ] **Step 5: 截取 TUI 开发任务**

Open the built pi-tui in a clean macOS Terminal window against the same isolated Home, submit“检查这个演示仓库当前状态，并告诉我有没有未提交改动。”and capture only the terminal window to `docs/assets/lobster0-tui-repository-check.png`.

Expected: image shows current `Lobster0` name, user request, Tool trail/final answer and telemetry; it must not show `MiniClaw` or unrelated terminal tabs.

- [ ] **Step 6: 尝试真实飞书画面并决定最终图片集合**

Activate `/Applications/Lark.app`, navigate directly to the Lobster0 bot conversation without inspecting unrelated chats, send a fictional work-summary request only if the configured bot is already available, then capture and crop the conversation pane. If the bot or clean live conversation is unavailable, omit this asset and retain 2～4 truthful Desktop/TUI/approval images.

Expected: any retained Feishu image is an actual Lark window with the current `Claw Trail` card; otherwise no Feishu screenshot is created or claimed.

- [ ] **Step 7: 原图视觉检查**

Open every candidate PNG at original detail and reject any image with clipped text, unreadable scaling, private data, stale branding, fake Live status or repeated content that adds no new information.

Expected: final `docs/assets/` set contains 2～5 clean, materially different screenshots.

### Task 2: 重写中文 README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1 retained asset filenames and `docs/superpowers/specs/2026-08-12-readme-screenshot-refresh-design.md`.
- Produces: the public Chinese landing-page README.

- [ ] **Step 1: 重排首屏和使用场景**

Replace the old Warp/MiniClaw hero with the Desktop hero. Lead with one plain-language sentence, a short “它能帮你做什么” list, and the retained screenshots. Each caption explains the work outcome, not the internal component names.

- [ ] **Step 2: 压缩安装与能力说明**

Keep the one-line installer status warning, source installation commands, core capability table, Permission Modes and common entry commands. Move the long installer flags, managed Node policy, upgrade failure internals and Desktop W0/D1 history behind existing documentation links.

- [ ] **Step 3: 压缩技术与证据部分**

Keep one architecture diagram, a plain-language six-step execution flow, concise Security/Memory/Automation/Browser summaries, current evidence status and essential verification commands. Remove repeated historical baselines and duplicated Phase explanations without changing any PASS/PENDING claim.

- [ ] **Step 4: 测量篇幅并做第二轮删改**

Run:

```bash
wc -l -w -c README.md
```

Expected: 330～360 lines and approximately 20～21 KiB. If one metric is outside target, delete repetition rather than removing a key fact or adding compressed jargon.

### Task 3: 验证与交付

**Files:**
- Verify: `README.md`
- Verify: `docs/assets/*.png`
- Verify: `docs/superpowers/plans/2026-08-12-readme-screenshot-refresh.md`

**Interfaces:**
- Consumes: completed README and retained screenshots.
- Produces: evidence that links, layout, tests and repository hygiene are sound.

- [ ] **Step 1: 运行文档和静态验证**

Run:

```bash
uv run python scripts/validate_docs.py
uv run ruff check .
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 2: 运行完整离线测试**

Run:

```bash
uv run python -m unittest discover -s tests -v
```

Expected: all tests pass without network or personal data access.

- [ ] **Step 3: 最终范围与隐私审计**

Run `git status --short`, inspect `git diff -- README.md` and list the exact new PNG files. Re-open every retained image at original detail and verify every README image target exists.

Expected: no product code, generated cache, personal configuration or unrelated concurrent change appears in the diff.

- [ ] **Step 4: 提交实现**

Before and after committing, run `git fetch` and `git status --short`. Stage only the explicit README, retained PNG and plan paths, then commit with:

```bash
git commit -m "docs(readme): 用代表性场景重写产品首页"
```

Expected: one implementation commit containing only the reviewed documentation and screenshot assets.
