# Phase 3 Memory、Skills 与 Compaction Implementation Plan

> 执行方式：在当前 `main` 上按 TDD 小步提交；保留用户无关工作区修改，不暂存真实 `.env` 或个人 Memory。

**目标：** 完成可持久、可审阅、可回放的长期/每日记忆，最多 3 个 Markdown Skill 惰性加载，以及不会删除原消息的会话压缩。

**架构：** 复用现有 `StatePaths`、`ContextBuilder`、`messages` 表和 Tool Executor。Memory 与 Skill 都是本地 Markdown；compaction summary 作为带 metadata 的 system message 落入现有 SQLite，不新增第三方依赖或向量库。

**技术栈：** Python 3.12、stdlib、SQLite、现有 OpenAI-compatible Provider、`unittest`、Ruff。

---

## Task 1：MemoryStore 与安全边界

**Files:**

- Create: `src/lobster0/memory/__init__.py`
- Create: `src/lobster0/memory/store.py`
- Create: `tests/test_memory_store.py`

**RED：** 写入长期/今日/昨日读取、重复事实、64 KiB、symlink、非法 UTF-8 与凭据过滤契约测试。

**GREEN：** 用 `Path`、`os.open(O_NOFOLLOW)`、SHA-256 和固定 Markdown 模板实现最小 Store。

**Commit：** `feat(memory): 建立 safe Markdown 记忆层`

## Task 2：Memory Tools、Policy 与审批闭环

**Files:**

- Create: `src/lobster0/tools/memory.py`
- Modify: `src/lobster0/config.py`
- Modify: `src/lobster0/runtime.py`
- Modify: `src/lobster0/agent/context.py`
- Modify: `tests/test_tool_contract.py`
- Modify: `tests/test_tool_executor.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_context.py`

**RED：** `read_memory` 自动执行；`propose_memory` 只能审批后写；当前 Query 明确要求记住时模型能看到 Tool 规则。

**GREEN：** 注册两个 Tool，复用风险 Policy；Context 注入长期、今日与昨日 Memory。

**Commit：** `feat(agent): 打通 Memory Tool 与 approval 闭环`

## Task 3：SkillLoader 与示例 Skill

**Files:**

- Create: `src/lobster0/skills/__init__.py`
- Create: `src/lobster0/skills/loader.py`
- Create: `tests/test_skills.py`
- Modify: `src/lobster0/bootstrap.py`
- Modify: `src/lobster0/agent/context.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/test_context.py`

**RED：** strict frontmatter、目录名、64 KiB、symlink、query 排名、稳定 tie-break、最多 3 个与正文按需读取。

**GREEN：** stdlib 单行 frontmatter parser、Unicode 关键词匹配和 SHA-256；初始化提供 `summarize` 示例 Skill。

**Commit：** `feat(skills): 实现 lazy SKILL.md 激活`

## Task 4：Context budget 与持久 Compaction

**Files:**

- Create: `src/lobster0/agent/compaction.py`
- Create: `tests/test_compaction.py`
- Modify: `src/lobster0/storage/conversations.py`
- Modify: `src/lobster0/agent/context.py`
- Modify: `src/lobster0/agent/turn.py`
- Modify: `src/lobster0/providers/base.py`
- Modify: `src/lobster0/runtime.py`
- Modify: `tests/test_conversations.py`
- Modify: `tests/test_context.py`
- Modify: `tests/test_turn.py`

**RED：** 80% 触发、完整 Turn 边界、最近两 Turn/waiting approval、summary 重启恢复、原消息不删、Provider 失败退化、snapshot hash。

**GREEN：** 当前 Provider 生成结构化摘要；复用 `messages` system row 保存 coverage metadata；ContextBuilder 预算裁剪并保持当前用户消息。

**Commit：** `feat(context): 增加 persistent compaction 与预算保护`

## Task 5：Agent 回归、文档与发布门禁

**Files:**

- Modify: `evals/cases/*.json` 或当前稳定 suite
- Modify: `docs/engineering/phase-3/20260808_memory-skills-compaction.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `README.md`
- Modify: `docs/progress/index.html`
- Modify: `/Users/nedonion/Documents/Codex/2026-08-07/new-chat/outputs/lobster0-progress.html`

**验证：**

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run lobster0 eval run --suite regression
uv build
git diff --check
```

再执行真实 TUI/CLI memory、Skill 与小预算 compaction smoke，记录不含个人内容的计数和状态证据。

**Commit：** `docs(phase3): 同步 Memory/Skills 验证与 progress`

**发布：** 检查只包含本阶段提交，推送 `main`；远端分支与本地 HEAD 一致后才宣告完成。
