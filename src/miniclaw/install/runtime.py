"""构建 hash-locked immutable Runtime，并原子切换 stable launcher target。"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import os
import re
import signal
import stat
import subprocess
import threading
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Never, Protocol

from miniclaw.install.layout import InstallLayout, _program_mode
from miniclaw.install.models import Artifact, InstallError, PlatformKey, ReleaseManifest
from miniclaw.install.receipt import InstallReceipt, _rename_no_replace

_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_RECEIPT_KEYS = {
    "git_commit",
    "installer_sha256",
    "node_sha256",
    "node_version",
    "python_version",
    "requirements_sha256",
    "runtime_relative",
    "tui_sha256",
    "tui_version",
    "version",
    "wheel_sha256",
}
_MAX_RECEIPT_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_TREE_ENTRIES = 20_000
_MAX_TREE_BYTES = 1_073_741_824
_COMMAND_TIMEOUT_SECONDS = 300.0
_SMOKE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    """保存一次 bounded subprocess 的脱敏结果。

    Args:
        returncode: 子进程退出码。
        stdout: 不超过 64 KiB 的标准输出。
        stderr: 不超过 64 KiB 的标准错误。

    Raises:
        InstallError: 类型或输出预算无效。
    """

    returncode: int
    stdout: bytes
    stderr: bytes

    def __post_init__(self) -> None:
        """拒绝 bool exit code、非 bytes 或超预算输出。"""
        if (
            type(self.returncode) is not int
            or type(self.stdout) is not bytes
            or type(self.stderr) is not bytes
            or len(self.stdout) > _MAX_OUTPUT_BYTES
            or len(self.stderr) > _MAX_OUTPUT_BYTES
        ):
            _runtime_failed()


class CommandRunner(Protocol):
    """描述 RuntimeBuilder 需要的 exact argv runner。"""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        """运行不经过 shell 的命令，并返回 bounded bytes。"""


@dataclass(frozen=True, slots=True)
class RuntimeInputs:
    """绑定一次 Runtime 构建的 Release、平台和 verified artifact paths。

    Args:
        layout: Task 7 已校验且版本化的安装布局。
        manifest: Task 4 已 strict 校验的 Release manifest。
        platform: 当前 concrete Tier 1 平台。
        wheel: 已下载校验的 universal wheel。
        requirements: 已下载校验的 hash-required lock。
        node: Task 6 safe extraction 产生的 `node` 目录。
        tui: Task 6 safe extraction 产生的 `tui` 目录。
        installer: 已下载校验的 installer zipapp。
        uv: bootstrap 已校验的 managed uv executable。

    Raises:
        InstallError: 类型、版本、平台或 lexical path 不闭合。
    """

    layout: InstallLayout
    manifest: ReleaseManifest
    platform: PlatformKey
    wheel: Path
    requirements: Path
    node: Path
    tui: Path
    installer: Path
    uv: Path

    def __post_init__(self) -> None:
        """在任何文件访问前校验 immutable inputs 的结构关系。"""
        if (
            type(self.layout) is not InstallLayout
            or type(self.manifest) is not ReleaseManifest
            or type(self.platform) is not PlatformKey
            or self.platform.os not in {"linux", "macos"}
            or self.platform.arch not in {"x86_64", "arm64"}
            or self.layout.runtime.name != self.manifest.version
        ):
            _runtime_failed()
        for value in (
            self.wheel,
            self.requirements,
            self.node,
            self.tui,
            self.installer,
            self.uv,
        ):
            if not _safe_absolute_path(value):
                _runtime_failed()


@dataclass(frozen=True, slots=True)
class RuntimeReceipt:
    """记录一个尚未激活 Runtime 的可验证构建结果。

    Args:
        version: MiniClaw Release SemVer。
        git_commit: Release 绑定的 40-hex commit。
        runtime_relative: 固定 `runtimes/<version>` 相对路径。
        python_version: 固定 Python `3.12` 系列标识。
        node_version: manifest 默认 Node 三段版本。
        tui_version: 与 Release 一致的 TUI SemVer。
        wheel_sha256: verified wheel hash。
        requirements_sha256: verified requirements hash。
        node_sha256: verified Node archive hash。
        tui_sha256: verified TUI archive hash。
        installer_sha256: verified installer zipapp hash。

    Raises:
        InstallError: 任一类型、版本、路径或 hash 不闭合。
    """

    version: str
    git_commit: str
    runtime_relative: str
    python_version: str
    node_version: str
    tui_version: str
    wheel_sha256: str
    requirements_sha256: str
    node_sha256: str
    tui_sha256: str
    installer_sha256: str

    def __post_init__(self) -> None:
        """校验 receipt exact schema 与 cross-field bindings。"""
        hashes = (
            self.wheel_sha256,
            self.requirements_sha256,
            self.node_sha256,
            self.tui_sha256,
            self.installer_sha256,
        )
        if (
            type(self.version) is not str
            or _SEMVER.fullmatch(self.version) is None
            or type(self.git_commit) is not str
            or _COMMIT.fullmatch(self.git_commit) is None
            or self.runtime_relative != f"runtimes/{self.version}"
            or self.python_version != "3.12"
            or type(self.node_version) is not str
            or _SEMVER.fullmatch(self.node_version) is None
            or type(self.tui_version) is not str
            or self.tui_version != self.version
            or any(type(value) is not str or _HASH.fullmatch(value) is None for value in hashes)
        ):
            _runtime_failed()

    def to_bytes(self) -> bytes:
        """返回 deterministic exact-key owner-only receipt JSON。"""
        return (
            json.dumps(
                {name: getattr(self, name) for name in sorted(_RECEIPT_KEYS)},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode()

    @classmethod
    def load(cls, path: Path, *, expected_uid: int | None = None) -> RuntimeReceipt:
        """no-follow 读取 owner-only Runtime receipt。

        Args:
            path: runtime 内 `install-receipt.json` 的 absolute path。
            expected_uid: 期望 owner；默认当前 euid。

        Returns:
            exact-key 且字段关系有效的 RuntimeReceipt。

        Raises:
            InstallError: 文件 type/owner/mode/size、JSON 或字段无效。
        """
        uid = os.geteuid() if expected_uid is None else expected_uid
        payload = _read_private_regular(path, uid, _MAX_RECEIPT_BYTES)
        try:
            document = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            _runtime_failed()
        if type(document) is not dict or set(document) != _RECEIPT_KEYS:
            _runtime_failed()
        try:
            return cls(**document)
        except (InstallError, TypeError):
            _runtime_failed()


@dataclass(frozen=True, slots=True)
class _FileToken:
    """绑定 verified regular file 的 path、metadata snapshot 与 hash。"""

    path: Path
    snapshot: tuple[int, ...]
    sha256: str
    mode: int


class _SubprocessRunner:
    """用 bounded drain、timeout 和 isolated process group 执行 argv。"""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        """不经过 shell 执行命令，并丢弃超过 64 KiB 的输出。"""
        if not _valid_argv(argv) or not _valid_environment(env) or timeout <= 0:
            _runtime_failed()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            _runtime_failed()
        assert process.stdout is not None and process.stderr is not None
        stdout: list[bytes] = []
        stderr: list[bytes] = []
        overflow = [False, False]
        threads = (
            threading.Thread(
                target=_drain_bounded,
                args=(process.stdout, stdout, overflow, 0),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_bounded,
                args=(process.stderr, stderr, overflow, 1),
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            process.wait()
            returncode = -signal.SIGKILL
            overflow[0] = True
        finally:
            for thread in threads:
                thread.join()
            process.stdout.close()
            process.stderr.close()
        if any(overflow):
            returncode = returncode or 125
        return CommandResult(returncode, b"".join(stdout), b"".join(stderr))


class RuntimeBuilder:
    """构建、smoke、持久化并发布一个 immutable managed Runtime。"""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        """使用 production bounded runner，或注入离线 exact fake。"""
        self._runner = _SubprocessRunner() if runner is None else runner
        if not callable(getattr(self._runner, "run", None)):
            _runtime_failed()

    def build(self, inputs: RuntimeInputs) -> RuntimeReceipt:
        """按固定 uv argv 构建、smoke 并 no-replace 发布 Runtime。

        Args:
            inputs: strict manifest/layout 与 verified artifact paths。

        Returns:
            已写入最终 immutable Runtime 的 RuntimeReceipt。

        Raises:
            InstallError: 输入漂移、metadata、命令、smoke、权限或发布失败。
        """
        staging_identity: tuple[int, int] | None = None
        published = False
        try:
            artifacts, tokens = _preflight(inputs)
            _prepare_runtime_parent(inputs.layout)
            if _lexists(inputs.layout.staging) or _lexists(inputs.layout.runtime):
                _runtime_failed()
            os.mkdir(inputs.layout.staging, 0o700)
            staging_metadata = inputs.layout.staging.lstat()
            staging_identity = (staging_metadata.st_dev, staging_metadata.st_ino)
            env = _runtime_environment(inputs.layout)
            for private in (env["HOME"], env["TMPDIR"], env["UV_CACHE_DIR"]):
                os.mkdir(private, 0o700)
            _copy_verified_tree(inputs.node, inputs.layout.staging / "node", {"bin/node"})
            _copy_verified_tree(inputs.tui, inputs.layout.staging / "tui", set())
            _copy_verified_file(
                tokens["installer"],
                inputs.layout.staging / "miniclaw-installer.pyz",
                0o700,
            )
            python = inputs.layout.staging / "venv" / "bin" / "python"
            self._checked(
                (
                    str(inputs.uv),
                    "venv",
                    "--python",
                    inputs.manifest.python,
                    str(inputs.layout.staging / "venv"),
                ),
                env,
                _COMMAND_TIMEOUT_SECONDS,
                (tokens["uv"],),
            )
            self._checked(
                (
                    str(inputs.uv),
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--require-hashes",
                    "-r",
                    str(inputs.requirements),
                ),
                env,
                _COMMAND_TIMEOUT_SECONDS,
                (tokens["uv"], tokens["requirements"]),
            )
            self._checked(
                (
                    str(inputs.uv),
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    str(inputs.wheel),
                ),
                env,
                _COMMAND_TIMEOUT_SECONDS,
                (tokens["uv"], tokens["wheel"]),
            )
            self.smoke(inputs)
            for private in (Path(env["HOME"]), Path(env["TMPDIR"]), Path(env["UV_CACHE_DIR"])):
                _remove_owned_tree(private)
            receipt = _receipt_for(inputs, artifacts)
            _write_exclusive(
                inputs.layout.staging / "release-manifest.json",
                _manifest_bytes(inputs.manifest),
                0o600,
            )
            _write_exclusive(
                inputs.layout.staging / "install-receipt.json",
                receipt.to_bytes(),
                0o600,
            )
            _harden_and_fsync_tree(inputs.layout.staging)
            _rename_no_replace(inputs.layout.staging, inputs.layout.runtime)
            published = True
            moved = inputs.layout.runtime.lstat()
            if (moved.st_dev, moved.st_ino) != staging_identity:
                _runtime_failed()
            _fsync_directory(inputs.layout.runtimes_dir)
            return receipt
        except InstallError:
            if published and staging_identity is not None:
                _quarantine_and_remove(inputs.layout.runtime, staging_identity)
            elif staging_identity is not None:
                _quarantine_and_remove(inputs.layout.staging, staging_identity)
            raise
        except BaseException as error:
            if published and staging_identity is not None:
                _quarantine_and_remove(inputs.layout.runtime, staging_identity)
            elif staging_identity is not None:
                _quarantine_and_remove(inputs.layout.staging, staging_identity)
            raise InstallError("runtime_install_failed", "manifest") from error

    def smoke(self, inputs: RuntimeInputs) -> None:
        """执行 Python version、install-smoke 与 checkout-independent TUI smoke。

        Args:
            inputs: 当前 staging 所属 strict Runtime inputs。

        Raises:
            InstallError: executable、输出、版本、Channel import 或 TUI smoke 失败。
        """
        if type(inputs) is not RuntimeInputs:
            _runtime_failed()
        python = inputs.layout.staging / "venv" / "bin" / "python"
        node = inputs.layout.staging / "node" / "bin" / "node"
        tui = inputs.layout.staging / "tui" / "dist" / "main.js"
        _verify_executable(python)
        _verify_executable(node)
        _verify_private_file(tui, expected_mode=0o600)
        env = _runtime_environment(inputs.layout)
        version = self._checked(
            (str(python), "-I", "-m", "miniclaw", "--version"),
            env,
            _SMOKE_TIMEOUT_SECONDS,
        )
        if version.stdout != f"miniclaw {inputs.manifest.version}\n".encode():
            _runtime_failed()
        install = self._checked(
            (str(python), "-I", "-m", "miniclaw", "install-smoke", "--json"),
            env,
            _SMOKE_TIMEOUT_SECONDS,
        )
        document = _json_object(install.stdout)
        if document.get("status") != "ok" or document.get("version") != inputs.manifest.version:
            _runtime_failed()
        tui_result = self._checked(
            (str(node), str(tui), "--smoke"),
            env,
            _SMOKE_TIMEOUT_SECONDS,
        )
        if _json_object(tui_result.stdout) != {
            "component": "pi-tui",
            "status": "ok",
            "version": inputs.manifest.version,
        }:
            _runtime_failed()

    def install_and_activate(self, inputs: RuntimeInputs) -> RuntimeReceipt:
        """仅在完整 build/smoke 成功后原子切换 current。

        Args:
            inputs: strict Runtime inputs。

        Returns:
            已激活 Runtime 的构建 receipt。

        Raises:
            InstallError: build 或 activation 失败。
        """
        receipt = self.build(inputs)
        activate_runtime(inputs.layout, receipt)
        return receipt

    def _checked(
        self,
        argv: tuple[str, ...],
        env: dict[str, str],
        timeout: float,
        tokens: tuple[_FileToken, ...] = (),
    ) -> CommandResult:
        """在命令前后重验 pathname tokens，并归一化 runner failures。"""
        for token in tokens:
            _revalidate_token(token)
        try:
            result = self._runner.run(argv, env=dict(env), timeout=timeout)
        except BaseException as error:
            raise InstallError("runtime_install_failed", "manifest") from error
        if type(result) is not CommandResult or result.returncode != 0:
            _runtime_failed()
        for token in tokens:
            _revalidate_token(token)
        return result


def activate_runtime(layout: InstallLayout, receipt: RuntimeReceipt) -> None:
    """用 relative `current.next` symlink 和 replace 原子激活 Runtime。

    Args:
        layout: 与 receipt version 绑定的 validated layout。
        receipt: build 已写入最终 Runtime 的 receipt。

    Raises:
        InstallError: target/current/current.next 不受管或 namespace 持久化失败。
    """
    next_link = layout.current.with_name("current.next") if type(layout) is InstallLayout else None
    next_identity: tuple[int, int] | None = None
    try:
        if (
            type(layout) is not InstallLayout
            or type(receipt) is not RuntimeReceipt
            or receipt.version != layout.runtime.name
            or receipt.runtime_relative != f"runtimes/{layout.runtime.name}"
        ):
            _activation_failed()
        _verify_runtime_directory(layout.runtime, receipt)
        assert next_link is not None
        if _lexists(next_link):
            _activation_failed()
        current_token = _validated_current(layout)
        target = receipt.runtime_relative
        os.symlink(target, next_link)
        metadata = next_link.lstat()
        next_identity = (metadata.st_dev, metadata.st_ino)
        if metadata.st_uid != os.geteuid() or not stat.S_ISLNK(metadata.st_mode):
            _activation_failed()
        _fsync_directory(layout.program_prefix)
        if current_token is None:
            if _lexists(layout.current):
                _activation_failed()
        else:
            _revalidate_symlink(layout.current, current_token)
        os.replace(next_link, layout.current)
        next_identity = None
        if _validated_current(layout) != target:
            _activation_failed()
        _fsync_directory(layout.program_prefix)
    except InstallError as error:
        if next_link is not None and next_identity is not None:
            _unlink_same_inode(next_link, next_identity)
        if error.code == "activation_failed":
            raise
        raise InstallError("activation_failed", "manifest") from error
    except BaseException as error:
        if next_link is not None and next_identity is not None:
            _unlink_same_inode(next_link, next_identity)
        raise InstallError("activation_failed", "manifest") from error


def retain_current_and_previous(layout: InstallLayout) -> tuple[Path, ...]:
    """删除 receipt 可验证但未被 current/previous 引用的旧 Runtime。

    Args:
        layout: 当前目标版本的 validated install layout。

    Returns:
        实际保留的 current/N-1 逻辑相对路径，按版本排序。

    Raises:
        InstallError: layout、current、root receipt 或受保护 Runtime 不可信。
    """
    try:
        if type(layout) is not InstallLayout:
            _runtime_failed()
        _verify_directory(layout.runtimes_dir, expected_mode=_program_mode(layout))
        current = _validated_current(layout)
        protected: set[str] = set()
        if current is not None:
            protected.add(current)
        if _lexists(layout.receipt):
            install_receipt = InstallReceipt.load(layout.receipt)
            protected.add(install_receipt.current_runtime)
            if install_receipt.previous_runtime is not None:
                protected.add(install_receipt.previous_runtime)
            if current is not None and current != install_receipt.current_runtime:
                _runtime_failed()
        managed: dict[str, tuple[Path, RuntimeReceipt]] = {}
        for entry in os.scandir(layout.runtimes_dir):
            path = Path(entry.path)
            metadata = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            try:
                receipt = RuntimeReceipt.load(path / "install-receipt.json")
                if receipt.runtime_relative != f"runtimes/{entry.name}":
                    continue
                _verify_runtime_directory(path, receipt)
            except InstallError:
                continue
            managed[receipt.runtime_relative] = (path, receipt)
        if any(value not in managed for value in protected):
            _runtime_failed()
        if len(protected) < 2:
            remaining = sorted(
                (value for value in managed if value not in protected),
                key=lambda value: _semver_key(PurePosixPath(value).name),
                reverse=True,
            )
            protected.update(remaining[: 2 - len(protected)])
        for relative, (path, _receipt) in managed.items():
            if relative not in protected:
                _quarantine_and_remove(path, _directory_identity(path))
        ordered = sorted(protected, key=lambda value: _semver_key(Path(value).name))
        return tuple(Path(value) for value in ordered)
    except InstallError:
        raise
    except BaseException as error:
        raise InstallError("runtime_install_failed", "manifest") from error


def _preflight(
    inputs: RuntimeInputs,
) -> tuple[dict[str, Artifact], dict[str, _FileToken]]:
    """在任何写入前绑定 manifest artifacts、filenames、hashes 与 source tokens。"""
    if type(inputs) is not RuntimeInputs:
        _runtime_failed()
    artifacts = {
        kind: inputs.manifest.require_artifact(kind, inputs.platform)
        for kind in ("wheel", "requirements", "node", "tui", "installer")
    }
    paths = {
        "wheel": inputs.wheel,
        "requirements": inputs.requirements,
        "installer": inputs.installer,
    }
    tokens: dict[str, _FileToken] = {}
    for kind, path in paths.items():
        artifact = artifacts[kind]
        if path.name != artifact.filename:
            _runtime_failed()
        tokens[kind] = _verify_private_file(
            path,
            expected_mode=0o600,
            expected_size=artifact.size,
            expected_sha256=artifact.sha256,
        )
    if inputs.node.name != "node" or inputs.tui.name != "tui":
        _runtime_failed()
    _validate_source_tree(inputs.node, {"bin/node"})
    _validate_source_tree(inputs.tui, set())
    tokens["uv"] = _verify_private_file(inputs.uv, expected_mode=0o700)
    _inspect_wheel(inputs.wheel, inputs.manifest.version)
    return artifacts, tokens


def _receipt_for(inputs: RuntimeInputs, artifacts: Mapping[str, Artifact]) -> RuntimeReceipt:
    """从 manifest-bound artifacts 生成 runtime receipt。"""
    return RuntimeReceipt(
        version=inputs.manifest.version,
        git_commit=inputs.manifest.git_commit,
        runtime_relative=f"runtimes/{inputs.manifest.version}",
        python_version=inputs.manifest.python,
        node_version=".".join(str(value) for value in inputs.manifest.node.default),
        tui_version=artifacts["tui"].component_version,
        wheel_sha256=artifacts["wheel"].sha256,
        requirements_sha256=artifacts["requirements"].sha256,
        node_sha256=artifacts["node"].sha256,
        tui_sha256=artifacts["tui"].sha256,
        installer_sha256=artifacts["installer"].sha256,
    )


def _runtime_environment(layout: InstallLayout) -> dict[str, str]:
    """返回不继承 user config/env/proxy 的 closed-world uv/smoke environment。"""
    staging = layout.staging
    return {
        "HOME": str(staging / ".home"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "TMPDIR": str(staging / ".tmp"),
        "UV_CACHE_DIR": str(staging / ".uv-cache"),
        "UV_NO_CONFIG": "1",
        "UV_NO_ENV_FILE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }


def _prepare_runtime_parent(layout: InstallLayout) -> None:
    """创建或验证 program/runtimes 两级受管目录。"""
    mode = _program_mode(layout)
    for path in (layout.program_prefix, layout.runtimes_dir):
        if not _lexists(path):
            path.mkdir(mode=mode)
        _verify_directory(path, expected_mode=mode)


def _verify_directory(path: Path, *, expected_mode: int = 0o700) -> os.stat_result:
    """验证 no-follow owner/mode directory 并返回 metadata。"""
    try:
        metadata = path.lstat()
    except OSError:
        _runtime_failed()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        _runtime_failed()
    return metadata


def _verify_private_file(
    path: Path,
    *,
    expected_mode: int,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> _FileToken:
    """no-follow 读取并绑定 regular file owner/mode/inode/size/hash。"""
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != expected_mode
            or expected_size is not None
            and before.st_size != expected_size
        ):
            _runtime_failed()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if _metadata_snapshot(opened) != _metadata_snapshot(before):
                _runtime_failed()
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = path.lstat()
    except InstallError:
        raise
    except OSError:
        _runtime_failed()
    if (
        _metadata_snapshot(after_open) != _metadata_snapshot(opened)
        or _metadata_snapshot(after) != _metadata_snapshot(before)
    ):
        _runtime_failed()
    value = digest.hexdigest()
    if expected_sha256 is not None and not hmac.compare_digest(value, expected_sha256):
        _runtime_failed()
    return _FileToken(path, _metadata_snapshot(before), value, expected_mode)


def _verify_executable(path: Path) -> _FileToken:
    """验证 owner-only no-follow executable file。"""
    return _verify_private_file(path, expected_mode=0o700)


def _revalidate_token(token: _FileToken) -> None:
    """重新读取 pathname 并确认仍指向同一 verified regular file。"""
    current = _verify_private_file(
        token.path,
        expected_mode=token.mode,
        expected_size=token.snapshot[6],
        expected_sha256=token.sha256,
    )
    if current.snapshot != token.snapshot:
        _runtime_failed()


def _inspect_wheel(path: Path, version: str) -> None:
    """从 no-follow wheel descriptor 校验 Name/Version 和 miniclaw console entry。"""
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream, zipfile.ZipFile(stream) as archive:
            names = archive.namelist()
            if len(names) > _MAX_TREE_ENTRIES or any(
                not _safe_archive_name(name) for name in names
            ):
                _runtime_failed()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
            if len(metadata_names) != 1 or len(entry_names) != 1:
                _runtime_failed()
            prefix = metadata_names[0].removesuffix("METADATA")
            if entry_names[0].removesuffix("entry_points.txt") != prefix:
                _runtime_failed()
            metadata_info = archive.getinfo(metadata_names[0])
            entry_info = archive.getinfo(entry_names[0])
            if max(metadata_info.file_size, entry_info.file_size) > _MAX_METADATA_BYTES:
                _runtime_failed()
            metadata = BytesParser().parsebytes(archive.read(metadata_info))
            parser = configparser.ConfigParser(interpolation=None, strict=True)
            parser.read_string(archive.read(entry_info).decode("utf-8"))
            if (
                metadata.get("Name") != "miniclaw-agent"
                or metadata.get("Version") != version
                or not parser.has_section("console_scripts")
                or parser.get("console_scripts", "miniclaw", fallback="")
                != "miniclaw.cli:main"
            ):
                _runtime_failed()
        after = path.lstat()
    except InstallError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeError, configparser.Error):
        _runtime_failed()
    if _metadata_snapshot(after) != _metadata_snapshot(before):
        _runtime_failed()


def _safe_archive_name(value: str) -> bool:
    """判断 wheel member 是 normalized non-traversing POSIX path。"""
    if not value or "\\" in value or unicodedata.normalize("NFC", value) != value:
        return False
    pure = PurePosixPath(value)
    return (
        not pure.is_absolute()
        and str(pure) == value.rstrip("/")
        and all(part not in {"", ".", ".."} for part in pure.parts)
    )


def _validate_source_tree(root: Path, required_executables: set[str]) -> None:
    """完整扫描 safe-extracted tree，拒绝 link/special/hardlink/mode 漂移。"""
    seen: set[str] = set()
    entries = 0
    total = 0
    root_before = _verify_directory(root)
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        _verify_directory(current)
        for name in sorted((*names, *files)):
            entries += 1
            if entries > _MAX_TREE_ENTRIES or not _safe_component(name):
                _runtime_failed()
            path = current / name
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in seen:
                _runtime_failed()
            seen.add(relative.casefold())
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                _verify_directory(path)
            elif stat.S_ISREG(metadata.st_mode):
                mode = 0o700 if relative in required_executables else stat.S_IMODE(metadata.st_mode)
                if mode not in {0o600, 0o700}:
                    _runtime_failed()
                _verify_private_file(path, expected_mode=mode)
                total += metadata.st_size
                if total > _MAX_TREE_BYTES:
                    _runtime_failed()
            else:
                _runtime_failed()
    if not required_executables.issubset(seen):
        _runtime_failed()
    root_after = root.lstat()
    if _metadata_snapshot(root_after) != _metadata_snapshot(root_before):
        _runtime_failed()


def _copy_verified_tree(source: Path, destination: Path, required: set[str]) -> None:
    """复制已验证 regular/dir tree，不复制任何 link 或特殊文件。"""
    _validate_source_tree(source, required)
    os.mkdir(destination, 0o700)
    for directory, names, files in os.walk(source, topdown=True, followlinks=False):
        source_directory = Path(directory)
        relative_directory = source_directory.relative_to(source)
        target_directory = destination / relative_directory
        for name in sorted(names):
            os.mkdir(target_directory / name, 0o700)
        for name in sorted(files):
            source_file = source_directory / name
            mode = stat.S_IMODE(source_file.lstat().st_mode)
            token = _verify_private_file(source_file, expected_mode=mode)
            _copy_verified_file(token, target_directory / name, mode)
    _validate_source_tree(source, required)


def _copy_verified_file(token: _FileToken, destination: Path, mode: int) -> None:
    """从 verified no-follow descriptor 向 O_EXCL regular file 复制并 fsync。"""
    source_descriptor = -1
    destination_descriptor = -1
    try:
        _revalidate_token(token)
        source_descriptor = os.open(token.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        source_metadata = os.fstat(source_descriptor)
        if _metadata_snapshot(source_metadata) != token.snapshot:
            _runtime_failed()
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        while chunk := os.read(source_descriptor, 1024 * 1024):
            _write_all(destination_descriptor, chunk)
        os.fchmod(destination_descriptor, mode)
        os.fsync(destination_descriptor)
        if _metadata_snapshot(os.fstat(source_descriptor)) != token.snapshot:
            _runtime_failed()
        _revalidate_token(token)
    except InstallError:
        raise
    except OSError:
        _runtime_failed()
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)


def _harden_and_fsync_tree(root: Path) -> None:
    """把 Runtime dirs/files 收敛到 0700/0600-or-0700 并 fsync tree boundary。"""
    directories: list[Path] = []
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        directories.append(current)
        for name in names:
            path = current / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                _runtime_failed()
        for name in files:
            path = current / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _runtime_failed()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                mode = 0o700 if stat.S_IMODE(metadata.st_mode) & 0o111 else 0o600
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        directory.chmod(0o700)
        _fsync_directory(directory)


def _manifest_bytes(manifest: ReleaseManifest) -> bytes:
    """将 strict ReleaseManifest 序列化为 deterministic exact JSON。"""
    document = {
        "schema_version": manifest.schema_version,
        "product": manifest.product,
        "version": manifest.version,
        "git_commit": manifest.git_commit,
        "python": manifest.python,
        "node": {
            "default": ".".join(str(value) for value in manifest.node.default),
            "accepted": [
                {
                    "minimum": ".".join(str(value) for value in item.minimum),
                    "maximum_exclusive": ".".join(
                        str(value) for value in item.maximum_exclusive
                    ),
                }
                for item in manifest.node.accepted
            ],
        },
        "artifacts": [
            {
                "kind": item.kind,
                "filename": item.filename,
                "url": item.url,
                "sha256": item.sha256,
                "size": item.size,
                "media_type": item.media_type,
                "platform": {"os": item.platform.os, "arch": item.platform.arch},
                "component_version": item.component_version,
                "source_repository": item.source_repository,
                "license_ref": item.license_ref,
                "upstream_sha256": item.upstream_sha256,
            }
            for item in manifest.artifacts
        ],
        "supported_platforms": [
            {"os": item.os, "arch": item.arch} for item in manifest.supported_platforms
        ],
        "features": list(manifest.features),
        "database_schema": manifest.database_schema,
        "minimum_readable_schema": manifest.minimum_readable_schema,
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_exclusive(path: Path, payload: bytes, mode: int) -> None:
    """以 O_EXCL、fchmod、fsync 写一个 Runtime metadata file。"""
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        os.fchmod(descriptor, mode)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except OSError:
        _runtime_failed()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _verify_runtime_directory(path: Path, receipt: RuntimeReceipt) -> None:
    """校验 immutable Runtime root 和内部 receipt 完全绑定。"""
    _verify_directory(path)
    stored = RuntimeReceipt.load(path / "install-receipt.json")
    if path.name != receipt.version or stored != receipt:
        _runtime_failed()


def _validated_current(layout: InstallLayout) -> str | None:
    """返回 validated relative current target；不存在时返回 None。"""
    if not _lexists(layout.current):
        return None
    try:
        metadata = layout.current.lstat()
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            _activation_failed()
        target = os.readlink(layout.current)
        pure = PurePosixPath(target)
        if (
            pure.is_absolute()
            or len(pure.parts) != 2
            or pure.parts[0] != "runtimes"
            or _SEMVER.fullmatch(pure.parts[1]) is None
            or str(pure) != target
        ):
            _activation_failed()
        runtime = layout.program_prefix.joinpath(*pure.parts)
        receipt = RuntimeReceipt.load(runtime / "install-receipt.json")
        _verify_runtime_directory(runtime, receipt)
        after = layout.current.lstat()
        if _metadata_snapshot(after) != _metadata_snapshot(metadata):
            _activation_failed()
        return target
    except InstallError:
        raise
    except OSError:
        _activation_failed()


def _revalidate_symlink(path: Path, expected_target: str) -> None:
    """确认 activation 前 existing current 仍是同一 relative target。"""
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or os.readlink(path) != expected_target
        ):
            _activation_failed()
    except OSError:
        _activation_failed()


def _quarantine_and_remove(path: Path, identity: tuple[int, int]) -> None:
    """只清理由本轮持有 inode 的目录，并先从公开 namespace 隔离。"""
    if not _lexists(path):
        return
    quarantine = path.with_name(f".{path.name}.cleanup-{os.getpid()}")
    try:
        if _lexists(quarantine):
            return
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity or not stat.S_ISDIR(metadata.st_mode):
            return
        _rename_no_replace(path, quarantine)
        moved = quarantine.lstat()
        if (moved.st_dev, moved.st_ino) != identity:
            return
        _fsync_directory(path.parent)
        _remove_owned_tree(quarantine)
        _fsync_directory(path.parent)
    except OSError:
        return


def _remove_owned_tree(root: Path) -> None:
    """不跟随 link 地删除 installer-owned private tree。"""
    if not _lexists(root):
        return
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        _runtime_failed()
    for directory, names, files in os.walk(root, topdown=False, followlinks=False):
        current = Path(directory)
        for name in files:
            path = current / name
            item = path.lstat()
            if item.st_uid != os.geteuid() or not (
                stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)
            ):
                _runtime_failed()
            path.unlink()
        for name in names:
            path = current / name
            item = path.lstat()
            if stat.S_ISLNK(item.st_mode):
                if item.st_uid != os.geteuid():
                    _runtime_failed()
                path.unlink()
            elif stat.S_ISDIR(item.st_mode) and item.st_uid == os.geteuid():
                path.rmdir()
            else:
                _runtime_failed()
    root.rmdir()


def _directory_identity(path: Path) -> tuple[int, int]:
    """返回 owner-only directory 的 device/inode token。"""
    metadata = _verify_directory(path)
    return metadata.st_dev, metadata.st_ino


def _read_private_regular(path: Path, uid: int, limit: int) -> bytes:
    """bounded no-follow 读取 mode 0600 regular file。"""
    token = _verify_private_file(path, expected_mode=0o600)
    if token.snapshot[6] > limit:
        _runtime_failed()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if os.fstat(descriptor).st_uid != uid:
                _runtime_failed()
            payload = os.read(descriptor, limit + 1)
            if os.read(descriptor, 1) or len(payload) > limit:
                _runtime_failed()
        finally:
            os.close(descriptor)
    except InstallError:
        raise
    except OSError:
        _runtime_failed()
    _revalidate_token(token)
    return payload


def _json_object(payload: bytes) -> dict[str, object]:
    """解析 single strict JSON object，拒绝 duplicate/non-object/invalid UTF-8。"""
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        _runtime_failed()
    if type(document) is not dict:
        _runtime_failed()
    return document


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造拒绝 duplicate keys 的 JSON object。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("invalid json object")
        result[key] = value
    return result


def _drain_bounded(
    stream: BinaryIO,
    chunks: list[bytes],
    overflow: list[bool],
    index: int,
) -> None:
    """持续 drain pipe，仅保留前 64 KiB。"""
    while chunk := stream.read(8192):
        remaining = _MAX_OUTPUT_BYTES - sum(len(value) for value in chunks)
        if remaining > 0:
            chunks.append(chunk[:remaining])
        if len(chunk) > remaining:
            overflow[index] = True


def _valid_argv(argv: object) -> bool:
    """判断 argv 是 nonempty、无 NUL/control 的 absolute-program tuple。"""
    return (
        type(argv) is tuple
        and bool(argv)
        and all(
            type(value) is str
            and value
            and "\x00" not in value
            and all(character >= " " and character != "\x7f" for character in value)
            for value in argv
        )
        and Path(argv[0]).is_absolute()
    )


def _valid_environment(env: object) -> bool:
    """判断 environment 是 closed string map 且不含 NUL。"""
    return type(env) is dict and all(
        type(key) is str
        and key
        and "=" not in key
        and "\x00" not in key
        and type(value) is str
        and "\x00" not in value
        for key, value in env.items()
    )


def _safe_component(value: str) -> bool:
    """判断 filesystem component normalized 且无 control/separator。"""
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and unicodedata.normalize("NFC", value) == value
        and all(character.isprintable() for character in value)
    )


def _safe_absolute_path(path: object) -> bool:
    """判断 Path 是 normalized lexical absolute non-root path。"""
    return (
        isinstance(path, Path)
        and path.is_absolute()
        and path != Path("/")
        and str(path) == os.path.normpath(str(path))
        and all(_safe_component(part) for part in path.parts[1:])
    )


def _semver_key(value: str) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    """返回可比较的 SemVer precedence key；stable 高于 prerelease。"""
    matched = _SEMVER.fullmatch(value)
    if matched is None:
        _runtime_failed()
    major, minor, patch = (int(matched.group(index)) for index in (1, 2, 3))
    prerelease = matched.group(4)
    if prerelease is None:
        return major, minor, patch, 1, ()
    identifiers = tuple(
        (0, int(item)) if item.isdigit() else (1, item) for item in prerelease.split(".")
    )
    return major, minor, patch, 0, identifiers


def _metadata_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    """返回足以发现 pathname/file mutation 的 metadata snapshot。"""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    """处理 short write，完整写入 payload。"""
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    """fsync 一个 no-follow directory namespace。"""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_same_inode(path: Path, identity: tuple[int, int]) -> None:
    """仅 unlink 仍匹配 device/inode 的临时 symlink。"""
    try:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) == identity:
            path.unlink()
    except OSError:
        return


def _lexists(path: Path) -> bool:
    """不跟随 symlink 判断目录项是否存在。"""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _runtime_failed() -> Never:
    """抛出不包含 path、subprocess output 或 Secret 的稳定构建错误。"""
    raise InstallError("runtime_install_failed", "manifest")


def _activation_failed() -> Never:
    """抛出不包含 path 或 target 的稳定激活错误。"""
    raise InstallError("activation_failed", "manifest")
