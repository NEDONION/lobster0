"""MiniClaw owned macOS LaunchAgent 生命周期测试。"""

import hashlib
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from miniclaw.install.service import (
    LaunchdService,
    ServiceError,
    render_launchd_service,
)


class _Runner:
    """记录 exact argv，并按 operation 返回受控 launchctl 结果。"""

    def __init__(self) -> None:
        """初始化调用记录与可覆盖返回码。"""
        self.argvs: list[tuple[str, ...]] = []
        self.returncodes: dict[str, int] = {}
        self.running = True

    def __call__(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        """返回不含本机数据的最小 CompletedProcess。"""
        self.argvs.append(argv)
        operation = argv[1]
        stdout = b"state = running\n" if operation == "print" and self.running else b""
        return subprocess.CompletedProcess(
            argv,
            self.returncodes.get(operation, 0),
            stdout=stdout,
            stderr=b"",
        )


class LaunchdServiceTest(unittest.TestCase):
    """验证 plist、ownership receipt、幂等与 exact launchctl argv。"""

    def setUp(self) -> None:
        """创建不接触真实 Home/launchd 的服务布局。"""
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.state_home = self.root / "state"
        self.state_home.mkdir(mode=0o700)
        (self.state_home / "run").mkdir(mode=0o700)
        (self.state_home / "logs").mkdir(mode=0o700)
        self.launch_agents = self.root / "LaunchAgents"
        self.launch_agents.mkdir()
        self.working_directory = self.root / "project"
        self.working_directory.mkdir()
        self.launcher = self.root / "runtime" / "bin" / "miniclaw"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.dotenv = self.root / "private.env"
        self.dotenv.write_text("SECRET_SENTINEL=private\n", encoding="utf-8")
        self.dotenv.chmod(0o600)
        self.spec = render_launchd_service(
            launcher=self.launcher,
            state_home=self.state_home,
            working_directory=self.working_directory,
            launch_agents=self.launch_agents,
            dotenv_path=self.dotenv,
            commit="a" * 40,
        )
        self.runner = _Runner()
        self.service = LaunchdService(
            self.spec,
            runner=self.runner,
            uid=os.getuid(),
            platform="darwin",
        )

    def test_plist_uses_exact_launcher_home_and_no_secret_value(self) -> None:
        """plist 只能保存固定 argv 与 dotenv path，不能复制 Secret 内容。"""
        value = plistlib.loads(self.spec.content)

        self.assertEqual(value["Label"], "io.miniclaw.gateway")
        self.assertEqual(
            value["ProgramArguments"],
            [str(self.launcher), "gateway", "--home", str(self.state_home)],
        )
        self.assertEqual(value["WorkingDirectory"], str(self.working_directory))
        self.assertEqual(value["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(
            value["EnvironmentVariables"],
            {
                "MINICLAW_ENV_FILE": str(self.dotenv),
                "MINICLAW_GATEWAY_COMMIT": "a" * 40,
            },
        )
        self.assertNotIn(b"SECRET_SENTINEL", self.spec.content)
        self.assertEqual(self.spec.sha256, hashlib.sha256(self.spec.content).hexdigest())

    def test_plist_rejects_invalid_commit_provenance(self) -> None:
        """LaunchAgent 不能以 unknown、dirty 占位符或非 40-hex 启动。"""
        with self.assertRaisesRegex(ServiceError, "service_spec_invalid"):
            render_launchd_service(
                launcher=self.launcher,
                state_home=self.state_home,
                working_directory=self.working_directory,
                launch_agents=self.launch_agents,
                dotenv_path=self.dotenv,
                commit="unknown",
            )

    def test_install_is_owner_only_and_idempotent_when_running(self) -> None:
        """重复 install 不能覆盖未知文件，也不能重复 bootstrap 已运行 job。"""
        self.service.install()
        first_argvs = tuple(self.runner.argvs)
        self.service.install()

        self.assertTrue(self.spec.path.is_file())
        self.assertEqual(stat.S_IMODE(self.spec.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.spec.receipt_path.stat().st_mode), 0o600)
        self.assertEqual(first_argvs[0][:2], ("/usr/bin/plutil", "-lint"))
        self.assertEqual(first_argvs[1], (
            "/bin/launchctl",
            "bootstrap",
            f"gui/{os.getuid()}",
            str(self.spec.path),
        ))
        self.assertEqual(
            sum(argv[1] == "bootstrap" for argv in self.runner.argvs),
            1,
        )

    def test_restart_and_status_use_exact_gui_domain_and_label(self) -> None:
        """status/restart 不接受 caller 提供 label、domain 或 launchctl path。"""
        self.service.install()

        status = self.service.status()
        self.service.restart()

        self.assertTrue(status.installed)
        self.assertTrue(status.loaded)
        self.assertTrue(status.running)
        self.assertIn(
            ("/bin/launchctl", "print", f"gui/{os.getuid()}/io.miniclaw.gateway"),
            self.runner.argvs,
        )
        self.assertEqual(
            self.runner.argvs[-1],
            (
                "/bin/launchctl",
                "kickstart",
                "-k",
                f"gui/{os.getuid()}/io.miniclaw.gateway",
            ),
        )

    def test_foreign_plist_is_never_overwritten_or_deleted(self) -> None:
        """没有匹配 receipt 的同名 plist 必须视为其他应用拥有。"""
        self.spec.path.write_bytes(b"foreign")
        self.spec.path.chmod(0o600)

        for operation in (self.service.install, self.service.uninstall):
            with self.subTest(operation=operation.__name__), self.assertRaisesRegex(
                ServiceError, "service_file_unmanaged"
            ):
                operation()

        self.assertEqual(self.spec.path.read_bytes(), b"foreign")
        self.assertEqual(self.runner.argvs, [])

    def test_failed_first_bootstrap_rolls_back_files(self) -> None:
        """launchctl 拒绝新 job 时不能留下半安装 plist/receipt。"""
        self.runner.returncodes["bootstrap"] = 5

        with self.assertRaisesRegex(ServiceError, "service_manager_failed"):
            self.service.install()

        self.assertFalse(self.spec.path.exists())
        self.assertFalse(self.spec.receipt_path.exists())

    def test_uninstall_boots_out_owned_job_and_is_idempotent(self) -> None:
        """uninstall 只删除匹配 receipt 的文件，重复调用无副作用。"""
        self.service.install()
        self.service.uninstall()
        count = len(self.runner.argvs)
        self.service.uninstall()

        self.assertFalse(self.spec.path.exists())
        self.assertFalse(self.spec.receipt_path.exists())
        self.assertEqual(len(self.runner.argvs), count)
        self.assertIn(
            (
                "/bin/launchctl",
                "bootout",
                f"gui/{os.getuid()}",
                str(self.spec.path),
            ),
            self.runner.argvs,
        )

    def test_root_or_non_macos_service_fails_before_file_or_command(self) -> None:
        """LaunchAgent 只能由非 root macOS 当前用户管理。"""
        candidates = (
            LaunchdService(self.spec, runner=self.runner, uid=0, platform="darwin"),
            LaunchdService(self.spec, runner=self.runner, uid=501, platform="linux"),
        )

        for service in candidates:
            with self.subTest(service=service), self.assertRaisesRegex(
                ServiceError, "service_platform_unsupported"
            ):
                service.install()

        self.assertFalse(self.spec.path.exists())
        self.assertEqual(self.runner.argvs, [])


if __name__ == "__main__":
    unittest.main()
