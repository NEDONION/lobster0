"""人工驱动、默认拒绝且不主动发送消息的 Channel live 验收记录器。"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from miniclaw.config import AppConfig, ConfigError, load_config
from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.env import DotEnvError, load_dotenv
from miniclaw.evals.gateway_process import ManagedGateway, ManagedGatewayError
from miniclaw.gateway import GatewayConfigError, validate_gateway_environment
from miniclaw.gateway_lease import GatewayProvenance
from miniclaw.paths import (
    PathConfigurationError,
    StatePaths,
    build_state_paths,
    resolve_home,
)
from miniclaw.storage.database import Database

CHECKLIST: tuple[str, ...] = (
    "auth_ready",
    "dm_twenty_rounds",
    "group_addressing",
    "reply_or_thread",
    "memory_restart",
    "read_tool",
    "approval_approve_deny",
    "non_owner_denied",
    "duplicate_event_once",
    "long_text_split",
    "rate_limit_retry_after",
    "gateway_restart_recovery",
    "network_reconnect",
    "experience_fallback",
    "secret_scan_zero",
)
_CHANNELS = frozenset({"telegram", "discord"})


class LiveHarnessError(ValueError):
    """表示 live harness 在人工动作前发现了静态阻塞。"""


@dataclass(frozen=True, slots=True)
class LiveHarnessExecution:
    """保存人工状态、受管进程来源和有界诊断，不含消息正文。"""

    checks: tuple[dict[str, str], ...]
    provenance: GatewayProvenance | None
    gateway_ready: bool
    gateway_graceful_exit: bool
    gateway_failures: int
    gateway_secret_matches: int
    diagnostics: tuple[str, ...]


def run_live_harness(channel: str, argv: Sequence[str] | None = None) -> int:
    """运行一个只记录人工结论和匿名状态计数的 live harness。"""
    if channel not in _CHANNELS:
        raise ValueError("unsupported live channel")
    arguments = _build_parser(channel).parse_args(argv)
    if not arguments.confirm_live:
        print(
            "error: --confirm-live is required; no config, token, or network was read",
            file=sys.stderr,
        )
        return 2

    started = datetime.now(UTC)
    project_root = Path(__file__).resolve().parents[3]
    try:
        paths, config, secrets = _load_preflight(channel, arguments.home)
        selected = getattr(config.channels, channel)
        _validate_channel_scope(channel, config)
        commit, dirty = _repository_state(project_root)
        if commit == "unknown":
            raise LiveHarnessError("repository commit is unavailable")
        if dirty:
            raise LiveHarnessError("repository is dirty")
    except (
        ConfigError,
        DotEnvError,
        GatewayConfigError,
        LiveHarnessError,
        OSError,
        PathConfigurationError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        execution = asyncio.run(
            _execute_live_checklist(
                channel=channel,
                account_id=selected.account_id,
                project_root=project_root,
                home=paths.home,
                commit=commit,
                secret_values=_secret_values(secrets),
            )
        )
    except ManagedGatewayError as error:
        print(f"error: {error.code}", file=sys.stderr)
        execution = LiveHarnessExecution(
            checks=tuple(
                {
                    "name": name,
                    "status": "fail" if index == 0 else "skip",
                }
                for index, name in enumerate(CHECKLIST)
            ),
            provenance=None,
            gateway_ready=False,
            gateway_graceful_exit=False,
            gateway_failures=1,
            gateway_secret_matches=0,
            diagnostics=(),
        )
    results = list(execution.checks)
    secret_matches = _secret_match_count(paths.logs, _secret_values(secrets))
    secret_matches += execution.gateway_secret_matches
    if secret_matches:
        _force_check_failure(results, "secret_scan_zero")
    current_commit, current_dirty = _repository_state(project_root)
    repository_changed = int(current_commit != commit or current_dirty)
    if repository_changed:
        _force_check_failure(results, "secret_scan_zero")
    if execution.gateway_failures or not execution.gateway_graceful_exit:
        _force_check_failure(results, "auth_ready")
    database = _database_counts(paths.database, channel)
    finished = datetime.now(UTC)
    statuses = [item["status"] for item in results]
    evidence = {
        "channel": channel,
        "commit": commit,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "gateway": _gateway_payload(execution),
        "checks": results,
        "counts": {
            "pass": statuses.count("pass"),
            "fail": statuses.count("fail"),
            "skip": statuses.count("skip"),
            "secret_matches": secret_matches,
            "repository_changed": repository_changed,
            "gateway_failures": execution.gateway_failures,
            "database": database,
        },
    }
    output_dir = arguments.output_dir or (
        Path.cwd() / ".local" / "eval-results" / channel
    )
    try:
        _prepare_output_directory(output_dir)
        target = output_dir / (finished.strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        _write_evidence(target, evidence)
    except (LiveHarnessError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Saved redacted evidence: {target}")
    return 0 if (
        all(status == "pass" for status in statuses)
        and execution.gateway_ready
        and execution.gateway_graceful_exit
        and execution.gateway_failures == 0
        and repository_changed == 0
        and secret_matches == 0
    ) else 1


async def _execute_live_checklist(
    *,
    channel: str,
    account_id: str,
    project_root: Path,
    home: Path,
    commit: str,
    secret_values: tuple[str, ...],
) -> LiveHarnessExecution:
    """启动唯一 Gateway，并在后台持续排空输出时收集人工结论。

    Args:
        channel: 已验证的 telegram 或 discord。
        account_id: typed config 中的本地账号名。
        project_root: 当前 clean worktree。
        home: 当前验收使用的 MiniClaw 状态目录。
        commit: preflight 固定的 40 位 commit。
        secret_values: 只在内存用于完整 stdout/stderr 匿名扫描的 Secret。

    Returns:
        人工状态和受管进程生命周期快照。

    Raises:
        ManagedGatewayError: Gateway 无法 ready 或有界退出。
    """
    gateway = await ManagedGateway.start(
        project_root=project_root,
        home=home,
        ready_line=f"MiniClaw gateway ready: {channel}/{account_id}",
        commit=commit,
        ready_timeout=30.0,
        secret_values=secret_values,
    )
    checks: tuple[dict[str, str], ...] = ()
    graceful = False
    gateway_failures = 0
    print(f"MiniClaw {channel.title()} live acceptance")
    print("1. This harness owns the exact-commit Gateway process.")
    print("2. Use only the configured private test account; the harness sends nothing.")
    print("3. For each check enter p=pass, f=fail, s=skip.")
    try:
        collected: list[dict[str, str]] = []
        for name in CHECKLIST:
            collected.append(
                {
                    "name": name,
                    "status": await asyncio.to_thread(_read_status, name),
                }
            )
        checks = tuple(collected)
    finally:
        try:
            exit_code = await gateway.stop()
            graceful = exit_code == 0
            gateway_failures = int(not graceful)
        except ManagedGatewayError:
            gateway_failures = 1
    return LiveHarnessExecution(
        checks=checks,
        provenance=gateway.provenance,
        gateway_ready=gateway.ready,
        gateway_graceful_exit=graceful,
        gateway_failures=gateway_failures,
        gateway_secret_matches=gateway.secret_match_count,
        diagnostics=gateway.bounded_diagnostics,
    )


def _validate_channel_scope(channel: str, config: AppConfig) -> None:
    """要求 strict Harness 只启用当前目标平台。

    Args:
        channel: 当前目标平台。
        config: 已通过 typed loader 的完整配置。

    Raises:
        LiveHarnessError: 目标未启用或任何 peer Channel 仍启用。
    """
    if not getattr(config.channels, channel).enabled:
        raise LiveHarnessError(f"{channel} channel is disabled")
    peers = _CHANNELS | {"feishu"}
    if any(
        name != channel and getattr(config.channels, name).enabled
        for name in peers
    ):
        raise LiveHarnessError("peer channel is enabled")


def _gateway_payload(execution: LiveHarnessExecution) -> dict[str, object]:
    """把可选本地 provenance 转成固定五字段对象。

    Args:
        execution: 已结束或启动失败的 Harness 执行结果。

    Returns:
        不含命令、路径或平台 ID 的 Gateway evidence。
    """
    provenance = execution.provenance
    return {
        "ready": execution.gateway_ready,
        "graceful_exit": execution.gateway_graceful_exit,
        "pid": provenance.pid if provenance is not None else None,
        "started_at": provenance.started_at if provenance is not None else None,
        "commit": provenance.commit if provenance is not None else None,
    }


def _force_check_failure(results: list[dict[str, str]], name: str) -> None:
    """让自动失败覆盖人工 pass/skip，但不改写其他 check。

    Args:
        results: 当前封闭 15 项结果。
        name: 必须失败的稳定 check 名。
    """
    for result in results:
        if result["name"] == name:
            result["status"] = "fail"
            return


def _write_evidence(path: Path, evidence: Mapping[str, object]) -> None:
    """使用 0600、O_EXCL 和 fsync 写入 ignored Evidence。

    Args:
        path: 不得已存在的目标 JSON 文件。
        evidence: 只含封闭字段和匿名计数的对象。

    Raises:
        OSError: 目标不安全、已存在或写入失败。
    """
    rendered = (
        json.dumps(
            evidence,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _prepare_output_directory(path: Path) -> None:
    """创建或验证 owner-only Evidence 最终目录。

    Args:
        path: 默认 ignored 路径或操作者显式选择的目录。

    Raises:
        LiveHarnessError: 最终路径是 symlink、非目录或权限宽于 0700。
    """
    try:
        if path.is_symlink():
            raise LiveHarnessError("evidence directory is unsafe")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            path.is_symlink()
            or not path.is_dir()
            or path.stat().st_mode & 0o077
        ):
            raise LiveHarnessError("evidence directory is unsafe")
    except LiveHarnessError:
        raise
    except OSError:
        raise LiveHarnessError("evidence directory is unavailable") from None


def _build_parser(channel: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Record human-driven {channel.title()} acceptance without sending messages."
    )
    parser.add_argument("--home", help="absolute MiniClaw state directory")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="confirm a human will interact with the configured test bot",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="redacted evidence directory; defaults to ignored .local/eval-results",
    )
    return parser


def _load_preflight(channel: str, home: str | None) -> tuple[StatePaths, AppConfig, Any]:
    """在显式确认后加载安全环境，并运行 Doctor 与全 Gateway preflight。"""
    del channel
    environment = dict(os.environ)
    load_dotenv(Path.cwd() / ".env", environment)
    paths = build_state_paths(resolve_home(home, environment))
    config = load_config(paths, environment)
    checks = run_local_checks(paths, environment)
    failed = sorted(item.name for item in checks if item.status is CheckStatus.FAIL)
    if failed:
        raise LiveHarnessError("doctor preflight failed: " + ", ".join(failed))
    secrets = validate_gateway_environment(config, environment)
    return paths, config, secrets


def _read_status(name: str) -> str:
    while True:
        try:
            value = input(f"{name} [p/f/s]: ").strip().lower()
        except EOFError:
            return "skip"
        if value in {"p", "f", "s"}:
            return {"p": "pass", "f": "fail", "s": "skip"}[value]


def _repository_state(project_root: Path) -> tuple[str, bool]:
    """有界读取当前 HEAD 与 dirty 状态，不读取 diff 或正文。

    Args:
        project_root: 当前 MiniClaw worktree 根目录。

    Returns:
        40 位 commit 或 unknown，以及 fail-closed dirty 标志。
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown", True
    value = head.stdout.strip().lower()
    if (
        head.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40}", value) is None
        or status.returncode != 0
    ):
        return "unknown", True
    return value, bool(status.stdout.strip())


def _secret_values(secrets: Any) -> tuple[str, ...]:
    """只在内存中收集 preflight 已读取的 Secret，绝不返回到 evidence。"""
    values = [getattr(secrets, "model_api_key", "")]
    tokens = getattr(secrets, "channel_tokens", {})
    if isinstance(tokens, Mapping):
        values.extend(tokens.values())
    return tuple(
        value
        for value in values
        if isinstance(value, str) and len(value.encode("utf-8")) >= 4
    )


def _secret_match_count(logs: Path, secrets: tuple[str, ...]) -> int:
    """有界扫描本地日志中的精确 Secret 字节，不读取 symlink 或大文件。"""
    if not logs.is_dir() or not secrets:
        return 0
    needles = tuple(value.encode("utf-8") for value in secrets)
    matches = 0
    scanned = 0
    for path in sorted(logs.rglob("*"), key=lambda item: item.as_posix()):
        if scanned >= 1000 or path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size > 1024 * 1024:
                continue
            content = path.read_bytes()
        except OSError:
            continue
        scanned += 1
        matches += sum(content.count(needle) for needle in needles)
    return matches


def _database_counts(path: Path, channel: str) -> dict[str, int]:
    """只按 channel/status 聚合，不读取正文、平台 ID 或用户名。"""
    try:
        with Database(path).connect_read_only() as connection:
            counts: dict[str, int] = {}
            for prefix, table in (("inbox", "processed_events"), ("delivery", "deliveries")):
                rows = connection.execute(
                    f"SELECT status, COUNT(*) FROM {table} "  # noqa: S608 - table is fixed above
                    "WHERE channel = ? GROUP BY status",
                    (channel,),
                )
                counts.update({f"{prefix}_{row[0]}": int(row[1]) for row in rows})
            return counts
    except Exception:  # noqa: BLE001 - evidence only exposes stable unavailable count
        return {"unavailable": 1}
