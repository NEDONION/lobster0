"""MiniClaw 状态目录的离线、只读本地诊断。"""

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from miniclaw.config import AppConfig, ConfigError, load_config
from miniclaw.paths import StatePaths
from miniclaw.storage.database import Database, DatabaseError
from miniclaw.storage.migrations import LATEST_SCHEMA_VERSION


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
        固定五项、按依赖顺序排列的安全诊断结果。
    """
    state_result = _check_state_home(paths)
    config_result, config = _check_config(paths, environ)
    return (
        state_result,
        config_result,
        _check_workspace(config),
        _check_database(paths),
        _check_permissions(paths),
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
