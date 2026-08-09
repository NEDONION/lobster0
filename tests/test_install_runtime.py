"""验证 hash-locked managed Runtime 的构建、smoke 与原子激活。"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from miniclaw.install import layout as layout_module
from miniclaw.install import runtime as runtime_module
from miniclaw.install.layout import InstallLayout
from miniclaw.install.models import (
    Artifact,
    InstallError,
    NodePolicy,
    NodeRange,
    PlatformKey,
    ReleaseManifest,
)
from miniclaw.install.receipt import InstallReceipt
from miniclaw.install.runtime import (
    CommandResult,
    RuntimeBuilder,
    RuntimeInputs,
    RuntimeReceipt,
    activate_runtime,
    retain_current_and_previous,
)


class FakeRunner:
    """记录 exact subprocess contract，并返回可注入的离线 smoke 结果。"""

    def __init__(self) -> None:
        """初始化调用记录和默认成功输出。"""
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.fail_token: str | None = None
        self.version = b"miniclaw 0.7.0\n"
        self.node_version = b"v24.18.0\n"
        self.python_version = (3, 12, 11)
        self.python_base_prefix: Path | None = None
        self.python_executable: Path | None = None
        self.install_smoke = b'{"status":"ok","version":"0.7.0"}\n'
        self.tui_smoke = b'{"component":"pi-tui","status":"ok","version":"0.7.0"}\n'

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        """记录 argv/env，并为 venv、版本和 smoke 命令返回 deterministic 结果。"""
        del timeout
        self.calls.append((argv, dict(env)))
        if self.fail_token is not None and self.fail_token in argv:
            return CommandResult(7, b"", b"secret-sentinel")
        if len(argv) >= 2 and argv[1] == "venv":
            python = Path(argv[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            internal = Path(argv[argv.index("--python") + 1])
            python.symlink_to(internal)
            (python.parent / "python3").symlink_to("python")
            (python.parent / "python3.12").symlink_to("python")
            (python.parents[1] / "pyvenv.cfg").write_text(
                f"home = {internal.parent}\nversion_info = 3.12.11\nrelocatable = true\n",
                encoding="utf-8",
            )
            (python.parents[1] / "pyvenv.cfg").chmod(0o600)
            (python.parent / "miniclaw").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (python.parent / "miniclaw").chmod(0o755)
        if argv[-1:] == ("--version",) and "/node/" in argv[0]:
            return CommandResult(0, self.node_version, b"")
        if len(argv) >= 3 and argv[-2] == "-c":
            runtime = Path(argv[0]).parents[2]
            return CommandResult(
                0,
                (
                    json.dumps(
                        {
                            "base_prefix": str(self.python_base_prefix or runtime / "python"),
                            "executable": str(
                                self.python_executable
                                or runtime / "python" / "bin" / "python3.12"
                            ),
                            "version": list(self.python_version),
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
                b"",
            )
        if argv[-2:] == ("miniclaw", "--version"):
            return CommandResult(0, self.version, b"")
        if argv[-3:] == ("miniclaw", "install-smoke", "--json"):
            return CommandResult(0, self.install_smoke, b"")
        if argv[-1:] == ("--smoke",):
            return CommandResult(0, self.tui_smoke, b"")
        return CommandResult(0, b"", b"")


class InstallRuntimeTests(unittest.TestCase):
    """覆盖 Runtime 构建的信任边界、原子切换和 retention。"""

    def setUp(self) -> None:
        """创建 owner-only layout 与完整五 artifact Release fixture。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.layout = InstallLayout._build(
            self.home / "program",
            self.home / "state",
            self.home / ".local" / "bin" / "miniclaw",
            "0.7.0",
        )
        self.sources = self.root / "sources"
        self.sources.mkdir(mode=0o700)
        self.wheel = self.sources / "miniclaw_agent-0.7.0-py3-none-any.whl"
        self._write_wheel()
        self.requirements = self.sources / "requirements-all.lock"
        self._write_private(
            self.requirements,
            b"httpx==0.28.1 --hash=sha256:" + b"1" * 64 + b"\n",
        )
        self.installer = self.sources / "miniclaw-installer.pyz"
        self._write_private(self.installer, b"verified installer")
        self.node = self.sources / "node"
        self.node.mkdir(mode=0o700)
        (self.node / "bin").mkdir(mode=0o700)
        self._write_private(self.node / "bin" / "node", b"#!/bin/sh\nexit 0\n", 0o700)
        self._write_private(self.node / "README.md", b"verified node data\n")
        self.tui = self.sources / "tui"
        self.tui.mkdir(mode=0o700)
        (self.tui / "dist").mkdir(mode=0o700)
        self._write_private(self.tui / "dist" / "main.js", b"// verified tui\n")
        self.uv = self.sources / "uv"
        self._write_private(self.uv, b"#!/bin/sh\nexit 0\n", 0o700)
        self.managed_python = self.sources / "managed-python"
        self.managed_python.mkdir(mode=0o700)
        (self.managed_python / "bin").mkdir(mode=0o700)
        managed_executable = Path(sys.base_prefix) / "bin" / "python3.12"
        shutil.copyfile(managed_executable, self.managed_python / "bin" / "python3.12")
        (self.managed_python / "bin" / "python3.12").chmod(0o700)
        (self.managed_python / "bin" / "python3").symlink_to("python3.12")
        (self.managed_python / "lib").mkdir(mode=0o700)
        self._write_private(self.managed_python / "lib" / "python-data.txt", b"python data\n")
        self.managed_python_executable = self.managed_python / "bin" / "python3.12"
        self.platform = PlatformKey("linux", "x86_64")
        self.manifest = self._manifest()
        self.inputs = RuntimeInputs(
            layout=self.layout,
            manifest=self.manifest,
            platform=self.platform,
            wheel=self.wheel,
            requirements=self.requirements,
            node=self.node,
            tui=self.tui,
            installer=self.installer,
            uv=self.uv,
            managed_python_root=self.managed_python,
            managed_python_executable=self.managed_python_executable,
        )
        self.runner = FakeRunner()

    def _write_private(self, path: Path, payload: bytes, mode: int = 0o600) -> None:
        """写入测试用 owner-only regular file。"""
        path.write_bytes(payload)
        path.chmod(mode)

    def _write_wheel(
        self,
        *,
        name: str = "miniclaw-agent",
        version: str = "0.7.0",
        entry: str | None = "miniclaw = miniclaw.cli:main",
        path: Path | None = None,
    ) -> None:
        """写入只含 metadata/entry point 的最小 wheel fixture。"""
        wheel = self.wheel if path is None else path
        dist_version = wheel.name.removeprefix("miniclaw_agent-").removesuffix(
            "-py3-none-any.whl"
        )
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                f"miniclaw_agent-{dist_version}.dist-info/METADATA",
                f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
            )
            if entry is not None:
                archive.writestr(
                    f"miniclaw_agent-{dist_version}.dist-info/entry_points.txt",
                    f"[console_scripts]\n{entry}\n",
                )
        wheel.chmod(0o600)

    def _manifest_for_version(self, version: str, wheel: Path | None = None) -> ReleaseManifest:
        """把基础 fixture 绑定到另一 Release SemVer 与 wheel。"""
        artifacts: list[Artifact] = []
        for artifact in self.manifest.artifacts:
            filename = artifact.filename
            component_version = artifact.component_version
            sha256 = artifact.sha256
            size = artifact.size
            if artifact.kind == "wheel":
                filename = (
                    wheel.name
                    if wheel is not None
                    else f"miniclaw_agent-{version}-py3-none-any.whl"
                )
                if wheel is not None:
                    sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
                    size = wheel.stat().st_size
            elif artifact.kind == "tui":
                filename = f"miniclaw-tui-{version}-linux-x86_64.tar.gz"
            if artifact.kind != "node":
                component_version = version
            artifacts.append(
                replace(
                    artifact,
                    filename=filename,
                    url=(
                        "https://github.com/NEDONION/miniclaw/releases/"
                        f"download/v{version}/{filename}"
                    ),
                    sha256=sha256,
                    size=size,
                    component_version=component_version,
                )
            )
        return replace(self.manifest, version=version, artifacts=tuple(artifacts))

    def _artifact(
        self,
        kind: str,
        path: Path,
        *,
        digest: str | None = None,
    ) -> Artifact:
        """从 fixture path 构造 manifest-bound artifact。"""
        universal = PlatformKey("any", "any")
        is_node = kind == "node"
        component_version = "24.18.0" if is_node else "0.7.0"
        filename = {
            "wheel": self.wheel.name,
            "requirements": self.requirements.name,
            "node": "miniclaw-node-24.18.0-linux-x86_64.tar.gz",
            "tui": "miniclaw-tui-0.7.0-linux-x86_64.tar.gz",
            "installer": self.installer.name,
        }[kind]
        value = digest or hashlib.sha256(path.read_bytes()).hexdigest()
        return Artifact(
            kind=kind,  # type: ignore[arg-type]
            filename=filename,
            url=f"https://github.com/NEDONION/miniclaw/releases/download/v0.7.0/{filename}",
            sha256=value,
            size=path.stat().st_size if path.is_file() else 1,
            media_type={
                "wheel": "application/zip",
                "requirements": "text/plain",
                "node": "application/gzip",
                "tui": "application/gzip",
                "installer": "application/zip",
            }[kind],
            platform=self.platform if kind in {"node", "tui"} else universal,
            component_version=component_version,
            source_repository=(
                "https://github.com/nodejs/node"
                if is_node
                else "https://github.com/NEDONION/miniclaw"
            ),
            license_ref="MIT",
            upstream_sha256="e" * 64 if is_node else None,
        )

    def _manifest(self) -> ReleaseManifest:
        """返回与当前 source files hash 完全绑定的 Release。"""
        return ReleaseManifest(
            schema_version=1,
            product="miniclaw",
            version="0.7.0",
            git_commit="0123456789abcdef0123456789abcdef01234567",
            python="3.12",
            node=NodePolicy(
                (24, 18, 0),
                (
                    NodeRange((22, 22, 3), (23, 0, 0)),
                    NodeRange((24, 15, 0), (25, 0, 0)),
                ),
            ),
            artifacts=(
                self._artifact("wheel", self.wheel),
                self._artifact("requirements", self.requirements),
                self._artifact("node", self.node, digest="d" * 64),
                self._artifact("tui", self.tui, digest="b" * 64),
                self._artifact("installer", self.installer),
            ),
            supported_platforms=(
                PlatformKey("linux", "x86_64"),
                PlatformKey("linux", "arm64"),
                PlatformKey("macos", "x86_64"),
                PlatformKey("macos", "arm64"),
            ),
            features=("agent", "tools", "tui", "feishu", "telegram", "discord"),
            database_schema=5,
            minimum_readable_schema=5,
        )

    def test_exported_lock_has_only_exact_hashed_logical_requirements(self) -> None:
        """每个非注释 logical requirement 都必须 exact/direct 且带 SHA-256。"""
        logical: list[str] = []
        current = ""
        for raw in Path("requirements-all.lock").read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            current += (" " if current else "") + stripped.removesuffix("\\").strip()
            if not stripped.endswith("\\"):
                logical.append(current)
                current = ""
        self.assertFalse(current)
        self.assertGreater(len(logical), 20)
        for requirement in logical:
            with self.subTest(requirement=requirement.split()[0]):
                self.assertTrue("==" in requirement or " @ " in requirement)
                self.assertIn("--hash=sha256:", requirement)

    def test_build_uses_exact_hash_locked_order_environment_and_receipt(self) -> None:
        """依赖、wheel 与三条 smoke 必须按固定 argv 和 closed-world env 执行。"""
        receipt = RuntimeBuilder(self.runner).build(self.inputs)

        python = self.layout.staging / "venv" / "bin" / "python"
        internal_python = self.layout.staging / "python" / "bin" / "python3.12"
        private_inputs = self.layout.staging / ".inputs"
        node = self.layout.staging / "node" / "bin" / "node"
        tui = self.layout.staging / "tui" / "dist" / "main.js"
        argvs = [call[0] for call in self.runner.calls]
        self.assertEqual(
            argvs[:3],
            [
                (
                    str(private_inputs / "uv"),
                    "venv",
                    "--relocatable",
                    "--python",
                    str(internal_python),
                    "--no-python-downloads",
                    str(self.layout.staging / "venv"),
                ),
                (
                    str(private_inputs / "uv"),
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--require-hashes",
                    "-r",
                    str(private_inputs / "requirements-all.lock"),
                ),
                (
                    str(private_inputs / "uv"),
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    str(private_inputs / self.wheel.name),
                ),
            ],
        )
        self.assertIn((str(node), "--version"), argvs)
        self.assertIn((str(node), str(tui), "--smoke"), argvs)
        self.assertIn(
            (str(self.layout.runtime / "node" / "bin" / "node"), "--version"),
            argvs,
        )
        expected_env = {
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "UV_NO_CONFIG": "1",
            "UV_NO_ENV_FILE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
        for argv, env in self.runner.calls:
            environment_root = (
                self.layout.runtime
                if argv[0].startswith(str(self.layout.runtime))
                else self.layout.staging
            )
            self.assertEqual(
                env,
                expected_env
                | {
                    "HOME": str(environment_root / ".home"),
                    "TMPDIR": str(environment_root / ".tmp"),
                    "UV_CACHE_DIR": str(environment_root / ".uv-cache"),
                },
            )
        self.assertEqual(
            receipt,
            RuntimeReceipt(
                version="0.7.0",
                git_commit=self.manifest.git_commit,
                runtime_relative="runtimes/0.7.0",
                python_version="3.12.11",
                node_version="24.18.0",
                tui_version="0.7.0",
                wheel_sha256=self.manifest.require_artifact("wheel", self.platform).sha256,
                requirements_sha256=self.manifest.require_artifact(
                    "requirements", self.platform
                ).sha256,
                node_sha256="d" * 64,
                tui_sha256="b" * 64,
                installer_sha256=self.manifest.require_artifact(
                    "installer", self.platform
                ).sha256,
            ),
        )
        self.assertTrue(self.layout.runtime.is_dir())
        self.assertFalse(self.layout.staging.exists())
        self.assertEqual(stat.S_IMODE((self.layout.runtime / "node").stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.layout.runtime / "tui" / "dist" / "main.js").stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE((self.layout.runtime / "miniclaw-installer.pyz").stat().st_mode),
            0o700,
        )
        self.assertEqual(
            RuntimeReceipt.load(self.layout.runtime / "install-receipt.json"), receipt
        )
        manifest = json.loads(
            (self.layout.runtime / "release-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], "0.7.0")
        self.assertEqual(manifest["git_commit"], self.manifest.git_commit)
        self.assertEqual(
            os.readlink(self.layout.runtime / "venv" / "bin" / "python"),
            "../../python/bin/python3.12",
        )
        self.assertEqual(
            os.readlink(self.layout.runtime / "python" / "bin" / "python3"),
            "python3.12",
        )
        self.assertIn(
            f"home = {self.layout.runtime / 'python' / 'bin'}",
            (self.layout.runtime / "venv" / "pyvenv.cfg").read_text(encoding="utf-8"),
        )

    def test_system_runtime_is_root_owned_public_program_data_and_activates(self) -> None:
        """system Runtime 的 0700/0600 发布树会让目标用户无法启动。"""
        system_prefix = self.root / "usr-local-lib" / "miniclaw"
        system_prefix.parent.mkdir(mode=0o755)
        system_prefix.parent.chmod(0o755)
        system_command = self.root / "usr-local-bin" / "miniclaw"
        state_home = self.home / "system-state"
        state_home.mkdir(mode=0o700)
        secret = state_home / "secrets.env"
        self._write_private(secret, b"MINICLAW_TEST_SECRET=preserved\n")
        with (
            mock.patch.object(layout_module, "_SYSTEM_PREFIX", system_prefix),
            mock.patch.object(layout_module, "_SYSTEM_COMMAND", system_command),
            mock.patch.object(layout_module, "_validate_system_prefix"),
        ):
            layout = InstallLayout._build(
                system_prefix,
                state_home,
                system_command,
                "0.7.0",
                owner_uid=os.geteuid(),
                user_home=self.home,
            )
            inputs = replace(self.inputs, layout=layout)
            with mock.patch.object(
                runtime_module, "_is_root_builder", return_value=True, create=True
            ):
                receipt = RuntimeBuilder(self.runner).build(inputs)
                activate_runtime(layout, receipt)

            executable_paths = {
                layout.runtime / "miniclaw-installer.pyz",
                layout.runtime / "node" / "bin" / "node",
                layout.runtime / "python" / "bin" / "python3.12",
                layout.runtime / "venv" / "bin" / "miniclaw",
            }
            for directory, names, files in os.walk(
                layout.runtime, topdown=True, followlinks=False
            ):
                current = Path(directory)
                metadata = current.lstat()
                self.assertEqual(metadata.st_uid, os.geteuid(), current)
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o755, current)
                self.assertEqual(stat.S_IMODE(metadata.st_mode) & 0o005, 0o005, current)
                for name in (*names, *files):
                    path = current / name
                    item = path.lstat()
                    self.assertEqual(item.st_uid, os.geteuid(), path)
                    if stat.S_ISLNK(item.st_mode):
                        self.assertFalse(Path(os.readlink(path)).is_absolute(), path)
                        self.assertTrue(
                            path.resolve(strict=True).is_relative_to(
                                layout.runtime.resolve(strict=True)
                            ),
                            path,
                        )
                        continue
                    if stat.S_ISREG(item.st_mode):
                        expected = 0o755 if path in executable_paths else 0o644
                        mode = stat.S_IMODE(item.st_mode)
                        self.assertEqual(mode, expected, path)
                        self.assertEqual(mode & 0o022, 0, path)
                        self.assertEqual(mode & 0o004, 0o004, path)
                        if path in executable_paths:
                            self.assertEqual(mode & 0o001, 0o001, path)

            self.assertEqual(os.readlink(layout.current), "runtimes/0.7.0")
            self.assertEqual(
                RuntimeReceipt.load(
                    layout.runtime / "install-receipt.json", expected_mode=0o644
                ),
                receipt,
            )
            self.assertEqual(stat.S_IMODE(state_home.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
            self.assertEqual(secret.read_bytes(), b"MINICLAW_TEST_SECRET=preserved\n")

    def test_user_runtime_keeps_exact_private_modes(self) -> None:
        """system 权限分支不得把默认用户 Runtime 扩大成 public-readable。"""
        RuntimeBuilder(self.runner).build(self.inputs)
        executable_paths = {
            self.layout.runtime / "miniclaw-installer.pyz",
            self.layout.runtime / "node" / "bin" / "node",
            self.layout.runtime / "python" / "bin" / "python3.12",
            self.layout.runtime / "venv" / "bin" / "miniclaw",
        }
        for directory, names, files in os.walk(
            self.layout.runtime, topdown=True, followlinks=False
        ):
            current = Path(directory)
            self.assertEqual(stat.S_IMODE(current.lstat().st_mode), 0o700, current)
            for name in (*names, *files):
                path = current / name
                item = path.lstat()
                if stat.S_ISREG(item.st_mode):
                    expected = 0o700 if path in executable_paths else 0o600
                    self.assertEqual(stat.S_IMODE(item.st_mode), expected, path)

    def test_system_runtime_rejects_non_root_before_write(self) -> None:
        """非 root builder 即使持有 system layout 也不能创建任何程序目录。"""
        system_prefix = self.root / "system-prefix" / "miniclaw"
        system_prefix.parent.mkdir(mode=0o755)
        system_prefix.parent.chmod(0o755)
        system_command = self.root / "system-bin" / "miniclaw"
        state_home = self.home / "system-state"
        state_home.mkdir(mode=0o700)
        with (
            mock.patch.object(layout_module, "_SYSTEM_PREFIX", system_prefix),
            mock.patch.object(layout_module, "_SYSTEM_COMMAND", system_command),
            mock.patch.object(layout_module, "_validate_system_prefix"),
        ):
            layout = InstallLayout._build(
                system_prefix,
                state_home,
                system_command,
                "0.7.0",
                owner_uid=os.geteuid(),
                user_home=self.home,
            )
            with (
                mock.patch.object(
                    runtime_module, "_is_root_builder", return_value=False, create=True
                ),
                self.assertRaisesRegex(InstallError, "runtime_install_failed"),
            ):
                RuntimeBuilder(self.runner).build(replace(self.inputs, layout=layout))

        self.assertEqual(self.runner.calls, [])
        self.assertFalse(system_prefix.exists())

    def test_system_runtime_failure_cleans_public_staging(self) -> None:
        """system build 失败仍须清理本轮 0755 staging，不能留下半成品。"""
        system_prefix = self.root / "failed-system-prefix" / "miniclaw"
        system_prefix.parent.mkdir(mode=0o755)
        system_prefix.parent.chmod(0o755)
        system_command = self.root / "failed-system-bin" / "miniclaw"
        state_home = self.home / "failed-system-state"
        state_home.mkdir(mode=0o700)
        runner = FakeRunner()
        runner.fail_token = "install-smoke"
        with (
            mock.patch.object(layout_module, "_SYSTEM_PREFIX", system_prefix),
            mock.patch.object(layout_module, "_SYSTEM_COMMAND", system_command),
            mock.patch.object(layout_module, "_validate_system_prefix"),
        ):
            layout = InstallLayout._build(
                system_prefix,
                state_home,
                system_command,
                "0.7.0",
                owner_uid=os.geteuid(),
                user_home=self.home,
            )
            with (
                mock.patch.object(
                    runtime_module, "_is_root_builder", return_value=True, create=True
                ),
                self.assertRaisesRegex(InstallError, "runtime_install_failed"),
            ):
                RuntimeBuilder(runner).build(replace(self.inputs, layout=layout))

            self.assertFalse(layout.staging.exists())
            self.assertFalse(layout.runtime.exists())

    def test_prerelease_runtime_uses_pep440_only_inside_wheel(self) -> None:
        """Runtime保留SemVer，wheel路径与METADATA只使用唯一PEP440映射。"""
        version = "0.8.0-rc.1"
        wheel = self.sources / "miniclaw_agent-0.8.0rc1-py3-none-any.whl"
        self._write_wheel(path=wheel, version="0.8.0rc1")
        manifest = self._manifest_for_version(version, wheel)
        layout = InstallLayout._build(
            self.layout.program_prefix,
            self.layout.state_home,
            self.layout.command_link,
            version,
        )
        runner = FakeRunner()
        runner.version = f"miniclaw {version}\n".encode()
        runner.install_smoke = json.dumps(
            {"status": "ok", "version": version}, separators=(",", ":")
        ).encode() + b"\n"
        runner.tui_smoke = json.dumps(
            {"component": "pi-tui", "status": "ok", "version": version},
            separators=(",", ":"),
        ).encode() + b"\n"

        receipt = RuntimeBuilder(runner).build(
            replace(self.inputs, layout=layout, manifest=manifest, wheel=wheel)
        )

        self.assertEqual(receipt.version, version)
        self.assertEqual(receipt.runtime_relative, f"runtimes/{version}")
        self.assertTrue(layout.runtime.is_dir())
        with self.assertRaisesRegex(InstallError, "runtime_install_failed") as caught:
            runtime_module._inspect_wheel(wheel, "0.8.0-preview.1")
        self.assertNotIn("preview", str(caught.exception))

    def test_real_uv_relocatable_venv_still_needs_final_internal_repair(self) -> None:
        """真实 uv relocatable venv 仍会把 interpreter/base_prefix 指向构建时 Python。"""
        uv = shutil.which("uv")
        self.assertIsNotNone(uv)
        assert uv is not None
        root = self.root / "real-uv"
        cache = self.root / "real-uv-cache"
        completed = subprocess.run(
            [
                uv,
                "venv",
                "--relocatable",
                "--python",
                str(Path(sys.base_prefix) / "bin" / "python3.12"),
                "--no-python-downloads",
                str(root / "venv"),
            ],
            env={
                "HOME": str(self.root / "real-uv-home"),
                "PATH": "/usr/bin:/bin",
                "UV_CACHE_DIR": str(cache),
                "UV_NO_CONFIG": "1",
                "UV_NO_ENV_FILE": "1",
            },
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertEqual(
            Path(os.readlink(root / "venv" / "bin" / "python")),
            Path(sys.base_prefix) / "bin" / "python3.12",
        )
        self.assertIn(
            f"home = {Path(sys.base_prefix) / 'bin'}",
            (root / "venv" / "pyvenv.cfg").read_text(encoding="utf-8"),
        )

    def test_subprocess_deadline_kills_descendant_that_keeps_pipe_open(self) -> None:
        """leader 退出后，持有 pipe 的 descendant 仍须受同一 deadline 约束。"""
        script = (
            "import os,time;"
            "pid=os.fork();"
            "time.sleep(2) if pid == 0 else None;"
            "os._exit(0)"
        )
        started = time.monotonic()

        result = runtime_module._SubprocessRunner().run(
            (sys.executable, "-c", script),
            env={"PATH": "/usr/bin:/bin"},
            timeout=0.2,
        )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertNotEqual(result.returncode, 0)

    def test_subprocess_deadline_bounds_continuous_output(self) -> None:
        """持续 ready 的 stdout 也不能绕过 deadline 或 64 KiB capture 上限。"""
        script = "import os;exec(\"while True: os.write(1, b'x' * 8192)\")"
        started = time.monotonic()

        result = runtime_module._SubprocessRunner().run(
            (sys.executable, "-c", script),
            env={"PATH": "/usr/bin:/bin"},
            timeout=0.2,
        )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout), 64 * 1024)

    def test_default_runner_builds_with_offline_fake_uv(self) -> None:
        """production subprocess runner 必须能在 closed-world env 中完成离线 fake build。"""
        self._write_private(
            self.uv,
            Path("tests/install/fake_uv.py").read_bytes(),
            0o700,
        )
        self._write_private(
            self.node / "bin" / "node",
            (
                b"#!/bin/sh\n"
                b"if [ \"$1\" = \"--version\" ]; then\n"
                b"  printf '%s\\n' 'v24.18.0'\n"
                b"else\n"
                b"  printf '%s\\n' "
                b"'{\"component\":\"pi-tui\",\"status\":\"ok\","
                b"\"version\":\"0.7.0\"}'\n"
                b"fi\n"
            ),
            0o700,
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            managed_root = Path(sys.base_prefix).resolve(strict=True)
            receipt = RuntimeBuilder().build(
                replace(
                    self.inputs,
                    managed_python_root=managed_root,
                    managed_python_executable=managed_root / "bin" / "python3.12",
                )
            )

        self.assertEqual(receipt.version, "0.7.0")
        self.assertTrue(self.layout.runtime.is_dir())
        self.assertFalse(
            [item for item in caught if issubclass(item.category, ResourceWarning)]
        )

    def test_bad_wheel_metadata_or_console_entry_fails_before_staging(self) -> None:
        """wheel name/version/entry point 偏离 manifest 时不能执行或写入。"""
        cases = (
            {"name": "other-agent"},
            {"version": "0.7.1"},
            {"entry": None},
            {"entry": "miniclaw = other.cli:main"},
        )
        for case in cases:
            with self.subTest(case=case):
                self._write_wheel(**case)  # type: ignore[arg-type]
                inputs = replace(self.inputs, manifest=self._manifest())
                with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
                    RuntimeBuilder(self.runner).build(inputs)
                self.assertEqual(self.runner.calls, [])
                self.assertFalse(self.layout.staging.exists())
        self._write_wheel()

    def test_wheel_rejects_high_ratio_zip_bomb_before_staging(self) -> None:
        """wheel 任意 member 的异常压缩比必须在执行 uv 前拒绝。"""
        with zipfile.ZipFile(self.wheel, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("miniclaw/padding.bin", b"x" * (2 * 1024 * 1024))
        self.wheel.chmod(0o600)
        inputs = replace(self.inputs, manifest=self._manifest())

        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).build(inputs)

        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.layout.staging.exists())

    def test_wheel_rejects_duplicate_tree_conflict_special_and_encrypted(self) -> None:
        """wheel 还须拒绝 casefold duplicate、tree conflict、special 与 encryption。"""
        for case in ("duplicate", "tree-conflict", "special"):
            with self.subTest(case=case):
                self._write_wheel()
                with zipfile.ZipFile(self.wheel, "a") as archive:
                    if case == "duplicate":
                        archive.writestr("miniclaw/Agent.py", b"one")
                        archive.writestr("miniclaw/agent.py", b"two")
                    elif case == "tree-conflict":
                        archive.writestr("miniclaw/conflict", b"file")
                        archive.writestr("miniclaw/conflict/child.py", b"child")
                    else:
                        link = zipfile.ZipInfo("miniclaw/link")
                        link.create_system = 3
                        link.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(link, b"target")
                self.wheel.chmod(0o600)
                with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
                    RuntimeBuilder(self.runner).build(
                        replace(self.inputs, manifest=self._manifest())
                    )
                self.assertFalse(self.layout.staging.exists())

        encrypted = zipfile.ZipInfo("miniclaw/encrypted.bin")
        encrypted.flag_bits = 0x1
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            runtime_module._validate_wheel_infos([encrypted])
        self.assertEqual(self.runner.calls, [])

    def test_failed_or_wrong_smoke_never_switches_current_and_redacts_output(self) -> None:
        """Channel/TUI/version smoke 任一失败都保留旧 current 且不泄漏 stderr。"""
        old = self.layout.runtimes_dir / "0.6.0"
        old.mkdir(parents=True, mode=0o700)
        self.layout.current.symlink_to("runtimes/0.6.0")
        cases = (
            ("install-smoke", "broken_channel", b"", b""),
            (None, "wrong_python", b"miniclaw 9.9.9\n", b""),
            (
                None,
                "wrong_tui",
                b"miniclaw 0.7.0\n",
                b'{"component":"pi-tui","status":"ok","version":"9.9.9"}\n',
            ),
            (None, "wrong_node", b"", b""),
            (None, "external_python", b"", b""),
            (None, "wrong_python_minor", b"", b""),
        )
        for fail_token, name, version, tui in cases:
            with self.subTest(case=name):
                runner = FakeRunner()
                runner.fail_token = fail_token
                if version:
                    runner.version = version
                if tui:
                    runner.tui_smoke = tui
                if name == "wrong_node":
                    runner.node_version = b"v24.17.0\n"
                if name == "external_python":
                    runner.python_base_prefix = self.root / "external-python"
                if name == "wrong_python_minor":
                    runner.python_version = (3, 13, 0)
                with self.assertRaisesRegex(InstallError, "runtime_install_failed") as caught:
                    RuntimeBuilder(runner).install_and_activate(self.inputs)
                self.assertNotIn("secret-sentinel", str(caught.exception))
                self.assertEqual(os.readlink(self.layout.current), "runtimes/0.6.0")
                self.assertFalse(self.layout.staging.exists())
                self.assertFalse(self.layout.runtime.exists())

    def test_manifest_hash_mode_link_and_runtime_collisions_fail_closed(self) -> None:
        """verified inputs 必须 no-follow/owner/mode/hash 绑定且不覆盖任何 collision。"""
        bad_manifest = replace(
            self.manifest,
            artifacts=tuple(
                replace(item, sha256="f" * 64) if item.kind == "wheel" else item
                for item in self.manifest.artifacts
            ),
        )
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).build(replace(self.inputs, manifest=bad_manifest))
        self.assertFalse(self.layout.staging.exists())

        self.wheel.chmod(0o644)
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).build(self.inputs)
        self.wheel.chmod(0o600)

        link = self.sources / "linked-node"
        link.symlink_to(self.node, target_is_directory=True)
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).build(replace(self.inputs, node=link))

        self.layout.staging.mkdir(parents=True, mode=0o700)
        marker = self.layout.staging / "foreign"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).build(self.inputs)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        for child in self.layout.staging.iterdir():
            child.unlink()
        self.layout.staging.rmdir()

        self.layout.runtime.mkdir(mode=0o700)
        marker = self.layout.runtime / "foreign"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).build(self.inputs)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_artifact_copy_rejects_ancestor_replaced_by_symlink(self) -> None:
        """verify 与 descriptor copy 间 ancestor 变 symlink 时必须 fail closed。"""
        real_copy = runtime_module._copy_verified_file
        moved_sources = self.root / "sources-moved"
        raced = False

        def swap_ancestor(token: object, destination: Path, mode: int) -> None:
            """在首个 artifact copy 前保持 leaf inode、仅替换 ancestor。"""
            nonlocal raced
            if not raced and getattr(token, "path", None) == self.installer:
                raced = True
                self.sources.rename(moved_sources)
                self.sources.symlink_to(moved_sources.name, target_is_directory=True)
            real_copy(token, destination, mode)  # type: ignore[arg-type]

        with mock.patch.object(runtime_module, "_copy_verified_file", swap_ancestor):
            with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
                RuntimeBuilder(self.runner).build(self.inputs)

        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.layout.runtime.exists())

    def test_copy_rejects_nested_link_and_cleanup_keeps_old_current(self) -> None:
        """safe-extracted Node/TUI tree 内的 link/special 不得被复制。"""
        old = self.layout.runtimes_dir / "0.6.0"
        old.mkdir(parents=True, mode=0o700)
        self.layout.current.symlink_to("runtimes/0.6.0")
        link = self.tui / "dist" / "escape"
        link.symlink_to("/etc/passwd")
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).install_and_activate(self.inputs)
        self.assertEqual(os.readlink(self.layout.current), "runtimes/0.6.0")
        self.assertFalse(self.layout.staging.exists())
        link.unlink()

    def test_source_trees_reject_escaping_python_alias_hardlink_and_special(self) -> None:
        """Python alias 只能留在root内，所有regular hardlink/special均拒绝。"""
        python_alias = self.managed_python / "bin" / "python3"
        python_alias.unlink()
        outside = self.root / "escape"
        outside.write_bytes(b"outside")
        python_alias.symlink_to("../../../escape")
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).build(self.inputs)
        python_alias.unlink()
        python_alias.symlink_to("python3.12")

        hardlink = self.managed_python / "lib" / "python-copy"
        os.link(self.managed_python_executable, hardlink)
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).build(self.inputs)
        hardlink.unlink()

        special = self.tui / "dist" / "pipe"
        os.mkfifo(special, 0o600)
        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
            RuntimeBuilder(self.runner).build(self.inputs)
        special.unlink()

        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.layout.staging.exists())

    def test_activate_uses_relative_atomic_link_and_rejects_foreign_targets(self) -> None:
        """激活只允许受管 runtime，并用 current.next + replace 发布相对 link。"""
        receipt = RuntimeBuilder(self.runner).build(self.inputs)
        activate_runtime(self.layout, receipt)
        self.assertTrue(self.layout.current.is_symlink())
        self.assertEqual(os.readlink(self.layout.current), "runtimes/0.7.0")
        self.assertFalse(self.layout.current.with_name("current.next").exists())

        foreign = self.layout.program_prefix / "foreign"
        foreign.mkdir(mode=0o700)
        self.layout.current.unlink()
        self.layout.current.symlink_to("foreign")
        with self.assertRaisesRegex(InstallError, "activation_failed"):
            activate_runtime(self.layout, receipt)
        self.assertEqual(os.readlink(self.layout.current), "foreign")

        self.layout.current.unlink()
        unowned_runtime = self.layout.runtimes_dir / "0.6.0"
        unowned_runtime.mkdir(mode=0o700)
        self.layout.current.symlink_to("runtimes/0.6.0")
        with self.assertRaisesRegex(InstallError, "activation_failed"):
            activate_runtime(self.layout, receipt)
        self.assertEqual(os.readlink(self.layout.current), "runtimes/0.6.0")

    def test_activation_fsync_failure_restores_old_current(self) -> None:
        """existing current 切换后的 parent fsync 失败必须原子恢复旧 link。"""
        self._write_runtime_receipt("0.6.0")
        self.layout.current.symlink_to("runtimes/0.6.0")
        receipt = RuntimeBuilder(self.runner).build(self.inputs)
        real_fsync = runtime_module._fsync_directory
        calls = 0

        def fail_post_switch(path: Path) -> None:
            """仅在 activation 的 post-switch fsync 注入故障。"""
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected fsync failure")
            real_fsync(path)

        with mock.patch.object(runtime_module, "_fsync_directory", fail_post_switch):
            with self.assertRaisesRegex(InstallError, "activation_failed"):
                activate_runtime(self.layout, receipt)

        self.assertEqual(os.readlink(self.layout.current), "runtimes/0.6.0")
        self.assertFalse(self.layout.current.with_name("current.next").exists())

    def test_activation_cleanup_failure_never_leaves_reserved_current_next(self) -> None:
        """old-link unlink失败不得让reserved current.next阻断幂等重试。"""
        self._write_runtime_receipt("0.6.0")
        self.layout.current.symlink_to("runtimes/0.6.0")
        receipt = RuntimeBuilder(self.runner).build(self.inputs)

        with mock.patch.object(runtime_module, "_unlink_same_inode", return_value=None):
            activate_runtime(self.layout, receipt)

        self.assertEqual(os.readlink(self.layout.current), "runtimes/0.7.0")
        self.assertFalse(self.layout.current.with_name("current.next").exists())
        activate_runtime(self.layout, receipt)
        self.assertEqual(os.readlink(self.layout.current), "runtimes/0.7.0")

    def test_activation_recovers_verified_residue_after_quarantine_interruption(self) -> None:
        """首次quarantine失败可留证据，下一入口必须验证并恢复。"""
        self._write_runtime_receipt("0.6.0")
        self.layout.current.symlink_to("runtimes/0.6.0")
        receipt = RuntimeBuilder(self.runner).build(self.inputs)
        next_link = self.layout.current.with_name("current.next")
        real_no_replace = runtime_module._rename_no_replace
        interrupted = False

        def fail_first_retire(source: Path, destination: Path) -> None:
            """只中断首次post-commit current.next quarantine。"""
            nonlocal interrupted
            if source == next_link and not interrupted:
                interrupted = True
                raise OSError(errno.EIO, "injected quarantine interruption")
            real_no_replace(source, destination)

        with mock.patch.object(runtime_module, "_rename_no_replace", fail_first_retire):
            activate_runtime(self.layout, receipt)

        self.assertEqual(os.readlink(self.layout.current), "runtimes/0.7.0")
        self.assertEqual(os.readlink(next_link), "runtimes/0.6.0")
        activate_runtime(self.layout, receipt)
        self.assertEqual(os.readlink(self.layout.current), "runtimes/0.7.0")
        self.assertFalse(next_link.exists())

    def test_activation_rejects_foreign_current_next_residue(self) -> None:
        """regular、absolute、escape、missing与invalid managed residue均fail closed。"""
        receipt = RuntimeBuilder(self.runner).build(self.inputs)
        next_link = self.layout.current.with_name("current.next")
        cases = (
            ("regular", "regular"),
            ("absolute", str(self.layout.runtime)),
            ("escape", "../outside"),
            ("missing", "runtimes/9.9.9"),
        )
        for name, target in cases:
            with self.subTest(case=name):
                if name == "regular":
                    next_link.write_text("foreign", encoding="utf-8")
                    next_link.chmod(0o600)
                else:
                    next_link.symlink_to(target)
                with self.assertRaisesRegex(InstallError, "activation_failed"):
                    activate_runtime(self.layout, receipt)
                self.assertTrue(next_link.exists() or next_link.is_symlink())
                next_link.unlink()

        invalid = self._write_runtime_receipt("0.6.0")
        (invalid / "release-manifest.json").chmod(0o644)
        next_link.symlink_to("runtimes/0.6.0")
        with self.assertRaisesRegex(InstallError, "activation_failed"):
            activate_runtime(self.layout, receipt)
        self.assertEqual(os.readlink(next_link), "runtimes/0.6.0")

    def test_activation_never_overwrites_foreign_current_races(self) -> None:
        """absent/existing current 的 concurrent foreign link 都不能被覆盖。"""
        receipt = RuntimeBuilder(self.runner).build(self.inputs)
        real_no_replace = runtime_module._rename_no_replace

        def race_absent(source: Path, destination: Path) -> None:
            """在 absent publish 前创建 foreign current。"""
            destination.symlink_to("foreign")
            real_no_replace(source, destination)

        with mock.patch.object(runtime_module, "_rename_no_replace", race_absent):
            with self.assertRaisesRegex(InstallError, "activation_failed"):
                activate_runtime(self.layout, receipt)
        self.assertEqual(os.readlink(self.layout.current), "foreign")

        self.layout.current.unlink()
        self._write_runtime_receipt("0.6.0")
        self.layout.current.symlink_to("runtimes/0.6.0")
        real_exchange = runtime_module._rename_exchange
        raced = False

        def race_existing(source: Path, destination: Path) -> None:
            """在 existing swap 前用 foreign current 替换旧 inode。"""
            nonlocal raced
            if not raced:
                raced = True
                destination.unlink()
                destination.symlink_to("competitor")
            real_exchange(source, destination)

        with mock.patch.object(runtime_module, "_rename_exchange", race_existing):
            with self.assertRaisesRegex(InstallError, "activation_failed"):
                activate_runtime(self.layout, receipt)
        self.assertEqual(os.readlink(self.layout.current), "competitor")

    def _write_runtime_receipt(self, version: str) -> Path:
        """创建 retention 可验证的 immutable runtime fixture。"""
        if not self.layout.program_prefix.exists():
            self.layout.program_prefix.mkdir(mode=0o700)
        if not self.layout.runtimes_dir.exists():
            self.layout.runtimes_dir.mkdir(mode=0o700)
        runtime = self.layout.runtimes_dir / version
        runtime.mkdir(mode=0o700)
        manifest = self._manifest_for_version(version)
        artifacts = {artifact.kind: artifact for artifact in manifest.artifacts}
        receipt = RuntimeReceipt(
            version=version,
            git_commit=manifest.git_commit,
            runtime_relative=f"runtimes/{version}",
            python_version="3.12.11",
            node_version="24.18.0",
            tui_version=version,
            wheel_sha256=artifacts["wheel"].sha256,
            requirements_sha256=artifacts["requirements"].sha256,
            node_sha256=artifacts["node"].sha256,
            tui_sha256=artifacts["tui"].sha256,
            installer_sha256=artifacts["installer"].sha256,
        )
        (runtime / "install-receipt.json").write_bytes(receipt.to_bytes())
        (runtime / "install-receipt.json").chmod(0o600)
        (runtime / "release-manifest.json").write_bytes(runtime_module._manifest_bytes(manifest))
        (runtime / "release-manifest.json").chmod(0o600)
        return runtime

    def test_retention_deletes_only_owned_unreferenced_runtime(self) -> None:
        """retention 保留 current/receipt previous/foreign/symlink，只删可验证旧 Runtime。"""
        old = self._write_runtime_receipt("0.5.0")
        previous = self._write_runtime_receipt("0.6.0")
        current = self._write_runtime_receipt("0.7.0")
        foreign = self.layout.runtimes_dir / "0.4.0"
        foreign.mkdir(mode=0o700)
        linked = self.layout.runtimes_dir / "linked"
        linked.symlink_to(foreign, target_is_directory=True)
        self.layout.current.symlink_to("runtimes/0.7.0")
        InstallReceipt(
            schema_version=1,
            version="0.7.0",
            git_commit=self.manifest.git_commit,
            platform=self.platform,
            installed_at="2026-08-10T00:00:00Z",
            managed_files=(("bin/miniclaw", "f" * 64),),
            current_runtime="runtimes/0.7.0",
            previous_runtime="runtimes/0.6.0",
            service_label=None,
            service_file=None,
            service_file_sha256=None,
        ).write(self.layout.receipt)

        retained = retain_current_and_previous(self.layout)

        self.assertEqual(retained, (Path("runtimes/0.6.0"), Path("runtimes/0.7.0")))
        self.assertFalse(old.exists())
        self.assertTrue(previous.is_dir())
        self.assertTrue(current.is_dir())
        self.assertTrue(foreign.is_dir())
        self.assertTrue(linked.is_symlink())


if __name__ == "__main__":
    unittest.main()
