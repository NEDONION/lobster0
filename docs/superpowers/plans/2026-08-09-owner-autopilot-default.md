# Owner Autopilot Default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让缺少 `tools.mode` 的旧配置默认进入可信 Owner Autopilot，使飞书 Owner 私聊的安全命令不再生成审批卡片。

**Architecture:** 只修改配置缺省语义，不修改 Policy、ToolExecutor 或飞书卡片分支。运行时继续先验证 Owner 私聊和所有硬安全边界；只有缺少模式配置时从 `safe` 改为 `autopilot`，显式模式保持不变。

**Tech Stack:** Python 3.12、标准库 `tomllib`、`unittest`、Ruff、SQLite Channel eval。

## Global Constraints

- 只为已验证的 Owner 私聊和本地 TUI启用 Autopilot；群聊与其他用户不得扩权。
- 敏感路径、危险命令、Workspace 逃逸、SSRF、超时和结果预算继续硬拒绝。
- 不删除 Approval 或飞书审批卡基础设施；显式 `safe`/`smart` 仍可创建审批。
- 不修改或提交用户现有的 `docs/assets/`。
- 测试离线、快速、可重复，不调用真实模型或飞书接口。

---

### Task 1: 统一旧配置与新安装的 Autopilot 默认值

**Files:**
- Modify: `tests/test_config.py:20-55`
- Modify: `src/miniclaw/config.py:151-160`
- Modify: `src/miniclaw/config.py:410-418`

**Interfaces:**
- Consumes: `load_config(paths: StatePaths, environ: Mapping[str, str] | None, overrides: Mapping[str, OverrideValue] | None) -> AppConfig`
- Produces: `ToolConfig.mode == "autopilot"` when `tools.mode` is absent; explicit valid modes remain unchanged.

- [ ] **Step 1: Write the failing configuration tests**

Change the missing-file expectation and add an explicit opt-out test:

```python
def test_missing_file_uses_predictable_defaults(self) -> None:
    """尚未生成配置文件时应返回可预测且不含密钥值的默认配置。"""
    config = load_config(
        self.paths,
        {"MINICLAW_MODEL_API_KEY": "secret-must-stay-outside-config"},
        {},
    )
    # existing assertions stay unchanged
    self.assertEqual(config.tools.mode, "autopilot")

def test_explicit_safe_tool_mode_overrides_autopilot_default(self) -> None:
    """用户显式选择 safe 时必须保留审批模式。"""
    self.paths.config.write_text('[tools]\nmode = "safe"\n', encoding="utf-8")

    config = load_config(self.paths, {}, {})

    self.assertEqual(config.tools.mode, "safe")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
uv run python -m unittest tests.test_config.ConfigTest.test_missing_file_uses_predictable_defaults -v
```

Expected: `FAIL` because the current loader returns `safe`, proving the regression test exercises the reported root cause.

- [ ] **Step 3: Implement the single configuration fix**

Use one source of truth for the dataclass and loader default:

```python
@dataclass(frozen=True, slots=True)
class ToolConfig:
    """保存 Phase 2 Tool 能力上限和审批默认值。"""

    enabled: tuple[str, ...] = BUILTIN_TOOL_NAMES
    mode: str = "autopilot"
    # remaining fields unchanged


tool_mode = _enum_string(
    tools_raw.get("mode", ToolConfig.mode),
    "tools.mode",
    frozenset({"safe", "smart", "autopilot", "yolo"}),
)
```

- [ ] **Step 4: Run focused configuration and permission tests and verify GREEN**

Run:

```bash
uv run python -m unittest tests.test_config tests.test_permission_modes -v
```

Expected: all tests pass. Existing permission tests continue proving that Autopilot only allows trusted Owner actions and that untrusted inputs still require approval.

- [ ] **Step 5: Commit the behavior change**

```bash
git add src/miniclaw/config.py tests/test_config.py
git commit -m "fix(policy): 统一 legacy config 的 autopilot 默认值"
```

### Task 2: 同步当前产品语义与 Owner 本机配置

**Files:**
- Modify: `README.md:138-146`
- Modify: `docs/product/20260807_产品需求文档.md:347-360`
- Modify: `docs/architecture/20260807_系统架构.md:206-214`
- Modify: `docs/engineering/phase-2/autopilot-permissions-and-approval-ui.md:105-121`
- Modify: `docs/engineering/phase-2/autopilot-permissions-and-approval-ui.md:217-228`
- Modify: `docs/superpowers/specs/2026-08-08-autopilot-permissions-and-approval-ui-design.md:70-78`
- Modify outside repository, never stage: `~/.miniclaw/config.toml`

**Interfaces:**
- Consumes: the confirmed design in `docs/superpowers/specs/2026-08-09-owner-autopilot-default-design.md`.
- Produces: current documentation consistently states the legacy fallback is `autopilot`; the Owner state file explicitly persists `mode = "autopilot"`.

- [ ] **Step 1: Update current documentation without weakening hard boundaries**

Use these exact semantics in each document:

```text
缺少 tools.mode 的旧配置按 autopilot 加载，与新安装默认值一致；显式 safe/smart 继续保留审批。
Autopilot 只对本地入口和经过验证的 Owner 私聊生效，群聊、其他用户与硬拒绝规则不变。
```

Mark the earlier “legacy defaults to safe” design statement as superseded by the 2026-08-09 confirmed design rather than silently leaving contradictory history.

- [ ] **Step 2: Persist the current Owner choice explicitly**

Patch only the `[tools]` section in `~/.miniclaw/config.toml`:

```toml
[tools]
mode = "autopilot"
security = "allowlist"
ask = "on-miss"
```

Do not read, print, or modify `.env`, credentials, channel secrets, or unrelated state fields.

- [ ] **Step 3: Validate documentation and the effective non-secret mode**

Run:

```bash
uv run python scripts/validate_docs.py
sed -n '/^\[tools\]/,/^\[/p' ~/.miniclaw/config.toml
```

Expected: documentation reports `PASS`; the displayed `[tools]` section contains `mode = "autopilot"` and no secret values.

- [ ] **Step 4: Commit repository documentation only**

```bash
git add README.md docs/product/20260807_产品需求文档.md docs/architecture/20260807_系统架构.md docs/engineering/phase-2/autopilot-permissions-and-approval-ui.md docs/superpowers/specs/2026-08-08-autopilot-permissions-and-approval-ui-design.md
git commit -m "docs(policy): 更新 Owner autopilot 默认语义"
```

### Task 3: 完成发布级验证

**Files:**
- Verify only: all modified repository files and existing Channel scenarios.

**Interfaces:**
- Consumes: Tasks 1-2 committed changes.
- Produces: fresh evidence that unit, static, documentation, and Channel stability gates pass.

- [ ] **Step 1: Run the full Python test suite**

```bash
uv run python -m unittest discover -s tests -v
```

Expected: exit 0 with zero failures and errors.

- [ ] **Step 2: Run Ruff**

```bash
uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 3: Run the versioned Channel gate and 20-round soak**

```bash
uv run miniclaw eval run --suite channel --root evals/scenarios
uv run miniclaw eval run --suite channel --repeat 20 --json --root evals/scenarios
```

Expected: single run passes all versioned Channel cases and soak exits 0 with all checks passing.

- [ ] **Step 4: Re-run docs and repository hygiene checks**

```bash
uv run python scripts/validate_docs.py
git diff --check
git status --short
```

Expected: docs pass, no whitespace errors, only intentional tracked changes/commits plus the pre-existing untracked `docs/assets/`.
