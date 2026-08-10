"""受管卸载、purge 双确认与 self-uninstall handoff 的离线行为测试。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from lobster0.install.layout import InstallLayout, render_launcher  # noqa: E402
from lobster0.install.models import (  # noqa: E402
    InstallError,
    InstallRequest,
    PlatformKey,
)
from lobster0.install.orchestrator import (  # noqa: E402
    Uninstaller,
    _validate_purge_root,
    resolve_install_facts,
)
from lobster0.install.receipt import InstallReceipt, managed_file_sha256  # noqa: E402
from lobster0.install.runtime import RuntimeReceipt  # noqa: E402

_INSTALLER_BYTES = b"PK\x03\x04 fake lobster0 installer zipapp\n"
_MANIFEST_BYTES = b'{"schema_version":1}\n'


class _ManagedInstall(unittest.TestCase):
    """构造一个真实的受管用户安装树与独立用户数据。"""

    version = "0.7.0"

    def setUp(self) -> None:
        """在临时 Home 下创建 launcher、command link、Runtime、receipt 与用户数据。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.home.chmod(0o700)
        self.layout = InstallLayout.user(self.home, version=self.version)
        self.state_home = self.layout.state_home
        self.layout.program_prefix.mkdir(mode=0o700, parents=True)
        self.layout.bin_dir.mkdir(mode=0o700)
        self.layout.runtimes_dir.mkdir(mode=0o700)
        self.runtime = self.layout.runtime
        self.runtime.mkdir(mode=0o700)
        self.installer_pyz = self.runtime / "lobster0-installer.pyz"
        self.installer_pyz.write_bytes(_INSTALLER_BYTES)
        self.installer_pyz.chmod(0o700)
        self.installer_sha256 = hashlib.sha256(_INSTALLER_BYTES).hexdigest()
        manifest = self.runtime / "release-manifest.json"
        manifest.write_bytes(_MANIFEST_BYTES)
        manifest.chmod(0o600)
        runtime_receipt = RuntimeReceipt(
            version=self.version,
            git_commit="a" * 40,
            runtime_relative=f"runtimes/{self.version}",
            python_version="3.12.11",
            node_version="24.18.0",
            tui_version=self.version,
            wheel_sha256="1" * 64,
            requirements_sha256="2" * 64,
            node_sha256="3" * 64,
            tui_sha256="4" * 64,
            installer_sha256=self.installer_sha256,
            executables_sha256=None,
        )
        runtime_metadata = self.runtime / "install-receipt.json"
        runtime_metadata.write_bytes(runtime_receipt.to_bytes())
        runtime_metadata.chmod(0o600)
        venv_bin = self.runtime / "venv" / "bin"
        venv_bin.mkdir(mode=0o700, parents=True)
        (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        (venv_bin / "python").chmod(0o700)
        node_bin = self.runtime / "node" / "bin"
        node_bin.mkdir(mode=0o700, parents=True)
        (node_bin / "node").write_text("#!/bin/sh\n", encoding="utf-8")
        (node_bin / "node").chmod(0o700)
        tui_dist = self.runtime / "tui" / "dist"
        tui_dist.mkdir(mode=0o700, parents=True)
        (tui_dist / "main.js").write_text("// tui\n", encoding="utf-8")
        python_bin = self.runtime / "python" / "bin"
        python_bin.mkdir(mode=0o700, parents=True)
        self.managed_python = python_bin / "python3.12"
        self.managed_python.write_text("#!/bin/sh\n", encoding="utf-8")
        self.managed_python.chmod(0o700)
        self.layout.current.symlink_to(f"runtimes/{self.version}")
        self.layout.launcher.write_bytes(render_launcher(self.layout))
        self.layout.launcher.chmod(0o700)
        self.layout.command_link.parent.mkdir(mode=0o755, parents=True)
        self.layout.command_link.symlink_to(
            os.path.relpath(self.layout.launcher, start=self.layout.command_link.parent)
        )
        self.receipt = InstallReceipt(
            schema_version=1,
            version=self.version,
            git_commit="a" * 40,
            platform=PlatformKey("macos", "arm64"),
            installed_at="2026-08-10T00:00:00Z",
            managed_files=(("bin/lobster0", managed_file_sha256(self.layout.launcher)),),
            current_runtime=f"runtimes/{self.version}",
            previous_runtime=None,
            service_label=None,
            service_file=None,
            service_file_sha256=None,
        )
        self.receipt.write(self.layout.receipt)
        self._write_user_data()
        self.request = InstallRequest(
            action="uninstall",
            version=None,
            channel="stable",
            prefix=None,
            state_home=self.state_home,
            system_prefix=False,
            onboard=False,
            config_file=None,
            secrets_file=None,
            service=None,
            allow_system_packages=False,
            dry_run=False,
            json_output=False,
            verbose=False,
            purge_data=False,
            confirm_data_loss=False,
        )
        self.outside_executable = self.home / "system-python"
        self.outside_executable.write_text("#!/bin/sh\n", encoding="utf-8")
        self.outside_executable.chmod(0o700)

    def _write_user_data(self) -> None:
        """写入 uninstall 必须完整保留的配置、Secret、数据库与个人目录。"""
        (self.state_home / "config.toml").write_text("[agent]\n", encoding="utf-8")
        secrets = self.state_home / "secrets.env"
        secrets.write_text("LOBSTER0_MODEL_API_KEY=user-secret\n", encoding="utf-8")
        secrets.chmod(0o600)
        (self.state_home / "lobster0.db").write_bytes(b"SQLite format 3\x00user rows")
        for name in ("memory", "skills", "prompts", "workspace", "logs", "browser"):
            directory = self.state_home / name
            directory.mkdir(mode=0o700)
            (directory / "personal.txt").write_text(f"{name} data\n", encoding="utf-8")

    def hash_user_data(self) -> str:
        """返回状态根中全部非受管程序文件的稳定内容指纹。"""
        managed = {
            self.layout.bin_dir,
            self.layout.runtimes_dir,
            self.layout.current,
            self.layout.receipt,
            self.layout.lock,
        }
        digest = hashlib.sha256()
        for path in sorted(self.state_home.rglob("*")):
            if any(path == item or path.is_relative_to(item) for item in managed):
                continue
            digest.update(str(path.relative_to(self.state_home)).encode())
            if path.is_symlink():
                digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
            elif path.is_file():
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def uninstaller(self, **kwargs: object) -> Uninstaller:
        """构造默认在 Runtime 之外运行、非 TTY 的卸载器。"""
        options: dict[str, object] = {
            "environ": {},
            "execve": self._forbidden_execve,
            "executable": self.outside_executable,
            "isatty": False,
            "stdout": io.StringIO(),
            "user_home": self.home,
        }
        options.update(kwargs)
        return Uninstaller(self.layout, **options)  # type: ignore[arg-type]

    def _forbidden_execve(self, *_args: object) -> None:
        """在不应发生 handoff 的用例中失败关闭。"""
        raise AssertionError("unexpected installer handoff")


class DefaultUninstallTests(_ManagedInstall):
    """验证默认卸载只删除 receipt 内的受管程序文件。"""

    def test_default_uninstall_preserves_all_user_data(self) -> None:
        """默认卸载必须保留配置、Secret、数据库、Memory、Skills 与 Workspace。"""
        before = self.hash_user_data()

        result = self.uninstaller().run(replace(self.request, purge_data=False))

        self.assertEqual(self.hash_user_data(), before)
        self.assertEqual(result.action, "uninstall")
        self.assertFalse(self.layout.runtimes_dir.exists())
        self.assertFalse(self.layout.launcher.exists())
        self.assertFalse(self.layout.command_link.is_symlink())
        self.assertFalse(self.layout.current.is_symlink())
        self.assertFalse(self.layout.receipt.exists())
        self.assertTrue((self.state_home / "lobster0.db").is_file())
        self.assertTrue((self.state_home / "secrets.env").is_file())
        self.assertTrue((self.state_home / "workspace" / "personal.txt").is_file())

    def test_uninstall_prints_exact_retained_root(self) -> None:
        """卸载输出必须给出精确保留目录，且不泄露 Secret 内容。"""
        stream = io.StringIO()
        self.uninstaller(stdout=stream).run(self.request)

        self.assertIn(str(self.state_home), stream.getvalue())
        self.assertNotIn("user-secret", stream.getvalue())

    def test_removes_managed_service_before_program_files(self) -> None:
        """必须先停止并移除受管服务，再删除 launcher 与 Runtime。"""
        replace(
            self.receipt,
            service_label="io.lobster0.gateway",
            service_file="Library/LaunchAgents/io.lobster0.gateway.plist",
            service_file_sha256="c" * 64,
        ).write(self.layout.receipt)
        order: list[str] = []

        def fake_uninstall(_spec: object, _runner: object, *, expected_sha256: str) -> None:
            order.append(f"service:{expected_sha256}")

        original_unlink = Path.unlink

        def recording_unlink(path: Path, missing_ok: bool = False) -> None:
            order.append(f"unlink:{path.name}")
            original_unlink(path, missing_ok=missing_ok)

        with (
            mock.patch("lobster0.install.orchestrator.render_service_spec"),
            mock.patch(
                "lobster0.install.orchestrator.service_uninstall",
                side_effect=fake_uninstall,
            ),
            mock.patch.object(Path, "unlink", recording_unlink),
        ):
            self.uninstaller().run(self.request)

        self.assertEqual(order[0], "service:" + "c" * 64)
        self.assertFalse(self.layout.launcher.exists())

    def test_foreign_runtime_entry_blocks_deletion_and_preserves_everything(self) -> None:
        """runtimes/ 下的非受管条目必须让卸载失败关闭而不是被递归删除。"""
        foreign = self.layout.runtimes_dir / "not-a-runtime"
        foreign.mkdir(mode=0o700)
        (foreign / "keep.txt").write_text("foreign\n", encoding="utf-8")
        before = self.hash_user_data()

        with self.assertRaises(InstallError) as raised:
            self.uninstaller().run(self.request)

        self.assertEqual(raised.exception.code, "uninstall_ownership_mismatch")
        self.assertTrue((foreign / "keep.txt").is_file())
        self.assertTrue(self.layout.launcher.is_file())
        self.assertTrue(self.layout.receipt.is_file())
        self.assertEqual(self.hash_user_data(), before)

    def test_launcher_hash_drift_blocks_deletion(self) -> None:
        """launcher 与 receipt hash 漂移时必须保留而不是删除。"""
        self.layout.launcher.write_bytes(b"#!/bin/sh\nexec /bin/false\n")
        self.layout.launcher.chmod(0o700)

        with self.assertRaises(InstallError) as raised:
            self.uninstaller().run(self.request)

        self.assertEqual(raised.exception.code, "uninstall_ownership_mismatch")
        self.assertTrue(self.layout.launcher.is_file())
        self.assertTrue(self.layout.runtime.is_dir())
        self.assertTrue(self.layout.receipt.is_file())

    def test_foreign_command_link_target_is_never_removed(self) -> None:
        """PATH 上的同名非受管命令必须保留。"""
        self.layout.command_link.unlink()
        self.layout.command_link.symlink_to("/usr/bin/true")

        with self.assertRaises(InstallError) as raised:
            self.uninstaller().run(self.request)

        self.assertEqual(raised.exception.code, "uninstall_ownership_mismatch")
        self.assertTrue(self.layout.command_link.is_symlink())
        self.assertTrue(self.layout.receipt.is_file())

    def test_partial_failure_preserves_receipt_and_user_data(self) -> None:
        """Runtime 删除失败时必须保留 receipt 与全部用户数据。"""
        before = self.hash_user_data()

        with (
            mock.patch(
                "lobster0.install.orchestrator.shutil.rmtree",
                side_effect=OSError("simulated removal failure"),
            ),
            self.assertRaises(InstallError),
        ):
            self.uninstaller().run(self.request)

        self.assertTrue(self.layout.receipt.is_file())
        self.assertTrue(self.layout.runtime.is_dir())
        self.assertEqual(self.hash_user_data(), before)

    def test_missing_receipt_fails_closed_without_touching_anything(self) -> None:
        """没有 receipt 时不得猜测受管文件。"""
        self.layout.receipt.unlink()
        before = self.hash_user_data()

        with self.assertRaises(InstallError):
            self.uninstaller().run(self.request)

        self.assertTrue(self.layout.launcher.is_file())
        self.assertEqual(self.hash_user_data(), before)


class SelfUninstallHandoffTests(_ManagedInstall):
    """验证删除 Runtime 前把控制权交给 receipt-matching installer pyz。"""

    def test_hands_off_to_verified_installer_copy_before_deleting_runtime(self) -> None:
        """必须复制、重新校验 hash 并 exec 私有 0700 目录中的 installer。"""
        calls: list[tuple[str, tuple[str, ...], dict[str, str]]] = []

        def capture(executable: str, argv: tuple[str, ...], env: dict[str, str]) -> None:
            calls.append((executable, tuple(argv), dict(env)))

        uninstaller = self.uninstaller(
            execve=capture,
            executable=self.runtime / "venv" / "bin" / "python",
        )
        with self.assertRaises(InstallError):
            uninstaller.run(self.request)

        self.assertEqual(len(calls), 1)
        executable, argv, environment = calls[0]
        self.assertEqual(environment.get("LOBSTER0_INSTALLER_HOPS"), "1")
        self.assertIn("uninstall", argv)
        copied = Path(argv[argv.index("uninstall") - 1])
        self.assertEqual(copied.name, "lobster0-installer.pyz")
        self.assertEqual(copied.read_bytes(), _INSTALLER_BYTES)
        self.assertEqual(stat.S_IMODE(copied.parent.lstat().st_mode), 0o700)
        self.assertNotEqual(copied.parent, self.runtime)
        self.assertFalse(copied.is_relative_to(self.layout.program_prefix))
        self.assertEqual(executable, str(self.managed_python))
        self.assertTrue(self.layout.runtime.is_dir())
        self.assertTrue(self.layout.receipt.is_file())

    def test_hop_guard_prevents_a_second_handoff(self) -> None:
        """已跳转一次的进程必须直接执行删除而不是再次 exec。"""
        uninstaller = self.uninstaller(
            environ={"LOBSTER0_INSTALLER_HOPS": "1"},
            executable=self.runtime / "venv" / "bin" / "python",
        )

        uninstaller.run(self.request)

        self.assertFalse(self.layout.runtimes_dir.exists())

    def test_installer_hash_mismatch_blocks_handoff_and_deletion(self) -> None:
        """installer pyz 与 Runtime receipt 不匹配时必须失败关闭。"""
        self.installer_pyz.write_bytes(b"tampered installer\n")
        self.installer_pyz.chmod(0o700)

        with self.assertRaises(InstallError) as raised:
            self.uninstaller(
                executable=self.runtime / "venv" / "bin" / "python",
            ).run(self.request)

        self.assertEqual(raised.exception.code, "uninstall_ownership_mismatch")
        self.assertTrue(self.layout.runtime.is_dir())
        self.assertTrue(self.layout.receipt.is_file())


class PurgeTests(_ManagedInstall):
    """验证 purge 的双确认、显式枚举与保守拒绝。"""

    def purge_request(self, **kwargs: object) -> InstallRequest:
        """返回一个 purge 请求。"""
        return replace(self.request, purge_data=True, **kwargs)  # type: ignore[arg-type]

    def test_noninteractive_purge_requires_the_exact_long_flag(self) -> None:
        """非交互 purge 缺少确认时必须拒绝并保留全部数据。"""
        before = self.hash_user_data()

        with self.assertRaises(InstallError) as raised:
            self.uninstaller(isatty=False).run(self.purge_request())

        self.assertEqual(raised.exception.code, "request_invalid")
        self.assertEqual(self.hash_user_data(), before)

    def test_interactive_purge_requires_both_confirmation_phrases(self) -> None:
        """交互 purge 的任一确认短语错误都必须中止且不删除数据。"""
        before = self.hash_user_data()
        for answers in (
            ["wrong", "DELETE ALL LOBSTER0 DATA"],
            [str(self.state_home), "delete all lobster0 data"],
            [str(self.state_home), ""],
        ):
            with self.subTest(answers=answers):
                pending = list(answers)
                with self.assertRaises(InstallError):
                    self.uninstaller(
                        isatty=True,
                        confirm=lambda _prompt, queue=pending: queue.pop(0),
                    ).run(self.purge_request())
                self.assertEqual(self.hash_user_data(), before)
                self.assertTrue(self.layout.receipt.is_file())

    def test_interactive_purge_removes_enumerated_state_and_keeps_workspace(self) -> None:
        """两个确认短语正确时只删除显式枚举的状态路径。"""
        answers = [str(self.state_home), "DELETE ALL LOBSTER0 DATA"]

        self.uninstaller(
            isatty=True,
            confirm=lambda _prompt, queue=answers: queue.pop(0),
        ).run(self.purge_request())

        self.assertFalse((self.state_home / "config.toml").exists())
        self.assertFalse((self.state_home / "secrets.env").exists())
        self.assertFalse((self.state_home / "lobster0.db").exists())
        self.assertFalse((self.state_home / "memory").exists())
        self.assertFalse((self.state_home / "skills").exists())
        self.assertFalse((self.state_home / "logs").exists())
        self.assertTrue((self.state_home / "workspace" / "personal.txt").is_file())

    def test_noninteractive_purge_with_exact_flag_removes_state(self) -> None:
        """显式 confirm flag 可以在无 TTY 时完成 purge。"""
        self.uninstaller(isatty=False).run(
            self.purge_request(confirm_data_loss=True)
        )

        self.assertFalse((self.state_home / "lobster0.db").exists())
        self.assertTrue((self.state_home / "workspace").is_dir())

    def test_purge_refuses_symlinked_state_entry(self) -> None:
        """枚举目标是 symlink 时必须整体拒绝，不跟随到外部目录。"""
        outside = self.home / "outside-memory"
        outside.mkdir(mode=0o700)
        (outside / "keep.txt").write_text("outside\n", encoding="utf-8")
        memory = self.state_home / "memory"
        (memory / "personal.txt").unlink()
        memory.rmdir()
        memory.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(InstallError) as raised:
            self.uninstaller(isatty=False).run(
                self.purge_request(confirm_data_loss=True)
            )

        self.assertEqual(raised.exception.code, "uninstall_ownership_mismatch")
        self.assertTrue((outside / "keep.txt").is_file())
        self.assertTrue((self.state_home / "lobster0.db").is_file())

    def test_purge_refuses_foreign_owned_state_home(self) -> None:
        """状态根不属于当前用户时必须拒绝任何删除。"""
        before = self.hash_user_data()

        with (
            mock.patch(
                "lobster0.install.orchestrator.os.geteuid",
                return_value=os.geteuid() + 1,
            ),
            self.assertRaises(InstallError),
        ):
            self.uninstaller(isatty=False).run(
                self.purge_request(confirm_data_loss=True)
            )

        self.assertEqual(self.hash_user_data(), before)

    def test_purge_refuses_root_home_root_and_workspace_targets(self) -> None:
        """`/`、Home 根、Workspace 根与 symlink 都不能成为 purge 根。"""
        link = self.home / "linked-state"
        link.symlink_to(self.state_home, target_is_directory=True)
        for candidate in (
            Path("/"),
            self.home,
            self.state_home / "workspace",
            link,
            Path("relative"),
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(InstallError):
                    _validate_purge_root(candidate, user_home=self.home)
        _validate_purge_root(self.state_home, user_home=self.home)
        self.assertTrue((self.state_home / "lobster0.db").is_file())


class InstallFactsTests(_ManagedInstall):
    """验证 CLI 与 Doctor 共用的受管/源码模式判定。"""

    def test_reports_managed_mode_for_a_real_receipt(self) -> None:
        """存在有效 receipt 时必须报告受管安装与精确版本。"""
        facts = resolve_install_facts(
            self.state_home,
            environ={"LOBSTER0_PREFIX": str(self.layout.program_prefix)},
            executable=self.outside_executable,
            user_home=self.home,
        )

        self.assertTrue(facts.managed)
        self.assertIsNotNone(facts.receipt)
        assert facts.receipt is not None
        self.assertEqual(facts.receipt.version, self.version)
        self.assertEqual(facts.program_prefix, self.layout.program_prefix)

    def test_reports_source_mode_without_a_receipt(self) -> None:
        """没有 receipt 的源码 checkout 必须报告非受管且不抛异常。"""
        self.layout.receipt.unlink()

        facts = resolve_install_facts(
            self.state_home,
            environ={},
            executable=self.outside_executable,
            user_home=self.home,
        )

        self.assertFalse(facts.managed)
        self.assertIsNone(facts.receipt)

    def test_reports_managed_runtime_metadata_for_doctor(self) -> None:
        """Doctor 需要的 Runtime/Node/TUI 事实必须来自 Runtime receipt。"""
        facts = resolve_install_facts(
            self.state_home,
            environ={"LOBSTER0_PREFIX": str(self.layout.program_prefix)},
            executable=self.outside_executable,
            user_home=self.home,
        )

        self.assertIsNotNone(facts.layout)
        assert facts.layout is not None
        runtime = facts.layout.program_prefix / "runtimes" / self.version
        self.assertTrue((runtime / "node" / "bin" / "node").is_file())
        self.assertTrue((runtime / "tui" / "dist" / "main.js").is_file())
        document = json.loads((runtime / "install-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(document["node_version"], "24.18.0")


if __name__ == "__main__":
    unittest.main()
