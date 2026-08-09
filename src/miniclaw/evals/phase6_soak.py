"""Phase 6 macOS + 飞书 production soak 的只读 invariant monitor。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
_CHECKPOINT_SCHEMA = 1
_SOAK_STATUSES = frozenset({"running", "failed", "passed"})
_RESTART_STATUSES = frozenset({"pending", "passed", "failed"})
_CLOCK_TOLERANCE_SECONDS = 5.0


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


@dataclass(frozen=True, slots=True)
class SoakCheckpoint:
    """保存可恢复且不含路径、PID、正文或 Secret 的 soak 状态。"""

    schema_version: int
    commit: str
    run_token_hash: str
    state_home_hash: str
    started_at: str
    last_observed_at: str
    required_at: str
    duration_seconds: int
    elapsed_seconds: int
    sample_count: int
    restart_status: str
    violation_codes: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        """验证 checkpoint 的封闭 schema 和状态约束。"""
        if (
            self.schema_version != _CHECKPOINT_SCHEMA
            or _COMMIT.fullmatch(self.commit) is None
            or any(
                re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in (self.run_token_hash, self.state_home_hash)
            )
            or type(self.duration_seconds) is not int
            or self.duration_seconds <= 0
            or type(self.elapsed_seconds) is not int
            or self.elapsed_seconds < 0
            or type(self.sample_count) is not int
            or self.sample_count < 0
            or self.restart_status not in _RESTART_STATUSES
            or self.status not in _SOAK_STATUSES
        ):
            raise ValueError("invalid soak checkpoint")
        started = _parse_timestamp(self.started_at)
        last = _parse_timestamp(self.last_observed_at)
        required = _parse_timestamp(self.required_at)
        if last < started or required != started + timedelta(seconds=self.duration_seconds):
            raise ValueError("invalid soak checkpoint")
        if tuple(sorted(set(self.violation_codes))) != self.violation_codes or any(
            re.fullmatch(r"[a-z][a-z0-9_]{2,63}", code) is None
            for code in self.violation_codes
        ):
            raise ValueError("invalid soak checkpoint")
        if self.status == "passed" and (
            self.elapsed_seconds < self.duration_seconds
            or last < required
            or self.sample_count == 0
            or self.restart_status != "passed"
            or self.violation_codes
        ):
            raise ValueError("invalid soak checkpoint")
        if self.status == "failed" and not self.violation_codes:
            raise ValueError("invalid soak checkpoint")

    def as_json(self) -> dict[str, object]:
        """返回固定字段、canonical-friendly 的 JSON object。"""
        return {
            "commit": self.commit,
            "duration_seconds": self.duration_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "last_observed_at": self.last_observed_at,
            "required_at": self.required_at,
            "restart_status": self.restart_status,
            "run_token_hash": self.run_token_hash,
            "sample_count": self.sample_count,
            "schema_version": self.schema_version,
            "started_at": self.started_at,
            "state_home_hash": self.state_home_hash,
            "status": self.status,
            "violation_codes": list(self.violation_codes),
        }


@dataclass(slots=True)
class SoakSession:
    """保存当前 monitor 进程的非持久化 monotonic 采样游标。"""

    checkpoint_path: Path = field(repr=False)
    commit: str
    run_token_hash: str
    state_home_hash: str
    duration_seconds: int
    last_monotonic: float = field(repr=False)
    resume_gap_seconds: float = field(default=0.0, repr=False)


def start_soak(
    checkpoint_path: Path,
    *,
    commit: str,
    run_token: str,
    state_home: Path,
    duration_seconds: int = 86_400,
    now: datetime | None = None,
    monotonic_now: float,
) -> tuple[SoakSession, SoakCheckpoint]:
    """独占创建一个 running soak checkpoint。

    Args:
        checkpoint_path: owner-only Evidence 目录中的新 JSON 文件。
        commit: 当前 clean release candidate 的完整 commit。
        run_token: 仅由 operator 持有的本次 run token，文件只保存 hash。
        state_home: 本次绑定的 MiniClaw state home。
        duration_seconds: 要求的健康时长；production CLI 固定为 86400。
        now: 测试可注入的 aware UTC 时间。
        monotonic_now: 当前进程 monotonic 起点。

    Returns:
        非持久化 session 与初始 checkpoint。

    Raises:
        SoakMonitorError: 输入、目录、权限或 exclusive create 不安全。
    """
    identity = _session_identity(commit, run_token, state_home, duration_seconds)
    observed = _aware_utc(now or datetime.now(UTC))
    _validate_monotonic(monotonic_now)
    checkpoint = SoakCheckpoint(
        schema_version=_CHECKPOINT_SCHEMA,
        commit=identity[0],
        run_token_hash=identity[1],
        state_home_hash=identity[2],
        started_at=_timestamp(observed),
        last_observed_at=_timestamp(observed),
        required_at=_timestamp(observed + timedelta(seconds=duration_seconds)),
        duration_seconds=duration_seconds,
        elapsed_seconds=0,
        sample_count=0,
        restart_status="pending",
        violation_codes=(),
        status="running",
    )
    _create_checkpoint(checkpoint_path, checkpoint)
    return (
        SoakSession(
            checkpoint_path,
            identity[0],
            identity[1],
            identity[2],
            duration_seconds,
            monotonic_now,
        ),
        checkpoint,
    )


def resume_soak(
    checkpoint_path: Path,
    *,
    commit: str,
    run_token: str,
    state_home: Path,
    duration_seconds: int = 86_400,
    now: datetime | None = None,
    monotonic_now: float,
) -> tuple[SoakSession, SoakCheckpoint]:
    """验证 durable identity，并从不超过 180 秒的 monitor gap 恢复。

    Args:
        checkpoint_path: 既有私有 checkpoint。
        commit: 必须与 start 时一致的 commit。
        run_token: 必须与 start 时一致的明文 token。
        state_home: 必须与 start 时一致的绝对状态根。
        duration_seconds: 必须与 start 时一致的时长。
        now: 当前 aware UTC 时间。
        monotonic_now: 新 monitor 进程的 monotonic 起点。

    Returns:
        可继续采样的 session；已 passed 时只读返回 terminal checkpoint。

    Raises:
        SoakMonitorError: identity 不匹配、checkpoint 已失败或 gap/clock 不安全。
    """
    identity = _session_identity(commit, run_token, state_home, duration_seconds)
    current = load_soak_checkpoint(checkpoint_path)
    _require_identity(current, identity)
    _validate_monotonic(monotonic_now)
    if current.status == "failed":
        raise SoakMonitorError("soak_failed")
    session = SoakSession(
        checkpoint_path,
        identity[0],
        identity[1],
        identity[2],
        duration_seconds,
        monotonic_now,
    )
    if current.status == "passed":
        return session, current
    observed = _aware_utc(now or datetime.now(UTC))
    last = _parse_timestamp(current.last_observed_at)
    gap = (observed - last).total_seconds()
    if gap < 0 or gap > MAX_MONITOR_GAP_SECONDS:
        code = "clock_rollback" if gap < 0 else "monitor_gap"
        _fail_checkpoint(checkpoint_path, current, (code,))
        raise SoakMonitorError("soak_failed")
    session.resume_gap_seconds = gap
    return session, current


def record_snapshot(
    session: SoakSession,
    snapshot: SoakSnapshot,
    *,
    monotonic_now: float,
) -> SoakCheckpoint:
    """原子记录一个 sample，并在时间或 invariant 异常时永久失败。

    Args:
        session: start/resume 返回的当前进程 session。
        snapshot: 只读 collector 的封闭结果。
        monotonic_now: 当前 monotonic 时间。

    Returns:
        写入后的 checkpoint；相同 UTC sample 幂等返回。

    Raises:
        SoakMonitorError: session identity、checkpoint 或 monotonic 输入不安全。
    """
    _validate_monotonic(monotonic_now)
    current = load_soak_checkpoint(session.checkpoint_path)
    _require_identity(
        current,
        (session.commit, session.run_token_hash, session.state_home_hash, session.duration_seconds),
    )
    if current.status != "running":
        return current
    previous = _parse_timestamp(current.last_observed_at)
    observed = _parse_timestamp(snapshot.observed_at)
    violations = {item.code for item in evaluate_snapshot(snapshot, previous_observed_at=previous)}
    wall_delta = (observed - previous).total_seconds()
    if wall_delta == 0 and not violations:
        return current
    monotonic_delta = monotonic_now - session.last_monotonic
    expected_monotonic = wall_delta - session.resume_gap_seconds
    if monotonic_delta < 0:
        violations.add("monotonic_rollback")
    if wall_delta > MAX_MONITOR_GAP_SECONDS or monotonic_delta > MAX_MONITOR_GAP_SECONDS:
        violations.add("monitor_gap")
    if (
        wall_delta >= 0
        and monotonic_delta >= 0
        and abs(monotonic_delta - expected_monotonic) > _CLOCK_TOLERANCE_SECONDS
    ):
        violations.add("clock_jump")
    if violations:
        failed = _fail_checkpoint(session.checkpoint_path, current, tuple(violations))
        session.last_monotonic = monotonic_now
        session.resume_gap_seconds = 0.0
        return failed
    elapsed_delta = session.resume_gap_seconds + monotonic_delta
    updated = replace(
        current,
        last_observed_at=snapshot.observed_at,
        elapsed_seconds=int(round(current.elapsed_seconds + elapsed_delta)),
        sample_count=current.sample_count + 1,
    )
    _replace_checkpoint(session.checkpoint_path, updated)
    session.last_monotonic = monotonic_now
    session.resume_gap_seconds = 0.0
    return updated


def record_restart_result(session: SoakSession, *, passed: bool) -> SoakCheckpoint:
    """持久化强制 service recovery case 的封闭结果。

    Args:
        session: 当前 soak session。
        passed: restart 与 exactly-one Delivery 是否全部通过。

    Returns:
        更新后的 checkpoint；失败会把整个 soak 终结为 failed。

    Raises:
        SoakMonitorError: ``passed`` 不是 bool 或 session/checkpoint 不匹配。
    """
    if type(passed) is not bool:
        raise SoakMonitorError("restart_result_invalid")
    current = load_soak_checkpoint(session.checkpoint_path)
    _require_identity(
        current,
        (session.commit, session.run_token_hash, session.state_home_hash, session.duration_seconds),
    )
    if current.status != "running":
        return current
    if not passed:
        return _fail_checkpoint(session.checkpoint_path, current, ("service_restart_failed",))
    updated = replace(current, restart_status="passed")
    _replace_checkpoint(session.checkpoint_path, updated)
    return updated


def finish_soak(session: SoakSession) -> SoakCheckpoint:
    """仅在时长、sample、restart 和 invariant 全部满足时标记 passed。

    Args:
        session: 当前 soak session。

    Returns:
        terminal 或仍为 running 的 checkpoint；不足时长不会改写成 PASS。

    Raises:
        SoakMonitorError: session identity 不匹配。
    """
    current = load_soak_checkpoint(session.checkpoint_path)
    _require_identity(
        current,
        (session.commit, session.run_token_hash, session.state_home_hash, session.duration_seconds),
    )
    if current.status != "running":
        return current
    if (
        current.elapsed_seconds < current.duration_seconds
        or _parse_timestamp(current.last_observed_at) < _parse_timestamp(current.required_at)
        or current.sample_count == 0
        or current.restart_status != "passed"
        or current.violation_codes
    ):
        return current
    passed = replace(current, status="passed")
    _replace_checkpoint(session.checkpoint_path, passed)
    return passed


def load_soak_checkpoint(path: Path) -> SoakCheckpoint:
    """安全读取 owner-only checkpoint 并验证精确 schema。

    Args:
        path: checkpoint 绝对路径。

    Returns:
        已验证的 SoakCheckpoint。

    Raises:
        SoakMonitorError: 路径、权限、JSON 或字段无效。
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise SoakMonitorError("checkpoint_unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise SoakMonitorError("checkpoint_unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size > 64 * 1024
        ):
            raise SoakMonitorError("checkpoint_unsafe")
        payload = json.loads(os.read(descriptor, 64 * 1024 + 1).decode("utf-8"))
        return _checkpoint_from_json(payload)
    except SoakMonitorError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise SoakMonitorError("checkpoint_invalid") from None
    finally:
        os.close(descriptor)


def render_progress(checkpoint: SoakCheckpoint) -> str:
    """渲染不含 commit、路径、ID、正文或 Secret 的单行进度。"""
    return (
        f"status={checkpoint.status} elapsed={_duration(checkpoint.elapsed_seconds)} "
        f"required={_duration(checkpoint.duration_seconds)} "
        f"samples={checkpoint.sample_count} violations={len(checkpoint.violation_codes)}"
    )


def write_progress(path: Path, checkpoint: SoakCheckpoint) -> None:
    """原子覆盖可选 owner progress file，失败只返回固定错误码。

    Args:
        path: 用户选择的绝对外部进度文件。
        checkpoint: 要渲染的封闭状态。

    Raises:
        SoakMonitorError: 父目录或写入操作不安全。
    """
    if not isinstance(path, Path) or not path.is_absolute() or not _owned_directory(path.parent):
        raise SoakMonitorError("progress_write_failed")
    if path.exists() or path.is_symlink():
        try:
            metadata = path.lstat()
        except OSError:
            raise SoakMonitorError("progress_write_failed") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise SoakMonitorError("progress_write_failed")
    payload = (render_progress(checkpoint) + "\n").encode("utf-8")
    _atomic_replace(path, payload, "progress_write_failed")


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


def _session_identity(
    commit: str,
    run_token: str,
    state_home: Path,
    duration_seconds: int,
) -> tuple[str, str, str, int]:
    """校验 session 输入并返回不含明文 token/path 的 identity。"""
    if (
        not isinstance(commit, str)
        or _COMMIT.fullmatch(commit) is None
        or not isinstance(run_token, str)
        or not 8 <= len(run_token) <= 512
        or any(ord(character) < 32 for character in run_token)
        or not isinstance(state_home, Path)
        or not state_home.is_absolute()
        or type(duration_seconds) is not int
        or duration_seconds <= 0
    ):
        raise SoakMonitorError("soak_identity_invalid")
    if state_home.exists() and not _paths_are_private((state_home,)):
        raise SoakMonitorError("soak_identity_invalid")
    return (
        commit,
        hashlib.sha256(run_token.encode("utf-8")).hexdigest(),
        hashlib.sha256(str(state_home).encode("utf-8")).hexdigest(),
        duration_seconds,
    )


def _require_identity(
    checkpoint: SoakCheckpoint,
    identity: tuple[str, str, str, int],
) -> None:
    """要求 checkpoint 与当前 commit/run/home/duration 完全相同。"""
    actual = (
        checkpoint.commit,
        checkpoint.run_token_hash,
        checkpoint.state_home_hash,
        checkpoint.duration_seconds,
    )
    if actual != identity:
        raise SoakMonitorError("soak_identity_mismatch")


def _create_checkpoint(path: Path, checkpoint: SoakCheckpoint) -> None:
    """以 0600、O_EXCL、fsync 创建第一份 checkpoint。"""
    if not isinstance(path, Path) or not path.is_absolute() or not _private_directory(path.parent):
        raise SoakMonitorError("checkpoint_unsafe")
    payload = _checkpoint_bytes(checkpoint)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise SoakMonitorError("checkpoint_exists") from None
    except OSError:
        raise SoakMonitorError("checkpoint_write_failed") from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SoakMonitorError("checkpoint_write_failed") from None


def _replace_checkpoint(path: Path, checkpoint: SoakCheckpoint) -> None:
    """使用同目录 0600 临时文件、fsync 和 os.replace 更新 checkpoint。"""
    load_soak_checkpoint(path)
    _atomic_replace(path, _checkpoint_bytes(checkpoint), "checkpoint_write_failed")


def _fail_checkpoint(
    path: Path,
    current: SoakCheckpoint,
    codes: Sequence[str],
) -> SoakCheckpoint:
    """幂等聚合固定 violation codes，并把 running session 终结为 failed。"""
    merged = tuple(sorted(set((*current.violation_codes, *codes))))
    failed = replace(current, status="failed", restart_status=(
        "failed" if "service_restart_failed" in merged else current.restart_status
    ), violation_codes=merged)
    _replace_checkpoint(path, failed)
    return failed


def _checkpoint_bytes(checkpoint: SoakCheckpoint) -> bytes:
    """把 checkpoint 渲染成稳定 UTF-8 JSON。"""
    return (
        json.dumps(
            checkpoint.as_json(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _checkpoint_from_json(payload: object) -> SoakCheckpoint:
    """从精确字段 JSON 构造已验证 checkpoint。"""
    keys = {
        "commit",
        "duration_seconds",
        "elapsed_seconds",
        "last_observed_at",
        "required_at",
        "restart_status",
        "run_token_hash",
        "sample_count",
        "schema_version",
        "started_at",
        "state_home_hash",
        "status",
        "violation_codes",
    }
    if not isinstance(payload, Mapping) or set(payload) != keys:
        raise SoakMonitorError("checkpoint_invalid")
    violations = payload["violation_codes"]
    if not isinstance(violations, list) or not all(isinstance(item, str) for item in violations):
        raise SoakMonitorError("checkpoint_invalid")
    try:
        return SoakCheckpoint(
            schema_version=payload["schema_version"],
            commit=payload["commit"],
            run_token_hash=payload["run_token_hash"],
            state_home_hash=payload["state_home_hash"],
            started_at=payload["started_at"],
            last_observed_at=payload["last_observed_at"],
            required_at=payload["required_at"],
            duration_seconds=payload["duration_seconds"],
            elapsed_seconds=payload["elapsed_seconds"],
            sample_count=payload["sample_count"],
            restart_status=payload["restart_status"],
            violation_codes=tuple(violations),
            status=payload["status"],
        )
    except (TypeError, ValueError):
        raise SoakMonitorError("checkpoint_invalid") from None


def _atomic_replace(path: Path, payload: bytes, error_code: str) -> None:
    """把 bounded payload 原子替换到同目录并同步目录。"""
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".miniclaw-soak-", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError:
        raise SoakMonitorError(error_code) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _private_directory(path: Path) -> bool:
    """验证 checkpoint 父目录是当前用户的 0700 真实目录。"""
    return _owned_directory(path) and not path.lstat().st_mode & 0o077


def _owned_directory(path: Path) -> bool:
    """验证路径是当前用户拥有且非 symlink 的真实目录。"""
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
    )


def _fsync_directory(path: Path) -> None:
    """同步父目录，保证 rename/create 的目录项尽量 durable。"""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_monotonic(value: float) -> None:
    """拒绝 bool、负数、NaN 和无穷 monotonic 输入。"""
    if type(value) not in {int, float} or value < 0 or value != value or value == float("inf"):
        raise SoakMonitorError("monotonic_clock_invalid")


def _duration(seconds: int) -> str:
    """把非负秒数渲染成可超过 24 小时的 HH:MM:SS。"""
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
