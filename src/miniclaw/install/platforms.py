"""提供安装器 Tier 1 平台检测与显式高权限动作计划。"""

import hashlib
import os
import platform as host_platform
import pwd
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from miniclaw.install.models import InstallError, InstallRequest, PlatformKey

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
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PINNED_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_SAFE_EXECUTABLE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
_MAX_INSTALLER_BYTES = 268_435_456
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
_SYSTEM_PREFIX_REASON = "rerun verified installer for explicit system prefix"
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
class SandboxVerification:
    """保存 production containment 使用的固定 pinned image。

    Args:
        container_image: Linux 必须提供的 digest-pinned sandbox image；macOS 为 ``None``。
    """

    container_image: str | None

    def __post_init__(self) -> None:
        """拒绝可变 image tag 或非字符串输入。

        Raises:
            InstallError: image 不是 ``name@sha256:<64hex>`` 或 ``None``。
        """
        if self.container_image is not None and (
            type(self.container_image) is not str
            or _PINNED_IMAGE.fullmatch(self.container_image) is None
        ):
            raise InstallError("system_dependency_missing", "platform")


@dataclass(frozen=True, slots=True)
class InstallerArtifactEvidence:
    """绑定 verified installer artifact 的 lexical path 与 expected SHA-256。

    Args:
        path: 将被 exact argv 执行的本地 installer artifact。
        expected_sha256: manifest/bootstrap 已验证的 lowercase SHA-256。
    """

    path: Path
    expected_sha256: str

    def __post_init__(self) -> None:
        """只接受绝对 lexical Path 与规范 SHA-256；文件事实由执行边界重验。

        Raises:
            InstallError: path/hash 类型或 lexical 形式无效。
        """
        if (
            not isinstance(self.path, Path)
            or not _lexical_absolute(self.path)
            or type(self.expected_sha256) is not str
            or _SHA256.fullmatch(self.expected_sha256) is None
        ):
            raise InstallError("privilege_denied", "system_argvs")


@dataclass(frozen=True, slots=True, init=False)
class ActivationEvidence:
    """表示 production backend 已为同一非 root UID 完成本地 containment 验证。"""

    backend: Literal["docker-rootless", "podman-rootless", "seatbelt"]
    uid: int
    verification: str

    def __new__(cls) -> "ActivationEvidence":
        """拒绝 public constructor；evidence 只能由本模块验证函数铸造。

        Raises:
            TypeError: 所有公开构造尝试。
        """
        raise TypeError("ActivationEvidence cannot be constructed directly")


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

    def __init__(self, verification: SandboxVerification) -> None:
        """绑定 digest-pinned containment 需求，不提供 dependency injection。

        Args:
            verification: Linux image 或 macOS 无 image 的 strict 需求。

        Raises:
            InstallError: verification 不是 strict public model。
        """
        if type(verification) is not SandboxVerification:
            raise InstallError("system_dependency_missing", "platform")
        self._verification = verification

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
            if platform.os == "macos":
                if self._verification.container_image is not None:
                    raise InstallError("system_dependency_missing", "platform")
                _verify_seatbelt_containment(account)
            else:
                image = self._verification.container_image
                if image is None:
                    raise InstallError("system_dependency_missing", "platform")
                _verify_rootless_containment(platform, account, image)
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
                "docker-rootless"
                if self.distro_id in _DEBIAN_FAMILY
                else "podman-rootless"
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

    category: Literal["system-package", "linger", "system-prefix"]
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
    if type(wsl) is not bool or wsl or type(libc) is not str or libc.lower() not in {
        "glibc",
        "gnu",
    }:
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
        selected_wsl = (
            "microsoft" in host_platform.release().lower() if wsl is None else wsl
        )
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
    verification: SandboxVerification,
    installer_artifact: InstallerArtifactEvidence | None = None,
) -> tuple[PrivilegeAction, ...]:
    """使用不可注入的 install-local production probe 构造权限动作。

    Args:
        platform: 已验证 Tier 1 平台。
        request: canonical installer 请求。
        verification: backend live containment 需求。
        installer_artifact: system-prefix rerun 的 verified installer artifact。
    Returns:
        immutable exact privilege actions。

    Raises:
        InstallError: 本地 readiness、账号、artifact 或请求不安全。
    """
    effective_uid, original_user, original_uid = _production_identity()
    return _build_dependency_actions_with_probe(
        platform,
        request,
        probe=LocalPlatformProbe(verification),
        verification=verification,
        installer_artifact=installer_artifact,
        effective_uid=effective_uid,
        original_user=original_user,
        original_uid=original_uid,
    )


def _build_dependency_actions_with_probe(
    platform: DetectedPlatform,
    request: InstallRequest,
    *,
    probe: _BackendProbe,
    verification: SandboxVerification | None = None,
    installer_artifact: InstallerArtifactEvidence | None = None,
    effective_uid: int | None = None,
    original_user: str | None = None,
    original_uid: int | None = None,
    getpwuid: Callable[[int], pwd.struct_passwd] | None = None,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
) -> tuple[PrivilegeAction, ...]:
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
        verification: production containment 需求；private fake seam 可为 ``None``。
        installer_artifact: non-root system-prefix 的 verified installer artifact。

    Returns:
        可供 dry-run 精确展示的不可变权限动作集合。

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
    if request.system_prefix and selected_uid != 0:
        if installer_artifact is None:
            raise InstallError("privilege_denied", "system_argvs")
        _validate_installer_artifact(installer_artifact, account)
        rerun_argv = _canonical_rerun_argv(installer_artifact.path, request)
        actions.append(
            PrivilegeAction(
                category="system-prefix",
                argv=("/usr/bin/sudo", "--", *rerun_argv),
                requires_sudo=True,
                reason=_SYSTEM_PREFIX_REASON,
            )
        )
    return tuple(actions)


def verify_privilege_action(
    action: PrivilegeAction,
    platform: DetectedPlatform,
    request: InstallRequest,
    *,
    verification: SandboxVerification,
    installer_artifact: InstallerArtifactEvidence | None = None,
    after_execution: bool = False,
) -> ActivationEvidence | tuple[PrivilegeAction, ...] | None:
    """使用 production LocalPlatformProbe 执行 action revalidation。

    Args:
        action: 待执行 exact action。
        platform: 绑定 Tier 1 平台。
        request: 绑定 canonical request。
        verification: backend containment 需求。
        installer_artifact: system-prefix verified artifact evidence。
        after_execution: 动作已完成时强制 backend re-probe。

    Returns:
        after-execution backend evidence；执行前返回 ``None``。

    Raises:
        InstallError: 任一绑定或本地证据不安全。
    """
    effective_uid, original_user, original_uid = _production_identity()
    return _verify_privilege_action_with_probe(
        action,
        platform,
        request,
        probe=LocalPlatformProbe(verification),
        verification=verification,
        installer_artifact=installer_artifact,
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
    verification: SandboxVerification | None = None,
    installer_artifact: InstallerArtifactEvidence | None = None,
    effective_uid: int | None = None,
    original_user: str | None = None,
    original_uid: int | None = None,
    getpwuid: Callable[[int], pwd.struct_passwd] | None = None,
    getpwnam: Callable[[str], pwd.struct_passwd] | None = None,
    after_execution: bool = False,
) -> ActivationEvidence | tuple[PrivilegeAction, ...] | None:
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
            return _verify_activation_ready_with_probe(
                platform,
                account,
                probe=probe,
                verification=verification,
            )
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
    elif action.category == "system-prefix":
        if installer_artifact is None:
            raise InstallError("privilege_denied", "system_argvs")
        _validate_installer_artifact(installer_artifact, account)
        expected_rerun = _canonical_rerun_argv(installer_artifact.path, request)
        if (
            selected_uid == 0
            or not request.system_prefix
            or action.argv != ("/usr/bin/sudo", "--", *expected_rerun)
        ):
            raise InstallError("privilege_denied", "system_argvs")
    if after_execution and action.category == "system-package":
        try:
            return _verify_activation_ready_with_probe(
                platform,
                account,
                probe=probe,
                verification=verification,
            )
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
    verification: SandboxVerification,
) -> ActivationEvidence:
    """用不可注入 production probe 返回 activation 必需的 typed evidence。

    Args:
        platform: 已验证 Tier 1 平台。
        verification: live containment 需求。
    Returns:
        只能由本函数生成的 ``ActivationEvidence``。

    Raises:
        InstallError: 账号或 backend local evidence 不完整。
    """
    effective_uid, original_user, original_uid = _production_identity()
    account = _resolve_invoking_user(
        effective_uid,
        original_user,
        original_uid,
    )
    return _verify_activation_ready_with_probe(
        platform,
        account,
        probe=LocalPlatformProbe(verification),
        verification=verification,
    )


def _verify_activation_ready_with_probe(
    platform: DetectedPlatform,
    account: pwd.struct_passwd,
    *,
    probe: _BackendProbe,
    verification: SandboxVerification | None = None,
) -> ActivationEvidence:
    """private test seam：成功 probe 后铸造不可公开构造的 activation evidence。

    Args:
        platform: 已验证平台。
        account: 已验证 non-root account。
        probe: production probe 或 private offline fake。
        verification: production containment requirement。

    Returns:
        与 backend/UID/image 精确绑定的 activation evidence。

    Raises:
        InstallError: probe 动态失败或输入错配。
    """
    try:
        probe.require_backend(platform, account)
    except Exception:
        raise InstallError("system_dependency_missing", "platform") from None
    evidence = object.__new__(ActivationEvidence)
    object.__setattr__(evidence, "backend", platform.sandbox_backend)
    object.__setattr__(evidence, "uid", account.pw_uid)
    marker = "test-private"
    if verification is not None:
        marker = verification.container_image or "seatbelt-local"
    object.__setattr__(evidence, "verification", marker)
    return evidence


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
        or selected_uid == 0 and account.pw_name != original_user
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
        '(version 1) (deny default) (deny network*) (allow process-exec '
        '(literal "/usr/bin/true"))'
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
            runtime
            if platform.sandbox_backend == "docker-rootless"
            else runtime / "podman"
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
            "--",
            image,
            "python",
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
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=45,
            check=False,
            env=child_environment,
        )
        if len(completed.stdout) > 4096 or len(completed.stderr) > 4096:
            raise OSError
        return completed
    except Exception:
        raise InstallError("system_dependency_missing", "platform") from None


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


def _validate_installer_artifact(
    evidence: InstallerArtifactEvidence,
    account: pwd.struct_passwd,
) -> None:
    """no-follow 验证 artifact 全路径、regular/execute 事实并重算 SHA-256。

    Args:
        evidence: strict lexical path 与 expected hash。
        account: 实际执行 artifact 的账号。

    Raises:
        InstallError: 任一 component 是 symlink、文件不可执行、超限或 hash 漂移。
    """
    if type(evidence) is not InstallerArtifactEvidence:
        raise InstallError("privilege_denied", "system_argvs")
    try:
        current = Path(evidence.path.anchor)
        for part in evidence.path.parts[1:]:
            current /= part
            fact = os.lstat(current)
            if stat.S_ISLNK(fact.st_mode):
                raise OSError
            if current != evidence.path and not stat.S_ISDIR(fact.st_mode):
                raise OSError
        final = os.lstat(evidence.path)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_size < 1
            or final.st_size > _MAX_INSTALLER_BYTES
            or not _mode_executable_by(final, account)
        ):
            raise OSError
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(evidence.path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != final.st_dev
                or opened.st_ino != final.st_ino
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise OSError
            digest = hashlib.sha256()
            count = 0
            while chunk := os.read(descriptor, 1_048_576):
                count += len(chunk)
                if count > _MAX_INSTALLER_BYTES:
                    raise OSError
                digest.update(chunk)
        finally:
            os.close(descriptor)
        if count != final.st_size or digest.hexdigest() != evidence.expected_sha256:
            raise OSError
    except Exception:
        raise InstallError("privilege_denied", "system_argvs") from None


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


def _canonical_rerun_argv(
    executable: Path,
    request: InstallRequest,
) -> tuple[str, ...]:
    """把完整 InstallRequest 规范化成唯一 system-prefix rerun argv。

    Args:
        executable: 已验证 installer artifact lexical path。
        request: strict immutable installer request。

    Returns:
        固定字段顺序、无默认语义漂移的 exact argv。

    Raises:
        InstallError: 请求不是 system-prefix 或 executable 不规范。
    """
    if (
        type(request) is not InstallRequest
        or not request.system_prefix
        or not _lexical_absolute(executable)
    ):
        raise InstallError("privilege_denied", "system_argvs")
    argv = [str(executable), request.action]
    if request.version is not None:
        argv.extend(("--version", request.version))
    argv.extend(("--channel", request.channel, "--state-home", str(request.state_home)))
    argv.append("--system-prefix")
    if not request.onboard:
        argv.append("--no-onboard")
    if request.config_file is not None:
        argv.extend(("--config", str(request.config_file)))
    if request.secrets_file is not None:
        argv.extend(("--secrets-file", str(request.secrets_file)))
    if request.service is True:
        argv.append("--install-service")
    elif request.service is False:
        argv.append("--no-service")
    if request.allow_system_packages:
        argv.append("--allow-system-packages")
    if request.dry_run:
        argv.append("--dry-run")
    if request.json_output:
        argv.append("--json")
    if request.verbose:
        argv.append("--verbose")
    if request.purge_data:
        argv.append("--purge-data")
    if request.confirm_data_loss:
        argv.append("--yes-i-understand-data-loss")
    return tuple(argv)


def _validate_rerun_argv(argv: object) -> None:
    """限制 public system-prefix action 为 canonical flag order。

    Args:
        argv: 不含 sudo wrapper 的 installer argv。

    Raises:
        InstallError: argv 含 shell、任意 flag 或字段顺序不规范。
    """
    if (
        type(argv) is not tuple
        or not 3 <= len(argv) <= 64
        or any(
            type(argument) is not str
            or not argument
            or len(argument) > 4096
            or not argument.isprintable()
            for argument in argv
        )
        or not _rerun_argv_allowed(cast(tuple[str, ...], argv))
    ):
        raise InstallError("privilege_denied", "system_argvs")


def _action(
    category: Literal["system-package", "linger", "system-prefix"],
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
        argv 是固定包命令、受限 setup、linger 或 system-prefix 动作时为 ``True``。
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
    if category == "system-prefix":
        return (
            len(argv) >= 5
            and argv[:2] == ("/usr/bin/sudo", "--")
            and _rerun_argv_allowed(argv[2:])
        )
    return False


def _rerun_argv_allowed(argv: tuple[str, ...]) -> bool:
    """判断 argv 是否是非 shell 的 verified installer system-prefix 重跑。

    Args:
        argv: 不含 sudo wrapper 的 installer argv。

    Returns:
        程序绝对、动作封闭且最后参数是 ``--system-prefix`` 时为 ``True``。
    """
    if (
        len(argv) < 7
        or not _lexical_absolute(Path(argv[0]))
        or Path(argv[0]).name in {"sh", "bash", "zsh", "fish", "env", "sudo"}
        or argv[1] not in {"install", "update", "uninstall"}
    ):
        return False
    index = 2
    if argv[index] == "--version":
        version = None if index + 1 >= len(argv) else argv[index + 1]
        if version is None or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
            return False
        index += 2
    if argv[index : index + 2] not in {
        ("--channel", "stable"),
        ("--channel", "dev"),
    }:
        return False
    index += 2
    if (
        argv[index] != "--state-home"
        or index + 1 >= len(argv)
        or not _lexical_absolute(Path(argv[index + 1]))
    ):
        return False
    index += 2
    if index >= len(argv) or argv[index] != "--system-prefix":
        return False
    index += 1
    pairs = {"--config", "--secrets-file"}
    singles = (
        "--no-onboard",
        "--install-service",
        "--no-service",
        "--allow-system-packages",
        "--dry-run",
        "--json",
        "--verbose",
        "--purge-data",
        "--yes-i-understand-data-loss",
    )
    ordered_flags = ("--no-onboard", "--config", "--secrets-file", *singles[1:])
    order = {name: position for position, name in enumerate(ordered_flags)}
    previous = -1
    service_seen = False
    while index < len(argv):
        flag = argv[index]
        position = order.get(flag)
        if position is None or position <= previous:
            return False
        if flag in {"--install-service", "--no-service"}:
            if service_seen:
                return False
            service_seen = True
        previous = position
        if flag in pairs:
            if index + 1 >= len(argv) or not _lexical_absolute(Path(argv[index + 1])):
                return False
            index += 2
        else:
            index += 1
    return True


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
    if category == "system-prefix":
        return _SYSTEM_PREFIX_REASON
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
