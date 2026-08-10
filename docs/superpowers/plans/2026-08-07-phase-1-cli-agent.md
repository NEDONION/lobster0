# Lobster0 Phase 1 CLI Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a real `lobster0 chat` path backed by DeepSeek V4 Pro, with safe local `.env` credentials, an async OpenAI-compatible provider, an eight-iteration Agent loop, and transactional SQLite conversation history.

**Architecture:** The CLI loads local configuration and creates one application-scoped Provider, ContextBuilder, AgentRunner, and TurnService. Provider-specific HTTP and response parsing remain below the Agent contract; conversation persistence remains below TurnService. Phase 1 deliberately does not create a Message Bus, Channel interface, Policy Engine, or built-in tools because no second channel or executable tool exists yet.

**Tech Stack:** Python 3.12, `asyncio`, `argparse`, `sqlite3`, `httpx>=0.28,<1`, standard-library `unittest`, Ruff.

## Global Constraints

- Product, package, import package, and CLI names remain `Lobster0` / `lobster0`.
- Default model is exactly `deepseek-v4-pro`; default base URL is exactly `https://api.deepseek.com`.
- The only credential variable used by the model path is `LOBSTER0_MODEL_API_KEY`.
- `.env` is local-only, ignored by Git, owner-readable (`0600`), and its values never enter logs, errors, tests, docs, or commits.
- Provider calls are async, use OpenAI Chat Completions, and retry connection failures, timeouts, 429, and 5xx at most once.
- Agent model/tool iterations are capped at 8; tool execution remains sequential.
- Unit and offline E2E tests never call a real model.
- Every production module ships with a detailed document under `docs/engineering/phase-1/` covering boundaries, interfaces, data flow, errors, security, tests, debugging, and limitations.
- Public and modified Python functions/classes have accurate type annotations and Chinese docstrings.

---

## File Map

| File | Responsibility |
| --- | --- |
| `src/lobster0/env.py` | Strict, non-executing local `.env` parser |
| `src/lobster0/providers/base.py` | Stable model request/response contracts and provider errors |
| `src/lobster0/providers/openai_compatible.py` | HTTPX request, SSE/JSON parsing, retry, DeepSeek-compatible reasoning/tool fragments |
| `src/lobster0/agent/context.py` | Deterministic System/SOUL/USER/history context construction |
| `src/lobster0/agent/runner.py` | Model/tool loop, empty response and eight-iteration guard |
| `src/lobster0/storage/conversations.py` | Session, Turn, Message repositories and completion transaction |
| `src/lobster0/agent/turn.py` | One CLI turn orchestration and error-state persistence |
| `src/lobster0/cli.py` | `chat` parser, dependency assembly, one-shot and interactive output |
| `docs/engineering/phase-1/*.md` | Module-level engineering records kept with each implementation commit |

---

### Task 1: Local `.env` boundary and DeepSeek defaults

**Files:**
- Create: `src/lobster0/env.py`
- Create: `tests/test_env.py`
- Create: `docs/engineering/phase-1/20260807_environment.md`
- Modify: `src/lobster0/bootstrap.py`
- Modify: `.env.example`
- Modify: `tests/test_bootstrap.py`

**Interfaces:**
- Produces: `load_dotenv(path: Path, environ: MutableMapping[str, str] | None = None) -> tuple[str, ...]`
- Produces: `DotEnvError(ValueError)` with path and line number only
- Changes defaults to model `deepseek-v4-pro`, base URL `https://api.deepseek.com`, key name `LOBSTER0_MODEL_API_KEY`

- [ ] **Step 1: Write failing `.env` tests**

```python
def test_load_dotenv_sets_missing_values_without_overriding_environment(self) -> None:
    path = self.root / ".env"
    path.write_text(
        "# local model\nLOBSTER0_MODEL_API_KEY='from-file'\nLOBSTER0_MODEL_NAME=deepseek-v4-pro\n",
        encoding="utf-8",
    )
    environ = {"LOBSTER0_MODEL_NAME": "from-shell"}

    loaded = load_dotenv(path, environ)

    self.assertEqual(loaded, ("LOBSTER0_MODEL_API_KEY",))
    self.assertEqual(environ["LOBSTER0_MODEL_API_KEY"], "from-file")
    self.assertEqual(environ["LOBSTER0_MODEL_NAME"], "from-shell")


def test_invalid_dotenv_reports_line_without_secret_value(self) -> None:
    path = self.root / ".env"
    path.write_text("export LOBSTER0_MODEL_API_KEY=never-print-this\n", encoding="utf-8")

    with self.assertRaises(DotEnvError) as caught:
        load_dotenv(path, {})

    self.assertIn(":1", str(caught.exception))
    self.assertNotIn("never-print-this", str(caught.exception))
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -m unittest tests.test_env -v`<br>
Expected: import failure because `lobster0.env` does not exist.

- [ ] **Step 3: Implement the strict parser with the standard library**

```python
_KEY = re.compile(r"[A-Z_][A-Z0-9_]*\Z")


def load_dotenv(
    path: Path,
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    target = os.environ if environ is None else environ
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    loaded: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        parsed = _parse_line(path, line_number, line)
        if parsed is not None and parsed[0] not in target:
            target[parsed[0]] = parsed[1]
            loaded.append(parsed[0])
    return tuple(loaded)
```

`_parse_line()` accepts comments, blank lines, `KEY=value`, and matching single/double quotes. It rejects `export`, missing `=`, invalid keys, unmatched quotes, embedded newlines, and NUL without echoing source content.

- [ ] **Step 4: Change generated and example configuration defaults**

Update `_render_default_config()` and `.env.example` to:

```toml
[agent]
model = "deepseek-v4-pro"

[provider]
base_url = "https://api.deepseek.com"
api_key_env = "LOBSTER0_MODEL_API_KEY"
```

Add a bootstrap assertion that a new state loads these exact values.

- [ ] **Step 5: Write the environment engineering document**

`docs/engineering/phase-1/20260807_environment.md` must explain discovery (`Path.cwd() / ".env"`), precedence, accepted grammar, rejection behavior, `0600`, one-time EvalHub migration, non-logging rules, tests, and the limitation that `.env` is not searched in parent directories.

- [ ] **Step 6: Run focused and full checks**

Run: `.venv/bin/python -m unittest tests.test_env tests.test_bootstrap tests.test_config -v`<br>
Expected: PASS.<br>
Run: `.venv/bin/ruff check --no-cache src/lobster0/env.py tests/test_env.py src/lobster0/bootstrap.py tests/test_bootstrap.py`<br>
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add .env.example src/lobster0/env.py src/lobster0/bootstrap.py tests/test_env.py tests/test_bootstrap.py docs/engineering/phase-1/20260807_environment.md
git commit -m "feat: add safe local environment loading"
```

---

### Task 2: Provider contracts and response parser

**Files:**
- Create: `src/lobster0/providers/__init__.py`
- Create: `src/lobster0/providers/base.py`
- Create: `tests/test_provider_contracts.py`
- Create: `docs/engineering/phase-1/20260807_provider-contract.md`

**Interfaces:**
- Produces: `JsonValue`, `ToolCall`, `ModelMessage`, `ModelRequest`, `ModelResponse`
- Produces: `StreamHandler = Callable[[str], Awaitable[None]]`
- Produces: `ModelProvider.complete(request, on_text=None) -> ModelResponse`
- Produces: `ProviderAuthenticationError`, `ProviderRateLimitError`, `ProviderTimeoutError`, `ProviderProtocolError`, `ProviderServerError`

- [ ] **Step 1: Write failing contract tests**

```python
def test_model_contract_keeps_reasoning_for_tool_continuation(self) -> None:
    call = ToolCall("call_1", "read_file", {"path": "README.md"})
    message = ModelMessage(
        role="assistant",
        content="",
        tool_calls=(call,),
        reasoning_content="internal continuation state",
    )

    self.assertEqual(message.tool_calls[0].arguments["path"], "README.md")
    self.assertEqual(message.reasoning_content, "internal continuation state")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -m unittest tests.test_provider_contracts -v`<br>
Expected: import failure because `lobster0.providers` does not exist.

- [ ] **Step 3: Implement immutable contracts and narrow error hierarchy**

```python
class ProviderError(RuntimeError):
    """表示调用模型服务失败且错误信息已经过安全收窄。"""


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    tool_calls: tuple[ToolCall, ...]
    reasoning_content: str | None
    finish_reason: str
    input_tokens: int | None
    output_tokens: int | None
    provider_request_id: str | None
```

Validate only at the remote-data parser boundary; the dataclasses remain transparent value objects so fakes are concise.

- [ ] **Step 4: Document the provider contract**

`20260807_provider-contract.md` must include the request/response diagram, each field, `reasoning_content` lifetime, error taxonomy, redaction boundary, fake implementation example, and Phase 2 extension point.

- [ ] **Step 5: Run tests and Ruff, then commit**

Run: `.venv/bin/python -m unittest tests.test_provider_contracts -v`<br>
Expected: PASS.<br>
Run: `.venv/bin/ruff check --no-cache src/lobster0/providers tests/test_provider_contracts.py`<br>
Expected: PASS.

```bash
git add src/lobster0/providers tests/test_provider_contracts.py docs/engineering/phase-1/20260807_provider-contract.md
git commit -m "feat: define model provider contracts"
```

---

### Task 3: Async OpenAI-compatible transport, SSE, and retry

**Files:**
- Create: `src/lobster0/providers/openai_compatible.py`
- Create: `tests/test_openai_compatible_provider.py`
- Create: `docs/engineering/phase-1/20260807_openai-compatible-provider.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Phase 1 provider contracts
- Produces: `OpenAICompatibleProvider(base_url, api_key, timeout_seconds, *, transport=None, sleep=asyncio.sleep)`
- Produces: `await provider.complete(request, on_text=None)` and `await provider.aclose()`

- [ ] **Step 1: Add `httpx` to project dependencies and sync**

```toml
dependencies = [
  "httpx>=0.28,<1",
]
```

Run: `uv sync --extra dev`<br>
Expected: lock file updated and editable package installed.

- [ ] **Step 2: Write the failing SSE request/response test**

```python
async def test_complete_streams_text_and_assembles_reasoning_and_tool_arguments(self) -> None:
    seen_request: httpx.Request | None = None

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "req_1"},
            text=SSE_WITH_REASONING_TEXT_TOOL_AND_USAGE,
        )

    provider = OpenAICompatibleProvider(
        "https://api.deepseek.com",
        "secret-test-key",
        10,
        transport=httpx.MockTransport(respond),
    )
    chunks: list[str] = []
    response = await provider.complete(REQUEST_WITH_TOOL, chunks.append)

    self.assertEqual(chunks, ["读取", "完成"])
    self.assertEqual(response.tool_calls[0].arguments, {"path": "README.md"})
    self.assertEqual(response.reasoning_content, "need file")
    self.assertEqual(response.provider_request_id, "req_1")
    self.assertNotIn("secret-test-key", repr(seen_request))
```

The real callback is async; the test uses an async collector rather than `list.append` in final code.

- [ ] **Step 3: Run the SSE test and verify RED**

Run: `.venv/bin/python -m unittest tests.test_openai_compatible_provider -v`<br>
Expected: import failure for `openai_compatible`.

- [ ] **Step 4: Implement request serialization and SSE aggregation**

The request payload is exactly:

```python
payload = {
    "model": request.model,
    "messages": [_message_payload(message) for message in request.messages],
    "stream": True,
    "stream_options": {"include_usage": True},
}
if request.tools:
    payload["tools"] = list(request.tools)
```

The parser accepts only `data:` SSE events, ignores comment/blank lines, stops on `[DONE]`, merges tool fragments by `index`, parses final arguments as JSON objects, and rejects missing `choices`, invalid JSON, non-object arguments, or a stream with neither response data nor usage.

- [ ] **Step 5: Write retry and redaction tests**

```python
async def test_server_error_is_retried_once_then_succeeds(self) -> None:
    attempts = 0
    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200, text=SUCCESS_SSE)

    response = await self.provider(respond).complete(SIMPLE_REQUEST)
    self.assertEqual(response.content, "ok")
    self.assertEqual(attempts, 2)


async def test_authentication_error_is_not_retried_or_leaked(self) -> None:
    # One 401 response whose body echoes a fake key must produce one safe exception.
```

- [ ] **Step 6: Implement status mapping and one retry**

Map 401/403, 429, 5xx, timeout/connect, and other invalid responses to the exact stable errors. Consume at most 2 KB only for a generic diagnostic category; never include response text in the public exception. Honor numeric `Retry-After`, capped at 30 seconds; otherwise sleep 0.5 seconds.

- [ ] **Step 7: Document transport internals and debugging**

`20260807_openai-compatible-provider.md` must include request JSON, SSE event examples, fragment merge rules, retry matrix, lifecycle/aclose, DeepSeek reasoning behavior, safe diagnostics, MockTransport testing, and `curl`-free local debugging.

- [ ] **Step 8: Run focused/full tests and commit**

Run: `.venv/bin/python -m unittest tests.test_provider_contracts tests.test_openai_compatible_provider -v`<br>
Expected: PASS.<br>
Run: `.venv/bin/ruff check --no-cache src/lobster0/providers tests/test_provider_contracts.py tests/test_openai_compatible_provider.py`<br>
Expected: PASS.

```bash
git add pyproject.toml uv.lock src/lobster0/providers tests/test_openai_compatible_provider.py docs/engineering/phase-1/20260807_openai-compatible-provider.md
git commit -m "feat: add openai compatible model provider"
```

---

### Task 4: ContextBuilder and AgentRunner

**Files:**
- Create: `src/lobster0/agent/__init__.py`
- Create: `src/lobster0/agent/context.py`
- Create: `src/lobster0/agent/runner.py`
- Create: `tests/fakes/__init__.py`
- Create: `tests/fakes/fake_provider.py`
- Create: `tests/test_context.py`
- Create: `tests/test_agent_runner.py`
- Create: `docs/engineering/phase-1/20260807_context-builder.md`
- Create: `docs/engineering/phase-1/20260807_agent-runner.md`

**Interfaces:**
- Produces: `ContextBuilder(paths).build(model, history) -> ModelRequest`
- Produces: `AgentRunner(provider, tools=None, max_iterations=8).run(request) -> AgentRunResult`
- Produces: `AgentLoopLimitError`, `EmptyModelResponseError`
- Produces: deterministic `FakeProvider(responses)` for later Turn and CLI tests

- [ ] **Step 1: Write ContextBuilder failing tests**

```python
def test_context_orders_identity_files_before_history(self) -> None:
    self.paths.soul.write_text("Be precise.", encoding="utf-8")
    self.paths.user.write_text("Name: Ned", encoding="utf-8")
    history = (
        ModelMessage("user", "previous"),
        ModelMessage("assistant", "answer"),
        ModelMessage("user", "current"),
    )

    request = ContextBuilder(self.paths).build("deepseek-v4-pro", history)

    self.assertEqual(request.messages[0].role, "system")
    self.assertIn("Be precise.", request.messages[0].content)
    self.assertIn("Name: Ned", request.messages[0].content)
    self.assertEqual(request.messages[-1].content, "current")
```

- [ ] **Step 2: Verify RED, then implement minimal deterministic context**

Run: `.venv/bin/python -m unittest tests.test_context -v`<br>
Expected: import failure.<br>
Implementation reads UTF-8 `SOUL.md` and `USER.md`, uses one built-in Lobster0 system preamble, preserves history order, and raises `ContextError` with paths but no file content on I/O errors.

- [ ] **Step 3: Write AgentRunner failing tests**

```python
async def test_runner_returns_final_text_after_tool_result(self) -> None:
    provider = FakeProvider((TOOL_RESPONSE, FINAL_RESPONSE))
    runner = AgentRunner(provider, {"echo": echo_tool})

    result = await runner.run(SIMPLE_REQUEST_WITH_ECHO_SCHEMA)

    self.assertEqual(result.content, "done")
    self.assertEqual(result.iterations, 2)
    self.assertEqual(provider.requests[1].messages[-1].role, "tool")
    self.assertEqual(provider.requests[1].messages[-2].reasoning_content, "need echo")
```

Add separate tests for empty final content, unknown tool result, exactly eight tool responses, and cancellation propagation.

- [ ] **Step 4: Verify RED, then implement the bounded loop**

Run: `.venv/bin/python -m unittest tests.test_agent_runner -v`<br>
Expected: import failure.<br>
The runner appends one Assistant `ModelMessage` per Provider response, awaits handlers sequentially, appends JSON Tool messages, and never catches `asyncio.CancelledError`.

- [ ] **Step 5: Write both module engineering documents**

`20260807_context-builder.md` documents prompt order, file reads, history limit ownership, token-budget deferral, errors, fixtures, and future Memory/Skill insertion points. `20260807_agent-runner.md` documents the state machine, request evolution, reasoning propagation, tool handler contract, loop counting, errors, cancellation, tests, and Phase 2 replacement boundary.

- [ ] **Step 6: Run focused/full checks and commit**

Run: `.venv/bin/python -m unittest tests.test_context tests.test_agent_runner -v`<br>
Expected: PASS.<br>
Run: `.venv/bin/ruff check --no-cache src/lobster0/agent tests/fakes tests/test_context.py tests/test_agent_runner.py`<br>
Expected: PASS.

```bash
git add src/lobster0/agent tests/fakes tests/test_context.py tests/test_agent_runner.py docs/engineering/phase-1/20260807_context-builder.md docs/engineering/phase-1/20260807_agent-runner.md
git commit -m "feat: add bounded agent runner"
```

---

### Task 5: Conversation repositories and TurnService

**Files:**
- Create: `src/lobster0/storage/conversations.py`
- Create: `src/lobster0/agent/turn.py`
- Create: `tests/test_conversations.py`
- Create: `tests/test_turn.py`
- Create: `docs/engineering/phase-1/20260807_conversation-storage.md`
- Create: `docs/engineering/phase-1/20260807_turn-service.md`

**Interfaces:**
- Produces: `SessionRepository.get_or_create_cli(user_id, conversation_id) -> Session`
- Produces: `MessageRepository.list_recent(session_id, limit=20) -> tuple[StoredMessage, ...]`
- Produces: `TurnRepository.create_with_user_message(...) -> Turn`
- Produces: `TurnRepository.complete_with_assistant_message(...) -> StoredMessage`
- Produces: `TurnRepository.fail(...)` and `cancel(...)`
- Produces: `TurnService.handle(user_id, text, conversation_id) -> TurnResult`

- [ ] **Step 1: Write failing repository transaction tests**

```python
def test_complete_writes_assistant_and_usage_atomically(self) -> None:
    turn = self.turns.create_with_user_message(self.session.id, "event-1", "deepseek-v4-pro", "hi")

    assistant = self.turns.complete_with_assistant_message(
        turn.id,
        self.session.id,
        "hello",
        input_tokens=10,
        output_tokens=4,
        provider_request_id="req_1",
    )

    saved = self.turns.get(turn.id)
    self.assertEqual(saved.status, "completed")
    self.assertEqual((saved.input_tokens, saved.output_tokens), (10, 4))
    self.assertEqual(assistant.content, "hello")
```

Also test Session idempotency, recent-message chronological order, failed status, cancelled status, and rollback when Assistant insertion violates a constraint.

- [ ] **Step 2: Verify RED, then implement parameterized repositories**

Run: `.venv/bin/python -m unittest tests.test_conversations -v`<br>
Expected: import failure.<br>
Use one `Database.connect()` transaction for each state transition. Store timestamps as timezone-aware UTC ISO strings and runtime snapshot as compact JSON containing only model and provider request ID.

- [ ] **Step 3: Write failing TurnService tests with FakeProvider**

```python
async def test_turn_service_persists_successful_conversation(self) -> None:
    service = self.build_service(FakeProvider((FINAL_RESPONSE,)))

    result = await service.handle(self.owner.id, "hello", "default")

    self.assertEqual(result.content, "world")
    self.assertEqual(self.turns.get(result.turn_id).status, "completed")
    self.assertEqual([m.role for m in self.messages.list_recent(result.session_id)], ["user", "assistant"])
```

Add Provider error → failed Turn and CancelledError → cancelled Turn tests. The persisted public error message must not contain upstream response content.

- [ ] **Step 4: Verify RED, then implement TurnService**

Run: `.venv/bin/python -m unittest tests.test_turn -v`<br>
Expected: import failure.<br>
Flow: session → queued Turn/user Message → running → recent history → ContextBuilder → AgentRunner → atomic Assistant/usage completion. Catch known Agent/Provider/Context errors only; cancel separately; let database/programming errors propagate after the repository rollback.

- [ ] **Step 5: Write storage and Turn engineering documents**

`20260807_conversation-storage.md` includes table mapping, dataclasses, SQL transaction diagrams, timestamps, indexes, queries, rollback behavior, tests, inspection SQL, and limitations. `20260807_turn-service.md` includes orchestration sequence, dependency graph, state transitions, error-code mapping, cancellation, idempotency scope, test fakes, debug flow, and the future Channel entry point.

- [ ] **Step 6: Run focused/full checks and commit**

Run: `.venv/bin/python -m unittest tests.test_conversations tests.test_turn -v`<br>
Expected: PASS.<br>
Run: `.venv/bin/ruff check --no-cache src/lobster0/storage/conversations.py src/lobster0/agent/turn.py tests/test_conversations.py tests/test_turn.py`<br>
Expected: PASS.

```bash
git add src/lobster0/storage/conversations.py src/lobster0/agent/turn.py tests/test_conversations.py tests/test_turn.py docs/engineering/phase-1/20260807_conversation-storage.md docs/engineering/phase-1/20260807_turn-service.md
git commit -m "feat: persist cli agent turns"
```

---

### Task 6: CLI chat, offline E2E, local secret, and live DeepSeek proof

**Files:**
- Modify: `src/lobster0/cli.py`
- Modify: `tests/test_cli.py`
- Create: `tests/test_cli_chat.py`
- Create: `docs/engineering/phase-1/20260807_cli-chat.md`
- Create: `docs/engineering/phase-1/20260807_testing-and-debugging.md`
- Modify: `README.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Produces: `lobster0 chat --message TEXT [--session ID] [--home PATH]`
- Produces: interactive prompt when `--message` is omitted
- Consumes: `.env`, `AppConfig`, initialized SQLite, Owner, Provider, ContextBuilder, AgentRunner, TurnService

- [ ] **Step 1: Write failing CLI parser and missing-key tests**

```python
def test_chat_requires_configured_model_key_without_leaking_environment(self) -> None:
    run_cli(["init", "--home", str(self.home)])
    with mock.patch.dict(os.environ, {}, clear=True), change_directory(self.root):
        code, output, error = run_cli(["chat", "--home", str(self.home), "--message", "hello"])

    self.assertEqual(code, 2)
    self.assertEqual(output, "")
    self.assertIn("LOBSTER0_MODEL_API_KEY is not configured", error)
```

- [ ] **Step 2: Write failing offline HTTP E2E**

Start a standard-library loopback `ThreadingHTTPServer` that validates Bearer presence without storing it and returns one SSE completion. Initialize a temporary Lobster0 home, rewrite only `provider.base_url` to the loopback URL, write a test `.env`, and run the real CLI.

Assertions: exit 0, stdout is the model answer, SQLite has one completed Turn and user/assistant Messages, model is `deepseek-v4-pro`, and stderr is empty.

- [ ] **Step 3: Verify RED, then assemble chat dependencies**

Run: `.venv/bin/python -m unittest tests.test_cli_chat -v`<br>
Expected: `chat` is not a recognized command.<br>
Implementation loads `.env` only for chat, validates initialized state and key, creates one Provider, closes it in `finally`, maps stable errors to exit codes, and prints only final Assistant content.

- [ ] **Step 4: Add the minimal interactive loop**

When `--message` is omitted, require stdin TTY, repeatedly read `You> `, use one stable session ID, print `Lobster0> ...`, and exit on EOF, empty `/exit`, or `/quit`. Non-TTY without `--message` returns code 2 with a scripting hint.

- [ ] **Step 5: Write CLI and test/debug engineering documents**

`20260807_cli-chat.md` covers parser, dependency assembly, one-shot/interactive flows, exit codes, stdout/stderr contract, provider lifecycle, session selection, tests, and limitations. `20260807_testing-and-debugging.md` covers focused commands, full gate, local fake server, safe SQLite inspection, live-test opt-in, redaction checks, and failure triage.

- [ ] **Step 6: Update user docs and progress HTML**

README and local guide show exact `.env`, init, doctor, one-shot chat, and test commands, while marking tools/IM as planned. Architecture marks CLI → Turn → Runner → Provider → SQLite as implemented. Progress page marks all Phase 1 checklist items complete only after their tests pass and displays the final test count from a fresh full run.

- [ ] **Step 7: Copy the existing EvalHub credential without displaying it**

Use EvalHub's `default_model_provider_repository().resolve_api_key("deepseek")` inside one local Python process that writes:

```text
LOBSTER0_MODEL_API_KEY=<resolved value>
```

to `/Users/nedonion/PycharmProjects/lobster0/.env` with mode `0600`. Do not print the value. For worktree live verification, create a separate ignored `.env` with the same mode, then remove it when the worktree is cleaned.

- [ ] **Step 8: Run offline completion gate**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check --no-cache .
git diff --check
uv build
```

Expected: every command succeeds, no warning or credential output, and `git status --short` contains only intended tracked files.

- [ ] **Step 9: Run live DeepSeek smoke test**

Initialize a fresh temporary home and run a one-shot prompt requesting a short fixed answer. Verify exit 0, non-empty stdout, completed SQLite Turn, positive reported token usage, model `deepseek-v4-pro`, and no credential substring in captured stdout/stderr. Never commit the temporary home, `.env`, database, or response.

- [ ] **Step 10: Commit**

```bash
git add src/lobster0/cli.py tests/test_cli.py tests/test_cli_chat.py README.md docs/getting-started/20260807_本地运行指南.md docs/architecture/20260807_系统架构.md docs/progress/index.html docs/engineering/phase-1/20260807_cli-chat.md docs/engineering/phase-1/20260807_testing-and-debugging.md
git commit -m "feat: complete cli agent loop"
```

---

## Phase Review and Merge Gate

- [ ] Compare every requirement in `docs/superpowers/specs/2026-08-07-phase-1-cli-agent-design.md` to one passing test or explicit live record.
- [ ] Confirm each Python module has its corresponding detailed engineering document and no document claims planned Tool/IM features are implemented.
- [ ] Review the full branch diff for secrets, response payloads, absolute EvalHub paths, debug output, and unrelated edits.
- [ ] Run a specification compliance review and a code-quality review.
- [ ] Re-run all tests, Ruff, build, CLI version, and `git diff --check` on the final branch head.
- [ ] Fast-forward merge the reviewed branch into `main`, re-run tests on `main`, push `main`, verify remote SHA, then clean the worktree and local branch.
