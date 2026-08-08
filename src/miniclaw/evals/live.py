"""人工驱动、默认拒绝且不主动发送消息的 Channel live 验收记录器。"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from miniclaw.config import AppConfig, ConfigError, load_config
from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.env import DotEnvError, load_dotenv
from miniclaw.gateway import GatewayConfigError, validate_gateway_environment
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
    try:
        paths, config, secrets = _load_preflight(channel, arguments.home)
        if not getattr(config.channels, channel).enabled:
            raise LiveHarnessError(f"{channel} channel is disabled")
        commit = _commit()
        if commit == "unknown":
            raise LiveHarnessError("repository commit is unavailable")
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

    print(f"MiniClaw {channel.title()} live acceptance")
    print("1. In another terminal run: uv run miniclaw gateway")
    print("2. Use only the configured private test account; this recorder sends nothing.")
    print("3. For each check enter p=pass, f=fail, s=skip.")
    results = [
        {"name": name, "status": _read_status(name)}
        for name in CHECKLIST
    ]
    secret_matches = _secret_match_count(paths.logs, _secret_values(secrets))
    if secret_matches:
        for result in results:
            if result["name"] == "secret_scan_zero":
                result["status"] = "fail"
                break
    database = _database_counts(paths.database, channel)
    finished = datetime.now(UTC)
    statuses = [item["status"] for item in results]
    evidence = {
        "channel": channel,
        "commit": commit,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "checks": results,
        "counts": {
            "pass": statuses.count("pass"),
            "fail": statuses.count("fail"),
            "skip": statuses.count("skip"),
            "secret_matches": secret_matches,
            "database": database,
        },
    }
    output_dir = arguments.output_dir or (
        Path.cwd() / ".local" / "eval-results" / channel
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / (finished.strftime("%Y%m%dT%H%M%SZ") + ".json")
    target.write_text(
        json.dumps(
            evidence,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Saved redacted evidence: {target}")
    return 0 if all(status == "pass" for status in statuses) else 1


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


def _commit() -> str:
    """从源码根读取精确 SHA；dirty 状态由最终 release gate 另行记录。"""
    project_root = Path(__file__).resolve().parents[3]
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
    value = result.stdout.strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else "unknown"


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
