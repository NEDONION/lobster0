#!/usr/bin/env python3
"""构建 production-only、symlink-free、可复现的 pi-tui bundle。"""

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

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
_COMMAND_TIMEOUT_SECONDS = 180.0
_COMMAND_STREAM_OUTPUT_LIMIT = 64 * 1024
_COMMAND_COMBINED_OUTPUT_LIMIT = 64 * 1024
_LICENSE_OUTPUT_LIMIT = 1024 * 1024
_NODE_TIMEOUT_SECONDS = 10.0
_NODE_OUTPUT_LIMIT = 1024
_PROCESS_GROUP_TERM_SECONDS = 0.1
_PROCESS_GROUP_CLEANUP_SECONDS = 0.5


class TuiBundleError(RuntimeError):
    """表示 TUI build、deploy、materialization 或 archive 失败。"""


class _BoundedProcessFailure(RuntimeError):
    """表示 child 超时、输出超限或进程组无法有界回收。"""


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


def _run(
    argv: tuple[str, ...],
    *,
    stdout_limit: int | None = None,
    combined_limit: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """用无 shell、无 Node 注入变量的环境运行一个 bounded pnpm 命令。"""
    environment = dict(os.environ)
    environment.pop("NODE_OPTIONS", None)
    environment.pop("NODE_PATH", None)
    environment.update({"CI": "1", "NO_COLOR": "1"})
    try:
        completed = _run_bounded_process(
            argv,
            cwd=None,
            environment=environment,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            stdout_limit=(
                _COMMAND_STREAM_OUTPUT_LIMIT if stdout_limit is None else stdout_limit
            ),
            stderr_limit=_COMMAND_STREAM_OUTPUT_LIMIT,
            combined_limit=(
                _COMMAND_COMBINED_OUTPUT_LIMIT if combined_limit is None else combined_limit
            ),
        )
    except _BoundedProcessFailure:
        raise TuiBundleError("pnpm release command failed") from None
    if completed.returncode != 0:
        raise TuiBundleError("pnpm release command failed")
    return completed


def _run_bounded_process(
    argv: tuple[str, ...],
    *,
    cwd: Path | None,
    environment: dict[str, str],
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    combined_limit: int,
) -> subprocess.CompletedProcess[bytes]:
    """用单一 deadline、nonblocking 双流和独立进程组执行 child。

    Args:
        argv: 不经过 shell 的显式参数数组。
        cwd: 显式工作目录；None 表示继承当前目录。
        environment: 完整 child 环境，不会隐式合并进程环境。
        timeout: 包含启动、双流读取和 direct child 等待的总秒数。
        stdout_limit: stdout 最大保留字节数。
        stderr_limit: stderr 最大保留字节数。
        combined_limit: stdout 与 stderr 合计最大字节数。

    Returns:
        已 bounded reap 的 direct child 结果。

    Raises:
        _BoundedProcessFailure: 输入边界、启动、超时、输出超限或回收失败。
    """
    if (
        not argv
        or timeout <= 0
        or stdout_limit <= 0
        or stderr_limit <= 0
        or combined_limit <= 0
    ):
        raise _BoundedProcessFailure
    deadline = time.monotonic() + timeout
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    streams: tuple[BinaryIO, ...] = ()
    stdout = bytearray()
    stderr = bytearray()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            raise _BoundedProcessFailure
        streams = (process.stdout, process.stderr)
        for index, stream in enumerate(streams):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, index)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BoundedProcessFailure
            events = selector.select(remaining)
            if not events:
                raise _BoundedProcessFailure
            for key, _mask in events:
                target = stdout if key.data == 0 else stderr
                stream_limit = stdout_limit if key.data == 0 else stderr_limit
                if not _read_ready_stream(
                    key.fileobj,
                    target,
                    stdout,
                    stderr,
                    stream_limit,
                    combined_limit,
                    deadline,
                ):
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _BoundedProcessFailure
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise _BoundedProcessFailure from None
        return subprocess.CompletedProcess(argv, returncode, bytes(stdout), bytes(stderr))
    except (OSError, ValueError, _BoundedProcessFailure):
        if process is not None:
            try:
                _terminate_process_group(process)
            except _BoundedProcessFailure:
                pass
        raise _BoundedProcessFailure from None
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()


def _read_ready_stream(
    stream: BinaryIO,
    target: bytearray,
    stdout: bytearray,
    stderr: bytearray,
    stream_limit: int,
    combined_limit: int,
    deadline: float,
) -> bool:
    """增量读取一个 ready pipe，超过单流或合计上限时立即失败。

    Args:
        stream: selectors 返回的 pipe file object。
        target: 当前 stdout 或 stderr buffer。
        stdout: stdout buffer，用于合计预算。
        stderr: stderr buffer，用于合计预算。
        stream_limit: 当前 stream 的独立上限。
        combined_limit: 两个 stream 的合计上限。
        deadline: 与启动、selector 和 wait 共享的 monotonic deadline。

    Returns:
        pipe 仍打开时为 True，EOF 时为 False。

    Raises:
        _BoundedProcessFailure: deadline 或任一输出预算耗尽。
    """
    descriptor = stream.fileno()
    while True:
        if time.monotonic() >= deadline:
            raise _BoundedProcessFailure
        stream_remaining = stream_limit - len(target)
        combined_remaining = combined_limit - len(stdout) - len(stderr)
        read_size = min(8192, stream_remaining + 1, combined_remaining + 1)
        try:
            chunk = os.read(descriptor, max(1, read_size))
        except BlockingIOError:
            return True
        if not chunk:
            return False
        if len(chunk) > stream_remaining or len(chunk) > combined_remaining:
            raise _BoundedProcessFailure
        target.extend(chunk)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """TERM→grace→KILL 独立进程组，并在 bounded deadline 内 reap leader。

    Args:
        process: 由当前 collector 以 start_new_session 启动的 direct child。

    Raises:
        _BoundedProcessFailure: direct child 无法在 cleanup deadline 内回收。
    """
    cleanup_deadline = time.monotonic() + _PROCESS_GROUP_CLEANUP_SECONDS
    group_exists = True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        group_exists = False
    except OSError:
        group_exists = process.returncode is None
    if group_exists:
        grace_deadline = min(
            cleanup_deadline,
            time.monotonic() + _PROCESS_GROUP_TERM_SECONDS,
        )
        while time.monotonic() < grace_deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                group_exists = False
                break
            except OSError:
                break
            time.sleep(min(0.01, max(0.0, grace_deadline - time.monotonic())))
    if group_exists:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.returncode is None:
                try:
                    process.kill()
                except OSError:
                    pass
    if process.returncode is None:
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 0:
            raise _BoundedProcessFailure
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise _BoundedProcessFailure from None


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
        completed = _run_bounded_process(
            argv,
            cwd=cwd,
            environment=dict(environment),
            timeout=_NODE_TIMEOUT_SECONDS,
            stdout_limit=_NODE_OUTPUT_LIMIT,
            stderr_limit=_NODE_OUTPUT_LIMIT,
            combined_limit=_NODE_OUTPUT_LIMIT,
        )
    except _BoundedProcessFailure:
        raise TuiBundleError("managed Node command failed") from None
    if completed.returncode != 0 or completed.stderr:
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
    completed = _run(
        (*_PNPM, "--dir", str(project), "licenses", "list", "--prod", "--json"),
        stdout_limit=_LICENSE_OUTPUT_LIMIT,
        combined_limit=_LICENSE_OUTPUT_LIMIT,
    )
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
