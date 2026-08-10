#!/usr/bin/env python3
"""Tier 1 install matrix 驱动器：本地离线证据、runner 标签导出与主机上实跑。

本地 ``--local-offline`` 只用假 artifact 与注入的主机事实证明可以离线证明的性质，
绝不假装完成 service、reboot、升级或回滚；这些用例如实标注 PENDING。
``--all-cases`` 只在已注册的 Tier 1 自托管主机上运行，产出 Release evidence。

诚实边界：本仓库当前没有任何自托管 runner，因此 ``--all-cases`` 从未被真实执行过。
只有 ``cases.json`` 中带 ``live_command`` 的用例落地了 live 过程；其余用例带的是
``live_pending_reason``，在主机上会被记为 PENDING 并让作业以非零码结束。换句话说，
即使将来注册了 runner，剩余 live 过程补齐之前 stable 提升仍然被阻断——这是刻意的，
不允许用"看起来绿"的假实现换取发布。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform as platform_module
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
_CASES = _HERE / "cases.json"
_MANIFEST_FIXTURE = _REPO_ROOT / "tests" / "install" / "manifest_v1.json"
_FEATURES = _REPO_ROOT / "release" / "features.json"
_RUNTIME_VERSIONS = _REPO_ROOT / "release" / "runtime-versions.json"

_UNSUPPORTED_HOSTS = (
    ("alpine", 'NAME="Alpine"\nID=alpine\nVERSION_ID="3.20"\n', "musl"),
    ("nixos", 'NAME="NixOS"\nID=nixos\nVERSION_ID="24.05"\n', "glibc"),
    ("ubuntu-20.04", 'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="20.04"\n', "glibc"),
    ("debian-11", 'NAME="Debian"\nID=debian\nVERSION_ID="11"\n', "glibc"),
    ("rhel-8", 'NAME="RHEL"\nID=rhel\nVERSION_ID="8"\n', "glibc"),
)

_REJECTED_NODE = ((20, 19, 0), (22, 22, 2), (23, 0, 0), (25, 0, 0), (26, 1, 0))
_ACCEPTED_NODE = ((22, 22, 3), (24, 15, 0), (24, 18, 0))

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:secret|token|api[_-]?key|apikey|password|passwd|credential|private[_-]?key)"
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+_.-]{16,})"
)
_SECRET_PLACEHOLDERS = re.compile(
    r"(?i)^(?:x{4,}|a{4,}|0+|1+|f+|<[^>]+>|\$\{[^}]+\}|changeme|redacted|example|"
    r"your[-_].*|placeholder|null|none|true|false)$"
)
_SECRET_SCAN_ROOTS = ("release", "deploy", ".github", "scripts", "tests/install_matrix")

_INSTALLER_ERROR_EVENT = re.compile(
    r'^\{"code":"[a-z_]+","detail":"[a-z_.]+","event":"install\.failed",'
    r'"status":"error"\}\n$'
)


class MatrixError(RuntimeError):
    """表示 matrix 定义、离线证据或主机执行不满足契约。"""


@dataclass(frozen=True, slots=True)
class Outcome:
    """保存一个用例在当前执行模式下的结论。

    Args:
        case_id: cases.json 中的稳定用例标识。
        status: ``PASS``、``PENDING`` 或 ``BLOCKED``。
        detail: 一行无凭据、无主机身份的结论说明。
    """

    case_id: str
    status: str
    detail: str


def load_document(path: Path = _CASES) -> dict[str, object]:
    """加载并严格校验 install matrix 定义。

    Args:
        path: ``cases.json`` 路径。

    Returns:
        含 ``platforms`` 与 ``cases`` 的已校验文档。

    Raises:
        MatrixError: 结构、编号或平台字段不可信。
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MatrixError("could not read the install matrix definition") from error
    if type(document) is not dict or document.get("schema_version") != 1:
        raise MatrixError("invalid install matrix definition")
    platforms = document.get("platforms")
    cases = document.get("cases")
    if type(platforms) is not list or not platforms:
        raise MatrixError("invalid platform set")
    if type(cases) is not list or len(cases) != 15:
        raise MatrixError("the matrix must declare exactly the fifteen design cases")
    numbers = sorted(int(case["design_case"]) for case in cases)
    if numbers != list(range(1, 16)):
        raise MatrixError("design case numbers must be 1..15 without gaps")
    return document


def _account(uid: int) -> pwd.struct_passwd:
    """返回不触碰真实账号数据库的 passwd 记录。"""
    return pwd.struct_passwd(
        ("builder", "x", uid, uid, "Matrix", f"/home/builder-{uid}", "/bin/sh")
    )


def _request(**changes: object):
    """构造一个默认无特权、无系统包授权的安装请求。"""
    from lobster0.install.models import InstallRequest

    values: dict[str, object] = {
        "action": "install",
        "version": "0.7.0",
        "channel": "stable",
        "prefix": Path("/opt/lobster0"),
        "state_home": Path("/var/lib/lobster0"),
        "system_prefix": False,
        "onboard": True,
        "config_file": None,
        "secrets_file": None,
        "service": False,
        "allow_system_packages": False,
        "dry_run": True,
        "json_output": False,
        "verbose": False,
        "purge_data": False,
        "confirm_data_loss": False,
    }
    values.update(changes)
    return InstallRequest(**values)  # type: ignore[arg-type]


def _detect(entry: dict[str, object], **changes: object):
    """按平台定义注入主机事实并执行 Tier 1 检测。"""
    from lobster0.install.platforms import detect_platform

    if entry["os"] == "macos":
        return detect_platform(
            _request(**changes),
            system="Darwin",
            machine=str(entry["uname_machine"]),
            macos_version=str(entry["macos_version"]),
            effective_uid=501,
            getpwuid=_account,
        )
    return detect_platform(
        _request(**changes),
        system="Linux",
        machine=str(entry["uname_machine"]),
        os_release_text=(
            f'NAME="Fixture"\nID={entry["distro"]}\n'
            f'VERSION_ID="{entry["distro_version"]}"\n'
        ),
        libc="glibc",
        wsl=False,
        service_manager=str(entry["service_manager"]),
        effective_uid=1000,
        getpwuid=_account,
    )


def probe_tier1_platform_matrix(document: dict[str, object], workdir: Path) -> str:
    """证明全部 Tier 1 组合被接受且非 Tier 1 主机被拒绝。"""
    from lobster0.install.models import InstallError, PlatformKey
    from lobster0.install.platforms import detect_platform

    del workdir
    for entry in document["platforms"]:
        detected = _detect(entry, service=True)
        expected = str(entry["artifact_platform"]).split("-", 1)
        if detected.artifact_platform != PlatformKey(expected[0], expected[1]):
            raise MatrixError(f"platform mapping drifted for {entry['id']}")
    rejected = 0
    for name, os_release, libc in _UNSUPPORTED_HOSTS:
        try:
            detect_platform(
                _request(),
                system="Linux",
                machine="x86_64",
                os_release_text=os_release,
                libc=libc,
                wsl=False,
                service_manager="systemd-user",
                effective_uid=1000,
                getpwuid=_account,
            )
        except InstallError as error:
            if error.code != "unsupported_platform":
                raise MatrixError(f"{name} produced the wrong refusal") from error
            rejected += 1
        else:
            raise MatrixError(f"{name} was accepted as a Tier 1 host")
    for machine in ("i686", "armv7l"):
        try:
            detect_platform(
                _request(),
                system="Linux",
                machine=machine,
                os_release_text='NAME="F"\nID=ubuntu\nVERSION_ID="24.04"\n',
                libc="glibc",
                wsl=False,
                service_manager="systemd-user",
                effective_uid=1000,
                getpwuid=_account,
            )
        except InstallError:
            rejected += 1
        else:
            raise MatrixError(f"{machine} was accepted as a Tier 1 architecture")
    count = len(document["platforms"])
    return f"{count} Tier 1 hosts resolved, {rejected} out-of-tier hosts refused"


def probe_managed_runtime_closure(document: dict[str, object], workdir: Path) -> str:
    """证明受管 Node 策略封闭且每个平台都有自带 Node/TUI 组件。"""
    from lobster0.install.platforms import node_version_supported

    del workdir
    for version in _REJECTED_NODE:
        if node_version_supported(version):
            raise MatrixError(f"node {version} must not be accepted")
    for version in _ACCEPTED_NODE:
        if not node_version_supported(version):
            raise MatrixError(f"node {version} must be accepted")
    pins = json.loads(_RUNTIME_VERSIONS.read_text(encoding="utf-8"))
    for component in ("uv", "node"):
        archives = pins[component]["archives"]
        for entry in document["platforms"]:
            key = str(entry["artifact_platform"])
            if key not in archives or len(archives[key]["sha256"]) != 64:
                raise MatrixError(f"missing pinned {component} archive for {key}")
    return (
        f"managed uv {pins['uv']['version']} / node {pins['node']['version']} pinned for "
        f"4 platforms; {len(_REJECTED_NODE)} out-of-policy Node versions refused"
    )


def probe_privilege_fail_closed(document: dict[str, object], workdir: Path) -> str:
    """证明默认安装既不请求提权也不会在提权身份不完整时继续。"""
    from lobster0.install.models import InstallError
    from lobster0.install.platforms import detect_platform

    del workdir
    default = _request()
    if default.system_prefix or default.allow_system_packages:
        raise MatrixError("the default request must not request elevated behaviour")
    try:
        _request(system_prefix=True, prefix=Path("/opt/lobster0"))
    except InstallError as error:
        if error.code != "request_invalid":
            raise MatrixError("prefix conflict produced the wrong refusal") from error
    else:
        raise MatrixError("system prefix must conflict with an explicit prefix")
    refusals = 0
    for entry in document["platforms"]:
        try:
            _detect(entry, service=True)
        except InstallError as error:  # pragma: no cover - defensive
            raise MatrixError(f"{entry['id']} lost its unprivileged path") from error
        try:
            if entry["os"] == "macos":
                detect_platform(
                    _request(),
                    system="Darwin",
                    machine=str(entry["uname_machine"]),
                    macos_version=str(entry["macos_version"]),
                    effective_uid=0,
                    getpwuid=_account,
                )
            else:
                detect_platform(
                    _request(),
                    system="Linux",
                    machine=str(entry["uname_machine"]),
                    os_release_text=(
                        f'NAME="Fixture"\nID={entry["distro"]}\n'
                        f'VERSION_ID="{entry["distro_version"]}"\n'
                    ),
                    libc="glibc",
                    wsl=False,
                    service_manager=str(entry["service_manager"]),
                    effective_uid=0,
                    original_user="builder",
                    original_uid=1000,
                    getpwuid=_account,
                    getpwnam=lambda _name: _account(4242),
                )
        except InstallError as error:
            if error.code != "privilege_denied":
                raise MatrixError(f"{entry['id']} produced the wrong refusal") from error
            refusals += 1
        else:
            raise MatrixError(f"{entry['id']} accepted an unverified elevated identity")
    return (
        f"default request stays unprivileged; {refusals} hosts refused an unverified "
        "elevated identity"
    )


def probe_dry_run_zero_writes(document: dict[str, object], workdir: Path) -> str:
    """证明未验证 bootstrap 的 dry-run 立即失败、只发一条脱敏事件且零写入。"""
    del document
    root = workdir / "dry-run"
    prefix = root / "prefix"
    state = root / "state"
    for directory in (prefix, state):
        directory.mkdir(parents=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lobster0.install",
            "install",
            "--dry-run",
            "--json",
            "--prefix",
            str(prefix),
            "--home",
            str(state),
            "--manifest-file",
            str(root / "absent-release-manifest.json"),
            "--manifest-sha256",
            "1" * 64,
            "--managed-uv",
            str(root / "uv" / "uv"),
            "--managed-python-root",
            str(root / "python"),
            "--managed-python-executable",
            str(root / "python" / "bin" / "python3.12"),
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
        timeout=120,
        check=False,
    )
    if result.returncode != 2:
        raise MatrixError("an unverified bootstrap must fail closed with code 2")
    if _INSTALLER_ERROR_EVENT.fullmatch(result.stdout) is None:
        raise MatrixError("the installer emitted a non-canonical failure document")
    if result.stderr:
        raise MatrixError("JSON mode must keep stderr empty")
    written = [path for path in root.rglob("*") if path.is_file()]
    if written:
        raise MatrixError("a refused dry-run must not persist any file")
    return "unverified bootstrap refused with code 2, one redacted event and zero writes"


def probe_json_boundary_fail_closed(document: dict[str, object], workdir: Path) -> str:
    """证明非 TTY 的 ``--no-onboard`` 自动化路径不会带出导入路径或部分事件。"""
    del document
    from lobster0.install.__main__ import main as installer_main

    internal = (
        "--manifest-file",
        str(workdir / "release-manifest.json"),
        "--manifest-sha256",
        "1" * 64,
        "--managed-uv",
        str(workdir / "uv" / "uv"),
        "--managed-python-root",
        str(workdir / "python"),
        "--managed-python-executable",
        str(workdir / "python" / "bin" / "python3.12"),
    )
    checks = (
        (("--no-onboard", *internal), "silent"),
        (("--json", "--no-onboard", *internal), "event"),
        (
            (
                "--json",
                "--onboard",
                "--config",
                "/tmp/MATRIX_SENTINEL-config.toml",
                "--secrets-file",
                "/tmp/MATRIX_SENTINEL-secrets.env",
                *internal,
            ),
            "event",
        ),
    )
    for argv, shape in checks:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = installer_main(argv, stdin_isatty=False)
        if code != 2:
            raise MatrixError("the automated install boundary did not fail closed")
        emitted = stdout.getvalue()
        if shape == "silent" and emitted:
            raise MatrixError("a refused human-mode request must not write to stdout")
        if shape == "event" and _INSTALLER_ERROR_EVENT.fullmatch(emitted) is None:
            raise MatrixError("a refused JSON request must emit exactly one redacted event")
        if "MATRIX_SENTINEL" in emitted + stderr.getvalue():
            raise MatrixError("import source paths must never be echoed")
    return (
        "non-TTY automated install fails closed with exactly one redacted event and no "
        "echoed import paths"
    )


def probe_feature_registry_closure(document: dict[str, object], workdir: Path) -> str:
    """证明 manifest 声明的每个能力都真实可导入，Channel SDK 契约成立。"""
    del document, workdir
    import importlib

    registry = json.loads(_FEATURES.read_text(encoding="utf-8"))
    resolved = 0
    for entry in registry["features"]:
        module = importlib.import_module(str(entry["module"]))
        if not hasattr(module, str(entry["attribute"])):
            raise MatrixError(f"feature {entry['feature']} is declared but absent")
        resolved += 1
    for name in ("feishu", "telegram", "discord"):
        importlib.import_module(f"lobster0.channels.{name}")
    return f"{resolved} declared features resolve, 3 Channel adapters import"


def probe_version_identity(document: dict[str, object], workdir: Path) -> str:
    """证明 CLI 版本、版本常量与 manifest fixture 是同一个事实。"""
    del document, workdir
    from lobster0 import __version__

    result = subprocess.run(
        [sys.executable, "-m", "lobster0", "--version"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise MatrixError("the CLI could not report its version")
    reported = (result.stdout + result.stderr).strip()
    if __version__ not in reported:
        raise MatrixError("the CLI version drifted from the version constant")
    fixture = json.loads(_MANIFEST_FIXTURE.read_text(encoding="utf-8"))
    if fixture["version"] != __version__:
        raise MatrixError("the manifest fixture drifted from the version constant")
    return f"CLI, version constant and manifest agree on {__version__}"


class _FakeResponse:
    """提供 download_artifact 需要的最小 response 契约。"""

    def __init__(self, payload: bytes, status: int = 200, location: str | None = None) -> None:
        """记录响应体与固定 headers。"""
        self.status = status
        self._stream = io.BytesIO(payload)
        self.headers = Message()
        self.headers["Content-Length"] = str(len(payload))
        if location is not None:
            self.headers["Location"] = location

    def read(self, size: int = -1) -> bytes:
        """读取最多 size 字节。"""
        return self._stream.read(size)

    def close(self) -> None:
        """关闭响应资源。"""
        self._stream.close()


class _FakeOpener:
    """按 URL 返回预置的假 artifact 字节或 redirect。"""

    def __init__(self, routes: dict[str, _Callable]) -> None:
        """记录 URL 到响应工厂的固定映射。"""
        self._routes = routes

    def open(self, request: object, timeout: float | None = None) -> _FakeResponse:
        """返回该 URL 的预置响应。"""
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        if url not in self._routes:
            raise OSError("offline matrix reached an unrouted URL")
        return self._routes[url]()


_Callable = Callable[[], _FakeResponse]


def _fake_bundle(path: Path, kind: str) -> Path:
    """生成一个有效或恶意的 tar.gz fixture。"""
    with tarfile.open(path, "w:gz") as archive:
        if kind == "valid":
            member = tarfile.TarInfo("tui/dist/main.js")
            payload = b"console.log('lobster0');\n"
            member.size = len(payload)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(payload))
        elif kind == "absolute":
            member = tarfile.TarInfo("/escape")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        elif kind == "dotdot":
            member = tarfile.TarInfo("safe/../escape")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        else:
            member = tarfile.TarInfo("link")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
    return path


def probe_artifact_integrity_rejection(document: dict[str, object], workdir: Path) -> str:
    """证明坏 hash、坏 size、跨 host redirect 与恶意 archive 都被拒绝。"""
    del document
    from lobster0.install.artifacts import ExtractionLimits, download_artifact, extract_tar_gz
    from lobster0.install.models import Artifact, InstallError

    root = workdir / "artifacts"
    root.mkdir(parents=True)
    payload = b"lobster0 offline matrix artifact\n" * 64
    digest = hashlib.sha256(payload).hexdigest()
    url = (
        "https://github.com/NEDONION/lobster0/releases/download/v0.7.0/"
        "lobster0-tui-0.7.0-linux-x86_64.tar.gz"
    )
    artifact = Artifact(
        kind="tui",
        filename="lobster0-tui-0.7.0-linux-x86_64.tar.gz",
        url=url,
        sha256=digest,
        size=len(payload),
        media_type="application/gzip",
        platform=__import__(
            "lobster0.install.models", fromlist=["PlatformKey"]
        ).PlatformKey("linux", "x86_64"),
        component_version="0.7.0",
        source_repository="https://github.com/NEDONION/lobster0",
        license_ref="MIT",
        upstream_sha256=None,
    )
    download_artifact(
        artifact,
        root / "good.bin",
        opener=_FakeOpener({url: lambda: _FakeResponse(payload)}),
    )
    if (root / "good.bin").read_bytes() != payload:
        raise MatrixError("a verified download did not land intact")
    refusals = 0
    corrupted = bytearray(payload)
    corrupted[0] ^= 0xFF
    injections = (
        ("bad-hash", lambda: _FakeResponse(bytes(corrupted))),
        ("short-read", lambda: _FakeResponse(payload[:-8])),
        (
            "foreign-redirect",
            lambda: _FakeResponse(b"", status=302, location="https://evil.example/x.tar.gz"),
        ),
        ("server-error", lambda: _FakeResponse(b"", status=503)),
    )
    for name, factory in injections:
        try:
            download_artifact(
                artifact,
                root / f"{name}.bin",
                opener=_FakeOpener({url: factory}),
            )
        except InstallError as error:
            if error.code not in {"artifact_hash_mismatch", "artifact_download_failed"}:
                raise MatrixError(f"{name} produced the wrong refusal") from error
            refusals += 1
        else:
            raise MatrixError(f"{name} was accepted as a trusted artifact")
    limits = ExtractionLimits(max_entries=64, max_bytes=1 << 20)
    extract_tar_gz(_fake_bundle(root / "valid.tar.gz", "valid"), root / "out", limits)
    for kind in ("absolute", "dotdot", "symlink"):
        try:
            extract_tar_gz(
                _fake_bundle(root / f"{kind}.tar.gz", kind),
                root / f"out-{kind}",
                limits,
            )
        except InstallError:
            refusals += 1
        else:
            raise MatrixError(f"{kind} archive was extracted")
    return f"1 verified artifact accepted, {refusals} corrupt or hostile inputs refused"


def probe_secret_scan(document: dict[str, object], workdir: Path) -> str:
    """对受管发布输入与 workflow 执行凭据扫描。"""
    del document, workdir
    scanned = 0
    for relative in _SECRET_SCAN_ROOTS:
        root = _REPO_ROOT / relative
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix in {".png", ".gz", ".whl", ".pyz", ".zip"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for match in _SECRET_ASSIGNMENT.finditer(text):
                value = match.group(1)
                if _SECRET_PLACEHOLDERS.fullmatch(value):
                    continue
                if len(set(value)) <= 2 or value.startswith("sha256"):
                    continue
                if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
                    continue
                raise MatrixError(f"possible credential literal in {path.name}")
            scanned += 0
    return f"{scanned} release-input files scanned, no credential literal found"


_PROBES: dict[str, Callable[[dict[str, object], Path], str]] = {
    "tier1_platform_matrix": probe_tier1_platform_matrix,
    "managed_runtime_closure": probe_managed_runtime_closure,
    "privilege_fail_closed": probe_privilege_fail_closed,
    "dry_run_zero_writes": probe_dry_run_zero_writes,
    "json_boundary_fail_closed": probe_json_boundary_fail_closed,
    "feature_registry_closure": probe_feature_registry_closure,
    "version_identity": probe_version_identity,
    "artifact_integrity_rejection": probe_artifact_integrity_rejection,
    "secret_scan": probe_secret_scan,
}


def run_local_offline(document: dict[str, object]) -> tuple[Outcome, ...]:
    """按 cases.json 执行全部可离线证明的用例并如实标注其余用例。

    Args:
        document: 已校验的 install matrix 定义。

    Returns:
        与 cases.json 顺序一致的结论序列。
    """
    outcomes: list[Outcome] = []
    workdir = Path(tempfile.mkdtemp(prefix="lobster0-install-matrix-"))
    try:
        for case in document["cases"]:
            case_id = str(case["id"])
            if str(case["local_offline"]).lower() != "true":
                outcomes.append(Outcome(case_id, "PENDING", str(case["pending_reason"])))
                continue
            probe = _PROBES.get(str(case["probe"]))
            if probe is None:
                outcomes.append(
                    Outcome(case_id, "BLOCKED", f"unknown probe {case['probe']}")
                )
                continue
            scope = workdir / case_id
            scope.mkdir(parents=True, exist_ok=True)
            try:
                detail = probe(document, scope)
            except Exception as error:  # noqa: BLE001 - reported, never swallowed
                outcomes.append(Outcome(case_id, "BLOCKED", f"{type(error).__name__}: {error}"))
            else:
                outcomes.append(Outcome(case_id, "PASS", detail))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return tuple(outcomes)


def _print_local_offline(document: dict[str, object], outcomes: tuple[Outcome, ...]) -> int:
    """打印离线证据表并返回进程退出码。"""
    platforms = len(document["platforms"])
    print(
        "lobster0 Tier 1 install matrix — local offline evidence "
        "(fake artifacts, no network, no elevation, no service)"
    )
    print(f"tier1 host combinations resolved offline: {platforms}")
    width = max(len(str(case["id"])) for case in document["cases"])
    for outcome in outcomes:
        print(f"{outcome.case_id.ljust(width)}  {outcome.status:<7}  {outcome.detail}")
    passed = sum(1 for item in outcomes if item.status == "PASS")
    pending = sum(1 for item in outcomes if item.status == "PENDING")
    blocked = sum(1 for item in outcomes if item.status == "BLOCKED")
    print(f"summary: pass={passed} pending={pending} blocked={blocked}")
    print(
        "PENDING cases can only reach LIVE PASS on a registered Tier 1 self-hosted host; "
        "this local run is never sufficient for stable promotion."
    )
    return 1 if blocked else 0


def _print_list(document: dict[str, object]) -> int:
    """打印全部平台标签与用例。"""
    print("platforms:")
    for entry in document["platforms"]:
        labels = ",".join(str(item) for item in entry["runner_labels"])
        print(f"  {entry['id']}  {entry['artifact_platform']}  runs-on=[{labels}]")
    print("cases:")
    for case in document["cases"]:
        offline = "offline" if str(case["local_offline"]).lower() == "true" else "-------"
        live = "live" if "live_command" in case else "----"
        print(f"  {case['design_case']:>2}  {case['id']}  [{offline}|{live}]  {case['title']}")
    live_ready = sum(1 for case in document["cases"] if "live_command" in case)
    print(
        f"total: {len(document['platforms'])} platforms x {len(document['cases'])} cases = "
        f"{len(document['platforms']) * len(document['cases'])} Tier 1 combinations"
    )
    print(
        f"live procedures implemented: {live_ready}/{len(document['cases'])}; the remaining "
        "cases report PENDING on a Tier 1 host and therefore block stable promotion"
    )
    return 0


def _print_required_runner_labels(document: dict[str, object]) -> int:
    """输出前置门禁使用的标签矩阵。"""
    payload = [
        {
            "id": str(entry["id"]),
            "artifact_platform": str(entry["artifact_platform"]),
            "runner_labels": [str(item) for item in entry["runner_labels"]],
        }
        for entry in document["platforms"]
    ]
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run_all_cases(document: dict[str, object], arguments: argparse.Namespace) -> int:
    """在已注册的 Tier 1 自托管主机上执行全部用例并写出 evidence。

    只有 ``live_command`` 已落地的用例才可能得到 LIVE PASS；其余用例如实记为
    PENDING 并让整个作业以非零码结束，从而阻断 stable 提升——绝不假装通过。
    """
    entry = next(
        (item for item in document["platforms"] if item["id"] == arguments.platform), None
    )
    if entry is None:
        raise MatrixError(f"unknown Tier 1 platform {arguments.platform}")
    host_os = "macos" if platform_module.system() == "Darwin" else "linux"
    if host_os != entry["os"]:
        raise MatrixError("the declared platform does not match this host")
    if not arguments.launcher.is_file():
        raise MatrixError("the managed launcher is missing on this host")
    outcomes: list[Outcome] = []
    for case in document["cases"]:
        command = case.get("live_command")
        if command is None:
            outcomes.append(
                Outcome(str(case["id"]), "PENDING", str(case["live_pending_reason"]))
            )
            continue
        result = subprocess.run(
            [str(arguments.launcher), *[str(item) for item in command]],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=arguments.case_timeout,
            check=False,
        )
        status = "PASS" if result.returncode == 0 else "BLOCKED"
        outcomes.append(Outcome(str(case["id"]), status, f"exit={result.returncode}"))
    blocked = [item for item in outcomes if item.status != "PASS"]
    evidence = {
        "schema_version": 1,
        "platform": str(entry["id"]),
        "artifact_platform": str(entry["artifact_platform"]),
        "release_tag": arguments.release_tag,
        "cases": [
            {"id": item.case_id, "status": item.status, "detail": item.detail}
            for item in outcomes
        ],
        "status": "PASS" if not blocked else "FAIL",
    }
    arguments.evidence_output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for item in outcomes:
        print(f"{item.case_id}  {item.status}  {item.detail}")
    return 1 if blocked else 0


def main(argv: list[str] | None = None) -> int:
    """解析 CLI 并执行选定的 install matrix 模式。"""
    parser = argparse.ArgumentParser(description="Run the Lobster0 Tier 1 install matrix")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="print platforms and cases")
    mode.add_argument(
        "--local-offline", action="store_true", help="run the offline fake-artifact evidence"
    )
    mode.add_argument(
        "--required-runner-labels",
        action="store_true",
        help="print the self-hosted label matrix the release preflight requires",
    )
    mode.add_argument(
        "--all-cases", action="store_true", help="run every case on a Tier 1 self-hosted host"
    )
    parser.add_argument("--platform", default=None)
    parser.add_argument("--release-tag", default=None)
    parser.add_argument(
        "--launcher", type=Path, default=Path.home() / ".local" / "bin" / "lobster0"
    )
    parser.add_argument("--evidence-output", type=Path, default=Path("tier1-evidence.json"))
    parser.add_argument("--case-timeout", type=int, default=1800)
    arguments = parser.parse_args(argv)
    try:
        document = load_document()
        if arguments.list:
            return _print_list(document)
        if arguments.required_runner_labels:
            return _print_required_runner_labels(document)
        if arguments.local_offline:
            return _print_local_offline(document, run_local_offline(document))
        if arguments.platform is None or arguments.release_tag is None:
            raise MatrixError("--all-cases requires --platform and --release-tag")
        return _run_all_cases(document, arguments)
    except MatrixError as error:
        print(f"install matrix error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
