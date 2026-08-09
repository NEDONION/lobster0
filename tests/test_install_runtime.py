"""验证 hash-locked managed Runtime 的构建、smoke 与原子激活。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path

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
            python.write_bytes(b"#!/bin/sh\nexit 0\n")
            python.chmod(0o700)
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
        self.tui = self.sources / "tui"
        self.tui.mkdir(mode=0o700)
        (self.tui / "dist").mkdir(mode=0o700)
        self._write_private(self.tui / "dist" / "main.js", b"// verified tui\n")
        self.uv = self.sources / "uv"
        self._write_private(self.uv, b"#!/bin/sh\nexit 0\n", 0o700)
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
    ) -> None:
        """写入只含 metadata/entry point 的最小 wheel fixture。"""
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr(
                "miniclaw_agent-0.7.0.dist-info/METADATA",
                f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
            )
            if entry is not None:
                archive.writestr(
                    "miniclaw_agent-0.7.0.dist-info/entry_points.txt",
                    f"[console_scripts]\n{entry}\n",
                )
        self.wheel.chmod(0o600)

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
        node = self.layout.staging / "node" / "bin" / "node"
        tui = self.layout.staging / "tui" / "dist" / "main.js"
        argvs = [call[0] for call in self.runner.calls]
        self.assertEqual(
            argvs,
            [
                (str(self.uv), "venv", "--python", "3.12", str(self.layout.staging / "venv")),
                (
                    str(self.uv),
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--require-hashes",
                    "-r",
                    str(self.requirements),
                ),
                (
                    str(self.uv),
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    str(self.wheel),
                ),
                (str(python), "-I", "-m", "miniclaw", "--version"),
                (str(python), "-I", "-m", "miniclaw", "install-smoke", "--json"),
                (str(node), str(tui), "--smoke"),
            ],
        )
        expected_env = {
            "HOME": str(self.layout.staging / ".home"),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "TMPDIR": str(self.layout.staging / ".tmp"),
            "UV_CACHE_DIR": str(self.layout.staging / ".uv-cache"),
            "UV_NO_CONFIG": "1",
            "UV_NO_ENV_FILE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
        self.assertTrue(all(env == expected_env for _, env in self.runner.calls))
        self.assertEqual(
            receipt,
            RuntimeReceipt(
                version="0.7.0",
                git_commit=self.manifest.git_commit,
                runtime_relative="runtimes/0.7.0",
                python_version="3.12",
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
                b"printf '%s\\n' "
                b"'{\"component\":\"pi-tui\",\"status\":\"ok\","
                b"\"version\":\"0.7.0\"}'\n"
            ),
            0o700,
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            receipt = RuntimeBuilder().build(self.inputs)

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
        )
        for fail_token, name, version, tui in cases:
            with self.subTest(case=name):
                runner = FakeRunner()
                runner.fail_token = fail_token
                if version:
                    runner.version = version
                if tui:
                    runner.tui_smoke = tui
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

    def _write_runtime_receipt(self, version: str) -> Path:
        """创建 retention 可验证的 immutable runtime fixture。"""
        if not self.layout.program_prefix.exists():
            self.layout.program_prefix.mkdir(mode=0o700)
        if not self.layout.runtimes_dir.exists():
            self.layout.runtimes_dir.mkdir(mode=0o700)
        runtime = self.layout.runtimes_dir / version
        runtime.mkdir(mode=0o700)
        receipt = RuntimeReceipt(
            version=version,
            git_commit=self.manifest.git_commit,
            runtime_relative=f"runtimes/{version}",
            python_version="3.12",
            node_version="24.18.0",
            tui_version=version,
            wheel_sha256="a" * 64,
            requirements_sha256="b" * 64,
            node_sha256="c" * 64,
            tui_sha256="d" * 64,
            installer_sha256="e" * 64,
        )
        (runtime / "install-receipt.json").write_bytes(receipt.to_bytes())
        (runtime / "install-receipt.json").chmod(0o600)
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
