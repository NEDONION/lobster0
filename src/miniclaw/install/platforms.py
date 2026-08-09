"""提供安装器 Tier 1 平台检测与显式高权限动作计划。"""

import hashlib
import os
import platform as host_platform
import pwd
import re
import select
import selectors
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from miniclaw.install.models import (
    Artifact,
    InstallError,
    InstallRequest,
    PlatformKey,
    ReleaseManifest,
)

_ARCHITECTURES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
_DEBIAN_FAMILY = {"ubuntu", "debian"}
_RHEL_FAMILY = {"rhel", "rocky", "almalinux"}
_OS_RELEASE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_OS_RELEASE_VALUE = re.compile(r"^[A-Za-z0-9._-]+$")
_MACOS_VERSION = re.compile(r"^(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*)){1,2}$")
_RHEL_VERSION = re.compile(r"^(?:9|10)(?:\.(?:0|[1-9][0-9]*))?$")
_USER_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\$?$")
_PINNED_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_SAFE_EXECUTABLE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
_MAX_SANDBOX_RECEIPT_BYTES = 4096
_LOCAL_PROBE_TIMEOUT_SECONDS = 45.0
_PROCESS_GROUP_TERM_SECONDS = 0.05
_PROCESS_GROUP_CLEANUP_SECONDS = 0.25
_ROOTLESS_TOOLS = {
    "/usr/bin/dockerd-rootless-setuptool.sh",
    "/usr/share/docker.io/contrib/dockerd-rootless-setuptool.sh",
}
_APT_UPDATE = ("/usr/bin/sudo", "/usr/bin/apt-get", "update")
_APT_INSTALL = (
    "/usr/bin/sudo",
    "/usr/bin/apt-get",
    "install",
    "-y",
    "docker.io",
    "rootlesskit",
    "uidmap",
    "dbus-user-session",
    "slirp4netns",
    "fuse-overlayfs",
)
_DNF_INSTALL = (
    "/usr/bin/sudo",
    "/usr/bin/dnf",
    "install",
    "-y",
    "podman-docker",
    "slirp4netns",
    "fuse-overlayfs",
    "shadow-utils",
    "dbus-daemon",
)
_APT_REASON = "install fixed Debian rootless dependencies"
_DNF_REASON = "install fixed RHEL rootless dependencies"
_SETUP_REASON = "configure rootless Docker for target user"
_LINGER_REASON = "enable confirmed headless user service"
_CONTAINMENT_PROGRAM = (
    "import pathlib,socket\n"
    "try:\n pathlib.Path('/must-not-write').write_text('x')\n"
    "except OSError:\n print('root-write-denied')\n"
    "else:\n print('root-write-open')\n"
    "try:\n socket.create_connection(('1.1.1.1',53),timeout=1).close()\n"
    "except OSError:\n print('network-denied')\n"
    "else:\n print('network-open')\n"
)


@dataclass(frozen=True, slots=True)
class _SandboxArtifactReceipt:
    """保存从 strict manifest 与本地 artifact 当场派生的 sandbox image。

    Args:
        manifest: 已验证 sandbox-image artifact 所属 ReleaseManifest。
        artifact: manifest 唯一选择出的 sandbox-image Artifact。
        platform: 选择 artifact 时绑定的目标平台。
        path: 已 no-follow/hash 校验的本地 artifact 路径。
        container_image: artifact 内 digest-pinned image reference。
    """

    manifest: ReleaseManifest
    artifact: Artifact
    platform: PlatformKey
    path: Path
    container_image: str


@dataclass(frozen=True, slots=True)
class DependencyPlan:
    """保存可单独确认的 privilege actions；Task12 前不生成 sudo rerun。

    Args:
        actions: 允许交给独立确认/执行边界的 capability 集合。
        manual_rerun: Task12 trusted bootstrap 接管前固定为 ``None``。

    Raises:
        InstallError: capability 或 manual instruction 类型不严格。
    """

    actions: tuple["PrivilegeAction", ...]
    manual_rerun: None = None

    def __post_init__(self) -> None:
        """拒绝把展示提示混入可执行 capability 集合。

        Raises:
            InstallError: actions 或 manual instruction 类型被伪造。
        """
        if (
            type(self.actions) is not tuple
            or any(type(action) is not PrivilegeAction for action in self.actions)
            or self.manual_rerun is not None
        ):
            raise InstallError("system_dependency_missing", "system_argvs")


@dataclass(frozen=True, slots=True)
class _TestActivationResult:
    """保存 private seam 的非 capability 测试结果。

    Args:
        backend: fake probe 已检查的 backend 标识。
        uid: fake probe 已检查的非 root UID。
    """

    backend: str
    uid: int


class _BackendProbe(Protocol):
    """定义本地 Sandbox 证据与 no-follow 文件事实边界。"""

    def require_backend(
        self,
        platform: "DetectedPlatform",
        account: pwd.struct_passwd,
    ) -> None:
        """验证 executable、用户 context 与 containment，否则抛出稳定错误。"""

    def lstat(self, path: Path) -> os.stat_result:
        """返回固定路径且不跟随 symlink 的本地文件事实。"""


class LocalPlatformProbe:
    """用 stdlib 固定路径、rootless socket 与 live containment 派生 production readiness。"""

    def __init__(
        self,
        platform: "DetectedPlatform",
        *,
        manifest: ReleaseManifest | None = None,
        sandbox_artifact_path: Path | None = None,
    ) -> None:
        """从 strict manifest 与本地 hash receipt 派生 containment 需求。

        Args:
            platform: 当前已验证 Tier 1 平台。
            manifest: Linux ReleaseManifest；macOS 必须为 ``None``。
            sandbox_artifact_path: Linux 已下载的 sandbox-image artifact。

        Raises:
            InstallError: manifest/artifact 与平台不一致或本地 bytes 漂移。
        """
        if type(platform) is not DetectedPlatform:
            raise InstallError("system_dependency_missing", "platform")
        self._platform = platform
        self._receipt = _load_sandbox_receipt(platform, manifest, sandbox_artifact_path)

    def require_backend(
        self,
        platform: "DetectedPlatform",
        account: pwd.struct_passwd,
    ) -> None:
        """验证 backend executable/socket/context，并执行 fixed live containment smoke。

        Args:
            platform: 已校验 Tier 1 平台。
            account: 已验证的实际运行用户 passwd 记录。

        Raises:
            InstallError: executable、socket/context 或 containment 不通过。
        """
        try:
            if platform != self._platform:
                raise InstallError("system_dependency_missing", "platform")
            if platform.os == "macos":
                if self._receipt is not None:
                    raise InstallError("system_dependency_missing", "platform")
                _verify_seatbelt_containment(account)
            else:
                receipt = self._receipt
                if receipt is None:
                    raise InstallError("system_dependency_missing", "platform")
                _revalidate_sandbox_receipt(receipt)
                _verify_rootless_containment(platform, account, receipt.container_image)
        except Exception:
            raise InstallError("system_dependency_missing", "platform") from None

    def lstat(self, path: Path) -> os.stat_result:
        """用同一个 no-follow boundary 返回 setup tool 本地事实。

        Args:
            path: 两个固定 setup tool 候选之一。

        Returns:
            ``os.lstat`` 或显式测试 adapter 返回的文件事实。

        Raises:
            OSError: 路径不存在或不可访问。
        """
        try:
            return os.lstat(path)
        except Exception:
            raise InstallError("system_dependency_missing", "system_argvs") from None


@dataclass(frozen=True, slots=True)
class DetectedPlatform:
    """保存通过 Tier 1 校验的本机事实。

    Args:
        os: 规范操作系统名。
        distro_id: Linux 发行版 ID；macOS 固定为 ``macos``。
        distro_version: 已校验的系统版本。
        arch: 规范 Release 架构。
        service_manager: 受支持的用户级服务管理器。
        artifact_platform: 对应的 Release artifact key。
        sandbox_backend: 与 Phase 6 一致的非 root Sandbox backend。
    """

    os: Literal["linux", "macos"]
    distro_id: str
    distro_version: str
    arch: Literal["x86_64", "arm64"]
    service_manager: Literal["systemd-user", "launchd"]
    artifact_platform: PlatformKey
    sandbox_backend: Literal["docker-rootless", "podman-rootless", "seatbelt"]

    def __post_init__(self) -> None:
        """拒绝 OS、发行版、服务、artifact 与 Sandbox backend 的交叉错配。

        Raises:
            InstallError: 任一字段类型、Tier 1 值或交叉关系无效。
        """
        if (
            type(self.os) is not str
            or type(self.distro_id) is not str
            or type(self.distro_version) is not str
            or type(self.arch) is not str
            or type(self.service_manager) is not str
            or type(self.sandbox_backend) is not str
            or type(self.artifact_platform) is not PlatformKey
        ):
            raise InstallError("unsupported_platform", "platform")
        if self.os == "linux":
            expected_backend = (
                "docker-rootless" if self.distro_id in _DEBIAN_FAMILY else "podman-rootless"
            )
            valid = (
                _linux_version_supported(self.distro_id, self.distro_version)
                and self.service_manager == "systemd-user"
                and self.sandbox_backend == expected_backend
            )
        elif self.os == "macos":
            valid = (
                self.distro_id == "macos"
                and _macos_version_supported(self.distro_version)
                and self.service_manager == "launchd"
                and self.sandbox_backend == "seatbelt"
            )
        else:
            valid = False
        if (
            not valid
            or self.arch not in {"x86_64", "arm64"}
            or self.artifact_platform != PlatformKey(self.os, self.arch)
        ):
            raise InstallError("unsupported_platform", "platform")


@dataclass(frozen=True, slots=True)
class PrivilegeAction:
    """保存一条需要单独展示和确认的高权限 exact argv。

    Args:
        category: 高权限动作的封闭类别。
        argv: 不经过 shell 的固定参数数组。
        requires_sudo: 动作是否调用固定 ``/usr/bin/sudo``。
        reason: 可安全展示的固定原因。
    """

    category: Literal["system-package", "linger"]
    argv: tuple[str, ...]
    requires_sudo: bool
    reason: str

    def __post_init__(self) -> None:
        """限制 category、sudo 标记与 exact argv 的封闭组合。

        Raises:
            InstallError: 动作可注入、类别错配、reason 不可安全展示或 sudo 标记不一致。
        """
        if (
            type(self.argv) is not tuple
            or not 1 <= len(self.argv) <= 64
            or any(
                type(argument) is not str
                or not argument
                or len(argument) > 4096
                or not argument.isprintable()
                for argument in self.argv
            )
            or type(self.requires_sudo) is not bool
            or self.requires_sudo != (self.argv[0] == "/usr/bin/sudo")
            or type(self.reason) is not str
            or not 1 <= len(self.reason) <= 200
            or not self.reason.isprintable()
            or not _privilege_argv_allowed(self.category, self.argv)
            or self.reason != _privilege_reason(self.category, self.argv)
        ):
            raise InstallError("system_dependency_missing", "system_argvs")


def node_version_supported(version: object) -> bool:
    """判断 Node 三段版本是否属于已经完成门禁的 LTS 范围。

    Args:
        version: 待检查的三段整数版本；其他输入直接拒绝。

    Returns:
        版本位于 Node 22.22.3～23 或 24.15.0～25 时为 ``True``。
    """
    if (
        type(version) is not tuple
        or len(version) != 3
        or any(type(part) is not int or part < 0 for part in version)
    ):
        return False
    parsed = cast(tuple[int, int, int], version)
    return (22, 22, 3) <= parsed < (23, 0, 0) or (24, 15, 0) <= parsed < (25, 0, 0)


def detect_linux(
    os_release_text: str,
    machine: str,
    *,
    libc: str = "glibc",
    wsl: bool = False,
    service_requested: bool = False,
    service_manager: str = "systemd-user",
    effective_uid: int | None = None,
    original_user: str | None = None,
    original_uid: int | None = None,
    getpwuid: Callable[[int], pwd.struct_passwd] | None = None,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
) -> DetectedPlatform:
    """从注入的静态事实纯解析 Tier 1 Linux 平台。

    Args:
        os_release_text: ``/etc/os-release`` 原始文本，不作为 shell 执行。
        machine: ``uname -m`` 风格架构名。
        libc: libc 身份；Tier 1 只接受 glibc。
        wsl: 是否处于 WSL。
        service_requested: 请求是否要创建用户级服务。
        service_manager: 检测到的用户级服务管理器。
        effective_uid: 当前进程有效 UID。
        original_user: sudo/root 调用时的原始非 root 用户名。
        original_uid: sudo/root 调用时的原始非 root UID。
        getpwuid: 测试可注入的 UID 账号解析器。
        getpwnam: 测试可注入的用户名账号解析器。

    Returns:
        规范化且不可变的平台事实。

    Raises:
        InstallError: 任一事实不属于固定 Tier 1 或 root 身份无法安全归属。
    """
    if (
        type(wsl) is not bool
        or wsl
        or type(libc) is not str
        or libc.lower()
        not in {
            "glibc",
            "gnu",
        }
    ):
        raise InstallError("unsupported_platform", "platform")
    arch = _normalize_arch(machine)
    values = _parse_os_release(os_release_text)
    distro = values.get("ID", "").lower()
    version = values.get("VERSION_ID", "")
    if not _linux_version_supported(distro, version):
        raise InstallError("unsupported_platform", "distro_version")
    if type(service_requested) is not bool or type(service_manager) is not str:
        raise InstallError("unsupported_platform", "service_manager")
    if service_requested and service_manager != "systemd-user":
        raise InstallError("unsupported_platform", "service_manager")
    _resolve_invoking_user(
        effective_uid,
        original_user,
        original_uid,
        getpwuid=getpwuid,
        getpwnam=getpwnam,
    )
    backend = "docker-rootless" if distro in _DEBIAN_FAMILY else "podman-rootless"
    return DetectedPlatform(
        os="linux",
        distro_id=distro,
        distro_version=version,
        arch=arch,
        service_manager="systemd-user",
        artifact_platform=PlatformKey("linux", arch),
        sandbox_backend=backend,
    )


def detect_macos(
    version: str,
    machine: str,
    *,
    effective_uid: int | None = None,
    original_user: str | None = None,
    original_uid: int | None = None,
    getpwuid: Callable[[int], pwd.struct_passwd] | None = None,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
) -> DetectedPlatform:
    """从注入事实纯解析 Tier 1 macOS 平台。

    Args:
        version: ``platform.mac_ver()`` 返回的系统版本。
        machine: ``platform.machine()`` 返回的架构。
        effective_uid: 当前进程有效 UID。
        original_user: sudo/root 调用时的原始非 root 用户名。
        original_uid: sudo/root 调用时的原始非 root UID。
        getpwuid: 测试可注入的 UID 账号解析器。
        getpwnam: 测试可注入的用户名账号解析器。

    Returns:
        使用 launchd 与 Seatbelt 的规范平台事实。

    Raises:
        InstallError: 版本低于 13、架构不支持或 root 身份无法安全归属。
    """
    if not _macos_version_supported(version):
        raise InstallError("unsupported_platform", "distro_version")
    arch = _normalize_arch(machine)
    _resolve_invoking_user(
        effective_uid,
        original_user,
        original_uid,
        getpwuid=getpwuid,
        getpwnam=getpwnam,
    )
    return DetectedPlatform(
        os="macos",
        distro_id="macos",
        distro_version=version,
        arch=arch,
        service_manager="launchd",
        artifact_platform=PlatformKey("macos", arch),
        sandbox_backend="seatbelt",
    )


def detect_platform(
    request: InstallRequest,
    *,
    system: str | None = None,
    machine: str | None = None,
    os_release_text: str | None = None,
    macos_version: str | None = None,
    libc: str | None = None,
    wsl: bool | None = None,
    service_manager: str | None = None,
    effective_uid: int | None = None,
    original_user: str | None = None,
    original_uid: int | None = None,
    getpwuid: Callable[[int], pwd.struct_passwd] | None = None,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
) -> DetectedPlatform:
    """读取或接收确定性主机事实并执行 Tier 1 检测。

    Args:
        request: 已校验的非 Secret 安装请求。
        system: 可注入的 ``platform.system()`` 结果。
        machine: 可注入的 ``platform.machine()`` 结果。
        os_release_text: 可注入的 Linux os-release 文本。
        macos_version: 可注入的 macOS 版本。
        libc: 可注入的 Linux libc 身份。
        wsl: 可注入的 WSL 标记。
        service_manager: 可注入的用户服务管理器。
        effective_uid: 可注入的有效 UID。
        original_user: 可注入的原始 sudo 用户。
        original_uid: 可注入的原始 sudo UID。
        getpwuid: 测试可注入的 UID 账号解析器。
        getpwnam: 测试可注入的用户名账号解析器。

    Returns:
        通过 Tier 1 校验的平台事实。

    Raises:
        InstallError: 请求或主机事实不受支持，或 root 无法解析到真实原用户。
        OSError: 本机事实文件无法读取。
    """
    if type(request) is not InstallRequest:
        raise InstallError("unsupported_platform", "platform")
    selected_system = host_platform.system() if system is None else system
    selected_machine = host_platform.machine() if machine is None else machine
    selected_uid = os.geteuid() if effective_uid is None else effective_uid
    selected_user = original_user
    selected_original_uid = original_uid
    if selected_uid == 0 and original_user is None and original_uid is None:
        selected_user, selected_original_uid = _resolve_original_user(getpwnam=getpwnam)
    elif selected_uid == 0:
        _require_real_user(selected_user, selected_original_uid, getpwnam=getpwnam)
    if selected_system == "Linux":
        selected_text = _read_os_release() if os_release_text is None else os_release_text
        selected_libc = host_platform.libc_ver()[0] if libc is None else libc
        selected_wsl = "microsoft" in host_platform.release().lower() if wsl is None else wsl
        selected_manager = service_manager
        if selected_manager is None:
            selected_manager = (
                "systemd-user" if Path("/run/systemd/system").is_dir() else "unsupported"
            )
        return detect_linux(
            selected_text,
            selected_machine,
            libc=selected_libc,
            wsl=selected_wsl,
            service_requested=request.service is not False,
            service_manager=selected_manager,
            effective_uid=selected_uid,
            original_user=selected_user,
            original_uid=selected_original_uid,
            getpwuid=getpwuid,
            getpwnam=getpwnam,
        )
    if selected_system == "Darwin":
        selected_version = host_platform.mac_ver()[0] if macos_version is None else macos_version
        return detect_macos(
            selected_version,
            selected_machine,
            effective_uid=selected_uid,
            original_user=selected_user,
            original_uid=selected_original_uid,
            getpwuid=getpwuid,
            getpwnam=getpwnam,
        )
    raise InstallError("unsupported_platform", "platform")


def build_dependency_actions(
    platform: DetectedPlatform,
    request: InstallRequest,
    *,
    manifest: ReleaseManifest | None = None,
    sandbox_artifact_path: Path | None = None,
) -> DependencyPlan:
    """使用不可注入的 install-local production probe 构造权限动作。

    Args:
        platform: 已验证 Tier 1 平台。
        request: canonical installer 请求。
        manifest: Linux sandbox 使用的 strict ReleaseManifest。
        sandbox_artifact_path: Linux 已验证 sandbox-image artifact 路径。
    Returns:
        privilege capabilities；manual rerun 在 Task12 trusted bootstrap 前固定为空。

    Raises:
        InstallError: 本地 readiness、账号、artifact 或请求不安全。
    """
    if type(request) is InstallRequest and request.system_prefix:
        raise InstallError("privilege_denied", "system_argvs")
    effective_uid, original_user, original_uid = _production_identity()
    return _build_dependency_actions_with_probe(
        platform,
        request,
        probe=LocalPlatformProbe(
            platform,
            manifest=manifest,
            sandbox_artifact_path=sandbox_artifact_path,
        ),
        effective_uid=effective_uid,
        original_user=original_user,
        original_uid=original_uid,
    )


def _build_dependency_actions_with_probe(
    platform: DetectedPlatform,
    request: InstallRequest,
    *,
    probe: _BackendProbe,
    effective_uid: int | None = None,
    original_user: str | None = None,
    original_uid: int | None = None,
    getpwuid: Callable[[int], pwd.struct_passwd] | None = None,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
) -> DependencyPlan:
    """从已校验请求、真实账号和显式本地 probe 构造固定权限 argv。

    Args:
        platform: 已通过 Tier 1 校验的平台。
        request: 控制 package/service/system-prefix 的不可变安装请求。
        probe: 显式验证 backend 并提供 no-follow lstat 的本地 adapter。
        effective_uid: 当前进程有效 UID。
        original_user: root 调用的原始非 root 用户。
        original_uid: root 调用的原始非 root UID。
        getpwuid: 测试可注入的 UID 账号解析器。
        getpwnam: 测试可注入的用户名账号解析器。

    Returns:
        可供 dry-run 精确展示的 capability/manual 分离计划。

    Raises:
        InstallError: 请求、账号、probe、setup tool 或 backend 证据不安全。
    """
    if (
        type(platform) is not DetectedPlatform
        or type(request) is not InstallRequest
        or not callable(getattr(probe, "require_backend", None))
        or not callable(getattr(probe, "lstat", None))
    ):
        raise InstallError("system_dependency_missing", "platform")
    selected_uid = os.geteuid() if effective_uid is None else effective_uid
    account = _resolve_invoking_user(
        selected_uid,
        original_user,
        original_uid,
        getpwuid=getpwuid,
        getpwnam=getpwnam,
    )
    if request.system_prefix:
        # Task 12 bootstrap 尚未提供 trusted current-loaded installer receipt；一律 fail closed。
        raise InstallError("privilege_denied", "system_argvs")
    actions: list[PrivilegeAction] = []
    try:
        probe.require_backend(platform, account)
    except InstallError:
        backend_ready = False
    except Exception:
        raise InstallError("system_dependency_missing", "platform") from None
    else:
        backend_ready = True
    if not backend_ready and platform.os == "macos":
        raise InstallError("system_dependency_missing", "platform")
    if not backend_ready and not request.allow_system_packages:
        raise InstallError("system_dependency_missing", "allow_system_packages")
    if not backend_ready and platform.distro_id in _DEBIAN_FAMILY:
        actions.extend(
            (
                _action(
                    "system-package",
                    _APT_UPDATE,
                    _APT_REASON,
                ),
                _action(
                    "system-package",
                    _APT_INSTALL,
                    _APT_REASON,
                ),
            )
        )
    elif not backend_ready and platform.distro_id in _RHEL_FAMILY:
        actions.append(
            _action(
                "system-package",
                _DNF_INSTALL,
                _DNF_REASON,
            )
        )
    if request.service is True and platform.os == "linux":
        actions.append(
            _action(
                "linger",
                (
                    "/usr/bin/sudo",
                    "/usr/bin/loginctl",
                    "enable-linger",
                    account.pw_name,
                ),
                _LINGER_REASON,
            )
        )
    return DependencyPlan(tuple(actions))


def verify_privilege_action(
    action: PrivilegeAction,
    platform: DetectedPlatform,
    request: InstallRequest,
    *,
    manifest: ReleaseManifest | None = None,
    sandbox_artifact_path: Path | None = None,
    after_execution: bool = False,
) -> tuple[PrivilegeAction, ...] | None:
    """使用 production LocalPlatformProbe 执行 action revalidation。

    Args:
        action: 待执行 exact action。
        platform: 绑定 Tier 1 平台。
        request: 绑定 canonical request。
        manifest: Linux sandbox artifact 所属 strict manifest。
        sandbox_artifact_path: Linux 已下载 sandbox-image artifact。
        after_execution: 动作已完成时强制 backend re-probe。

    Returns:
        Debian package 完成后仍需执行的 setup/package actions；否则 ``None``。

    Raises:
        InstallError: 任一绑定或本地证据不安全。
    """
    effective_uid, original_user, original_uid = _production_identity()
    return _verify_privilege_action_with_probe(
        action,
        platform,
        request,
        probe=LocalPlatformProbe(
            platform,
            manifest=manifest,
            sandbox_artifact_path=sandbox_artifact_path,
        ),
        effective_uid=effective_uid,
        original_user=original_user,
        original_uid=original_uid,
        after_execution=after_execution,
    )


def _verify_privilege_action_with_probe(
    action: PrivilegeAction,
    platform: DetectedPlatform,
    request: InstallRequest,
    *,
    probe: _BackendProbe,
    effective_uid: int | None = None,
    original_user: str | None = None,
    original_uid: int | None = None,
    getpwuid: Callable[[int], pwd.struct_passwd] | None = None,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
    after_execution: bool = False,
) -> tuple[PrivilegeAction, ...] | None:
    """执行前重验 action 文件与账号，setup 后再验证同用户 backend。

    Args:
        action: build 阶段产生的 immutable 权限动作。
        platform: 绑定动作的 Tier 1 平台。
        request: 绑定动作的安装请求。
        probe: 与 build 共用的本地证据 adapter。
        effective_uid: 当前进程有效 UID。
        original_user: root 调用的原始非 root 用户。
        original_uid: root 调用的原始非 root UID。
        getpwuid: 测试可注入的 UID 账号解析器。
        getpwnam: 测试可注入的用户名账号解析器。
        after_execution: setup 已成功运行时要求重新验证 backend context/containment。

    Raises:
        InstallError: 动作、请求、账号、setup tool 或 postcondition 不再安全。
    """
    if (
        type(action) is not PrivilegeAction
        or type(platform) is not DetectedPlatform
        or type(request) is not InstallRequest
        or type(after_execution) is not bool
        or not callable(getattr(probe, "require_backend", None))
        or not callable(getattr(probe, "lstat", None))
    ):
        raise InstallError("system_dependency_missing", "system_argvs")
    selected_uid = os.geteuid() if effective_uid is None else effective_uid
    account = _resolve_invoking_user(
        selected_uid,
        original_user,
        original_uid,
        getpwuid=getpwuid,
        getpwnam=getpwnam,
    )
    tool = _setup_tool_from_action(action)
    if tool is not None:
        if platform.distro_id not in _DEBIAN_FAMILY:
            raise InstallError("privilege_denied", "system_argvs")
        if not _setup_tool_safe(tool, probe.lstat, account):
            raise InstallError("system_dependency_missing", "system_argvs")
        expected = (
            (str(tool), "install")
            if selected_uid != 0
            else (
                "/usr/bin/sudo",
                "-u",
                account.pw_name,
                "--",
                str(tool),
                "install",
            )
        )
        if action.argv != expected:
            raise InstallError("privilege_denied", "system_argvs")
        if after_execution:
            _verify_activation_ready_with_probe(
                platform,
                account,
                probe=probe,
            )
            return None
    if action.category == "linger":
        if (
            platform.os != "linux"
            or request.service is not True
            or action.argv[-1] != account.pw_name
        ):
            raise InstallError("privilege_denied", "service")
    elif action.category == "system-package":
        if not request.allow_system_packages:
            raise InstallError("system_dependency_missing", "allow_system_packages")
        if (
            action.argv in {_APT_UPDATE, _APT_INSTALL}
            and platform.distro_id not in _DEBIAN_FAMILY
            or action.argv == _DNF_INSTALL
            and platform.distro_id not in _RHEL_FAMILY
        ):
            raise InstallError("privilege_denied", "system_argvs")
    if after_execution and action.category == "system-package":
        try:
            _verify_activation_ready_with_probe(
                platform,
                account,
                probe=probe,
            )
            return None
        except InstallError:
            if action.argv == _APT_UPDATE:
                return (_action("system-package", _APT_INSTALL, _APT_REASON),)
            if action.argv == _APT_INSTALL:
                tool = _select_setup_tool(probe.lstat, account)
                if tool is None:
                    raise InstallError("system_dependency_missing", "platform") from None
                setup_argv = (str(tool), "install")
                if selected_uid == 0:
                    setup_argv = (
                        "/usr/bin/sudo",
                        "-u",
                        account.pw_name,
                        "--",
                        *setup_argv,
                    )
                return (
                    PrivilegeAction(
                        category="system-package",
                        argv=setup_argv,
                        requires_sudo=selected_uid == 0,
                        reason=_SETUP_REASON,
                    ),
                )
            raise
    return None


def verify_activation_ready(
    platform: DetectedPlatform,
    *,
    manifest: ReleaseManifest | None = None,
    sandbox_artifact_path: Path | None = None,
) -> None:
    """用即时构造的 production probe 验证 activation readiness。

    Args:
        platform: 已验证 Tier 1 平台。
        manifest: Linux sandbox-image 所属 strict manifest。
        sandbox_artifact_path: Linux 已下载 sandbox-image artifact。

    Raises:
        InstallError: 账号或 backend local evidence 不完整。
    """
    effective_uid, original_user, original_uid = _production_identity()
    account = _resolve_invoking_user(
        effective_uid,
        original_user,
        original_uid,
    )
    LocalPlatformProbe(
        platform,
        manifest=manifest,
        sandbox_artifact_path=sandbox_artifact_path,
    ).require_backend(
        platform,
        account,
    )


def _verify_activation_ready_with_probe(
    platform: DetectedPlatform,
    account: pwd.struct_passwd,
    *,
    probe: _BackendProbe,
) -> _TestActivationResult:
    """private test seam：成功 probe 后返回不可被 production 消费的测试结果。

    Args:
        platform: 已验证平台。
        account: 已验证 non-root account。
        probe: production probe 或 private offline fake。
    Returns:
        只供断言的 private backend/UID 结果。

    Raises:
        InstallError: probe 动态失败或输入错配。
    """
    try:
        probe.require_backend(platform, account)
    except Exception:
        raise InstallError("system_dependency_missing", "platform") from None
    return _TestActivationResult(platform.sandbox_backend, account.pw_uid)


def _parse_os_release(text: str) -> dict[str, str]:
    """纯解析有限 os-release 键值，不解释任何 shell 语法。

    Args:
        text: 有界 UTF-8 os-release 文本。

    Returns:
        只含 ``ID`` 与 ``VERSION_ID`` 的静态字符串映射。

    Raises:
        InstallError: 文本超限、目标键重复或目标值含非静态字符。
    """
    if type(text) is not str:
        raise InstallError("unsupported_platform", "platform")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise InstallError("unsupported_platform", "platform") from None
    if not 1 <= len(encoded) <= 65_536:
        raise InstallError("unsupported_platform", "platform")
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise InstallError("unsupported_platform", "platform")
        key, raw_value = line.split("=", 1)
        if _OS_RELEASE_KEY.fullmatch(key) is None:
            raise InstallError("unsupported_platform", "platform")
        if key not in {"ID", "VERSION_ID"}:
            continue
        if key in values:
            raise InstallError("unsupported_platform", "platform")
        value = raw_value
        if value[:1] in {'"', "'"}:
            if len(value) < 2 or value[-1] != value[0]:
                raise InstallError("unsupported_platform", "platform")
            value = value[1:-1]
        if _OS_RELEASE_VALUE.fullmatch(value) is None:
            raise InstallError("unsupported_platform", "platform")
        values[key] = value
    return values


def _linux_version_supported(distro: str, version: str) -> bool:
    """返回 distro/version 是否命中固定 Tier 1 matrix。

    Args:
        distro: 已规范化的小写发行版 ID。
        version: 静态 ``VERSION_ID`` 值。

    Returns:
        组合属于 Ubuntu、Debian 或 RHEL family 固定版本时为 ``True``。
    """
    if distro == "ubuntu":
        return version in {"22.04", "24.04"}
    if distro == "debian":
        return version in {"12", "13"}
    if distro in _RHEL_FAMILY:
        return _RHEL_VERSION.fullmatch(version) is not None
    return False


def _macos_version_supported(version: object) -> bool:
    """判断 macOS 版本是否为 13+ 的规范三段以内版本。

    Args:
        version: 待校验版本事实。

    Returns:
        值规范且 major 不小于 13 时为 ``True``。
    """
    return (
        type(version) is str
        and _MACOS_VERSION.fullmatch(version) is not None
        and int(version.split(".", 1)[0]) >= 13
    )


def _normalize_arch(machine: object) -> Literal["x86_64", "arm64"]:
    """规范化两个受支持 Release 架构，否则 fail closed。

    Args:
        machine: 待规范化的主机架构事实。

    Returns:
        Release 使用的 ``x86_64`` 或 ``arm64``。

    Raises:
        InstallError: 输入类型或架构不受支持。
    """
    if type(machine) is not str:
        raise InstallError("unsupported_platform", "platform")
    arch = _ARCHITECTURES.get(machine.lower())
    if arch is None:
        raise InstallError("unsupported_platform", "platform")
    return cast(Literal["x86_64", "arm64"], arch)


def _validate_invoking_user(
    effective_uid: object,
    original_user: object,
    original_uid: object,
) -> None:
    """确保 UID 0 调用始终绑定到规范非 root 原用户。

    Args:
        effective_uid: 当前进程的有效 UID 事实。
        original_user: 原始非 root 用户名事实。
        original_uid: 原始非 root UID 事实。

    Raises:
        InstallError: UID 无效或 root 调用没有规范非 root 归属。
    """
    if type(effective_uid) is not int or effective_uid < 0:
        raise InstallError("privilege_denied", "platform")
    if effective_uid != 0:
        return
    if not _valid_user(original_user) or type(original_uid) is not int or original_uid <= 0:
        raise InstallError("privilege_denied", "platform")


def _production_identity() -> tuple[int, str | None, int | None]:
    """读取不可注入的当前 euid，并在 root 时绑定真实 sudo 原用户。

    Returns:
        effective UID、可选 original user 与 original UID。

    Raises:
        InstallError: root 没有可验证的 ``SUDO_USER``/``SUDO_UID``。
    """
    effective_uid = os.geteuid()
    if effective_uid != 0:
        return effective_uid, None, None
    user, uid = _resolve_original_user()
    return effective_uid, user, uid


def _resolve_invoking_user(
    effective_uid: object,
    original_user: object,
    original_uid: object,
    *,
    getpwuid: Callable[[int], pwd.struct_passwd] | None = None,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
) -> pwd.struct_passwd:
    """把 public detector 的身份事实绑定到真实非 root passwd 记录。

    Args:
        effective_uid: 当前进程有效 UID；``None`` 时读取本进程。
        original_user: root 调用的原始用户名。
        original_uid: root 调用的原始 UID。
        getpwuid: 可注入的 UID 账号解析器。
        getpwnam: 可注入的用户名账号解析器。

    Returns:
        UID/name 精确匹配的非 root passwd 记录。

    Raises:
        InstallError: UID、原用户或系统账号记录不安全。
    """
    selected_uid = os.geteuid() if effective_uid is None else effective_uid
    _validate_invoking_user(selected_uid, original_user, original_uid)
    uid_lookup = pwd.getpwuid if getpwuid is None else getpwuid
    name_lookup = pwd.getpwnam if getpwnam is None else getpwnam
    try:
        if selected_uid == 0:
            account = name_lookup(cast(str, original_user))
            if account.pw_uid != original_uid:
                raise InstallError("privilege_denied", "platform")
        else:
            if original_user is not None or original_uid is not None:
                raise InstallError("privilege_denied", "platform")
            account = uid_lookup(cast(int, selected_uid))
    except InstallError:
        raise
    except Exception:
        raise InstallError("privilege_denied", "platform") from None
    if type(account) is not pwd.struct_passwd:
        raise InstallError("privilege_denied", "platform")
    if account.pw_uid <= 0 or account.pw_uid != selected_uid and selected_uid != 0:
        raise InstallError("privilege_denied", "platform")
    if (
        not _valid_user(account.pw_name)
        or selected_uid == 0
        and account.pw_name != original_user
        or not Path(account.pw_dir).is_absolute()
        or Path(account.pw_dir) == Path("/")
    ):
        raise InstallError("privilege_denied", "platform")
    return account


def _valid_user(value: object) -> bool:
    """判断用户名是否可安全作为 exact argv 单参数。

    Args:
        value: 待校验用户名。

    Returns:
        值为规范非 root POSIX 用户名时为 ``True``。
    """
    return type(value) is str and _USER_NAME.fullmatch(value) is not None and value != "root"


def _resolve_original_user(
    *,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
) -> tuple[str, int]:
    """从 sudo 固定环境解析并验证真实非 root 原用户。

    Returns:
        与系统账号数据库一致的用户名和正 UID。

    Args:
        getpwnam: 测试可注入的用户名账号解析器。

    Raises:
        InstallError: 环境缺失、类型不规范、用户不存在或 UID 不匹配。
    """
    user = os.environ.get("SUDO_USER")
    uid_text = os.environ.get("SUDO_UID")
    if (
        not _valid_user(user)
        or uid_text is None
        or not 1 <= len(uid_text) <= 10
        or not uid_text.isascii()
        or not uid_text.isdecimal()
        or uid_text != str(int(uid_text))
    ):
        raise InstallError("privilege_denied", "platform")
    uid = int(uid_text)
    if not 1 <= uid < 2**32 - 1:
        raise InstallError("privilege_denied", "platform")
    _require_real_user(user, uid, getpwnam=getpwnam)
    return cast(str, user), uid


def _require_real_user(
    user: object,
    uid: object,
    *,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
) -> None:
    """验证显式 root handoff 用户存在且 UID 精确匹配。

    Args:
        user: 待验证用户名。
        uid: 待验证 UID。
        getpwnam: 测试可注入的用户名账号解析器。

    Raises:
        InstallError: 用户事实不规范、不存在、是 root 或 UID 不匹配。
    """
    _validate_invoking_user(0, user, uid)
    lookup = pwd.getpwnam if getpwnam is None else getpwnam
    try:
        account = lookup(cast(str, user))
    except Exception:
        raise InstallError("privilege_denied", "platform") from None
    if (
        type(account) is not pwd.struct_passwd
        or account.pw_uid != uid
        or account.pw_uid == 0
        or account.pw_name != user
        or not Path(account.pw_dir).is_absolute()
        or Path(account.pw_dir) == Path("/")
    ):
        raise InstallError("privilege_denied", "platform")


def _read_os_release() -> str:
    """有界读取 Linux os-release，避免搜索或执行文件内容。

    Returns:
        UTF-8 解码后的原始文本。

    Raises:
        OSError: 固定文件不能读取。
        InstallError: 文件为空、超限或不是 UTF-8。
    """
    path = Path("/etc/os-release")
    data = path.read_bytes()
    if not 1 <= len(data) <= 65_536:
        raise InstallError("unsupported_platform", "platform")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise InstallError("unsupported_platform", "platform") from None


def _select_setup_tool(
    lstat: Callable[[Path], object],
    account: pwd.struct_passwd,
) -> Path | None:
    """按固定顺序选择第一个 no-follow regular executable setup tool。

    Args:
        lstat: 本地 no-follow 文件事实读取器。
        account: 将实际执行 setup tool 的已验证非 root 账号。

    Returns:
        安全候选绝对路径；两个候选都不可用时为 ``None``。
    """
    for value in (
        "/usr/bin/dockerd-rootless-setuptool.sh",
        "/usr/share/docker.io/contrib/dockerd-rootless-setuptool.sh",
    ):
        path = Path(value)
        if _setup_tool_safe(path, lstat, account):
            return path
    return None


def _setup_tool_safe(
    path: Path,
    lstat: Callable[[Path], object],
    account: pwd.struct_passwd,
) -> bool:
    """用 no-follow fact 判断固定 setup tool 是否可被目标用户执行。

    Args:
        path: 固定候选绝对路径。
        lstat: no-follow 文件事实读取器。
        account: 实际执行该工具的非 root 账号。

    Returns:
        路径属于候选、是 regular，且目标用户对应的 executable bit 有效时为 ``True``。
    """
    if str(path) not in _ROOTLESS_TOOLS:
        return False
    try:
        fact = lstat(path)
        mode = fact.st_mode  # type: ignore[attr-defined]
        uid = fact.st_uid  # type: ignore[attr-defined]
        gid = fact.st_gid  # type: ignore[attr-defined]
    except Exception:
        return False
    if (
        type(mode) is not int
        or type(uid) is not int
        or type(gid) is not int
        or uid < 0
        or gid < 0
        or not stat.S_ISREG(mode)
    ):
        return False
    if uid == account.pw_uid:
        executable = stat.S_IXUSR
    elif gid == account.pw_gid:
        executable = stat.S_IXGRP
    else:
        executable = stat.S_IXOTH
    return bool(mode & executable)


def _setup_tool_from_action(action: PrivilegeAction) -> Path | None:
    """从已校验 action 提取 rootless setup 固定路径。

    Args:
        action: 已通过 ``PrivilegeAction`` post-init 的动作。

    Returns:
        setup tool 路径；其他动作返回 ``None``。
    """
    for argument in action.argv:
        if argument in _ROOTLESS_TOOLS:
            return Path(argument)
    return None


def _verify_seatbelt_containment(account: pwd.struct_passwd) -> None:
    """验证 fixed Seatbelt executable 并执行 deny-default local smoke。

    Args:
        account: 必须与当前 non-root 用户一致的账号。

    Raises:
        InstallError: platform、executable 或 smoke 不可用。
    """
    executable = Path("/usr/bin/sandbox-exec")
    profile = (
        '(version 1) (deny default) (deny network*) (allow process-exec (literal "/usr/bin/true"))'
    )
    try:
        if host_platform.system() != "Darwin" or not _regular_executable(executable, account):
            raise OSError
        completed = _run_local_probe(
            (str(executable), "-p", profile, "--", "/usr/bin/true"),
            account,
            {},
        )
        if completed.returncode != 0 or completed.stdout or completed.stderr:
            raise OSError
    except Exception:
        raise InstallError("system_dependency_missing", "platform") from None


def _verify_rootless_containment(
    platform: DetectedPlatform,
    account: pwd.struct_passwd,
    image: str,
) -> None:
    """按 Phase 6 socket contract 验证 rootless CLI 并运行 hardened containment smoke。

    Args:
        platform: Linux Tier 1 backend。
        account: socket owner 与实际 CLI 用户。
        image: digest-pinned sandbox image。

    Raises:
        InstallError: CLI、socket/context、Podman compatibility 或 containment 不通过。
    """
    try:
        runtime = Path("/run/user") / str(account.pw_uid)
        engine_runtime = (
            runtime if platform.sandbox_backend == "docker-rootless" else runtime / "podman"
        )
        socket = (
            runtime / "docker.sock"
            if platform.sandbox_backend == "docker-rootless"
            else engine_runtime / "podman.sock"
        )
        for directory in dict.fromkeys((runtime, engine_runtime)):
            fact = os.lstat(directory)
            if (
                not stat.S_ISDIR(fact.st_mode)
                or fact.st_uid != account.pw_uid
                or stat.S_IMODE(fact.st_mode) != 0o700
            ):
                raise OSError
        socket_fact = os.lstat(socket)
        if not stat.S_ISSOCK(socket_fact.st_mode) or socket_fact.st_uid != account.pw_uid:
            raise OSError
        if platform.sandbox_backend == "podman-rootless":
            requested = Path("/usr/bin/docker")
            resolved = requested.resolve(strict=True)
            if resolved != Path("/usr/bin/podman") or not _regular_executable(
                resolved,
                account,
            ):
                raise OSError
            executable = requested
            socket_name = "CONTAINER_HOST"
        else:
            discovered = shutil.which("docker", path=_SAFE_EXECUTABLE_PATH)
            if discovered is None:
                raise OSError
            executable = Path(discovered).resolve(strict=True)
            if executable.name != "docker" or not _regular_executable(executable, account):
                raise OSError
            socket_name = "DOCKER_HOST"
        environment = {
            "HOME": account.pw_dir,
            "XDG_RUNTIME_DIR": str(runtime),
            socket_name: f"unix://{socket}",
        }
        if platform.sandbox_backend == "podman-rootless":
            version = _run_local_probe((str(executable), "--version"), account, environment)
            if version.returncode != 0 or b"podman" not in version.stdout.lower():
                raise OSError
        argv = (
            str(executable),
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "32",
            "--memory",
            "128m",
            "--user",
            "65532:65532",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16m",
            "--entrypoint",
            "python",
            "--",
            image,
            "-c",
            _CONTAINMENT_PROGRAM,
        )
        completed = _run_local_probe(argv, account, environment)
        if (
            completed.returncode != 0
            or completed.stdout != b"root-write-denied\nnetwork-denied\n"
            or completed.stderr
        ):
            raise OSError
    except Exception:
        raise InstallError("system_dependency_missing", "platform") from None


def _regular_executable(path: Path, account: pwd.struct_passwd) -> bool:
    """用 no-follow final fact 验证 regular file 与目标账号 execute bit。

    Args:
        path: resolved absolute executable。
        account: 实际执行账号。

    Returns:
        final path 非 symlink regular 且可执行时为 ``True``。
    """
    try:
        fact = os.lstat(path)
    except Exception:
        return False
    return stat.S_ISREG(fact.st_mode) and _mode_executable_by(fact, account)


def _run_local_probe(
    argv: tuple[str, ...],
    account: pwd.struct_passwd,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """以同一目标用户和固定最小环境执行 bounded local probe argv。

    Args:
        argv: 固定 probe argv。
        account: 实际执行的 non-root 用户。
        environment: 本地派生的 HOME/runtime/socket 环境。

    Returns:
        stdout/stderr 都不超过 4096 bytes 的完成结果。

    Raises:
        InstallError: subprocess 失败、超时或输出超限。
    """
    minimal = {"PATH": _SAFE_EXECUTABLE_PATH, "LANG": "C.UTF-8", **environment}
    command = argv
    child_environment = minimal
    if os.geteuid() == 0:
        assignments = tuple(f"{key}={value}" for key, value in sorted(minimal.items()))
        command = (
            "/usr/bin/sudo",
            "-u",
            account.pw_name,
            "--",
            "/usr/bin/env",
            "-i",
            *assignments,
            *argv,
        )
        child_environment = {"PATH": _SAFE_EXECUTABLE_PATH, "LANG": "C.UTF-8"}
    process: subprocess.Popen[bytes] | None = None

    def close_exit_observer() -> None:
        """在 exit observer 尚未创建时提供 no-op cleanup。"""

    cleanup_attempted = False
    deadline = time.monotonic() + _LOCAL_PROBE_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_environment,
            start_new_session=True,
        )
        direct_child_exited, close_exit_observer = _open_direct_child_exit_observer(process.pid)
        if process.stdout is None or process.stderr is None:
            raise OSError
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        output = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                events = selector.select(min(remaining, 0.25))
                for key, _mask in events:
                    try:
                        chunk = os.read(key.fd, 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = output[key.data]
                    target.extend(chunk)
                    if len(target) > 4096 or sum(map(len, output.values())) > 4096:
                        raise OSError
        finally:
            selector.close()
        _wait_for_direct_child_exit(direct_child_exited, deadline)
        cleanup_attempted = True
        returncode = _cleanup_probe_process_group(process)
        completed = subprocess.CompletedProcess(
            command,
            returncode,
            bytes(output["stdout"]),
            bytes(output["stderr"]),
        )
        return completed
    except Exception:
        if process is not None and not cleanup_attempted:
            cleanup_attempted = True
            try:
                _cleanup_probe_process_group(process)
            except Exception:
                pass
        raise InstallError("system_dependency_missing", "platform") from None
    finally:
        try:
            close_exit_observer()
        except Exception:
            pass
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass


def _open_direct_child_exit_observer(
    process_id: int,
) -> tuple[Callable[[], bool], Callable[[], None]]:
    """建立不 reap direct child 的退出观察器。

    Args:
        process_id: 刚由当前进程启动且尚未 reap 的 child PID。

    Returns:
        无阻塞退出检查与关闭函数；观察期间 leader 仍保留 zombie/PGID。

    Raises:
        OSError: 当前 POSIX 平台没有可靠的 no-reap 观察边界。
    """
    waitid = getattr(os, "waitid", None)
    if callable(waitid):

        def waitid_exited() -> bool:
            """用 WNOWAIT 观察 child 退出而不回收 leader。"""
            result = waitid(os.P_PID, process_id, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            return result is not None and result.si_pid == process_id

        return waitid_exited, lambda: None
    kqueue_type = getattr(select, "kqueue", None)
    kevent_type = getattr(select, "kevent", None)
    if not callable(kqueue_type) or not callable(kevent_type):
        raise OSError
    queue = kqueue_type()
    exited = False
    try:
        event = kevent_type(
            process_id,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_EXIT,
        )
        queue.control([event], 0, 0)
    except ProcessLookupError:
        # Popen 尚未 reap，因此立即 ESRCH 只能表示 child 已退出。
        exited = True
    except Exception:
        queue.close()
        raise OSError from None

    def kqueue_exited() -> bool:
        """用 EVFILT_PROC/NOTE_EXIT 观察 Darwin child 而不 reap。"""
        nonlocal exited
        if not exited:
            exited = bool(queue.control(None, 1, 0))
        return exited

    return kqueue_exited, queue.close


def _wait_for_direct_child_exit(exited: Callable[[], bool], deadline: float) -> None:
    """在同一 monotonic deadline 内等待 direct child 退出但不 reap。

    Args:
        exited: no-reap 退出观察器。
        deadline: 与 pipe/output 读取共享的 monotonic deadline。

    Raises:
        TimeoutError: child 未在 deadline 前退出。
    """
    while not exited():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        time.sleep(min(remaining, 0.01))


def _cleanup_probe_process_group(process: subprocess.Popen[bytes]) -> int:
    """在 reap leader 前无条件 TERM→grace→KILL 整个独立 process group。

    Args:
        process: 以 ``start_new_session=True`` 启动且尚未 reap 的 child。

    Returns:
        direct child 的 POSIX return code。

    Raises:
        OSError: 进程组无法在 bounded cleanup deadline 内回收。
    """
    process_group = process.pid
    cleanup_deadline = time.monotonic() + _PROCESS_GROUP_CLEANUP_SECONDS
    try:
        os.killpg(process_group, signal.SIGTERM)
    except OSError:
        pass
    grace_deadline = min(cleanup_deadline, time.monotonic() + _PROCESS_GROUP_TERM_SECONDS)
    while time.monotonic() < grace_deadline:
        time.sleep(min(0.01, grace_deadline - time.monotonic()))
    try:
        os.killpg(process_group, signal.SIGKILL)
    except OSError:
        pass
    while True:
        try:
            reaped_pid, status = os.waitpid(process.pid, os.WNOHANG)
        except ChildProcessError:
            raise OSError from None
        if reaped_pid == process.pid:
            returncode = os.waitstatus_to_exitcode(status)
            process.returncode = returncode
            return returncode
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 0:
            raise OSError
        time.sleep(min(remaining, 0.01))


def _lexical_absolute(path: Path) -> bool:
    """判断 Path 是无 dot-segment 的 absolute lexical 路径。

    Args:
        path: 待校验路径。

    Returns:
        路径绝对、非 root 且规范字符串不变化时为 ``True``。
    """
    return (
        path.is_absolute()
        and path != Path(path.anchor)
        and ".." not in path.parts
        and str(path) == os.path.normpath(str(path))
    )


def _read_verified_sandbox_artifact(
    manifest: ReleaseManifest,
    platform: PlatformKey,
    path: Path,
) -> bytes:
    """从 strict manifest 选择 sandbox-image Artifact 并 no-follow 重验本地 bytes。

    Args:
        manifest: 已完成 Task4 schema 校验的 ReleaseManifest。
        platform: 目标 Release 平台。
        path: 已下载 artifact 的 absolute lexical 路径。

    Returns:
        与 manifest size/hash 精确一致的有界 bytes。

    Raises:
        InstallError: 选择、路径、文件类型、size 或 hash 不一致。
    """
    try:
        if (
            type(manifest) is not ReleaseManifest
            or type(platform) is not PlatformKey
            or not isinstance(path, Path)
            or not _lexical_absolute(path)
        ):
            raise OSError
        artifact = manifest.require_artifact("sandbox-image", platform)
        if path.name != artifact.filename or artifact.size > _MAX_SANDBOX_RECEIPT_BYTES:
            raise OSError
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            fact = os.lstat(current)
            if stat.S_ISLNK(fact.st_mode):
                raise OSError
            if current != path and not stat.S_ISDIR(fact.st_mode):
                raise OSError
        final = os.lstat(path)
        if not stat.S_ISREG(final.st_mode) or final.st_size != artifact.size:
            raise OSError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != final.st_dev
                or opened.st_ino != final.st_ino
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise OSError
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            count = 0
            while chunk := os.read(descriptor, min(1_048_576, artifact.size - count + 1)):
                count += len(chunk)
                if count > artifact.size:
                    raise OSError
                digest.update(chunk)
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        if count != artifact.size or digest.hexdigest() != artifact.sha256:
            raise OSError
        return b"".join(chunks)
    except Exception:
        raise InstallError("system_dependency_missing", "platform") from None


def _load_sandbox_receipt(
    platform: DetectedPlatform,
    manifest: ReleaseManifest | None,
    path: Path | None,
) -> _SandboxArtifactReceipt | None:
    """从 strict sandbox-image Artifact 当场派生不可注入的 local receipt。

    Args:
        platform: 已验证 Tier 1 平台。
        manifest: Linux manifest；macOS 必须为空。
        path: Linux 本地 sandbox-image artifact；macOS 必须为空。

    Returns:
        Linux receipt；macOS 返回 ``None``。

    Raises:
        InstallError: platform/manifest/path 或 digest artifact 不一致。
    """
    if platform.os == "macos":
        if manifest is not None or path is not None:
            raise InstallError("system_dependency_missing", "platform")
        return None
    if type(manifest) is not ReleaseManifest or not isinstance(path, Path):
        raise InstallError("system_dependency_missing", "platform")
    data = _read_verified_sandbox_artifact(
        manifest,
        platform.artifact_platform,
        path,
    )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise InstallError("system_dependency_missing", "platform") from None
    image = text.removesuffix("\n")
    if "\n" in image or _PINNED_IMAGE.fullmatch(image) is None:
        raise InstallError("system_dependency_missing", "platform")
    return _SandboxArtifactReceipt(
        manifest,
        manifest.require_artifact("sandbox-image", platform.artifact_platform),
        platform.artifact_platform,
        path,
        image,
    )


def _revalidate_sandbox_receipt(receipt: _SandboxArtifactReceipt) -> None:
    """在每次 containment 前重新验证 receipt path 未被替换。

    Args:
        receipt: 内部从 manifest 与 artifact 派生的 receipt。

    Raises:
        InstallError: receipt 类型或本地 bytes/hash 已漂移。
    """
    if type(receipt) is not _SandboxArtifactReceipt:
        raise InstallError("system_dependency_missing", "platform")
    data = _read_verified_sandbox_artifact(
        receipt.manifest,
        receipt.platform,
        receipt.path,
    )
    if data.decode("utf-8").removesuffix("\n") != receipt.container_image:
        raise InstallError("system_dependency_missing", "platform")


def _mode_executable_by(fact: os.stat_result, account: pwd.struct_passwd) -> bool:
    """判断 no-follow stat mode 对目标账号的对应 execute bit 是否有效。

    Args:
        fact: ``lstat``/``fstat`` 文件事实。
        account: 目标 non-root 账号。

    Returns:
        owner、primary group 或 other 对应 execute bit 有效时为 ``True``。
    """
    if fact.st_uid == account.pw_uid:
        executable = stat.S_IXUSR
    elif fact.st_gid == account.pw_gid:
        executable = stat.S_IXGRP
    else:
        executable = stat.S_IXOTH
    return bool(fact.st_mode & executable)


def _action(
    category: Literal["system-package", "linger"],
    argv: tuple[str, ...],
    reason: str,
) -> PrivilegeAction:
    """构造必须经独立确认且显式使用 sudo 的固定动作。

    Args:
        category: 固定权限动作类别。
        argv: 已在调用点封闭定义的 exact argv。
        reason: 可安全展示的固定原因。

    Returns:
        不含可由调用方伪造 approval 位且 ``requires_sudo=True`` 的不可变动作。
    """
    return PrivilegeAction(
        category=category,
        argv=argv,
        requires_sudo=True,
        reason=reason,
    )


def _privilege_argv_allowed(category: object, argv: tuple[str, ...]) -> bool:
    """判断 public PrivilegeAction 是否命中封闭 exact argv 形状。

    Args:
        category: 待校验动作类别。
        argv: 待校验 exact argv。

    Returns:
        argv 是固定包命令、受限 setup 或 linger 动作时为 ``True``。
    """
    if category == "system-package":
        if argv in {_APT_UPDATE, _APT_INSTALL, _DNF_INSTALL}:
            return True
        direct_setup = len(argv) == 2 and argv[0] in _ROOTLESS_TOOLS and argv[1] == "install"
        sudo_setup = (
            len(argv) == 6
            and argv[:2] == ("/usr/bin/sudo", "-u")
            and _valid_user(argv[2])
            and argv[3] == "--"
            and argv[4] in _ROOTLESS_TOOLS
            and argv[5:] == ("install",)
        )
        return direct_setup or sudo_setup
    if category == "linger":
        return (
            len(argv) == 4
            and argv[:3] == ("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger")
            and _valid_user(argv[3])
        )
    return False


def _privilege_reason(category: object, argv: tuple[str, ...]) -> str | None:
    """返回与 exact category/argv 唯一绑定的安全展示 reason。

    Args:
        category: strict action category。
        argv: exact action argv。

    Returns:
        固定 reason；无匹配时为 ``None``。
    """
    if category == "system-package":
        if argv in {_APT_UPDATE, _APT_INSTALL}:
            return _APT_REASON
        if argv == _DNF_INSTALL:
            return _DNF_REASON
        if _setup_tool_from_action_argv(argv) is not None:
            return _SETUP_REASON
    if category == "linger":
        return _LINGER_REASON
    return None


def _setup_tool_from_action_argv(argv: tuple[str, ...]) -> Path | None:
    """从尚未构造 PrivilegeAction 的 argv 提取 setup tool。

    Args:
        argv: exact action argv。

    Returns:
        固定 setup path 或 ``None``。
    """
    for argument in argv:
        if argument in _ROOTLESS_TOOLS:
            return Path(argument)
    return None
