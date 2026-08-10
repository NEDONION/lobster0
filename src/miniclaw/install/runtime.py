"""构建 hash-locked immutable Runtime，并原子切换 stable launcher target。"""

from __future__ import annotations

import configparser
import ctypes
import errno
import hashlib
import hmac
import json
import os
import posixpath
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Never, Protocol

from miniclaw.install.layout import InstallLayout, InstallLock, _program_mode
from miniclaw.install.models import (
    Artifact,
    InstallError,
    PlatformKey,
    ReleaseManifest,
    _python_filename_version,
)
from miniclaw.install.receipt import InstallReceipt, _rename_no_replace

_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PYTHON_VERSION = re.compile(r"^3\.12\.(0|[1-9][0-9]*)$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_RECEIPT_KEYS = {
    "executables_sha256",
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
_LEGACY_RECEIPT_KEYS = _RECEIPT_KEYS - {"executables_sha256"}
_EXECUTABLES_FILENAME = ".runtime-executables.json"
_TRANSIENT_RUNTIME_NAMES = frozenset({".home", ".inputs", ".tmp", ".uv-cache"})
_RUNTIME_METADATA_PATHS = frozenset(
    {_EXECUTABLES_FILENAME, "install-receipt.json", "release-manifest.json"}
)
_REQUIRED_RUNTIME_EXECUTABLES = (
    "miniclaw-installer.pyz",
    "node/bin/node",
    "python/bin/python3.12",
    "venv/bin/miniclaw",
)
_MAX_RECEIPT_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_TREE_ENTRIES = 20_000
_MAX_TREE_BYTES = 1_073_741_824
_MAX_WHEEL_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_WHEEL_BYTES = 512 * 1024 * 1024
_MAX_WHEEL_RATIO = 200
_COMMAND_TIMEOUT_SECONDS = 300.0
_SMOKE_TIMEOUT_SECONDS = 30.0
_PYTHON_PROBE = (
    "import json,os,sys;"
    "print(json.dumps({'base_prefix':os.path.realpath(sys.base_prefix),"
    "'executable':os.path.realpath(sys.executable),"
    "'version':list(sys.version_info[:3])},sort_keys=True,separators=(',',':')))"
)


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
        managed_python_root: bootstrap 已验证的 managed Python 完整根目录。
        managed_python_executable: root 内 canonical `bin/python3.12` regular executable。

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
    managed_python_root: Path
    managed_python_executable: Path

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
            self.managed_python_root,
            self.managed_python_executable,
        ):
            if not _safe_absolute_path(value):
                _runtime_failed()
        if self.managed_python_executable != self.managed_python_root / "bin" / "python3.12":
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
        executables_sha256: Runtime executable path manifest hash；legacy receipt 为 None。

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
    executables_sha256: str | None

    def __post_init__(self) -> None:
        """校验 receipt exact schema 与 cross-field bindings。"""
        artifact_hashes = (
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
            or type(self.python_version) is not str
            or _PYTHON_VERSION.fullmatch(self.python_version) is None
            or type(self.node_version) is not str
            or _SEMVER.fullmatch(self.node_version) is None
            or type(self.tui_version) is not str
            or self.tui_version != self.version
            or any(
                type(value) is not str or _HASH.fullmatch(value) is None
                for value in artifact_hashes
            )
            or self.executables_sha256 is not None
            and (
                type(self.executables_sha256) is not str
                or _HASH.fullmatch(self.executables_sha256) is None
            )
        ):
            _runtime_failed()

    def to_bytes(self) -> bytes:
        """返回 deterministic exact-key owner-only receipt JSON。"""
        keys = (
            _LEGACY_RECEIPT_KEYS
            if self.executables_sha256 is None
            else _RECEIPT_KEYS
        )
        return (
            json.dumps(
                {name: getattr(self, name) for name in sorted(keys)},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode()

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_uid: int | None = None,
        expected_mode: int = 0o600,
    ) -> RuntimeReceipt:
        """no-follow 读取权限精确的 Runtime receipt。

        Args:
            path: runtime 内 `install-receipt.json` 的 absolute path。
            expected_uid: 期望 owner；默认当前 euid。
            expected_mode: user-prefix 为 0600，system-prefix 为 0644。

        Returns:
            exact-key 且字段关系有效的 RuntimeReceipt。

        Raises:
            InstallError: 文件 type/owner/mode/size、JSON 或字段无效。
        """
        uid = os.geteuid() if expected_uid is None else expected_uid
        payload = _read_private_regular(
            path,
            uid,
            _MAX_RECEIPT_BYTES,
            expected_mode=expected_mode,
        )
        try:
            document = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            _runtime_failed()
        if type(document) is not dict or frozenset(document) not in {
            frozenset(_RECEIPT_KEYS),
            frozenset(_LEGACY_RECEIPT_KEYS),
        }:
            _runtime_failed()
        if "executables_sha256" not in document:
            document["executables_sha256"] = None
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


@dataclass(frozen=True, slots=True)
class _SymlinkToken:
    """绑定 owner-only symlink 的 pathname、metadata 与 target。"""

    path: Path
    snapshot: tuple[int, ...]
    target: str


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
                umask=0o077,
            )
        except OSError:
            _runtime_failed()
        assert process.stdout is not None and process.stderr is not None
        stdout = bytearray()
        stderr = bytearray()
        overflow = [False, False]
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        for index, stream in enumerate((process.stdout, process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, index)
        timed_out = False
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                events = selector.select(remaining)
                if not events:
                    timed_out = True
                    break
                for key, _ in events:
                    target = stdout if key.data == 0 else stderr
                    if not _drain_ready(
                        key.fileobj.fileno(), target, overflow, key.data, deadline
                    ):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
            if not timed_out:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                else:
                    try:
                        returncode = process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        timed_out = True
            if timed_out:
                _terminate_process_group(process)
                returncode = -signal.SIGKILL
                overflow[0] = True
        finally:
            selector.close()
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
        if any(overflow):
            returncode = returncode or 125
        return CommandResult(returncode, bytes(stdout), bytes(stderr))


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
            if type(inputs) is not RuntimeInputs:
                _runtime_failed()
            program_mode, _data_mode = _runtime_modes(inputs.layout)
            if program_mode == 0o755 and not _is_root_builder():
                _runtime_failed()
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
            private_inputs = inputs.layout.staging / ".inputs"
            os.mkdir(private_inputs, 0o700)
            _copy_verified_tree(
                inputs.managed_python_root,
                inputs.layout.staging / "python",
                {"bin/python3.12"},
                allow_internal_symlinks=True,
                allow_public_read=True,
                program_mode=0o700,
            )
            _copy_verified_tree(
                inputs.node,
                inputs.layout.staging / "node",
                {"bin/node"},
                program_mode=0o700,
            )
            _copy_verified_tree(
                inputs.tui,
                inputs.layout.staging / "tui",
                set(),
                program_mode=0o700,
            )
            _copy_verified_file(
                tokens["installer"],
                inputs.layout.staging / "miniclaw-installer.pyz",
                0o700,
            )
            private_tokens: dict[str, _FileToken] = {}
            for name, mode in (("uv", 0o700), ("requirements", 0o600), ("wheel", 0o600)):
                filename = tokens[name].path.name if name != "uv" else "uv"
                destination = private_inputs / filename
                _copy_verified_file(tokens[name], destination, mode)
                private_tokens[name] = _verify_private_file(
                    destination,
                    expected_mode=mode,
                    expected_size=tokens[name].snapshot[6],
                    expected_sha256=tokens[name].sha256,
                )
            _inspect_wheel(private_tokens["wheel"].path, inputs.manifest.version)
            python = inputs.layout.staging / "venv" / "bin" / "python"
            internal_python = inputs.layout.staging / "python" / "bin" / "python3.12"
            self._checked(
                (
                    str(private_tokens["uv"].path),
                    "venv",
                    "--relocatable",
                    "--python",
                    str(internal_python),
                    "--no-python-downloads",
                    str(inputs.layout.staging / "venv"),
                ),
                env,
                _COMMAND_TIMEOUT_SECONDS,
                (private_tokens["uv"],),
            )
            _normalize_staging_venv_link(inputs.layout)
            _harden_and_fsync_tree(inputs.layout.staging / "venv", 0o700)
            self._checked(
                (
                    str(private_tokens["uv"].path),
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--require-hashes",
                    "-r",
                    str(private_tokens["requirements"].path),
                ),
                env,
                _COMMAND_TIMEOUT_SECONDS,
                (private_tokens["uv"], private_tokens["requirements"]),
            )
            self._checked(
                (
                    str(private_tokens["uv"].path),
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    str(private_tokens["wheel"].path),
                ),
                env,
                _COMMAND_TIMEOUT_SECONDS,
                (private_tokens["uv"], private_tokens["wheel"]),
            )
            executable_paths = _required_runtime_executables(inputs.layout.staging)
            _harden_and_fsync_tree(
                inputs.layout.staging,
                0o700,
                executable_paths=executable_paths,
            )
            _verify_runtime_tree(
                inputs.layout.staging,
                program_mode=0o700,
                expected_executables=executable_paths,
                allow_transient=True,
            )
            executable_manifest = _executable_manifest_bytes(executable_paths)
            executables_sha256 = hashlib.sha256(executable_manifest).hexdigest()
            _write_exclusive(
                inputs.layout.staging / _EXECUTABLES_FILENAME,
                executable_manifest,
                0o600,
            )
            staging_python_version = self._smoke(inputs, expected_program_mode=0o700)
            receipt = _receipt_for(
                inputs,
                artifacts,
                staging_python_version,
                executables_sha256,
            )
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
            _harden_and_fsync_tree(
                inputs.layout.staging,
                0o700,
                executable_paths=executable_paths,
            )
            _verify_runtime_tree(
                inputs.layout.staging,
                program_mode=0o700,
                expected_executables=executable_paths,
                allow_transient=True,
            )
            _rename_no_replace(inputs.layout.staging, inputs.layout.runtime)
            published = True
            moved = inputs.layout.runtime.lstat()
            if (moved.st_dev, moved.st_ino) != staging_identity:
                _runtime_failed()
            _repair_final_venv(inputs.layout, expected_program_mode=0o700)
            final_python_version = self._smoke(
                inputs,
                runtime=inputs.layout.runtime,
                expected_program_mode=0o700,
            )
            if final_python_version != staging_python_version:
                _runtime_failed()
            final_env = _runtime_environment(inputs.layout, runtime=inputs.layout.runtime)
            for private in (
                Path(final_env["HOME"]),
                Path(final_env["TMPDIR"]),
                Path(final_env["UV_CACHE_DIR"]),
                inputs.layout.runtime / ".inputs",
            ):
                _remove_owned_tree(private)
            _verify_runtime_tree(
                inputs.layout.runtime,
                program_mode=0o700,
                expected_executables=executable_paths,
            )
            if program_mode == 0o755:
                _publish_system_runtime(inputs.layout.runtime, executable_paths)
            _verify_runtime_directory(
                inputs.layout.runtime,
                receipt,
                program_mode=program_mode,
            )
            _fsync_directory(inputs.layout.runtimes_dir)
            return receipt
        except InstallError:
            if published and staging_identity is not None:
                _secure_failed_tree(inputs.layout.runtime, staging_identity)
            elif staging_identity is not None:
                _secure_failed_tree(inputs.layout.staging, staging_identity)
            raise
        except BaseException as error:
            if published and staging_identity is not None:
                _secure_failed_tree(inputs.layout.runtime, staging_identity)
            elif staging_identity is not None:
                _secure_failed_tree(inputs.layout.staging, staging_identity)
            raise InstallError("runtime_install_failed", "manifest") from error

    def smoke(self, inputs: RuntimeInputs, *, runtime: Path | None = None) -> str:
        """执行 Python version、install-smoke 与 checkout-independent TUI smoke。

        Args:
            inputs: 当前 staging 所属 strict Runtime inputs。

        Raises:
            InstallError: executable、输出、版本、Channel import 或 TUI smoke 失败。
        """
        if type(inputs) is not RuntimeInputs:
            _runtime_failed()
        return self._smoke(
            inputs,
            runtime=runtime,
            expected_program_mode=_program_mode(inputs.layout),
        )

    def _smoke(
        self,
        inputs: RuntimeInputs,
        *,
        runtime: Path | None = None,
        expected_program_mode: int,
    ) -> str:
        """按显式 private/published mode 执行同一组 Runtime smoke。"""
        if type(inputs) is not RuntimeInputs:
            _runtime_failed()
        root = inputs.layout.staging if runtime is None else runtime
        if root not in {inputs.layout.staging, inputs.layout.runtime}:
            _runtime_failed()
        python = root / "venv" / "bin" / "python"
        node = root / "node" / "bin" / "node"
        tui = root / "tui" / "dist" / "main.js"
        canonical_python = root / "python" / "bin" / "python3.12"
        if expected_program_mode not in {0o700, 0o755}:
            _runtime_failed()
        program_mode = expected_program_mode
        data_mode = 0o644 if program_mode == 0o755 else 0o600
        python_token, python_link = _verify_internal_python_link(
            python,
            root,
            canonical_python,
            require_relative=runtime is not None,
            expected_mode=program_mode,
        )
        config_token = _verify_private_file(
            root / "venv" / "pyvenv.cfg", expected_mode=data_mode
        )
        node_token = _verify_executable(node, expected_mode=program_mode)
        tui_token = _verify_private_file(tui, expected_mode=data_mode)
        env = _runtime_environment(inputs.layout, runtime=root)
        probe = self._checked(
            (str(python), "-I", "-c", _PYTHON_PROBE),
            env,
            _SMOKE_TIMEOUT_SECONDS,
            (python_token, config_token),
            (python_link,),
        )
        facts = _python_facts(probe.stdout, root, canonical_python)
        version = self._checked(
            (str(python), "-I", "-m", "miniclaw", "--version"),
            env,
            _SMOKE_TIMEOUT_SECONDS,
            (python_token, config_token),
            (python_link,),
        )
        if version.stdout != f"miniclaw {inputs.manifest.version}\n".encode():
            _runtime_failed()
        install = self._checked(
            (str(python), "-I", "-m", "miniclaw", "install-smoke", "--json"),
            env,
            _SMOKE_TIMEOUT_SECONDS,
            (python_token, config_token),
            (python_link,),
        )
        document = _json_object(install.stdout)
        if document.get("status") != "ok" or document.get("version") != inputs.manifest.version:
            _runtime_failed()
        node_version = self._checked(
            (str(node), "--version"),
            env,
            _SMOKE_TIMEOUT_SECONDS,
            (node_token,),
        )
        expected_node = "v" + ".".join(str(value) for value in inputs.manifest.node.default) + "\n"
        if node_version.stdout != expected_node.encode():
            _runtime_failed()
        tui_result = self._checked(
            (str(node), str(tui), "--smoke"),
            env,
            _SMOKE_TIMEOUT_SECONDS,
            (node_token, tui_token),
        )
        if _json_object(tui_result.stdout) != {
            "component": "pi-tui",
            "status": "ok",
            "version": inputs.manifest.version,
        }:
            _runtime_failed()
        return facts

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
        links: tuple[_SymlinkToken, ...] = (),
    ) -> CommandResult:
        """在命令前后重验 pathname tokens，并归一化 runner failures。"""
        for token in tokens:
            _revalidate_token(token)
        for link in links:
            _revalidate_link_token(link)
        try:
            result = self._runner.run(argv, env=dict(env), timeout=timeout)
        except BaseException as error:
            raise InstallError("runtime_install_failed", "manifest") from error
        if type(result) is not CommandResult or result.returncode != 0:
            _runtime_failed()
        for token in tokens:
            _revalidate_token(token)
        for link in links:
            _revalidate_link_token(link)
        return result


def activate_runtime(layout: InstallLayout, receipt: RuntimeReceipt) -> None:
    """用 relative `current.next` symlink 和 native swap 原子激活 Runtime。

    Args:
        layout: 与 receipt version 绑定的 validated layout。
        receipt: build 已写入最终 Runtime 的 receipt。

    Raises:
        InstallError: target/current/current.next 不受管或 namespace 持久化失败。
    """
    next_link = layout.current.with_name("current.next") if type(layout) is InstallLayout else None
    next_identity: tuple[int, int] | None = None
    old_identity: tuple[int, int] | None = None
    old_target: str | None = None
    published_absent = False
    swapped = False
    committed = False
    try:
        if (
            type(layout) is not InstallLayout
            or type(receipt) is not RuntimeReceipt
            or receipt.version != layout.runtime.name
            or receipt.runtime_relative != f"runtimes/{layout.runtime.name}"
        ):
            _activation_failed()
        target = receipt.runtime_relative
        if _verified_runtime_target(layout, target) != receipt:
            _activation_failed()
        assert next_link is not None
        current_token = _validated_current(layout)
        if _lexists(next_link):
            _recover_current_next(layout, next_link, target, current_token)
            current_token = _validated_current(layout)
        os.symlink(target, next_link)
        metadata = next_link.lstat()
        next_identity = (metadata.st_dev, metadata.st_ino)
        if metadata.st_uid != os.geteuid() or not stat.S_ISLNK(metadata.st_mode):
            _activation_failed()
        _fsync_directory(layout.program_prefix)
        if current_token is None:
            if _lexists(layout.current):
                _activation_failed()
            _rename_no_replace(next_link, layout.current)
            published_absent = True
            if not _same_symlink(layout.current, next_identity, target):
                _activation_failed()
        else:
            old_target = current_token
            old_identity = _symlink_identity(layout.current, old_target)
            _rename_exchange(next_link, layout.current)
            swapped = True
            if (
                not _same_symlink(layout.current, next_identity, target)
                or not _same_symlink(next_link, old_identity, old_target)
            ):
                _activation_failed()
        if _validated_current(layout) != target or not _same_symlink(
            layout.current, next_identity, target
        ):
            _activation_failed()
        _fsync_directory(layout.program_prefix)
        committed = True
        if swapped and old_identity is not None and old_target is not None:
            _retire_current_next(next_link, old_identity, old_target)
    except InstallError as error:
        if not committed:
            _rollback_activation(
                layout,
                next_link,
                next_identity,
                swapped=swapped,
                published_absent=published_absent,
            )
        if error.code == "activation_failed":
            raise
        raise InstallError("activation_failed", "manifest") from error
    except BaseException as error:
        if not committed:
            _rollback_activation(
                layout,
                next_link,
                next_identity,
                swapped=swapped,
                published_absent=published_absent,
            )
        raise InstallError("activation_failed", "manifest") from error


def discard_unactivated_runtime(layout: InstallLayout, lock: InstallLock) -> bool:
    """清理完整、未被引用且尚未激活的目标 Runtime。

    Args:
        layout: 与待清理版本绑定的 validated layout。
        lock: 当前进程通过 `InstallLock.acquire` 持有的原始实例。

    Returns:
        删除目标 Runtime 时返回 true；目标不存在或仍被受管引用时返回 false。

    Raises:
        InstallError: lock、Runtime、引用、inode 或持久化事实不可信。
    """
    try:
        if (
            type(layout) is not InstallLayout
            or type(lock) is not InstallLock
            or not lock.owns(layout)
        ):
            _runtime_failed()
        if not _lexists(layout.runtime):
            return False
        program_mode, _data_mode = _runtime_modes(layout)
        identity = _directory_identity(layout.runtime, expected_mode=program_mode)
        target = f"runtimes/{layout.runtime.name}"
        receipt = _verified_runtime_target(layout, target)
        if receipt.executables_sha256 is None:
            _runtime_failed()
        if target in _runtime_references(layout):
            return False
        if (
            not lock.owns(layout)
            or _directory_identity(layout.runtime, expected_mode=program_mode) != identity
        ):
            _runtime_failed()
        if target in _runtime_references(layout):
            return False
        if (
            _verified_runtime_target(layout, target) != receipt
            or not lock.owns(layout)
            or _directory_identity(layout.runtime, expected_mode=program_mode) != identity
            or not _revoke_tree_root_private(layout.runtime, identity)
            or not _quarantine_and_remove(layout.runtime, identity)
        ):
            _runtime_failed()
        return True
    except InstallError as error:
        if error.code == "runtime_install_failed":
            raise
        raise InstallError("runtime_install_failed", "manifest") from error
    except BaseException as error:
        raise InstallError("runtime_install_failed", "manifest") from error


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
        program_mode, data_mode = _runtime_modes(layout)
        _verify_directory(layout.runtimes_dir, expected_mode=program_mode)
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
                receipt = RuntimeReceipt.load(
                    path / "install-receipt.json", expected_mode=data_mode
                )
                if receipt.runtime_relative != f"runtimes/{entry.name}":
                    continue
                _verify_runtime_directory(path, receipt, program_mode=program_mode)
            except InstallError:
                continue
            if receipt.executables_sha256 is None and receipt.runtime_relative not in protected:
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
                _quarantine_and_remove(
                    path,
                    _directory_identity(path, expected_mode=program_mode),
                )
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
    _validate_source_tree(
        inputs.managed_python_root,
        {"bin/python3.12"},
        allow_internal_symlinks=True,
        allow_public_read=True,
    )
    python_mode = stat.S_IMODE(inputs.managed_python_executable.lstat().st_mode)
    if python_mode not in {0o700, 0o755}:
        _runtime_failed()
    tokens["managed_python"] = _verify_private_file(
        inputs.managed_python_executable,
        expected_mode=python_mode,
    )
    return artifacts, tokens


def _receipt_for(
    inputs: RuntimeInputs,
    artifacts: Mapping[str, Artifact],
    python_version: str,
    executables_sha256: str,
) -> RuntimeReceipt:
    """从 manifest-bound artifacts 生成 runtime receipt。"""
    return RuntimeReceipt(
        version=inputs.manifest.version,
        git_commit=inputs.manifest.git_commit,
        runtime_relative=f"runtimes/{inputs.manifest.version}",
        python_version=python_version,
        node_version=".".join(str(value) for value in inputs.manifest.node.default),
        tui_version=artifacts["tui"].component_version,
        wheel_sha256=artifacts["wheel"].sha256,
        requirements_sha256=artifacts["requirements"].sha256,
        node_sha256=artifacts["node"].sha256,
        tui_sha256=artifacts["tui"].sha256,
        installer_sha256=artifacts["installer"].sha256,
        executables_sha256=executables_sha256,
    )


def _runtime_environment(
    layout: InstallLayout,
    *,
    runtime: Path | None = None,
) -> dict[str, str]:
    """返回不继承 user config/env/proxy 的 closed-world uv/smoke environment。"""
    staging = layout.staging if runtime is None else runtime
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


def _python_facts(payload: bytes, runtime: Path, canonical: Path) -> str:
    """解析 fixed probe，并绑定 exact 3.12、canonical executable 与 internal base prefix。"""
    document = _json_object(payload)
    if set(document) != {"base_prefix", "executable", "version"}:
        _runtime_failed()
    version = document["version"]
    if (
        type(version) is not list
        or len(version) != 3
        or any(type(value) is not int or value < 0 for value in version)
        or version[:2] != [3, 12]
        or document["executable"] != str(canonical)
        or document["base_prefix"] != str(runtime / "python")
    ):
        _runtime_failed()
    return ".".join(str(value) for value in version)


def _verify_internal_python_link(
    path: Path,
    runtime: Path,
    canonical: Path,
    *,
    require_relative: bool,
    expected_mode: int,
) -> tuple[_FileToken, _SymlinkToken]:
    """绑定 venv interpreter link，并确保最终指向内部 canonical Python。"""
    parent = -1
    try:
        parent, name = _open_parent_nofollow(path)
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        target = os.readlink(name, dir_fd=parent)
        resolved = path.resolve(strict=True)
        runtime_resolved = runtime.resolve(strict=True)
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except (OSError, RuntimeError):
        _runtime_failed()
    finally:
        if parent >= 0:
            os.close(parent)
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or _metadata_snapshot(after) != _metadata_snapshot(metadata)
        or resolved != canonical
        or not resolved.is_relative_to(runtime_resolved)
        or require_relative
        and Path(target).is_absolute()
        or Path(target).is_absolute()
        and not Path(target).is_relative_to(runtime)
    ):
        _runtime_failed()
    executable = _verify_executable(canonical, expected_mode=expected_mode)
    return executable, _SymlinkToken(path, _metadata_snapshot(metadata), target)


def _normalize_staging_venv_link(layout: InstallLayout) -> None:
    """把 uv 生成且绑定 staging canonical Python 的 absolute link 改为 relative。"""
    runtime = layout.staging
    python = runtime / "venv" / "bin" / "python"
    canonical = runtime / "python" / "bin" / "python3.12"
    temporary = python.with_name("python.relative")
    try:
        _verify_internal_python_link(
            python,
            runtime,
            canonical,
            require_relative=False,
            expected_mode=0o700,
        )
        if os.readlink(python) != str(canonical):
            _runtime_failed()
        relative = os.path.relpath(canonical, start=python.parent)
        os.symlink(relative, temporary)
        os.replace(temporary, python)
        _fsync_directory(python.parent)
        _verify_internal_python_link(
            python,
            runtime,
            canonical,
            require_relative=True,
            expected_mode=0o700,
        )
    except InstallError:
        raise
    except OSError:
        _runtime_failed()
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _repair_final_venv(
    layout: InstallLayout,
    *,
    expected_program_mode: int,
) -> None:
    """按当前构建 mode 修复 final Runtime 内部 venv link/config。"""
    runtime = layout.runtime
    if expected_program_mode not in {0o700, 0o755}:
        _runtime_failed()
    program_mode = expected_program_mode
    data_mode = 0o644 if program_mode == 0o755 else 0o600
    python = runtime / "venv" / "bin" / "python"
    canonical = runtime / "python" / "bin" / "python3.12"
    config = runtime / "venv" / "pyvenv.cfg"
    temporary_link = python.with_name("python.next")
    temporary_config = config.with_name("pyvenv.cfg.next")
    try:
        metadata = python.lstat()
        if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _runtime_failed()
        old_target = os.readlink(python)
        expected_old = os.path.relpath(canonical, start=python.parent)
        if old_target != expected_old:
            _runtime_failed()
        original = _read_private_regular(
            config,
            os.geteuid(),
            _MAX_METADATA_BYTES,
            expected_mode=data_mode,
        ).decode("utf-8")
        lines = original.splitlines()
        if sum(line.startswith("home = ") for line in lines) != 1:
            _runtime_failed()
        updated = "\n".join(
            f"home = {runtime / 'python' / 'bin'}" if line.startswith("home = ") else line
            for line in lines
        ) + "\n"
        _write_exclusive(temporary_config, updated.encode(), data_mode)
        relative = os.path.relpath(canonical, start=python.parent)
        os.symlink(relative, temporary_link)
        os.replace(temporary_link, python)
        os.replace(temporary_config, config)
        _fsync_directory(python.parent)
        _fsync_directory(config.parent)
        _verify_internal_python_link(
            python,
            runtime,
            canonical,
            require_relative=True,
            expected_mode=program_mode,
        )
        persisted = _read_private_regular(
            config,
            os.geteuid(),
            _MAX_METADATA_BYTES,
            expected_mode=data_mode,
        )
        if persisted != updated.encode():
            _runtime_failed()
    except InstallError:
        raise
    except (OSError, UnicodeError) as error:
        raise InstallError("runtime_install_failed", "manifest") from error
    finally:
        for temporary in (temporary_link, temporary_config):
            try:
                temporary.unlink()
            except OSError:
                pass


def _prepare_runtime_parent(layout: InstallLayout) -> None:
    """创建或验证 program/runtimes 两级受管目录。"""
    mode = _program_mode(layout)
    for path in (layout.program_prefix, layout.runtimes_dir):
        if not _lexists(path):
            path.mkdir(mode=mode)
        _verify_directory(path, expected_mode=mode)


def _runtime_modes(layout: InstallLayout) -> tuple[int, int]:
    """返回 Runtime 目录/可执行文件 mode 与 regular data mode。"""
    program_mode = _program_mode(layout)
    return program_mode, 0o644 if program_mode == 0o755 else 0o600


def _is_root_builder() -> bool:
    """判断当前 Runtime builder 是否持有 system-prefix 所需 root 身份。"""
    return os.geteuid() == 0


def _open_parent_nofollow(path: Path) -> tuple[int, str]:
    """component-wise no-follow 打开 absolute path 的 parent dirfd。"""
    if not _safe_absolute_path(path) or path == Path("/"):
        _runtime_failed()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                flags | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            metadata = os.fstat(child)
            mode = stat.S_IMODE(metadata.st_mode)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in {0, os.geteuid()}
                or mode & 0o022
                and not (metadata.st_uid == 0 and mode & stat.S_ISVTX)
            ):
                os.close(child)
                _runtime_failed()
            os.close(descriptor)
            descriptor = child
        return descriptor, path.name
    except InstallError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        _runtime_failed()


def _open_directory_nofollow(path: Path) -> tuple[int, os.stat_result]:
    """component-wise no-follow 打开目录并返回稳定 descriptor/metadata。"""
    parent = -1
    descriptor = -1
    try:
        parent, name = _open_parent_nofollow(path)
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        metadata = os.fstat(descriptor)
        pathname_metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _metadata_snapshot(metadata) != _metadata_snapshot(pathname_metadata)
        ):
            _runtime_failed()
        os.close(parent)
        return descriptor, metadata
    except InstallError:
        if parent >= 0:
            os.close(parent)
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if parent >= 0:
            os.close(parent)
        if descriptor >= 0:
            os.close(descriptor)
        _runtime_failed()


def _open_regular_nofollow(path: Path) -> tuple[int, int, str, os.stat_result]:
    """component-wise no-follow 打开 regular file，并保留 parent dirfd。"""
    parent = -1
    descriptor = -1
    try:
        parent, name = _open_parent_nofollow(path)
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _metadata_snapshot(opened) != _metadata_snapshot(before)
        ):
            _runtime_failed()
        return descriptor, parent, name, opened
    except InstallError:
        if descriptor >= 0:
            os.close(descriptor)
        if parent >= 0:
            os.close(parent)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        if parent >= 0:
            os.close(parent)
        _runtime_failed()


def _verify_directory(path: Path, *, expected_mode: int = 0o700) -> os.stat_result:
    """验证 no-follow owner/mode directory 并返回 metadata。"""
    descriptor = -1
    try:
        descriptor, metadata = _open_directory_nofollow(path)
    except (InstallError, OSError):
        _runtime_failed()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        _runtime_failed()
    return metadata


def _verify_tree_directory(path: Path, allowed_modes: set[int]) -> os.stat_result:
    """验证 source tree directory no-follow/owner 且 mode 不可写。"""
    descriptor = -1
    try:
        descriptor, metadata = _open_directory_nofollow(path)
    except (InstallError, OSError):
        _runtime_failed()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) not in allowed_modes
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
    descriptor = -1
    parent = -1
    try:
        descriptor, parent, name, before = _open_regular_nofollow(path)
        if (
            before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != expected_mode
            or expected_size is not None
            and before.st_size != expected_size
        ):
            _runtime_failed()
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after_open = os.fstat(descriptor)
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except InstallError:
        raise
    except OSError:
        _runtime_failed()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent >= 0:
            os.close(parent)
    if (
        _metadata_snapshot(after_open) != _metadata_snapshot(before)
        or _metadata_snapshot(after) != _metadata_snapshot(before)
    ):
        _runtime_failed()
    value = digest.hexdigest()
    if expected_sha256 is not None and not hmac.compare_digest(value, expected_sha256):
        _runtime_failed()
    return _FileToken(path, _metadata_snapshot(before), value, expected_mode)


def _verify_executable(path: Path, *, expected_mode: int = 0o700) -> _FileToken:
    """验证 no-follow executable file 的 owner 与精确程序 mode。"""
    return _verify_private_file(path, expected_mode=expected_mode)


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


def _revalidate_link_token(token: _SymlinkToken) -> None:
    """重验 symlink pathname 仍绑定相同 metadata 与 target。"""
    parent = -1
    try:
        parent, name = _open_parent_nofollow(token.path)
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        target = os.readlink(name, dir_fd=parent)
    except OSError:
        _runtime_failed()
    finally:
        if parent >= 0:
            os.close(parent)
    if _metadata_snapshot(metadata) != token.snapshot or target != token.target:
        _runtime_failed()


def _inspect_wheel(path: Path, version: str) -> None:
    """从 no-follow wheel descriptor 校验 Name/Version 和 miniclaw console entry。"""
    descriptor = -1
    parent = -1
    try:
        python_version = _python_filename_version(version)
    except InstallError:
        _runtime_failed()
    try:
        descriptor, parent, name, before = _open_regular_nofollow(path)
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream, zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            _validate_wheel_infos(infos)
            expected_dist_info = f"miniclaw_agent-{python_version}.dist-info"
            metadata_names = [
                info.filename
                for info in infos
                if PurePosixPath(info.filename).parts == (expected_dist_info, "METADATA")
            ]
            entry_names = [
                info.filename
                for info in infos
                if PurePosixPath(info.filename).parts
                == (expected_dist_info, "entry_points.txt")
            ]
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
                or metadata.get("Version") != python_version
                or not parser.has_section("console_scripts")
                or parser.get("console_scripts", "miniclaw", fallback="")
                != "miniclaw.cli:main"
            ):
                _runtime_failed()
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except InstallError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeError, configparser.Error):
        _runtime_failed()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent >= 0:
            os.close(parent)
    if _metadata_snapshot(after) != _metadata_snapshot(before):
        _runtime_failed()


def _validate_wheel_infos(infos: list[zipfile.ZipInfo]) -> None:
    """校验全部 wheel members 的预算、类型、名称和 tree 拓扑。"""
    if len(infos) > _MAX_TREE_ENTRIES:
        _runtime_failed()
    seen: set[str] = set()
    files: set[str] = set()
    directories: set[str] = set()
    total_size = 0
    total_compressed = 0
    for info in infos:
        name = info.filename
        if not _safe_archive_name(name) or info.flag_bits & 0x1:
            _runtime_failed()
        key = name.rstrip("/").casefold()
        if key in seen:
            _runtime_failed()
        seen.add(key)
        parts = tuple(part.casefold() for part in PurePosixPath(name).parts)
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in files:
                _runtime_failed()
            directories.add(parent)
        unix_mode = info.external_attr >> 16
        file_type = stat.S_IFMT(unix_mode)
        is_directory = info.is_dir()
        if file_type not in ({0, stat.S_IFDIR} if is_directory else {0, stat.S_IFREG}):
            _runtime_failed()
        if is_directory:
            if key in files:
                _runtime_failed()
            directories.add(key)
            continue
        if key in directories:
            _runtime_failed()
        files.add(key)
        if (
            info.file_size < 0
            or info.compress_size < 0
            or info.file_size > _MAX_WHEEL_MEMBER_BYTES
            or info.file_size > 0
            and (
                info.compress_size == 0
                or info.file_size > info.compress_size * _MAX_WHEEL_RATIO
            )
        ):
            _runtime_failed()
        total_size += info.file_size
        total_compressed += info.compress_size
        if total_size > _MAX_WHEEL_BYTES:
            _runtime_failed()
    if total_size > 0 and (
        total_compressed == 0 or total_size > total_compressed * _MAX_WHEEL_RATIO
    ):
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


def _validate_source_tree(
    root: Path,
    required_executables: set[str],
    *,
    allow_internal_symlinks: bool = False,
    allow_public_read: bool = False,
) -> dict[str, tuple[tuple[int, ...], str | None]]:
    """完整扫描 safe-extracted tree，仅按需允许 root 内部相对 alias link。"""
    seen: set[str] = set()
    entries = 0
    total = 0
    directory_modes = {0o700, 0o755} if allow_public_read else {0o700}
    file_modes = {0o600, 0o644, 0o700, 0o755} if allow_public_read else {0o600, 0o700}
    root_before = _verify_tree_directory(root, directory_modes)
    manifest = {"": (_metadata_snapshot(root_before), None)}
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        _verify_tree_directory(current, directory_modes)
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
            if stat.S_ISLNK(metadata.st_mode):
                if not allow_internal_symlinks:
                    _runtime_failed()
                target = _safe_internal_symlink(root, path)
                manifest[relative] = (_metadata_snapshot(metadata), target)
            elif stat.S_ISDIR(metadata.st_mode):
                directory_metadata = _verify_tree_directory(path, directory_modes)
                manifest[relative] = (_metadata_snapshot(directory_metadata), None)
            elif stat.S_ISREG(metadata.st_mode):
                mode = stat.S_IMODE(metadata.st_mode)
                if mode not in file_modes or relative in required_executables and mode & 0o111 == 0:
                    _runtime_failed()
                token = _verify_private_file(path, expected_mode=mode)
                manifest[relative] = (token.snapshot, None)
                total += token.snapshot[6]
                if total > _MAX_TREE_BYTES:
                    _runtime_failed()
            else:
                _runtime_failed()
    if not required_executables.issubset(seen):
        _runtime_failed()
    root_after = _verify_tree_directory(root, directory_modes)
    if _metadata_snapshot(root_after) != _metadata_snapshot(root_before):
        _runtime_failed()
    return manifest


def _copy_verified_tree(
    source: Path,
    destination: Path,
    required: set[str],
    *,
    allow_internal_symlinks: bool = False,
    allow_public_read: bool = False,
    program_mode: int = 0o700,
) -> None:
    """复制 verified tree；Python 仅重建 root 内部 relative alias links。"""
    if program_mode not in {0o700, 0o755}:
        _runtime_failed()
    data_mode = 0o644 if program_mode == 0o755 else 0o600
    manifest = _validate_source_tree(
        source,
        required,
        allow_internal_symlinks=allow_internal_symlinks,
        allow_public_read=allow_public_read,
    )
    os.mkdir(destination, program_mode)
    for directory, names, files in os.walk(source, topdown=True, followlinks=False):
        source_directory = Path(directory)
        relative_directory = source_directory.relative_to(source)
        relative_directory_text = (
            "" if relative_directory == Path(".") else relative_directory.as_posix()
        )
        directory_metadata = _verify_tree_directory(
            source_directory,
            {0o700, 0o755} if allow_public_read else {0o700},
        )
        if manifest.get(relative_directory_text) != (
            _metadata_snapshot(directory_metadata),
            None,
        ):
            _runtime_failed()
        target_directory = destination / relative_directory
        directory_links: list[str] = []
        for name in sorted(names):
            source_path = source_directory / name
            if source_path.is_symlink():
                directory_links.append(name)
                target = _safe_internal_symlink(source, source_path)
                relative = source_path.relative_to(source).as_posix()
                if manifest.get(relative) != (
                    _metadata_snapshot(source_path.lstat()),
                    target,
                ):
                    _runtime_failed()
                os.symlink(target, target_directory / name)
            else:
                relative = source_path.relative_to(source).as_posix()
                child_metadata = _verify_tree_directory(
                    source_path,
                    {0o700, 0o755} if allow_public_read else {0o700},
                )
                if manifest.get(relative) != (_metadata_snapshot(child_metadata), None):
                    _runtime_failed()
                os.mkdir(target_directory / name, program_mode)
        names[:] = [name for name in names if name not in directory_links]
        for name in sorted(files):
            source_file = source_directory / name
            if source_file.is_symlink():
                target = _safe_internal_symlink(source, source_file)
                relative = source_file.relative_to(source).as_posix()
                if manifest.get(relative) != (
                    _metadata_snapshot(source_file.lstat()),
                    target,
                ):
                    _runtime_failed()
                os.symlink(target, target_directory / name)
                continue
            mode = stat.S_IMODE(source_file.lstat().st_mode)
            token = _verify_private_file(source_file, expected_mode=mode)
            relative = source_file.relative_to(source).as_posix()
            if manifest.get(relative) != (token.snapshot, None):
                _runtime_failed()
            destination_mode = program_mode if mode & 0o111 else data_mode
            _copy_verified_file(token, target_directory / name, destination_mode)
    if manifest != _validate_source_tree(
        source,
        required,
        allow_internal_symlinks=allow_internal_symlinks,
        allow_public_read=allow_public_read,
    ):
        _runtime_failed()


def _safe_internal_symlink(root: Path, path: Path) -> str:
    """验证 alias target 为 relative、non-dangling/cycle 且最终仍在同一 root。"""
    try:
        target = os.readlink(path)
        pure = PurePosixPath(target)
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        _runtime_failed()
    if (
        pure.is_absolute()
        or str(pure) != target
        or "\\" in target
        or unicodedata.normalize("NFC", target) != target
        or not target.isprintable()
        or len(target.encode("utf-8")) > 4096
        or not resolved.is_relative_to(resolved_root)
    ):
        _runtime_failed()
    return target


def _copy_verified_file(token: _FileToken, destination: Path, mode: int) -> None:
    """从 verified no-follow descriptor 向 O_EXCL regular file 复制并 fsync。"""
    source_descriptor = -1
    source_parent = -1
    destination_descriptor = -1
    try:
        _revalidate_token(token)
        source_descriptor, source_parent, source_name, source_metadata = _open_regular_nofollow(
            token.path
        )
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
        if (
            _metadata_snapshot(os.fstat(source_descriptor)) != token.snapshot
            or _metadata_snapshot(
                os.stat(source_name, dir_fd=source_parent, follow_symlinks=False)
            )
            != token.snapshot
        ):
            _runtime_failed()
        _revalidate_token(token)
    except InstallError:
        raise
    except OSError:
        _runtime_failed()
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if source_parent >= 0:
            os.close(source_parent)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)


def _harden_and_fsync_tree(
    root: Path,
    program_mode: int,
    *,
    executable_paths: tuple[str, ...] | None = None,
) -> None:
    """按显式 executable 闭包收敛 Runtime；未提供时仅用于 private venv 预处理。"""
    if program_mode not in {0o700, 0o755}:
        _runtime_failed()
    executable_set = (
        None
        if executable_paths is None
        else set(_validate_executable_paths(list(executable_paths)))
    )
    data_mode = 0o644 if program_mode == 0o755 else 0o600
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
                relative = path.relative_to(root).as_posix()
                is_program = (
                    bool(stat.S_IMODE(metadata.st_mode) & 0o111)
                    if executable_set is None
                    else relative in executable_set
                )
                mode = program_mode if is_program else data_mode
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        directory.chmod(program_mode)
        _fsync_directory(directory)


def _executable_manifest_bytes(paths: tuple[str, ...]) -> bytes:
    """序列化 canonical Runtime executable path 清单。"""
    if _validate_executable_paths(list(paths)) != paths:
        _runtime_failed()
    return (
        json.dumps(
            {"executables": list(paths)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode()


def _required_runtime_executables(root: Path) -> tuple[str, ...]:
    """验证四个 verified-source Runtime entrypoints 存在且初始权限安全可执行。"""
    for relative in _REQUIRED_RUNTIME_EXECUTABLES:
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            mode = stat.S_IMODE(path.lstat().st_mode)
        except OSError:
            _runtime_failed()
        if not mode & stat.S_IXUSR or mode & 0o022:
            _runtime_failed()
        _verify_executable(path, expected_mode=mode)
    return _REQUIRED_RUNTIME_EXECUTABLES


def _validate_executable_paths(values: object) -> tuple[str, ...]:
    """校验 executable 清单是排序、唯一、非 transient 的 relative paths。"""
    if type(values) is not list or len(values) > _MAX_TREE_ENTRIES:
        _runtime_failed()
    paths: list[str] = []
    for value in values:
        if type(value) is not str:
            _runtime_failed()
        pure = PurePosixPath(value)
        if (
            pure.is_absolute()
            or not pure.parts
            or str(pure) != value
            or any(not _safe_component(part) for part in pure.parts)
            or pure.parts[0] in _TRANSIENT_RUNTIME_NAMES
            or value in _RUNTIME_METADATA_PATHS
        ):
            _runtime_failed()
        paths.append(value)
    result = tuple(paths)
    if result != _REQUIRED_RUNTIME_EXECUTABLES:
        _runtime_failed()
    return result


def _load_runtime_executables(
    root: Path,
    receipt: RuntimeReceipt,
    *,
    data_mode: int,
) -> tuple[str, ...]:
    """读取 receipt-hash-bound executable path 清单。"""
    expected_sha256 = receipt.executables_sha256
    if type(expected_sha256) is not str:
        _runtime_failed()
    payload = _read_private_regular(
        root / _EXECUTABLES_FILENAME,
        os.geteuid(),
        _MAX_METADATA_BYTES,
        expected_mode=data_mode,
    )
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected_sha256):
        _runtime_failed()
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        _runtime_failed()
    if type(document) is not dict or set(document) != {"executables"}:
        _runtime_failed()
    return _validate_executable_paths(document["executables"])


def _verify_runtime_regular(path: Path, *, expected_mode: int) -> os.stat_result:
    """descriptor-bound 验证 Runtime regular file owner、mode、nlink 与稳定 metadata。"""
    descriptor = -1
    parent = -1
    try:
        descriptor, parent, name, before = _open_regular_nofollow(path)
        opened = os.fstat(descriptor)
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except InstallError:
        raise
    except OSError:
        _runtime_failed()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent >= 0:
            os.close(parent)
    if (
        before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != expected_mode
        or _metadata_snapshot(opened) != _metadata_snapshot(before)
        or _metadata_snapshot(after) != _metadata_snapshot(before)
    ):
        _runtime_failed()
    return before


def _verify_runtime_symlink(
    root: Path,
    path: Path,
    *,
    program_mode: int,
    data_mode: int,
    executable_paths: set[str],
) -> None:
    """验证 Runtime symlink lexical target、闭包、owner 与最终 target mode。"""
    symlink_parent = -1
    target_parent = -1
    target_descriptor = -1
    try:
        symlink_parent, symlink_name = _open_parent_nofollow(path)
        before = os.stat(symlink_name, dir_fd=symlink_parent, follow_symlinks=False)
        target = os.readlink(symlink_name, dir_fd=symlink_parent)
        pure = PurePosixPath(target)
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        target_parent, target_name = _open_parent_nofollow(resolved)
        target_before = os.stat(
            target_name,
            dir_fd=target_parent,
            follow_symlinks=False,
        )
        if not (
            stat.S_ISREG(target_before.st_mode)
            or stat.S_ISDIR(target_before.st_mode)
        ):
            _runtime_failed()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        if stat.S_ISDIR(target_before.st_mode):
            flags |= getattr(os, "O_DIRECTORY", 0)
        target_descriptor = os.open(target_name, flags, dir_fd=target_parent)
        target_metadata = os.fstat(target_descriptor)
        target_after = os.stat(
            target_name,
            dir_fd=target_parent,
            follow_symlinks=False,
        )
        resolved_after = path.resolve(strict=True)
        after = os.stat(symlink_name, dir_fd=symlink_parent, follow_symlinks=False)
    except (OSError, RuntimeError):
        _runtime_failed()
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if target_parent >= 0:
            os.close(target_parent)
        if symlink_parent >= 0:
            os.close(symlink_parent)
    if (
        not stat.S_ISLNK(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or _metadata_snapshot(after) != _metadata_snapshot(before)
        or pure.is_absolute()
        or not target
        or posixpath.normpath(target) != target
        or "\\" in target
        or unicodedata.normalize("NFC", target) != target
        or not target.isprintable()
        or len(target.encode("utf-8")) > 4096
        or not resolved.is_relative_to(resolved_root)
        or resolved_after != resolved
        or _metadata_snapshot(target_metadata) != _metadata_snapshot(target_before)
        or _metadata_snapshot(target_after) != _metadata_snapshot(target_before)
        or target_metadata.st_uid != os.geteuid()
    ):
        _runtime_failed()
    relative = resolved.relative_to(resolved_root).as_posix()
    target_mode = stat.S_IMODE(target_metadata.st_mode)
    if stat.S_ISDIR(target_metadata.st_mode):
        parent = path.parent.resolve(strict=True)
        if parent.is_relative_to(resolved) or target_mode != program_mode:
            _runtime_failed()
    elif stat.S_ISREG(target_metadata.st_mode):
        expected = program_mode if relative in executable_paths else data_mode
        if target_metadata.st_nlink != 1 or target_mode != expected:
            _runtime_failed()
    else:
        _runtime_failed()


def _verify_runtime_tree(
    root: Path,
    *,
    program_mode: int,
    expected_executables: tuple[str, ...] | None = None,
    allow_transient: bool = False,
) -> tuple[str, ...]:
    """完整验证 Runtime owner/mode/type/symlink，并返回已知 program executables。"""
    if program_mode not in {0o700, 0o755}:
        _runtime_failed()
    data_mode = 0o644 if program_mode == 0o755 else 0o600
    observed_program: set[str] = set()
    observed_all: set[str] = set()
    symlinks: list[Path] = []
    entries = 0
    total = 0
    _verify_directory(root, expected_mode=program_mode)
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        relative_directory = current.relative_to(root)
        transient = bool(
            relative_directory.parts
            and relative_directory.parts[0] in _TRANSIENT_RUNTIME_NAMES
        )
        if transient and not allow_transient:
            _runtime_failed()
        _verify_directory(current, expected_mode=program_mode)
        for name in sorted((*names, *files)):
            entries += 1
            if entries > _MAX_TREE_ENTRIES or not _safe_component(name):
                _runtime_failed()
            path = current / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if metadata.st_uid != os.geteuid():
                _runtime_failed()
            child_transient = relative.split("/", 1)[0] in _TRANSIENT_RUNTIME_NAMES
            if child_transient and not allow_transient:
                _runtime_failed()
            if stat.S_ISLNK(metadata.st_mode):
                symlinks.append(path)
            elif stat.S_ISDIR(metadata.st_mode):
                _verify_directory(path, expected_mode=program_mode)
            elif stat.S_ISREG(metadata.st_mode):
                mode = stat.S_IMODE(metadata.st_mode)
                if mode not in {data_mode, program_mode}:
                    _runtime_failed()
                if relative in _RUNTIME_METADATA_PATHS and mode != data_mode:
                    _runtime_failed()
                stable = _verify_runtime_regular(path, expected_mode=mode)
                total += stable.st_size
                if total > _MAX_TREE_BYTES:
                    _runtime_failed()
                if mode == program_mode:
                    observed_all.add(relative)
                    if not child_transient:
                        observed_program.add(relative)
            else:
                _runtime_failed()
    expected = tuple(sorted(observed_program))
    if expected_executables is not None and expected != expected_executables:
        _runtime_failed()
    for path in symlinks:
        _verify_runtime_symlink(
            root,
            path,
            program_mode=program_mode,
            data_mode=data_mode,
            executable_paths=observed_all,
        )
    return expected


def _publish_system_runtime(root: Path, executable_paths: tuple[str, ...]) -> None:
    """在 private root 后转换 program modes，并最后发布 0755 Runtime root。"""
    _verify_runtime_tree(
        root,
        program_mode=0o700,
        expected_executables=executable_paths,
    )
    executable_set = set(executable_paths)
    directories: list[Path] = []
    for directory, _names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        directories.append(current)
        for name in files:
            path = current / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _runtime_failed()
            relative = path.relative_to(root).as_posix()
            mode = 0o755 if relative in executable_set else 0o644
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        descriptor, metadata = _open_directory_nofollow(directory)
        try:
            if metadata.st_uid != os.geteuid():
                _runtime_failed()
            os.fchmod(descriptor, 0o755)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _verify_runtime_tree(
        root,
        program_mode=0o755,
        expected_executables=executable_paths,
    )


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


def _verify_runtime_directory(
    path: Path,
    receipt: RuntimeReceipt,
    *,
    program_mode: int = 0o700,
) -> None:
    """重验完整 immutable Runtime tree 与 receipt/executable bindings。"""
    data_mode = 0o644 if program_mode == 0o755 else 0o600
    try:
        stored = RuntimeReceipt.load(
            path / "install-receipt.json", expected_mode=data_mode
        )
        if receipt.executables_sha256 is None:
            if _lexists(path / _EXECUTABLES_FILENAME):
                _runtime_failed()
            executable_paths = None
        else:
            executable_paths = _load_runtime_executables(
                path,
                receipt,
                data_mode=data_mode,
            )
        _verify_runtime_tree(
            path,
            program_mode=program_mode,
            expected_executables=executable_paths,
        )
    except InstallError:
        raise
    except OSError:
        _runtime_failed()
    if path.name != receipt.version or stored != receipt:
        _runtime_failed()


def _verified_runtime_target(layout: InstallLayout, target: str) -> RuntimeReceipt:
    """验证relative Runtime target的目录、receipt、manifest及artifact绑定。"""
    try:
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
        program_mode, data_mode = _runtime_modes(layout)
        receipt = RuntimeReceipt.load(
            runtime / "install-receipt.json", expected_mode=data_mode
        )
        _verify_runtime_directory(runtime, receipt, program_mode=program_mode)
        manifest = ReleaseManifest.from_bytes(
            _read_private_regular(
                runtime / "release-manifest.json",
                os.geteuid(),
                _MAX_METADATA_BYTES,
                expected_mode=data_mode,
            )
        )
        bindings = {
            "wheel": receipt.wheel_sha256,
            "requirements": receipt.requirements_sha256,
            "node": receipt.node_sha256,
            "tui": receipt.tui_sha256,
            "installer": receipt.installer_sha256,
        }
        if (
            manifest.version != receipt.version
            or manifest.git_commit != receipt.git_commit
            or any(
                not any(
                    artifact.kind == kind and artifact.sha256 == digest
                    for artifact in manifest.artifacts
                )
                for kind, digest in bindings.items()
            )
            or not any(
                artifact.kind == "node"
                and artifact.component_version == receipt.node_version
                for artifact in manifest.artifacts
            )
            or not any(
                artifact.kind == "tui" and artifact.component_version == receipt.tui_version
                for artifact in manifest.artifacts
            )
        ):
            _activation_failed()
        return receipt
    except InstallError as error:
        if error.code == "activation_failed":
            raise
        _activation_failed()
    except (OSError, ValueError, TypeError):
        _activation_failed()


def _validated_current(layout: InstallLayout) -> str | None:
    """返回 validated relative current target；不存在时返回 None。"""
    return _validated_runtime_link(layout, layout.current)


def _runtime_references(layout: InstallLayout) -> set[str]:
    """读取受管 Runtime 引用。

    Args:
        layout: 与待清理版本绑定的 validated layout。

    Returns:
        `current`、`current.next` 与根 receipt 中的全部 Runtime 相对路径。

    Raises:
        InstallError: 任一存在的引用不是完整、稳定且受管的事实。
    """
    references = {
        reference
        for reference in (
            _validated_current(layout),
            _validated_runtime_link(layout, layout.current.with_name("current.next")),
        )
        if reference is not None
    }
    if _lexists(layout.receipt):
        install_receipt = InstallReceipt.load(layout.receipt)
        references.add(install_receipt.current_runtime)
        if install_receipt.previous_runtime is not None:
            references.add(install_receipt.previous_runtime)
    return references


def _validated_runtime_link(layout: InstallLayout, path: Path) -> str | None:
    """no-follow 验证一个受管 Runtime reference symlink。

    Args:
        layout: 用于解析受管 Runtime target 的 validated layout。
        path: 待检查的 symlink 路径。

    Returns:
        不存在时返回 None；否则返回完整验证后的相对 Runtime target。

    Raises:
        InstallError: 路径、symlink metadata、target tree 或稳定性不可信。
    """
    if not _lexists(path):
        return None
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            _activation_failed()
        target = os.readlink(path)
        _verified_runtime_target(layout, target)
        after = path.lstat()
        if _metadata_snapshot(after) != _metadata_snapshot(metadata):
            _activation_failed()
        return target
    except InstallError:
        raise
    except OSError:
        _activation_failed()


def _symlink_identity(path: Path, expected_target: str) -> tuple[int, int]:
    """读取 owner-only symlink identity，并绑定预期 relative target。"""
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or os.readlink(path) != expected_target
        ):
            _activation_failed()
        return metadata.st_dev, metadata.st_ino
    except OSError:
        _activation_failed()


def _same_symlink(path: Path, identity: tuple[int, int], expected_target: str) -> bool:
    """判断 pathname 是否仍绑定同一 symlink inode 与 target。"""
    try:
        metadata = path.lstat()
        return (
            stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and (metadata.st_dev, metadata.st_ino) == identity
            and os.readlink(path) == expected_target
        )
    except OSError:
        return False


def _rename_exchange(source: Path, destination: Path) -> None:
    """用 Linux/macOS native exchange 原子交换两个 pathname。"""
    library = ctypes.CDLL(None, use_errno=True)
    encoded_source = os.fsencode(source)
    encoded_destination = os.fsencode(destination)
    if sys.platform == "darwin":
        try:
            operation = library.renamex_np
        except AttributeError as error:
            raise OSError(errno.ENOTSUP, "atomic exchange unavailable") from error
        operation.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        operation.restype = ctypes.c_int
        result = operation(encoded_source, encoded_destination, 0x00000002)
    elif sys.platform.startswith("linux"):
        try:
            operation = library.renameat2
        except AttributeError as error:
            raise OSError(errno.ENOTSUP, "atomic exchange unavailable") from error
        operation.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        operation.restype = ctypes.c_int
        result = operation(-100, encoded_source, -100, encoded_destination, 2)
    else:
        raise OSError(errno.ENOTSUP, "atomic exchange unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _recover_current_next(
    layout: InstallLayout,
    next_link: Path,
    desired_target: str,
    current_target: str | None,
) -> None:
    """验证并quarantine可恢复current.next；foreign residue继续fail closed。"""
    state = _owned_symlink_state(next_link)
    if state is None:
        _activation_failed()
    identity, residue_target = state
    _verified_runtime_target(layout, residue_target)
    if (
        current_target is None
        and residue_target != desired_target
        or current_target is not None
        and current_target != desired_target
        and residue_target != desired_target
    ):
        _activation_failed()
    if not _retire_current_next(next_link, identity, residue_target):
        _activation_failed()


def _retire_current_next(
    next_link: Path,
    identity: tuple[int, int],
    target: str,
) -> bool:
    """先原子移走verified reserved link并fsync，再best-effort清理private residue。"""
    if not _same_symlink(next_link, identity, target):
        return False
    private: Path | None = None
    for attempt in range(16):
        candidate = next_link.with_name(
            f".current.next.retired-{os.getpid()}-{time.monotonic_ns()}-{attempt}"
        )
        try:
            _rename_no_replace(next_link, candidate)
        except OSError as error:
            if error.errno == errno.EEXIST:
                continue
            return False
        private = candidate
        break
    if private is None or not _same_symlink(private, identity, target):
        return False
    try:
        _fsync_directory(next_link.parent)
    except OSError:
        return False
    _unlink_same_inode(private, identity)
    if not _lexists(private):
        try:
            _fsync_directory(next_link.parent)
        except OSError:
            pass
    return True


def _rollback_activation(
    layout: InstallLayout,
    next_link: Path | None,
    next_identity: tuple[int, int] | None,
    *,
    swapped: bool,
    published_absent: bool,
) -> None:
    """仅在双方 inode 未漂移时恢复 activation 前 namespace。"""
    if next_link is None or next_identity is None:
        return
    try:
        if swapped and _same_symlink(
            layout.current, next_identity, f"runtimes/{layout.runtime.name}"
        ):
            other = _owned_symlink_state(next_link)
            if other is not None and _same_symlink(next_link, other[0], other[1]):
                _rename_exchange(next_link, layout.current)
                _fsync_directory(layout.program_prefix)
        elif (
            published_absent
            and not _lexists(next_link)
            and _same_symlink(
                layout.current, next_identity, f"runtimes/{layout.runtime.name}"
            )
        ):
            _rename_no_replace(layout.current, next_link)
            _fsync_directory(layout.program_prefix)
        _unlink_same_inode(next_link, next_identity)
        _fsync_directory(layout.program_prefix)
    except (InstallError, OSError):
        return


def _owned_symlink_state(path: Path) -> tuple[tuple[int, int], str] | None:
    """读取 owner-only symlink 的稳定 identity/target；漂移或缺失返回 None。"""
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            return None
        identity = metadata.st_dev, metadata.st_ino
        target = os.readlink(path)
        after = path.lstat()
        if (after.st_dev, after.st_ino) != identity:
            return None
        return identity, target
    except OSError:
        return None


def _revoke_tree_root_private(path: Path, identity: tuple[int, int]) -> bool:
    """将仍由本轮持有 inode 的失败树根目录 descriptor-bound 降权为 0700。"""
    descriptor = -1
    try:
        descriptor, metadata = _open_directory_nofollow(path)
        if (
            (metadata.st_dev, metadata.st_ino) != identity
            or metadata.st_uid != os.geteuid()
        ):
            return False
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        pathname = path.lstat()
        return (
            (after.st_dev, after.st_ino) == identity
            and (pathname.st_dev, pathname.st_ino) == identity
            and after.st_uid == os.geteuid()
            and pathname.st_uid == os.geteuid()
            and stat.S_IMODE(after.st_mode) == 0o700
            and stat.S_IMODE(pathname.st_mode) == 0o700
        )
    except (InstallError, OSError):
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _secure_failed_tree(path: Path, identity: tuple[int, int]) -> None:
    """先撤回失败树公开可遍历权限，再 best-effort 原子隔离并清理。"""
    _revoke_tree_root_private(path, identity)
    _quarantine_and_remove(path, identity)


def _quarantine_and_remove(path: Path, identity: tuple[int, int]) -> bool:
    """只清理由本轮持有 inode 的目录，并先从公开 namespace 隔离。"""
    if not _lexists(path):
        return True
    quarantine = path.with_name(f".{path.name}.cleanup-{os.getpid()}")
    try:
        if _lexists(quarantine):
            return False
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity or not stat.S_ISDIR(metadata.st_mode):
            return False
        _rename_no_replace(path, quarantine)
        moved = quarantine.lstat()
        if (moved.st_dev, moved.st_ino) != identity:
            return False
        _fsync_directory(path.parent)
        _remove_owned_tree(quarantine)
        _fsync_directory(path.parent)
        return True
    except (InstallError, OSError):
        return False


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


def _directory_identity(path: Path, *, expected_mode: int = 0o700) -> tuple[int, int]:
    """返回 owner 且 mode 精确的 directory device/inode token。"""
    metadata = _verify_directory(path, expected_mode=expected_mode)
    return metadata.st_dev, metadata.st_ino


def _read_private_regular(
    path: Path,
    uid: int,
    limit: int,
    *,
    expected_mode: int = 0o600,
) -> bytes:
    """bounded no-follow 读取 owner 与 mode 精确的 regular file。"""
    token = _verify_private_file(path, expected_mode=expected_mode)
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


def _drain_ready(
    descriptor: int,
    target: bytearray,
    overflow: list[bool],
    index: int,
    deadline: float,
) -> bool:
    """drain 一个 nonblocking ready pipe，并报告它是否仍打开。"""
    while True:
        if time.monotonic() >= deadline:
            return True
        try:
            chunk = os.read(descriptor, 8192)
        except BlockingIOError:
            return True
        if not chunk:
            return False
        remaining = _MAX_OUTPUT_BYTES - len(target)
        if remaining > 0:
            target.extend(chunk[:remaining])
        if len(chunk) > remaining:
            overflow[index] = True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """依次 TERM、KILL isolated process group，并 bounded reap direct child。"""
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signal_number)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                try:
                    process.send_signal(signal_number)
                except OSError:
                    _runtime_failed()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        _runtime_failed()


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
