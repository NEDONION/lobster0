"""MiniClaw 唯一 TUI 入口与本地维护命令。"""

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from miniclaw import __version__
from miniclaw.automation.models import ScheduledTask
from miniclaw.automation.repository import (
    AutomationControlRepository,
    AutomationDataError,
    AutomationStateError,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from miniclaw.bootstrap import BootstrapError, initialize_state
from miniclaw.config import ConfigError
from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.env import DotEnvError, load_dotenv, resolve_dotenv_path
from miniclaw.evals.automation import run_automation_suite
from miniclaw.evals.cases import EvalCaseError, load_automation_cases, load_cases
from miniclaw.evals.channel import run_channel_suite
from miniclaw.evals.runner import run_offline_suite
from miniclaw.gateway import (
    GatewayConfigError,
    GatewayRuntimeError,
    prepare_gateway_sdk_runtime,
    run_gateway,
)
from miniclaw.paths import PathConfigurationError, StatePaths, build_state_paths, resolve_home
from miniclaw.setup import SetupError, run_interactive_setup
from miniclaw.storage.database import Database, DatabaseError
from miniclaw.storage.migrations import MigrationError, apply_migrations
from miniclaw.storage.repositories import OwnerRepository
from miniclaw.tui_launcher import TuiLaunchError, run_default_tui


def build_parser() -> argparse.ArgumentParser:
    """创建裸 TUI 与 setup/init/doctor/eval 维护命令的解析器。"""
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
    init_parser.add_argument(
        "--sandbox-image",
        help="digest-pinned MiniClaw Sandbox image",
    )
    setup_parser = subparsers.add_parser("setup", help="configure a fresh MiniClaw state")
    setup_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute MiniClaw state directory",
    )
    setup_parser.add_argument(
        "--sandbox-image",
        help="digest-pinned MiniClaw Sandbox image",
    )
    doctor_parser = subparsers.add_parser("doctor", help="check local MiniClaw state")
    doctor_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute MiniClaw state directory",
    )
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="run all enabled IM channels with one long-lived Agent runtime",
    )
    gateway_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute MiniClaw state directory",
    )
    task_parser = subparsers.add_parser(
        "task",
        help="inspect and control the local durable Task ledger",
    )
    task_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute MiniClaw state directory",
    )
    task_subparsers = task_parser.add_subparsers(dest="task_command", required=True)
    task_subparsers.add_parser("list", help="list scheduled tasks")
    task_show = task_subparsers.add_parser("show", help="show one redacted task")
    task_show.add_argument("task_id", type=_positive_cli_id)
    for name in ("pause", "resume", "run", "cancel", "runs"):
        child = task_subparsers.add_parser(name, help=f"{name} a durable task")
        child.add_argument("task_id", type=_positive_cli_id)
    task_halt = task_subparsers.add_parser("halt", help="activate durable automation E-stop")
    task_halt.add_argument("--reason", required=True)
    task_subparsers.add_parser("unhalt", help="clear durable automation E-stop")
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
    eval_run.add_argument(
        "--suite",
        choices=("offline", "channel", "automation", "all"),
        required=True,
    )
    eval_run.add_argument(
        "--repeat",
        type=_bounded_eval_repeat,
        default=1,
        help="repeat the selected suite 1..1000 times for a local endurance gate",
    )
    eval_run.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit one redacted machine-readable report",
    )
    return parser


def _bounded_eval_repeat(raw: str) -> int:
    """把 eval repeat 收窄到可预测的本地执行范围。"""
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 1000") from error
    if not 1 <= value <= 1000:
        raise argparse.ArgumentTypeError("must be from 1 to 1000")
    return value


def _positive_cli_id(raw: str) -> int:
    """把 Task CLI ID 收窄为非 bool 正整数。"""
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """运行裸 TUI，或执行不与聊天竞争的本地维护命令。"""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "eval":
        return _run_eval(arguments)

    home = getattr(arguments, "command_home", None) or arguments.home
    try:
        if arguments.command == "setup":
            selected_home = (
                home or os.environ.get("MINICLAW_HOME") or Path.home() / ".miniclaw"
            )
            if Path(selected_home).expanduser().is_symlink():
                raise PathConfigurationError(
                    "MiniClaw setup home must not be a symbolic link"
                )
        paths = build_state_paths(resolve_home(home))
    except (PathConfigurationError, ConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if arguments.command == "doctor":
        environment = dict(os.environ)
        try:
            load_dotenv(resolve_dotenv_path(paths, environment), environment)
        except DotEnvError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        results = run_local_checks(paths, environment)
        for result in results:
            print(f"[{result.status.value.upper()}] {result.name}: {result.message}")
        return 2 if any(result.status is CheckStatus.FAIL for result in results) else 0

    if arguments.command == "init":
        return _run_init(paths, arguments.sandbox_image)

    if arguments.command == "setup":
        return _run_setup(paths, arguments.sandbox_image)

    if arguments.command == "task":
        return _run_task(paths, arguments)

    if arguments.command == "gateway":
        try:
            prepare_gateway_sdk_runtime()
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
        return run_default_tui(paths)
    except (ConfigError, DotEnvError, TuiLaunchError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (DatabaseError, MigrationError, OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


def _run_init(paths: StatePaths, sandbox_image: str | None = None) -> int:
    """初始化状态并保留既有稳定退出码。

    Args:
        paths: 已解析的 MiniClaw 状态路径。
        sandbox_image: 首次配置使用的 digest-pinned Sandbox image。

    Returns:
        成功为 0、配置错误为 2、持久化错误为 5。
    """
    try:
        result = initialize_state(paths, sandbox_image=sandbox_image)
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


def _run_setup(paths: StatePaths, sandbox_image: str | None) -> int:
    """运行 fresh-only 交互 setup 并映射稳定退出码。

    Args:
        paths: 已解析的 MiniClaw 状态路径。
        sandbox_image: 安装器提供的 digest-pinned Sandbox image。

    Returns:
        成功为 0、输入或配置错误为 2、持久化错误为 5、取消为 130。
    """
    try:
        result = run_interactive_setup(paths, sandbox_image=sandbox_image)
    except (ConfigError, SetupError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (BootstrapError, DatabaseError, MigrationError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    print(f"Configured MiniClaw at {paths.home} (owner {result.owner.id}).")
    return 0


def _run_task(paths: StatePaths, arguments: argparse.Namespace) -> int:
    """执行 repository-only Task 运维命令，不加载 Provider 或 Channel SDK。"""
    if not paths.database.is_file() or paths.database.is_symlink():
        print("error: MiniClaw state is not initialized", file=sys.stderr)
        return 2
    try:
        database = Database(paths.database)
        apply_migrations(database)
        owner = OwnerRepository(database).get_or_create()
        tasks = ScheduledTaskRepository(database)
        runs = TaskRunRepository(database)
        control = AutomationControlRepository(database)
        command = arguments.task_command
        if command == "list":
            selected = tasks.list(owner_id=owner.id, limit=1000)
            for task in selected:
                print(_task_line(task))
            if not selected:
                print("No tasks.")
            return 0
        if command == "show":
            task = tasks.get(arguments.task_id, owner_id=owner.id)
            print(_task_line(task))
            print(
                f"schedule={task.schedule.kind.value}:{task.schedule.expression} "
                f"timezone={task.schedule.timezone} "
                f"prompt_bytes={len(task.prompt.encode('utf-8'))} "
                f"delivery={task.delivery.route}/{task.delivery.channel}"
            )
            return 0
        if command == "runs":
            task = tasks.get(arguments.task_id, owner_id=owner.id)
            selected_runs = runs.list(task_id=task.id, limit=1000)
            for run in selected_runs:
                print(
                    f"run={run.id} task={run.task_id} status={run.status.value} "
                    f"scheduled_for={run.scheduled_for.isoformat()} "
                    f"error={run.error_code or '-'}"
                )
            if not selected_runs:
                print("No task runs.")
            return 0
        if command == "halt":
            state = control.halt(arguments.reason)
            print(f"automation halted revision={state.revision}")
            return 0
        if command == "unhalt":
            state = control.unhalt()
            print(f"automation active revision={state.revision}")
            return 0

        task = tasks.get(arguments.task_id, owner_id=owner.id)
        if command == "pause":
            task = tasks.pause(
                task.id,
                owner_id=owner.id,
                expected_version=task.version,
            )
        elif command == "resume":
            task = tasks.resume(
                task.id,
                owner_id=owner.id,
                expected_version=task.version,
            )
        elif command == "cancel":
            task = tasks.cancel(
                task.id,
                owner_id=owner.id,
                expected_version=task.version,
            )
        elif command == "run":
            run = runs.enqueue(
                task,
                scheduled_for=datetime.now(UTC),
                idempotency_key=f"manual:{uuid4().hex}",
            )
            print(f"run={run.id} task={run.task_id} status={run.status.value}")
            return 0
        else:
            raise ValueError("unsupported task command")
        print(_task_line(task))
        return 0
    except (AutomationStateError, AutomationDataError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 4
    except (DatabaseError, MigrationError, OSError, sqlite3.Error) as error:
        print(f"error: {type(error).__name__}", file=sys.stderr)
        return 5


def _task_line(task: ScheduledTask) -> str:
    """渲染不含 Prompt、平台标识和 Secret 的单行 Task 摘要。"""
    next_run = "-" if task.schedule.next_run_at is None else task.schedule.next_run_at.isoformat()
    system = " system" if task.system_key is not None else ""
    return (
        f"task={task.id} name={task.name!r} status={task.status.value}{system} "
        f"schedule={task.schedule.kind.value} next={next_run} version={task.version}"
    )


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

    offline = tuple(
        case for case in cases if case.status == "active" and "offline" in case.layers
    )
    channel = tuple(
        case for case in cases if case.status == "active" and "channel" in case.layers
    )
    automation = tuple(
        case for case in cases if case.status == "active" and "automation" in case.layers
    )
    if arguments.suite in {"offline", "all"} and not offline:
        print("error: no active offline eval cases", file=sys.stderr)
        return 2
    if arguments.suite in {"channel", "all"} and not channel:
        print("error: no active channel eval cases", file=sys.stderr)
        return 2
    if arguments.suite in {"automation", "all"}:
        try:
            automation = load_automation_cases(arguments.root)
        except EvalCaseError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    json_output = bool(arguments.json_output)
    failed = 0
    passed = 0
    checks = 0
    duration_ms = 0
    if arguments.suite in {"offline", "all"}:
        offline_passed = 0
        offline_total = 0
        offline_duration = 0
        offline_runs = 0
        for iteration in range(1, arguments.repeat + 1):
            suite = asyncio.run(run_offline_suite(offline))
            offline_passed += suite.passed
            offline_total += suite.total
            offline_duration += suite.duration_ms
            passed += suite.passed
            checks += suite.total
            duration_ms += suite.duration_ms
            offline_runs = iteration
            if not json_output and (arguments.repeat == 1 or suite.failed):
                for result in suite.cases:
                    if result.passed:
                        print(f"PASS {result.case_id} {result.duration_ms}ms")
                    else:
                        print(
                            f"FAIL {result.case_id} {','.join(result.failures)} "
                            f"run={iteration}"
                        )
            failed += suite.failed
            if suite.failed:
                break
        if json_output:
            pass
        elif arguments.repeat == 1:
            print(
                f"Offline eval: {offline_passed}/{offline_total} passed, "
                f"{failed} failed ({offline_duration}ms)."
            )
        else:
            print(
                f"Offline repeat gate: {offline_passed}/{offline_total} checks passed "
                f"across {offline_runs}/{arguments.repeat} runs ({offline_duration}ms)."
            )
    if arguments.suite in {"channel", "all"}:
        channel_passed = 0
        channel_total = 0
        channel_duration = 0
        channel_runs = 0
        channel_failed = 0
        for iteration in range(1, arguments.repeat + 1):
            channel_suite = asyncio.run(run_channel_suite(channel))
            channel_passed += channel_suite.passed
            channel_total += channel_suite.total
            channel_duration += channel_suite.duration_ms
            passed += channel_suite.passed
            checks += channel_suite.total
            duration_ms += channel_suite.duration_ms
            channel_runs = iteration
            if not json_output and (arguments.repeat == 1 or channel_suite.failed):
                for result in channel_suite.cases:
                    if result.passed:
                        print(f"PASS {result.case_id} {result.duration_ms}ms")
                    else:
                        print(
                            f"FAIL {result.case_id} {','.join(result.failures)} "
                            f"run={iteration}"
                        )
            channel_failed += channel_suite.failed
            failed += channel_suite.failed
            if channel_suite.failed:
                break
        if json_output:
            pass
        elif arguments.repeat == 1:
            print(
                f"Channel eval: {channel_passed}/{channel_total} passed, "
                f"{channel_failed} failed ({channel_duration}ms)."
            )
        else:
            print(
                f"Channel local soak: {channel_passed}/{channel_total} checks passed "
                f"across {channel_runs}/{arguments.repeat} runs ({channel_duration}ms)."
            )
    if arguments.suite in {"automation", "all"}:
        automation_passed = 0
        automation_total = 0
        automation_duration = 0
        automation_runs = 0
        automation_failed = 0
        for iteration in range(1, arguments.repeat + 1):
            automation_suite = asyncio.run(run_automation_suite(automation))
            automation_passed += automation_suite.passed
            automation_total += automation_suite.total
            automation_duration += automation_suite.duration_ms
            passed += automation_suite.passed
            checks += automation_suite.total
            duration_ms += automation_suite.duration_ms
            automation_runs = iteration
            if not json_output and (arguments.repeat == 1 or automation_suite.failed):
                for result in automation_suite.cases:
                    if result.passed:
                        print(f"PASS {result.case_id} {result.duration_ms}ms")
                    else:
                        print(
                            f"FAIL {result.case_id} {','.join(result.failures)} "
                            f"run={iteration}"
                        )
            automation_failed += automation_suite.failed
            failed += automation_suite.failed
            if automation_suite.failed:
                break
        if json_output:
            pass
        elif arguments.repeat == 1:
            print(
                f"Automation eval: {automation_passed}/{automation_total} passed, "
                f"{automation_failed} failed ({automation_duration}ms)."
            )
        else:
            print(
                f"Automation local soak: {automation_passed}/{automation_total} checks passed "
                f"across {automation_runs}/{arguments.repeat} runs "
                f"({automation_duration}ms)."
            )
    if json_output:
        selected = (
            offline
            if arguments.suite == "offline"
            else channel
            if arguments.suite == "channel"
            else automation
            if arguments.suite == "automation"
            else (*offline, *channel, *automation)
        )
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "suite": arguments.suite,
                    "suite_version": 1,
                    "commit": _repository_commit(),
                    "case_ids": [case.id for case in selected],
                    "cases_per_run": len(selected),
                    "repeat": arguments.repeat,
                    "checks": checks,
                    "passed": passed,
                    "failed": failed,
                    "duration_ms": duration_ms,
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    return 1 if failed else 0


def _repository_commit() -> str:
    """best-effort 读取当前本地 commit；失败时返回稳定占位符。"""
    project_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip().lower()
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else "unknown"
