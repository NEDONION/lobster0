"""提供安装器 Tier 1 平台检测与显式高权限动作计划。"""

import os
import platform as host_platform
import pwd
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

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
_ROOTLESS_TOOLS = {
    "/usr/bin/dockerd-rootless-setuptool.sh",
    "/usr/share/docker.io/contrib/dockerd-rootless-setuptool.sh",
}
_DEPENDENCY_FACTS = {
    "backend_ready",
    "linger_user",
    "podman_docker_compatible",
    "rootless_setup_tool",
    "rootless_setup_tool_executable",
    "rootless_setup_tool_regular",
    "system_packages_missing",
    "system_prefix",
    "target_user",
}


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


@dataclass(frozen=True, slots=True)
class PrivilegeAction:
    """保存一条需要单独展示和确认的高权限 exact argv。

    Args:
        category: 高权限动作的封闭类别。
        argv: 不经过 shell 的固定参数数组。
        requires_sudo: 动作是否调用固定 ``/usr/bin/sudo``。
        reason: 可安全展示的固定原因。
        approved: 是否经过调用方单独确认；默认永远为否。
    """

    category: Literal["system-package", "linger", "system-prefix"]
    argv: tuple[str, ...]
    requires_sudo: bool
    reason: str
    approved: bool = False


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
    effective_uid: int = 1000,
    original_user: str | None = None,
    original_uid: int | None = None,
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
    _validate_invoking_user(effective_uid, original_user, original_uid)
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
    effective_uid: int = 1000,
    original_user: str | None = None,
    original_uid: int | None = None,
) -> DetectedPlatform:
    """从注入事实纯解析 Tier 1 macOS 平台。

    Args:
        version: ``platform.mac_ver()`` 返回的系统版本。
        machine: ``platform.machine()`` 返回的架构。
        effective_uid: 当前进程有效 UID。
        original_user: sudo/root 调用时的原始非 root 用户名。
        original_uid: sudo/root 调用时的原始非 root UID。

    Returns:
        使用 launchd 与 Seatbelt 的规范平台事实。

    Raises:
        InstallError: 版本低于 13、架构不支持或 root 身份无法安全归属。
    """
    if type(version) is not str or _MACOS_VERSION.fullmatch(version) is None:
        raise InstallError("unsupported_platform", "distro_version")
    major = int(version.split(".", 1)[0])
    if major < 13:
        raise InstallError("unsupported_platform", "distro_version")
    arch = _normalize_arch(machine)
    _validate_invoking_user(effective_uid, original_user, original_uid)
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
        selected_user, selected_original_uid = _resolve_original_user()
    elif selected_uid == 0:
        _require_real_user(selected_user, selected_original_uid)
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
        )
    if selected_system == "Darwin":
        selected_version = host_platform.mac_ver()[0] if macos_version is None else macos_version
        return detect_macos(
            selected_version,
            selected_machine,
            effective_uid=selected_uid,
            original_user=selected_user,
            original_uid=selected_original_uid,
        )
    raise InstallError("unsupported_platform", "platform")


def build_dependency_actions(
    platform: DetectedPlatform,
    facts: Mapping[str, object],
) -> tuple[PrivilegeAction, ...]:
    """把封闭主机事实转换为默认未批准的固定依赖 argv。

    Args:
        platform: 已通过 Tier 1 校验的平台。
        facts: 仅含固定 bool、setup tool 候选、linger 用户与 system-prefix 标记的事实。

    Returns:
        可供 dry-run 精确展示的不可变权限动作集合。

    Raises:
        InstallError: facts 含未知键、错误类型、注入值或 backend 不能安全建立。
    """
    if type(platform) is not DetectedPlatform or type(facts) is not dict:
        raise InstallError("system_dependency_missing", "platform")
    if any(type(key) is not str or key not in _DEPENDENCY_FACTS for key in facts):
        raise InstallError("system_dependency_missing", "system_argvs")
    packages_missing = _bool_fact(facts, "system_packages_missing", False)
    backend_ready = _bool_fact(facts, "backend_ready", platform.os == "macos")
    system_prefix = _bool_fact(facts, "system_prefix", False)
    podman_compatible = _bool_fact(
        facts,
        "podman_docker_compatible",
        platform.distro_id not in _RHEL_FAMILY,
    )
    linger_user = facts.get("linger_user")
    target_user = facts.get("target_user")
    tool = facts.get("rootless_setup_tool")
    tool_regular = _bool_fact(facts, "rootless_setup_tool_regular", False)
    tool_executable = _bool_fact(facts, "rootless_setup_tool_executable", False)
    if linger_user is not None and not _valid_user(linger_user):
        raise InstallError("system_dependency_missing", "service")
    if target_user is not None and not _valid_user(target_user):
        raise InstallError("system_dependency_missing", "system_argvs")
    if tool is not None and (
        type(tool) is not str
        or tool not in _ROOTLESS_TOOLS
        or not tool_regular
        or not tool_executable
        or platform.distro_id not in _DEBIAN_FAMILY
    ):
        raise InstallError("system_dependency_missing", "system_argvs")
    if tool is None and (tool_regular or tool_executable):
        raise InstallError("system_dependency_missing", "system_argvs")
    if platform.distro_id not in _RHEL_FAMILY and "podman_docker_compatible" in facts:
        raise InstallError("system_dependency_missing", "platform")
    if not backend_ready and not packages_missing:
        raise InstallError("system_dependency_missing", "platform")
    if platform.distro_id in _RHEL_FAMILY and not podman_compatible and not packages_missing:
        raise InstallError("system_dependency_missing", "platform")

    actions: list[PrivilegeAction] = []
    if packages_missing and platform.distro_id in _DEBIAN_FAMILY:
        actions.extend(
            (
                _action(
                    "system-package",
                    ("/usr/bin/sudo", "/usr/bin/apt-get", "update"),
                    "install fixed Debian rootless dependencies",
                ),
                _action(
                    "system-package",
                    (
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
                    ),
                    "install fixed Debian rootless dependencies",
                ),
            )
        )
    elif packages_missing and platform.distro_id in _RHEL_FAMILY:
        actions.append(
            _action(
                "system-package",
                (
                    "/usr/bin/sudo",
                    "/usr/bin/dnf",
                    "install",
                    "-y",
                    "podman-docker",
                    "slirp4netns",
                    "fuse-overlayfs",
                    "shadow-utils",
                    "dbus-daemon",
                ),
                "install fixed RHEL rootless dependencies",
            )
        )
    elif packages_missing:
        raise InstallError("system_dependency_missing", "platform")
    if tool is not None:
        argv = (tool, "install")
        if target_user is not None:
            argv = ("/usr/bin/sudo", "-u", target_user, "--", *argv)
        actions.append(
            PrivilegeAction(
                category="system-package",
                argv=argv,
                requires_sudo=target_user is not None,
                reason="configure rootless Docker for target user",
            )
        )
    if linger_user is not None:
        if platform.os != "linux":
            raise InstallError("system_dependency_missing", "service")
        actions.append(
            _action(
                "linger",
                ("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger", linger_user),
                "enable confirmed headless user service",
            )
        )
    if system_prefix:
        actions.append(
            _action(
                "system-prefix",
                (
                    "/usr/bin/sudo",
                    "/usr/bin/install",
                    "-d",
                    "-m",
                    "0755",
                    "/usr/local/lib/miniclaw",
                ),
                "create explicit system program prefix",
            )
        )
    return tuple(actions)


def _parse_os_release(text: str) -> dict[str, str]:
    """纯解析有限 os-release 键值，不解释任何 shell 语法。

    Args:
        text: 有界 UTF-8 os-release 文本。

    Returns:
        只含 ``ID`` 与 ``VERSION_ID`` 的静态字符串映射。

    Raises:
        InstallError: 文本超限、目标键重复或目标值含非静态字符。
    """
    if type(text) is not str or not 1 <= len(text.encode("utf-8")) <= 65_536:
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


def _valid_user(value: object) -> bool:
    """判断用户名是否可安全作为 exact argv 单参数。

    Args:
        value: 待校验用户名。

    Returns:
        值为规范非 root POSIX 用户名时为 ``True``。
    """
    return type(value) is str and _USER_NAME.fullmatch(value) is not None and value != "root"


def _resolve_original_user() -> tuple[str, int]:
    """从 sudo 固定环境解析并验证真实非 root 原用户。

    Returns:
        与系统账号数据库一致的用户名和正 UID。

    Raises:
        InstallError: 环境缺失、类型不规范、用户不存在或 UID 不匹配。
    """
    user = os.environ.get("SUDO_USER")
    uid_text = os.environ.get("SUDO_UID")
    if (
        not _valid_user(user)
        or uid_text is None
        or not uid_text.isascii()
        or not uid_text.isdecimal()
    ):
        raise InstallError("privilege_denied", "platform")
    uid = int(uid_text)
    _require_real_user(user, uid)
    return cast(str, user), uid


def _require_real_user(user: object, uid: object) -> None:
    """验证显式 root handoff 用户存在且 UID 精确匹配。

    Args:
        user: 待验证用户名。
        uid: 待验证 UID。

    Raises:
        InstallError: 用户事实不规范、不存在、是 root 或 UID 不匹配。
    """
    _validate_invoking_user(0, user, uid)
    try:
        account = pwd.getpwnam(cast(str, user))
    except KeyError as error:
        raise InstallError("privilege_denied", "platform") from error
    if account.pw_uid != uid or account.pw_uid == 0:
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
    except UnicodeDecodeError as error:
        raise InstallError("unsupported_platform", "platform") from error


def _bool_fact(facts: Mapping[str, object], name: str, default: bool) -> bool:
    """读取 strict bool fact，拒绝 int 冒充。

    Args:
        facts: 封闭系统事实映射。
        name: 要读取的固定字段名。
        default: 字段缺失时使用的安全默认值。

    Returns:
        精确 bool 值。

    Raises:
        InstallError: 值不是 exact bool。
    """
    value = facts.get(name, default)
    if type(value) is not bool:
        raise InstallError("system_dependency_missing", "system_argvs")
    return value


def _action(
    category: Literal["system-package", "linger", "system-prefix"],
    argv: tuple[str, ...],
    reason: str,
) -> PrivilegeAction:
    """构造默认未批准且显式使用 sudo 的固定动作。

    Args:
        category: 固定权限动作类别。
        argv: 已在调用点封闭定义的 exact argv。
        reason: 可安全展示的固定原因。

    Returns:
        ``approved=False`` 且 ``requires_sudo=True`` 的不可变动作。
    """
    return PrivilegeAction(
        category=category,
        argv=argv,
        requires_sudo=True,
        reason=reason,
    )
