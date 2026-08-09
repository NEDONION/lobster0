"""Phase 6 macOS + 飞书 production soak 的只读 invariant monitor。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.evals.production_evidence import (
    ProductionEvidenceError,
    scan_secret_matches,
)
from miniclaw.install.service import ServiceError, ServiceStatus
from miniclaw.storage.database import Database, DatabaseError

SAMPLE_CADENCE_SECONDS = 60
MAX_MONITOR_GAP_SECONDS = 180
_STALE_RUNTIME_SECONDS = 300
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class SoakMonitorError(RuntimeError):
    """表示 monitor 只公开的稳定失败码。"""

    def __init__(self, code: str) -> None:
        """保存不含路径、SQL、PID 或底层异常的错误码。

        Args:
            code: 稳定小写错误码。
        """
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SoakSnapshot:
    """保存一次只读采样得到的匿名 production aggregate。"""

    observed_at: str
    service_loaded: bool
    service_running: bool
    gateway_lease_fresh: bool
    database_healthy: bool
    running_turns: int
    stale_task_runs: int
    pending_deliveries: int
    failed_deliveries: int
    pending_approvals: int
    secret_matches: int
    owner_only_state: bool

    def __post_init__(self) -> None:
        """拒绝非法时间、bool 冒充 count 和负数 aggregate。"""
        _parse_timestamp(self.observed_at)
        booleans = (
            self.service_loaded,
            self.service_running,
            self.gateway_lease_fresh,
            self.database_healthy,
            self.owner_only_state,
        )
        counts = (
            self.running_turns,
            self.stale_task_runs,
            self.pending_deliveries,
            self.failed_deliveries,
            self.pending_approvals,
            self.secret_matches,
        )
        if any(type(value) is not bool for value in booleans) or any(
            type(value) is not int or value < 0 for value in counts
        ):
            raise ValueError("invalid soak snapshot")


@dataclass(frozen=True, slots=True)
class SoakViolation:
    """保存一个不携带本机详情的固定 invariant code。"""

    code: str

    def __post_init__(self) -> None:
        """只允许稳定小写错误码。"""
        if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", self.code) is None:
            raise ValueError("invalid soak violation")


def collect_soak_snapshot(
    *,
    service_status: Callable[[], ServiceStatus],
    database: Database,
    lease_check: Callable[[], bool],
    private_paths: Sequence[Path],
    evidence_paths: Sequence[Path] = (),
    secrets: Sequence[str] = (),
    now: datetime | None = None,
) -> SoakSnapshot:
    """读取 service、lease、SQLite、权限和 Secret scan 的封闭事实。

    Args:
        service_status: PROD-A LaunchdService.status 的无参读取函数。
        database: 已存在的 MiniClaw SQLite。
        lease_check: 验证 expected commit 与活跃文件锁的只读函数。
        private_paths: 必须由当前用户私有持有的状态/Evidence 路径。
        evidence_paths: 允许做有界 exact Secret scan 的路径。
        secrets: 只在内存中参与 exact scan 的 Secret 值。
        now: 测试可注入的 aware UTC 时间。

    Returns:
        不含路径、PID、正文、平台 ID 和 Secret 值的 SoakSnapshot。

    Raises:
        SoakMonitorError: service、lease、SQLite 或 Secret scan 无法安全读取。
        ValueError: ``now`` 不是 aware datetime。
    """
    observed = _aware_utc(now or datetime.now(UTC))
    try:
        service = service_status()
    except (OSError, ServiceError):
        raise SoakMonitorError("service_query_failed") from None
    if not isinstance(service, ServiceStatus):
        raise SoakMonitorError("service_query_failed")
    try:
        lease_fresh = lease_check()
    except (OSError, SoakMonitorError):
        raise SoakMonitorError("gateway_lease_query_failed") from None
    if type(lease_fresh) is not bool:
        raise SoakMonitorError("gateway_lease_query_failed")

    try:
        counts = _database_counts(database, observed)
    except (DatabaseError, OSError, sqlite3.Error, TypeError, ValueError):
        raise SoakMonitorError("database_query_failed") from None
    try:
        secret_matches = scan_secret_matches(evidence_paths, secrets)
    except ProductionEvidenceError:
        raise SoakMonitorError("secret_scan_failed") from None

    return SoakSnapshot(
        observed_at=_timestamp(observed),
        service_loaded=service.loaded,
        service_running=service.running,
        gateway_lease_fresh=lease_fresh,
        database_healthy=counts[0],
        running_turns=counts[1],
        stale_task_runs=counts[2],
        pending_deliveries=counts[3],
        failed_deliveries=counts[4],
        pending_approvals=counts[5],
        secret_matches=secret_matches,
        owner_only_state=_paths_are_private(private_paths),
    )


def evaluate_snapshot(
    snapshot: SoakSnapshot,
    *,
    previous_observed_at: datetime | None = None,
) -> tuple[SoakViolation, ...]:
    """把一次 snapshot 转成稳定排序的 invariant violations。

    Args:
        snapshot: 已验证的封闭采样结果。
        previous_observed_at: 上次采样时间，用于发现 wall-clock rollback。

    Returns:
        按错误码排序且不含底层详情的 violations。

    Raises:
        ValueError: previous time 不是 aware datetime。
    """
    observed = _parse_timestamp(snapshot.observed_at)
    previous = None if previous_observed_at is None else _aware_utc(previous_observed_at)
    checks = {
        "clock_rollback": previous is not None and observed < previous,
        "database_unhealthy": not snapshot.database_healthy,
        "delivery_backlog": snapshot.pending_deliveries > 0,
        "delivery_failed": snapshot.failed_deliveries > 0,
        "gateway_lease_unhealthy": not snapshot.gateway_lease_fresh,
        "orphan_approval": snapshot.pending_approvals > 0,
        "secret_match": snapshot.secret_matches > 0,
        "service_not_running": not snapshot.service_running,
        "service_unloaded": not snapshot.service_loaded,
        "stale_task_run": snapshot.stale_task_runs > 0,
        "state_permissions_unsafe": not snapshot.owner_only_state,
        "stuck_turn": snapshot.running_turns > 0,
    }
    failed_codes = sorted(code for code, failed in checks.items() if failed)
    return tuple(SoakViolation(code) for code in failed_codes)


def gateway_lease_is_fresh(path: Path, expected_commit: str) -> bool:
    """验证 Gateway lease 是私有普通文件、commit 匹配且正被其他进程持锁。

    Args:
        path: 固定状态目录中的 `gateway.lock`。
        expected_commit: 当前 release candidate 的完整 Git commit。

    Returns:
        lease 内容有效且独占锁当前被持有时返回 ``True``；否则返回 ``False``。

    Raises:
        SoakMonitorError: 输入路径或 commit 本身无效。
    """
    if not isinstance(path, Path) or not path.is_absolute() or _COMMIT.fullmatch(
        expected_commit
    ) is None:
        raise SoakMonitorError("gateway_lease_query_failed")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size > 4096
        ):
            return False
        payload = json.loads(os.read(descriptor, 4097).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return False
        if payload.get("commit") != expected_commit or type(payload.get("pid")) is not int:
            return False
        _parse_timestamp(payload.get("started_at"))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return False
    finally:
        os.close(descriptor)


def _database_counts(
    database: Database,
    now: datetime,
) -> tuple[bool, int, int, int, int, int]:
    """执行只读 quick_check 和固定 count 查询。"""
    cutoff = (now - timedelta(seconds=_STALE_RUNTIME_SECONDS)).isoformat()
    current = now.isoformat()
    with database.connect_read_only() as connection:
        rows = connection.execute("PRAGMA quick_check").fetchall()
        healthy = len(rows) == 1 and str(rows[0][0]).lower() == "ok"
        running_turns = _count(
            connection,
            "SELECT COUNT(*) FROM turns WHERE status = 'running' "
            "AND (started_at IS NULL OR started_at <= ?)",
            cutoff,
        )
        stale_task_runs = _count(
            connection,
            "SELECT COUNT(*) FROM task_runs WHERE status IN ('claimed', 'running') "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
            current,
        )
        pending_deliveries = _count(
            connection,
            "SELECT COUNT(*) FROM deliveries "
            "WHERE status IN ('queued', 'sending', 'retry_wait', 'unknown') "
            "AND updated_at <= ?",
            cutoff,
        )
        failed_deliveries = _count(
            connection,
            "SELECT COUNT(*) FROM deliveries WHERE status = 'failed'",
        )
        pending_approvals = _count(
            connection,
            "SELECT COUNT(*) FROM approvals WHERE status = 'pending' AND expires_at <= ?",
            current,
        )
    return (
        healthy,
        running_turns,
        stale_task_runs,
        pending_deliveries,
        failed_deliveries,
        pending_approvals,
    )


def _count(connection: sqlite3.Connection, query: str, *parameters: str) -> int:
    """执行一个固定 COUNT 查询并拒绝非法返回值。"""
    row = connection.execute(query, parameters).fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise ValueError("invalid database count")
    return int(row[0])


def _paths_are_private(paths: Sequence[Path]) -> bool:
    """要求给定文件/目录真实存在、无 symlink 且不向 group/other 开放。"""
    for path in paths:
        if not isinstance(path, Path) or not path.is_absolute():
            return False
        try:
            metadata = path.lstat()
        except OSError:
            return False
        if (
            stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
        ):
            return False
    return True


def _timestamp(value: datetime) -> str:
    """把 aware datetime 规范化成带微秒的 UTC 文本。"""
    return _aware_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: object) -> datetime:
    """解析固定 UTC timestamp，拒绝其他文本。"""
    if not isinstance(value, str):
        raise ValueError("invalid UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("invalid UTC timestamp") from None
    return parsed


def _aware_utc(value: datetime) -> datetime:
    """要求 aware datetime 并转换到 UTC。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)
