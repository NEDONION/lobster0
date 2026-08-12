"""包安装（wheel / uv tool / pipx）下的 service 生命周期测试。"""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lobster0.gateway_service import ServiceError
from lobster0.install.orchestrator import InstallMethod, resolve_install_facts
from lobster0.install.package_service import (
    PackageServiceError,
    package_service_platform,
    resolve_package_launcher,
    run_package_service_action,
)
from lobster0.install.service import ServicePlatform, render_package_service_spec

FakeSystemctlRunner = runpy.run_path("tests/install/fake_systemctl.py")["FakeSystemctlRunner"]


class _Owned(unittest.TestCase):
    """提供 owner-only 临时 Home 与 state home。"""

    def setUp(self) -> None:
        """创建 0700 的临时 Home、state home 与假 launcher。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve(strict=True)
        root.chmod(0o700)
        self.home = root / "owner"
        self.home.mkdir(mode=0o700)
        self.state_home = self.home / ".lobster0"
        self.state_home.mkdir(mode=0o700)
        self.tool_bin = self.home / ".local" / "share" / "uv" / "tools" / "lobster0-agent" / "bin"
        self.tool_bin.mkdir(mode=0o700, parents=True)
        self.launcher = self.tool_bin / "lobster0"
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o700)


class PackageServiceSpecTests(_Owned):
    """验证包安装复用 install/service.py 的 unit 渲染。"""

    def test_systemd_unit_points_at_the_installed_executable(self) -> None:
        """ExecStart 必须是解析到的 launcher，并带自愈与 Secret 路径。"""
        spec = render_package_service_spec(
            launcher=self.launcher,
            state_home=self.state_home,
            platform=ServicePlatform.SYSTEMD_USER,
            user_home=self.home,
        )
        unit = spec.content.decode("utf-8")

        self.assertEqual(spec.label, "lobster0-gateway.service")
        self.assertEqual(
            spec.path,
            self.home / ".config" / "systemd" / "user" / "lobster0-gateway.service",
        )
        self.assertIn(
            f"ExecStart={self.launcher} gateway --home {self.state_home}\n",
            unit,
        )
        self.assertIn("Restart=on-failure\n", unit)
        self.assertIn("RestartSec=5\n", unit)
        self.assertIn("WantedBy=default.target\n", unit)
        self.assertIn(
            f"Environment=LOBSTER0_ENV_FILE={self.state_home / 'secrets.env'}\n",
            unit,
        )
        self.assertNotIn("User=", unit)
        self.assertNotIn("WorkingDirectory=", unit)

    def test_secrets_file_matches_the_runtime_dotenv_fallback(self) -> None:
        """unit 里的 LOBSTER0_ENV_FILE 必须与 StatePaths.secrets_file 完全一致。"""
        from lobster0.paths import build_state_paths

        paths = build_state_paths(self.state_home)
        spec = render_package_service_spec(
            launcher=self.launcher,
            state_home=self.state_home,
            platform=ServicePlatform.SYSTEMD_USER,
            user_home=self.home,
        )

        self.assertIn(
            f"Environment=LOBSTER0_ENV_FILE={paths.secrets_file}\n",
            spec.content.decode("utf-8"),
        )

    def test_launchd_plist_is_rendered_for_macos_package_installs(self) -> None:
        """macOS 包安装必须复用同一渲染器生成 LaunchAgent。"""
        spec = render_package_service_spec(
            launcher=self.launcher,
            state_home=self.state_home,
            platform=ServicePlatform.LAUNCHD,
            user_home=self.home,
        )

        self.assertEqual(spec.label, "io.lobster0.gateway")
        self.assertIn(b"<key>KeepAlive</key>", spec.content)
        self.assertIn(str(self.launcher).encode(), spec.content)


class LauncherResolutionTests(_Owned):
    """验证 ExecStart 的可执行文件解析顺序与失败姿态。"""

    def test_prefers_the_console_script_beside_the_interpreter(self) -> None:
        """venv / uv tool 安装的 console script 与解释器同目录。"""
        interpreter = self.tool_bin / "python"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o700)
        other = self.home / ".local" / "bin"
        other.mkdir(mode=0o700, parents=True)
        decoy = other / "lobster0"
        decoy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        decoy.chmod(0o700)

        resolved = resolve_package_launcher(
            executable=interpreter,
            scripts_paths=(other,),
        )

        self.assertEqual(resolved, self.launcher)

    def test_falls_back_to_the_scripts_directory_for_user_installs(self) -> None:
        """`pip install --user` 的解释器旁没有 console script。"""
        interpreter = self.home / "system" / "bin" / "python3"
        interpreter.parent.mkdir(mode=0o700, parents=True)
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o700)

        resolved = resolve_package_launcher(
            executable=interpreter,
            scripts_paths=(self.tool_bin,),
        )

        self.assertEqual(resolved, self.launcher)

    def test_fails_closed_with_a_stable_code_when_nothing_is_executable(self) -> None:
        """解析不到时必须 fail closed，绝不回退到 PATH 上的同名命令。"""
        self.launcher.chmod(0o600)
        interpreter = self.tool_bin / "python"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o700)

        with self.assertRaises(PackageServiceError) as captured:
            resolve_package_launcher(executable=interpreter, scripts_paths=())

        self.assertEqual(captured.exception.code, "service_executable_unresolved")
        self.assertTrue(captured.exception.hint)

    def test_platform_maps_linux_to_systemd_and_rejects_others(self) -> None:
        """只有 Linux 与 macOS 有受支持的用户级 service manager。"""
        self.assertIs(
            package_service_platform("linux"),
            ServicePlatform.SYSTEMD_USER,
        )
        self.assertIs(package_service_platform("darwin"), ServicePlatform.LAUNCHD)
        with self.assertRaises(PackageServiceError) as captured:
            package_service_platform("win32")

        self.assertEqual(captured.exception.code, "service_platform_unsupported")


class PackageServiceLifecycleTests(_Owned):
    """验证 install/status/logs/restart/uninstall 五个动作在包安装下可用。"""

    def _run(
        self,
        command: str,
        runner: object,
    ) -> tuple[int, str, str]:
        """在受控 platform/executable 下执行一次包安装 service 动作。"""
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = run_package_service_action(
            command,
            state_home=self.state_home,
            runner=runner,
            stdout=stdout,
            stderr=stderr,
            platform_name="linux",
            launcher=self.launcher,
            user_home=self.home,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def unit_path(self) -> Path:
        """返回包安装模式写入的固定 unit 路径。"""
        return self.home / ".config" / "systemd" / "user" / "lobster0-gateway.service"

    def receipt_path(self) -> Path:
        """返回包安装模式的 owner-only service receipt。"""
        return self.state_home / "run" / "service.json"

    def test_install_publishes_the_unit_and_records_an_owner_only_receipt(self) -> None:
        """安装后 unit 是 0600 文件，receipt 记录 label/path/hash。"""
        runner = FakeSystemctlRunner()

        code, output, error = self._run("install", runner)

        self.assertEqual((code, error), (0, ""))
        self.assertIn("service installed", output)
        unit = self.unit_path()
        self.assertTrue(unit.is_file())
        self.assertEqual(unit.stat().st_mode & 0o777, 0o600)
        document = json.loads(self.receipt_path().read_text(encoding="utf-8"))
        self.assertEqual(document["label"], "lobster0-gateway.service")
        self.assertEqual(document["path"], str(unit))
        self.assertEqual(
            document["sha256"],
            hashlib.sha256(unit.read_bytes()).hexdigest(),
        )
        self.assertEqual(self.receipt_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/bin/systemctl", "--user", "daemon-reload"),
                (
                    "/usr/bin/systemctl",
                    "--user",
                    "enable",
                    "--now",
                    "lobster0-gateway.service",
                ),
                (
                    "/usr/bin/systemctl",
                    "--user",
                    "is-active",
                    "lobster0-gateway.service",
                ),
            ],
        )

    def test_install_is_idempotent_and_status_reports_the_receipt(self) -> None:
        """重复安装不得因为已有自有文件而失败。"""
        self.assertEqual(self._run("install", FakeSystemctlRunner())[0], 0)

        code, output, error = self._run("install", FakeSystemctlRunner())
        self.assertEqual((code, error), (0, ""))

        code, output, error = self._run("status", FakeSystemctlRunner())
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output, "service installed=true running=true\n")

    def test_status_reports_not_installed_before_install(self) -> None:
        """未安装时 status 必须成功返回 false，而不是报错。"""
        code, output, error = self._run(
            "status",
            FakeSystemctlRunner(outcomes=(3,)),
        )

        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output, "service installed=false running=false\n")

    def test_logs_reads_the_user_journal(self) -> None:
        """systemd 下 logs 必须走 journalctl --user-unit。"""
        runner = FakeSystemctlRunner()

        code, output, error = self._run("logs", runner)

        self.assertEqual((code, error), (0, ""))
        self.assertEqual(
            runner.calls[0][0],
            ("/usr/bin/journalctl", "--user-unit", "lobster0-gateway.service"),
        )
        self.assertIn("active", output)

    def test_restart_uses_the_exact_manager_argv(self) -> None:
        """restart 必须是固定 argv，不接受任何注入。"""
        runner = FakeSystemctlRunner()

        code, _output, error = self._run("restart", runner)

        self.assertEqual((code, error), (0, ""))
        self.assertEqual(
            runner.calls[0][0],
            ("/usr/bin/systemctl", "--user", "restart", "lobster0-gateway.service"),
        )

    def test_uninstall_removes_the_unit_and_the_receipt(self) -> None:
        """install 之后 uninstall 必须能在同一模式下清干净。"""
        self.assertEqual(self._run("install", FakeSystemctlRunner())[0], 0)

        code, output, error = self._run("uninstall", FakeSystemctlRunner())

        self.assertEqual((code, error), (0, ""))
        self.assertIn("service uninstalled", output)
        self.assertFalse(self.unit_path().exists())
        self.assertFalse(self.receipt_path().exists())

    def test_uninstall_without_any_install_is_a_clean_success(self) -> None:
        """没装过时 uninstall 不得报错。"""
        code, output, error = self._run("uninstall", FakeSystemctlRunner())

        self.assertEqual((code, error), (0, ""))
        self.assertIn("service uninstalled", output)

    def test_refuses_to_overwrite_a_unit_it_does_not_own(self) -> None:
        """已有同名 unit 但没有 receipt 时必须拒绝覆盖。"""
        unit = self.unit_path()
        unit.parent.mkdir(mode=0o700, parents=True)
        unit.write_text("[Unit]\nDescription=foreign\n", encoding="utf-8")
        unit.chmod(0o600)

        code, output, error = self._run("install", FakeSystemctlRunner())

        self.assertEqual((code, output), (5, ""))
        self.assertIn("service_file_unowned", error)
        self.assertIn("foreign", unit.read_text(encoding="utf-8"))

    def test_unknown_command_is_a_request_error(self) -> None:
        """未知动作必须返回请求错误码而不是 lifecycle 错误。"""
        code, output, error = self._run("reload", FakeSystemctlRunner())

        self.assertEqual((code, output), (2, ""))
        self.assertIn("request_invalid", error)

    def test_unresolvable_executable_fails_before_writing_anything(self) -> None:
        """launcher 解析失败必须在写文件前退出并给出可执行指引。"""
        stdout = io.StringIO()
        stderr = io.StringIO()
        interpreter = self.home / "nowhere" / "bin" / "python3"

        code = run_package_service_action(
            "install",
            state_home=self.state_home,
            runner=FakeSystemctlRunner(),
            stdout=stdout,
            stderr=stderr,
            platform_name="linux",
            executable=interpreter,
            scripts_paths=(),
            user_home=self.home,
        )

        self.assertEqual((code, stdout.getvalue()), (2, ""))
        self.assertIn("service_executable_unresolved", stderr.getvalue())
        self.assertFalse(self.unit_path().exists())


class InstallMethodTests(_Owned):
    """验证 CLI 与 Doctor 共用的三态安装模式判定。"""

    def test_site_packages_module_is_reported_as_a_package_install(self) -> None:
        """wheel / uv tool 安装的模块位于 site-packages 下。"""
        package_dir = self.home / "venv" / "lib" / "python3.12" / "site-packages" / "lobster0"

        facts = resolve_install_facts(
            self.state_home,
            environ={},
            executable=self.home / "venv" / "bin" / "python",
            user_home=self.home,
            package_dir=package_dir,
        )

        self.assertFalse(facts.managed)
        self.assertIs(facts.method, InstallMethod.PACKAGE)
        self.assertEqual(facts.detail, "package")

    def test_source_tree_module_stays_a_source_checkout(self) -> None:
        """src 布局的工作树必须保持 SOURCE，删掉 .git 也不能降级成包安装。"""
        package_dir = self.home / "repo" / "src" / "lobster0"

        facts = resolve_install_facts(
            self.state_home,
            environ={},
            executable=self.home / "repo" / ".venv" / "bin" / "python",
            user_home=self.home,
            package_dir=package_dir,
        )

        self.assertIs(facts.method, InstallMethod.SOURCE)
        self.assertEqual(facts.detail, "source")

    def test_degraded_receipt_never_degrades_into_package_mode(self) -> None:
        """受管安装的 receipt 损坏时不得改走包安装路径。"""
        prefix = self.home / ".lobster0"
        (prefix / "install-receipt.json").write_text("{", encoding="utf-8")
        package_dir = (
            prefix / "runtimes" / "0.7.0" / "venv" / "lib" / "python3.12" / "site-packages"
        ) / "lobster0"

        facts = resolve_install_facts(
            prefix,
            environ={"LOBSTER0_PREFIX": str(prefix)},
            executable=self.home / "venv" / "bin" / "python",
            user_home=self.home,
            package_dir=package_dir,
        )

        self.assertFalse(facts.managed)
        self.assertIs(facts.method, InstallMethod.SOURCE)
        self.assertEqual(facts.detail, "receipt_invalid")

    def test_this_checkout_is_reported_as_source(self) -> None:
        """本仓库自身运行时必须判定为源码 checkout。"""
        facts = resolve_install_facts(self.state_home, environ={}, user_home=self.home)

        self.assertIs(facts.method, InstallMethod.SOURCE)


class PackageServiceCliTests(_Owned):
    """验证 CLI 把包安装分发到新的 systemd/launchd 路径。"""

    def test_cli_dispatches_package_installs_to_the_package_service(self) -> None:
        """包安装的 service install 不得再走 Git provenance。"""
        from lobster0.cli import main

        with (
            mock.patch(
                "lobster0.cli.resolve_install_facts",
                return_value=mock.Mock(managed=False, method=InstallMethod.PACKAGE),
            ),
            mock.patch("lobster0.cli._package_service_preflight"),
            mock.patch(
                "lobster0.cli.run_package_service_action",
                return_value=0,
            ) as action,
            mock.patch(
                "lobster0.cli._launchd_service",
                side_effect=AssertionError("source checkout path must not run"),
            ),
        ):
            code = main(["service", "--home", str(self.state_home), "install"])

        self.assertEqual(code, 0)
        self.assertEqual(action.call_args.args[0], "install")
        self.assertEqual(action.call_args.kwargs["state_home"], self.state_home)

    def test_source_checkout_still_goes_through_git_provenance(self) -> None:
        """收窄作用域不等于放宽：源码 checkout 必须仍然绑定干净 commit。"""
        from lobster0.cli import main

        with (
            mock.patch(
                "lobster0.cli.resolve_install_facts",
                return_value=mock.Mock(managed=False, method=InstallMethod.SOURCE),
            ),
            mock.patch(
                "lobster0.cli.run_package_service_action",
                side_effect=AssertionError("package path must not run for a checkout"),
            ),
            mock.patch("lobster0.cli.render_launchd_service"),
            mock.patch("lobster0.cli.LaunchdService"),
            mock.patch("lobster0.cli.sys.executable", str(self.tool_bin / "python")),
            mock.patch(
                "lobster0.cli._service_repository_commit",
                side_effect=ServiceError("service_repository_dirty"),
            ) as provenance,
        ):
            code = main(["service", "--home", str(self.state_home), "install"])

        self.assertEqual(code, 5)
        provenance.assert_called_once()

    def test_dirty_source_checkout_cannot_reach_the_package_service(self) -> None:
        """本仓库自身（工作树）永远不会被分发到包安装路径。"""
        from lobster0.cli import _run_service

        arguments = mock.Mock(service_command="install")
        paths = mock.Mock(home=self.state_home)
        with (
            mock.patch(
                "lobster0.cli.run_package_service_action",
                side_effect=AssertionError("package path must not run for a checkout"),
            ),
            mock.patch(
                "lobster0.cli._launchd_service",
                side_effect=ServiceError("service_repository_dirty"),
            ),
        ):
            self.assertEqual(_run_service(paths, arguments), 5)

    def test_package_install_preflight_requires_one_enabled_channel(self) -> None:
        """装成常驻服务前必须至少启用一个 Channel（由上游 fail closed 保证）。"""
        from lobster0.cli import _package_service_preflight
        from lobster0.gateway import GatewayConfigError
        from lobster0.paths import build_state_paths

        paths = build_state_paths(self.state_home)
        with (
            mock.patch("lobster0.cli.load_dotenv"),
            mock.patch("lobster0.cli.load_config"),
            mock.patch(
                "lobster0.cli.collect_enabled_channels",
                side_effect=GatewayConfigError("no_channels_enabled"),
            ),
            mock.patch(
                "lobster0.cli.run_local_checks",
                side_effect=AssertionError("Doctor must not run after a config failure"),
            ),
        ):
            with self.assertRaises(GatewayConfigError):
                _package_service_preflight(paths)

    def test_package_install_surfaces_the_config_failure_as_exit_code_two(self) -> None:
        """preflight 失败必须在写 unit 之前返回配置错误码。"""
        from lobster0.cli import _run_package_service
        from lobster0.gateway import GatewayConfigError
        from lobster0.paths import build_state_paths

        paths = build_state_paths(self.state_home)
        with (
            mock.patch(
                "lobster0.cli._package_service_preflight",
                side_effect=GatewayConfigError("no_channels_enabled"),
            ),
            mock.patch(
                "lobster0.cli.run_package_service_action",
                side_effect=AssertionError("must not reach the service layer"),
            ),
        ):
            self.assertEqual(_run_package_service(paths, "install"), 2)

    def test_package_install_preflight_accepts_non_feishu_channels(self) -> None:
        """包安装不继承 Phase 6 的 Feishu-only 范围闸门。"""
        from lobster0.cli import _package_service_preflight
        from lobster0.doctor import CheckResult, CheckStatus
        from lobster0.paths import build_state_paths

        paths = build_state_paths(self.state_home)
        checks = (
            CheckResult("workspace", CheckStatus.PASS, "ready"),
            CheckResult("pi_tui", CheckStatus.FAIL, "not built"),
        )
        with (
            mock.patch("lobster0.cli.load_dotenv") as load,
            mock.patch("lobster0.cli.load_config"),
            mock.patch(
                "lobster0.cli.collect_enabled_channels",
                return_value=("telegram", "discord"),
            ),
            mock.patch("lobster0.cli.run_local_checks", return_value=checks),
        ):
            _package_service_preflight(paths)

        self.assertEqual(load.call_args.args[0], paths.secrets_file)


class RenderedUnitVerificationTests(_Owned):
    """在有 systemd 的主机上用 systemd-analyze 校验渲染出的 unit。"""

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/usr/bin/systemd-analyze").is_file(),
        "systemd-analyze is only available on a systemd host",
    )
    def test_rendered_unit_passes_systemd_analyze_verify(self) -> None:
        """渲染出的 unit 必须通过 systemd 自带的静态校验。"""
        import subprocess

        spec = render_package_service_spec(
            launcher=self.launcher,
            state_home=self.state_home,
            platform=ServicePlatform.SYSTEMD_USER,
            user_home=self.home,
        )
        unit = self.home / "lobster0-gateway.service"
        unit.write_bytes(spec.content)
        result = subprocess.run(
            ("/usr/bin/systemd-analyze", "--user", "verify", str(unit)),
            capture_output=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)},
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
