"""MiniClaw 命令行入口。"""

import argparse
import sys
from collections.abc import Sequence

from miniclaw import __version__
from miniclaw.bootstrap import BootstrapError, initialize_state
from miniclaw.config import ConfigError
from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.paths import PathConfigurationError, build_state_paths, resolve_home
from miniclaw.storage.database import DatabaseError
from miniclaw.storage.migrations import MigrationError


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器。

    Returns:
        配置好程序名、版本参数和 Phase 0 子命令的解析器。
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行参数并运行当前可用入口。

    Args:
        argv: 需要解析的参数；为 ``None`` 时由 ``argparse`` 读取进程参数。

    Returns:
        成功为 0，路径或配置错误为 2，初始化运行错误为 5。
    """
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.print_help()
        return 0

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

    try:
        result = initialize_state(paths)
    except (BootstrapError, DatabaseError, MigrationError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 5

    if result.created_files or result.applied_migrations:
        print(f"Initialized MiniClaw at {paths.home} (owner {result.owner.id}).")
    else:
        print(f"MiniClaw is already initialized at {paths.home} (owner {result.owner.id}).")
    return 0
