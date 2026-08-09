"""构建 hash-locked immutable Runtime，并原子切换 stable launcher target。"""

from __future__ import annotations

import configparser
import ctypes
import errno
import hashlib
import hmac
import json
import os
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

from miniclaw.install.layout import InstallLayout, _program_mode
from miniclaw.install.models import Artifact, InstallError, PlatformKey, ReleaseManifest
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
            or type(self.python_version) is not str
            or _PYTHON_VERSION.fullmatch(self.python_version) is None
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
            )
            _copy_verified_tree(inputs.node, inputs.layout.staging / "node", {"bin/node"})
            _copy_verified_tree(inputs.tui, inputs.layout.staging / "tui", set())
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
            _harden_and_fsync_tree(inputs.layout.staging / "venv")
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
            staging_python_version = self.smoke(inputs)
            receipt = _receipt_for(inputs, artifacts, staging_python_version)
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
            _repair_final_venv(inputs.layout)
            final_python_version = self.smoke(inputs, runtime=inputs.layout.runtime)
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
            _harden_and_fsync_tree(inputs.layout.runtime)
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

    def smoke(self, inputs: RuntimeInputs, *, runtime: Path | None = None) -> str:
        """执行 Python version、install-smoke 与 checkout-independent TUI smoke。

        Args:
            inputs: 当前 staging 所属 strict Runtime inputs。

        Raises:
            InstallError: executable、输出、版本、Channel import 或 TUI smoke 失败。
        """
        if type(inputs) is not RuntimeInputs:
            _runtime_failed()
        root = inputs.layout.staging if runtime is None else runtime
        if root not in {inputs.layout.staging, inputs.layout.runtime}:
            _runtime_failed()
        python = root / "venv" / "bin" / "python"
        node = root / "node" / "bin" / "node"
        tui = root / "tui" / "dist" / "main.js"
        canonical_python = root / "python" / "bin" / "python3.12"
        python_token, python_link = _verify_internal_python_link(
            python,
            root,
            canonical_python,
            require_relative=runtime is not None,
        )
        config_token = _verify_private_file(root / "venv" / "pyvenv.cfg", expected_mode=0o600)
        node_token = _verify_executable(node)
        tui_token = _verify_private_file(tui, expected_mode=0o600)
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
        if swapped and old_identity is not None:
            _unlink_same_inode(next_link, old_identity)
            try:
                _fsync_directory(layout.program_prefix)
            except OSError:
                pass
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
    executable = _verify_executable(canonical)
    return executable, _SymlinkToken(path, _metadata_snapshot(metadata), target)


def _repair_final_venv(layout: InstallLayout) -> None:
    """把已发布 venv 的 interpreter link/config 改为 final Runtime 内部路径。"""
    runtime = layout.runtime
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
        expected_old = str(layout.staging / "python" / "bin" / "python3.12")
        if old_target != expected_old:
            _runtime_failed()
        original = _read_private_regular(config, os.geteuid(), _MAX_METADATA_BYTES).decode("utf-8")
        lines = original.splitlines()
        if sum(line.startswith("home = ") for line in lines) != 1:
            _runtime_failed()
        updated = "\n".join(
            f"home = {runtime / 'python' / 'bin'}" if line.startswith("home = ") else line
            for line in lines
        ) + "\n"
        _write_exclusive(temporary_config, updated.encode(), 0o600)
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
        )
        persisted = _read_private_regular(config, os.geteuid(), _MAX_METADATA_BYTES)
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
        descriptor, parent, name, before = _open_regular_nofollow(path)
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream, zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            _validate_wheel_infos(infos)
            expected_dist_info = f"miniclaw_agent-{version}.dist-info"
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
                or metadata.get("Version") != version
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
) -> None:
    """复制 verified tree；Python 仅重建 root 内部 relative alias links。"""
    manifest = _validate_source_tree(
        source,
        required,
        allow_internal_symlinks=allow_internal_symlinks,
        allow_public_read=allow_public_read,
    )
    os.mkdir(destination, 0o700)
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
                os.mkdir(target_directory / name, 0o700)
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
            destination_mode = 0o700 if mode & 0o111 else 0o600
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
