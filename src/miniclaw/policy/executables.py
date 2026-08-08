"""不执行登录 Shell 的确定性本机 CLI 发现。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_BASE_SYSTEM_ROOTS = (Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"), Path("/sbin"))
_DARWIN_SYSTEM_ROOTS = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
_USER_GLOB_ROOTS = (
    ".config/nvm/versions/node/*/bin",
    ".nvm/versions/node/*/bin",
    ".local/share/uv/tools/*/bin",
)
_USER_FIXED_ROOTS = (
    ".local/share/pnpm",
    "Library/pnpm",
    ".local/bin",
    ".cargo/bin",
    ".bun/bin",
)


@dataclass(frozen=True, slots=True)
class ExecutableEnvironment:
    """保存 Policy 与执行器共用的最小可执行搜索环境。"""

    search_roots: tuple[Path, ...]
    path_value: str
    home: Path | None


def discover_executables(
    profile: str,
    *,
    home: Path | None,
    explicit_roots: tuple[Path, ...],
    discover_user: bool,
    platform_name: str,
) -> ExecutableEnvironment:
    """组合系统、显式和已知用户安装器目录，不读取 Shell 启动文件。"""
    if profile not in {"workspace", "personal"}:
        raise ValueError("invalid permission profile")
    validated_explicit = tuple(_validate_explicit_root(root) for root in explicit_roots)
    system_candidates = (
        (*_BASE_SYSTEM_ROOTS, *_DARWIN_SYSTEM_ROOTS)
        if platform_name == "darwin"
        else _BASE_SYSTEM_ROOTS
    )
    roots: list[Path] = []
    for candidate in system_candidates:
        _append_system_root(roots, candidate)

    if profile == "workspace":
        return _environment(roots, None)

    for candidate in validated_explicit:
        _append_unique(roots, candidate)
    owner_home = _validate_home(home)
    if discover_user:
        for pattern in _USER_GLOB_ROOTS:
            for candidate in sorted(owner_home.glob(pattern), key=lambda path: path.as_posix()):
                _append_discovered_root(roots, candidate)
        for relative in _USER_FIXED_ROOTS:
            _append_discovered_root(roots, owner_home / relative)
    return _environment(roots, owner_home)


def _validate_home(home: Path | None) -> Path:
    """Personal Profile 必须使用存在、真实且非 symlink 的 Owner Home。"""
    if home is None:
        raise ValueError("personal profile requires an owner home")
    return _validate_real_directory(home, "owner home")


def _validate_explicit_root(root: Path) -> Path:
    """显式 executable root 必须是绝对、存在且非 symlink 的真实目录。"""
    if not root.is_absolute():
        raise ValueError("executable roots must be absolute")
    return _validate_real_directory(root, "executable root")


def _validate_real_directory(path: Path, label: str) -> Path:
    """解析真实目录，同时拒绝任意 symlink 组件。"""
    try:
        absolute = path.absolute()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"{label} must be an existing directory") from None
    if not resolved.is_dir() or resolved != absolute:
        raise ValueError(f"{label} must be a real directory")
    return resolved


def _append_system_root(roots: list[Path], candidate: Path) -> None:
    """加入存在的系统目录；系统兼容别名按规范路径去重。"""
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    if resolved.is_dir():
        _append_unique(roots, resolved)


def _append_discovered_root(roots: list[Path], candidate: Path) -> None:
    """仅加入无 symlink 组件的已知用户级真实目录。"""
    try:
        resolved = _validate_real_directory(candidate, "discovered executable root")
    except ValueError:
        return
    _append_unique(roots, resolved)


def _append_unique(roots: list[Path], candidate: Path) -> None:
    """按首次出现顺序去重目录。"""
    if candidate not in roots:
        roots.append(candidate)


def _environment(roots: list[Path], home: Path | None) -> ExecutableEnvironment:
    """冻结发现结果，并生成给 shutil/subprocess 共用的 PATH。"""
    frozen = tuple(roots)
    return ExecutableEnvironment(
        search_roots=frozen,
        path_value=os.pathsep.join(map(str, frozen)),
        home=home,
    )
