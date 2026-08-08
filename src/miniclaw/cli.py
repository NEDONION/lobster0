"""MiniClaw 唯一 TUI 入口与本地维护命令。"""

import argparse
import asyncio
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from miniclaw import __version__
from miniclaw.bootstrap import BootstrapError, initialize_state
from miniclaw.config import ConfigError
from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.env import DotEnvError
from miniclaw.evals.cases import EvalCaseError, load_cases
from miniclaw.evals.runner import run_offline_suite
from miniclaw.gateway import GatewayConfigError, GatewayRuntimeError, run_gateway
from miniclaw.paths import PathConfigurationError, build_state_paths, resolve_home
from miniclaw.storage.database import DatabaseError
from miniclaw.storage.migrations import MigrationError
from miniclaw.tui import run_tui


def build_parser() -> argparse.ArgumentParser:
    """创建裸 TUI 与 init/doctor/eval 维护命令的解析器。"""
    parser = argparse.ArgumentParser(
        prog="miniclaw",
        description="MiniClaw — a tiny self-hosted personal agent. Run bare to open the TUI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--home", help="absolute MiniClaw state directory")
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser("init", help="initialize local MiniClaw state")
    init_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute MiniClaw state directory",
    )
    doctor_parser = subparsers.add_parser("doctor", help="check local MiniClaw state")
    doctor_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute MiniClaw state directory",
    )
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="run the long-lived Feishu gateway",
    )
    gateway_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute MiniClaw state directory",
    )
    eval_parser = subparsers.add_parser("eval", help="run deterministic agent regressions")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", required=True)
    eval_list = eval_subparsers.add_parser("list", help="list versioned eval cases")
    eval_validate = eval_subparsers.add_parser("validate", help="validate eval case files")
    eval_run = eval_subparsers.add_parser("run", help="run an eval suite")
    for child in (eval_list, eval_validate, eval_run):
        child.add_argument(
            "--root",
            type=Path,
            default=Path.cwd() / "evals" / "scenarios",
            help="directory containing versioned JSONL cases",
        )
    eval_run.add_argument("--suite", choices=("offline",), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """运行裸 TUI，或执行不与聊天竞争的本地维护命令。"""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "eval":
        return _run_eval(arguments)

    home = getattr(arguments, "command_home", None) or arguments.home
    try:
        paths = build_state_paths(resolve_home(home))
    except (PathConfigurationError, ConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if arguments.command == "doctor":
        results = run_local_checks(paths)
        for result in results:
            print(f"[{result.status.value.upper()}] {result.name}: {result.message}")
        return 2 if any(result.status is CheckStatus.FAIL for result in results) else 0

    if arguments.command == "init":
        return _run_init(paths)

    if arguments.command == "gateway":
        try:
            asyncio.run(run_gateway(paths))
        except (ConfigError, DotEnvError, GatewayConfigError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except (GatewayRuntimeError, DatabaseError, MigrationError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 5
        except KeyboardInterrupt:
            print("Cancelled.", file=sys.stderr)
            return 130
        return 0

    if not _is_tui_terminal():
        print("error: MiniClaw TUI requires an interactive terminal", file=sys.stderr)
        return 2
    try:
        return run_tui(paths)
    except (ConfigError, DotEnvError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (DatabaseError, MigrationError, OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


def _run_init(paths) -> int:
    """初始化状态并保留既有稳定退出码。"""
    try:
        result = initialize_state(paths)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (BootstrapError, DatabaseError, MigrationError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 5
    if result.created_files or result.applied_migrations:
        print(f"Initialized MiniClaw at {paths.home} (owner {result.owner.id}).")
    else:
        print(f"MiniClaw is already initialized at {paths.home} (owner {result.owner.id}).")
    return 0


def _is_tui_terminal() -> bool:
    """拒绝无法可靠运行全屏应用的 pipe、CI 和 dumb 终端。"""
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "").lower() != "dumb"
    )


def _run_eval(arguments: argparse.Namespace) -> int:
    """运行无需本地 Agent 状态或模型凭据的离线回归命令。"""
    try:
        cases = load_cases(arguments.root)
    except EvalCaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if arguments.eval_command == "list":
        for case in cases:
            print(f"{case.id} {case.status} {case.capability} {case.title}")
        return 0
    if arguments.eval_command == "validate":
        print(f"Validated {len(cases)} eval cases.")
        return 0

    active = tuple(
        case for case in cases if case.status == "active" and "offline" in case.layers
    )
    if not active:
        print("error: no active offline eval cases", file=sys.stderr)
        return 2
    suite = asyncio.run(run_offline_suite(active))
    for result in suite.cases:
        if result.passed:
            print(f"PASS {result.case_id} {result.duration_ms}ms")
        else:
            print(f"FAIL {result.case_id} {','.join(result.failures)}")
    print(
        f"Offline eval: {suite.passed}/{suite.total} passed, "
        f"{suite.failed} failed ({suite.duration_ms}ms)."
    )
    return 1 if suite.failed else 0
