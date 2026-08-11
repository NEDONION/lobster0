"""Lobster0 唯一 TUI 入口与本地维护命令。"""

import argparse
import asyncio
import importlib.metadata as importlib_metadata
import importlib.util
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

from lobster0 import __version__
from lobster0.automation.models import ScheduledTask
from lobster0.automation.repository import (
    AutomationControlRepository,
    AutomationDataError,
    AutomationStateError,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from lobster0.bootstrap import BootstrapError, initialize_state
from lobster0.channels.supervisor import collect_enabled_channels
from lobster0.config import ConfigError, load_config
from lobster0.doctor import CheckStatus, run_local_checks
from lobster0.env import DotEnvError, load_dotenv, resolve_dotenv_path
from lobster0.evals.automation import run_automation_suite
from lobster0.evals.browser import run_browser_suite
from lobster0.evals.cases import (
    EvalCase,
    EvalCaseError,
    load_automation_cases,
    load_browser_cases,
    load_cases,
    load_evolution_cases,
)
from lobster0.evals.channel import run_channel_suite
from lobster0.evals.runner import run_offline_suite
from lobster0.evolution.evaluator import EvaluationError, evaluate_proposal_version
from lobster0.evolution.models import (
    Feedback,
    FeedbackRating,
    Proposal,
    ProposalStatus,
    ProposalTargetType,
)
from lobster0.evolution.proposals import CandidateError, validate_prompt_candidate
from lobster0.evolution.repository import (
    ActiveRevisionRepository,
    EvalRepository,
    EvolutionApprovalRepository,
    EvolutionError,
    FeedbackRepository,
    ProposalRepository,
)
from lobster0.evolution.service import EvolutionService
from lobster0.gateway import (
    GatewayConfigError,
    GatewayRuntimeError,
    prepare_gateway_sdk_runtime,
    run_gateway,
)
from lobster0.gateway_service import (
    LaunchdService,
    ServiceError,
    render_launchd_service,
)
from lobster0.install.orchestrator import resolve_install_facts, run_install_action
from lobster0.paths import PathConfigurationError, StatePaths, build_state_paths, resolve_home
from lobster0.setup import SetupError, run_interactive_setup
from lobster0.storage.database import Database, DatabaseError
from lobster0.storage.migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    apply_migrations,
)
from lobster0.storage.repositories import OwnerRepository
from lobster0.tui_launcher import (
    TuiLaunchError,
    inspect_pi_tui,
    is_supported_node_version,
    run_default_tui,
)
from lobster0.web_launcher import DEFAULT_WEB_PORT, WebLaunchError, run_web_console

_SERVICE_OPTIONAL_CHECKS = frozenset({"pi_tui", "browser"})


def build_parser() -> argparse.ArgumentParser:
    """创建裸 TUI 与 setup/init/doctor/eval 维护命令的解析器。"""
    parser = argparse.ArgumentParser(
        prog="lobster0",
        description="Lobster0 — a tiny self-hosted personal agent. Run bare to open the TUI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--home", help="absolute Lobster0 state directory")
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser("init", help="initialize local Lobster0 state")
    init_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    init_parser.add_argument(
        "--sandbox-image",
        help="digest-pinned Lobster0 Sandbox image",
    )
    setup_parser = subparsers.add_parser("setup", help="configure a fresh Lobster0 state")
    setup_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    setup_parser.add_argument(
        "--sandbox-image",
        help="digest-pinned Lobster0 Sandbox image",
    )
    doctor_parser = subparsers.add_parser("doctor", help="check local Lobster0 state")
    doctor_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="run all enabled IM channels with one long-lived Agent runtime",
    )
    gateway_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    web_parser = subparsers.add_parser(
        "web",
        help="serve the local web console on loopback for browser access",
    )
    web_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    web_parser.add_argument(
        "--host",
        help="bind address; defaults to loopback. A non-loopback address additionally "
        "requires the LOBSTER0_WEB_TOKEN environment variable",
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"bind port; defaults to {DEFAULT_WEB_PORT}",
    )
    task_parser = subparsers.add_parser(
        "task",
        help="inspect and control the local durable Task ledger",
    )
    task_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
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
    feedback_parser = subparsers.add_parser(
        "feedback",
        help="inspect and manage Owner feedback for Controlled Evolution",
    )
    feedback_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    feedback_subparsers = feedback_parser.add_subparsers(
        dest="feedback_command", required=True
    )
    feedback_list = feedback_subparsers.add_parser("list", help="list recorded feedback")
    feedback_list.add_argument("--rating", choices=("good", "bad"))
    feedback_show = feedback_subparsers.add_parser("show", help="show one redacted feedback")
    feedback_show.add_argument("feedback_id", type=_positive_cli_id)
    feedback_forget = feedback_subparsers.add_parser(
        "forget", help="clear a feedback's reason material"
    )
    feedback_forget.add_argument("feedback_id", type=_positive_cli_id)
    evolve_parser = subparsers.add_parser(
        "evolve",
        help="drive the Controlled Evolution pipeline for one restricted target",
    )
    evolve_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    evolve_subparsers = evolve_parser.add_subparsers(dest="evolve_command", required=True)
    evolve_propose = evolve_subparsers.add_parser(
        "propose", help="create a draft candidate from an owner feedback"
    )
    evolve_propose.add_argument("--feedback", type=_positive_cli_id, required=True)
    evolve_propose.add_argument("--block", default="agent-behavior")
    evolve_propose.add_argument(
        "--candidate-file",
        type=Path,
        required=True,
        help="UTF-8 file holding the complete candidate block (never a diff)",
    )
    evolve_propose.add_argument("--rationale", default="owner feedback")
    evolve_show = evolve_subparsers.add_parser("show", help="show one proposal's state")
    evolve_show.add_argument("proposal_id", type=_positive_cli_id)
    evolve_eval = evolve_subparsers.add_parser(
        "eval", help="run the deterministic gate for a proposal's current version"
    )
    evolve_eval.add_argument("proposal_id", type=_positive_cli_id)
    evolve_eval.add_argument(
        "--root",
        type=Path,
        default=Path.cwd() / "evals" / "scenarios",
        help="directory containing versioned JSONL cases",
    )
    evolve_request = evolve_subparsers.add_parser(
        "request", help="create a pending owner approval bound to exact hashes"
    )
    evolve_request.add_argument("proposal_id", type=_positive_cli_id)
    evolve_request.add_argument("--eval-run", type=_positive_cli_id, required=True)
    evolve_request.add_argument(
        "--action", choices=("apply", "rollback"), default="apply"
    )
    for name in ("approve", "deny", "apply", "rollback"):
        child = evolve_subparsers.add_parser(name, help=f"{name} an evolution approval")
        child.add_argument("approval_id", type=_positive_cli_id)
    service_parser = subparsers.add_parser(
        "service",
        help="manage the owned Lobster0 Gateway user service",
    )
    service_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    service_subparsers = service_parser.add_subparsers(
        dest="service_command",
        required=True,
    )
    for name in ("install", "status", "logs", "restart", "uninstall"):
        service_subparsers.add_parser(name, help=f"{name} the Lobster0 Gateway service")
    update_parser = subparsers.add_parser(
        "update",
        help="update a managed Lobster0 install to a newer Release",
    )
    update_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    update_parser.add_argument("--version", dest="target_version", help="exact target SemVer")
    update_parser.add_argument("--channel", choices=("stable", "dev"), default="stable")
    update_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit one redacted machine-readable report",
    )
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="remove the managed Lobster0 install and keep user data by default",
    )
    uninstall_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    uninstall_parser.add_argument(
        "--purge-data",
        action="store_true",
        help="also delete the enumerated Lobster0 state; the Workspace is always kept",
    )
    uninstall_parser.add_argument(
        "--yes-i-understand-data-loss",
        action="store_true",
        dest="confirm_data_loss",
        help="required exact confirmation for a noninteractive --purge-data run",
    )
    uninstall_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit one redacted machine-readable report",
    )
    smoke_parser = subparsers.add_parser(
        "install-smoke",
        help="internal offline install gate for a freshly built Runtime",
    )
    smoke_parser.add_argument(
        "--home",
        dest="command_home",
        help="absolute Lobster0 state directory",
    )
    smoke_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit one machine-readable smoke document",
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
    eval_run.add_argument(
        "--suite",
        choices=("offline", "channel", "automation", "browser", "evolution", "all"),
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
                home or os.environ.get("LOBSTER0_HOME") or Path.home() / ".lobster0"
            )
            if Path(selected_home).expanduser().is_symlink():
                raise PathConfigurationError(
                    "Lobster0 setup home must not be a symbolic link"
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

    if arguments.command == "feedback":
        return _run_feedback(paths, arguments)

    if arguments.command == "evolve":
        return _run_evolve(paths, arguments)

    if arguments.command == "service":
        return _run_service(paths, arguments)

    if arguments.command == "update":
        return run_install_action(
            "update",
            state_home=paths.home,
            version=arguments.target_version,
            channel=arguments.channel,
            json_output=bool(arguments.json_output),
        )

    if arguments.command == "uninstall":
        return run_install_action(
            "uninstall",
            state_home=paths.home,
            purge_data=bool(arguments.purge_data),
            confirm_data_loss=bool(arguments.confirm_data_loss),
            json_output=bool(arguments.json_output),
        )

    if arguments.command == "install-smoke":
        return _run_install_smoke(paths, json_output=bool(arguments.json_output))

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

    if arguments.command == "web":
        # Web 控制台由浏览器交互，因此不套用 TUI 的 TTY 前置条件。
        try:
            return run_web_console(paths, host=arguments.host, port=arguments.port)
        except (ConfigError, DotEnvError, WebLaunchError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except (DatabaseError, MigrationError, OSError, sqlite3.Error) as error:
            print(f"error: {error}", file=sys.stderr)
            return 5
        except KeyboardInterrupt:
            print("Cancelled.", file=sys.stderr)
            return 130

    if not _is_tui_terminal():
        print("error: Lobster0 TUI requires an interactive terminal", file=sys.stderr)
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
        paths: 已解析的 Lobster0 状态路径。
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
        print(f"Initialized Lobster0 at {paths.home} (owner {result.owner.id}).")
    else:
        print(f"Lobster0 is already initialized at {paths.home} (owner {result.owner.id}).")
    return 0


def _run_setup(paths: StatePaths, sandbox_image: str | None) -> int:
    """运行 fresh-only 交互 setup 并映射稳定退出码。

    Args:
        paths: 已解析的 Lobster0 状态路径。
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
    print(f"Configured Lobster0 at {paths.home} (owner {result.owner.id}).")
    return 0


def _run_task(paths: StatePaths, arguments: argparse.Namespace) -> int:
    """执行 repository-only Task 运维命令，不加载 Provider 或 Channel SDK。"""
    if not paths.database.is_file() or paths.database.is_symlink():
        print("error: Lobster0 state is not initialized", file=sys.stderr)
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


def _run_feedback(paths: StatePaths, arguments: argparse.Namespace) -> int:
    """执行 repository-only Feedback 运维命令，不加载 Provider 或 Channel SDK。"""
    if not paths.database.is_file() or paths.database.is_symlink():
        print("error: Lobster0 state is not initialized", file=sys.stderr)
        return 2
    try:
        database = Database(paths.database)
        apply_migrations(database)
        owner = OwnerRepository(database).get_or_create()
        feedback = FeedbackRepository(database)
        command = arguments.feedback_command
        if command == "list":
            rating = None if arguments.rating is None else FeedbackRating(arguments.rating)
            selected = feedback.list(owner.id, rating=rating)
            for item in selected:
                print(_feedback_line(item))
            if not selected:
                print("No feedback.")
            return 0
        if command == "show":
            item = feedback.get(owner.id, arguments.feedback_id)
            print(_feedback_line(item))
            print(f"reason={item.redacted_reason or '-'}")
            return 0
        if command == "forget":
            item = feedback.forget(owner.id, arguments.feedback_id)
            print(_feedback_line(item))
            return 0
        raise ValueError("unsupported feedback command")
    except EvolutionError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 4
    except (DatabaseError, MigrationError, OSError, sqlite3.Error) as error:
        print(f"error: {type(error).__name__}", file=sys.stderr)
        return 5


def _run_service(paths: StatePaths, arguments: argparse.Namespace) -> int:
    """按 install receipt 分流受管 user service 与源码 checkout LaunchAgent。

    受管安装（存在有效 install receipt）走 installer 的 systemd-user/launchd
    service 层；源码 checkout 保持 Phase 6 的 macOS LaunchAgent 行为、输出与
    退出码不变。`logs` 只对受管安装有意义，源码模式按既有约定干净失败。

    Args:
        paths: 已解析的 Lobster0 状态路径。
        arguments: argparse 生成且只含五个固定动作的参数。

    Returns:
        成功为 0、配置/preflight 错误为 2、service lifecycle 错误为 5。
    """
    command = arguments.service_command
    if resolve_install_facts(paths.home).managed:
        return run_install_action(f"service.{command}", state_home=paths.home)
    if command == "logs":
        print("error: service_logs_unavailable", file=sys.stderr)
        return 2
    try:
        service = _launchd_service(paths)
        if command == "install":
            _service_install_preflight(paths)
            service.install()
            print("service installed")
        elif command == "status":
            status = service.status()
            print(
                f"service installed={_service_bool(status.installed)} "
                f"loaded={_service_bool(status.loaded)} "
                f"running={_service_bool(status.running)}"
            )
        elif command == "restart":
            service.restart()
            print("service restarted")
        elif command == "uninstall":
            service.uninstall()
            print("service uninstalled")
        else:
            raise ServiceError("service_command_invalid")
        return 0
    except (ConfigError, DotEnvError, GatewayConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except ServiceError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 5


def _run_install_smoke(paths: StatePaths, *, json_output: bool) -> int:
    """离线校验一个新建 Runtime 是否可用，不触碰 Provider、Channel 或网络。

    只做 module discovery、包元数据读取、Node/TUI 入口检查和本地数据库
    schema 只读比对；不实例化任何 SDK Client，也不发起认证或 HTTP。

    Args:
        paths: 已解析的 Lobster0 状态路径。
        json_output: 为真时输出唯一 machine-readable 文档。

    Returns:
        全部检查通过为 0，否则为 2。
    """
    find_spec = importlib.util.find_spec
    checks: dict[str, str] = {}
    try:
        distribution = importlib_metadata.version("lobster0-agent")
    except importlib_metadata.PackageNotFoundError:
        distribution = None
    checks["metadata"] = "ok" if distribution == __version__ else "error"
    console_scripts = {
        entry.name for entry in importlib_metadata.entry_points(group="console_scripts")
    }
    checks["entry_point"] = "ok" if "lobster0" in console_scripts else "error"
    for channel, module in (
        ("feishu", "lark_channel"),
        ("telegram", "telegram"),
        ("discord", "discord"),
    ):
        checks[f"channel_{channel}"] = "ok" if find_spec(module) is not None else "error"
    for name, module in (
        ("automation", "lobster0.automation.repository"),
        ("sandbox", "lobster0.sandbox.base"),
        ("checkpoint", "lobster0.checkpoints"),
    ):
        checks[name] = "ok" if find_spec(module) is not None else "error"
    inspection = inspect_pi_tui(os.environ)
    checks["node"] = (
        "ok"
        if inspection.node_version is not None
        and is_supported_node_version(inspection.node_version)
        else "error"
    )
    checks["tui_entry"] = "ok" if inspection.entry is not None else "error"
    checks["database"] = _install_smoke_database(paths)
    status = "ok" if all(value == "ok" for value in checks.values()) else "error"
    if json_output:
        print(
            json.dumps(
                {"checks": checks, "status": status, "version": __version__},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        )
    else:
        for name, value in sorted(checks.items()):
            print(f"[{value.upper()}] {name}")
        print(f"install-smoke {status} version={__version__}")
    return 0 if status == "ok" else 2


def _install_smoke_database(paths: StatePaths) -> str:
    """只读比对本地数据库 schema；状态尚未创建时视为无需校验。"""
    if not paths.database.is_file() or paths.database.is_symlink():
        return "ok"
    try:
        with Database(paths.database).connect_read_only() as connection:
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
    except (DatabaseError, sqlite3.Error, OSError):
        return "error"
    return "ok" if version == LATEST_SCHEMA_VERSION else "error"


def _launchd_service(paths: StatePaths) -> LaunchdService:
    """从当前 managed runtime 与固定用户路径构造唯一 LaunchAgent。

    Args:
        paths: Gateway 使用的状态路径。

    Returns:
        绑定当前 console launcher、cwd 与 dotenv path 的服务对象。

    Raises:
        ServiceError: 当前 Python runtime 中没有 exact ``lobster0`` launcher。
    """
    launcher = Path(sys.executable).parent / "lobster0"
    if not launcher.is_file():
        raise ServiceError("service_runtime_invalid")
    spec = render_launchd_service(
        launcher=launcher,
        state_home=paths.home,
        working_directory=Path.cwd().resolve(strict=True),
        launch_agents=Path.home().resolve(strict=True) / "Library" / "LaunchAgents",
        dotenv_path=resolve_dotenv_path(paths, os.environ),
        commit=_service_repository_commit(Path.cwd()),
    )
    return LaunchdService(spec)


def _service_repository_commit(root: Path) -> str:
    """返回 LaunchAgent 必须绑定的 clean repository commit。

    Args:
        root: 调用 service install/status 时的仓库工作目录。

    Returns:
        当前 HEAD 的 lowercase 40-hex commit。

    Raises:
        ServiceError: root 不安全、Git 不可用、HEAD 无效或工作树不干净。
    """
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ServiceError("service_repository_invalid") from None
    if resolved != root or not resolved.is_dir():
        raise ServiceError("service_repository_invalid")
    try:
        head = subprocess.run(
            ("/usr/bin/git", "rev-parse", "--verify", "HEAD"),
            cwd=resolved,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            ("/usr/bin/git", "status", "--porcelain", "--untracked-files=normal"),
            cwd=resolved,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise ServiceError("service_repository_invalid") from None
    commit = head.stdout.strip().lower()
    if (
        head.returncode != 0
        or status.returncode != 0
        or status.stdout
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        raise ServiceError("service_repository_dirty")
    return commit


def _service_install_preflight(paths: StatePaths) -> None:
    """在写 plist 前确认当前状态只启用飞书且本地 Doctor 全绿。

    Args:
        paths: 即将交给 LaunchAgent 的状态路径。

    Returns:
        配置与全部本地检查通过时返回 ``None``。

    Raises:
        ConfigError: 配置无法加载。
        DotEnvError: dotenv 文件不安全。
        GatewayConfigError: enabled Channel 不是唯一飞书。
        ServiceError: 任一本地 Doctor check 失败。
    """
    environment = dict(os.environ)
    load_dotenv(resolve_dotenv_path(paths, environment), environment)
    config = load_config(paths, environment)
    if collect_enabled_channels(config) != ("feishu",):
        raise GatewayConfigError("service production target must be Feishu only")
    if any(
        result.status is CheckStatus.FAIL and result.name not in _SERVICE_OPTIONAL_CHECKS
        for result in run_local_checks(paths, environment)
    ):
        raise ServiceError("service_preflight_failed")


def _service_bool(value: bool) -> str:
    """把 service 状态渲染为稳定的小写 boolean。"""
    return "true" if value else "false"


def _task_line(task: ScheduledTask) -> str:
    """渲染不含 Prompt、平台标识和 Secret 的单行 Task 摘要。"""
    next_run = "-" if task.schedule.next_run_at is None else task.schedule.next_run_at.isoformat()
    system = " system" if task.system_key is not None else ""
    return (
        f"task={task.id} name={task.name!r} status={task.status.value}{system} "
        f"schedule={task.schedule.kind.value} next={next_run} version={task.version}"
    )



def _run_evolve(paths: StatePaths, arguments: argparse.Namespace) -> int:
    """执行 Controlled Evolution 的单步命令；每条命令只做一个动作。

    刻意不把 propose、eval、approve、apply 串成一条"魔法命令"：每一步都要 Owner 显式发起，
    并且 apply/rollback 必须消费一条已经批准、绑定精确哈希的审批。
    """
    if not paths.database.is_file() or paths.database.is_symlink():
        print("error: Lobster0 state is not initialized", file=sys.stderr)
        return 2
    try:
        database = Database(paths.database)
        apply_migrations(database)
        owner = OwnerRepository(database).get_or_create()
        proposals = ProposalRepository(database)
        evals = EvalRepository(database)
        approvals = EvolutionApprovalRepository(database)
        active = ActiveRevisionRepository(database)
        service = EvolutionService(
            proposals=proposals,
            evals=evals,
            approvals=approvals,
            active=active,
            prompt_versions_root=paths.prompt_versions,
        )
        command = arguments.evolve_command

        if command == "propose":
            text = arguments.candidate_file.read_text(encoding="utf-8")
            material = validate_prompt_candidate(
                paths.prompt_versions, arguments.block, text
            )
            proposal, version = proposals.create_draft(
                owner_id=owner.id,
                feedback_id=arguments.feedback,
                target_type=ProposalTargetType.PROMPT,
                target_name=arguments.block,
                base_hash=material.base_hash,
                candidate_hash=material.candidate_hash,
                manifest_json=material.manifest_json,
                candidate_ref=material.candidate_ref,
                rationale=arguments.rationale,
            )
            print(_proposal_line(proposal))
            print(f"version={version.id} ordinal={version.ordinal}")
            print(f"candidate_hash={version.candidate_hash}")
            return 0

        if command == "show":
            proposal = proposals.get(owner.id, arguments.proposal_id)
            print(_proposal_line(proposal))
            if proposal.current_version_id is not None:
                version = proposals.get_version(owner.id, proposal.current_version_id)
                print(
                    f"version={version.id} ordinal={version.ordinal} "
                    f"base={version.base_hash[:16]} candidate={version.candidate_hash[:16]}"
                )
            pointer = active.get(owner.id, proposal.target_type, proposal.target_name)
            print(
                "active="
                + ("-" if pointer is None else str(pointer.proposal_version_id))
            )
            return 0

        if command == "eval":
            proposal = proposals.get(owner.id, arguments.proposal_id)
            if proposal.status is ProposalStatus.DRAFT:
                proposal = proposals.transition(
                    owner.id,
                    proposal.id,
                    expected_status=ProposalStatus.DRAFT,
                    new_status=ProposalStatus.EVALUATING,
                )
            if proposal.current_version_id is None:
                print("error: proposal_version_missing", file=sys.stderr)
                return 4
            receipt = asyncio.run(
                evaluate_proposal_version(
                    evals,
                    proposal_version_id=proposal.current_version_id,
                    suite_root=arguments.root,
                )
            )
            print(
                f"gate={'PASS' if receipt.outcome.passed else 'FAIL'} "
                f"cases={receipt.outcome.passed_cases}/{receipt.outcome.total_cases} "
                f"safety_failures={receipt.outcome.safety_failures}"
            )
            if receipt.outcome.violations:
                print(f"violations={','.join(receipt.outcome.violations)}")
            print(f"receipt={receipt.receipt_hash}")
            return 0 if receipt.outcome.passed else 1

        if command == "request":
            if arguments.action == "apply":
                approval = service.request_apply_approval(
                    owner.id, arguments.proposal_id, eval_run_id=arguments.eval_run
                )
            else:
                approval = service.request_rollback_approval(
                    owner.id, arguments.proposal_id
                )
            print(
                f"approval={approval.id} action={approval.action.value} "
                f"status={approval.status.value} expires={approval.expires_at.isoformat()}"
            )
            print(f"binding={approval.binding_hash}")
            return 0

        if command in {"approve", "deny"}:
            approval = approvals.decide(
                owner.id, arguments.approval_id, approved=command == "approve"
            )
            print(f"approval={approval.id} status={approval.status.value}")
            return 0

        if command == "apply":
            receipt = service.apply(owner.id, arguments.approval_id)
            print(
                f"applied proposal={receipt.proposal_id} "
                f"version={receipt.proposal_version_id} "
                f"target={receipt.target_type.value}:{receipt.target_name}"
            )
            return 0

        if command == "rollback":
            rollback = service.rollback(owner.id, arguments.approval_id)
            print(
                f"rolled_back proposal={rollback.proposal_id} "
                f"restored_version={rollback.restored_version_id}"
            )
            return 0

        raise ValueError("unsupported evolve command")
    except CandidateError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 4
    except (EvolutionError, EvaluationError) as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 4
    except (OSError, UnicodeDecodeError):
        print("error: candidate_file_unreadable", file=sys.stderr)
        return 2
    except (DatabaseError, MigrationError, sqlite3.Error) as error:
        print(f"error: {type(error).__name__}", file=sys.stderr)
        return 5


def _proposal_line(proposal: Proposal) -> str:
    """渲染不含候选正文的单行 Proposal 摘要。"""
    return (
        f"proposal={proposal.id} target={proposal.target_type.value}:{proposal.target_name} "
        f"status={proposal.status.value} feedback={proposal.feedback_id} "
        f"version={proposal.current_version_id or '-'}"
    )


def _feedback_line(item: Feedback) -> str:
    """渲染不含完整原因正文的单行 Feedback 摘要。"""
    reason_present = " has_reason" if item.redacted_reason else ""
    return (
        f"feedback={item.id} message={item.message_id} rating={item.rating.value} "
        f"status={item.status.value}{reason_present} created={item.created_at.isoformat()}"
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
    browser = tuple(
        case for case in cases if case.status == "active" and "browser" in case.layers
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
    if arguments.suite in {"browser", "all"}:
        try:
            browser = load_browser_cases(arguments.root)
        except EvalCaseError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    evolution: tuple[EvalCase, ...] = ()
    if arguments.suite in {"evolution", "all"}:
        try:
            evolution = load_evolution_cases(arguments.root)
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
    if arguments.suite in {"browser", "all"}:
        browser_passed = 0
        browser_total = 0
        browser_duration = 0
        browser_runs = 0
        browser_failed = 0
        for iteration in range(1, arguments.repeat + 1):
            browser_suite = asyncio.run(run_browser_suite(browser))
            browser_passed += browser_suite.passed
            browser_total += browser_suite.total
            browser_duration += browser_suite.duration_ms
            passed += browser_suite.passed
            checks += browser_suite.total
            duration_ms += browser_suite.duration_ms
            browser_runs = iteration
            if not json_output and (arguments.repeat == 1 or browser_suite.failed):
                for result in browser_suite.cases:
                    if result.passed:
                        print(f"PASS {result.case_id} {result.duration_ms}ms")
                    else:
                        print(
                            f"FAIL {result.case_id} {','.join(result.failures)} "
                            f"run={iteration}"
                        )
            browser_failed += browser_suite.failed
            failed += browser_suite.failed
            if browser_suite.failed:
                break
        if json_output:
            pass
        elif arguments.repeat == 1:
            print(
                f"Browser eval: {browser_passed}/{browser_total} passed, "
                f"{browser_failed} failed ({browser_duration}ms)."
            )
        else:
            print(
                f"Browser local soak: {browser_passed}/{browser_total} checks passed "
                f"across {browser_runs}/{arguments.repeat} runs ({browser_duration}ms)."
            )
    if arguments.suite in {"evolution", "all"}:
        evolution_passed = 0
        evolution_total = 0
        evolution_duration = 0
        evolution_runs = 0
        evolution_failed = 0
        for iteration in range(1, arguments.repeat + 1):
            evolution_suite = asyncio.run(run_offline_suite(evolution))
            evolution_passed += evolution_suite.passed
            evolution_total += evolution_suite.total
            evolution_duration += evolution_suite.duration_ms
            passed += evolution_suite.passed
            checks += evolution_suite.total
            duration_ms += evolution_suite.duration_ms
            evolution_runs = iteration
            if not json_output and (arguments.repeat == 1 or evolution_suite.failed):
                for result in evolution_suite.cases:
                    if result.passed:
                        print(f"PASS {result.case_id} {result.duration_ms}ms")
                    else:
                        print(
                            f"FAIL {result.case_id} {','.join(result.failures)} "
                            f"run={iteration}"
                        )
            evolution_failed += evolution_suite.failed
            failed += evolution_suite.failed
            if evolution_suite.failed:
                break
        if json_output:
            pass
        elif arguments.repeat == 1:
            print(
                f"Evolution eval: {evolution_passed}/{evolution_total} passed, "
                f"{evolution_failed} failed ({evolution_duration}ms)."
            )
        else:
            print(
                f"Evolution local soak: {evolution_passed}/{evolution_total} checks passed "
                f"across {evolution_runs}/{arguments.repeat} runs ({evolution_duration}ms)."
            )
    if json_output:
        selected = (
            offline
            if arguments.suite == "offline"
            else channel
            if arguments.suite == "channel"
            else automation
            if arguments.suite == "automation"
            else browser
            if arguments.suite == "browser"
            else evolution
            if arguments.suite == "evolution"
            else (*offline, *channel, *automation, *browser, *evolution)
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
