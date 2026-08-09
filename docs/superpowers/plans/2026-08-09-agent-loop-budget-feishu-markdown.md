# Adaptive Agent Loop and Feishu Markdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MiniClaw's abrupt eight-round stop with a 32→64 adaptive model budget and render structured Markdown correctly inside the single Feishu progress card.

**Architecture:** Keep the existing `AgentRunner` and Feishu Card 2.0 boundaries. Add typed soft/hard/no-progress budgets to config, make Runner suppress exact repeated Tool requests and reserve a tool-free finalization request, then replace the answer-wide bullet flattener with a fence-aware Markdown normalizer that only degrades tables.

**Tech Stack:** Python 3.12+, standard library `json`/`re`/`html`, `unittest`, Feishu Card JSON 2.0, Ruff.

## Global Constraints

- Soft model budget defaults to 32, hard budget to 64, and no-progress threshold to 3.
- No model loop is unbounded; hard budget must be greater than or equal to soft budget.
- Tool Policy, Approval, cancellation and Provider failure semantics remain unchanged.
- Final answer may expose Markdown structure but never raw HTML, private reasoning, Tool arguments, Tool output or credentials.
- Markdown tables become bullet rows; headings, paragraphs, lists, quotes, links, emphasis and code remain structured.
- Card payload stays at or below 20 KiB and `visible_answer_chars` remains an exact prefix offset.
- Tests are offline, deterministic and use the existing `FakeProvider` and temporary state.

---

### Task 1: Typed adaptive budget configuration

**Files:**
- Modify: `src/miniclaw/config.py`
- Modify: `src/miniclaw/bootstrap.py`
- Modify: `src/miniclaw/runtime.py`
- Modify: `src/miniclaw/evals/runner.py`
- Test: `tests/test_config.py`
- Test: `tests/test_bootstrap.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Produces: `AgentConfig.max_tool_iterations: int`, `AgentConfig.max_tool_iterations_hard: int`, and `AgentConfig.max_no_progress_iterations: int`.
- Produces: environment overrides `MINICLAW_MAX_TOOL_ITERATIONS_HARD` and `MINICLAW_MAX_NO_PROGRESS_ITERATIONS`.
- Consumes: existing `AgentRunner(..., max_iterations=...)` construction points, expanded in Task 2.

- [ ] **Step 1: Write failing config tests**

```python
def test_missing_file_uses_adaptive_agent_defaults(self) -> None:
    config = load_config(self.paths, {}, {})
    self.assertEqual(config.agent.max_tool_iterations, 32)
    self.assertEqual(config.agent.max_tool_iterations_hard, 64)
    self.assertEqual(config.agent.max_no_progress_iterations, 3)

def test_agent_budget_rejects_hard_limit_below_soft_limit(self) -> None:
    self.paths.config.write_text(
        "[agent]\nmax_tool_iterations = 40\nmax_tool_iterations_hard = 32\n",
        encoding="utf-8",
    )
    with self.assertRaisesRegex(ConfigError, "max_tool_iterations_hard"):
        load_config(self.paths, {}, {})
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `PYTHONPATH=src ../../.venv/bin/python -m unittest tests.test_config tests.test_bootstrap tests.test_runtime -v`

Expected: failures show missing adaptive fields and the old default value 8.

- [ ] **Step 3: Implement strict config parsing and assembly**

```python
@dataclass(frozen=True, slots=True)
class AgentConfig:
    model: str = "provider/model"
    max_tool_iterations: int = 32
    max_tool_iterations_hard: int = 64
    max_no_progress_iterations: int = 3
    context_budget_tokens: int = 32_000
    tool_result_max_chars: int = 20_000
```

Add the two keys to `_AGENT_KEYS`, parse file and environment values as positive integers, and reject
`max_tool_iterations_hard < max_tool_iterations` after environment overrides. Update `init` to emit all three values.

- [ ] **Step 4: Pass all construction points**

```python
AgentRunner(
    provider,
    executor,
    max_iterations=config.agent.max_tool_iterations,
    hard_max_iterations=config.agent.max_tool_iterations_hard,
    max_no_progress_iterations=config.agent.max_no_progress_iterations,
)
```

Apply the exact construction to production Runtime and offline eval Runtime.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `PYTHONPATH=src ../../.venv/bin/python -m unittest tests.test_config tests.test_bootstrap tests.test_runtime -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/miniclaw/config.py src/miniclaw/bootstrap.py src/miniclaw/runtime.py src/miniclaw/evals/runner.py tests/test_config.py tests/test_bootstrap.py tests/test_runtime.py
git commit -m "feat(config): 增加 adaptive Agent loop budgets"
```

---

### Task 2: Adaptive Runner, repeated-call guard and actionable diagnostics

**Files:**
- Modify: `src/miniclaw/agent/runner.py`
- Modify: `src/miniclaw/agent/turn.py`
- Modify: `src/miniclaw/channels/manager.py`
- Modify: `src/miniclaw/tui/app.py`
- Test: `tests/test_agent_runner.py`
- Test: `tests/test_turn.py`
- Test: `tests/test_channel_manager.py`

**Interfaces:**
- Produces: `AgentNoProgressError(AgentError)` mapped to `loop_no_progress`.
- Produces: `AgentRunner(..., max_iterations=32, hard_max_iterations=64, max_no_progress_iterations=3)`.
- Consumes: the three typed config fields from Task 1.

- [ ] **Step 1: Write failing Runner tests**

```python
async def test_successful_progress_extends_soft_budget_and_hard_round_has_no_tools(self) -> None:
    calls = tuple(
        response("", tool_calls=(ToolCall(f"call_{index}", "echo", {"text": str(index)}),))
        for index in range(4)
    )
    provider = FakeProvider((*calls, response("wrapped")))
    executor = self.executor(_EchoTool())
    result = await AgentRunner(
        provider,
        executor,
        max_iterations=3,
        hard_max_iterations=5,
        max_no_progress_iterations=3,
    ).run(request(*executor.schemas), tool_context=self.tool_context)
    self.assertEqual(result.content, "wrapped")
    self.assertEqual(provider.requests[-1].tools, ())

async def test_three_repeated_tool_fingerprints_stop_without_reexecution(self) -> None:
    provider = FakeProvider(tuple(
        response("", tool_calls=(ToolCall(f"call_{index}", "echo", {"text": "same"}),))
        for index in range(4)
    ))
    tool = _EchoTool()
    executor = self.executor(tool)
    with self.assertRaises(AgentNoProgressError):
        await AgentRunner(
            provider,
            executor,
            max_iterations=8,
            hard_max_iterations=12,
            max_no_progress_iterations=3,
        ).run(request(*executor.schemas), tool_context=self.tool_context)
    self.assertEqual(tool.executions, 1)
```

- [ ] **Step 2: Run focused Runner tests and confirm RED**

Run: `PYTHONPATH=src ../../.venv/bin/python -m unittest tests.test_agent_runner -v`

Expected: constructor rejects the new keyword arguments and `AgentNoProgressError` is missing.

- [ ] **Step 3: Implement progress fingerprints and finalization request**

```python
def _tool_fingerprint(call: ToolCall) -> str:
    return json.dumps(
        {"name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

def _tool_succeeded(message: ModelMessage) -> bool:
    try:
        payload = json.loads(message.content)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True
```

Track attempted fingerprints, skip repeated execution with a synthetic `duplicate_tool_call` Tool result, count a model
round as progress only when at least one novel Tool succeeds, and raise `AgentNoProgressError` after three non-progress
rounds. Before the soft-boundary request, extend only if the previous batch progressed. Always make the hard-boundary
request with `tools=()` and an ephemeral system instruction that requires an evidence-based final answer.

- [ ] **Step 4: Persist and present the new stable error**

Map `AgentNoProgressError` before `AgentLoopLimitError` in `agent.turn._error_code`. Add Chinese/English TUI summaries.
In `channels.manager._failure_profile`, return:

```python
(
    "Agent Tool Loop",
    "连续多轮没有新的成功 Tool 结果，已停止重复执行。",
    "请检查 Claw Trail 与 ToolRun；调整请求后重试。",
)
```

Keep `_failure_diagnostics` as the single red-card formatter so Turn/Event IDs and Tool count remain visible.

- [ ] **Step 5: Run Runner, Turn and Channel failure tests**

Run: `PYTHONPATH=src ../../.venv/bin/python -m unittest tests.test_agent_runner tests.test_turn tests.test_channel_manager -v`

Expected: PASS, including existing Approval, cancellation and `loop_limit` tests updated to the hard-boundary contract.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/miniclaw/agent/runner.py src/miniclaw/agent/turn.py src/miniclaw/channels/manager.py src/miniclaw/tui/app.py tests/test_agent_runner.py tests/test_turn.py tests/test_channel_manager.py
git commit -m "feat(agent): 自适应扩展 Tool Loop 并阻止重复调用"
```

---

### Task 3: Structure-preserving Feishu Markdown renderer

**Files:**
- Modify: `src/miniclaw/channels/feishu_cards.py`
- Test: `tests/test_feishu_agent_card.py`
- Test: `tests/test_channel_experience.py`

**Interfaces:**
- Produces: `_render_answer_markdown(answer: str) -> str` for final-answer content only.
- Produces: `_safe_markdown_prefix_length(answer: str, limit: int) -> int` for exact overflow offsets.
- Preserves: `_escape_markdown(text: str)` for trusted internal labels and Claw Trail data.

- [ ] **Step 1: Replace bullet-only tests with failing Markdown coverage**

```python
def test_final_answer_preserves_commonmark_and_only_converts_tables(self) -> None:
    answer = (
        "# 结论\n\n普通段落含 **粗体**、[链接](https://example.com) 和 `code`。\n\n"
        "> 引用\n\n1. 第一项\n2. 第二项\n\n"
        "```python\nprint('| not a table |')\n```\n\n"
        "| 项目 | 内容 |\n| --- | --- |\n| 标题 | 文档 A |"
    )
    final_content = self._final_content(answer)
    self.assertIn("# 结论", final_content)
    self.assertIn("普通段落含 **粗体**", final_content)
    self.assertIn("> 引用", final_content)
    self.assertIn("```python", final_content)
    self.assertIn("print('| not a table |')", final_content)
    self.assertIn("- **标题**：文档 A", final_content)
    self.assertNotIn("| --- |", final_content)
```

Add a raw `<at id=all></at>` case that must render as escaped text, and an oversized fenced-code case whose card contains
a closing fence while `answer[visible_answer_chars:]` remains the exact tail.

- [ ] **Step 2: Run focused card tests and confirm RED**

Run: `PYTHONPATH=src ../../.venv/bin/python -m unittest tests.test_feishu_agent_card tests.test_channel_experience -v`

Expected: current bullet flattener removes paragraph/headings and escapes code fences.

- [ ] **Step 3: Implement fence-aware Markdown normalization**

Scan `splitlines(keepends=False)` while tracking backtick or tilde fence marker. Outside fences, escape only raw HTML tags
and detect Markdown tables; inside fences, preserve bytes and never run table/HTML conversion. Keep all blank lines and
non-table Markdown unchanged. If input ends inside a fence, append a matching closing marker on its own line.

Replace:

```python
answer_content = _escape_markdown(_answer_as_bullets(answer))
```

with:

```python
answer_content = _render_answer_markdown(answer)
```

Use `\n\n> _答案过长，剩余内容将继续发送。_` for the trimmed notice.

- [ ] **Step 4: Make overflow clipping prefer structural boundaries**

After binary search finds the largest byte-safe prefix, retreat to the last paragraph break or newline when one exists;
rebuild the card and return that exact original-character offset. Do not retreat for a single unbroken line.

- [ ] **Step 5: Run focused card and delivery tests**

Run: `PYTHONPATH=src ../../.venv/bin/python -m unittest tests.test_feishu_agent_card tests.test_channel_experience tests.test_channel_manager -v`

Expected: PASS, including exact tail-delivery assertions.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/miniclaw/channels/feishu_cards.py tests/test_feishu_agent_card.py tests/test_channel_experience.py
git commit -m "fix(feishu): 保留 Markdown 结构并安全降级 tables"
```

---

### Task 4: Documentation, local rollout and release gates

**Files:**
- Modify: `README.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Modify: `docs/engineering/phase-1/20260807_agent-runner.md`
- Modify: local untracked `/Users/nedonion/.miniclaw/config.toml` after Git merge

**Interfaces:**
- Consumes: verified 32/64/3 config and Feishu Markdown behavior from Tasks 1–3.
- Produces: user-facing configuration examples and a restarted production Gateway on merged `main`.

- [ ] **Step 1: Update current behavior documentation**

Document that model-call budgets are 32 soft / 64 hard / 3 no-progress rounds, and that Feishu preserves CommonMark while
converting only tables to bullets. Remove the statement that all final answers become bullet points.

- [ ] **Step 2: Run focused and complete verification**

```bash
PYTHONPATH=src ../../.venv/bin/python -m unittest tests.test_agent_runner tests.test_config tests.test_feishu_agent_card tests.test_channel_manager -v
PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests -v
../../.venv/bin/ruff check .
PYTHONPATH=src ../../.venv/bin/python scripts/validate_docs.py
PYTHONPATH=src ../../.venv/bin/python -m miniclaw eval run --suite channel --repeat 20 --json --root evals/scenarios
git diff --check
```

Expected: all unit tests pass, Ruff and docs pass, and Channel gate reports 640/640.

- [ ] **Step 3: Commit documentation and request final review**

```bash
git add README.md docs/getting-started/20260807_本地运行指南.md docs/engineering/phase-1/20260807_agent-runner.md
git commit -m "docs(agent): 同步 adaptive budget 与 Feishu Markdown"
```

Run the `requesting-code-review` skill and fix all Critical or Important findings, then rerun affected tests.

- [ ] **Step 4: Integrate with latest main without overwriting concurrent work**

Fetch/review current `main`, rebase or merge the feature branch onto it, merge with `--ff-only` when possible, rerun the
release gates on the exact merge commit, and push `main` to origin. Do not stage `.pnpm-store/` or unrelated Phase 6 files.

- [ ] **Step 5: Upgrade private local config and restart Gateway**

Under the existing `[agent]` table set:

```toml
max_tool_iterations = 32
max_tool_iterations_hard = 64
max_no_progress_iterations = 3
```

Preserve file mode 0600 and every unrelated setting. Restart the macOS LaunchAgent once, then verify the new Gateway PID,
ready log marker and loaded non-secret budget values. Never print credentials or the full config file.
