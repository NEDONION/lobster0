"""验证 installer layout、stable launcher 与跨进程 lock 的安全边界。"""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miniclaw.install import layout as layout_module
from miniclaw.install.layout import (
    InstallLayout,
    InstallLock,
    install_launcher,
    render_launcher,
)
from miniclaw.install.models import InstallError, InstallRequest


def _account(home: Path, *, uid: int | None = None) -> pwd.struct_passwd:
    """构造与临时 Home 绑定的规范非 root passwd 记录。"""
    selected_uid = os.geteuid() if uid is None else uid
    return pwd.struct_passwd(
        ("alice", "x", selected_uid, os.getegid(), "Fixture", str(home), "/bin/sh")
    )


class InstallLayoutTests(unittest.TestCase):
    """覆盖路径 lexical/no-follow、归属、launcher 和 lock 行为。"""

    def setUp(self) -> None:
        """创建 owner-only 临时 Home。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.account = _account(self.home)

    def tearDown(self) -> None:
        """清理临时目录。"""
        self.temporary.cleanup()

    def request(self, **changes: object) -> InstallRequest:
        """构造显式版本的 canonical user install 请求。"""
        values: dict[str, object] = {
            "action": "install",
            "version": "0.7.0",
            "channel": "stable",
            "prefix": None,
            "state_home": self.home / ".miniclaw",
            "system_prefix": False,
            "onboard": True,
            "config_file": None,
            "secrets_file": None,
            "service": None,
            "allow_system_packages": False,
            "dry_run": False,
            "json_output": False,
            "verbose": False,
            "purge_data": False,
            "confirm_data_loss": False,
        }
        values.update(changes)
        return InstallRequest(**values)  # type: ignore[arg-type]

    def test_default_layout_separates_runtime_from_user_state(self) -> None:
        """错误的默认根或把 runtime 混进状态树会破坏升级和数据保留。"""
        layout = InstallLayout.user(self.home, version="0.7.0")

        self.assertEqual(layout.program_prefix, self.home / ".miniclaw")
        self.assertEqual(layout.state_home, self.home / ".miniclaw")
        self.assertEqual(layout.runtime, layout.program_prefix / "runtimes" / "0.7.0")
        self.assertEqual(layout.secrets_file, layout.state_home / "secrets.env")
        self.assertEqual(layout.command_link, self.home / ".local" / "bin" / "miniclaw")
        self.assertEqual(layout.launcher, layout.program_prefix / "bin" / "miniclaw")

    def test_layout_rejects_broad_relative_or_noncanonical_roots(self) -> None:
        """允许 `/`、Home、相对路径或 `..` 会扩大后续写入/删除边界。"""
        cases = (
            lambda: InstallLayout.user(Path("relative"), version="0.7.0"),
            lambda: InstallLayout.user(Path("/"), version="0.7.0"),
            lambda: InstallLayout.for_request(
                self.request(prefix=self.home), self.account
            ),
            lambda: InstallLayout.for_request(
                self.request(prefix=self.home / "safe" / ".." / "escape"), self.account
            ),
        )
        for build in cases:
            with self.subTest(build=build), self.assertRaisesRegex(
                InstallError, "request_invalid"
            ):
                build()

    def test_layout_rejects_symlink_and_group_or_world_writable_parent(self) -> None:
        """跟随 symlink 或在可被同组/任意用户改写的父目录下安装会被换路径。"""
        target = self.home / "target"
        target.mkdir(mode=0o700)
        symlink = self.home / "linked"
        symlink.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            InstallLayout.for_request(self.request(prefix=symlink), self.account)

        unsafe = self.home / "unsafe"
        unsafe.mkdir(mode=0o770)
        unsafe.chmod(0o770)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            InstallLayout.for_request(self.request(prefix=unsafe / "miniclaw"), self.account)
        unsafe.chmod(0o707)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            InstallLayout.for_request(self.request(prefix=unsafe / "miniclaw"), self.account)

    def test_request_binds_user_and_system_state_to_target_owner(self) -> None:
        """system prefix 只能改变程序根，state 和命令仍归原非 root 用户。"""
        # 本测试只验证 system layout/state 归属；真实 /usr/local parent 安全性另由实现 fail closed。
        with mock.patch.object(layout_module, "_validate_system_prefix"):
            system = InstallLayout.for_request(
                self.request(system_prefix=True),
                self.account,
            )

        self.assertEqual(system.program_prefix, Path("/usr/local/lib/miniclaw"))
        self.assertEqual(system.state_home, self.home / ".miniclaw")
        self.assertEqual(system.command_link, Path("/usr/local/bin/miniclaw"))
        with self.assertRaisesRegex(InstallError, "privilege_denied"):
            InstallLayout.for_request(self.request(), _account(self.home, uid=os.geteuid() + 1))

    def test_rendered_launcher_quotes_paths_preserves_argv_and_exports_only_paths(self) -> None:
        """shell quoting 或 `$@` 错误会拆分前缀、参数或泄漏非路径安装变量。"""
        quoted_home = self.home / "space and 'quote"
        quoted_home.mkdir(mode=0o700)
        layout = InstallLayout.user(quoted_home, version="0.7.0")
        fake_python = layout.current / "venv" / "bin" / "python"
        fake_python.parent.mkdir(mode=0o700, parents=True)
        capture = self.root / "capture.json"
        fake_python.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "payload = {\n"
            "  'argv': sys.argv[1:],\n"
            "  'env': {k: v for k, v in os.environ.items() if k.startswith('MINICLAW_')},\n"
            "}\n"
            "open(os.environ['CAPTURE_FILE'], 'w', encoding='utf-8').write(json.dumps(payload))\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)
        layout.launcher.parent.mkdir(mode=0o700, parents=True)
        layout.launcher.write_bytes(render_launcher(layout))
        layout.launcher.chmod(0o700)

        subprocess.run(
            [str(layout.launcher), "plain", "two words", "quote'arg", "$(never)"],
            check=True,
            env={"CAPTURE_FILE": str(capture), "PATH": os.environ.get("PATH", "")},
        )
        payload = json.loads(capture.read_text(encoding="utf-8"))

        self.assertEqual(
            payload["argv"],
            ["-m", "miniclaw", "plain", "two words", "quote'arg", "$(never)"],
        )
        self.assertEqual(
            payload["env"],
            {
                "MINICLAW_HOME": str(layout.state_home),
                "MINICLAW_NODE": str(layout.current / "node" / "bin" / "node"),
                "MINICLAW_TUI_ENTRY": str(layout.current / "tui" / "dist" / "main.js"),
                "MINICLAW_ENV_FILE": str(layout.secrets_file),
            },
        )

    def test_install_launcher_uses_relative_link_and_never_overwrites_foreign_files(self) -> None:
        """缺少 receipt hash 仍覆盖 launcher/link 会接管用户已有命令。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        launcher_hash, link_hash = install_launcher(layout)

        self.assertTrue(layout.launcher.is_file())
        self.assertTrue(layout.command_link.is_symlink())
        self.assertFalse(os.readlink(layout.command_link).startswith("/"))
        self.assertEqual(layout.command_link.resolve(), layout.launcher.resolve())
        self.assertEqual(
            install_launcher(
                layout,
                launcher_sha256=launcher_hash,
                command_link_sha256=link_hash,
            ),
            (launcher_hash, link_hash),
        )

        foreign_layout = InstallLayout.user(self.home / "other", version="0.7.0")
        foreign_layout.command_link.parent.mkdir(mode=0o700, parents=True)
        foreign_layout.command_link.write_text("foreign", encoding="utf-8")
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            install_launcher(foreign_layout)
        self.assertEqual(foreign_layout.command_link.read_text(encoding="utf-8"), "foreign")
        self.assertFalse(foreign_layout.launcher.exists())

    def test_lock_is_exact_private_exclusive_and_close_is_ownership_bound(self) -> None:
        """非 O_EXCL lock 或无 inode 绑定 close 会并发安装或删除他人的新 lock。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        first = InstallLock.acquire(layout)
        document = json.loads(layout.lock.read_text(encoding="utf-8"))
        self.assertEqual(set(document), {"pid", "uid", "start"})
        self.assertEqual(stat.S_IMODE(layout.lock.lstat().st_mode), 0o600)
        with self.assertRaisesRegex(InstallError, "install_locked"):
            InstallLock.acquire(layout)

        layout.lock.unlink()
        layout.lock.write_text('{"pid":999,"uid":999,"start":"2026-01-01T00:00:00Z"}\n')
        first.close()
        self.assertTrue(layout.lock.exists())

    def test_lock_removes_only_same_uid_confirmed_dead_or_reused_pid(self) -> None:
        """stale 判定漏掉 UID/PID start 会删除 foreign/live installer 的锁。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        layout.program_prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
        stale = {
            "pid": 424242,
            "uid": os.geteuid(),
            "start": "2026-01-01T00:00:00Z",
        }
        layout.lock.write_text(json.dumps(stale) + "\n", encoding="utf-8")
        layout.lock.chmod(0o600)
        with mock.patch.object(layout_module, "_probe_process", return_value=("dead", None)):
            acquired = InstallLock.acquire(layout)
        acquired.close()

        layout.lock.write_text(json.dumps(stale) + "\n", encoding="utf-8")
        layout.lock.chmod(0o600)
        with mock.patch.object(
            layout_module,
            "_probe_process",
            return_value=("alive", "2026-02-01T00:00:00Z"),
        ):
            acquired = InstallLock.acquire(layout)
        acquired.close()

        for document, probe in (
            ({**stale, "uid": os.geteuid() + 1}, ("dead", None)),
            (stale, ("alive", stale["start"])),
            (stale, ("unknown", None)),
        ):
            layout.lock.write_text(json.dumps(document) + "\n", encoding="utf-8")
            layout.lock.chmod(0o600)
            with (
                self.subTest(document=document, probe=probe),
                mock.patch.object(layout_module, "_probe_process", return_value=probe),
                self.assertRaisesRegex(InstallError, "install_locked"),
            ):
                InstallLock.acquire(layout)
            layout.lock.unlink()

    def test_malformed_lock_fails_closed(self) -> None:
        """损坏、unknown-key 或 symlink lock 不能被当作 stale 静默移除。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        layout.program_prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
        cases = (b"not-json", b'{"pid":1,"uid":1,"start":"x","extra":1}')
        for payload in cases:
            layout.lock.write_bytes(payload)
            layout.lock.chmod(0o600)
            with self.subTest(payload=payload), self.assertRaisesRegex(
                InstallError, "install_locked"
            ):
                InstallLock.acquire(layout)
            layout.lock.unlink()
        target = self.root / "foreign-lock"
        target.write_text("foreign", encoding="utf-8")
        layout.lock.symlink_to(target)
        with self.assertRaisesRegex(InstallError, "install_locked"):
            InstallLock.acquire(layout)


if __name__ == "__main__":
    unittest.main()
