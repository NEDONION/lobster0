#!/usr/bin/env python3
"""构建 production-only、symlink-free、可复现的 pi-tui bundle。"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from miniclaw.tui_launcher import is_supported_node_version

if __package__:
    from scripts.build_node_bundle import NodeBundleError, write_deterministic_tar
else:
    from build_node_bundle import NodeBundleError, write_deterministic_tar

_PLATFORMS = {"linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64"}
_VERSION = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_NODE_VERSION = re.compile(rb"^v(\d+)\.(\d+)\.(\d+)\n$")
_PNPM = ("corepack", "pnpm")
_STRIP_NAMES = {".bin", ".cache", ".modules.yaml", ".pnpm", ".pnpm-store"}
_NODE_OUTPUT_LIMIT = 1024


class TuiBundleError(RuntimeError):
    """表示 TUI build、deploy、materialization 或 archive 失败。"""


def build_tui_bundle(
    project: Path,
    output_directory: Path,
    platform: str,
    version: str,
    managed_node: Path,
) -> Path:
    """运行 pnpm test/build/prod deploy 并生成 deterministic TUI bundle。

    Args:
        project: 包含 TUI-local workspace、package 与 frozen lock 的目录。
        output_directory: 新 artifact 的父目录。
        platform: Tier 1 `os-arch` key。
        version: 与 MiniClaw Release 相同的三段版本。
        managed_node: 已验证且位于受支持 LTS 区间的显式 Node executable。

    Returns:
        已生成的 `miniclaw-tui-<version>-<platform>.tar.gz`。

    Raises:
        TuiBundleError: 工具链、production tree、link 或输出不满足契约。
    """
    root = Path(project).resolve()
    if (
        platform not in _PLATFORMS
        or type(version) is not str
        or _VERSION.fullmatch(version) is None
        or not root.is_dir()
        or root.is_symlink()
    ):
        raise TuiBundleError("invalid TUI bundle inputs")
    for required in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"):
        candidate = root / required
        if not candidate.is_file() or candidate.is_symlink():
            raise TuiBundleError("TUI project is missing a regular release input")
    node = _resolve_managed_node(managed_node)

    _run((*_PNPM, "--dir", str(root), "test"))
    _run((*_PNPM, "--dir", str(root), "build"))
    licenses = _production_licenses(root)
    with tempfile.TemporaryDirectory(prefix="miniclaw-tui-bundle-") as temporary:
        staging = Path(temporary)
        deployed = staging / "deployed"
        _run(
            (
                *_PNPM,
                "--dir",
                str(root),
                "run",
                "release:deploy",
                "--",
                str(deployed),
            )
        )
        _validate_deploy(deployed)
        (deployed / "licenses.json").write_bytes(licenses)
        materialized = staging / "materialized/tui"
        materialize_tree(deployed, materialized)
        _promote_transitive_dependencies(materialized)
        _strip_deploy_metadata(materialized)
        _validate_materialized_tui(materialized)
        _smoke_materialized_tui(materialized, node, version)
        destination = Path(output_directory) / f"miniclaw-tui-{version}-{platform}.tar.gz"
        try:
            return write_deterministic_tar(materialized.parent, destination)
        except NodeBundleError as error:
            raise TuiBundleError(str(error)) from error


def materialize_tree(source: Path, destination: Path) -> None:
    """复制 deploy tree，并只把 staging 内 link 解引用为 regular 内容。

    Args:
        source: pnpm production deploy root。
        destination: 必须尚不存在且位于 source 外的输出 root。

    Raises:
        TuiBundleError: source/destination 非法，或发现逃逸、循环、损坏 link/special file。
    """
    source_root = Path(source)
    target_root = Path(destination)
    if (
        not source_root.is_dir()
        or source_root.is_symlink()
        or target_root.exists()
        or target_root.is_symlink()
    ):
        raise TuiBundleError("invalid materialization roots")
    try:
        canonical_root = source_root.resolve(strict=True)
        target_root.parent.mkdir(parents=True, exist_ok=True)
        _copy_materialized(source_root, target_root, canonical_root, frozenset())
    except TuiBundleError:
        shutil.rmtree(target_root, ignore_errors=True)
        raise
    except (OSError, RuntimeError) as error:
        shutil.rmtree(target_root, ignore_errors=True)
        raise TuiBundleError("unsafe symlink in production deploy") from error


def _copy_materialized(
    source: Path,
    destination: Path,
    root: Path,
    ancestors: frozenset[Path],
) -> None:
    """递归复制一个 entry，并通过 canonical ancestor 集合拒绝 link cycle。"""
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        linked = os.readlink(source)
        candidate = (source.parent / linked).resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise TuiBundleError("unsafe symlink escapes production deploy") from error
        if candidate in ancestors:
            raise TuiBundleError("unsafe symlink cycle in production deploy")
        _copy_materialized(candidate, destination, root, ancestors)
        return
    canonical = source.resolve(strict=True)
    try:
        canonical.relative_to(root)
    except ValueError as error:
        raise TuiBundleError("production entry escapes deploy root") from error
    if stat.S_ISDIR(metadata.st_mode):
        if canonical in ancestors:
            raise TuiBundleError("unsafe symlink cycle in production deploy")
        destination.mkdir(mode=0o755)
        next_ancestors = ancestors | {canonical}
        for child in sorted(source.iterdir(), key=lambda path: path.name):
            _copy_materialized(child, destination / child.name, root, next_ancestors)
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise TuiBundleError("production deploy contains a special file")
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o755 if metadata.st_mode & 0o111 else 0o644)


def _run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    """用无 shell、无 Node 注入变量的环境运行一个 bounded pnpm 命令。"""
    environment = dict(os.environ)
    environment.pop("NODE_OPTIONS", None)
    environment.pop("NODE_PATH", None)
    environment.update({"CI": "1", "NO_COLOR": "1"})
    try:
        completed = subprocess.run(
            argv,
            env=environment,
            capture_output=True,
            timeout=180,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TuiBundleError("pnpm release command failed") from error
    if completed.returncode != 0:
        raise TuiBundleError("pnpm release command failed")
    return completed


def _resolve_managed_node(candidate: Path) -> Path:
    """验证调用方显式提供的 regular Node 路径及其支持区间。

    Args:
        candidate: 不经 PATH 搜索的绝对 Node executable 路径。

    Returns:
        已验证的 canonical executable 路径。

    Raises:
        TuiBundleError: 路径、文件类型、权限、探测输出或版本区间不满足契约。
    """
    node = Path(candidate)
    if (
        not node.is_absolute()
        or not node.is_file()
        or node.is_symlink()
        or not os.access(node, os.X_OK)
    ):
        raise TuiBundleError("managed Node must be an explicit regular executable")
    resolved = node.resolve(strict=True)
    completed = _run_managed_node((str(resolved), "--version"), cwd=resolved.parent, environment={})
    matched = _NODE_VERSION.fullmatch(completed.stdout)
    if matched is None:
        raise TuiBundleError("managed Node version is invalid")
    parsed = tuple(int(part) for part in matched.groups())
    if not is_supported_node_version(parsed):
        raise TuiBundleError("managed Node version is not supported")
    return resolved


def _run_managed_node(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """在封闭环境和有界输出契约下运行 managed Node。

    Args:
        argv: 以已验证 Node executable 开头的显式参数数组。
        cwd: 不依赖 checkout 的工作目录。
        environment: 完整 child 环境；不会合并当前进程环境。

    Returns:
        输出大小、退出码和 stderr 均通过校验的进程结果。

    Raises:
        TuiBundleError: 启动、超时、非零退出或输出边界不满足契约。
    """
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TuiBundleError("managed Node command failed") from error
    if (
        completed.returncode != 0
        or completed.stderr
        or len(completed.stdout) > _NODE_OUTPUT_LIMIT
    ):
        raise TuiBundleError("managed Node command failed")
    return completed


def _smoke_materialized_tui(root: Path, node: Path, version: str) -> None:
    """用封闭 managed Node 环境验证 materialized 入口的精确 smoke JSON。

    Args:
        root: 已完成校验、去 link 和去 cache 的 TUI production tree。
        node: 已验证的显式 managed Node executable。
        version: artifact 预期的精确 TUI 版本。

    Raises:
        TuiBundleError: smoke 失败、输出非 compact 单 JSON 或字段/版本不匹配。
    """
    entry = root / "dist/main.js"
    completed = _run_managed_node(
        (str(node), str(entry), "--smoke"),
        cwd=root.parent,
        environment={
            "MINICLAW_NODE": str(node),
            "MINICLAW_TUI_ENTRY": str(entry),
        },
    )
    expected = {"component": "pi-tui", "version": version, "status": "ok"}
    canonical = f"{json.dumps(expected, separators=(',', ':'))}\n"
    try:
        text = completed.stdout.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TuiBundleError("materialized TUI smoke returned invalid JSON") from error
    if type(document) is not dict or document != expected or text != canonical:
        raise TuiBundleError("materialized TUI smoke contract mismatch")


def _production_licenses(project: Path) -> bytes:
    """捕获、去路径并 canonicalize pnpm production license inventory。"""
    completed = _run((*_PNPM, "--dir", str(project), "licenses", "list", "--prod", "--json"))
    try:
        document = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TuiBundleError("pnpm returned invalid license inventory") from error
    normalized = _drop_paths(document)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if (
        "@earendil-works/pi-tui" not in encoded
        or "typescript" in encoded
        or "@types/node" in encoded
    ):
        raise TuiBundleError("license inventory is not production-only")
    return f"{encoded}\n".encode()


def _drop_paths(value: object) -> object:
    """递归删除 pnpm inventory 中依赖 checkout 的 path/paths 字段。"""
    if type(value) is dict:
        return {
            key: _drop_paths(item)
            for key, item in sorted(value.items())
            if key not in {"path", "paths"}
        }
    if type(value) is list:
        return [_drop_paths(item) for item in value]
    return value


def _validate_deploy(deployed: Path) -> None:
    """要求 pnpm deploy 提供编译入口与真实 pi-tui production package。"""
    required = (
        deployed / "dist/main.js",
        deployed / "package.json",
        deployed / "node_modules/@earendil-works/pi-tui/package.json",
    )
    if (
        not deployed.is_dir()
        or deployed.is_symlink()
        or not all(path.is_file() for path in required)
    ):
        raise TuiBundleError("pnpm production deploy is incomplete")
    for forbidden in (
        deployed / "node_modules/typescript",
        deployed / "node_modules/@types/node",
        deployed / ".pnpm-store",
    ):
        if forbidden.exists() or forbidden.is_symlink():
            raise TuiBundleError("pnpm production deploy contains a dev dependency or cache")


def _promote_transitive_dependencies(root: Path) -> None:
    """把 pnpm virtual-store 的 prod 依赖复制到 Node 可解析的顶层 node_modules。"""
    modules = root / "node_modules"
    hoisted = modules / ".pnpm/node_modules"
    if not hoisted.is_dir() or hoisted.is_symlink():
        raise TuiBundleError("pnpm production deploy is missing transitive dependencies")
    for entry in sorted(hoisted.iterdir(), key=lambda path: path.name):
        if entry.name == ".bin":
            continue
        if entry.name.startswith("@") and entry.is_dir():
            for package in sorted(entry.iterdir(), key=lambda path: path.name):
                _copy_promoted_package(package, modules / entry.name / package.name)
        else:
            _copy_promoted_package(entry, modules / entry.name)


def _copy_promoted_package(source: Path, destination: Path) -> None:
    """复制一个已 materialize 的 prod package，拒绝覆盖 public dependency。"""
    if destination.exists() or destination.is_symlink():
        raise TuiBundleError("production dependency promotion collides")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        shutil.copy2(source, destination)
    else:
        raise TuiBundleError("production dependency promotion found a special entry")


def _strip_deploy_metadata(root: Path) -> None:
    """删除 materialize 后不参与运行的 pnpm metadata、cache 与编译辅助文件。"""
    candidates = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for candidate in candidates:
        if candidate.name in _STRIP_NAMES:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        elif candidate.is_file() and (
            candidate.name.endswith(".d.ts") or candidate.name.endswith(".js.map")
        ):
            candidate.unlink()


def _validate_materialized_tui(root: Path) -> None:
    """最终要求 tree 无 link/special/dev/cache 且 smoke 入口仍存在。"""
    required = (
        root / "dist/main.js",
        root / "licenses.json",
        root / "node_modules/@earendil-works/pi-tui/package.json",
    )
    if not all(path.is_file() and not path.is_symlink() for path in required):
        raise TuiBundleError("materialized TUI deploy is incomplete")
    for candidate in root.rglob("*"):
        metadata = candidate.lstat()
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise TuiBundleError("materialized TUI deploy contains a non-regular entry")
        if candidate.name in _STRIP_NAMES:
            raise TuiBundleError("materialized TUI deploy contains package-manager metadata")


def main() -> int:
    """解析 CLI 参数并构建一个平台 TUI bundle。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("tui"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--platform", choices=tuple(sorted(_PLATFORMS)), required=True)
    parser.add_argument("--version", default="0.7.0")
    parser.add_argument("--node", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        output = build_tui_bundle(
            arguments.project,
            arguments.output_dir,
            arguments.platform,
            arguments.version,
            arguments.node,
        )
    except TuiBundleError as error:
        parser.error(str(error))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
