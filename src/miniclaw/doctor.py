"""MiniClaw 状态目录的离线、只读本地诊断。"""

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from miniclaw.config import AppConfig, ConfigError, load_config
from miniclaw.paths import StatePaths
from miniclaw.policy.command import CommandPolicyError, normalize_command
from miniclaw.policy.network import NetworkPolicyError, normalize_network_rule
from miniclaw.storage.database import Database, DatabaseError
from miniclaw.storage.migrations import LATEST_SCHEMA_VERSION
from miniclaw.tui_launcher import MINIMUM_NODE_VERSION, inspect_pi_tui


class CheckStatus(StrEnum):
    """表示单项诊断的通过、警告或失败状态。"""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """保存一个不含敏感数据的诊断结果。"""

    name: str
    status: CheckStatus
    message: str


def run_local_checks(
    paths: StatePaths,
    environ: Mapping[str, str] | None = None,
) -> tuple[CheckResult, ...]:
    """检查状态、配置、Workspace、数据库和权限且不修改任何数据。

    Args:
        paths: 需要诊断的 MiniClaw 状态路径。
        environ: 配置覆盖使用的环境变量；默认使用当前进程环境。

    Returns:
        固定九项、按依赖顺序排列的安全诊断结果。
    """
    state_result = _check_state_home(paths)
    config_result, config = _check_config(paths, environ)
    node_result, pi_tui_result = _check_pi_tui(environ)
    return (
        state_result,
        config_result,
        _check_workspace(config),
        _check_tools(config),
        _check_database(paths),
        _check_approvals(paths),
        _check_permissions(paths),
        node_result,
        pi_tui_result,
    )


def _check_pi_tui(
    environ: Mapping[str, str] | None,
) -> tuple[CheckResult, CheckResult]:
    """检查默认 TypeScript TUI 的 Node 版本和编译入口。"""
    inspection = inspect_pi_tui(environ)
    minimum = ".".join(str(part) for part in MINIMUM_NODE_VERSION)
    if inspection.node is None:
        return (
            CheckResult("node", CheckStatus.FAIL, f"Node.js >= {minimum} is required"),
            CheckResult("pi_tui", CheckStatus.FAIL, "pi-tui cannot be checked without Node.js"),
        )
    if inspection.node_version is None or inspection.node_version < MINIMUM_NODE_VERSION:
        return (
            CheckResult(
                "node",
                CheckStatus.FAIL,
                inspection.problem or f"Node.js >= {minimum} is required",
            ),
            CheckResult(
                "pi_tui",
                CheckStatus.FAIL,
                "pi-tui cannot run until Node.js is upgraded",
            ),
        )
    version = ".".join(str(part) for part in inspection.node_version)
    node_result = CheckResult(
        "node",
        CheckStatus.PASS,
        f"Node.js {version} is compatible: {inspection.node}",
    )
    if inspection.entry is None:
        return (
            node_result,
            CheckResult(
                "pi_tui",
                CheckStatus.FAIL,
                inspection.problem or "pi-tui build entry is missing",
            ),
        )
    return (
        node_result,
        CheckResult(
            "pi_tui",
            CheckStatus.PASS,
            f"pi-tui build is ready: {inspection.entry}",
        ),
    )


def _check_state_home(paths: StatePaths) -> CheckResult:
    """确认状态根和初始化目录都真实存在。"""
    missing = [path.name for path in paths.directories if not path.is_dir()]
    if missing:
        return CheckResult(
            "state_home",
            CheckStatus.FAIL,
            f"missing state directories: {', '.join(missing)}",
        )
    return CheckResult("state_home", CheckStatus.PASS, f"state directory is ready: {paths.home}")


def _check_config(
    paths: StatePaths,
    environ: Mapping[str, str] | None,
) -> tuple[CheckResult, AppConfig | None]:
    """通过生产配置加载器检查配置，但不读取密钥值。"""
    if not paths.config.is_file() or paths.config.is_symlink():
        return CheckResult("config", CheckStatus.FAIL, "config.toml is missing or unsafe"), None
    try:
        config = load_config(paths, environ)
    except ConfigError as error:
        return CheckResult("config", CheckStatus.FAIL, str(error)), None
    return CheckResult("config", CheckStatus.PASS, "configuration is valid"), config


def _check_workspace(config: AppConfig | None) -> CheckResult:
    """确认已校验配置中的 Workspace 可作为本地可写目录。"""
    if config is None:
        return CheckResult(
            "workspace", CheckStatus.FAIL, "workspace cannot be checked without valid config"
        )
    workspace = config.workspace.path
    if not workspace.is_dir():
        return CheckResult("workspace", CheckStatus.FAIL, f"workspace is missing: {workspace}")
    if not os.access(workspace, os.W_OK):
        return CheckResult("workspace", CheckStatus.FAIL, f"workspace is not writable: {workspace}")
    return CheckResult("workspace", CheckStatus.PASS, f"workspace is writable: {workspace}")


def _check_database(paths: StatePaths) -> CheckResult:
    """以只读意图检查数据库完整性和迁移版本。"""
    if not paths.database.is_file() or paths.database.is_symlink():
        return CheckResult("database", CheckStatus.FAIL, "miniclaw.db is missing or unsafe")
    database = Database(paths.database)
    try:
        with database.connect_read_only() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            version = int(version_row[0])
    except (DatabaseError, sqlite3.Error, OSError):
        return CheckResult("database", CheckStatus.FAIL, "database cannot be opened or verified")
    if integrity != "ok":
        return CheckResult("database", CheckStatus.FAIL, "database integrity check failed")
    if version != LATEST_SCHEMA_VERSION:
        return CheckResult(
            "database",
            CheckStatus.FAIL,
            f"database schema is {version}; expected {LATEST_SCHEMA_VERSION}",
        )
    return CheckResult("database", CheckStatus.PASS, f"database schema {version} is healthy")


def _check_tools(config: AppConfig | None) -> CheckResult:
    """只解析本地 command/hostname 规则，不执行命令、DNS 或网络请求。"""
    if config is None:
        return CheckResult(
            "tools",
            CheckStatus.FAIL,
            "tools cannot be checked without valid config",
        )
    try:
        for rule in config.tools.run_command.allow_commands:
            normalize_command(rule.program, rule.args, config.workspace.path)
        for value in config.tools.http_get.allow_hosts:
            normalize_network_rule(value)
    except (CommandPolicyError, NetworkPolicyError):
        return CheckResult(
            "tools",
            CheckStatus.FAIL,
            "a configured tool allow rule is invalid or unavailable",
        )
    return CheckResult(
        "tools",
        CheckStatus.PASS,
        (
            f"{len(config.tools.enabled)} tools enabled; "
            f"{len(config.tools.run_command.allow_commands)} command rules; "
            f"{len(config.tools.http_get.allow_hosts)} hostname rules"
        ),
    )


def _check_approvals(paths: StatePaths) -> CheckResult:
    """只读统计尚未过期的 pending Approval，不触发 lazy expiry。"""
    if not paths.database.is_file() or paths.database.is_symlink():
        return CheckResult("approvals", CheckStatus.FAIL, "approval database is unavailable")
    try:
        with Database(paths.database).connect_read_only() as connection:
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM approvals
                    WHERE status = 'pending' AND expires_at > ?
                    """,
                    (datetime.now(UTC).isoformat(),),
                ).fetchone()[0]
            )
    except (DatabaseError, sqlite3.Error, OSError):
        return CheckResult("approvals", CheckStatus.FAIL, "approvals cannot be inspected")
    if count:
        return CheckResult(
            "approvals",
            CheckStatus.WARN,
            f"{count} pending approval(s); doctor did not execute them",
        )
    return CheckResult("approvals", CheckStatus.PASS, "0 pending approvals")


def _check_permissions(paths: StatePaths) -> CheckResult:
    """确认状态根和配置未向 group 或 other 开放。"""
    if os.name != "posix":
        return CheckResult(
            "permissions", CheckStatus.WARN, "POSIX permission check is unavailable"
        )
    if not paths.home.is_dir() or not paths.config.is_file():
        return CheckResult("permissions", CheckStatus.FAIL, "state home or config is missing")
    insecure = [
        path.name
        for path in (paths.home, paths.config)
        if path.stat().st_mode & 0o077
    ]
    if insecure:
        return CheckResult(
            "permissions",
            CheckStatus.FAIL,
            f"group/other permissions are enabled: {', '.join(insecure)}",
        )
    return CheckResult("permissions", CheckStatus.PASS, "state home and config are owner-only")
