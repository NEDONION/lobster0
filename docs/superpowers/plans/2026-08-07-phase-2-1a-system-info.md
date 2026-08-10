# Lobster0 Phase 2.1A System Info Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Lobster0 在真实 CLI 对话中向模型暴露 `system_info`，经过统一 Policy 和 ToolExecutor 执行，保存 ToolRun 与完整消息轨迹，再基于真实、脱敏的本机信息回答。

**Architecture:** 保留现有 Provider、AgentRunner、TurnService 和 SQLite。新增最小 Tool Contract、Registry、低风险 Policy、ToolExecutor、ToolRun Repository 与 `SystemInfoTool`；AgentRunner 只通过 ToolExecutor 执行真实工具，TurnService 为每轮提供 ToolContext，并把中间 Assistant Tool Call 与 Tool Result 和最终回答一起持久化。

**Tech Stack:** Python 3.12、标准库 `asyncio/platform/subprocess/shutil/sqlite3/json/hashlib`、现有 `httpx` Provider、标准库 `unittest`、Ruff、SQLite v1 Schema。

## Global Constraints

- Python 最低版本 3.12，继续使用 `src` layout、`uv`、标准库 `unittest` 和 Ruff line length 100。
- 不新增第三方依赖；优先复用现有 `JsonValue`、`ModelRequest`、`ToolCall`、SQLite Schema 与 Repository 模式。
- 所有新增或修改的顶层类、函数和方法都提供中文 docstring 与准确类型标注。
- 生产代码之前必须先写会因目标行为缺失而失败的测试，并实际观察 RED。
- Agent 只能通过一个 ToolExecutor 执行 Tool；Tool 不读取 Provider Key、Channel SDK 或完整环境变量。
- `system_info` 只执行代码中固定的系统查询，不接收程序名或命令参数。
- 返回值不得包含序列号、Hardware UUID、hostname、username、MAC、IP 或环境变量。
- SQLite 继续使用现有 v1 表；除非 RED 测试证明无法表达目标，否则不新增 migration。
- 本计划只交付 P2.1A `system_info` 纵切；`read_file`、`glob`、`grep` 放入 P2.1B，写入/审批放入 P2.2。
- 不直接复制上游代码；如实现时发生实质性复制，必须同步文件头与 `THIRD_PARTY_NOTICES.md`。

---

## File Map

| 文件 | 职责 |
| --- | --- |
| `src/lobster0/tools/base.py` | ToolDefinition、ToolContext、ToolResult、ValidationError 与 Tool Protocol |
| `src/lobster0/tools/registry.py` | 唯一名称注册、查询和稳定 OpenAI Tool Schema |
| `src/lobster0/policy/engine.py` | low/medium/high 风险映射为 allow/require_approval/deny 的单一决策点 |
| `src/lobster0/storage/tooling.py` | ToolRun 开始、成功、失败、中断及脱敏 Audit 的事务写入 |
| `src/lobster0/tools/executor.py` | 参数校验、Policy、Repository、Tool 调用与稳定 JSON 结果的唯一入口 |
| `src/lobster0/tools/system.py` | macOS/Linux/通用平台的脱敏 System Info 收集 |
| `src/lobster0/agent/runner.py` | 使用 ToolExecutor，并返回中间 Tool 消息轨迹 |
| `src/lobster0/agent/context.py` | 把 Registry Schema 放入 ModelRequest，并补充工具使用提示 |
| `src/lobster0/agent/turn.py` | 构造 ToolContext、恢复历史 Tool Call、传递并持久化中间消息 |
| `src/lobster0/storage/conversations.py` | 同一事务保存中间 Assistant/Tool 与最终 Assistant Message |
| `src/lobster0/cli.py` | 组装 Registry、Policy、Repository、Executor 和 SystemInfoTool |
| `tests/test_tool_contract.py` | Tool Result JSON 与 Registry 契约 |
| `tests/test_system_info.py` | 字段、平台 fallback、固定命令和隐私过滤 |
| `tests/test_tool_executor.py` | Policy、ToolRun、Audit、错误与取消 |
| `tests/test_agent_runner.py` | Runner 通过 Executor 完成模型—工具—模型循环 |
| `tests/test_context.py` | Tool Schema 被放进 ModelRequest |
| `tests/test_conversations.py` | 中间消息和 Tool Call metadata 事务持久化 |
| `tests/test_turn.py` | 完整 Turn 持久化 ToolRun 与可恢复消息历史 |
| `docs/engineering/phase-2/20260807_tool-runtime-and-system-info.md` | 已实现行为、边界、调试与测试说明 |

---

### Task 1: Tool Contract 与 Registry

**Files:**
- Create: `src/lobster0/tools/__init__.py`
- Create: `src/lobster0/tools/base.py`
- Create: `src/lobster0/tools/registry.py`
- Test: `tests/test_tool_contract.py`

**Interfaces:**
- Consumes: `lobster0.providers.base.JsonValue`。
- Produces: `ToolRisk`、`ToolDefinition.to_model_schema()`、`ToolContext`、`ToolResult.to_model_text()`、`ToolValidationError`、`Tool`、`ToolRegistry.get()`、`ToolRegistry.schemas`。

- [ ] **Step 1: 写 Registry 稳定 Schema 与重复名称的失败测试**

```python
class _EchoTool:
    definition = ToolDefinition(
        name="echo",
        description="Echo one text value.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return arguments

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        return ToolResult.success(arguments)


class ToolContractTest(unittest.TestCase):
    def test_registry_emits_stable_openai_schema_and_rejects_duplicate_names(self) -> None:
        registry = ToolRegistry((_EchoTool(),))

        self.assertEqual(registry.get("echo").definition.name, "echo")
        self.assertEqual(
            registry.schemas,
            (
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "Echo one text value.",
                        "parameters": _EchoTool.definition.parameters,
                    },
                },
            ),
        )
        with self.assertRaisesRegex(ValueError, "duplicate tool name: echo"):
            ToolRegistry((_EchoTool(), _EchoTool()))
```

- [ ] **Step 2: 运行测试并确认 RED 是 tools package 不存在**

Run: `uv run python -m unittest tests.test_tool_contract.ToolContractTest.test_registry_emits_stable_openai_schema_and_rejects_duplicate_names -v`

Expected: `ModuleNotFoundError: No module named 'lobster0.tools'`。

- [ ] **Step 3: 实现最小 Contract 与 Registry**

```python
class ToolRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, JsonValue]
    risk: ToolRisk

    def to_model_schema(self) -> dict[str, JsonValue]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolContext:
    user_id: int
    session_id: int
    turn_id: int
    state_home: Path
    workspace: Path
    read_only_roots: tuple[Path, ...]


class ToolValidationError(ValueError):
    """表示模型提供的 Tool 参数不符合公开 Schema。"""


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    data: JsonValue = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    @classmethod
    def success(cls, data: JsonValue) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> "ToolResult":
        return cls(
            ok=False,
            error_code=code,
            error_message=message,
            retryable=retryable,
        )

    def to_model_text(self, tool_name: str) -> str:
        body: dict[str, JsonValue] = {"ok": self.ok, "tool": tool_name}
        if self.ok:
            body["data"] = self.data
        else:
            body["error"] = {
                "code": self.error_code,
                "message": self.error_message,
                "retryable": self.retryable,
            }
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Tool(Protocol):
    definition: ToolDefinition

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验并返回供 Policy 与执行共用的规范参数。"""
        ...

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """在 Policy 已放行后执行一个工具动作。"""
        ...
```

`ToolRegistry` 使用普通 `dict[str, Tool]`，不增加 PluginManager、动态加载或 Factory：

```python
class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.definition.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.definition.name}")
            self._tools[tool.definition.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def schemas(self) -> tuple[dict[str, JsonValue], ...]:
        return tuple(
            self._tools[name].definition.to_model_schema()
            for name in sorted(self._tools)
        )
```

- [ ] **Step 4: 增加 Tool Result 成功/失败 JSON 测试并运行整个文件**

```python
def test_tool_result_uses_stable_model_json_without_traceback(self) -> None:
    success = json.loads(ToolResult.success({"value": 1}).to_model_text("echo"))
    failure = json.loads(
        ToolResult.failure("invalid_arguments", "text is required").to_model_text("echo")
    )

    self.assertEqual(success, {"ok": True, "tool": "echo", "data": {"value": 1}})
    self.assertEqual(
        failure,
        {
            "ok": False,
            "tool": "echo",
            "error": {
                "code": "invalid_arguments",
                "message": "text is required",
                "retryable": False,
            },
        },
    )
```

Run: `uv run python -m unittest tests.test_tool_contract -v`

Expected: 2 tests PASS。

- [ ] **Step 5: Ruff 并提交**

```bash
uv run ruff check src/lobster0/tools tests/test_tool_contract.py
git add src/lobster0/tools tests/test_tool_contract.py
git commit -m "feat: add tool contract and registry"
```

---

### Task 2: `system_info` 脱敏收集器

**Files:**
- Create: `src/lobster0/tools/system.py`
- Test: `tests/test_system_info.py`

**Interfaces:**
- Consumes: Task 1 的 `ToolContext`、`ToolDefinition`、`ToolResult`、`ToolRisk`、`ToolValidationError`。
- Produces: `SystemInfoTool.validate(arguments)` 与 `SystemInfoTool.execute(context, arguments)`。

- [ ] **Step 1: 写跨平台稳定字段和隐私排除的失败测试**

```python
class SystemInfoToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_whitelisted_sections_without_device_identifiers(self) -> None:
        context = ToolContext(
            user_id=1,
            session_id=2,
            turn_id=3,
            state_home=Path("/state"),
            workspace=Path("/workspace"),
            read_only_roots=(),
        )
        with (
            mock.patch("lobster0.tools.system.platform.system", return_value="Darwin"),
            mock.patch("lobster0.tools.system.platform.mac_ver", return_value=(("15.1"), ("", "", ""), "arm64")),
            mock.patch("lobster0.tools.system.platform.machine", return_value="arm64"),
            mock.patch("lobster0.tools.system.os.cpu_count", return_value=10),
            mock.patch(
                "lobster0.tools.system._mac_hardware",
                return_value={
                    "chip": "Apple M4",
                    "memory_bytes": 17179869184,
                    "gpus": ["Apple M4"],
                },
            ),
            mock.patch(
                "lobster0.tools.system.shutil.disk_usage",
                return_value=SimpleNamespace(total=1000, used=400, free=600),
            ),
        ):
            result = await SystemInfoTool().execute(context, {})

        self.assertTrue(result.ok)
        assert isinstance(result.data, dict)
        self.assertEqual(result.data["cpu"]["model"], "Apple M4")
        self.assertEqual(result.data["memory"]["total_bytes"], 17179869184)
        serialized = json.dumps(result.data)
        for forbidden in ("serial", "uuid", "hostname", "username", "mac_address", "environment"):
            self.assertNotIn(forbidden, serialized.lower())
```

- [ ] **Step 2: 运行并确认 RED 是 SystemInfoTool 不存在**

Run: `uv run python -m unittest tests.test_system_info.SystemInfoToolTest.test_returns_whitelisted_sections_without_device_identifiers -v`

Expected: import failure for `lobster0.tools.system`。

- [ ] **Step 3: 实现参数白名单与通用收集路径**

`SystemInfoTool.definition` 只声明 `sections = [os, cpu, memory, storage, gpu]`。`validate` 接受缺省参数并返回全部 section；拒绝未知键、非字符串 list、空 list 和未知 section。

```python
_SECTIONS = ("os", "cpu", "memory", "storage", "gpu")


def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if set(arguments) - {"sections"}:
        raise ToolValidationError("unexpected system_info argument")
    raw = arguments.get("sections")
    if raw is None:
        return {"sections": list(_SECTIONS)}
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise ToolValidationError("sections must be a non-empty string array")
    sections = list(dict.fromkeys(raw))
    if any(section not in _SECTIONS for section in sections):
        raise ToolValidationError("sections contains an unsupported value")
    return {"sections": sections}
```

`execute` 使用 `await asyncio.to_thread(_collect, sections)`，避免固定系统命令阻塞 event loop。通用字段只来自字段白名单：

```python
result: dict[str, JsonValue] = {
    "os": {
        "name": "macOS" if system == "Darwin" else system,
        "version": version,
        "architecture": platform.machine() or "unknown",
    },
    "cpu": {
        "model": cpu_model or "unknown",
        "logical_cores": os.cpu_count(),
    },
    "memory": {"total_bytes": memory_bytes},
    "storage": [
        {
            "mount": "/",
            "total_bytes": usage.total,
            "free_bytes": usage.free,
        }
    ],
    "gpu": [{"model": model} for model in gpu_models],
    "unavailable_sections": unavailable,
}
```

- [ ] **Step 4: 实现固定 macOS 查询和 Linux fallback**

macOS 仅运行以下固定 argv，模型参数绝不进入命令：

```python
subprocess.run(
    ["/usr/sbin/system_profiler", "SPHardwareDataType", "SPDisplaysDataType", "-json"],
    check=False,
    capture_output=True,
    text=True,
    timeout=5,
)
subprocess.run(
    ["/usr/sbin/sysctl", "-n", "hw.memsize"],
    check=False,
    capture_output=True,
    text=True,
    timeout=5,
)
```

从 `system_profiler` JSON 只提取 `chip_type` 和 `sppci_model`；明确忽略 `serial_number`、`platform_UUID`、`provisioning_UDID`。Linux CPU 只读 `/proc/cpuinfo` 的第一个 `model name`，内存优先使用 `os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")`。任何单节失败都加入 `unavailable_sections`，不让整个 Tool 失败。

- [ ] **Step 5: 写非法参数、命令超时和部分字段失败测试**

```python
def test_validate_rejects_unknown_sections_and_arguments(self) -> None:
    tool = SystemInfoTool()
    with self.assertRaises(ToolValidationError):
        tool.validate({"sections": ["serial"]})
    with self.assertRaises(ToolValidationError):
        tool.validate({"command": "env"})


async def test_platform_collector_failure_marks_section_unavailable(self) -> None:
    with mock.patch("lobster0.tools.system._mac_hardware", side_effect=OSError("blocked")):
        result = await SystemInfoTool().execute(self.context, {"sections": ["cpu", "gpu"]})
    self.assertTrue(result.ok)
    assert isinstance(result.data, dict)
    self.assertIn("gpu", result.data["unavailable_sections"])
```

Run: `uv run python -m unittest tests.test_system_info -v`

Expected: all SystemInfo tests PASS；测试输出不得包含本机实际序列号或环境变量。

- [ ] **Step 6: Ruff 并提交**

```bash
uv run ruff check src/lobster0/tools/system.py tests/test_system_info.py
git add src/lobster0/tools/system.py tests/test_system_info.py
git commit -m "feat: add privacy-safe system info tool"
```

---

### Task 3: Policy、ToolRun Repository 与唯一 ToolExecutor

**Files:**
- Create: `src/lobster0/policy/__init__.py`
- Create: `src/lobster0/policy/engine.py`
- Create: `src/lobster0/storage/tooling.py`
- Create: `src/lobster0/tools/executor.py`
- Test: `tests/test_tool_executor.py`

**Interfaces:**
- Consumes: Task 1 Contract/Registry、现有 `Database` 与 v1 `tool_runs/audit_events`。
- Produces: `PolicyAction`、`PolicyDecision`、`PolicyEngine.authorize()`、`ToolRunRepository`、`ToolExecutor.schemas`、`ToolExecutor.execute(context, call)`。

- [ ] **Step 1: 写 low-risk Tool 经过 Policy 执行并保存 ToolRun/Audit 的失败测试**

```python
async def test_low_risk_tool_executes_and_persists_succeeded_run(self) -> None:
    call = ToolCall("call_1", "echo", {"text": "hello"})
    executor = self.executor(_EchoTool())

    model_text = await executor.execute(self.context, call)

    self.assertEqual(json.loads(model_text)["data"], {"text": "hello"})
    with self.database.connect_read_only() as connection:
        run = connection.execute("SELECT * FROM tool_runs").fetchone()
        events = connection.execute(
            "SELECT event_type FROM audit_events ORDER BY id"
        ).fetchall()
    self.assertEqual(run["status"], "succeeded")
    self.assertEqual(run["policy_action"], "allow")
    self.assertEqual(run["tool_name"], "echo")
    self.assertEqual([row[0] for row in events], ["tool.started", "tool.succeeded"])
```

测试 setup 使用真实 `initialize_state`、真实 Turn/Session，确保 `turn_id` 外键有效。

- [ ] **Step 2: 运行并确认 RED 是 Policy/Executor 模块不存在**

Run: `uv run python -m unittest tests.test_tool_executor.ToolExecutorTest.test_low_risk_tool_executes_and_persists_succeeded_run -v`

Expected: import failure for `lobster0.policy` 或 `lobster0.tools.executor`。

- [ ] **Step 3: 实现最小 PolicyEngine**

```python
class PolicyAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: PolicyAction
    reason: str


class PolicyEngine:
    def authorize(
        self,
        definition: ToolDefinition,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> PolicyDecision:
        if definition.risk is ToolRisk.LOW:
            return PolicyDecision(PolicyAction.ALLOW, "built_in_read_only")
        if definition.risk is ToolRisk.CRITICAL:
            return PolicyDecision(PolicyAction.DENY, "critical_action")
        return PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required")
```

P2.1A 只注册 low-risk SystemInfoTool；`REQUIRE_APPROVAL` 先返回稳定 Tool Result，不创建假审批。真正等待、消费和续执行由 P2.2 实现。

- [ ] **Step 4: 实现 ToolRunRepository 的原子状态写入**

`start` 规范化 JSON 并计算：

```python
arguments_json = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
arguments_hash = hashlib.sha256(
    f"{call.name}\n{arguments_json}".encode("utf-8")
).hexdigest()
```

同一事务插入 `tool_runs(status='running', policy_action='allow')` 和
`audit_events(event_type='tool.started')`。`succeed`/`fail`/`interrupt` 使用
`WHERE id = ? AND status = 'running'`，rowcount 非 1 抛出 `ToolStateError`。结果预览最多 2,000 字符，Audit metadata 只保存 ToolRun ID、Tool 名、arguments hash 前 12 位和 error code。

- [ ] **Step 5: 实现 ToolExecutor**

执行顺序固定为：get → validate → policy → start → execute → finish。

```python
class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        runs: ToolRunRepository,
        *,
        result_max_chars: int = 20_000,
    ) -> None:
        if type(result_max_chars) is not int or result_max_chars <= 0:
            raise ValueError("result_max_chars must be a positive integer")
        self._registry = registry
        self._policy = policy
        self._runs = runs
        self._result_max_chars = result_max_chars

    @property
    def schemas(self) -> tuple[dict[str, JsonValue], ...]:
        return self._registry.schemas

    async def execute(self, context: ToolContext, call: ToolCall) -> str:
        tool = self._registry.get(call.name)
        if tool is None:
            return ToolResult.failure(
                "tool_not_found",
                f"tool is not available: {call.name}",
            ).to_model_text(call.name)
        try:
            arguments = tool.validate(call.arguments)
        except ToolValidationError as error:
            return ToolResult.failure("invalid_arguments", str(error)).to_model_text(call.name)
        decision = self._policy.authorize(tool.definition, context, arguments)
        if decision.action is not PolicyAction.ALLOW:
            code = (
                "approval_required"
                if decision.action is PolicyAction.REQUIRE_APPROVAL
                else "denied"
            )
            return ToolResult.failure(code, decision.reason).to_model_text(call.name)

        run_id = self._runs.start(context, call, arguments, decision)
        started = time.monotonic()
        try:
            result = await tool.execute(context, arguments)
        except asyncio.CancelledError:
            self._runs.interrupt(run_id, _elapsed_ms(started))
            raise
        except Exception:
            result = ToolResult.failure("tool_failed", "tool execution failed")
        model_text = result.to_model_text(call.name)
        if len(model_text) > self._result_max_chars:
            result = ToolResult.failure(
                "tool_result_too_large",
                "tool result exceeded the configured size limit",
            )
            model_text = result.to_model_text(call.name)
        if result.ok:
            self._runs.succeed(run_id, model_text, _elapsed_ms(started))
        else:
            self._runs.fail(run_id, model_text, _elapsed_ms(started), result.error_code)
        return model_text
```

不把捕获异常文本或 traceback 放入模型结果和 Audit。

- [ ] **Step 6: 增加 validation、unknown、异常和 CancelledError 测试**

```python
async def test_unexpected_tool_error_is_redacted_and_persisted(self) -> None:
    result = await self.executor(_BrokenTool()).execute(
        self.context,
        ToolCall("call_broken", "broken", {}),
    )
    self.assertEqual(json.loads(result)["error"]["code"], "tool_failed")
    self.assertNotIn("private-test-value", result)


async def test_cancel_marks_tool_run_interrupted_and_propagates(self) -> None:
    with self.assertRaises(asyncio.CancelledError):
        await self.executor(_CancelledTool()).execute(
            self.context,
            ToolCall("call_cancel", "cancel", {}),
        )
    with self.database.connect_read_only() as connection:
        status = connection.execute("SELECT status FROM tool_runs").fetchone()[0]
    self.assertEqual(status, "interrupted")


async def test_oversized_result_becomes_bounded_failure(self) -> None:
    result = await self.executor(_EchoTool(), result_max_chars=200).execute(
        self.context,
        ToolCall("call_large", "echo", {"text": "x" * 1000}),
    )
    self.assertLessEqual(len(result), 200)
    self.assertEqual(json.loads(result)["error"]["code"], "tool_result_too_large")
```

Run: `uv run python -m unittest tests.test_tool_executor -v`

Expected: Policy、持久化、错误和取消测试全部 PASS。

- [ ] **Step 7: Ruff 并提交**

```bash
uv run ruff check src/lobster0/policy src/lobster0/storage/tooling.py src/lobster0/tools/executor.py tests/test_tool_executor.py
git add src/lobster0/policy src/lobster0/storage/tooling.py src/lobster0/tools/executor.py tests/test_tool_executor.py
git commit -m "feat: enforce policy for persisted tool execution"
```

---

### Task 4: AgentRunner ToolExecutor 集成与中间消息轨迹

**Files:**
- Modify: `src/lobster0/agent/runner.py`
- Modify: `tests/test_agent_runner.py`

**Interfaces:**
- Consumes: Task 3 `ToolExecutor.execute()` 与 `ToolExecutor.schemas`。
- Produces: `AgentRunner.tool_schemas`、`AgentRunner.run(..., tool_context=...)`、`AgentRunResult.intermediate_messages`。

- [ ] **Step 1: 把现有 echo handler 测试改成真实 Registry/Executor 并先观察 RED**

测试使用真实 `_EchoTool`、PolicyEngine、临时数据库和 ToolRunRepository。核心断言：

```python
result = await AgentRunner(provider, executor).run(
    request(*executor.schemas),
    tool_context=context,
)

self.assertEqual(result.content, "done")
self.assertEqual(
    [(message.role, message.tool_call_id) for message in result.intermediate_messages],
    [("assistant", None), ("tool", "call_1")],
)
self.assertEqual(json.loads(result.intermediate_messages[-1].content)["data"], {"text": "hello"})
```

- [ ] **Step 2: 运行并确认 RED 是新 constructor/run/result 字段不存在**

Run: `uv run python -m unittest tests.test_agent_runner.AgentRunnerTest.test_tool_result_continues_with_reasoning_and_aggregates_usage -v`

Expected: `TypeError` 或 `AttributeError` 指向尚未实现的 executor/tool_context/intermediate_messages。

- [ ] **Step 3: 最小修改 AgentRunner**

- constructor 从 `tools: Mapping[str, ToolHandler]` 改为 `executor: ToolExecutor | None`；
- 新增只读属性 `tool_schemas`，无 executor 时返回 `()`；
- `run` 新增 keyword-only `tool_context: ToolContext | None = None`；
- 有 executor 但没有 ToolContext 且模型请求 Tool 时抛 `AgentError("tool context is required")`；
- 无 executor 时保持当前 `tool_not_found` 后备，保护 Phase 1 单元测试；
- 每轮把 `_assistant_tool_message` 与每个 Tool Message 同时加入 `messages` 和 `intermediate_messages`；
- 最终 `AgentRunResult` 返回 immutable tuple。

```python
@dataclass(frozen=True, slots=True)
class AgentRunResult:
    content: str
    iterations: int
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None
    finish_reason: str
    intermediate_messages: tuple[ModelMessage, ...] = ()
```

- [ ] **Step 4: 更新未知 Tool、循环上限和取消测试**

未知 Tool 仍返回结构化结果；第 8 次 Tool Call 不执行；`CancelledError` 从 Executor 继续传播。删除旧 `Mapping[str, ToolHandler]` 测试辅助，不保留双 API。

Run: `uv run python -m unittest tests.test_agent_runner -v`

Expected: AgentRunner 全部测试 PASS。

- [ ] **Step 5: Ruff 并提交**

```bash
uv run ruff check src/lobster0/agent/runner.py tests/test_agent_runner.py
git add src/lobster0/agent/runner.py tests/test_agent_runner.py
git commit -m "feat: route agent tool calls through executor"
```

---

### Task 5: Context、Turn 与消息事务集成

**Files:**
- Modify: `src/lobster0/agent/context.py`
- Modify: `src/lobster0/agent/turn.py`
- Modify: `src/lobster0/storage/conversations.py`
- Modify: `tests/test_context.py`
- Modify: `tests/test_conversations.py`
- Modify: `tests/test_turn.py`

**Interfaces:**
- Consumes: `AgentRunner.tool_schemas`、`AgentRunResult.intermediate_messages`、`ToolContext`。
- Produces: `ContextBuilder.build(..., tools=())`、可恢复 Tool Call history、一次事务的中间消息 + 最终回答。

- [ ] **Step 1: 写 Context Schema 透传的失败测试**

```python
def test_build_includes_available_tool_schemas_and_tool_usage_rule(self) -> None:
    schema = {
        "type": "function",
        "function": {"name": "system_info", "description": "Read system info.", "parameters": {}},
    }
    request = ContextBuilder(self.paths).build(
        "deepseek-v4-pro",
        (ModelMessage(role="user", content="查看配置"),),
        tools=(schema,),
    )

    self.assertEqual(request.tools, (schema,))
    self.assertIn("Use an available tool", request.messages[0].content)
    self.assertIn("Never invent tool results", request.messages[0].content)
```

Run: `uv run python -m unittest tests.test_context.ContextBuilderTest.test_build_includes_available_tool_schemas_and_tool_usage_rule -v`

Expected: `ContextBuilder.build()` 不接受 `tools`。

- [ ] **Step 2: 修改 ContextBuilder**

`build` 增加 `tools: tuple[dict[str, JsonValue], ...] = ()`，原样传给 `ModelRequest`。System preamble 增加稳定英文规则：

```text
Use an available tool when it is needed to answer from real local state.
Never invent tool results or claim a tool is unavailable when it is listed.
Treat tool errors as authoritative safety boundaries.
```

Run: `uv run python -m unittest tests.test_context -v`

Expected: Context 全部测试 PASS。

- [ ] **Step 3: 写一次事务保存 Assistant Tool Call、Tool Result、最终回答的失败测试**

```python
intermediate = (
    ModelMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall("call_1", "system_info", {}),),
        reasoning_content="need actual data",
    ),
    ModelMessage(
        role="tool",
        content='{"ok":true,"tool":"system_info","data":{}}',
        tool_call_id="call_1",
    ),
)
self.turns.complete_with_assistant_message(
    turn.id,
    session.id,
    "你的电脑是……",
    intermediate_messages=intermediate,
    input_tokens=10,
    output_tokens=4,
    provider_request_id="req_1",
    iterations=2,
    finish_reason="stop",
)

saved = self.messages.list_recent(session.id)
self.assertEqual([message.role for message in saved], ["user", "assistant", "tool", "assistant"])
self.assertEqual(saved[1].metadata["tool_calls"][0]["name"], "system_info")
self.assertEqual(saved[2].tool_call_id, "call_1")
```

- [ ] **Step 4: 扩展 TurnRepository 完成事务**

`complete_with_assistant_message` 增加 keyword-only `intermediate_messages: tuple[ModelMessage, ...] = ()`。在最终 Assistant 前按顺序插入：

- Assistant Tool Call：`content`、`metadata_json.tool_calls`、`metadata_json.reasoning_content`；
- Tool Result：`role='tool'`、`tool_call_id`、`content`；
- 最终 Assistant：保持现有 provider_message_id 和 metadata。

所有插入与 `turns.status='completed'` 在同一个 `Database.connect()` 事务内。任何一条违反约束时回滚全部中间消息和最终状态。

- [ ] **Step 5: 在 TurnService 恢复 Tool Call metadata 并传递 ToolContext**

新增私有 `_model_message(stored: StoredMessage) -> ModelMessage`：

```python
def _model_message(message: StoredMessage) -> ModelMessage:
    calls_value = message.metadata.get("tool_calls", [])
    calls = tuple(
        ToolCall(
            call_id=cast(dict[str, JsonValue], value)["call_id"],
            name=cast(dict[str, JsonValue], value)["name"],
            arguments=cast(dict[str, JsonValue], value)["arguments"],
        )
        for value in cast(list[JsonValue], calls_value)
    )
    reasoning = message.metadata.get("reasoning_content")
    return ModelMessage(
        role=message.role,
        content=message.content,
        tool_calls=calls,
        tool_call_id=message.tool_call_id,
        reasoning_content=reasoning if isinstance(reasoning, str) else None,
    )
```

如果 metadata 形状损坏，抛现有 `ConversationDataError`，不能把错误 JSON 发给 Provider。

`TurnService` constructor 新增 `state_home: Path` 与 `workspace: WorkspaceConfig`；handle 创建：

```python
tool_context = ToolContext(
    user_id=user_id,
    session_id=session.id,
    turn_id=turn.id,
    state_home=self._state_home,
    workspace=self._workspace.path,
    read_only_roots=self._workspace.read_only_roots,
)
request = self._context.build(self._model, history, tools=self._runner.tool_schemas)
result = await self._runner.run(request, tool_context=tool_context, on_text=on_text)
```

完成时把 `result.intermediate_messages` 传给 Repository。

- [ ] **Step 6: 写 Turn 集成测试**

FakeProvider 第一轮返回 `ToolCall("call_system", "system_info", {})`，第二轮返回最终文本。使用真实 Registry、Policy、ToolExecutor、SystemInfoTool 和临时 SQLite，SystemInfo collector 用 patch 返回固定脱敏数据。

断言：

```python
self.assertEqual(saved_turn.status, "completed")
self.assertEqual([m.role for m in history], ["user", "assistant", "tool", "assistant"])
self.assertEqual(tool_runs[0]["tool_name"], "system_info")
self.assertEqual(tool_runs[0]["status"], "succeeded")
self.assertEqual(provider.requests[0].tools[0]["function"]["name"], "system_info")
self.assertEqual(provider.requests[1].messages[-1].role, "tool")
```

再执行第二个 Turn，断言历史中的 Assistant Tool Call 被正确恢复成 `ModelMessage.tool_calls`，Provider payload 不会出现孤立 Tool Message。

Run: `uv run python -m unittest tests.test_context tests.test_conversations tests.test_turn -v`

Expected: Context、Conversation、Turn 测试全部 PASS。

- [ ] **Step 7: Ruff 并提交**

```bash
uv run ruff check src/lobster0/agent src/lobster0/storage/conversations.py tests/test_context.py tests/test_conversations.py tests/test_turn.py
git add src/lobster0/agent src/lobster0/storage/conversations.py tests/test_context.py tests/test_conversations.py tests/test_turn.py
git commit -m "feat: persist tool conversations in agent turns"
```

---

### Task 6: CLI 生产装配、工程文档与验收

**Files:**
- Modify: `src/lobster0/cli.py`
- Modify: `tests/test_cli_chat.py`
- Create: `docs/engineering/phase-2/20260807_tool-runtime-and-system-info.md`
- Modify: `docs/README.md`
- Modify: `README.md`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Consumes: 前五个 Task 的全部稳定接口。
- Produces: `uv run lobster0 chat --message "帮我看看我的电脑是什么配置"` 的真实 Tool Loop。

- [ ] **Step 1: 写 CLI 模型请求包含 system_info Schema 的离线失败测试**

扩展 `_ModelServer` 记录请求中的 `tools`，并允许按请求次数返回两段 SSE：

1. 第一段返回 `tool_calls`，调用 `system_info`；
2. 第二段返回 `offline system answer`。

测试断言：

```python
self.assertEqual((code, output, error), (0, "offline system answer\n", ""))
self.assertEqual(server.observations[0]["tools"][0]["function"]["name"], "system_info")
with sqlite3.connect(home / "lobster0.db") as connection:
    tool_run = connection.execute(
        "SELECT tool_name, status, policy_action FROM tool_runs"
    ).fetchone()
    roles = [row[0] for row in connection.execute("SELECT role FROM messages ORDER BY id")]
self.assertEqual(tool_run, ("system_info", "succeeded", "allow"))
self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
```

- [ ] **Step 2: 运行并确认 RED 是 CLI 未装配 Tool**

Run: `uv run python -m unittest tests.test_cli_chat.CliChatTest.test_chat_executes_system_info_tool_and_persists_trace -v`

Expected: 第一请求没有 `tools`，或模型返回 Tool Call 后得到 `tool_not_found`。

- [ ] **Step 3: 在 `_chat` 组装唯一生产 Tool 链路**

```python
registry = ToolRegistry((SystemInfoTool(),))
executor = ToolExecutor(
    registry=registry,
    policy=PolicyEngine(),
    runs=ToolRunRepository(database),
    result_max_chars=config.agent.tool_result_max_chars,
)
service = TurnService(
    model=config.agent.model,
    state_home=paths.home,
    workspace=config.workspace,
    sessions=SessionRepository(database),
    messages=MessageRepository(database),
    turns=TurnRepository(database),
    context=ContextBuilder(paths),
    runner=AgentRunner(
        provider,
        executor=executor,
        max_iterations=config.agent.max_tool_iterations,
    ),
)
```

不增加第二套调试执行入口，不增加 `lobster0 tools run`，避免 CLI 绕过正常 Turn、Policy 和审计。

- [ ] **Step 4: 运行 CLI、Provider 与全量离线测试**

```bash
uv run python -m unittest tests.test_cli_chat -v
uv run python -m unittest tests.test_openai_compatible_provider -v
uv run python -m unittest discover -s tests -v
uv run ruff check .
git diff --check
```

Expected: 全部测试 PASS；Ruff 和 diff check 无输出；离线测试不访问真实 DeepSeek。

- [ ] **Step 5: 编写事实工程文档**

`docs/engineering/phase-2/20260807_tool-runtime-and-system-info.md` 必须记录：

- 用户说一句话后从 Provider → Runner → Executor → Policy → Tool → SQLite → Provider 的实际调用链；
- `system_info` 的 macOS/Linux 字段来源和隐私白名单；
- 当前已实现与仍未实现的 Tool 列表；
- Tool Result JSON、ToolRun/Audit 数据位置；
- 离线测试命令；
- 真实 CLI 验证命令与预期，但不记录真实序列号、路径、API Key；
- 已知边界：应用级 Policy 不是 OS Sandbox，P2.1A 不含文件工具和审批。

在 `docs/README.md` 加入口；README 只写已经测试通过的 `system_info` 能力；进度 HTML 只把 P2.1A 标为完成，不把整个 Phase 2 标为完成。

- [ ] **Step 6: 使用本地真实配置做显式冒烟**

```bash
uv run lobster0 doctor
uv run lobster0 chat --message "帮我看看我的电脑是什么配置"
```

Expected: `doctor` 继续 PASS；回答包含本机可获取的 OS/CPU/内存/存储信息，不出现“我无法访问你的电脑”，不出现序列号、UUID、用户名或 API Key。若模型没有调用 Tool，先保存脱敏请求观察值并修正 System Prompt/Schema 描述，不能在回答中硬编码机器配置。

- [ ] **Step 7: 最终验证并提交 P2.1A**

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
git diff --check
git status --short
git add README.md docs src tests
git commit -m "feat: let lobster0 inspect system information"
```

Expected: 工作区只有计划内文件；提交不包含 `.env`、数据库、真实 Tool 输出、日志或结果文件。

---

## Plan Self-Review

- Spec coverage: 本计划覆盖 P2.1A Tool Contract、Registry、低风险 Policy、SystemInfo、ToolRun/Audit、Agent Loop、消息持久化、CLI 和工程文档。
- Deliberate gap: `read_file`、`glob`、`grep` 是 P2.1B；`write_file/edit_file/Approval` 是 P2.2；`run_command` 是 P2.3；`http_get` 是 P2.4。
- Schema decision: 现有 SQLite v1 已满足 P2.1A，不创建 migration v2。
- Dependency decision: 标准库足够，不添加 JSON Schema、系统信息或 CLI 第三方库。
- Safety decision: system_info 只有固定命令与输出字段白名单；模型不能控制 subprocess argv。
- Persistence decision: 中间 Tool 消息和最终 Assistant 在一个 Turn 完成事务内保存；ToolRun 自身在执行前单独落盘以保留崩溃轨迹。
- Placeholder scan: 未发现未完成标记、模糊测试步骤或未定义接口。
