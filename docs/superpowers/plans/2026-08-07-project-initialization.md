# MiniClaw Project Initialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Initialize `/Users/nedonion/PycharmProjects/miniclaw` as a runnable Python 3.12 open-source project and move the renamed MiniClaw PRD into an EvalHub-style documentation layout.

**Architecture:** Use a minimal `src` package with a standard-library `argparse` CLI and `unittest` smoke tests. Keep product, architecture, getting-started, development, and implementation records under `docs/`; no Agent runtime, Feishu integration, database, or deployment code is implemented during repository initialization.

**Tech Stack:** Python 3.12+, `uv`, `setuptools`, `argparse`, `unittest`, Ruff, Markdown, Mermaid.

## Global Constraints

- The project, repository, import package, and CLI are named `miniclaw`; prose uses `MiniClaw`.
- The existing PRD must use `MiniClaw/miniclaw` consistently and contain no legacy working name.
- Preserve the existing `.idea/` and `.venv/` directories in the target project.
- Do not add runtime dependencies during initialization.
- Follow an EvalHub-style `AGENTS.md`, `docs/README.md`, and categorized documentation layout.
- The initialized CLI must run without model credentials or network access.

---

### Task 1: Repository metadata and documentation

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `LICENSE`
- Create: `README.md`
- Create: `AGENTS.md`
- Create: `docs/README.md`
- Create: `docs/product/20260807_产品需求文档.md`
- Create: `docs/architecture/20260807_系统架构.md`
- Create: `docs/getting-started/20260807_本地运行指南.md`
- Create: `docs/development/20260807_Codex对话沉淀工作流.md`
- Create: `docs/superpowers/plans/2026-08-07-project-initialization.md`

**Interfaces:**
- Consumes: the confirmed MiniClaw product scope and the layout conventions in EvalHub.
- Produces: repository rules, navigation, setup instructions, architecture boundaries, and the canonical PRD.

- [x] **Step 1: Write project metadata**

Create `.gitignore` rules for Python caches, local environments, IDE metadata, credentials, runtime data, logs, build artifacts, and MiniClaw's personal workspace.

Create `.env.example` with non-secret placeholders for the OpenAI-compatible provider and Feishu application settings.

Create an MIT license attributed to MiniClaw contributors.

- [x] **Step 2: Rename and place the PRD**

Copy the existing PRD to `docs/product/20260807_产品需求文档.md`, replace the legacy working name with
`MiniClaw/miniclaw` throughout, and rewrite the naming section so it records `MiniClaw` as the accepted
project name instead of rejecting it.

- [x] **Step 3: Create the documentation index and focused guides**

Create `docs/README.md` linking the PRD, architecture, local guide, development workflow, and implementation plan. Create a concise architecture document that fixes the channel/core/tool/policy/storage/evolution boundaries, plus a local guide whose commands execute from the repository root.

- [x] **Step 4: Create repository-level agent rules**

Create `AGENTS.md` that specifies Python 3.12+, the `src/miniclaw` package, standard-library-first dependencies, Chinese docstrings, deterministic offline unit tests, security boundaries, documentation synchronization, and completion checks.

- [x] **Step 5: Check documentation links and stale names**

Run:

```bash
rg -n "L[o]blet|l[o]blet" README.md AGENTS.md docs
```

Expected: no matches.

Run:

```bash
test -f docs/product/20260807_产品需求文档.md
```

Expected: exit code 0.

### Task 2: Minimal installable Python CLI

**Files:**
- Create: `pyproject.toml`
- Create: `src/miniclaw/__init__.py`
- Create: `src/miniclaw/__main__.py`
- Create: `src/miniclaw/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Python 3.12+, package name `miniclaw`, and standard-library-only runtime constraints.
- Produces: `miniclaw.cli.build_parser() -> argparse.ArgumentParser`, `miniclaw.cli.main(argv: Sequence[str] | None = None) -> int`, console command `miniclaw`, and module command `python -m miniclaw`.

- [x] **Step 1: Write the CLI smoke tests**

```python
import contextlib
import io
import unittest

from miniclaw.cli import main


class CliTest(unittest.TestCase):
    def test_no_arguments_prints_help(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("MiniClaw", output.getvalue())

    def test_version_option_prints_version(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, "0"):
                main(["--version"])
        self.assertIn("miniclaw 0.1.0", output.getvalue())
```

- [x] **Step 2: Run the test to verify it fails before implementation**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: FAIL because `miniclaw.cli` does not exist.

- [x] **Step 3: Implement the minimal CLI**

`src/miniclaw/cli.py` must use `argparse`, expose a parser with `--version`, print help when no command is supplied, and return `0`. `src/miniclaw/__main__.py` must raise `SystemExit(main())`; `src/miniclaw/__init__.py` must define `__version__ = "0.1.0"`.

- [x] **Step 4: Configure packaging and tools**

Create `pyproject.toml` with:

```toml
[project]
name = "miniclaw"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
miniclaw = "miniclaw.cli:main"
```

Use `setuptools` with package discovery under `src`, `unittest` for tests, and Ruff rules `E`, `F`, `I`, `UP`, and `B` with a 100-character line width.

- [x] **Step 5: Run focused verification**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: 2 tests pass.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m miniclaw --version
```

Expected: `miniclaw 0.1.0`.

### Task 3: Initialize Git and verify the repository

**Files:**
- Modify: `.git/` through `git init`
- Create: `uv.lock` through `uv sync`
- Verify: all created files

**Interfaces:**
- Consumes: the completed metadata, documentation, package, and tests.
- Produces: a cleanly initialized local Git repository ready for the user's first commit.

- [x] **Step 1: Initialize Git without creating a commit**

Run:

```bash
git init
```

Expected: `.git/` exists and the user's `.idea/` and `.venv/` remain untouched and ignored.

- [x] **Step 2: Sync the project in the existing virtual environment**

Run:

```bash
uv sync --extra dev
```

Expected: the local package and Ruff install, and `uv.lock` records the resolved development environment.

- [x] **Step 3: Run all local checks**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check --no-cache .
.venv/bin/miniclaw --version
git diff --check
git status --short
```

Expected: unit tests and Ruff pass, the CLI prints `miniclaw 0.1.0`, `git diff --check` reports no whitespace errors, and only intended untracked project files appear in Git status.

- [x] **Step 4: Review scope**

Confirm the repository contains no implementation claims for the Agent loop, Feishu, persistence, skills, or evolution system. Those capabilities remain PRD milestones and are not represented as completed features.
