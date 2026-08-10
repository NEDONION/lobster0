"""协调受信任 Release 的安装事务并输出脱敏事件。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pwd
import re
import shutil
import stat
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Never, Protocol, TextIO

from miniclaw.install.artifacts import (
    ExtractionLimits,
    _Opener,
    _read_bounded_url,
    download_artifact,
    extract_tar_gz,
)
from miniclaw.install.layout import (
    InstallLayout,
    InstallLock,
    install_launcher,
)
from miniclaw.install.models import (
    InstallError,
    InstallEvent,
    InstallPlan,
    InstallRequest,
    PlatformKey,
    ReleaseManifest,
)
from miniclaw.install.platforms import (
    DependencyPlan,
    DetectedPlatform,
    PrivilegeAction,
    _production_identity,
    _resolve_invoking_user,
    build_dependency_actions,
    detect_platform,
    verify_privilege_action,
)
from miniclaw.install.receipt import InstallReceipt, managed_file_sha256
from miniclaw.install.releases import resolve_release_source
from miniclaw.install.runtime import (
    CommandResult,
    RuntimeBuilder,
    RuntimeInputs,
    RuntimeReceipt,
    _SubprocessRunner,
    activate_runtime,
    retain_current_and_previous,
)
from miniclaw.install.service import (
    ServicePlatform,
    render_service_spec,
    service_install,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9./_-]{0,255}@sha256:[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_SECRET_BYTES = 64 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_PATH = "/usr/local/bin:/usr/bin:/bin"
_TIMEOUT = 300.0
_INTERNAL_HOP = "MINICLAW_INSTALLER_HOPS"
_CHANNEL_FIELDS = {
    "feishu": ("app_id_env", "app_secret_env"),
    "telegram": ("bot_token_env",),
    "discord": ("bot_token_env",),
}


@dataclass(frozen=True, slots=True)
class BootstrapInputs:
    """保存 bootstrap 已下载并校验的 manifest、uv 与 Python 路径。

    Args:
        manifest_file: private bootstrap tree 根下的 manifest 普通文件。
        manifest_sha256: bootstrap 内嵌并已比对的 manifest SHA-256。
        managed_uv: 同一 tree 下的 managed uv 普通可执行文件。
        managed_python_root: 同一 tree 下的 managed Python 根目录。
        managed_python_executable: 固定为 root/bin/python3.12 的普通可执行文件。
        public_argv: installer 跳转时原样透传的非内部参数。

    Raises:
        InstallError: 字段不是 canonical absolute path、hash 或字符串元组。
    """

    manifest_file: Path
    manifest_sha256: str
    managed_uv: Path
    managed_python_root: Path
    managed_python_executable: Path
    public_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        """执行无文件访问的结构校验。"""
        paths = (
            self.manifest_file,
            self.managed_uv,
            self.managed_python_root,
            self.managed_python_executable,
        )
        if (
            any(not _canonical_absolute(path) for path in paths)
            or type(self.manifest_sha256) is not str
            or _HASH.fullmatch(self.manifest_sha256) is None
            or type(self.public_argv) is not tuple
            or any(type(value) is not str or "\x00" in value for value in self.public_argv)
            or self.managed_python_executable
            != self.managed_python_root / "bin" / "python3.12"
        ):
            raise InstallError("request_invalid", "manifest")


@dataclass(frozen=True, slots=True)
class InstallResult:
    """保存一次安装动作的安全终态。"""

    action: Literal["install", "update", "uninstall"]
    version: str | None
    platform: PlatformKey | None
    changed: bool
    events: tuple[InstallEvent, ...]

    def __post_init__(self) -> None:
        """拒绝无效 action、flag 或事件集合。"""
        if (
            self.action not in {"install", "update", "uninstall"}
            or self.version is not None
            and (
                type(self.version) is not str
                or len(self.version) > 200
                or _VERSION.fullmatch(self.version) is None
            )
            or self.platform is not None
            and type(self.platform) is not PlatformKey
            or self.action in {"install", "update"}
            and self.platform is None
            or type(self.changed) is not bool
            or type(self.events) is not tuple
            or any(type(event) is not InstallEvent for event in self.events)
        ):
            raise InstallError("installer_error", "manifest")


@dataclass(frozen=True, slots=True)
class _Downloaded:
    """绑定一次事务下载和 safe extraction 后的 Runtime inputs。"""

    root: Path
    wheel: Path
    requirements: Path
    node: Path
    tui: Path
    installer: Path
    sandbox_image: str


class _Operations(Protocol):
    """描述 Installer 状态机需要的最小副作用集合。"""

    def preflight(self, request: InstallRequest, bootstrap: BootstrapInputs) -> InstallPlan:
        """完成只读 manifest/platform/plan 校验。"""

    def select_target(
        self,
        request: InstallRequest,
        plan: InstallPlan,
        bootstrap: BootstrapInputs,
        public_argv: tuple[str, ...],
        environ: dict[str, str],
    ) -> tuple[str, tuple[str, ...], dict[str, str]] | None:
        """返回可选 target installer execve 三元组。"""

    def prepare_dependencies(
        self,
        plan: InstallPlan,
        bootstrap: BootstrapInputs,
    ) -> InstallPlan:
        """在 persistent layout 前验证 sandbox 并生成 live dependency plan。"""

    def layout(self, plan: InstallPlan) -> InstallLayout:
        """构造目标安装 layout。"""

    def lock(self, layout: InstallLayout) -> InstallLock:
        """获取 owner-only install lock。"""

    def previous_current(self, layout: InstallLayout) -> str | None:
        """返回事务前 current symlink target。"""

    def download(self, plan: InstallPlan, layout: InstallLayout) -> _Downloaded:
        """下载并解包全部 verified artifacts。"""

    def build(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        downloaded: _Downloaded,
    ) -> RuntimeReceipt:
        """构建并 smoke 尚未激活的 Runtime。"""

    def setup(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        built: RuntimeReceipt,
    ) -> None:
        """运行 staged setup/init 并安全导入文件。"""

    def doctor(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        built: RuntimeReceipt,
    ) -> bool:
        """运行本地 Doctor 并返回 Gateway service readiness。"""

    def activate(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        built: RuntimeReceipt,
    ) -> None:
        """原子切换 current。"""

    def commit(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        built: RuntimeReceipt,
    ) -> None:
        """写 stable launcher 与 receipt。"""

    def install_service(self, plan: InstallPlan, layout: InstallLayout) -> None:
        """安装并 health-check Gateway service。"""

    def retain(self, layout: InstallLayout) -> None:
        """只保留 current 与 N-1 Runtime。"""

    def rollback(self, layout: InstallLayout, previous: str | None, stage: str) -> None:
        """失败时恢复事务前 current。"""

    def cleanup(self, layout: InstallLayout) -> None:
        """清理本事务的临时下载和 staging。"""


class Installer:
    """协调一次 install/update/uninstall，具体副作用委托给受测组件。"""

    def __init__(
        self,
        bootstrap: BootstrapInputs,
        *,
        operations: _Operations | None = None,
        execve: Callable[[str, tuple[str, ...], dict[str, str]], object] = os.execve,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """绑定 bootstrap inputs、生产操作或离线 fake 与进程环境。"""
        if type(bootstrap) is not BootstrapInputs or not callable(execve):
            raise InstallError("request_invalid", "manifest")
        self._bootstrap = bootstrap
        self._operations = _SystemOperations(bootstrap) if operations is None else operations
        self._execve = execve
        self._environ = dict(os.environ if environ is None else environ)
        self._events: list[InstallEvent] = []

    def run(self, request: InstallRequest) -> InstallResult:
        """执行一次受锁事务或在 persistent write 前跳转到 target installer。

        Args:
            request: 已通过 strict model 校验的非 Secret 请求。

        Returns:
            安全终态和有序脱敏事件。

        Raises:
            InstallError: preflight、构建、Doctor、activation 或 service 失败。
        """
        if type(request) is not InstallRequest:
            raise InstallError("request_invalid", "model")
        self._events = []
        plan = self._operations.preflight(request, self._bootstrap)
        self._emit(InstallEvent("install.preflight", "ok", None, "manifest"))
        if request.dry_run:
            self._emit(
                InstallEvent(
                    "install.dependencies.deferred",
                    "warn",
                    None,
                    "system_argvs",
                )
            )
            return InstallResult(
                request.action,
                _dry_run_target_version(request, plan),
                plan.platform,
                False,
                tuple(self._events),
            )
        hop = self._environ.get(_INTERNAL_HOP)
        if hop not in {None, "", "1"}:
            raise InstallError("request_invalid", "manifest")
        handoff = self._operations.select_target(
            request,
            plan,
            self._bootstrap,
            self._bootstrap.public_argv,
            self._environ,
        )
        if handoff is not None:
            if hop == "1":
                raise InstallError("request_invalid", "manifest")
            executable, argv, environment = handoff
            self._execve(executable, argv, environment)
            raise InstallError("installer_error", "manifest")
        plan = self._operations.prepare_dependencies(plan, self._bootstrap)
        layout = self._operations.layout(plan)
        with self._operations.lock(layout):
            previous = self._operations.previous_current(layout)
            stage = "locked"
            try:
                downloaded = self._operations.download(plan, layout)
                stage = "downloaded"
                self._emit(InstallEvent("install.download", "ok", None, "artifacts"))
                built = self._operations.build(plan, layout, downloaded)
                stage = "built"
                self._emit(InstallEvent("install.staged", "ok", None, "version"))
                self._operations.setup(plan, layout, built)
                service_ready = self._operations.doctor(plan, layout, built)
                self._emit(InstallEvent("install.smoke", "ok", None, "service"))
                if not service_ready and request.service is True:
                    raise InstallError("doctor_blocked", "service")
                self._operations.activate(plan, layout, built)
                stage = "activated"
                self._emit(InstallEvent("install.activated", "ok", None, "version"))
                stage = "committing"
                self._operations.commit(plan, layout, built)
                stage = "committed"
                if plan.install_service and service_ready:
                    self._operations.install_service(plan, layout)
                    stage = "service"
                    self._emit(InstallEvent("service.installed", "ok", None, "service"))
                else:
                    self._emit(InstallEvent("service.skipped", "warn", None, "service"))
                self._operations.retain(layout)
                self._operations.cleanup(layout)
                self._emit(InstallEvent("install.complete", "ok", None, "version"))
                return InstallResult(
                    request.action,
                    plan.manifest.version,
                    plan.platform,
                    True,
                    tuple(self._events),
                )
            except BaseException as error:
                try:
                    self._operations.rollback(layout, previous, stage)
                except InstallError:
                    raise
                finally:
                    self._operations.cleanup(layout)
                if isinstance(error, InstallError):
                    raise
                raise InstallError("installer_error", "manifest") from None

    def _emit(self, event: InstallEvent) -> None:
        """把一个已脱敏事件追加到当前事务。"""
        self._events.append(event)


def _dry_run_target_version(request: InstallRequest, plan: InstallPlan) -> str | None:
    """在零下载条件下返回可证明的 target version，否则返回未知。"""
    if request.version is not None:
        return request.version
    if request.action == "update":
        return None
    if request.channel == "dev" and "-" not in plan.manifest.version:
        return None
    return plan.manifest.version


class _SystemOperations:
    """用 Tasks 4～10 primitives 执行生产安装副作用。"""

    def __init__(self, bootstrap: BootstrapInputs, opener: _Opener | None = None) -> None:
        """绑定 verified bootstrap paths 与可替换下载 opener。"""
        self.bootstrap = bootstrap
        self.opener = opener
        self.runner = _SubprocessRunner()
        self._platform: DetectedPlatform | None = None
        self._downloaded: dict[Path, _Downloaded] = {}
        self._download_roots: dict[Path, Path] = {}
        self._previous: dict[Path, str | None] = {}
        self._prior_receipts: dict[Path, InstallReceipt | None] = {}
        self._launcher_existed: dict[Path, bool] = {}
        self._command_existed: dict[Path, bool] = {}
        self._created_metadata_hashes: dict[Path, tuple[str, str]] = {}
        self._sandbox_artifact: Path | None = None
        self._sandbox_image: str | None = None

    def preflight(self, request: InstallRequest, bootstrap: BootstrapInputs) -> InstallPlan:
        """重验 bootstrap manifest、平台与所有 Release artifact 选择。"""
        manifest = ReleaseManifest.from_bytes(verify_bootstrap_inputs(bootstrap))
        platform = detect_platform(request)
        self._platform = platform
        canonical_request = replace(request, version=manifest.version)
        account = _invoking_account()
        layout = InstallLayout.for_request(canonical_request, account)
        required = ("wheel", "requirements", "node", "tui", "sandbox-image", "installer")
        artifacts = tuple(
            manifest.require_artifact(kind, platform.artifact_platform) for kind in required
        )
        return InstallPlan(
            request=canonical_request,
            manifest=manifest,
            platform=platform.artifact_platform,
            distro_id=platform.distro_id,
            distro_version=platform.distro_version,
            service_manager=platform.service_manager,
            program_prefix=layout.program_prefix,
            state_home=layout.state_home,
            artifact_filenames=tuple(artifact.filename for artifact in artifacts),
            system_argvs=(),
            install_service=request.service is not False,
            run_onboarding=request.onboard,
        )

    def select_target(
        self,
        request: InstallRequest,
        plan: InstallPlan,
        bootstrap: BootstrapInputs,
        public_argv: tuple[str, ...],
        environ: dict[str, str],
    ) -> tuple[str, tuple[str, ...], dict[str, str]] | None:
        """发现并校验不同目标 Release，然后构造一次 exact execve。"""
        if environ.get(_INTERNAL_HOP) == "1":
            return None
        needs_discovery = request.action == "update" or (
            request.version is not None and request.version != plan.manifest.version
        ) or request.channel == "dev" and "-" not in plan.manifest.version
        if not needs_discovery:
            return None
        source = resolve_release_source(request.channel, request.version, self.opener)
        payload = _read_bounded_url(
            source.manifest_url,
            _MAX_MANIFEST_BYTES,
            self.opener,
            code="manifest_invalid",
        )
        manifest = ReleaseManifest.from_bytes(payload)
        if source.requested_version is not None and manifest.version != source.requested_version:
            raise InstallError("manifest_invalid", "version")
        if request.action == "update":
            layout = InstallLayout.for_plan(plan)
            if layout.receipt.exists():
                installed = InstallReceipt.load(layout.receipt)
                if _semver_key(manifest.version) <= _semver_key(installed.version):
                    raise InstallError("request_invalid", "version")
        digest = hashlib.sha256(payload).hexdigest()
        if digest == bootstrap.manifest_sha256 and manifest.version == plan.manifest.version:
            return None
        bootstrap_root = bootstrap.manifest_file.parent
        root = bootstrap_root / f"target-{manifest.version}"
        root.mkdir(mode=0o700)
        manifest_path = bootstrap_root / f"release-manifest-{manifest.version}.json"
        _write_exclusive(manifest_path, payload, 0o600)
        installer_artifact = manifest.require_artifact("installer", plan.platform)
        installer_path = root / installer_artifact.filename
        download_artifact(installer_artifact, installer_path, self.opener)
        environment = {"PATH": _PATH, _INTERNAL_HOP: "1"}
        if request.system_prefix:
            account = _invoking_account()
            environment["SUDO_USER"] = account.pw_name
            environment["SUDO_UID"] = str(account.pw_uid)
        python = str(bootstrap.managed_python_executable)
        argv = (
            python,
            str(installer_path),
            request.action,
            *public_argv,
            "--manifest-file",
            str(manifest_path),
            "--manifest-sha256",
            digest,
            "--managed-uv",
            str(bootstrap.managed_uv),
            "--managed-python-root",
            str(bootstrap.managed_python_root),
            "--managed-python-executable",
            python,
        )
        return python, argv, environment

    def prepare_dependencies(
        self,
        plan: InstallPlan,
        bootstrap: BootstrapInputs,
    ) -> InstallPlan:
        """在 bootstrap tree 验证 sandbox，再生成并执行 Task5 live plan。"""
        if type(plan) is not InstallPlan or bootstrap != self.bootstrap or self._platform is None:
            raise InstallError("plan_invalid", "model")
        artifact = plan.manifest.require_artifact("sandbox-image", plan.platform)
        path = bootstrap.manifest_file.parent / artifact.filename
        downloaded = False
        try:
            download_artifact(artifact, path, self.opener)
            downloaded = True
            image = _load_sandbox_image(plan, path)
            dependencies = build_dependency_actions(
                self._platform,
                plan.request,
                manifest=plan.manifest,
                sandbox_artifact_path=path,
            )
            if type(dependencies) is not DependencyPlan:
                raise InstallError("system_dependency_missing", "system_argvs")
            system_argvs = _execute_dependency_actions(
                dependencies.actions,
                self._platform,
                plan.request,
                plan.manifest,
                path,
                self.runner,
            )
        except BaseException:
            if downloaded:
                _unlink_ephemeral(path)
            raise
        self._sandbox_artifact = path
        self._sandbox_image = image
        return replace(plan, system_argvs=system_argvs)

    def layout(self, plan: InstallPlan) -> InstallLayout:
        """从 preflight plan 构造受管 layout。"""
        return InstallLayout.for_plan(plan)

    def lock(self, layout: InstallLayout) -> InstallLock:
        """获取共用 owner-only install lock。"""
        return InstallLock.acquire(layout)

    def previous_current(self, layout: InstallLayout) -> str | None:
        """读取并限制事务前 current 相对 Runtime target。"""
        previous = _read_current(layout)
        self._previous[layout.program_prefix] = previous
        return previous

    def download(self, plan: InstallPlan, layout: InstallLayout) -> _Downloaded:
        """重验 sandbox 后下载其余五类 artifacts 并安全解包 Node/TUI。"""
        try:
            sandbox_path = self._sandbox_artifact
            if sandbox_path is None:
                raise InstallError("artifact_hash_mismatch", "artifacts")
            image = _load_sandbox_image(plan, sandbox_path)
            if image != self._sandbox_image:
                raise InstallError("artifact_hash_mismatch", "artifacts")
            runtime_parent_mode = 0o755 if plan.request.system_prefix else 0o700
            layout.runtimes_dir.mkdir(mode=runtime_parent_mode, parents=True, exist_ok=True)
            root = layout.runtimes_dir / f".{plan.manifest.version}.downloads"
            root.mkdir(mode=0o700)
            self._download_roots[layout.program_prefix] = root
            selected = {
                kind: plan.manifest.require_artifact(kind, plan.platform)
                for kind in ("wheel", "requirements", "node", "tui", "installer")
            }
            files = {
                kind: download_artifact(artifact, root / artifact.filename, self.opener)
                for kind, artifact in selected.items()
            }
            node = root / "node-tree"
            tui = root / "tui-tree"
            extract_tar_gz(
                files["node"],
                node,
                ExtractionLimits(),
                executable_paths={"node/bin/node"},
            )
            extract_tar_gz(files["tui"], tui, ExtractionLimits())
            node_root = node / "node" if (node / "node").is_dir() else node
            tui_root = tui / "tui" if (tui / "tui").is_dir() else tui
            downloaded = _Downloaded(
                root,
                files["wheel"],
                files["requirements"],
                node_root,
                tui_root,
                files["installer"],
                image,
            )
            self._downloaded[layout.program_prefix] = downloaded
            return downloaded
        except InstallError:
            raise
        except (OSError, UnicodeError):
            raise InstallError("artifact_download_failed", "artifacts") from None

    def build(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        downloaded: _Downloaded,
    ) -> RuntimeReceipt:
        """复用 RuntimeBuilder 构建、smoke 并发布未激活 Runtime。"""
        return RuntimeBuilder(self.runner).build(
            RuntimeInputs(
                layout=layout,
                manifest=plan.manifest,
                platform=plan.platform,
                wheel=downloaded.wheel,
                requirements=downloaded.requirements,
                node=downloaded.node,
                tui=downloaded.tui,
                installer=downloaded.installer,
                uv=self.bootstrap.managed_uv,
                managed_python_root=self.bootstrap.managed_python_root,
                managed_python_executable=self.bootstrap.managed_python_executable,
            )
        )

    def setup(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        built: RuntimeReceipt,
    ) -> None:
        """校验导入文件后从新 Runtime 运行 setup 或 init。"""
        del built
        downloaded = self._downloaded[layout.program_prefix]
        python = layout.runtime / "venv" / "bin" / "python"
        if plan.request.config_file is not None:
            _validate_and_import_config(
                plan.request.config_file,
                layout,
                python,
                downloaded.root,
                self.runner,
                plan.request.system_prefix,
            )
        if plan.request.secrets_file is not None:
            _import_secrets(
                plan.request.secrets_file,
                layout,
                python,
                self.runner,
                system_prefix=plan.request.system_prefix,
            )
        command = "setup" if plan.run_onboarding else "init"
        argv = (
            str(python),
            "-I",
            "-m",
            "miniclaw",
            command,
            "--home",
            str(layout.state_home),
            "--sandbox-image",
            downloaded.sandbox_image,
        )
        _checked_owner_command(
            self.runner,
            argv,
            layout,
            system_prefix=plan.request.system_prefix,
        )

    def doctor(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        built: RuntimeReceipt,
    ) -> bool:
        """运行新 Runtime Doctor，并校验 enabled Channel 的 Secret names。"""
        del built
        python = layout.runtime / "venv" / "bin" / "python"
        _checked_owner_command(
            self.runner,
            (
                str(python),
                "-I",
                "-m",
                "miniclaw",
                "doctor",
                "--home",
                str(layout.state_home),
            ),
            layout,
            system_prefix=plan.request.system_prefix,
        )
        if plan.request.system_prefix:
            return _service_inputs_complete_as_owner(
                layout.state_home / "config.toml",
                layout.secrets_file,
                python,
                self.runner,
            )
        return _service_inputs_complete(layout.state_home / "config.toml", layout.secrets_file)

    def activate(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        built: RuntimeReceipt,
    ) -> None:
        """原子激活已完成 Doctor 的 Runtime。"""
        del plan
        activate_runtime(layout, built)

    def commit(
        self,
        plan: InstallPlan,
        layout: InstallLayout,
        built: RuntimeReceipt,
    ) -> None:
        """安装 stable launcher 并原子写不含 Secret 的 receipt。"""
        prior: InstallReceipt | None = None
        if layout.receipt.exists():
            prior = InstallReceipt.load(layout.receipt)
        self._prior_receipts[layout.program_prefix] = prior
        self._launcher_existed[layout.program_prefix] = layout.launcher.exists()
        self._command_existed[layout.program_prefix] = layout.command_link.is_symlink()
        command_hash = _prior_command_hash(layout, prior)
        launcher_hash, _command_hash = install_launcher(
            layout,
            launcher_sha256=None if prior is None else prior.launcher_sha256,
            command_link_sha256=command_hash,
        )
        self._created_metadata_hashes[layout.program_prefix] = (
            launcher_hash,
            _command_hash,
        )
        previous = self._previous.get(layout.program_prefix)
        receipt = InstallReceipt(
            schema_version=1,
            version=plan.manifest.version,
            git_commit=plan.manifest.git_commit,
            platform=plan.platform,
            installed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            managed_files=(("bin/miniclaw", launcher_hash),),
            current_runtime=built.runtime_relative,
            previous_runtime=previous,
            service_label=None,
            service_file=None,
            service_file_sha256=None,
        )
        receipt.write(layout.receipt)

    def install_service(self, plan: InstallPlan, layout: InstallLayout) -> None:
        """安装 user service；system-prefix 通过 validated user 的 sudo helper。"""
        platform = (
            ServicePlatform.SYSTEMD_USER
            if plan.service_manager == "systemd-user"
            else ServicePlatform.LAUNCHD
        )
        if plan.request.system_prefix:
            digest, label, relative = _install_service_as_owner(layout, platform, self.runner)
        else:
            spec = render_service_spec(layout, platform)
            digest = service_install(spec, self.runner)
            label = spec.label
            relative = _service_relative(spec.path, _invoking_account())
        current = InstallReceipt.load(layout.receipt)
        replace(
            current,
            service_label=label,
            service_file=relative,
            service_file_sha256=digest,
        ).write(layout.receipt)

    def retain(self, layout: InstallLayout) -> None:
        """复用 Runtime retention，仅保留 current 与 N-1。"""
        retain_current_and_previous(layout)

    def rollback(self, layout: InstallLayout, previous: str | None, stage: str) -> None:
        """activation 后失败时只恢复仍指向本次 target 的 current。"""
        if stage not in {"activated", "committing", "committed", "service"}:
            return
        _restore_current(layout, previous)
        if layout.program_prefix in self._prior_receipts:
            prior = self._prior_receipts[layout.program_prefix]
            if prior is None:
                if layout.receipt.exists():
                    _remove_fresh_install_metadata(
                        layout,
                        launcher_existed=self._launcher_existed.get(
                            layout.program_prefix, True
                        ),
                        command_existed=self._command_existed.get(layout.program_prefix, True),
                    )
                else:
                    hashes = self._created_metadata_hashes.get(layout.program_prefix)
                    if hashes is not None:
                        _remove_created_install_metadata(
                            layout,
                            launcher_sha256=hashes[0],
                            command_sha256=hashes[1],
                            launcher_existed=self._launcher_existed.get(
                                layout.program_prefix, True
                            ),
                            command_existed=self._command_existed.get(
                                layout.program_prefix, True
                            ),
                        )
            else:
                prior.write(layout.receipt)

    def cleanup(self, layout: InstallLayout) -> None:
        """只删除本事务创建且仍在固定位置的 downloads tree。"""
        self._downloaded.pop(layout.program_prefix, None)
        download_root = self._download_roots.pop(layout.program_prefix, None)
        self._prior_receipts.pop(layout.program_prefix, None)
        self._launcher_existed.pop(layout.program_prefix, None)
        self._command_existed.pop(layout.program_prefix, None)
        self._created_metadata_hashes.pop(layout.program_prefix, None)
        sandbox = self._sandbox_artifact
        self._sandbox_artifact = None
        self._sandbox_image = None
        if sandbox is not None:
            _unlink_ephemeral(sandbox)
        if download_root is None:
            return
        try:
            if download_root.parent == layout.runtimes_dir and download_root.name == (
                f".{layout.runtime.name}.downloads"
            ):
                shutil.rmtree(download_root)
        except OSError:
            pass


def verify_bootstrap_inputs(inputs: BootstrapInputs) -> bytes:
    """component-wise no-follow 重验同一 current-owner bootstrap tree。

    Args:
        inputs: CLI 内部 handoff 解析出的五项信任输入。

    Returns:
        exact manifest bytes。

    Raises:
        InstallError: owner/type/mode/tree/hash 任一不匹配。
    """
    if type(inputs) is not BootstrapInputs:
        raise InstallError("request_invalid", "manifest")
    root = inputs.manifest_file.parent
    uid = os.geteuid()
    try:
        _require_private_directory(root, uid)
        for path in (
            inputs.managed_uv,
            inputs.managed_python_root,
            inputs.managed_python_executable,
        ):
            if not path.is_relative_to(root) or path == root:
                raise InstallError("request_invalid", "manifest")
            _validate_components(root, path, uid)
        _require_owned_directory(inputs.managed_python_root, uid)
        manifest = _read_private_file(
            inputs.manifest_file,
            uid,
            expected_mode=0o600,
            maximum=_MAX_MANIFEST_BYTES,
        )
        _require_private_executable(inputs.managed_uv, uid)
        _require_private_executable(inputs.managed_python_executable, uid)
    except InstallError:
        raise
    except (OSError, ValueError):
        raise InstallError("request_invalid", "manifest") from None
    if not hmac.compare_digest(hashlib.sha256(manifest).hexdigest(), inputs.manifest_sha256):
        raise InstallError("artifact_hash_mismatch", "manifest")
    return manifest


def emit_event(
    event: InstallEvent,
    *,
    json_output: bool,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    """把单个脱敏事件输出为 compact NDJSON 或有界人类状态行。

    Args:
        event: 已由 strict model 脱敏的事件。
        json_output: 为真时只写 stdout NDJSON，否则只写 stderr。
        stdout: 测试可替换 stdout。
        stderr: 测试可替换 stderr。

    Raises:
        InstallError: event 类型无效。
    """
    if type(event) is not InstallEvent:
        raise InstallError("event_invalid", "event")
    selected_stdout = sys.stdout if stdout is None else stdout
    selected_stderr = sys.stderr if stderr is None else stderr
    if json_output:
        document = {
            "code": event.code,
            "detail": event.detail,
            "event": event.name,
            "status": event.status,
        }
        print(
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            file=selected_stdout,
        )
        return
    line = f"[{event.status.upper()}] {event.name}: {event.detail}"
    encoded = line.encode("utf-8", errors="replace")[:4094]
    print(encoded.decode("utf-8", errors="ignore"), file=selected_stderr)


def _invoking_account() -> pwd.struct_passwd:
    """解析当前或 sudo 原始 non-root 用户。"""
    uid, user, original_uid = _production_identity()
    return _resolve_invoking_user(uid, user, original_uid)


def _execute_dependency_actions(
    actions: tuple[PrivilegeAction, ...],
    platform: DetectedPlatform,
    request: InstallRequest,
    manifest: ReleaseManifest,
    sandbox_artifact_path: Path,
    runner: _SubprocessRunner,
) -> tuple[tuple[str, ...], ...]:
    """逐项重验、执行并确认 Task5 privilege capabilities。

    Args:
        actions: Task5 live plan 生成的 immutable capabilities。
        platform: capabilities 绑定的 Tier 1 平台。
        request: 含显式 system package/service 授权的请求。
        manifest: sandbox artifact 所属 verified manifest。
        sandbox_artifact_path: bootstrap-private sandbox digest 文件。
        runner: 不经过 shell、输出有界的 subprocess runner。

    Returns:
        按实际执行顺序记录的 exact argv。

    Raises:
        InstallError: action 漂移、sudo 拒绝、执行失败或 follow-up 不收敛。
    """
    if type(actions) is not tuple or any(type(action) is not PrivilegeAction for action in actions):
        raise InstallError("privilege_denied", "system_argvs")
    pending = list(actions)
    executed: list[tuple[str, ...]] = []
    while pending:
        if len(executed) >= 16:
            raise InstallError("privilege_denied", "system_argvs")
        action = pending.pop(0)
        if action.argv in executed:
            raise InstallError("privilege_denied", "system_argvs")
        verify_privilege_action(
            action,
            platform,
            request,
            manifest=manifest,
            sandbox_artifact_path=sandbox_artifact_path,
            after_execution=False,
        )
        result = runner.run(action.argv, env={"PATH": _PATH}, timeout=_TIMEOUT)
        if type(result) is not CommandResult or result.returncode != 0:
            code = "privilege_denied" if action.requires_sudo else "system_dependency_missing"
            raise InstallError(code, "system_argvs")
        followups = verify_privilege_action(
            action,
            platform,
            request,
            manifest=manifest,
            sandbox_artifact_path=sandbox_artifact_path,
            after_execution=True,
        )
        executed.append(action.argv)
        if followups is None:
            continue
        for followup in reversed(followups):
            if followup.argv in executed:
                raise InstallError("privilege_denied", "system_argvs")
            if all(existing.argv != followup.argv for existing in pending):
                pending.insert(0, followup)
    return tuple(executed)


def _load_sandbox_image(plan: InstallPlan, path: Path) -> str:
    """重验 bootstrap-private sandbox artifact 并返回 digest-pinned image。"""
    artifact = plan.manifest.require_artifact("sandbox-image", plan.platform)
    try:
        payload = _read_private_file(
            path,
            os.geteuid(),
            expected_mode=0o600,
            maximum=artifact.size,
        )
    except InstallError:
        raise InstallError("artifact_hash_mismatch", "artifacts") from None
    if (
        len(payload) != artifact.size
        or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), artifact.sha256)
    ):
        raise InstallError("artifact_hash_mismatch", "artifacts")
    try:
        image = payload.decode("utf-8").strip()
    except UnicodeError:
        raise InstallError("manifest_invalid", "artifacts") from None
    if _IMAGE.fullmatch(image) is None:
        raise InstallError("manifest_invalid", "artifacts")
    return image


def _unlink_ephemeral(path: Path) -> None:
    """只删除仍为 current-owner regular file 的 bootstrap artifact。"""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            return
        path.unlink()
        _fsync_directory(path.parent)
    except OSError:
        pass


def _checked_owner_command(
    runner: _SubprocessRunner,
    argv: tuple[str, ...],
    layout: InstallLayout,
    *,
    system_prefix: bool,
) -> CommandResult:
    """用 minimal env 执行 staged state/Doctor 命令并拒绝 raw output 转发。"""
    account = _invoking_account()
    selected = argv
    if system_prefix:
        selected = (
            "/usr/bin/sudo",
            "-u",
            account.pw_name,
            "--",
            "/usr/bin/env",
            "-i",
            f"HOME={account.pw_dir}",
            f"PATH={_PATH}",
            f"MINICLAW_HOME={layout.state_home}",
            f"MINICLAW_ENV_FILE={layout.secrets_file}",
            *argv,
        )
    environment = {
        "HOME": account.pw_dir,
        "PATH": _PATH,
        "MINICLAW_HOME": str(layout.state_home),
        "MINICLAW_ENV_FILE": str(layout.secrets_file),
    }
    result = runner.run(selected, env=environment, timeout=_TIMEOUT)
    if type(result) is not CommandResult or result.returncode != 0:
        raise InstallError("doctor_blocked", "service")
    return result


def _validate_and_import_config(
    source: Path,
    layout: InstallLayout,
    python: Path,
    scratch: Path,
    runner: _SubprocessRunner,
    system_prefix: bool,
) -> None:
    """让 staged Runtime strict 解析 config 后以 O_EXCL 导入 state。"""
    account = _invoking_account()
    if system_prefix:
        _import_config_as_owner(source, layout, python, runner, account)
        return
    payload = _read_import_file(
        source,
        expected_mode=None,
        maximum=_MAX_CONFIG_BYTES,
        expected_uid=account.pw_uid,
    )
    validation = scratch / "config-validation"
    validation.mkdir(mode=0o700)
    candidate = validation / "config.toml"
    _write_exclusive(candidate, payload, 0o600)
    code = (
        "from pathlib import Path;from miniclaw.paths import build_state_paths;"
        "from miniclaw.config import load_config;import sys;"
        "load_config(build_state_paths(Path(sys.argv[1])),{})"
    )
    _checked_owner_command(
        runner,
        (str(python), "-I", "-c", code, str(validation)),
        layout,
        system_prefix=system_prefix,
    )
    layout.state_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_exclusive(layout.state_home / "config.toml", payload, 0o600)


def _import_secrets(
    source: Path,
    layout: InstallLayout,
    python: Path,
    runner: _SubprocessRunner,
    *,
    system_prefix: bool,
) -> None:
    """校验 owner-only dotenv 后在目标用户边界以 O_EXCL 导入。"""
    account = _invoking_account()
    if system_prefix:
        _import_secrets_as_owner(source, layout, python, runner, account)
        return
    payload = _read_import_file(
        source,
        expected_mode=0o600,
        maximum=_MAX_SECRET_BYTES,
        expected_uid=account.pw_uid,
    )
    _parse_secret_names(payload)
    layout.secrets_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_exclusive(layout.secrets_file, payload, 0o600)


def _service_inputs_complete(config: Path, secrets: Path) -> bool:
    """判断至少一个 enabled Channel 的固定 env names 是否全部存在。"""
    try:
        uid = _invoking_account().pw_uid
        config_bytes = _read_import_file(
            config,
            expected_mode=0o600,
            maximum=_MAX_CONFIG_BYTES,
            expected_uid=uid,
        )
        document = tomllib.loads(config_bytes.decode("utf-8"))
        names = _parse_secret_names(
            _read_import_file(
                secrets,
                expected_mode=0o600,
                maximum=_MAX_SECRET_BYTES,
                expected_uid=uid,
            )
        )
    except (InstallError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    channels = document.get("channels")
    if type(channels) is not dict:
        return False
    provider = document.get("provider", {})
    if type(provider) is not dict:
        return False
    api_key_env = provider.get("api_key_env", "MINICLAW_MODEL_API_KEY")
    if type(api_key_env) is not str or _ENV_NAME.fullmatch(api_key_env) is None:
        return False
    enabled = 0
    required = {api_key_env}
    for channel, fields in _CHANNEL_FIELDS.items():
        section = channels.get(channel)
        if type(section) is not dict or section.get("enabled") is not True:
            continue
        enabled += 1
        for field in fields:
            value = section.get(field)
            if type(value) is not str or _ENV_NAME.fullmatch(value) is None:
                return False
            required.add(value)
    return enabled > 0 and required.issubset(names)


def _service_inputs_complete_as_owner(
    config: Path,
    secrets: Path,
    python: Path,
    runner: _SubprocessRunner,
) -> bool:
    """让 system-prefix 目标用户读取配置与 Secret，并只返回 readiness token。"""
    account = _invoking_account()
    code = (
        "import sys;from pathlib import Path;"
        "from miniclaw.install.orchestrator import _service_inputs_complete;"
        "print('ready' if _service_inputs_complete(Path(sys.argv[1]),Path(sys.argv[2])) "
        "else 'not-ready')"
    )
    argv = (
        "/usr/bin/sudo",
        "-u",
        account.pw_name,
        "--",
        "/usr/bin/env",
        "-i",
        f"HOME={account.pw_dir}",
        f"PATH={_PATH}",
        str(python),
        "-I",
        "-c",
        code,
        str(config),
        str(secrets),
    )
    result = runner.run(argv, env={"PATH": _PATH}, timeout=_TIMEOUT)
    if type(result) is not CommandResult or result.returncode != 0:
        raise InstallError("doctor_blocked", "service")
    return result.stdout == b"ready\n"


def _parse_secret_names(payload: bytes) -> frozenset[str]:
    """解析受限 dotenv 并只返回 validated environment names。"""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InstallError("doctor_blocked", "secrets_file") from error
    names: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if (
            not separator
            or _ENV_NAME.fullmatch(name) is None
            or name in names
            or not value
            or value != value.strip()
            or value[0] in "'\""
            or "\x00" in value
        ):
            raise InstallError("doctor_blocked", "secrets_file")
        names.add(name)
    return frozenset(names)


def _import_config_as_owner(
    source: Path,
    layout: InstallLayout,
    python: Path,
    runner: _SubprocessRunner,
    account: pwd.struct_passwd,
) -> None:
    """让 system-prefix 目标用户 strict 解析并 O_EXCL 导入 config。"""
    code = (
        "import os,stat,sys,tempfile;from pathlib import Path;"
        "from miniclaw.config import load_config;from miniclaw.paths import build_state_paths;"
        "src=Path(sys.argv[1]);home=Path(sys.argv[2]);"
        "fd=os.open(src,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0));s=os.fstat(fd);"
        "assert stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and s.st_nlink==1 "
        f"and s.st_size<={_MAX_CONFIG_BYTES};"
        "data=os.read(fd,s.st_size+1);os.close(fd);"
        "assert len(data)==s.st_size;"
        "tmp=tempfile.TemporaryDirectory(prefix='miniclaw-config-');p=Path(tmp.name);"
        "c=p/'config.toml';c.write_bytes(data);c.chmod(0o600);"
        "load_config(build_state_paths(p),{});"
        "home.mkdir(mode=0o700,parents=True,exist_ok=True);"
        "d=home/'config.toml';o=os.open(d,os.O_WRONLY|os.O_CREAT|os.O_EXCL|"
        "getattr(os,'O_NOFOLLOW',0),0o600);os.write(o,data);os.fsync(o);os.close(o);"
        "tmp.cleanup()"
    )
    _run_owner_helper(runner, python, code, (str(source), str(layout.state_home)), account)


def _import_secrets_as_owner(
    source: Path,
    layout: InstallLayout,
    python: Path,
    runner: _SubprocessRunner,
    account: pwd.struct_passwd,
) -> None:
    """让 system-prefix 目标用户读取、校验并 O_EXCL 导入 Secret。"""
    code = (
        "import os,stat,sys,tempfile;from pathlib import Path;"
        "from miniclaw.env import load_dotenv;"
        "src=Path(sys.argv[1]);dst=Path(sys.argv[2]);"
        "fd=os.open(src,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0));s=os.fstat(fd);"
        "assert stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and s.st_nlink==1 "
        f"and stat.S_IMODE(s.st_mode)==0o600 and s.st_size<={_MAX_SECRET_BYTES};"
        "data=os.read(fd,s.st_size+1);os.close(fd);assert len(data)==s.st_size;"
        "tmp=tempfile.TemporaryDirectory(prefix='miniclaw-secret-');p=Path(tmp.name)/'s.env';"
        "p.write_bytes(data);p.chmod(0o600);load_dotenv(p,{});"
        "dst.parent.mkdir(mode=0o700,parents=True,exist_ok=True);"
        "o=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL|"
        "getattr(os,'O_NOFOLLOW',0),0o600);os.write(o,data);os.fsync(o);os.close(o);"
        "tmp.cleanup()"
    )
    _run_owner_helper(runner, python, code, (str(source), str(layout.secrets_file)), account)


def _run_owner_helper(
    runner: _SubprocessRunner,
    python: Path,
    code: str,
    arguments: tuple[str, ...],
    account: pwd.struct_passwd,
) -> None:
    """用 sudo user 与 fixed env 执行不回传配置/Secret 的 staged helper。"""
    argv = (
        "/usr/bin/sudo",
        "-u",
        account.pw_name,
        "--",
        "/usr/bin/env",
        "-i",
        f"HOME={account.pw_dir}",
        f"PATH={_PATH}",
        str(python),
        "-I",
        "-c",
        code,
        *arguments,
    )
    result = runner.run(argv, env={"PATH": _PATH}, timeout=_TIMEOUT)
    if result.returncode != 0:
        raise InstallError("doctor_blocked", "service")


def _install_service_as_owner(
    layout: InstallLayout,
    platform: ServicePlatform,
    runner: _SubprocessRunner,
) -> tuple[str, str, str]:
    """在 system-prefix 模式让 non-root child 调用 Task10 lifecycle。"""
    account = _invoking_account()
    code = (
        "import json;from pathlib import Path;from miniclaw.install.layout import InstallLayout;"
        "from miniclaw.install.service import ServicePlatform,render_service_spec,service_install;"
        "l=InstallLayout._build(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),"
        "sys.argv[4],owner_uid=int(sys.argv[5]),user_home=Path(sys.argv[6]));"
        "s=render_service_spec(l,ServicePlatform(sys.argv[7]));"
        "print(json.dumps([service_install(s),s.label,str(s.path)]))"
    )
    python = layout.runtime / "venv" / "bin" / "python"
    argv = (
        "/usr/bin/sudo",
        "-u",
        account.pw_name,
        "--",
        "/usr/bin/env",
        "-i",
        f"HOME={account.pw_dir}",
        f"PATH={_PATH}",
        str(python),
        "-I",
        "-c",
        "import sys;" + code,
        str(layout.program_prefix),
        str(layout.state_home),
        str(layout.command_link),
        layout.runtime.name,
        str(account.pw_uid),
        account.pw_dir,
        platform.value,
    )
    result = runner.run(argv, env={"HOME": account.pw_dir, "PATH": _PATH}, timeout=_TIMEOUT)
    try:
        value = json.loads(result.stdout.decode("utf-8"))
        digest, label, path = value
    except (UnicodeError, ValueError, TypeError):
        raise InstallError("service_install_failed", "service") from None
    if result.returncode != 0 or _HASH.fullmatch(digest) is None:
        raise InstallError("service_install_failed", "service")
    return digest, label, _service_relative(Path(path), account)


def _service_relative(path: Path, account: pwd.struct_passwd) -> str:
    """把固定用户 service path 转为 receipt 安全相对路径。"""
    try:
        return PurePosixPath(path.relative_to(Path(account.pw_dir))).as_posix()
    except ValueError:
        raise InstallError("service_install_failed", "service") from None


def _prior_command_hash(layout: InstallLayout, prior: InstallReceipt | None) -> str | None:
    """用 prior receipt 的 launcher ownership 绑定已有 command symlink。"""
    if prior is None:
        return None
    if not layout.command_link.is_symlink():
        raise InstallError("uninstall_ownership_mismatch", "manifest")
    expected = os.path.relpath(layout.launcher, start=layout.command_link.parent)
    if os.readlink(layout.command_link) != expected:
        raise InstallError("uninstall_ownership_mismatch", "manifest")
    return managed_file_sha256(layout.command_link, require_symlink=True)


def _remove_created_install_metadata(
    layout: InstallLayout,
    *,
    launcher_sha256: str,
    command_sha256: str,
    launcher_existed: bool,
    command_existed: bool,
) -> None:
    """receipt 未落地时按 exact hash 删除本事务 fresh launcher/link。"""
    try:
        if not command_existed and (
            not layout.command_link.is_symlink()
            or managed_file_sha256(layout.command_link, require_symlink=True)
            != command_sha256
        ):
            raise InstallError("rollback_conflict", "manifest")
        if not launcher_existed and (
            not layout.launcher.is_file()
            or layout.launcher.is_symlink()
            or managed_file_sha256(layout.launcher) != launcher_sha256
        ):
            raise InstallError("rollback_conflict", "manifest")
        if not command_existed:
            layout.command_link.unlink()
            _fsync_directory(layout.command_link.parent)
        if not launcher_existed:
            layout.launcher.unlink()
            _fsync_directory(layout.launcher.parent)
    except InstallError:
        raise
    except OSError:
        raise InstallError("rollback_conflict", "manifest") from None


def _remove_fresh_install_metadata(
    layout: InstallLayout,
    *,
    launcher_existed: bool,
    command_existed: bool,
) -> None:
    """service 失败时只删除本次 fresh commit 创建且仍匹配 receipt 的文件。"""
    try:
        receipt = InstallReceipt.load(layout.receipt)
        if receipt.version != layout.runtime.name:
            raise InstallError("rollback_conflict", "manifest")
        if not command_existed:
            expected = os.path.relpath(layout.launcher, start=layout.command_link.parent)
            if not layout.command_link.is_symlink() or os.readlink(layout.command_link) != expected:
                raise InstallError("rollback_conflict", "manifest")
            layout.command_link.unlink()
            _fsync_directory(layout.command_link.parent)
        if not launcher_existed:
            if managed_file_sha256(layout.launcher) != receipt.launcher_sha256:
                raise InstallError("rollback_conflict", "manifest")
            layout.launcher.unlink()
            _fsync_directory(layout.launcher.parent)
        layout.receipt.unlink()
        _fsync_directory(layout.receipt.parent)
    except InstallError:
        raise
    except OSError:
        raise InstallError("rollback_conflict", "manifest") from None


def _read_current(layout: InstallLayout) -> str | None:
    """读取 current 的受限相对 Runtime target。"""
    try:
        metadata = layout.current.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise InstallError("activation_failed", "manifest")
    target = os.readlink(layout.current)
    pure = PurePosixPath(target)
    if pure.is_absolute() or len(pure.parts) != 2 or pure.parts[0] != "runtimes":
        raise InstallError("activation_failed", "manifest")
    return target


def _restore_current(layout: InstallLayout, previous: str | None) -> None:
    """仅在 current 仍指向本次 target 时原子恢复事务前 target。"""
    try:
        if _read_current(layout) != f"runtimes/{layout.runtime.name}":
            raise InstallError("rollback_conflict", "manifest")
        if previous is None:
            layout.current.unlink()
        else:
            rollback = layout.current.with_name("current.rollback")
            os.symlink(previous, rollback)
            os.replace(rollback, layout.current)
        _fsync_directory(layout.program_prefix)
    except InstallError:
        raise
    except OSError:
        raise InstallError("rollback_conflict", "manifest") from None


def _read_import_file(
    path: Path,
    *,
    expected_mode: int | None,
    maximum: int,
    expected_uid: int | None = None,
) -> bytes:
    """no-follow 读取 absolute current-owner regular import file。"""
    if not _canonical_absolute(path):
        raise InstallError("request_invalid", "config_file")
    uid = os.geteuid() if expected_uid is None else expected_uid
    return _read_private_file(path, uid, expected_mode=expected_mode, maximum=maximum)


def _read_private_file(
    path: Path,
    uid: int,
    *,
    expected_mode: int | None,
    maximum: int,
) -> bytes:
    """用 O_NOFOLLOW 和 metadata snapshot 有界读取普通文件。"""
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != uid
            or before.st_nlink != 1
            or expected_mode is not None
            and stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_size > maximum
        ):
            raise InstallError("request_invalid", "manifest")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise InstallError("request_invalid", "manifest")
            payload = bytearray()
            while chunk := os.read(descriptor, min(64 * 1024, maximum - len(payload) + 1)):
                payload.extend(chunk)
                if len(payload) > maximum:
                    raise InstallError("request_invalid", "manifest")
            after = os.fstat(descriptor)
            if _stat_snapshot(after) != _stat_snapshot(opened):
                raise InstallError("request_invalid", "manifest")
            return bytes(payload)
        finally:
            os.close(descriptor)
    except InstallError:
        raise
    except OSError:
        raise InstallError("request_invalid", "manifest") from None


def _require_private_directory(path: Path, uid: int) -> None:
    """要求路径为 current-owner、0700、非 symlink 目录。"""
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise InstallError("request_invalid", "manifest")


def _require_owned_directory(path: Path, uid: int) -> None:
    """要求 child component 为 current-owner 且不可 group/world 写。"""
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise InstallError("request_invalid", "manifest")


def _require_private_executable(path: Path, uid: int) -> None:
    """要求路径为 current-owner、不可写共享、单链接普通可执行文件。"""
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_nlink != 1
        or not stat.S_IMODE(metadata.st_mode) & stat.S_IXUSR
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise InstallError("request_invalid", "manifest")


def _validate_components(root: Path, target: Path, uid: int) -> None:
    """从 bootstrap root 逐 component 拒绝 symlink 与宽权限目录。"""
    relative = target.relative_to(root)
    current = root
    for part in relative.parts[:-1] if target != current else ():
        current /= part
        _require_owned_directory(current, uid)


def _write_exclusive(path: Path, payload: bytes, mode: int) -> None:
    """以 O_EXCL/no-follow 写入、fsync 并持久化 parent。"""
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    """fsync 一个已验证目录的 namespace。"""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_absolute(path: object) -> bool:
    """判断 Path 是无 dot segment 的 lexical absolute path。"""
    return (
        isinstance(path, Path)
        and path.is_absolute()
        and path != Path("/")
        and str(path) == os.path.normpath(str(path))
        and "\x00" not in str(path)
    )


def _stat_snapshot(value: os.stat_result) -> tuple[int, ...]:
    """返回检测 concurrent mutation 所需的 metadata snapshot。"""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _semver_key(value: str) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    """返回足以拒绝 equal/downgrade 的 SemVer 排序键。"""
    semantic = value.split("+", 1)[0]
    core, separator, prerelease = semantic.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    identifiers = tuple(
        (0, int(part)) if part.isdecimal() else (1, part)
        for part in prerelease.split(".")
        if part
    )
    return major, minor, patch, 0 if separator else 1, identifiers


def _installer_error() -> Never:
    """抛出不含底层路径的稳定 installer error。"""
    raise InstallError("installer_error", "manifest")
