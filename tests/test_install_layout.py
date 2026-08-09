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
from miniclaw.install.models import (
    InstallError,
    InstallPlan,
    InstallRequest,
    PlatformKey,
    ReleaseManifest,
)


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
        self.root = Path(self.temporary.name).resolve(strict=True)
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

    def plan(self, request: InstallRequest, program_prefix: Path) -> InstallPlan:
        """构造绑定临时 layout 的 strict InstallPlan。"""
        manifest = ReleaseManifest.from_bytes(
            (Path(__file__).parent / "install" / "manifest_v1.json").read_bytes()
        )
        return InstallPlan(
            request=request,
            manifest=manifest,
            platform=PlatformKey("linux", "x86_64"),
            distro_id="ubuntu",
            distro_version="24.04",
            service_manager="systemd-user",
            program_prefix=program_prefix,
            state_home=request.state_home,
            artifact_filenames=("miniclaw-tui-0.7.0-linux-x86_64.tar.gz",),
            system_argvs=(),
            install_service=False,
            run_onboarding=True,
        )

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

    def test_home_and_roots_reject_symlink_in_any_ancestor_component(self) -> None:
        """只 lstat Home 自身会漏掉更上层 symlink ancestor，后续根仍可被重定向。"""
        real_parent = self.root / "real-parent"
        real_parent.mkdir(mode=0o700)
        nested_home = real_parent / "nested-home"
        nested_home.mkdir(mode=0o700)
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)

        with self.assertRaisesRegex(InstallError, "request_invalid"):
            InstallLayout.user(linked_parent / "nested-home", version="0.7.0")

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

    def test_lock_requires_real_process_start_before_creating_path(self) -> None:
        """把 wall clock 冒充 process start 会把活锁误判成 PID reuse stale。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        with (
            mock.patch.object(layout_module, "_probe_process", return_value=("alive", None)),
            self.assertRaisesRegex(InstallError, "install_locked"),
        ):
            InstallLock.acquire(layout)
        self.assertFalse(layout.lock.exists())

    def test_lock_close_does_not_unlink_path_replaced_after_payload_read(self) -> None:
        """close 的 pathname check/unlink 窗口不得删除另一个 installer 的 replacement。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        lock = InstallLock.acquire(layout)
        replacement = b'{"pid":999,"uid":501,"start":"2026-01-01T00:00:00Z"}\n'
        real_read = layout_module._read_lock_bytes

        def replace_after_read(path: Path, uid: int) -> bytes:
            """读完原 lock 后在 unlink 前替换 pathname。"""
            payload = real_read(path, uid)
            path.unlink()
            path.write_bytes(replacement)
            path.chmod(0o600)
            return payload

        with mock.patch.object(layout_module, "_read_lock_bytes", side_effect=replace_after_read):
            lock.close()
        self.assertEqual(layout.lock.read_bytes(), replacement)

    def test_lock_close_and_stale_takeover_quarantine_postcheck_replacement(self) -> None:
        """最终 token check 后的 replacement 仍不得被 close/stale unlink。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        replacement = b'{"pid":999,"uid":501,"start":"2026-05-01T00:00:00Z"}\n'
        lock = InstallLock.acquire(layout)
        real_owned = layout_module._lock_path_owned

        def replace_after_owned(*args: object, **kwargs: object) -> bool:
            """在最终 ownership check 返回后替换公开 pathname。"""
            owned = real_owned(*args, **kwargs)  # type: ignore[arg-type]
            if owned:
                layout.lock.unlink()
                layout.lock.write_bytes(replacement)
                layout.lock.chmod(0o600)
            return owned

        with mock.patch.object(
            layout_module,
            "_lock_path_owned",
            side_effect=replace_after_owned,
        ):
            lock.close()
        self.assertEqual(layout.lock.read_bytes(), replacement)

        layout.lock.unlink()
        stale = {
            "pid": 424242,
            "uid": os.geteuid(),
            "start": "2026-01-01T00:00:00Z",
        }
        layout.lock.write_text(json.dumps(stale) + "\n", encoding="utf-8")
        layout.lock.chmod(0o600)
        with (
            mock.patch.object(layout_module, "_probe_process", return_value=("dead", None)),
            mock.patch.object(
                layout_module,
                "_lock_path_owned",
                side_effect=replace_after_owned,
            ),
            self.assertRaisesRegex(InstallError, "install_locked"),
        ):
            InstallLock.acquire(layout)
        self.assertEqual(layout.lock.read_bytes(), replacement)

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
        with mock.patch.object(
            layout_module,
            "_probe_process",
            side_effect=(
                ("dead", None),
                ("alive", "2026-03-01T00:00:00Z"),
            ),
        ):
            acquired = InstallLock.acquire(layout)
        acquired.close()

        layout.lock.write_text(json.dumps(stale) + "\n", encoding="utf-8")
        layout.lock.chmod(0o600)
        with mock.patch.object(
            layout_module,
            "_probe_process",
            side_effect=(
                ("alive", "2026-02-01T00:00:00Z"),
                ("alive", "2026-03-01T00:00:00Z"),
            ),
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

        layout.lock.write_text(json.dumps(stale) + "\n", encoding="utf-8")
        layout.lock.chmod(0o600)
        replacement = b'{"pid":999,"uid":501,"start":"2026-04-01T00:00:00Z"}\n'
        real_read = layout_module._read_lock_bytes

        def replace_stale_after_read(path: Path, uid: int) -> bytes:
            """在 stale takeover unlink 前用新 lock 替换 pathname。"""
            payload = real_read(path, uid)
            path.unlink()
            path.write_bytes(replacement)
            path.chmod(0o600)
            return payload

        with (
            mock.patch.object(layout_module, "_probe_process", return_value=("dead", None)),
            mock.patch.object(
                layout_module,
                "_read_lock_bytes",
                side_effect=replace_stale_after_read,
            ),
            self.assertRaisesRegex(InstallError, "install_locked"),
        ):
            InstallLock.acquire(layout)
        self.assertEqual(layout.lock.read_bytes(), replacement)

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

    def test_user_and_system_launcher_modes_are_traversable_but_not_writable(self) -> None:
        """system launcher 0700 会让目标用户无法执行，user 文件则不得扩大到 0755。"""
        user_layout = InstallLayout.user(self.home, version="0.7.0")
        install_launcher(user_layout)
        self.assertEqual(stat.S_IMODE(user_layout.program_prefix.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(user_layout.bin_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(user_layout.launcher.stat().st_mode), 0o700)

        system_prefix = self.root / "usr-local-lib" / "miniclaw"
        system_command = self.root / "usr-local-bin" / "miniclaw"
        state_home = self.home / ".miniclaw-system"
        state_home.mkdir(mode=0o700)
        with (
            mock.patch.object(layout_module, "_SYSTEM_PREFIX", system_prefix),
            mock.patch.object(layout_module, "_SYSTEM_COMMAND", system_command),
            mock.patch.object(layout_module, "_validate_system_prefix"),
        ):
            system_layout = InstallLayout.for_request(
                self.request(system_prefix=True, state_home=state_home),
                self.account,
            )
            install_launcher(system_layout)
        self.assertEqual(stat.S_IMODE(system_layout.program_prefix.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(system_layout.bin_dir.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(system_layout.launcher.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(state_home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(system_layout.program_prefix.parent.stat().st_mode), 0o755)

    def test_safe_existing_local_bin_0755_is_accepted(self) -> None:
        """owner 持有且不可组/全局写的 conventional ~/.local/bin 0755 应可复用。"""
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(mode=0o755, parents=True)
        local_bin.chmod(0o755)
        layout = InstallLayout.user(self.home, version="0.7.0")

        install_launcher(layout)

        self.assertEqual(stat.S_IMODE(local_bin.stat().st_mode), 0o755)
        self.assertTrue(layout.command_link.is_symlink())

    def test_launcher_create_failures_clean_only_owned_inode_and_retry(self) -> None:
        """file/link fsync 失败必须清理本次项，同时不能删除竞态 replacement。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        real_fsync = layout_module._fsync_directory
        failed = False

        def fail_launcher_once(path: Path) -> None:
            """首次 launcher parent fsync 失败。"""
            nonlocal failed
            if path == layout.launcher.parent and not failed:
                failed = True
                raise OSError("launcher fsync crash")
            real_fsync(path)

        with mock.patch.object(layout_module, "_fsync_directory", side_effect=fail_launcher_once):
            with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
                install_launcher(layout)
        self.assertFalse(layout.launcher.exists())
        self.assertFalse(layout.command_link.exists())
        install_launcher(layout)

        other = InstallLayout.user(self.home / "retry", version="0.7.0")

        def replace_then_fail(path: Path) -> None:
            """模拟 fsync 前 pathname 被换成 foreign regular file。"""
            if path == other.launcher.parent:
                other.launcher.unlink()
                other.launcher.write_bytes(b"replacement")
                other.launcher.chmod(0o700)
                raise OSError("replacement race")
            real_fsync(path)

        with mock.patch.object(layout_module, "_fsync_directory", side_effect=replace_then_fail):
            with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
                install_launcher(other)
        self.assertEqual(other.launcher.read_bytes(), b"replacement")

    def test_command_link_fsync_failure_cleans_link_and_launcher_for_retry(self) -> None:
        """command link parent fsync 失败不能遗留半安装 link 或阻塞重试。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        real_fsync = layout_module._fsync_directory

        def fail_link(path: Path) -> None:
            """只让 command link parent durability 失败。"""
            if path == layout.command_link.parent:
                raise OSError("link fsync crash")
            real_fsync(path)

        with mock.patch.object(layout_module, "_fsync_directory", side_effect=fail_link):
            with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
                install_launcher(layout)
        self.assertFalse(layout.launcher.exists())
        self.assertFalse(layout.command_link.exists())
        install_launcher(layout)

    def test_layout_cannot_be_directly_forged_outside_validated_factories(self) -> None:
        """公开 dataclass init 会绕过 symlink/owner/mode 和 command-link cross-field。"""
        layout = InstallLayout.user(self.home, version="0.7.0")
        values = {name: getattr(layout, name) for name in layout.__dataclass_fields__}
        values["command_link"] = self.root / "foreign-command"
        with self.assertRaises(TypeError):
            InstallLayout(**values)

    def test_private_build_still_enforces_no_follow_owner_and_cross_fields(self) -> None:
        """可从 runtime 调用的 `_build` 不能成为绕过 public factories 的后门。"""
        target = self.home / "target"
        target.mkdir(mode=0o700)
        linked = self.home / "linked"
        linked.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            InstallLayout._build(
                linked,
                self.home / ".miniclaw",
                self.home / ".local" / "bin" / "miniclaw",
                "0.7.0",
            )

    def test_for_plan_update_uses_user_home_not_existing_install_root(self) -> None:
        """已有 ~/.miniclaw 0700 是 update 目标，不得被误当成 forbidden Home root。"""
        prefix = self.home / ".miniclaw"
        prefix.mkdir(mode=0o700)
        request = self.request(prefix=prefix, state_home=prefix, onboard=True)

        with (
            mock.patch.object(
                layout_module,
                "_production_identity",
                create=True,
                return_value=(os.geteuid(), None, None),
            ),
            mock.patch.object(
                layout_module,
                "_resolve_invoking_user",
                create=True,
                return_value=self.account,
            ),
        ):
            layout = InstallLayout.for_plan(self.plan(request, prefix))

        self.assertEqual(layout.program_prefix, prefix)
        self.assertEqual(layout.command_link, self.home / ".local" / "bin" / "miniclaw")

    def test_for_plan_system_state_binds_real_target_passwd_home(self) -> None:
        """system plan 的 state UID 还必须绑定 passwd Home，不能只信最近 ancestor。"""
        system_prefix = self.root / "system" / "miniclaw"
        state_home = self.home / ".miniclaw-system"
        state_home.mkdir(mode=0o700)
        request = self.request(
            prefix=None,
            state_home=state_home,
            system_prefix=True,
            onboard=True,
        )
        plan = self.plan(request, system_prefix)
        with (
            mock.patch.object(layout_module, "_SYSTEM_PREFIX", system_prefix),
            mock.patch.object(layout_module, "_SYSTEM_COMMAND", self.root / "bin" / "miniclaw"),
            mock.patch.object(layout_module, "_validate_system_prefix"),
            mock.patch.object(
                layout_module,
                "_production_identity",
                create=True,
                return_value=(0, "alice", os.geteuid()),
            ),
            mock.patch.object(
                layout_module,
                "_resolve_invoking_user",
                create=True,
                side_effect=InstallError("privilege_denied", "platform"),
            ),
            self.assertRaisesRegex(InstallError, "privilege_denied"),
        ):
            InstallLayout.for_plan(plan)

    def test_for_plan_binds_production_passwd_home_not_custom_install_roots(self) -> None:
        """custom prefix/state 不能反推 Home；command 必须绑定 current/sudo passwd Home。"""
        identity_home = self.root / "identity-home"
        identity_home.mkdir(mode=0o700)
        account = _account(identity_home)
        prefix = self.home / "custom-program"
        state = self.home / "custom-state"
        request = self.request(prefix=prefix, state_home=state)
        production = mock.Mock(return_value=(0, "alice", os.geteuid()))
        resolver = mock.Mock(return_value=account)

        with (
            mock.patch.object(
                layout_module,
                "_production_identity",
                create=True,
                new=production,
            ),
            mock.patch.object(
                layout_module,
                "_resolve_invoking_user",
                create=True,
                new=resolver,
            ),
        ):
            layout = InstallLayout.for_plan(self.plan(request, prefix))

        self.assertEqual(layout.command_link, identity_home / ".local" / "bin" / "miniclaw")
        production.assert_called_once_with()
        resolver.assert_called_once_with(0, "alice", os.geteuid())

    def test_launcher_updates_are_inode_stable_or_fail_without_enoent_window(self) -> None:
        """成功重入保持目录项；内容/target 变化 fail closed，不能 remove-then-publish。"""
        prefix = self.home / "program"
        command = self.home / ".local" / "bin" / "miniclaw"
        first = InstallLayout._build(prefix, self.home / "state-a", command, "0.7.0")
        launcher_hash, link_hash = install_launcher(first)
        launcher_before = first.launcher.lstat()
        link_before = first.command_link.lstat()
        launcher_bytes = first.launcher.read_bytes()
        link_target = os.readlink(first.command_link)

        self.assertEqual(
            install_launcher(
                first,
                launcher_sha256=launcher_hash,
                command_link_sha256=link_hash,
            ),
            (launcher_hash, link_hash),
        )
        self.assertEqual(first.launcher.lstat().st_ino, launcher_before.st_ino)
        self.assertEqual(first.command_link.lstat().st_ino, link_before.st_ino)

        second = InstallLayout._build(prefix, self.home / "state-b", command, "0.7.0")
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            install_launcher(
                second,
                launcher_sha256=launcher_hash,
                command_link_sha256=link_hash,
            )
        self.assertEqual(first.launcher.read_bytes(), launcher_bytes)
        self.assertEqual(first.launcher.lstat().st_ino, launcher_before.st_ino)

        other_prefix = self.home / "other-program"
        link_update = InstallLayout._build(
            other_prefix,
            self.home / "state-b",
            command,
            "0.7.0",
        )
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            install_launcher(
                link_update,
                command_link_sha256=link_hash,
            )
        self.assertEqual(os.readlink(command), link_target)
        self.assertEqual(command.lstat().st_ino, link_before.st_ino)
        self.assertFalse(link_update.launcher.exists())


if __name__ == "__main__":
    unittest.main()
