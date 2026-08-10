# Phase 2.1B Workspace Read Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Lobster0 能在配置允许的目录内安全地读取文本、匹配路径和搜索文本，同时阻止绝对路径、`..`、符号链接和敏感文件逃逸。

**Architecture:** 复用已有 `ToolRegistry → PolicyEngine → ToolExecutor → SQLite` 唯一执行链。新增一个标准库实现的 `WorkspaceGuard` 统一解析路径与敏感路径；`read_file`、`glob`、`grep` 在 Policy 预检后，执行前再次使用同一个 Guard，避免任何 Tool 自己发明安全规则。

**Tech Stack:** Python 3.12、`pathlib`、`os.walk`、`re`、标准库 `unittest`、现有 SQLite ToolRun/Audit、Ruff；不新增依赖，不启动外部 `grep` 进程。

## Global Constraints

- 只实现 `read_file`、`glob`、`grep` 和 Workspace 读边界；不实现写文件、Shell、HTTP、审批或 Web 管理页。
- 相对路径以 `workspace.path` 为基准；绝对路径只能位于 Workspace 或 `workspace.read_only_roots`。
- `.env*`、私钥、常见凭据、Lobster0 自身配置/数据库/日志和系统敏感文件必须硬拒绝。
- `Path.resolve(strict=False)` 后重新检查允许根，拒绝目标或父链符号链接逃逸。
- `read_file` 只读 UTF-8 文本，默认 200 行、最大 1000 行、单次最多 512 KiB。
- `glob` 最多返回 200 个按相对路径字典序排列的结果，不跟随目录符号链接。
- `grep` 使用 Python `re`；最多扫描 200 个文件、单文件 1 MiB、总计 20 MiB、返回 100 条结果。
- 所有 Tool 仍只能经 `ToolExecutor` 执行并留下 ToolRun/Audit；直接调用仅用于纯单元测试。
- 所有新增或修改的公共类、函数、方法必须有准确类型标注和中文 docstring。
- 离线测试不得读取真实 Home、真实 `.env`、真实模型或飞书接口。

---

## File Map

| 文件 | 单一职责 |
| --- | --- |
| `src/lobster0/policy/workspace.py` | 解析允许根、阻止路径逃逸、识别敏感路径、生成不泄露 Home 的展示路径 |
| `src/lobster0/policy/engine.py` | 在 ToolRun 创建前对三个文件 Tool 做 Workspace 预检，并返回稳定拒绝码 |
| `src/lobster0/tools/filesystem.py` | `read_file` 参数契约、UTF-8/二进制判断、行窗口和 512 KiB 上限 |
| `src/lobster0/tools/search.py` | 不跟随目录 symlink 的候选遍历，以及 `glob`/`grep` 上限 |
| `src/lobster0/cli.py` | 在产品 Bootstrap 中注册三个新 Tool |
| `tests/test_workspace_policy.py` | 允许根、`..`、绝对路径、symlink、敏感文件和只读根矩阵 |
| `tests/test_file_tools.py` | `read_file` 正常行窗口、二进制、编码、大小和错误码 |
| `tests/test_search_tools.py` | `glob`/`grep` 排序、过滤、上限、正则和二进制行为 |
| `tests/test_turn.py` | 真实 Turn/Executor/Fake Provider 的 `read_file` 两轮 Tool Loop |
| `docs/engineering/phase-2/20260807_workspace-read-tools.md` | 面向学习者的模块、调用链、安全边界、调试和测试说明 |
| `docs/progress/index.html` | 把 P2.1B 标成已验证，并展示下一阶段仍未实现的能力 |

---

### Task 1: WorkspaceGuard 与 Policy 预检

**Files:**
- Create: `src/lobster0/policy/workspace.py`
- Modify: `src/lobster0/policy/engine.py`
- Create: `tests/test_workspace_policy.py`
- Modify: `tests/test_tool_executor.py`

**Interfaces:**
- Consumes: `ToolContext(workspace, read_only_roots, state_home)`、已规范化的 Tool 参数。
- Produces: `WorkspaceGuard.resolve_read(context, raw_path) -> Path`、`WorkspaceGuard.display(context, path, root=None) -> str`、`WorkspaceAccessError(code, message)`、`PolicyDecision.error_code`。

- [ ] **Step 1: 写普通路径、绝对只读根和 `..` 逃逸的失败测试**

```python
def test_resolve_read_allows_workspace_and_read_only_root(self) -> None:
    workspace_file = self.workspace / "notes.txt"
    shared_file = self.read_only / "guide.md"
    workspace_file.write_text("notes", encoding="utf-8")
    shared_file.write_text("guide", encoding="utf-8")

    self.assertEqual(self.guard.resolve_read(self.context, "notes.txt"), workspace_file)
    self.assertEqual(
        self.guard.resolve_read(self.context, str(shared_file)),
        shared_file,
    )

def test_resolve_read_rejects_parent_and_absolute_escape(self) -> None:
    for candidate in ("../outside.txt", str(self.outside / "outside.txt")):
        with self.subTest(candidate=candidate), self.assertRaises(WorkspaceAccessError) as caught:
            self.guard.resolve_read(self.context, candidate)
        self.assertEqual(caught.exception.code, "workspace_escape")
```

- [ ] **Step 2: 运行 Workspace 测试，确认因模块不存在而 RED**

Run: `.venv/bin/python -m unittest tests.test_workspace_policy -v`

Expected: `ModuleNotFoundError: No module named 'lobster0.policy.workspace'`。

- [ ] **Step 3: 用 `Path.resolve(strict=False)` 实现最小允许根解析**

```python
class WorkspaceAccessError(ValueError):
    """表示路径违反 Workspace 读取边界。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkspaceGuard:
    """统一解析模型提供的路径，并把结果限制在配置允许根内。"""

    def resolve_read(self, context: ToolContext, raw_path: str) -> Path:
        """返回允许读取的规范路径；逃逸或敏感路径时抛出稳定异常。"""
        supplied = Path(raw_path)
        candidate = supplied if supplied.is_absolute() else context.workspace / supplied
        resolved = candidate.resolve(strict=False)
        roots = (context.workspace, *context.read_only_roots)
        if not any(_contains(root.resolve(strict=False), resolved) for root in roots):
            raise WorkspaceAccessError(
                "workspace_escape",
                "path is outside the configured workspace",
            )
        return resolved
```

- [ ] **Step 4: 运行 Workspace 测试，确认普通路径 GREEN**

Run: `.venv/bin/python -m unittest tests.test_workspace_policy -v`

Expected: 普通路径和逃逸用例通过。

- [ ] **Step 5: 增加 symlink、敏感路径和 Lobster0 状态文件的失败测试**

```python
def test_symlink_cannot_escape_workspace(self) -> None:
    (self.workspace / "jump").symlink_to(self.outside, target_is_directory=True)
    with self.assertRaises(WorkspaceAccessError) as caught:
        self.guard.resolve_read(self.context, "jump/outside.txt")
    self.assertEqual(caught.exception.code, "workspace_escape")

def test_sensitive_names_are_denied_even_inside_workspace(self) -> None:
    for candidate in (".env", ".env.local", ".ssh/id_ed25519", "credentials.json"):
        with self.subTest(candidate=candidate), self.assertRaises(WorkspaceAccessError) as caught:
            self.guard.resolve_read(self.context, candidate)
        self.assertEqual(caught.exception.code, "sensitive_path")
```

- [ ] **Step 6: 运行新增测试，确认当前实现没有敏感路径判断而 RED**

Run: `.venv/bin/python -m unittest tests.test_workspace_policy -v`

Expected: symlink 逃逸已失败关闭；敏感文件用例仍失败，证明测试命中缺失分支。

- [ ] **Step 7: 在同一个 Guard 中增加大小写归一化敏感路径与安全展示路径**

```python
def display(self, context: ToolContext, path: Path, *, root: Path | None = None) -> str:
    """返回相对允许根的路径，不把本机 Home 目录暴露给模型。"""
    base = (root or context.workspace).resolve(strict=False)
    return path.resolve(strict=False).relative_to(base).as_posix() or "."
```

实现同时覆盖 `.env*`、`.ssh/.aws/.gnupg/.kube/.config/gcloud`、私钥名、凭据名、`state_home/config.toml`、`state_home/lobster0.db`、`state_home/logs`、系统 shadow/sudoers 和容器 socket；逻辑输入与 resolve 后路径各检查一次。

- [ ] **Step 8: 让 Policy 在创建 ToolRun 前返回具体拒绝码**

```python
@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: PolicyAction
    reason: str
    error_code: str = "denied"
```

`PolicyEngine.authorize()` 对 `read_file.path` 以及 `glob/grep.root` 调用 `WorkspaceGuard.resolve_read()`；捕获 `WorkspaceAccessError` 后返回 `DENY + error_code`。`ToolExecutor` 的 deny 分支使用 `decision.error_code`，测试断言逃逸请求返回 `workspace_escape` 且 `tool_runs` 数量仍为 0。

- [ ] **Step 9: 运行相关测试并提交**

Run: `.venv/bin/python -m unittest tests.test_workspace_policy tests.test_tool_executor -v`

Expected: 全部通过。

```bash
git add src/lobster0/policy/workspace.py src/lobster0/policy/engine.py tests/test_workspace_policy.py tests/test_tool_executor.py
git commit -m "feat: enforce workspace read boundaries"
```

---

### Task 2: `read_file` UTF-8 行窗口

**Files:**
- Create: `src/lobster0/tools/filesystem.py`
- Create: `tests/test_file_tools.py`

**Interfaces:**
- Consumes: `WorkspaceGuard.resolve_read()` 与 `ToolContext`。
- Produces: `ReadFileTool.definition`、`validate(arguments) -> {path, offset, limit}`、异步 `execute() -> ToolResult`。

- [ ] **Step 1: 写默认行窗口、显式 offset/limit 与公开 Schema 的失败测试**

```python
async def test_reads_utf8_lines_with_one_based_offset(self) -> None:
    (self.workspace / "notes.txt").write_text("一\n二\n三\n四\n", encoding="utf-8")
    result = await ReadFileTool().execute(
        self.context,
        ReadFileTool().validate({"path": "notes.txt", "offset": 2, "limit": 2}),
    )
    self.assertTrue(result.ok)
    self.assertEqual(
        result.data,
        {
            "path": "notes.txt",
            "content": "二\n三\n",
            "offset": 2,
            "lines": 2,
            "truncated": True,
            "next_offset": 4,
        },
    )
```

- [ ] **Step 2: 运行文件 Tool 测试，确认因 `ReadFileTool` 不存在而 RED**

Run: `.venv/bin/python -m unittest tests.test_file_tools -v`

Expected: import failure 指向 `lobster0.tools.filesystem`。

- [ ] **Step 3: 实现严格参数校验和最小 UTF-8 读取**

```python
definition = ToolDefinition(
    name="read_file",
    description="Read a UTF-8 text file inside the configured workspace.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    risk=ToolRisk.LOW,
)
```

执行时最多读取 512 KiB 前缀，拒绝 NUL 和非法 UTF-8，按 `splitlines(keepends=True)` 生成一开始的行号窗口；所有 `OSError` 只映射成稳定码，不把绝对路径或系统错误原文交给模型。

- [ ] **Step 4: 运行正常读取测试，确认 GREEN**

Run: `.venv/bin/python -m unittest tests.test_file_tools.ReadFileToolTest.test_reads_utf8_lines_with_one_based_offset -v`

Expected: PASS。

- [ ] **Step 5: 写二进制、非法 UTF-8、超上限参数、目录和不存在文件测试**

```python
async def test_binary_and_invalid_utf8_are_rejected(self) -> None:
    for name, content in (("nul.bin", b"a\x00b"), ("bad.txt", b"\xff")):
        (self.workspace / name).write_bytes(content)
        with self.subTest(name=name):
            result = await self._run({"path": name})
            self.assertEqual(result.error_code, "binary_file")

def test_validate_rejects_booleans_unknown_keys_and_large_limit(self) -> None:
    for arguments in (
        {"path": "a", "offset": True},
        {"path": "a", "limit": 1001},
        {"path": "a", "command": "cat"},
    ):
        with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError):
            ReadFileTool().validate(arguments)
```

- [ ] **Step 6: 运行用例确认 RED，再补齐稳定错误和 512 KiB 截断元数据**

Run: `.venv/bin/python -m unittest tests.test_file_tools -v`

Expected RED: 尚未实现的 `binary_file`、`not_found`、`not_a_file` 或截断分支失败。

实现后 Expected GREEN: 全部通过；结果不得包含测试临时目录的绝对路径。

- [ ] **Step 7: 运行相关检查并提交**

Run: `.venv/bin/python -m unittest tests.test_workspace_policy tests.test_file_tools tests.test_tool_executor -v`

Expected: 全部通过。

```bash
git add src/lobster0/tools/filesystem.py tests/test_file_tools.py
git commit -m "feat: add bounded read file tool"
```

---

### Task 3: `glob` 与 `grep` 安全搜索

**Files:**
- Create: `src/lobster0/tools/search.py`
- Create: `tests/test_search_tools.py`

**Interfaces:**
- Consumes: `WorkspaceGuard`、`ToolContext`、标准库 `os.walk`、`pathlib.PurePath.match`、`re`。
- Produces: `GlobTool` 与 `GrepTool`；两者共享一个不跟随目录 symlink 的模块内候选遍历函数。

- [ ] **Step 1: 写 glob 排序、相对路径、敏感文件过滤和 symlink 不跟随测试**

```python
async def test_glob_returns_sorted_safe_relative_paths(self) -> None:
    (self.workspace / "b.py").write_text("", encoding="utf-8")
    (self.workspace / "a.py").write_text("", encoding="utf-8")
    (self.workspace / ".env").write_text("SECRET=x", encoding="utf-8")
    (self.workspace / "jump").symlink_to(self.outside, target_is_directory=True)

    result = await self._glob({"pattern": "**/*", "limit": 20})

    self.assertEqual(result.data["matches"], ["a.py", "b.py"])
    self.assertNotIn(".env", repr(result.data))
    self.assertNotIn("jump/hidden.py", repr(result.data))
```

- [ ] **Step 2: 运行搜索测试，确认 import RED**

Run: `.venv/bin/python -m unittest tests.test_search_tools -v`

Expected: `GlobTool`/`GrepTool` 尚不存在。

- [ ] **Step 3: 实现 `os.walk(..., followlinks=False)` 候选遍历和 GlobTool**

`pattern` 必须是非空相对 glob；`root` 默认 `.`；`limit` 默认且最大为 200。遍历时原地移除 symlink 目录，候选再次通过 `WorkspaceGuard`，敏感路径静默过滤；收集 `limit + 1` 个安全结果后停止并设置 `truncated`。

- [ ] **Step 4: 运行 glob 用例，确认 GREEN**

Run: `.venv/bin/python -m unittest tests.test_search_tools.GlobToolTest -v`

Expected: 排序、上限、敏感过滤、逃逸和参数矩阵全部通过。

- [ ] **Step 5: 写 grep 正常匹配、非法正则、二进制/大文件跳过和总量上限测试**

```python
async def test_grep_returns_path_line_number_and_bounded_text(self) -> None:
    (self.workspace / "agent.py").write_text(
        "class AgentRunner:\n    pass\n",
        encoding="utf-8",
    )
    result = await self._grep({"pattern": "AgentRunner", "glob": "**/*.py"})
    self.assertEqual(
        result.data["matches"],
        [{"path": "agent.py", "line": 1, "text": "class AgentRunner:"}],
    )

async def test_invalid_regex_has_stable_error_code(self) -> None:
    result = await self._grep({"pattern": "["})
    self.assertFalse(result.ok)
    self.assertEqual(result.error_code, "invalid_pattern")
```

- [ ] **Step 6: 运行 grep 用例确认 RED，再实现最小扫描循环**

Run: `.venv/bin/python -m unittest tests.test_search_tools.GrepToolTest -v`

Expected RED: `grep` 执行分支尚未实现。

实现约束：最多选择 200 个普通文件；用 `read_bytes()` 的有界等价实现拒绝超过 1 MiB 的文件；累计读取达到 20 MiB 后停止；NUL/非法 UTF-8 跳过；每行只产生一个结果；展示文本去掉换行并截到 500 字符；达到 `limit` 后停止并设置 `truncated`。

- [ ] **Step 7: 运行搜索与 Workspace 测试并提交**

Run: `.venv/bin/python -m unittest tests.test_workspace_policy tests.test_search_tools -v`

Expected: 全部通过。

```bash
git add src/lobster0/tools/search.py tests/test_search_tools.py
git commit -m "feat: add safe workspace search tools"
```

---

### Task 4: 产品 Bootstrap 与真实 Turn Tool Loop

**Files:**
- Modify: `src/lobster0/cli.py`
- Modify: `tests/test_turn.py`
- Modify: `tests/test_tool_contract.py`

**Interfaces:**
- Consumes: `ReadFileTool()`、`GlobTool()`、`GrepTool()` 与已有 `SystemInfoTool()`。
- Produces: CLI 产品运行时向模型暴露按名称稳定排序的四个 Tool Schema。

- [ ] **Step 1: 写 Registry schema 和 `read_file` 两轮 Fake Provider 集成测试**

```python
def test_builtin_registry_exposes_four_tools_in_stable_order(self) -> None:
    registry = ToolRegistry((SystemInfoTool(), ReadFileTool(), GlobTool(), GrepTool()))
    names = [schema["function"]["name"] for schema in registry.schemas]
    self.assertEqual(names, ["glob", "grep", "read_file", "system_info"])
```

Turn 测试在临时 Workspace 写入 `README.md`：Fake Provider 第一轮返回 `read_file` call，第二轮断言收到合法 Tool Message 后返回总结；最终断言 Turn completed、ToolRun succeeded、Audit 包含 `tool.started/tool.succeeded`。

- [ ] **Step 2: 运行集成测试，确认 CLI 仍只注册 system_info 而 RED**

Run: `.venv/bin/python -m unittest tests.test_tool_contract tests.test_turn -v`

Expected: 内置 schema 数量或 `read_file` Tool Loop 失败。

- [ ] **Step 3: 在 `_chat()` 的唯一 Registry 组装点注册三个 Tool**

```python
registry = ToolRegistry(
    (
        SystemInfoTool(),
        ReadFileTool(),
        GlobTool(),
        GrepTool(),
    )
)
```

不创建 ToolFactory、不增加配置开关，也不复制第二套 Bootstrap。

- [ ] **Step 4: 运行集成与全量测试并提交**

Run: `.venv/bin/python -m unittest tests.test_tool_contract tests.test_turn tests.test_agent_runner -v`

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: 所有测试通过，且原 99 个测试无回归。

```bash
git add src/lobster0/cli.py tests/test_tool_contract.py tests/test_turn.py
git commit -m "feat: expose workspace read tools to the agent"
```

---

### Task 5: 学习文档与可点击进度页

**Files:**
- Create: `docs/engineering/phase-2/20260807_workspace-read-tools.md`
- Modify: `docs/engineering/README.md`
- Modify: `README.md`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Consumes: 已验证的真实实现、测试命令和已知限制。
- Produces: 一份大白话工程说明和一个能直接打开的阶段状态页。

- [ ] **Step 1: 写工程文档**

文档必须包含：本阶段范围/非范围、四层架构图、一次 `read_file` 时序图、Workspace Guard 判断流程图、三个 Tool 参数/返回/错误表、每个源文件职责、为何不使用 Shell `cat/find/grep`、本地调试命令、测试矩阵、常见报错、TOCTOU 与 Python regex 的已知边界、P2.2 接口衔接。

- [ ] **Step 2: 更新 README 和工程文档索引**

README 只把已实现的 `system_info/read_file/glob/grep` 标成可用，并给出 Workspace 内演示命令；写文件、Shell、飞书仍明确标注未实现。

- [ ] **Step 3: 更新进度 HTML**

把 P2.1B 标成“已验证”，列出实际测试数量、相关提交和下一阶段 P2.2；不得把真实 DeepSeek 文件调用、写入或审批描述成已完成。

- [ ] **Step 4: 检查 Mermaid、链接和文档事实后提交**

Run: `rg -n "loblet|已实现.*write_file|已实现.*run_command" README.md docs --glob '!docs/superpowers/plans/**'`

Expected: 无旧产品名或把未来功能写成已完成的命中；另人工确认新增文档没有占位段落。

```bash
git add README.md docs/engineering/README.md docs/engineering/phase-2/20260807_workspace-read-tools.md docs/progress/index.html
git commit -m "docs: explain workspace read tools"
```

---

### Task 6: 完成验证、审查与 main 集成

**Files:**
- Verify: all changed source, tests, Markdown and HTML files

**Interfaces:**
- Consumes: Tasks 1–5 的 feature branch。
- Produces: 可审查、可合并、已推送的 `main`。

- [ ] **Step 1: 运行完成门禁**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Run: `.venv/bin/ruff check .`

Run: `git diff --check main...HEAD`

Run: `git status --short`

Expected: 全量测试 0 failure/error、Ruff clean、无空白错误、只有当前阶段预期改动。

- [ ] **Step 2: 做本地无模型冒烟**

在临时 Workspace 通过真实 Registry/Policy/Executor 依次执行 `read_file`、`glob`、`grep`，断言成功结果和 ToolRun/Audit；再请求 `../outside.txt` 与 `.env`，断言失败且没有读取内容。冒烟不得接触真实 Home 或外部模型。

- [ ] **Step 3: 请求只读代码审查并修复所有有效问题**

审查范围：`main...HEAD`；重点看路径规范化、symlink、敏感文件、读取上限、异常脱敏、ToolRun 创建时机、测试是否真正命中安全分支。任何修复先补能复现的失败测试，再做最小实现。

- [ ] **Step 4: 审查修复后重跑完整门禁**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Run: `.venv/bin/ruff check .`

Run: `git diff --check main...HEAD`

Expected: 全部通过。

- [ ] **Step 5: 合并并推送 main**

```bash
git checkout main
git merge --ff-only phase-2-1b-workspace-read-tools
git push origin main
```

- [ ] **Step 6: 在合并后的 main 再验证并清理已合并 worktree**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Run: `.venv/bin/ruff check .`

Expected: 合并后结果与 feature branch 一致；确认 `main` 与 `origin/main` 同步后，移除已合并 worktree 和本地 feature branch。
