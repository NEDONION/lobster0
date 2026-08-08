"""MiniClaw 状态目录的离线、只读本地诊断。"""

import hashlib
import importlib.util
import os
import shutil
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from miniclaw.config import (
    AppConfig,
    ConfigError,
    ResolvedPermissionRoots,
    load_config,
    resolve_permission_roots,
)
from miniclaw.paths import StatePaths
from miniclaw.policy.command import CommandPolicyError, normalize_command
from miniclaw.policy.executables import ExecutableEnvironment, discover_executables
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
    *,
    find_spec: Callable[[str], object | None] | None = None,
) -> tuple[CheckResult, ...]:
    """检查状态、配置、Workspace、数据库和权限且不修改任何数据。

    Args:
        paths: 需要诊断的 MiniClaw 状态路径。
        environ: 配置覆盖使用的环境变量；默认使用当前进程环境。

    Returns:
        固定二十二项、按依赖顺序排列的安全诊断结果。
    """
    state_result = _check_state_home(paths)
    config_result, config = _check_config(paths, environ)
    node_result, pi_tui_result = _check_pi_tui(environ)
    source = os.environ if environ is None else environ
    module_spec = find_spec or importlib.util.find_spec
    return (
        state_result,
        config_result,
        _check_workspace(config),
        _check_tools(config),
        _check_personal_permissions(config),
        _check_executables(config),
        _check_database(paths),
        _check_memory(paths),
        _check_approvals(paths),
        _check_permissions(paths),
        node_result,
        pi_tui_result,
        _check_feishu_config(config),
        _check_channel_sdk(config, "feishu", "lark_channel", module_spec),
        _check_channel_runtime(config, "feishu", source),
        _check_telegram_config(config),
        _check_channel_sdk(config, "telegram", "telegram", module_spec),
        _check_channel_runtime(config, "telegram", source),
        _check_discord_config(config),
        _check_channel_sdk(config, "discord", "discord", module_spec),
        _check_channel_runtime(config, "discord", source),
        _check_channel_database(paths),
        _check_channel_workers(config),
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
        executables = _executable_environment(config)
        for rule in config.tools.run_command.allow_commands:
            normalize_command(
                rule.program,
                rule.args,
                config.workspace.path,
                executable_path=executables.path_value,
            )
        for value in config.tools.http_get.allow_hosts:
            normalize_network_rule(value)
    except (CommandPolicyError, NetworkPolicyError, OSError, ValueError):
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


def _check_personal_permissions(config: AppConfig | None) -> CheckResult:
    """只展示 Profile 与 Root 数量，不暴露 Owner Home 或绝对目录。"""
    if config is None:
        return CheckResult(
            "personal_permissions",
            CheckStatus.FAIL,
            "permission profile cannot be checked without valid config",
        )
    try:
        roots = _permission_roots(config)
    except (OSError, RuntimeError, ValueError):
        return CheckResult(
            "personal_permissions",
            CheckStatus.FAIL,
            "permission roots could not be resolved safely",
        )
    return CheckResult(
        "personal_permissions",
        CheckStatus.PASS,
        (
            f"profile {config.permissions.profile}; "
            f"{len(roots.read_roots)} read root(s); "
            f"{len(roots.write_roots)} write root(s)"
        ),
    )


def _check_executables(config: AppConfig | None) -> CheckResult:
    """离线报告可信 executable root 数量和 lark-cli 可用性。"""
    if config is None:
        return CheckResult(
            "executables",
            CheckStatus.FAIL,
            "executables cannot be checked without valid config",
        )
    try:
        environment = _executable_environment(config)
    except (OSError, RuntimeError, ValueError):
        return CheckResult(
            "executables",
            CheckStatus.FAIL,
            "executable roots could not be resolved safely",
        )
    availability = (
        "lark-cli available"
        if shutil.which("lark-cli", path=environment.path_value) is not None
        else "lark-cli unavailable"
    )
    return CheckResult(
        "executables",
        CheckStatus.PASS,
        f"{len(environment.search_roots)} executable root(s); {availability}",
    )


def _permission_roots(config: AppConfig) -> ResolvedPermissionRoots:
    """使用与 Runtime 相同的纯函数解析当前本机文件边界。"""
    return resolve_permission_roots(
        config.permissions,
        config.workspace.path,
        home=Path.home(),
        platform_name=sys.platform,
    )


def _executable_environment(config: AppConfig) -> ExecutableEnvironment:
    """使用与 Runtime 相同的发现器构造本机 CLI 搜索环境。"""
    roots = _permission_roots(config)
    return discover_executables(
        config.permissions.profile,
        home=roots.owner_home,
        explicit_roots=config.permissions.executable_roots,
        discover_user=config.permissions.discover_user_executables,
        platform_name=sys.platform,
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


def _check_memory(paths: StatePaths) -> CheckResult:
    """只读报告 manifest、Projection、retry/dead-letter、lease 和 legacy 状态。"""
    if not paths.database.is_file() or paths.database.is_symlink():
        return CheckResult("memory", CheckStatus.FAIL, "memory database is unavailable")
    try:
        with Database(paths.database).connect_read_only() as connection:
            manifest_rows = connection.execute(
                """
                SELECT owner_id, content_hash, status FROM memory_manifests
                ORDER BY owner_id
                """
            ).fetchall()
            parser_errors = sum(row["status"] == "error" for row in manifest_rows)
            manifest_drift = sum(row["status"] == "drift" for row in manifest_rows)
            retry_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM memory_flush_runs WHERE status = 'retry'"
                ).fetchone()[0]
            )
            dead_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM memory_flush_runs WHERE status = 'dead_letter'"
                ).fetchone()[0]
            )
            stale_leases = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM memory_flush_runs
                    WHERE status = 'running' AND lease_expires_at <= ?
                    """,
                    (datetime.now(UTC).isoformat(),),
                ).fetchone()[0]
            )
            legacy_count = int(
                connection.execute("SELECT COUNT(*) FROM memory_legacy_imports").fetchone()[0]
            )
            fts_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'memory_fts'"
            ).fetchone() is not None
            projection_drift = 0
            if fts_exists:
                unit_count = int(
                    connection.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
                )
                fts_count = int(
                    connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
                )
                projection_drift = int(unit_count != fts_count)
    except (DatabaseError, sqlite3.Error, OSError):
        return CheckResult("memory", CheckStatus.FAIL, "memory state cannot be inspected")
    filesystem_drift = 0
    for row in manifest_rows:
        path = paths.memory_dir / "owners" / str(int(row["owner_id"])) / "memory.md"
        try:
            if path.is_symlink() or not path.is_file():
                filesystem_drift += 1
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["content_hash"]:
                filesystem_drift += 1
        except OSError:
            filesystem_drift += 1
    status = CheckStatus.PASS
    if parser_errors:
        status = CheckStatus.FAIL
    elif manifest_drift or filesystem_drift or projection_drift or retry_count or dead_count:
        status = CheckStatus.WARN
    elif stale_leases:
        status = CheckStatus.WARN
    return CheckResult(
        "memory",
        status,
        (
            f"parser_errors={parser_errors}; manifest_drift={manifest_drift + filesystem_drift}; "
            f"projection_drift={projection_drift}; retry={retry_count}; "
            f"dead_letter={dead_count}; stale_leases={stale_leases}; "
            f"legacy_imports={legacy_count}"
        ),
    )


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


def _check_feishu_config(config: AppConfig | None) -> CheckResult:
    """离线报告飞书开关、账号和 allowlist 数量。"""
    if config is None:
        return CheckResult(
            "feishu_config",
            CheckStatus.FAIL,
            "Feishu config cannot be checked without valid config",
        )
    feishu = config.channels.feishu
    if not feishu.enabled:
        return CheckResult(
            "feishu_config",
            CheckStatus.PASS,
            "Feishu channel is disabled",
        )
    return CheckResult(
        "feishu_config",
        CheckStatus.PASS,
        (
            f"Feishu account {feishu.account_id} is enabled; "
            f"{len(feishu.allowed_open_ids)} user(s), "
            f"{len(feishu.allowed_chat_ids)} group(s) allowed"
        ),
    )


def _check_telegram_config(config: AppConfig | None) -> CheckResult:
    """报告 Telegram 开关和 allowlist 规模，不显示 user/chat ID。"""
    if config is None:
        return CheckResult(
            "telegram_config",
            CheckStatus.FAIL,
            "Telegram config cannot be checked without valid config",
        )
    selected = config.channels.telegram
    if not selected.enabled:
        return CheckResult(
            "telegram_config",
            CheckStatus.PASS,
            "Telegram channel is disabled; configuration is not required",
        )
    return CheckResult(
        "telegram_config",
        CheckStatus.PASS,
        (
            f"Telegram account {selected.account_id} is enabled; "
            f"{len(selected.allowed_user_ids)} user(s), "
            f"{len(selected.allowed_chat_ids)} group(s) allowed"
        ),
    )


def _check_discord_config(config: AppConfig | None) -> CheckResult:
    """报告 Discord 开关和 allowlist 规模，不显示 snowflake。"""
    if config is None:
        return CheckResult(
            "discord_config",
            CheckStatus.FAIL,
            "Discord config cannot be checked without valid config",
        )
    selected = config.channels.discord
    if not selected.enabled:
        return CheckResult(
            "discord_config",
            CheckStatus.PASS,
            "Discord channel is disabled; configuration is not required",
        )
    return CheckResult(
        "discord_config",
        CheckStatus.PASS,
        (
            f"Discord account {selected.account_id} is enabled; "
            f"{len(selected.allowed_user_ids)} user(s), "
            f"{len(selected.allowed_guild_ids)} guild(s), "
            f"{len(selected.allowed_channel_ids)} channel(s) allowed"
        ),
    )


def _check_channel_sdk(
    config: AppConfig | None,
    channel: str,
    module: str,
    find_spec: Callable[[str], object | None],
) -> CheckResult:
    """只调用 module discovery；不实例化 SDK Client 或执行认证。"""
    name = f"{channel}_sdk"
    display = channel.capitalize()
    if config is None:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"{display} SDK cannot be checked without valid config",
        )
    selected = getattr(config.channels, channel)
    if not selected.enabled:
        return CheckResult(
            name,
            CheckStatus.PASS,
            f"official {display} SDK is not required while channel is disabled",
        )
    if find_spec(module) is None:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"official {display} SDK is missing; run uv sync --extra {channel}",
        )
    return CheckResult(
        name,
        CheckStatus.PASS,
        f"official {display} SDK is installed; no network check was run",
    )


def _check_channel_runtime(
    config: AppConfig | None,
    channel: str,
    environ: Mapping[str, str],
) -> CheckResult:
    """只验证凭据变量存在，不调用 get_me/login/DNS/HTTP。"""
    name = f"{channel}_runtime"
    display = channel.capitalize()
    if config is None:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"{display} runtime cannot be checked without valid config",
        )
    selected = getattr(config.channels, channel)
    if not selected.enabled:
        return CheckResult(
            name,
            CheckStatus.PASS,
            f"{display} runtime is not started by doctor",
        )
    variables = (
        (selected.app_id_env, selected.app_secret_env)
        if channel == "feishu"
        else (selected.bot_token_env,)
    )
    missing = [
        variable
        for variable in variables
        if not str(environ.get(variable, "")).strip()
    ]
    if missing:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"missing environment variable(s): {', '.join(missing)}",
        )
    return CheckResult(
        name,
        CheckStatus.PASS,
        f"{display} credentials are locally ready; no authentication check was run",
    )


def _check_channel_database(paths: StatePaths) -> CheckResult:
    """只读确认共享 Inbox/Outbox/Identity 表和关键索引存在。"""
    name = "channel_database"
    if not paths.database.is_file() or paths.database.is_symlink():
        return CheckResult(name, CheckStatus.FAIL, "Channel database is unavailable")
    required_tables = {"channel_identities", "processed_events", "deliveries"}
    required_indexes = {"processed_events_status_idx", "deliveries_status_idx"}
    try:
        with Database(paths.database).connect_read_only() as connection:
            objects = {
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE type IN ('table', 'index')"
                )
            }
    except (DatabaseError, sqlite3.Error, OSError):
        return CheckResult(
            name,
            CheckStatus.FAIL,
            "Channel database cannot be inspected",
        )
    tables = {object_name for kind, object_name in objects if kind == "table"}
    indexes = {object_name for kind, object_name in objects if kind == "index"}
    missing = sorted((required_tables - tables) | (required_indexes - indexes))
    if missing:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"Channel schema objects are missing: {', '.join(missing)}",
        )
    return CheckResult(
        name,
        CheckStatus.PASS,
        "shared Channel Inbox, Outbox, identity tables and indexes are ready",
    )


def _check_channel_workers(config: AppConfig | None) -> CheckResult:
    """报告 enabled pipelines 的总 worker 预算，不阻止显式高并发配置。"""
    if config is None:
        return CheckResult(
            "channel_workers",
            CheckStatus.FAIL,
            "Channel worker budget cannot be checked without valid config",
        )
    total = sum(
        selected.worker_count
        for selected in (
            config.channels.feishu,
            config.channels.telegram,
            config.channels.discord,
        )
        if selected.enabled
    )
    if total > 8:
        return CheckResult(
            "channel_workers",
            CheckStatus.WARN,
            f"{total} enabled Channel workers exceed the recommended personal limit of 8",
        )
    return CheckResult(
        "channel_workers",
        CheckStatus.PASS,
        f"{total} enabled Channel worker(s) are within the local budget",
    )
