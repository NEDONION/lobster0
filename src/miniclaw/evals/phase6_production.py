"""Phase 6 当前 Mac + 飞书 production gate 的薄编排与 aggregate report。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.channels.supervisor import collect_enabled_channels
from miniclaw.config import AppConfig, load_config
from miniclaw.env import load_dotenv, resolve_dotenv_path
from miniclaw.evals.feishu_automation_live import validate_automation_evidence_report
from miniclaw.evals.feishu_live import validate_evidence_report
from miniclaw.evals.phase6_soak import (
    SAMPLE_CADENCE_SECONDS,
    SoakCheckpoint,
    SoakSession,
    SoakViolation,
    collect_soak_snapshot,
    fail_soak,
    finish_soak,
    gateway_lease_is_fresh,
    load_soak_checkpoint,
    record_restart_result,
    record_snapshot,
    render_progress,
    resume_soak,
    start_soak,
    write_progress,
)
from miniclaw.evals.production_evidence import (
    ProductionEvidenceError,
    clean_repository_commit,
    validate_seatbelt_evidence_report,
    write_private_json,
)
from miniclaw.gateway import validate_gateway_environment
from miniclaw.install.service import (
    LaunchdService,
    ServiceSpec,
    ServiceStatus,
)
from miniclaw.paths import StatePaths, build_state_paths, resolve_home
from miniclaw.storage.channels import DeliveryRepository
from miniclaw.storage.conversations import MessageRepository
from miniclaw.storage.database import Database

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_EVIDENCE_LAYOUT = {
    "seatbelt": ("seatbelt", 2),
    "feishu_channel": ("feishu-channel", 1),
    "feishu_automation": ("feishu-automation", 1),
}
_DEEPSEEK_CHECKS = frozenset({"normal", "tool", "approval"})


class ProductionGateError(RuntimeError):
    """表示 production orchestrator 只公开的稳定错误码。"""

    def __init__(self, code: str) -> None:
        """保存不含路径、PID、正文、SQL 或底层异常的错误码。"""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """保存一类 private Evidence 的脱敏 aggregate 与文件 hash。"""

    kind: str
    commit: str
    status: str
    total: int
    passed: int
    secret_matches: int
    sha256: str

    def __post_init__(self) -> None:
        """验证 Evidence aggregate 的封闭字段。"""
        if (
            self.kind not in _EVIDENCE_LAYOUT
            or _COMMIT.fullmatch(self.commit) is None
            or self.status not in {"verified", "failed"}
            or type(self.total) is not int
            or self.total < 0
            or type(self.passed) is not int
            or not 0 <= self.passed <= self.total
            or type(self.secret_matches) is not int
            or self.secret_matches < 0
            or _HASH.fullmatch(self.sha256) is None
        ):
            raise ValueError("invalid gate evidence")


@dataclass(frozen=True, slots=True)
class ProductionPreflightFacts:
    """保存 preflight 已读取的封闭本机与 Evidence 事实。"""

    platform: str
    managed_python_312: bool
    repository_commit: str
    repository_clean: bool
    service_owned: bool
    service_status: ServiceStatus
    gateway_lease_fresh: bool
    owner_only_state: bool
    evidence: tuple[GateEvidence, ...]
    secret_matches: int


@dataclass(frozen=True, slots=True)
class GatewayLeaseFact:
    """保存 recovery 可比较且不公开 PID/started_at 的 lease identity。"""

    instance_hash: str
    commit: str
    active: bool

    def __post_init__(self) -> None:
        """验证实例 hash、commit 和 active 类型。"""
        if (
            _HASH.fullmatch(self.instance_hash) is None
            or _COMMIT.fullmatch(self.commit) is None
            or type(self.active) is not bool
        ):
            raise ValueError("invalid gateway lease fact")


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """保存受管 restart 与唯一 probe Delivery 的封闭结论。"""

    status: str
    error_code: str | None
    delivery_count: int

    def __post_init__(self) -> None:
        """验证 passed/failed 与安全错误码一致。"""
        if (
            self.status not in {"passed", "failed"}
            or type(self.delivery_count) is not int
            or self.delivery_count < 0
            or (self.status == "passed" and self.error_code is not None)
            or (
                self.status == "failed"
                and (
                    not isinstance(self.error_code, str)
                    or re.fullmatch(r"[a-z][a-z0-9_]{2,63}", self.error_code) is None
                )
            )
        ):
            raise ValueError("invalid recovery result")


@dataclass(frozen=True, slots=True)
class ProductionRuntime:
    """保存 confirm 后的本机对象；Secret 仅驻内存且不进入 repr。"""

    project_root: Path = dataclass_field(repr=False)
    evidence_dir: Path = dataclass_field(repr=False)
    paths: StatePaths = dataclass_field(repr=False)
    config: AppConfig = dataclass_field(repr=False)
    environment: Mapping[str, str] = dataclass_field(repr=False)
    secret_values: tuple[str, ...] = dataclass_field(repr=False)
    commit: str
    service: LaunchdService = dataclass_field(repr=False)
    evidence: tuple[GateEvidence, ...]


def evaluate_production_preflight(
    facts: ProductionPreflightFacts,
) -> tuple[SoakViolation, ...]:
    """把平台、服务、Evidence 和隐私事实转成稳定排序的 violations。

    Args:
        facts: 已由只读 collector 收窄的 production facts。

    Returns:
        空 tuple 表示可进入 recovery/soak；否则按 code 排序。
    """
    checks: dict[str, bool] = {
        "platform_unsupported": facts.platform != "darwin",
        "managed_python_invalid": facts.managed_python_312 is not True,
        "repository_commit_invalid": _COMMIT.fullmatch(facts.repository_commit) is None,
        "repository_dirty": facts.repository_clean is not True,
        "service_unowned": facts.service_owned is not True,
        "service_not_installed": not facts.service_status.installed,
        "service_not_loaded": not facts.service_status.loaded,
        "service_not_running": not facts.service_status.running,
        "gateway_lease_unhealthy": facts.gateway_lease_fresh is not True,
        "state_permissions_unsafe": facts.owner_only_state is not True,
        "secret_match": type(facts.secret_matches) is not int or facts.secret_matches != 0,
    }
    by_kind = {item.kind: item for item in facts.evidence}
    if len(by_kind) != len(facts.evidence):
        checks["evidence_duplicate"] = True
    expected_totals = {"seatbelt": 2, "feishu_channel": 15, "feishu_automation": 10}
    for kind, total in expected_totals.items():
        evidence = by_kind.get(kind)
        code = f"{kind}_evidence_invalid"
        checks[code] = (
            evidence is None
            or evidence.status != "verified"
            or evidence.total != total
            or evidence.passed != total
            or evidence.secret_matches != 0
        )
        if evidence is not None and evidence.commit != facts.repository_commit:
            checks["evidence_commit_mismatch"] = True
    failed = sorted(code for code, condition in checks.items() if condition)
    return tuple(SoakViolation(code) for code in failed)


def load_gate_evidence(
    evidence_dir: Path,
    *,
    expected_commit: str,
) -> tuple[GateEvidence, ...]:
    """安全读取 Seatbelt 2、Channel 15 与 Automation 10 的 private Evidence。

    Args:
        evidence_dir: 本次 run 的 owner-only 根目录。
        expected_commit: 三类报告必须共同绑定的 clean commit。

    Returns:
        固定顺序的三个 GateEvidence aggregate。

    Raises:
        ProductionGateError: 目录、文件、schema、commit 或 case totals 无效。
    """
    if _COMMIT.fullmatch(expected_commit) is None or not _private_directory(evidence_dir):
        raise ProductionGateError("evidence_directory_unsafe")
    results: list[GateEvidence] = []
    for kind in ("seatbelt", "feishu_channel", "feishu_automation"):
        directory_name, expected_files = _EVIDENCE_LAYOUT[kind]
        directory = evidence_dir / directory_name
        if not _private_directory(directory):
            raise ProductionGateError("evidence_directory_unsafe")
        try:
            files = tuple(sorted(directory.glob("*.json"), key=lambda path: path.name))
        except OSError:
            raise ProductionGateError("evidence_directory_unsafe") from None
        if len(files) != expected_files:
            raise ProductionGateError(f"{kind}_evidence_missing")
        reports = tuple(_read_private_report(path) for path in files)
        results.append(_aggregate_evidence(kind, reports, expected_commit))
    return tuple(results)


def run_managed_recovery(
    *,
    restart: Callable[[], None],
    read_gateway: Callable[[], GatewayLeaseFact],
    send_probe: Callable[[], str],
    count_probe_deliveries: Callable[[str], int],
    approval_is_stable: Callable[[], bool],
    wait: Callable[[float], None] = time.sleep,
    attempts: int = 30,
) -> RecoveryResult:
    """使用受管 service restart，验证新 lease 与 exactly-one probe Delivery。

    Args:
        restart: PROD-A LaunchdService.restart bound method。
        read_gateway: 读取匿名 lease identity 的函数。
        send_probe: 发送唯一飞书 recovery probe 并返回本地 token。
        count_probe_deliveries: 按本地 token 查询已发送 Delivery 数量。
        approval_is_stable: 验证 restart 前后 active Approval 没有丢失。
        wait: 测试可替换的 bounded 等待函数。
        attempts: ready/Delivery 的最多轮询次数。

    Returns:
        不含平台 ID、正文和 PID 的 RecoveryResult。
    """
    if type(attempts) is not int or not 1 <= attempts <= 120:
        raise ValueError("attempts must be between 1 and 120")
    try:
        before = read_gateway()
        if not before.active:
            return RecoveryResult("failed", "gateway_lease_unhealthy", 0)
        if not approval_is_stable():
            return RecoveryResult("failed", "approval_recovery_failed", 0)
        restart()
    except Exception:
        return RecoveryResult("failed", "service_restart_failed", 0)
    after: GatewayLeaseFact | None = None
    for _ in range(attempts):
        try:
            candidate = read_gateway()
        except Exception:
            candidate = None
        if (
            candidate is not None
            and candidate.active
            and candidate.commit == before.commit
            and candidate.instance_hash != before.instance_hash
        ):
            after = candidate
            break
        wait(1.0)
    if after is None:
        return RecoveryResult("failed", "gateway_not_restarted", 0)
    if not approval_is_stable():
        return RecoveryResult("failed", "approval_recovery_failed", 0)
    try:
        token = send_probe()
    except Exception:
        return RecoveryResult("failed", "recovery_probe_failed", 0)
    if not isinstance(token, str) or not token:
        return RecoveryResult("failed", "recovery_probe_failed", 0)
    count = 0
    for _ in range(attempts):
        try:
            count = count_probe_deliveries(token)
        except Exception:
            return RecoveryResult("failed", "recovery_delivery_unavailable", 0)
        if type(count) is not int or count < 0:
            return RecoveryResult("failed", "recovery_delivery_unavailable", 0)
        if count > 1:
            return RecoveryResult("failed", "recovery_delivery_duplicate", count)
        if count == 1:
            return RecoveryResult("passed", None, 1)
        wait(1.0)
    return RecoveryResult("failed", "recovery_delivery_missing", count)


def build_production_report(
    *,
    commit: str,
    started_at: str,
    finished_at: str,
    evidence: Sequence[GateEvidence],
    recovery: RecoveryResult,
    soak: SoakCheckpoint,
    deepseek_checks: Sequence[str],
    os_reboot: str,
) -> dict[str, object]:
    """从封闭 A/B/C Evidence 构造可跟踪的最终 aggregate report。

    Args:
        commit: 全部 Evidence 绑定的 clean commit。
        started_at: production gate UTC 起始时间。
        finished_at: production gate UTC 结束时间。
        evidence: Seatbelt、Channel、Automation aggregate。
        recovery: 受管 restart 结论。
        soak: exact-duration terminal checkpoint。
        deepseek_checks: normal/tool/approval 的封闭通过项。
        os_reboot: ``pass``、``not_run`` 或 ``fail``，not_run 不冒充 PASS。

    Returns:
        不含路径、PID、正文、平台 ID 或 Secret 的 aggregate report。

    Raises:
        ProductionGateError: 顶层输入不是封闭合法值。
    """
    if (
        _COMMIT.fullmatch(commit) is None
        or not _is_timestamp(started_at)
        or not _is_timestamp(finished_at)
        or os_reboot not in {"pass", "not_run", "fail"}
        or len({item.kind for item in evidence}) != len(evidence)
        or any(not isinstance(item, GateEvidence) for item in evidence)
        or not isinstance(recovery, RecoveryResult)
        or not isinstance(soak, SoakCheckpoint)
        or any(not isinstance(item, str) for item in deepseek_checks)
    ):
        raise ProductionGateError("production_report_invalid")
    ordered = sorted(evidence, key=lambda item: item.kind)
    expected = {"seatbelt": 2, "feishu_channel": 15, "feishu_automation": 10}
    evidence_passed = (
        {item.kind for item in ordered} == set(expected)
        and all(
            item.commit == commit
            and item.status == "verified"
            and item.total == expected[item.kind]
            and item.passed == expected[item.kind]
            and item.secret_matches == 0
            for item in ordered
        )
    )
    soak_passed = (
        soak.commit == commit
        and soak.status == "passed"
        and soak.elapsed_seconds >= 86_400
        and not soak.violation_codes
        and soak.restart_status == "passed"
    )
    provider_passed = set(deepseek_checks) == _DEEPSEEK_CHECKS
    verified = evidence_passed and soak_passed and provider_passed and recovery.status == "passed"
    return {
        "schema_version": 1,
        "commit": commit,
        "started_at": started_at,
        "finished_at": finished_at,
        "environment": ["macos", "feishu", "deepseek", "seatbelt"],
        "gates": [
            {
                "kind": item.kind,
                "status": item.status,
                "total": item.total,
                "passed": item.passed,
                "secret_matches": item.secret_matches,
                "sha256": item.sha256,
            }
            for item in ordered
        ],
        "deepseek_checks": sorted(set(deepseek_checks)),
        "recovery": {
            "status": recovery.status,
            "delivery_count": recovery.delivery_count,
            "error_code": recovery.error_code,
        },
        "soak": {
            "status": soak.status,
            "elapsed_seconds": soak.elapsed_seconds,
            "samples": soak.sample_count,
            "violations": len(soak.violation_codes),
        },
        "os_reboot": os_reboot,
        "secret_matches": sum(item.secret_matches for item in ordered),
        "release_status": (
            "PHASE6_MACOS_FEISHU_PRODUCTION_VERIFIED"
            if verified
            else "PHASE6_MACOS_FEISHU_PRODUCTION_FAILED"
        ),
    }


def run_phase6_production_gate(argv: Sequence[str] | None = None) -> int:
    """解析 production CLI，并在所有写操作前要求显式 ``--confirm-live``。"""
    parser = _build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    if arguments.command is None:
        print("error: production subcommand is required", file=sys.stderr)
        return 2
    if arguments.command != "status" and not arguments.confirm_live:
        print(
            "error: --confirm-live is required; no Secret, state, service, or file was read",
            file=sys.stderr,
        )
        return 2
    return _run_confirmed_command(arguments)


def _build_parser() -> argparse.ArgumentParser:
    """创建 preflight/start/resume/status/finalize 的无副作用 parser。"""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    for command in ("preflight", "start", "resume", "status", "finalize"):
        child = subparsers.add_parser(command)
        child.add_argument("--evidence-dir", required=True)
        child.add_argument("--home")
        if command != "status":
            child.add_argument("--confirm-live", action="store_true")
        if command in {"start", "resume"}:
            child.add_argument("--progress-output")
    return parser


def _run_confirmed_command(arguments: argparse.Namespace) -> int:
    """执行已通过 confirm gate 的 production preflight/recovery/soak。"""
    try:
        evidence_dir = Path(arguments.evidence_dir).expanduser().resolve(strict=False)
        if arguments.command == "status":
            checkpoint = load_soak_checkpoint(evidence_dir / "soak.json")
            print(render_progress(checkpoint))
            return 0 if checkpoint.status == "passed" else 1
        runtime = _load_production_runtime(
            evidence_dir=evidence_dir,
            home=arguments.home,
        )
        if arguments.command == "preflight":
            print("production preflight=PASS")
            return 0
        token = _run_token(runtime.commit, evidence_dir)
        checkpoint_path = evidence_dir / "soak.json"
        if arguments.command == "start":
            session, checkpoint = start_soak(
                checkpoint_path,
                commit=runtime.commit,
                run_token=token,
                state_home=runtime.paths.home,
                now=datetime.now(UTC),
                monotonic_now=time.monotonic(),
            )
            recovery = _execute_runtime_recovery(runtime)
            _write_recovery(evidence_dir / "recovery.json", recovery)
            checkpoint = record_restart_result(
                session,
                passed=recovery.status == "passed",
            )
            if checkpoint.status == "failed":
                print(render_progress(checkpoint))
                return 1
            return _monitor_session(
                runtime,
                session,
                progress_output=_progress_path(arguments.progress_output),
            )
        if arguments.command == "resume":
            session, _ = resume_soak(
                checkpoint_path,
                commit=runtime.commit,
                run_token=token,
                state_home=runtime.paths.home,
                now=datetime.now(UTC),
                monotonic_now=time.monotonic(),
            )
            return _monitor_session(
                runtime,
                session,
                progress_output=_progress_path(arguments.progress_output),
            )
        if arguments.command == "finalize":
            session, _ = resume_soak(
                checkpoint_path,
                commit=runtime.commit,
                run_token=token,
                state_home=runtime.paths.home,
                now=datetime.now(UTC),
                monotonic_now=time.monotonic(),
            )
            checkpoint = finish_soak(session)
            if checkpoint.status != "passed":
                print(render_progress(checkpoint))
                return 1
            recovery = _read_recovery(evidence_dir / "recovery.json")
            report = build_production_report(
                commit=runtime.commit,
                started_at=checkpoint.started_at,
                finished_at=checkpoint.last_observed_at,
                evidence=runtime.evidence,
                recovery=recovery,
                soak=checkpoint,
                deepseek_checks=("normal", "tool", "approval"),
                os_reboot="not_run",
            )
            if report["release_status"] != "PHASE6_MACOS_FEISHU_PRODUCTION_VERIFIED":
                print("error: production_gate_failed", file=sys.stderr)
                return 1
            _write_release(evidence_dir / "release.json", report)
            print("PHASE6_MACOS_FEISHU_PRODUCTION_VERIFIED")
            return 0
        raise ProductionGateError("production_command_invalid")
    except ProductionGateError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 1
    except ProductionEvidenceError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 1
    except Exception:
        print("error: production_gate_failed", file=sys.stderr)
        return 1


def _load_production_runtime(
    *,
    evidence_dir: Path,
    home: str | None,
) -> ProductionRuntime:
    """在 confirm 后加载单飞书配置、受管服务和同 commit Evidence。"""
    try:
        project_root = Path.cwd().resolve(strict=True)
        commit = clean_repository_commit(project_root)
        environment = dict(os.environ)
        paths = build_state_paths(resolve_home(home, environment))
        load_dotenv(resolve_dotenv_path(paths, environment, cwd=project_root), environment)
        config = load_config(paths, environment)
        if (
            collect_enabled_channels(config) != ("feishu",)
            or config.tools.mode != "safe"
            or not config.automation.enabled
            or config.sandbox.backend != "seatbelt"
            or config.sandbox.network != "none"
            or "deepseek" not in config.agent.model.lower()
        ):
            raise ProductionGateError("production_config_invalid")
        secrets = validate_gateway_environment(config, environment)
        secret_values = tuple(
            dict.fromkeys(
                (
                    secrets.model_api_key,
                    *secrets.channel_tokens.values(),
                    secrets.feishu_app_id,
                )
            )
        )
        evidence = load_gate_evidence(evidence_dir, expected_commit=commit)
        service, managed_python = _load_installed_service(
            paths,
            project_root=project_root,
            expected_commit=commit,
        )
        snapshot = _collect_snapshot(
            paths=paths,
            service=service,
            evidence_dir=evidence_dir,
            commit=commit,
            secret_values=secret_values,
        )
        facts = ProductionPreflightFacts(
            platform=sys.platform,
            managed_python_312=managed_python,
            repository_commit=commit,
            repository_clean=True,
            service_owned=True,
            service_status=service.status(),
            gateway_lease_fresh=snapshot.gateway_lease_fresh,
            owner_only_state=snapshot.owner_only_state,
            evidence=evidence,
            secret_matches=snapshot.secret_matches,
        )
        violations = evaluate_production_preflight(facts)
        if violations:
            raise ProductionGateError(violations[0].code)
        return ProductionRuntime(
            project_root,
            evidence_dir,
            paths,
            config,
            environment,
            secret_values,
            commit,
            service,
            evidence,
        )
    except ProductionGateError:
        raise
    except ProductionEvidenceError as error:
        raise ProductionGateError(error.code) from None
    except Exception:
        raise ProductionGateError("production_preflight_failed") from None


def _load_installed_service(
    paths: StatePaths,
    *,
    project_root: Path,
    expected_commit: str,
) -> tuple[LaunchdService, bool]:
    """从受管 plist/receipt 构造 status reader，并验证 managed Python 3.12。"""
    launch_agents = Path.home().resolve(strict=True) / "Library" / "LaunchAgents"
    plist_path = launch_agents / "io.miniclaw.gateway.plist"
    content = _read_private_bytes(plist_path, maximum=128 * 1024)
    try:
        payload = plistlib.loads(content)
        arguments = payload["ProgramArguments"]
        environment = payload["EnvironmentVariables"]
        working_directory = Path(payload["WorkingDirectory"])
        launcher = Path(arguments[0])
    except (KeyError, TypeError, ValueError, plistlib.InvalidFileException):
        raise ProductionGateError("service_unowned") from None
    if (
        payload.get("Label") != "io.miniclaw.gateway"
        or not isinstance(arguments, list)
        or len(arguments) != 4
        or arguments[1:3] != ["gateway", "--home"]
        or Path(arguments[3]) != paths.home
        or not isinstance(environment, dict)
        or environment.get("MINICLAW_GATEWAY_COMMIT") != expected_commit
        or working_directory != project_root
    ):
        raise ProductionGateError("service_unowned")
    spec = ServiceSpec(
        label="io.miniclaw.gateway",
        path=plist_path,
        receipt_path=paths.run / "launchd-service.json",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    service = LaunchdService(spec)
    return service, _is_managed_python_312(launcher, project_root)


def _is_managed_python_312(launcher: Path, project_root: Path) -> bool:
    """验证 console launcher 使用项目目录外的 exact CPython 3.12。"""
    try:
        metadata = launcher.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or not metadata.st_mode & stat.S_IXUSR
        ):
            return False
        header = _read_private_bytes(launcher, maximum=4096, require_private=False).splitlines()[0]
        if not header.startswith(b"#!"):
            return False
        parts = header[2:].decode("utf-8").strip().split()
        if len(parts) != 1:
            return False
        interpreter = Path(parts[0]).resolve(strict=True)
        if interpreter.is_relative_to(project_root):
            return False
        version_probe = (
            "import sys;"
            "print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        )
        result = subprocess.run(
            (str(interpreter), "-c", version_probe),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "3.12"


def _collect_snapshot(
    *,
    paths: StatePaths,
    service: LaunchdService,
    evidence_dir: Path,
    commit: str,
    secret_values: Sequence[str],
):
    """调用唯一 invariant collector，并只选择存在的私有状态根。"""
    candidates = (
        paths.home,
        paths.config,
        paths.database,
        paths.logs,
        paths.run,
        evidence_dir,
        evidence_dir / "seatbelt",
        evidence_dir / "feishu-channel",
        evidence_dir / "feishu-automation",
    )
    private_paths = tuple(path for path in candidates if path.exists() or path.is_symlink())
    return collect_soak_snapshot(
        service_status=service.status,
        database=Database(paths.database),
        lease_check=lambda: gateway_lease_is_fresh(paths.run / "gateway.lock", commit),
        private_paths=private_paths,
        evidence_paths=(evidence_dir, paths.logs),
        secrets=secret_values,
        now=datetime.now(UTC),
    )


def _execute_runtime_recovery(runtime: ProductionRuntime) -> RecoveryResult:
    """把受管 restart、active Approval 和 Owner DM probe 接到纯 recovery 编排器。"""
    approval_stable = _approval_stability_reader(runtime.paths.database)
    return run_managed_recovery(
        restart=runtime.service.restart,
        read_gateway=lambda: _gateway_lease_fact(
            runtime.paths.run / "gateway.lock",
            runtime.commit,
        ),
        send_probe=lambda: _send_recovery_probe(runtime),
        count_probe_deliveries=lambda token: _sent_delivery_count(
            runtime.paths.database,
            token,
        ),
        approval_is_stable=approval_stable,
        attempts=60,
    )


def _gateway_lease_fact(path: Path, expected_commit: str) -> GatewayLeaseFact:
    """把 lease 中的 PID/时间压成不可逆 instance hash。"""
    payload = _read_private_report(path)
    if (
        payload.get("schema_version") != 1
        or payload.get("commit") != expected_commit
        or type(payload.get("pid")) is not int
        or not _is_timestamp(payload.get("started_at"))
    ):
        raise ProductionGateError("gateway_lease_unhealthy")
    identity = json.dumps(
        {
            "commit": payload["commit"],
            "pid": payload["pid"],
            "started_at": payload["started_at"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return GatewayLeaseFact(
        hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        expected_commit,
        gateway_lease_is_fresh(path, expected_commit),
    )


def _approval_stability_reader(database_path: Path) -> Callable[[], bool]:
    """冻结当前 pending Approval ID/status，并返回 restart 前后比较函数。"""
    with Database(database_path).connect_read_only() as connection:
        baseline = tuple(
            (int(row["id"]), str(row["status"]))
            for row in connection.execute(
                "SELECT id, status FROM approvals WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        )

    def stable() -> bool:
        """验证 restart 没有删除或改写冻结的 pending Approval。"""
        if not baseline:
            return True
        placeholders = ",".join("?" for _ in baseline)
        with Database(database_path).connect_read_only() as connection:
            current = tuple(
                (int(row["id"]), str(row["status"]))
                for row in connection.execute(
                    f"SELECT id, status FROM approvals WHERE id IN ({placeholders}) ORDER BY id",
                    tuple(item[0] for item in baseline),
                ).fetchall()
            )
        return current == baseline

    return stable


def _send_recovery_probe(runtime: ProductionRuntime) -> str:
    """创建一条 system-owned Owner DM notice，并让现有 DeliveryWorker 发送。"""
    feishu = runtime.config.channels.feishu
    with Database(runtime.paths.database).connect_read_only() as connection:
        route = connection.execute(
            """
            SELECT session_id, external_conversation_id FROM processed_events
            WHERE channel = 'feishu' AND account_id = ? AND external_user_id = ?
              AND chat_type = 'p2p' AND status = 'completed' AND session_id IS NOT NULL
            ORDER BY rowid DESC LIMIT 1
            """,
            (feishu.account_id, feishu.owner_open_id),
        ).fetchone()
    if route is None:
        raise ProductionGateError("owner_dm_route_unavailable")
    marker = hashlib.sha256(f"{runtime.commit}:{time.time_ns()}".encode()).hexdigest()[:12]
    message = MessageRepository(Database(runtime.paths.database)).create_channel_notice(
        int(route["session_id"]),
        f"MiniClaw Phase 6 recovery probe · {marker}",
    )
    DeliveryRepository(Database(runtime.paths.database)).create_parts(
        message_id=message.id,
        channel="feishu",
        account_id=feishu.account_id,
        external_conversation_id=str(route["external_conversation_id"]),
        reply_to_message_id="",
        kind="message",
        contents=(message.content,),
    )
    return str(message.id)


def _sent_delivery_count(database_path: Path, message_token: str) -> int:
    """按本地 message ID 统计 sent Delivery，不读取正文或平台 ID。"""
    try:
        message_id = int(message_token)
    except ValueError:
        return -1
    with Database(database_path).connect_read_only() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM deliveries WHERE message_id = ? AND status = 'sent'",
            (message_id,),
        ).fetchone()
    return int(row[0])


def _monitor_session(
    runtime: ProductionRuntime,
    session: SoakSession,
    *,
    progress_output: Path | None,
) -> int:
    """每 60 秒采样直到 passed/failed；中断只保留 running checkpoint。"""
    while True:
        try:
            snapshot = _collect_snapshot(
                paths=runtime.paths,
                service=runtime.service,
                evidence_dir=runtime.evidence_dir,
                commit=runtime.commit,
                secret_values=runtime.secret_values,
            )
            checkpoint = record_snapshot(
                session,
                snapshot,
                monotonic_now=time.monotonic(),
            )
            checkpoint = finish_soak(session)
            if progress_output is not None:
                write_progress(progress_output, checkpoint)
            print(render_progress(checkpoint), flush=True)
            if checkpoint.status == "passed":
                return 0
            if checkpoint.status == "failed":
                return 1
            time.sleep(SAMPLE_CADENCE_SECONDS)
        except KeyboardInterrupt:
            checkpoint = load_soak_checkpoint(session.checkpoint_path)
            print(render_progress(checkpoint))
            return 130
        except Exception:
            checkpoint = fail_soak(session, "monitor_sample_failed")
            print(render_progress(checkpoint))
            return 1


def _run_token(commit: str, evidence_dir: Path) -> str:
    """由本次 private run directory 派生稳定 token，checkpoint 仍只保存二次 hash。"""
    return hashlib.sha256(f"{commit}\0{evidence_dir}".encode()).hexdigest()


def _progress_path(value: str | None) -> Path | None:
    """只在 confirm 后解析可选外部 progress path。"""
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve(strict=False)


def _write_recovery(path: Path, recovery: RecoveryResult) -> None:
    """独占写入不含 probe token/正文的 recovery aggregate。"""
    write_private_json(
        path,
        {
            "schema_version": 1,
            "status": recovery.status,
            "error_code": recovery.error_code,
            "delivery_count": recovery.delivery_count,
        },
    )


def _read_recovery(path: Path) -> RecoveryResult:
    """读取并验证固定 recovery aggregate。"""
    payload = _read_private_report(path)
    if set(payload) != {"schema_version", "status", "error_code", "delivery_count"}:
        raise ProductionGateError("recovery_evidence_invalid")
    try:
        recovery = RecoveryResult(
            payload["status"],
            payload["error_code"],
            payload["delivery_count"],
        )
    except (TypeError, ValueError):
        raise ProductionGateError("recovery_evidence_invalid") from None
    if payload["schema_version"] != 1:
        raise ProductionGateError("recovery_evidence_invalid")
    return recovery


def _write_release(path: Path, report: Mapping[str, object]) -> None:
    """幂等写入最终 verified aggregate；已存在时必须逐字段相同。"""
    if path.exists() or path.is_symlink():
        if dict(_read_private_report(path)) != dict(report):
            raise ProductionGateError("release_evidence_conflict")
        return
    write_private_json(path, report)


def _read_private_bytes(
    path: Path,
    *,
    maximum: int,
    require_private: bool = True,
) -> bytes:
    """no-follow 读取 bounded owner regular file。"""
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ProductionGateError("service_unowned") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or (require_private and metadata.st_mode & 0o077)
            or metadata.st_size > maximum
        ):
            raise ProductionGateError("service_unowned")
        content = os.read(descriptor, maximum + 1)
        if len(content) > maximum:
            raise ProductionGateError("service_unowned")
        return content
    except OSError:
        raise ProductionGateError("service_unowned") from None
    finally:
        os.close(descriptor)


def _read_private_report(path: Path) -> Mapping[str, object]:
    """使用 no-follow fd 读取至多 1 MiB 的 0600 JSON object。"""
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ProductionGateError("evidence_file_unsafe") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size > 1024 * 1024
        ):
            raise ProductionGateError("evidence_file_unsafe")
        payload = json.loads(os.read(descriptor, 1024 * 1024 + 1).decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ProductionGateError("evidence_file_invalid")
        return payload
    except ProductionGateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProductionGateError("evidence_file_invalid") from None
    finally:
        os.close(descriptor)


def _aggregate_evidence(
    kind: str,
    reports: Sequence[Mapping[str, object]],
    expected_commit: str,
) -> GateEvidence:
    """验证一类 report 并派生固定 totals/status/hash。"""
    commits = {report.get("commit") for report in reports}
    if commits != {expected_commit}:
        raise ProductionGateError("evidence_commit_mismatch")
    if kind == "seatbelt":
        if not all(validate_seatbelt_evidence_report(report) for report in reports):
            raise ProductionGateError("seatbelt_evidence_invalid")
        probes = {report.get("probe") for report in reports}
        if probes != {"python", "node-chain"}:
            raise ProductionGateError("seatbelt_evidence_invalid")
        total = 2
        passed = sum(
            report.get("release_status") == "SEATBELT_CONTAINMENT_VERIFIED"
            for report in reports
        )
        secret_matches = sum(int(report["secret_matches"]) for report in reports)
    elif kind == "feishu_channel":
        report = reports[0]
        if not validate_evidence_report(report):
            raise ProductionGateError("feishu_channel_evidence_invalid")
        counts = report["counts"]
        assert isinstance(counts, Mapping)
        total = int(counts["cases_total"])
        passed = int(counts["cases_passed"])
        secret_matches = int(counts["secret_matches"])
    else:
        report = reports[0]
        if not validate_automation_evidence_report(report):
            raise ProductionGateError("feishu_automation_evidence_invalid")
        counts = report["counts"]
        assert isinstance(counts, Mapping)
        total = int(counts["cases_total"])
        passed = int(counts["cases_passed"])
        secret_matches = int(counts["secret_matches"])
    expected_total = {"seatbelt": 2, "feishu_channel": 15, "feishu_automation": 10}[kind]
    verified = total == expected_total and passed == total and not secret_matches
    status = "verified" if verified else "failed"
    canonical = json.dumps(
        list(reports),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return GateEvidence(
        kind,
        expected_commit,
        status,
        total,
        passed,
        secret_matches,
        hashlib.sha256(canonical).hexdigest(),
    )


def _private_directory(path: Path) -> bool:
    """验证 owner-only、非 symlink 的真实 Evidence 目录。"""
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and not metadata.st_mode & 0o077
    )


def _is_timestamp(value: object) -> bool:
    """验证带微秒的 UTC Evidence timestamp。"""
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return True


def _evidence_commit(evidence_dir: Path) -> str:
    """从唯一 Channel Evidence 读取候选 commit，随后仍由完整 loader 复核。"""
    directory = evidence_dir / "feishu-channel"
    if not _private_directory(directory):
        raise ProductionGateError("evidence_directory_unsafe")
    files = tuple(directory.glob("*.json"))
    if len(files) != 1:
        raise ProductionGateError("feishu_channel_evidence_missing")
    commit = _read_private_report(files[0]).get("commit")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise ProductionGateError("evidence_commit_mismatch")
    return commit
