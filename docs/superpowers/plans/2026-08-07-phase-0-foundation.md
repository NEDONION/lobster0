# MiniClaw Phase 0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a safe, idempotent local foundation with resolved state paths, validated TOML
configuration, versioned SQLite migrations, a single Owner, `miniclaw init`, and offline
`miniclaw doctor`.

**Architecture:** Keep Phase 0 synchronous and standard-library-only. `paths.py` owns filesystem
locations, `config.py` owns parsing and precedence, `storage/` owns SQLite, `bootstrap.py` composes
initialization, and `doctor.py` inspects the same public boundaries without repairing them.

**Tech Stack:** Python 3.12+, `argparse`, `dataclasses`, `pathlib`, `tomllib`, `sqlite3`,
`importlib.resources`, `unittest`, Ruff, `uv`.

## Global Constraints

- Work on branch `phase-0-foundation` in an isolated worktree.
- Keep runtime dependencies empty; use the Python 3.12 standard library.
- Use `src/miniclaw/`, Chinese docstrings, UTF-8 text, aware UTC timestamps, and 100-character lines.
- `MINICLAW_HOME` and configured Workspace paths must resolve to absolute paths; relative paths fail.
- State directories use owner-only permissions when created; existing user files are never overwritten.
- Configuration precedence is defaults < `config.toml` < `MINICLAW_*` environment < explicit override.
- SQLite enables foreign keys, WAL, and a 5,000 ms busy timeout on every connection.
- Tests are offline `unittest` cases using temporary directories and real files/databases.
- Planning claims remain marked as targets; README only advertises behavior verified in this phase.

---

### Task 1: Safe state paths

**Files:**
- Create: `src/miniclaw/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: optional explicit home and a `Mapping[str, str]` environment.
- Produces: `PathConfigurationError`, `StatePaths`,
  `resolve_home(value, environ) -> Path`, and `build_state_paths(home) -> StatePaths`.

- [x] **Step 1: Write failing path tests**

The production change caught by these tests is accepting a relative state root or deriving a state
file outside the selected root.

```python
import tempfile
import unittest
from pathlib import Path

from miniclaw.paths import (
    PathConfigurationError,
    build_state_paths,
    resolve_home,
)


class StatePathsTest(unittest.TestCase):
    def test_environment_home_is_expanded_and_all_paths_stay_under_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = resolve_home(None, {"MINICLAW_HOME": directory})
            paths = build_state_paths(home)

        self.assertEqual(home, Path(directory).resolve())
        self.assertEqual(paths.database, home / "miniclaw.db")
        self.assertEqual(paths.workspace, home / "workspace")
        self.assertTrue(all(path == home or home in path.parents for path in paths.directories))

    def test_relative_home_is_rejected(self) -> None:
        with self.assertRaisesRegex(PathConfigurationError, "absolute"):
            resolve_home("relative/state", {})
```

- [x] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_paths -v
```

Expected: import failure because `miniclaw.paths` does not exist.

- [x] **Step 3: Implement the path boundary**

Use a frozen, slotted dataclass with these fields:

```python
@dataclass(frozen=True, slots=True)
class StatePaths:
    home: Path
    config: Path
    database: Path
    soul: Path
    user: Path
    memory_file: Path
    memory_dir: Path
    prompts: Path
    prompt_versions: Path
    skills: Path
    skill_versions: Path
    evals: Path
    eval_baseline: Path
    eval_failures: Path
    workspace: Path
    logs: Path
    run: Path

    @property
    def directories(self) -> tuple[Path, ...]:
        return (
            self.home,
            self.memory_dir,
            self.prompts,
            self.prompt_versions,
            self.skills,
            self.skill_versions,
            self.evals,
            self.eval_baseline,
            self.eval_failures,
            self.workspace,
            self.logs,
            self.run,
        )
```

`resolve_home()` chooses explicit value, then `MINICLAW_HOME`, then `~/.miniclaw`; it calls
`expanduser()`, rejects a non-absolute result, and returns `resolve(strict=False)`.

- [x] **Step 4: Verify GREEN and commit**

Run:

```bash
.venv/bin/python -m unittest tests.test_paths -v
.venv/bin/ruff check --no-cache src/miniclaw/paths.py tests/test_paths.py
git add src/miniclaw/paths.py tests/test_paths.py
git commit -m "feat: add safe state paths"
```

Expected: focused tests and Ruff pass; commit contains only path code and tests.

### Task 2: Validated configuration and precedence

**Files:**
- Create: `src/miniclaw/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `StatePaths`, an optional environment mapping, and optional explicit string overrides.
- Produces: `ConfigError`, `AgentConfig`, `ProviderConfig`, `WorkspaceConfig`, `AppConfig`, and
  `load_config(paths, environ, overrides) -> AppConfig`.

- [x] **Step 1: Write failing default and precedence tests**

The production changes caught are reversing precedence, loading an API key value into printable
configuration, or accepting a relative Workspace.

```python
import tempfile
import unittest
from pathlib import Path

from miniclaw.config import ConfigError, load_config
from miniclaw.paths import build_state_paths


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        home = Path(self.temporary_directory.name).resolve()
        self.paths = build_state_paths(home)
        self.workspace = home / "custom-workspace"

    def test_environment_and_explicit_values_override_toml(self) -> None:
        self.paths.config.write_text(
            '[agent]\nmodel = "file-model"\n'
            '[provider]\nbase_url = "https://file.example/v1"\n'
            '[workspace]\npath = "' + self.workspace.as_posix() + '"\n',
            encoding="utf-8",
        )

        config = load_config(
            self.paths,
            {"MINICLAW_MODEL_NAME": "env-model"},
            {"model": "cli-model"},
        )

        self.assertEqual(config.agent.model, "cli-model")
        self.assertEqual(config.provider.base_url, "https://file.example/v1")
        self.assertEqual(config.provider.api_key_env, "MINICLAW_MODEL_API_KEY")

    def test_relative_workspace_is_rejected(self) -> None:
        self.paths.config.write_text('[workspace]\npath = "relative"\n', encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "workspace.path"):
            load_config(self.paths, {}, {})
```

- [x] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_config -v
```

Expected: import failure because `miniclaw.config` does not exist.

- [x] **Step 3: Implement typed configuration**

Use these public dataclasses and defaults:

```python
@dataclass(frozen=True, slots=True)
class AgentConfig:
    model: str = "provider/model"
    max_tool_iterations: int = 8
    context_budget_tokens: int = 32_000
    tool_result_max_chars: int = 20_000


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "MINICLAW_MODEL_API_KEY"
    timeout_seconds: int = 120


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    path: Path
    read_only_roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class AppConfig:
    agent: AgentConfig
    provider: ProviderConfig
    workspace: WorkspaceConfig
```

Parse with `tomllib.load()`. Reject unknown top-level keys, unknown keys inside the three Phase 0
sections, booleans where integers are expected, empty strings, non-positive limits, relative paths,
and URL credentials. Never resolve the value stored in `api_key_env`; it is an environment variable
name, not a secret. Wrap `TOMLDecodeError`, `OSError`, and validation failures in `ConfigError` without
including secret environment values.

- [x] **Step 4: Add malformed input tests**

```python
def test_malformed_toml_reports_config_path(self) -> None:
    self.paths.config.write_text("[agent\n", encoding="utf-8")

    with self.assertRaisesRegex(ConfigError, "config.toml"):
        load_config(self.paths, {}, {})

def test_boolean_is_not_accepted_as_integer(self) -> None:
    self.paths.config.write_text("[agent]\nmax_tool_iterations = true\n", encoding="utf-8")

    with self.assertRaisesRegex(ConfigError, "max_tool_iterations"):
        load_config(self.paths, {}, {})
```

- [x] **Step 5: Verify GREEN and commit**

Run:

```bash
.venv/bin/python -m unittest tests.test_config -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check --no-cache src/miniclaw/config.py tests/test_config.py
git add src/miniclaw/config.py tests/test_config.py
git commit -m "feat: add validated configuration"
```

Expected: all tests pass and no secret values appear in failure text.

### Task 3: SQLite migration and Owner repository

**Files:**
- Create: `src/miniclaw/storage/__init__.py`
- Create: `src/miniclaw/storage/database.py`
- Create: `src/miniclaw/storage/migrations.py`
- Create: `src/miniclaw/storage/repositories.py`
- Create: `src/miniclaw/storage/schema.sql`
- Modify: `pyproject.toml`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: the database path from `StatePaths`.
- Produces: `Database(path)`, `apply_migrations(database) -> tuple[int, ...]`,
  `current_schema_version(database) -> int`, `Owner`, and
  `OwnerRepository.get_or_create(display_name="Owner") -> Owner`.

- [x] **Step 1: Write failing migration tests**

The production changes caught are omitting a core table, failing to enable a required PRAGMA, or
reapplying migration 1.

```python
import tempfile
import unittest
from pathlib import Path

from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import OwnerRepository


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "miniclaw.db"

    def test_migration_creates_schema_once_with_required_pragmas(self) -> None:
        database = Database(self.database_path)

        first = apply_migrations(database)
        second = apply_migrations(database)

        with database.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertEqual(first, (1,))
        self.assertEqual(second, ())
        self.assertTrue({"schema_migrations", "users", "sessions", "audit_events"} <= tables)
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(busy_timeout, 5000)
```

- [x] **Step 2: Verify migration RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_storage.StorageTest.test_migration_creates_schema_once_with_required_pragmas -v
```

Expected: import failure because `miniclaw.storage` does not exist.

- [x] **Step 3: Implement connection and migration primitives**

`Database.connect()` requires an existing parent directory, opens the database path, sets
`sqlite3.Row`, and executes:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
```

`apply_migrations()` loads `schema.sql` through `importlib.resources`, checks
`schema_migrations`, and applies version 1 in `BEGIN IMMEDIATE ... COMMIT`. On failure it rolls back
and raises `MigrationError`. Add this package-data declaration:

```toml
[tool.setuptools.package-data]
"miniclaw.storage" = ["schema.sql"]
```

Use the complete version-1 DDL from the approved engineering specification; do not add repository
classes for tables not used in Phase 0.

- [x] **Step 4: Write failing Owner idempotency test**

The production change caught is inserting a second Owner or overwriting its display name on a
repeated initialization.

```python
def test_owner_is_created_once_and_preserved(self) -> None:
    database = Database(self.database_path)
    apply_migrations(database)
    repository = OwnerRepository(database)

    first = repository.get_or_create("Owner")
    second = repository.get_or_create("Replacement")

    self.assertEqual(first.id, second.id)
    self.assertEqual(second.display_name, "Owner")
```

- [x] **Step 5: Verify Owner RED, implement, and verify GREEN**

Run the Owner test before implementation; expect `ImportError` for `OwnerRepository`. Implement
`get_or_create()` using `BEGIN IMMEDIATE`, select the lowest existing user ID, insert only when no
row exists, and store `datetime.now(UTC).isoformat()`.

Run:

```bash
.venv/bin/python -m unittest tests.test_storage -v
.venv/bin/ruff check --no-cache src/miniclaw/storage tests/test_storage.py
git add pyproject.toml src/miniclaw/storage tests/test_storage.py
git commit -m "feat: add sqlite migrations and owner repository"
```

Expected: storage tests and Ruff pass; a second migration run is empty and a second Owner call does
not add or rename a row.

### Task 4: Idempotent bootstrap and `miniclaw init`

**Files:**
- Create: `src/miniclaw/bootstrap.py`
- Modify: `src/miniclaw/cli.py`
- Test: `tests/test_bootstrap.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `StatePaths`, config loader, migrations, and Owner repository.
- Produces: `InitResult`, `initialize_state(paths) -> InitResult`, and CLI
  `miniclaw init [--home PATH]`.

- [x] **Step 1: Write failing bootstrap idempotency test**

The production changes caught are replacing user-owned Markdown/config content or adding a second
Owner during repeated initialization.

```python
import tempfile
import unittest
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths


class BootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = self.temporary_directory.name

    def test_repeated_initialization_preserves_user_files_and_owner(self) -> None:
        paths = build_state_paths(Path(self.directory).resolve())
        first = initialize_state(paths)
        paths.user.write_text("My profile\n", encoding="utf-8")

        second = initialize_state(paths)

        self.assertEqual(first.owner.id, second.owner.id)
        self.assertEqual(paths.user.read_text(encoding="utf-8"), "My profile\n")
        self.assertEqual(paths.config.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(path.is_dir() for path in paths.directories))
```

- [x] **Step 2: Verify bootstrap RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_bootstrap -v
```

Expected: import failure because `miniclaw.bootstrap` does not exist.

- [x] **Step 3: Implement minimal initialization**

Create every `StatePaths.directories` entry with mode `0o700`. Create only missing files using
exclusive mode and UTF-8:

- `config.toml`: `[agent]`, `[provider]`, and `[workspace]` values matching `load_config()` defaults;
- `SOUL.md`: `# MiniClaw\n`;
- `USER.md`: `# User\n`;
- `MEMORY.md`: `# Long-term Memory\n`.

Set newly created private files to `0o600`, load the resulting config, apply migrations, and call
`OwnerRepository.get_or_create()`. Return:

```python
@dataclass(frozen=True, slots=True)
class InitResult:
    paths: StatePaths
    owner: Owner
    applied_migrations: tuple[int, ...]
    created_files: tuple[Path, ...]
```

- [x] **Step 4: Write failing CLI init test**

```python
def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = main(arguments)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def test_init_creates_state_and_is_repeatable(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        first_code, first_output, first_error = run_cli(["init", "--home", directory])
        second_code, second_output, second_error = run_cli(["init", "--home", directory])

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual((first_error, second_error), ("", ""))
        self.assertIn("Initialized MiniClaw", first_output)
        self.assertIn("already initialized", second_output)
        self.assertTrue((Path(directory) / "miniclaw.db").is_file())
```

`run_cli()` is a test-only helper that redirects stdout/stderr around the real `main()` function.

- [x] **Step 5: Verify CLI RED, implement, and commit**

Add a required `init` subparser only when a command is present; preserve no-argument help and
`--version`. `main()` catches `PathConfigurationError`, `ConfigError`, `MigrationError`, and
`OSError`, writes `error: <safe message>` to stderr, and returns 2 for path/config errors or 5 for
storage/runtime failures.

Run:

```bash
.venv/bin/python -m unittest tests.test_bootstrap tests.test_cli -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check --no-cache src tests
git add src/miniclaw/bootstrap.py src/miniclaw/cli.py tests/test_bootstrap.py tests/test_cli.py
git commit -m "feat: add idempotent init command"
```

Expected: repeated init succeeds without replacing files or creating another Owner.

### Task 5: Offline diagnostics and `miniclaw doctor`

**Files:**
- Create: `src/miniclaw/doctor.py`
- Modify: `src/miniclaw/cli.py`
- Create: `tests/test_doctor.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: initialized `StatePaths`, config loader, and `Database`.
- Produces: `CheckStatus`, `CheckResult`, `run_local_checks(paths, environ)`, and CLI
  `miniclaw doctor [--home PATH]`.

- [x] **Step 1: Write failing healthy-state diagnostics test**

The production changes caught are a doctor that reports success without actually parsing config,
opening SQLite, or checking Workspace.

```python
import tempfile
import unittest
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.paths import build_state_paths


class DoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = self.temporary_directory.name

    def test_initialized_state_passes_all_local_checks(self) -> None:
        paths = build_state_paths(Path(self.directory).resolve())
        initialize_state(paths)

        results = run_local_checks(paths, {})

        self.assertTrue(results)
        self.assertTrue(all(result.status is CheckStatus.PASS for result in results))
        self.assertEqual(
            {result.name for result in results},
            {"state_home", "config", "workspace", "database", "permissions"},
        )
```

- [x] **Step 2: Verify doctor RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_doctor -v
```

Expected: import failure because `miniclaw.doctor` does not exist.

- [x] **Step 3: Implement real local checks**

Use `StrEnum` values `pass`, `warn`, and `fail`. Each `CheckResult` contains `name`, `status`, and a
safe human-readable `message`. Checks must:

1. confirm the state root and expected directories exist;
2. parse config and validate it through `load_config()`;
3. confirm configured Workspace exists, is a directory, and is writable;
4. open SQLite, run `PRAGMA integrity_check`, and compare schema version with version 1;
5. ensure the state root and config are not group/world accessible on POSIX.

Do not create or repair state, call a network, or read an API key value.

- [x] **Step 4: Add corrupt-config and CLI tests**

```python
def test_corrupt_config_fails_without_exposing_file_contents(self) -> None:
    paths = build_state_paths(Path(self.directory).resolve())
    initialize_state(paths)
    paths.config.write_text('[provider\napi_key = "super-secret"\n', encoding="utf-8")

    results = run_local_checks(paths, {})
    config_result = next(result for result in results if result.name == "config")

    self.assertIs(config_result.status, CheckStatus.FAIL)
    self.assertNotIn("super-secret", config_result.message)

def test_doctor_returns_two_for_corrupt_config(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_cli(["init", "--home", directory])
        Path(directory, "config.toml").write_text("[agent\n", encoding="utf-8")

        code, output, error = run_cli(["doctor", "--home", directory])

        self.assertEqual(code, 2)
        self.assertIn("[FAIL] config", output)
        self.assertEqual(error, "")
```

- [x] **Step 5: Verify GREEN and commit**

Run:

```bash
.venv/bin/python -m unittest tests.test_doctor tests.test_cli -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check --no-cache src tests
git add src/miniclaw/doctor.py src/miniclaw/cli.py tests/test_doctor.py tests/test_cli.py
git commit -m "feat: add offline doctor checks"
```

Expected: initialized state is healthy, corrupt TOML yields CLI exit code 2, and the secret fixture
does not appear in output.

### Task 6: User documentation and Phase 0 release gate

**Files:**
- Modify: `README.md`
- Modify: `docs/getting-started/20260807_本地运行指南.md`
- Modify: `docs/architecture/20260807_系统架构.md`
- Modify: `docs/superpowers/plans/2026-08-07-phase-0-foundation.md`

**Interfaces:**
- Consumes: verified CLI behavior from Tasks 1-5.
- Produces: commands and status descriptions that match the implementation.

- [x] **Step 1: Update only verified claims**

Change repository status from scaffold to Phase 0 foundation. Document:

```bash
uv run miniclaw init
uv run miniclaw doctor
MINICLAW_HOME=/absolute/path uv run miniclaw init
```

List created local files, configuration precedence, exit codes 0/2/5, and the fact that doctor is
offline. Keep Agent chat, Provider calls, Tools, and Feishu explicitly unimplemented.

- [x] **Step 2: Run the release gate in a temporary home**

Run:

```bash
phase0_home="$(mktemp -d)"
.venv/bin/miniclaw init --home "$phase0_home"
.venv/bin/miniclaw init --home "$phase0_home"
.venv/bin/miniclaw doctor --home "$phase0_home"
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check --no-cache .
git diff --check
```

Expected: both init calls return 0, doctor reports five PASS checks, all tests and Ruff pass, and
`git diff --check` is empty. Remove only the exact temporary directory printed by `mktemp -d` after
verifying its value is non-empty and starts with the platform temporary root.

- [x] **Step 3: Mark this plan's completed checkboxes and commit docs**

Run:

```bash
git add README.md docs/architecture/20260807_系统架构.md \
  docs/getting-started/20260807_本地运行指南.md \
  docs/superpowers/plans/2026-08-07-phase-0-foundation.md
git commit -m "docs: document phase 0 foundation"
git status --short --branch
```

Expected: the branch is clean and contains focused commits for paths, config, storage, init, doctor,
and documentation.
