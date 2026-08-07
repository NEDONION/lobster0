"""MiniClaw 命令行入口。"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from miniclaw import __version__
from miniclaw.agent.context import ContextBuilder, ContextError
from miniclaw.agent.runner import AgentError, AgentRunner
from miniclaw.agent.turn import TurnService
from miniclaw.bootstrap import BootstrapError, initialize_state
from miniclaw.config import AppConfig, ConfigError, load_config
from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.env import DotEnvError, load_dotenv
from miniclaw.evals.cases import EvalCaseError, load_cases
from miniclaw.evals.runner import run_offline_suite
from miniclaw.paths import PathConfigurationError, StatePaths, build_state_paths, resolve_home
from miniclaw.policy.approvals import ApprovalError
from miniclaw.policy.command import normalize_command
from miniclaw.policy.engine import PolicyEngine
from miniclaw.policy.network import normalize_network_rule
from miniclaw.providers.base import ProviderAuthenticationError, ProviderError
from miniclaw.providers.openai_compatible import OpenAICompatibleProvider
from miniclaw.storage.conversations import (
    ConversationDataError,
    ConversationStateError,
    MessageRepository,
    SessionRepository,
    TurnRepository,
)
from miniclaw.storage.database import Database, DatabaseError
from miniclaw.storage.migrations import MigrationError, apply_migrations
from miniclaw.storage.repositories import OwnerRepository
from miniclaw.storage.tooling import (
    ApprovalRepository,
    PolicyRuleRepository,
    StoredApproval,
    ToolRunRepository,
    ToolStateError,
)
from miniclaw.tools.command import RunCommandTool
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.search import GlobTool, GrepTool
from miniclaw.tools.system import SystemInfoTool
from miniclaw.tools.web import HttpGetTool

_DEFAULT_CLI_SESSION = "default"


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器。

    Returns:
        配置好程序名、版本参数和当前子命令的解析器。
    """
    parser = argparse.ArgumentParser(
        prog="miniclaw",
        description="MiniClaw — a tiny self-hosted personal agent.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser("init", help="initialize local MiniClaw state")
    init_parser.add_argument("--home", help="absolute MiniClaw state directory")
    doctor_parser = subparsers.add_parser("doctor", help="check local MiniClaw state")
    doctor_parser.add_argument("--home", help="absolute MiniClaw state directory")
    chat_parser = subparsers.add_parser("chat", help="chat with the local MiniClaw agent")
    chat_parser.add_argument("--home", help="absolute MiniClaw state directory")
    chat_parser.add_argument("--session", default=_DEFAULT_CLI_SESSION, help="CLI session ID")
    chat_parser.add_argument("--message", help="one-shot user message")
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
    approvals_parser = subparsers.add_parser("approvals", help="inspect and decide tool approvals")
    approvals_parser.add_argument("--home", help="absolute MiniClaw state directory")
    approval_commands = approvals_parser.add_subparsers(
        dest="approval_command",
        required=True,
    )
    approval_list = approval_commands.add_parser("list", help="list approval requests")
    approval_list.add_argument(
        "--status",
        choices=("pending", "approved", "denied", "expired", "consumed"),
    )
    approval_list.add_argument("--json", action="store_true", dest="as_json")
    approval_show = approval_commands.add_parser("show", help="show one approval")
    approval_show.add_argument("approval_id", type=int)
    approval_show.add_argument("--json", action="store_true", dest="as_json")
    approval_approve = approval_commands.add_parser("approve", help="approve and continue")
    approval_approve.add_argument("approval_id", type=int)
    approval_approve.add_argument("--always", action="store_true")
    approval_approve.add_argument("--json", action="store_true", dest="as_json")
    approval_deny = approval_commands.add_parser("deny", help="deny and continue")
    approval_deny.add_argument("approval_id", type=int)
    approval_deny.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行参数并运行当前可用入口。

    Args:
        argv: 需要解析的参数；为 ``None`` 时由 ``argparse`` 读取进程参数。

    Returns:
        成功为 0；eval case 失败为 1；参数/配置为 2；认证为 3；Agent/Provider 为 4；
        本地 I/O 为 5；用户中断为 130。
    """
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.print_help()
        return 0
    if arguments.command == "eval":
        return _run_eval(arguments)

    try:
        paths = build_state_paths(resolve_home(arguments.home))
    except (PathConfigurationError, ConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if arguments.command == "doctor":
        results = run_local_checks(paths)
        for result in results:
            print(f"[{result.status.value.upper()}] {result.name}: {result.message}")
        return 2 if any(result.status is CheckStatus.FAIL for result in results) else 0

    if arguments.command == "approvals":
        return _run_approvals(arguments, paths)

    if arguments.command == "chat":
        if arguments.message is None and not sys.stdin.isatty():
            print("error: non-interactive chat requires --message TEXT", file=sys.stderr)
            return 2
        return _run_chat(arguments, paths)

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


def _run_eval(arguments: argparse.Namespace) -> int:
    """运行无需本地 Agent 状态或模型凭据的离线回归命令。

    Args:
        arguments: 已由 eval 子解析器校验的参数命名空间。

    Returns:
        成功为 0；任一场景失败为 1；场景输入无效为 2。
    """
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


def _run_approvals(arguments: argparse.Namespace, paths: StatePaths) -> int:
    """查询或决定持久 Approval，并使用稳定退出码。"""
    try:
        _require_initialized_state(paths)
        database = Database(paths.database)
        apply_migrations(database)
        owner = OwnerRepository(database).get_or_create()
        approvals = ApprovalRepository(database)
        if arguments.approval_command == "list":
            items = approvals.list(owner.id, status=arguments.status)
            _print_approvals(items, arguments.as_json)
            return 0
        if arguments.approval_command == "show":
            item = approvals.get(owner.id, arguments.approval_id)
            _print_approval(item, arguments.as_json)
            return 0

        load_dotenv(Path.cwd() / ".env")
        config = load_config(paths)
        api_key = os.environ.get(config.provider.api_key_env, "").strip()
        if not api_key:
            raise ConfigError(f"{config.provider.api_key_env} is not configured")
        if arguments.approval_command == "approve" and arguments.always:
            item = approvals.get(owner.id, arguments.approval_id)
            if item.tool_name not in {"run_command", "http_get"}:
                raise ConfigError("--always only supports exact command or hostname rules")
        return asyncio.run(
            _continue_approval(
                config,
                paths,
                api_key,
                owner.id,
                arguments.approval_id,
                approved=arguments.approval_command == "approve",
                as_json=arguments.as_json,
                always=(
                    arguments.approval_command == "approve" and arguments.always
                ),
            )
        )
    except ProviderAuthenticationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    except (ProviderError, AgentError, ContextError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 4
    except (ApprovalError, ConfigError, DotEnvError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (
        ConversationDataError,
        ConversationStateError,
        DatabaseError,
        MigrationError,
        OSError,
        ToolStateError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 5
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Cancelled.", file=sys.stderr)
        return 130


async def _continue_approval(
    config: AppConfig,
    paths: StatePaths,
    api_key: str,
    owner_id: int,
    approval_id: int,
    *,
    approved: bool,
    as_json: bool,
    always: bool,
) -> int:
    """创建运行期、继续一次审批，并确保关闭 Provider。"""
    runtime_owner_id, provider, service = _create_runtime(config, paths, api_key)
    if runtime_owner_id != owner_id:
        await provider.aclose()
        raise ApprovalError("not_owner", "approval belongs to a different owner")
    try:
        result = await service.continue_approval(
            owner_id,
            approval_id,
            approved=approved,
        )
        if always:
            item = ApprovalRepository(Database(paths.database)).get(owner_id, approval_id)
            if item.tool_name == "run_command":
                PolicyRuleRepository(Database(paths.database)).add_command_from_approval(
                    owner_id,
                    approval_id,
                )
            elif item.tool_name == "http_get":
                PolicyRuleRepository(Database(paths.database)).add_network_from_approval(
                    owner_id,
                    approval_id,
                )
            else:
                raise ConfigError("--always rule is not implemented for this tool")
        if as_json:
            print(
                json.dumps(
                    {
                        "turn_id": result.turn_id,
                        "session_id": result.session_id,
                        "content": result.content,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        else:
            print(result.content)
        return 0
    finally:
        await provider.aclose()


def _approval_payload(approval: StoredApproval) -> dict[str, object]:
    """把不含原始 Tool 参数的 Approval 转为稳定 JSON object。"""
    return {
        "id": approval.id,
        "tool": approval.tool_name,
        "summary": approval.summary,
        "status": approval.status,
        "expires_at": approval.expires_at.isoformat(),
        "created_at": approval.created_at.isoformat(),
        "decided_at": (
            approval.decided_at.isoformat() if approval.decided_at is not None else None
        ),
    }


def _print_approvals(items: tuple[StoredApproval, ...], as_json: bool) -> None:
    """打印稳定 JSON 或人类可扫读的列表。"""
    if as_json:
        print(
            json.dumps(
                [_approval_payload(item) for item in items],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return
    print("ID STATUS TOOL EXPIRES SUMMARY")
    for item in items:
        print(
            f"{item.id} {item.status} {item.tool_name} "
            f"{item.expires_at.isoformat()} {item.summary}"
        )


def _print_approval(item: StoredApproval, as_json: bool) -> None:
    """打印单条脱敏 Approval。"""
    payload = _approval_payload(item)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return
    for key in ("id", "status", "tool", "summary", "created_at", "expires_at", "decided_at"):
        print(f"{key}: {payload[key]}")


def _run_chat(arguments: argparse.Namespace, paths: StatePaths) -> int:
    """运行单次或交互聊天，并把稳定异常映射为 CLI 退出码。

    Args:
        arguments: 已由 chat 子解析器校验的参数命名空间。
        paths: 已安全解析的状态路径。

    Returns:
        成功为 0；配置为 2；认证为 3；模型/Agent 为 4；本地状态为 5；中断为 130。
    """
    try:
        _require_initialized_state(paths)
        load_dotenv(Path.cwd() / ".env")
        config = load_config(paths)
        api_key = os.environ.get(config.provider.api_key_env, "").strip()
        if not api_key:
            raise ConfigError(f"{config.provider.api_key_env} is not configured")
        session = arguments.session.strip()
        if not session:
            raise ConfigError("chat session ID must not be empty")
        return asyncio.run(_chat(config, paths, api_key, session, arguments.message))
    except ProviderAuthenticationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    except (ProviderError, AgentError, ContextError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 4
    except (ConfigError, DotEnvError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (
        ConversationDataError,
        ConversationStateError,
        DatabaseError,
        MigrationError,
        OSError,
        ToolStateError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 5
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Cancelled.", file=sys.stderr)
        return 130


def _require_initialized_state(paths: StatePaths) -> None:
    """拒绝缺失或符号链接状态，避免 chat 暗中创建半初始化实例。

    Args:
        paths: 需要检查的状态路径。

    Raises:
        ConfigError: 状态根、配置、数据库或身份文件未安全初始化。
    """
    required = (paths.config, paths.database, paths.soul, paths.user)
    if (
        not paths.home.is_dir()
        or paths.home.is_symlink()
        or any(not path.is_file() or path.is_symlink() for path in required)
    ):
        raise ConfigError(f"MiniClaw is not initialized at {paths.home}; run miniclaw init first")


async def _chat(
    config: AppConfig,
    paths: StatePaths,
    api_key: str,
    session: str,
    message: str | None,
) -> int:
    """组装一次运行期依赖，并复用同一个 Provider 完成聊天。

    Args:
        config: 已校验的模型和循环配置。
        paths: 已初始化的本地状态路径。
        api_key: 仅保存在内存中的 Provider 凭据。
        session: 当前 CLI 会话标识。
        message: 单次消息；为 ``None`` 时进入 TTY 交互循环。

    Returns:
        所有请求成功或交互正常退出时返回 0。

    Raises:
        ProviderError: 模型认证、协议、网络或服务端失败。
        AgentError: 模型循环无法得到有效最终答案。
        DatabaseError: 本地数据库不可用。
        MigrationError: 数据库无法升级到当前 Schema。
    """
    owner_id, provider, service = _create_runtime(config, paths, api_key)
    try:
        if message is not None:
            result = await service.handle(owner_id, message, session)
            print(result.content)
            return 0
        return await _interactive_chat(service, owner_id, session)
    finally:
        await provider.aclose()


def _create_runtime(
    config: AppConfig,
    paths: StatePaths,
    api_key: str,
) -> tuple[int, OpenAICompatibleProvider, TurnService]:
    """组装 chat 与 approval continuation 共用的唯一运行期。"""
    database = Database(paths.database)
    apply_migrations(database)
    owner = OwnerRepository(database).get_or_create()
    runs = ToolRunRepository(database)
    runs.interrupt_stale_runs()
    provider = OpenAICompatibleProvider(
        config.provider.base_url,
        api_key,
        config.provider.timeout_seconds,
    )
    approvals = ApprovalRepository(database)
    rules = PolicyRuleRepository(database)
    configured_command_rules = tuple(
        normalize_command(rule.program, rule.args, config.workspace.path)
        for rule in config.tools.run_command.allow_commands
    )
    command_rules = tuple(
        dict.fromkeys(
            (*configured_command_rules, *rules.command_rules(owner.id))
        )
    )
    configured_network_rules = tuple(
        normalize_network_rule(value) for value in config.tools.http_get.allow_hosts
    )
    network_rules = tuple(
        dict.fromkeys((*configured_network_rules, *rules.network_rules(owner.id)))
    )
    available_tools = (
        SystemInfoTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        HttpGetTool(
            timeout_seconds=config.tools.http_get.timeout_seconds,
            max_response_bytes=config.tools.http_get.max_response_bytes,
            allow_rules=network_rules,
        ),
        RunCommandTool(
            timeout_seconds=config.tools.run_command.timeout_seconds,
            max_timeout_seconds=config.tools.run_command.max_timeout_seconds,
        ),
    )
    executor = ToolExecutor(
        ToolRegistry(
            tool for tool in available_tools if tool.definition.name in config.tools.enabled
        ),
        PolicyEngine(
            security=config.tools.security,
            ask=config.tools.ask,
            command_rules=command_rules,
            network_rules=network_rules,
        ),
        runs,
        result_max_chars=config.agent.tool_result_max_chars,
        approvals=approvals,
        approval_ttl_seconds=config.tools.approval_ttl_seconds,
    )
    service = TurnService(
        model=config.agent.model,
        sessions=SessionRepository(database),
        messages=MessageRepository(database),
        turns=TurnRepository(database),
        context=ContextBuilder(paths),
        runner=AgentRunner(
            provider,
            executor,
            max_iterations=config.agent.max_tool_iterations,
        ),
        approvals=approvals,
        state_home=paths.home,
        workspace=config.workspace,
    )
    return owner.id, provider, service


async def _interactive_chat(service: TurnService, owner_id: int, session: str) -> int:
    """从 TTY 顺序读取输入，直到 EOF 或显式退出指令。

    Args:
        service: 当前运行期共用的 TurnService。
        owner_id: 本地唯一 Owner ID。
        session: 整个交互过程复用的 CLI 会话标识。

    Returns:
        EOF、``/exit`` 或 ``/quit`` 正常结束时返回 0。

    Raises:
        ProviderError: 任一模型请求失败。
        AgentError: 任一 Agent Loop 失败。
    """
    while True:
        try:
            text = input("You> ")
        except EOFError:
            print()
            return 0
        if text.strip() in {"/exit", "/quit"}:
            return 0
        if not text.strip():
            continue
        result = await service.handle(owner_id, text, session)
        print(f"MiniClaw> {result.content}")
