# Lobster0 Desktop W0/W1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a development-ready, light-theme Lobster0 Desktop with four views and one complete single-Agent task loop over the existing Python Bridge.

**Architecture:** Electron Main owns the Python Bridge child process and exposes a fixed typed IPC surface through Preload. React renders Home, Task Workbench, Automation, and Settings; Python Core remains the only authority for sessions, tools, approvals, permissions, automation data, and workspace access. W0/W1 reuse the existing `@lobster0/pi-tui` Bridge client and event reducer instead of creating a second protocol implementation.

**Tech Stack:** Python 3.12+, Electron, React, TypeScript, Vite/electron-vite, Tailwind CSS, pnpm 10.14, SQLite through the existing Lobster0 repositories, standard-library `unittest`, Node/Vitest tests.

## Global Constraints

- Scope is W0/W1 only: development shell, single foreground Agent task, recent task history, read-only Automation list, permission settings, and workspace switching.
- W2 Artifact preview, W3 Sub-agent, installers/signing, auto-update, Office editors, external Agent adapters, cloud accounts, and dark theme are out of scope.
- The four views are `home`, `task`, `automation`, and `settings`; do not add a router, dashboard, Agent topology, or marketplace.
- The first theme is light-only with `#F7F8FA` canvas, `#FFFFFF` surface, `#E5E7EB` border, `#1D2433` primary text, `#667085` secondary text, and `#2563EB` accent.
- Renderer has no Node integration and cannot read SQLite, secrets, workspace files, or spawn commands. All effects use fixed Preload methods and Python Core validation.
- Keep protocol v1 backward compatible. New request types are capability-gated and existing pi-tui behavior must remain unchanged.
- Use the existing `BridgeClient`, NDJSON decoder, and timeline reducer from `tui/`; do not copy them into `desktop/`.
- One Python Bridge supports one foreground Turn. Do not add concurrency, queues, or speculative multi-Agent abstractions.
- Pin all new JavaScript dependencies in `desktop/pnpm-lock.yaml`; do not add Python dependencies.
- The implementation must start from a green base. If the Browser Doctor count/expected-name failures are still present, coordinate that baseline fix before counting Desktop tests as passing.
- Never read, log, persist, or commit real API keys, IM credentials, conversation data, or `.env` contents.

## File Map

### Existing files to modify

- `tui/package.json`: export the existing Bridge client, protocol types, and event reducer for same-repo reuse.
- `tui/src/bridge-client.ts`: parameterize client identity and add an explicit workspace spawn argument.
- `tui/test/bridge-client.test.ts`: preserve pi-tui defaults and test Desktop spawn arguments.
- `src/lobster0/bridge/__main__.py`: accept a validated optional workspace override.
- `src/lobster0/bridge/protocol.py`: add strict read-only session and Automation request schemas.
- `src/lobster0/bridge/server.py`: route new query requests through Core-owned services.
- `src/lobster0/runtime.py`: expose the conversation query service and settle stale foreground Turns.
- `src/lobster0/storage/conversations.py`: add bounded CLI Session listing and stale Turn settlement.
- `tests/test_bridge_protocol.py`, `tests/test_bridge_server.py`, `tests/test_conversations.py`: cover new trust-boundary behavior.
- `README.md`, `docs/engineering/README.md`: document development commands and implementation status only after the code passes.

### New Python file

- `src/lobster0/bridge/conversations.py`: serialize Owner-scoped Session summaries/history without exposing SQLite to Electron.
- `tests/test_bridge_conversations.py`: focused repository/service tests for recent tasks and safe history.

### New Desktop package

- `desktop/package.json`, `desktop/pnpm-lock.yaml`: isolated package and pinned dependencies.
- `desktop/electron.vite.config.ts`, `desktop/tsconfig.json`, `desktop/tailwind.config.ts`, `desktop/postcss.config.cjs`, `desktop/index.html`: build configuration.
- `desktop/src/common/api.ts`: exact Renderer/Preload/Main contract and shared value types.
- `desktop/src/main/index.ts`: BrowserWindow lifecycle and shutdown.
- `desktop/src/main/bridge-service.ts`: sole owner of the shared `BridgeClient`.
- `desktop/src/main/ipc.ts`: fixed IPC handlers; no generic pass-through channel.
- `desktop/src/preload/index.ts`: narrow `window.lobster0` API.
- `desktop/src/renderer/main.tsx`, `desktop/src/renderer/app.tsx`: React entry and four-view shell.
- `desktop/src/renderer/navigation.ts`: exact view IDs and labels.
- `desktop/src/renderer/task-state.ts`: Desktop wrapper around the existing pi-tui reducer.
- `desktop/src/renderer/task-workbench.tsx`: three-column task UI and approval controls.
- `desktop/src/renderer/styles.css`: light-only tokens and layout.
- `desktop/test/navigation.test.ts`, `desktop/test/bridge-service.test.ts`, `desktop/test/preload.test.ts`, `desktop/test/task-state.test.ts`: deterministic W0/W1 tests.

---

### Task 1: Bootstrap the light four-view Electron shell

**Files:**
- Create: `desktop/package.json`
- Create: `desktop/electron.vite.config.ts`
- Create: `desktop/tsconfig.json`
- Create: `desktop/tailwind.config.ts`
- Create: `desktop/postcss.config.cjs`
- Create: `desktop/index.html`
- Create: `desktop/src/main/index.ts`
- Create: `desktop/src/preload/index.ts`
- Create: `desktop/src/renderer/main.tsx`
- Create: `desktop/src/renderer/app.tsx`
- Create: `desktop/src/renderer/navigation.ts`
- Create: `desktop/src/renderer/styles.css`
- Test: `desktop/test/navigation.test.ts`

**Interfaces:**
- Consumes: no new Core API; this task renders static view shells only.
- Produces: `ViewId`, `NAV_ITEMS`, a sandboxed BrowserWindow, and a buildable `desktop/` package used by every later task.

- [ ] **Step 1: Create the package and build configuration**

Use this package shape, then install with `pnpm --dir desktop install` so the lockfile pins exact versions:

```json
{
  "name": "@lobster0/desktop",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "packageManager": "pnpm@10.14.0",
  "engines": { "node": ">=22.19.0" },
  "main": "out/main/index.js",
  "scripts": {
    "dev": "electron-vite dev",
    "build": "electron-vite build",
    "typecheck": "tsc --noEmit",
    "test": "vitest run"
  },
  "dependencies": {
    "@lobster0/pi-tui": "file:../tui",
    "react": "18.3.1",
    "react-dom": "18.3.1"
  },
  "devDependencies": {
    "@types/node": "24.10.1",
    "@types/react": "18.3.31",
    "@types/react-dom": "18.3.7",
    "@vitejs/plugin-react": "5.1.4",
    "electron": "43.2.0",
    "electron-vite": "5.0.0",
    "postcss": "8.5.12",
    "tailwindcss": "3.4.17",
    "typescript": "5.9.3",
    "vite": "7.3.0",
    "vitest": "4.1.10"
  }
}
```

- [ ] **Step 2: Write the failing navigation test**

```ts
import { describe, expect, it } from "vitest";
import { NAV_ITEMS } from "../src/renderer/navigation";

describe("desktop navigation", () => {
  it("exposes exactly the four approved views", () => {
    expect(NAV_ITEMS.map((item) => item.id)).toEqual([
      "home",
      "task",
      "automation",
      "settings",
    ]);
  });
});
```

- [ ] **Step 3: Run the focused test and confirm RED**

Run: `pnpm --dir desktop test -- navigation.test.ts`

Expected: FAIL because `src/renderer/navigation.ts` does not exist.

- [ ] **Step 4: Implement the minimal navigation and shell**

```ts
export type ViewId = "home" | "task" | "automation" | "settings";

export const NAV_ITEMS = [
  { id: "home", label: "首页" },
  { id: "task", label: "任务" },
  { id: "automation", label: "自动化" },
  { id: "settings", label: "设置" },
] as const satisfies readonly { id: ViewId; label: string }[];
```

`app.tsx` keeps one `useState<ViewId>("home")` and conditionally renders four semantic `<main>` sections. Do not install a routing library.

Create the BrowserWindow with the exact security settings:

```ts
webPreferences: {
  preload: join(__dirname, "../preload/index.js"),
  contextIsolation: true,
  nodeIntegration: false,
  sandbox: true,
}
```

Define the approved tokens in `styles.css`:

```css
:root {
  color-scheme: light;
  --canvas: #f7f8fa;
  --surface: #ffffff;
  --border: #e5e7eb;
  --text-primary: #1d2433;
  --text-secondary: #667085;
  --accent: #2563eb;
  --success: #15803d;
  --warning: #b45309;
  --danger: #b42318;
}
```

The Automation and Settings sections may contain only truthful W0 copy. Do not show buttons for unavailable operations.

- [ ] **Step 5: Run the shell checks**

Run:

```bash
pnpm --dir desktop test
pnpm --dir desktop typecheck
pnpm --dir desktop build
```

Expected: all commands exit 0 and `desktop/out/` is ignored by Git.

- [ ] **Step 6: Commit the W0 shell**

```bash
git add desktop
git commit -m "feat(desktop): 增加 light Electron 四界面壳"
```

### Task 2: Reuse the pi-tui Bridge client and bind workspace at process start

**Files:**
- Modify: `tui/package.json`
- Modify: `tui/src/bridge-client.ts`
- Modify: `tui/test/bridge-client.test.ts`
- Modify: `src/lobster0/bridge/__main__.py`
- Test: `tests/test_bridge_server.py`

**Interfaces:**
- Consumes: existing `BridgeClient`, `BridgeClient.spawnFromEnvironment()`, and protocol v1.
- Produces: exported `@lobster0/pi-tui/bridge-client`, `@lobster0/pi-tui/protocol`, `@lobster0/pi-tui/state`; `BridgeClient.hello(clientName, clientVersion)`; optional `LOBSTER0_WORKSPACE` mapped to Python `--workspace`.

- [ ] **Step 1: Write failing TypeScript tests for Desktop identity and argv**

Add an exported pure spawn-spec function and test its intended contract before implementing it:

```ts
const spec = buildBridgeSpawnSpec({
  LOBSTER0_PYTHON: "/opt/lobster0/python",
  LOBSTER0_HOME: "/state/lobster0",
  LOBSTER0_WORKSPACE: "/work/report",
});

assert.equal(spec.program, "/opt/lobster0/python");
assert.deepEqual(spec.args, [
  "-m", "lobster0.bridge",
  "--home", "/state/lobster0",
  "--workspace", "/work/report",
]);
```

Add a second test that calls `client.hello("lobster0-desktop", "0.1.0")` and asserts the exact hello payload. Preserve the zero-argument pi-tui default.

- [ ] **Step 2: Write the failing Python parser test**

```python
def test_bridge_parser_accepts_absolute_workspace_override(self) -> None:
    arguments = build_parser().parse_args(
        ["--home", "/state/lobster0", "--workspace", "/work/report"]
    )
    self.assertEqual(arguments.workspace, Path("/work/report"))
```

- [ ] **Step 3: Run focused tests and confirm RED**

Run:

```bash
pnpm --dir tui test
uv run python -m unittest tests.test_bridge_server -v
```

Expected: FAIL because `buildBridgeSpawnSpec`, parameterized `hello`, and `--workspace` do not exist.

- [ ] **Step 4: Implement the minimal shared client changes**

Use these exact signatures:

```ts
export interface BridgeSpawnSpec {
  program: string;
  args: string[];
  environment: NodeJS.ProcessEnv;
}

export function buildBridgeSpawnSpec(environment: NodeJS.ProcessEnv): BridgeSpawnSpec;

public async hello(
  clientName = "lobster0-pi-tui",
  clientVersion = "0.1.0",
): Promise<Record<string, JsonValue>>;
```

`buildBridgeSpawnSpec` must reject missing Python/Home and a non-absolute `LOBSTER0_WORKSPACE` with `BridgeRequestError("bridge_configuration", ...)`. It must keep `shell: false` and explicit argv.

Expose existing compiled modules in `tui/package.json`:

```json
"exports": {
  "./bridge-client": "./dist/bridge-client.js",
  "./protocol": "./dist/protocol.js",
  "./state": "./dist/state.js"
},
"files": ["dist"]
```

- [ ] **Step 5: Bind the workspace override in Python**

Change `_run` to this typed boundary:

```python
async def _run(home: str | None, workspace: Path | None) -> int:
    paths = build_state_paths(resolve_home(home))
    load_dotenv(Path.cwd() / ".env")
    overrides = {} if workspace is None else {"workspace": workspace}
    config = load_config(paths, overrides=overrides)
```

Add `parser.add_argument("--workspace", type=Path)` and pass it to `_run`. Rely on existing `load_config` absolute-path validation; do not write the selected path back into `config.toml`.

- [ ] **Step 6: Run compatibility checks**

Run:

```bash
uv run python -m unittest tests.test_bridge_server tests.test_config -v
pnpm --dir tui test
```

Expected: PASS; the existing pi-tui real Bridge handshake still sends `lobster0-pi-tui`.

- [ ] **Step 7: Commit the shared Bridge boundary**

```bash
git add tui src/lobster0/bridge tests/test_bridge_server.py
git commit -m "feat(bridge): 支持 Desktop identity 与 Workspace binding"
```

### Task 3: Add the fixed Main/Preload API and Bridge supervisor

**Files:**
- Create: `desktop/src/common/api.ts`
- Create: `desktop/src/main/bridge-service.ts`
- Create: `desktop/src/main/ipc.ts`
- Modify: `desktop/src/main/index.ts`
- Modify: `desktop/src/preload/index.ts`
- Test: `desktop/test/bridge-service.test.ts`
- Test: `desktop/test/preload.test.ts`

**Interfaces:**
- Consumes: `BridgeClient`, `ServerFrame`, and `PermissionMode` from `@lobster0/pi-tui` exports.
- Produces: `DesktopApi`, `DesktopBootstrap`, `BridgeService.start/stop/startTurn/cancelTurn/resolveApproval/setPermissionMode/restartWorkspace`, and fixed IPC channels.

- [ ] **Step 1: Define the failing public-contract tests**

The Preload factory must expose exactly these keys:

```ts
expect(Object.keys(api).sort()).toEqual([
  "bootstrap",
  "cancelTurn",
  "chooseWorkspace",
  "listAutomations",
  "listSessions",
  "loadSession",
  "onFrame",
  "resolveApproval",
  "setPermissionMode",
  "startTurn",
].sort());
```

The BridgeService test uses a structural fake and verifies:

```ts
await service.start();
expect(fake.helloCalls).toEqual([["lobster0-desktop", "0.1.0"]]);
await service.startTurn({ sessionKey: "task-1", text: "整理报告" });
expect(fake.turns).toEqual([["task-1", "整理报告"]]);
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `pnpm --dir desktop test -- bridge-service.test.ts preload.test.ts`

Expected: FAIL because the common contract, service, and Preload factory do not exist.

- [ ] **Step 3: Implement shared types and BridgeService**

Use these exact public shapes:

```ts
export interface DesktopBootstrap {
  coreVersion: string;
  model: string;
  workspace: string;
  language: string;
  permissionMode: PermissionMode;
  tools: string[];
  capabilities: string[];
}

export interface StartTurnInput {
  sessionKey: string;
  text: string;
}

export interface DesktopApi {
  bootstrap(): Promise<DesktopBootstrap>;
  startTurn(input: StartTurnInput): Promise<void>;
  cancelTurn(): Promise<void>;
  resolveApproval(approvalId: number, decision: ApprovalDecision): Promise<void>;
  setPermissionMode(mode: PermissionMode): Promise<PermissionMode>;
  listSessions(limit?: number): Promise<SessionSummary[]>;
  loadSession(sessionKey: string, limit?: number): Promise<SessionHistory>;
  listAutomations(limit?: number): Promise<AutomationSummary[]>;
  chooseWorkspace(): Promise<string | null>;
  onFrame(handler: (frame: ServerFrame) => void): () => void;
}
```

Methods whose Core request arrives in later tasks must reject with a stable `feature_unavailable` error until their handler is added; do not return fake data.

`BridgeService` owns one client, caches only the hello payload, forwards event frames, and prevents workspace restart while a Turn or Approval is active. The state machine is exactly `starting | idle | running | waiting_approval | stopped | failed`.

- [ ] **Step 4: Implement fixed IPC and Preload**

Use named channels such as `desktop:bootstrap` and `desktop:task:start`; do not expose `ipcRenderer.invoke(channel, ...)` to Renderer.

Preload must use a factory that is testable without Electron:

```ts
export function createDesktopApi(
  invoke: (channel: string, payload?: unknown) => Promise<unknown>,
  subscribe: (channel: string, handler: (value: unknown) => void) => () => void,
): DesktopApi;
```

Validate text length, positive approval ID, fixed decisions, permission modes, session keys, and limits in Main even though Core validates again.

- [ ] **Step 5: Run security and lifecycle tests**

Run:

```bash
pnpm --dir desktop test
pnpm --dir desktop typecheck
pnpm --dir desktop build
```

Expected: PASS; the built Renderer contains no `node:`, `electron`, `child_process`, or SQLite imports.

Also run this inspection and expect no matches:

```bash
rg -n "node:|electron|child_process|better-sqlite3" desktop/out/renderer
```

- [ ] **Step 6: Commit the process boundary**

```bash
git add desktop/src desktop/test
git commit -m "feat(desktop): 建立 typed Preload 与 Bridge supervisor"
```

### Task 4: Implement the single-Agent Task Workbench

**Files:**
- Create: `desktop/src/renderer/task-state.ts`
- Create: `desktop/src/renderer/task-workbench.tsx`
- Modify: `desktop/src/renderer/app.tsx`
- Modify: `desktop/src/renderer/styles.css`
- Test: `desktop/test/task-state.test.ts`

**Interfaces:**
- Consumes: `createInitialState`, `appendUser`, `reduceFrame`, `AppState`, and `ServerFrame` from `@lobster0/pi-tui/state` and `@lobster0/pi-tui/protocol`; `window.lobster0` from Task 3.
- Produces: `DesktopTaskState`, `createDesktopTaskState`, `reduceDesktopFrame`, and the functional Task Workbench.

- [ ] **Step 1: Write the failing reducer test**

```ts
it("projects a complete turn and approval without inventing state", () => {
  let state = createDesktopTaskState("task-1");
  state = reduceDesktopFrame(state, frame("event.turn_started", { turn_id: 7 }));
  state = reduceDesktopFrame(state, frame("event.model_text_delta", {
    turn_id: 7,
    text: "处理中",
  }));
  state = reduceDesktopFrame(state, frame("event.approval_required", {
    turn_id: 7,
    approval_id: 9,
    call_id: "call-9",
    tool_name: "write_file",
    summary: "写入报告",
    arguments: { path: "report.md" },
    grant_modes: ["once"],
  }));

  expect(state.run.busy).toBe(false);
  expect(state.run.pendingApproval?.approvalId).toBe(9);
  expect(state.status).toBe("waiting_approval");
});
```

Add cases for `turn_finished`, `turn_failed`, `turn_cancelled`, and `event.bridge_error` with stable user-facing text.

- [ ] **Step 2: Run the reducer test and confirm RED**

Run: `pnpm --dir desktop test -- task-state.test.ts`

Expected: FAIL because `task-state.ts` does not exist.

- [ ] **Step 3: Implement the wrapper reducer**

```ts
export type DesktopTaskStatus =
  | "idle"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface DesktopTaskState {
  sessionKey: string;
  status: DesktopTaskStatus;
  run: AppState;
  error: string | null;
}
```

Delegate known `event.*` frames to the existing `reduceFrame`. Add only Desktop status/error projection; do not fork Tool or telemetry logic.

- [ ] **Step 4: Implement the three-column Task Workbench**

The workbench must contain:

- left: current/recent Task list supplied by App;
- center: one scrollable timeline, one composer, cancel action, and inline Approval buttons;
- right: collapsed W1 result panel showing final text metadata only; no filesystem scanning or Artifact preview.

On submit, call `appendUser` locally only after `startTurn` resolves. Disable submit for blank text, running Turn, or pending Approval. Approval buttons must come from Core `grantModes`; denial is always allowed.

- [ ] **Step 5: Verify the focused UI state**

Run:

```bash
pnpm --dir desktop test
pnpm --dir desktop typecheck
pnpm --dir desktop build
```

Expected: PASS; one event subscription is installed and removed on React unmount.

- [ ] **Step 6: Commit the task loop UI**

```bash
git add desktop/src/renderer desktop/test/task-state.test.ts
git commit -m "feat(desktop): 打通 single-Agent Task Workbench"
```

### Task 5: Add Owner-scoped recent Tasks, history, and interrupted recovery

**Files:**
- Modify: `src/lobster0/storage/conversations.py`
- Create: `src/lobster0/bridge/conversations.py`
- Modify: `src/lobster0/runtime.py`
- Modify: `src/lobster0/bridge/protocol.py`
- Modify: `src/lobster0/bridge/server.py`
- Modify: `tui/src/protocol.ts`
- Test: `tests/test_conversations.py`
- Test: `tests/test_bridge_protocol.py`
- Test: `tests/test_bridge_server.py`
- Create: `tests/test_bridge_conversations.py`
- Modify: `desktop/src/main/bridge-service.ts`
- Modify: `desktop/src/main/ipc.ts`
- Modify: `desktop/src/renderer/app.tsx`
- Modify: `desktop/src/renderer/task-state.ts`

**Interfaces:**
- Consumes: existing Session/Message/Turn repositories and Desktop API placeholders from Task 3.
- Produces: `SessionRepository.get_cli`, `SessionRepository.list_cli`, `TurnRepository.interrupt_stale`, `ConversationConsole.list_sessions/history`, protocol requests `session.list` and `session.history`, and functional Home/recent Task selection.

- [ ] **Step 1: Write failing repository tests**

Cover Owner isolation, bounded order, and restart settlement:

```python
def test_cli_sessions_are_owner_scoped_and_newest_first(self) -> None:
    first = self.sessions.get_or_create_cli(self.owner.id, "task-old")
    second = self.sessions.get_or_create_cli(self.owner.id, "task-new")
    self.assertEqual(
        [session.external_conversation_id for session in self.sessions.list_cli(self.owner.id, 10)],
        [second.external_conversation_id, first.external_conversation_id],
    )

def test_stale_queued_and_running_turns_fail_as_runtime_interrupted(self) -> None:
    count = self.turns.interrupt_stale()
    self.assertEqual(count, 2)
    self.assertEqual(self.turns.get(self.running.id).error_code, "runtime_interrupted")
```

`waiting_approval`, completed, cancelled, and failed Turns must remain unchanged.

- [ ] **Step 2: Write failing protocol and console tests**

Accepted requests:

```json
{"v":1,"id":"s1","type":"session.list","payload":{"limit":20}}
{"v":1,"id":"s2","type":"session.history","payload":{"session_key":"task-1","limit":100}}
```

Reject extra keys, boolean limits, limits outside `1..50` for list or `1..200` for history, empty keys, and another Owner's Session.

The history response contains only bounded values:

```json
{
  "session_key": "task-1",
  "updated_at": "2026-08-09T00:00:00+00:00",
  "turns": [{"turn_id": 7, "status": "failed", "error_code": "runtime_interrupted"}],
  "messages": [{"role": "user", "content": "整理报告", "turn_id": 7}]
}
```

- [ ] **Step 3: Run Python tests and confirm RED**

Run:

```bash
uv run python -m unittest tests.test_conversations tests.test_bridge_protocol tests.test_bridge_conversations tests.test_bridge_server -v
```

Expected: FAIL because the repository methods, console, and request types do not exist.

- [ ] **Step 4: Implement the bounded repositories and query console**

Use exact signatures:

```python
def get_cli(self, user_id: int, conversation_id: str) -> Session | None: ...
def list_cli(self, user_id: int, limit: int = 50) -> tuple[Session, ...]: ...
def interrupt_stale(self) -> int: ...

class ConversationConsole:
    def list_sessions(self, owner_id: int, *, limit: int) -> dict[str, JsonValue]: ...
    def history(
        self,
        owner_id: int,
        *,
        session_key: str,
        limit: int,
    ) -> dict[str, JsonValue]: ...
```

`list_sessions` may perform one newest-Turn lookup per returned Session because the limit is 50. Mark the ceiling:

```python
# ponytail: bounded 50-session N+1; replace with one window query only if profiling requires it.
```

Never include Tool arguments, provider payloads, runtime snapshots, secrets, or internal filesystem paths in list/history responses.

- [ ] **Step 5: Wire runtime settlement and Bridge routing**

In `create_runtime`, call `TurnRepository(database).interrupt_stale()` once after migrations and before accepting new work. Instantiate one `ConversationConsole(database)` on `AgentRuntime`.

Add `session.list` and `session.history` to Python and TypeScript `RequestType`; advertise capabilities `sessions` and `history` in hello. Existing clients ignore them.

- [ ] **Step 6: Implement Home and history hydration**

`BridgeService.listSessions()` and `.loadSession()` call exact new requests. Home creates new keys with `crypto.randomUUID()`, lists recent Sessions, and opens Task Workbench on selection.

Add a pure function:

```ts
export function hydrateSession(history: SessionHistory): DesktopTaskState;
```

Map a last Turn with `status === "failed"` and `errorCode === "runtime_interrupted"` to Desktop status `interrupted`. Do not replay it automatically.

- [ ] **Step 7: Run Python, TUI, and Desktop tests**

Run:

```bash
uv run python -m unittest tests.test_conversations tests.test_bridge_protocol tests.test_bridge_conversations tests.test_bridge_server tests.test_runtime -v
pnpm --dir tui test
pnpm --dir desktop test
pnpm --dir desktop typecheck
```

Expected: PASS with no real provider or user database access.

- [ ] **Step 8: Commit durable Task history**

```bash
git add src/lobster0 tui/src/protocol.ts tests desktop
git commit -m "feat(desktop): 增加 durable Session history 与 interrupted 状态"
```

### Task 6: Make Automation, Settings, and Workspace selection functional

**Files:**
- Modify: `src/lobster0/bridge/protocol.py`
- Modify: `src/lobster0/bridge/server.py`
- Modify: `tui/src/protocol.ts`
- Modify: `tests/test_bridge_protocol.py`
- Modify: `tests/test_bridge_server.py`
- Modify: `desktop/src/common/api.ts`
- Modify: `desktop/src/main/bridge-service.ts`
- Modify: `desktop/src/main/ipc.ts`
- Modify: `desktop/src/main/index.ts`
- Modify: `desktop/src/renderer/app.tsx`
- Modify: `desktop/src/renderer/styles.css`
- Test: `desktop/test/bridge-service.test.ts`

**Interfaces:**
- Consumes: existing `ScheduledTaskRepository.list`, permission request, hello metadata, native Electron folder dialog, and workspace override from Task 2.
- Produces: protocol request `automation.list`, read-only Automation view, functional permission Settings, and user-driven Bridge restart for workspace switching.

- [ ] **Step 1: Write failing Automation boundary tests**

Accepted request:

```json
{"v":1,"id":"a1","type":"automation.list","payload":{"limit":50}}
```

Reject extra fields, boolean values, and limits outside `1..100`. Bridge response exposes only:

```json
{
  "enabled": true,
  "tasks": [{
    "task_id": 4,
    "name": "每日简报",
    "status": "active",
    "schedule_kind": "cron",
    "next_run_at": "2026-08-10T01:00:00+00:00"
  }]
}
```

Do not expose prompt text, delivery target, Skill arguments, budget JSON, or internal error details.

- [ ] **Step 2: Run protocol tests and confirm RED**

Run: `uv run python -m unittest tests.test_bridge_protocol tests.test_bridge_server -v`

Expected: FAIL because `automation.list` is unknown.

- [ ] **Step 3: Implement read-only Automation routing**

Use `ScheduledTaskRepository(self._runtime.database).list(owner_id=self._runtime.owner_id, limit=limit)` inside a private Bridge handler. Add hello fields `automation_enabled` and capability `automation_read`.

The Automation view renders enabled/disabled state and returned rows. It has no create, edit, pause, resume, run-now, or delete controls in W1.

- [ ] **Step 4: Implement Settings from existing hello and permission APIs**

Settings displays Core version, model, workspace name, tools, capabilities, and current permission mode. Only the four exact modes are selectable; disable the control during running or waiting Approval.

- [ ] **Step 5: Implement user-driven workspace switching**

Main uses `dialog.showOpenDialog({ properties: ["openDirectory"] })`. A cancelled picker returns `null`. A selected absolute path is passed to `BridgeService.restartWorkspace(path)`, which:

1. rejects unless state is `idle`;
2. gracefully shuts down the current Bridge;
3. starts a new Bridge with `LOBSTER0_WORKSPACE=path`;
4. requires a successful Desktop hello before publishing the new bootstrap;
5. restores the previous Bridge configuration if startup fails.

Renderer never receives a generic filesystem API. It receives only the selected workspace display path after Core hello succeeds.

- [ ] **Step 6: Add restart tests**

Test idle success, picker cancellation, running rejection, and failed-new-Bridge rollback. Assert that no path is persisted to `config.toml` and no task is automatically replayed.

- [ ] **Step 7: Run all W0/W1 focused checks**

Run:

```bash
uv run python -m unittest tests.test_bridge_protocol tests.test_bridge_server -v
pnpm --dir tui test
pnpm --dir desktop test
pnpm --dir desktop typecheck
pnpm --dir desktop build
```

Expected: PASS; all four approved views now show truthful data or controls.

- [ ] **Step 8: Commit the remaining views**

```bash
git add src/lobster0/bridge tui/src/protocol.ts tests desktop
git commit -m "feat(desktop): 接入 Automation、Settings 与 Workspace switch"
```

### Task 7: Cross-process smoke, documentation, and final W0/W1 gate

**Files:**
- Create: `desktop/test/python-bridge.test.ts`
- Modify: `README.md`
- Modify: `docs/engineering/README.md`
- Modify: `docs/architecture/20260809_通用桌面Agent工作台设计.md`

**Interfaces:**
- Consumes: all W0/W1 public APIs and existing offline Bridge initialization pattern from `tui/test/python-bridge.test.ts`.
- Produces: one real Desktop-client/Python-Bridge handshake test, reproducible local development commands, and verified implementation status.

- [ ] **Step 1: Write the real-process smoke test**

Reuse the existing temporary-home pattern, but call the shared client with Desktop identity and a temporary workspace:

```ts
const client = BridgeClient.spawnFromEnvironment({
  ...environment,
  LOBSTER0_WORKSPACE: workspace,
});
try {
  const hello = await client.hello("lobster0-desktop", "0.1.0");
  assert.equal(hello.protocol, 1);
  assert.equal(hello.workspace, basename(workspace));
  assert.equal(Array.isArray(hello.capabilities), true);
  await client.shutdown();
} finally {
  client.kill();
}
```

The test uses `offline-smoke-key`, never calls a provider, and removes its temporary state/workspace.

- [ ] **Step 2: Run the cross-process smoke**

Run: `LOBSTER0_PYTHON=.venv/bin/python pnpm --dir desktop test -- python-bridge.test.ts`

Expected: PASS with no network access and stdout containing only protocol frames.

- [ ] **Step 3: Document exact local commands**

Add this development flow to `README.md`:

```bash
uv sync --extra dev
pnpm --dir tui install
pnpm --dir tui build
pnpm --dir desktop install
LOBSTER0_PYTHON=.venv/bin/python \
LOBSTER0_HOME=/absolute/path/to/lobster0-home \
pnpm --dir desktop dev
```

State explicitly that this is `W0/W1 DEVELOPMENT BUILD`: no installer, signing, Artifact preview, Sub-agent, or external Agent adapter.

- [ ] **Step 4: Run all repository gates**

Run:

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
pnpm --dir tui test
pnpm --dir desktop test
pnpm --dir desktop typecheck
pnpm --dir desktop build
uv run lobster0 eval run --suite automation --repeat 20 --json --root evals/scenarios
uv run python scripts/validate_docs.py
git diff --check
```

Expected:

- Python, TUI, Desktop, Ruff, docs, and diff checks exit 0;
- Automation reports all 15 cases passing in all 20 repeats;
- no live provider, Channel, or personal data is used;
- `git status --short` contains only intended W0/W1 files.

- [ ] **Step 5: Perform one credential-free manual light-theme smoke**

Using an initialized test Lobster0 home and a disposable workspace, verify:

1. all four views are reachable by mouse and keyboard;
2. Home creates a Task and opens Task Workbench;
3. an offline Bridge handshake shows real Core metadata;
4. permission changes persist through the existing Core API;
5. workspace switching restarts Core without replay;
6. app close performs `bridge.shutdown` and leaves no child process.

Record only PASS/FAIL and stable error codes. Do not record prompts, provider responses, paths, or credentials.

- [ ] **Step 6: Optionally perform an Owner-authorized LIVE smoke**

Only when the Owner supplies a disposable Provider configuration, run one harmless prompt and verify streaming, Approval/deny, cancel, and terminal state. If authorization or credentials are absent, record `LIVE PENDING`; this does not block local `IMPLEMENTATION PASS`.

- [ ] **Step 7: Update implementation status truthfully**

Only after automated and credential-free manual gates pass, change the design status from `IMPLEMENTATION PENDING` to `W0/W1 LOCAL IMPLEMENTATION PASS`. Record the separate LIVE status as `PASS` or `PENDING`; installer/signing and W2/W3 remain planned.

- [ ] **Step 8: Commit the verified W0/W1 slice**

```bash
git add README.md docs desktop/test/python-bridge.test.ts
git commit -m "docs(desktop): 记录 W0/W1 development gate"
```

## Plan Self-Review Result

- Spec coverage: W0 shell, W1 task loop, four views, light theme, security boundary, workspace selection, history, interruption, Automation read model, and Settings all map to Tasks 1–7.
- Explicitly deferred: W2 Artifact content/preview, W3 Sub-agent, packaging/signing, Office editors, external Agent adapters, cloud/team features, and dark theme.
- Type consistency: `DesktopApi`, `BridgeService`, `DesktopTaskState`, `SessionSummary`, `SessionHistory`, and request names are defined once and reused by later tasks.
- No implementation step requires networked tests, real credentials, user state, or direct Renderer access to local capabilities.
