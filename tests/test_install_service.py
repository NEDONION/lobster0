"""验证 systemd user 与 LaunchAgent 的受管 lifecycle。"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import runpy
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miniclaw.install import receipt as receipt_module
from miniclaw.install import service as service_module
from miniclaw.install.layout import InstallLayout
from miniclaw.install.models import InstallError
from miniclaw.install.service import (
    ServicePlatform,
    ServiceSpec,
    render_service_spec,
    service_install,
    service_logs,
    service_restart,
    service_status,
    service_uninstall,
)

FakeSystemctlRunner = runpy.run_path("tests/install/fake_systemctl.py")["FakeSystemctlRunner"]
FakeLaunchctlRunner = runpy.run_path("tests/install/fake_launchctl.py")["FakeLaunchctlRunner"]


class InstallServiceTests(unittest.TestCase):
    """覆盖 renderer、manager argv、ownership 与 crash rollback。"""

    def setUp(self) -> None:
        """创建不接触真实 Home/service manager 的 owner-only layout。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve(strict=True)
        root.chmod(0o700)
        self.home = root / "owner"
        self.home.mkdir(mode=0o700)
        self.layout = InstallLayout.user(self.home, version="0.7.0")
        self.uid = os.geteuid()

    def systemd(self) -> ServiceSpec:
        """返回当前临时 layout 的 systemd user spec。"""
        return render_service_spec(self.layout, ServicePlatform.SYSTEMD_USER)

    def launchd(self) -> ServiceSpec:
        """返回当前临时 layout 的 LaunchAgent spec。"""
        return render_service_spec(self.layout, ServicePlatform.LAUNCHD)

    def test_systemd_unit_is_exact_user_service_without_secret_value(self) -> None:
        """加入 root/User/WorkingDirectory 或环境继承会破坏 user-only 边界。"""
        sentinel = "sentinel-service-secret"
        with mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": sentinel}):
            spec = self.systemd()
        text = spec.content.decode("utf-8")
        self.assertEqual(spec.label, "miniclaw-gateway.service")
        self.assertEqual(
            spec.path,
            self.home / ".config/systemd/user/miniclaw-gateway.service",
        )
        self.assertIn(
            f"ExecStart={self.layout.launcher} gateway --home {self.layout.state_home}",
            text,
        )
        self.assertIn("Environment=PATH=/usr/local/bin:/usr/bin:/bin", text)
        self.assertIn(f"Environment=MINICLAW_ENV_FILE={self.layout.secrets_file}", text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("RestartSec=5", text)
        self.assertIn("TimeoutStopSec=30", text)
        self.assertIn("UMask=0077", text)
        self.assertNotIn("User=", text)
        self.assertNotIn("WorkingDirectory=", text)
        self.assertNotIn(sentinel, text)

    def test_systemd_escapes_spaces_quotes_backslashes_and_specifiers(self) -> None:
        """未转义路径会拆 argv，单个百分号会触发 systemd specifier。"""
        home = self.home.parent / 'owner %i "quoted" \\ path'
        home.mkdir(mode=0o700)
        layout = InstallLayout.user(home, version="0.7.0")
        text = render_service_spec(layout, ServicePlatform.SYSTEMD_USER).content.decode()
        exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        self.assertIn("%%i", exec_line)
        self.assertIn('\\"quoted\\"', exec_line)
        self.assertIn("\\\\", exec_line)
        self.assertIsNone(re.search(r"(?<!%)%(?!%)", exec_line))

    def test_systemd_exec_and_environment_use_distinct_exact_escaping(self) -> None:
        """ExecStart 必须 literal 化 `$`/`%`，Environment 则不得破坏 `$`。"""
        home = self.home.parent / 'owner ${PATH} $USER apostrophe\'s;semi %i space "quote" \\slash'
        home.mkdir(mode=0o700)
        layout = InstallLayout.user(home, version="0.7.0")
        spec = render_service_spec(layout, ServicePlatform.SYSTEMD_USER)
        text = spec.content.decode("utf-8")
        exec_value = next(
            line.removeprefix("ExecStart=")
            for line in text.splitlines()
            if line.startswith("ExecStart=")
        )
        environment_value = next(
            line.removeprefix("Environment=")
            for line in text.splitlines()
            if line.startswith("Environment=MINICLAW_ENV_FILE=")
            or line.startswith('Environment="MINICLAW_ENV_FILE=')
        )
        arguments = (
            str(layout.launcher),
            "gateway",
            "--home",
            str(layout.state_home),
        )
        self.assertIn("$${PATH}", exec_value)
        self.assertIn("$$USER", exec_value)
        self.assertIn("apostrophe's;semi", exec_value)
        self.assertIn("%%i", exec_value)
        self.assertIn('\\"quote\\"', exec_value)
        self.assertIn("\\\\slash", exec_value)
        self.assertEqual(service_module._parse_systemd_exec(exec_value), arguments)

        self.assertIn("${PATH}", environment_value)
        self.assertIn("$USER", environment_value)
        self.assertNotIn("$${PATH}", environment_value)
        self.assertIn("%%i", environment_value)
        self.assertEqual(
            service_module._parse_systemd_environment(environment_value),
            (f"MINICLAW_ENV_FILE={layout.secrets_file}",),
        )

    def test_systemd_apostrophe_only_paths_are_double_quoted_and_exact(self) -> None:
        """仅含 apostrophe 的路径也必须双引号编码，裸 apostrophe 必须拒绝。"""
        home = self.home.parent / "owner'apostrophe"
        home.mkdir(mode=0o700)
        layout = InstallLayout.user(home, version="0.7.0")
        spec = render_service_spec(layout, ServicePlatform.SYSTEMD_USER)
        text = spec.content.decode("utf-8")
        exec_value = next(
            line.removeprefix("ExecStart=")
            for line in text.splitlines()
            if line.startswith("ExecStart=")
        )
        environment_value = next(
            line.removeprefix("Environment=")
            for line in text.splitlines()
            if line.startswith("Environment=MINICLAW_ENV_FILE=")
            or line.startswith('Environment="MINICLAW_ENV_FILE=')
        )

        self.assertEqual(
            exec_value,
            f'"{layout.launcher}" gateway --home "{layout.state_home}"',
        )
        self.assertEqual(
            environment_value,
            f'"MINICLAW_ENV_FILE={layout.secrets_file}"',
        )
        self.assertEqual(
            service_module._parse_systemd_exec(exec_value),
            (str(layout.launcher), "gateway", "--home", str(layout.state_home)),
        )
        self.assertEqual(
            service_module._parse_systemd_environment(environment_value),
            (f"MINICLAW_ENV_FILE={layout.secrets_file}",),
        )
        with self.assertRaisesRegex(InstallError, "service_install_failed"):
            service_module._parse_systemd_exec("/tmp/owner'apostrophe")
        with self.assertRaisesRegex(InstallError, "service_install_failed"):
            service_module._parse_systemd_environment("NAME=owner'apostrophe")

    def test_launchd_plist_uses_exact_arguments_environment_and_owner_logs(self) -> None:
        """字符串命令或相对日志路径会重新引入 shell/工作目录依赖。"""
        sentinel = "sentinel-launchd-secret"
        with mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": sentinel}):
            spec = self.launchd()
        document = plistlib.loads(spec.content)
        self.assertEqual(
            set(document),
            {
                "EnvironmentVariables",
                "KeepAlive",
                "Label",
                "ProcessType",
                "ProgramArguments",
                "RunAtLoad",
                "StandardErrorPath",
                "StandardOutPath",
                "Umask",
            },
        )
        self.assertEqual(document["Label"], "io.miniclaw.gateway")
        self.assertEqual(
            document["ProgramArguments"],
            [str(self.layout.launcher), "gateway", "--home", str(self.layout.state_home)],
        )
        self.assertEqual(
            document["EnvironmentVariables"],
            {
                "MINICLAW_ENV_FILE": str(self.layout.secrets_file),
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            },
        )
        self.assertEqual(document["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(document["ProcessType"], "Background")
        self.assertEqual(
            document["StandardOutPath"],
            str(self.layout.state_home / "logs/gateway.stdout.log"),
        )
        self.assertEqual(
            document["StandardErrorPath"],
            str(self.layout.state_home / "logs/gateway.stderr.log"),
        )
        self.assertNotIn(sentinel, spec.content.decode())

    def test_service_spec_direct_constructor_is_closed_world(self) -> None:
        """伪造 platform、label、content 或 manager argv 不得通过构造态。"""
        for spec in (self.systemd(), self.launchd()):
            values = {
                field: getattr(spec, field)
                for field in spec.__dataclass_fields__
                if not field.startswith("_")
            }
            with self.subTest(platform=spec.platform), self.assertRaises(TypeError):
                ServiceSpec(**values)

    def test_lifecycle_rejects_unsealed_or_mutated_spec_for_both_platforms(self) -> None:
        """复制 public fields 或篡改 sealed spec 不得绕过 canonical layout 绑定。"""
        for spec in (self.systemd(), self.launchd()):
            unsealed = object.__new__(ServiceSpec)
            for field in spec.__dataclass_fields__:
                if not field.startswith("_"):
                    object.__setattr__(unsealed, field, getattr(spec, field))
            with (
                self.subTest(platform=spec.platform, case="unsealed"),
                self.assertRaisesRegex(InstallError, "service_install_failed"),
            ):
                service_status(unsealed, FakeSystemctlRunner())

            copied = object.__new__(ServiceSpec)
            for field in spec.__dataclass_fields__:
                object.__setattr__(copied, field, getattr(spec, field))
            with (
                self.subTest(platform=spec.platform, case="copied-seal"),
                self.assertRaisesRegex(InstallError, "service_install_failed"),
            ):
                service_status(copied, FakeSystemctlRunner())

            for field, value in (
                ("path", spec.path.with_name("forged.service")),
                ("content", spec.content + b"forged\n"),
                ("install_argvs", (("/bin/true",),)),
                ("status_argv", ("/bin/true",)),
                ("restart_argv", ("/bin/true",)),
                ("uninstall_argvs", (("/bin/true",),)),
            ):
                forged = object.__new__(ServiceSpec)
                for name in spec.__dataclass_fields__:
                    object.__setattr__(forged, name, getattr(spec, name))
                object.__setattr__(forged, field, value)
                with (
                    self.subTest(platform=spec.platform, field=field),
                    self.assertRaisesRegex(InstallError, "service_install_failed"),
                ):
                    service_status(forged, FakeSystemctlRunner())

    def test_lifecycle_rejects_synchronized_public_and_evidence_tampering(self) -> None:
        """同步改写 spec 与内部 evidence 也不得替换 render 时的可信 snapshot。"""
        for platform in (ServicePlatform.SYSTEMD_USER, ServicePlatform.LAUNCHD):
            for case in ("same-evidence-fields", "replacement-evidence"):
                with self.subTest(platform=platform, case=case):
                    spec = render_service_spec(self.layout, platform)
                    alternate_home = self.home.parent / f"alternate-{platform.value}-{case}"
                    alternate_home.mkdir(mode=0o700)
                    replacement = render_service_spec(
                        InstallLayout.user(alternate_home, version="0.7.0"),
                        platform,
                    )
                    for field in (
                        "platform",
                        "label",
                        "path",
                        "content",
                        "install_argvs",
                        "status_argv",
                        "restart_argv",
                        "uninstall_argvs",
                    ):
                        object.__setattr__(spec, field, getattr(replacement, field))
                    if case == "same-evidence-fields":
                        for field in (
                            "platform",
                            "owner_uid",
                            "home",
                            "launcher",
                            "state_home",
                            "secrets_file",
                            "stdout_log",
                            "stderr_log",
                        ):
                            object.__setattr__(
                                spec._evidence,
                                field,
                                getattr(replacement._evidence, field),
                            )
                    else:
                        object.__setattr__(spec, "_evidence", replacement._evidence)
                        object.__setattr__(replacement._evidence, "bound_spec", spec)

                    with self.assertRaisesRegex(InstallError, "service_install_failed"):
                        service_status(spec, FakeSystemctlRunner())

            for field, value in (("seal", object()), ("bound_spec", object())):
                with self.subTest(platform=platform, evidence_field=field):
                    spec = render_service_spec(self.layout, platform)
                    object.__setattr__(spec._evidence, field, value)
                    with self.assertRaisesRegex(InstallError, "service_install_failed"):
                        service_status(spec, FakeSystemctlRunner())

    def test_render_rejects_root_and_control_paths(self) -> None:
        """Gateway 不得由 UID 0 注册，控制字符也不得进入 unit/plist。"""
        with (
            mock.patch("miniclaw.install.service.os.geteuid", return_value=0),
            self.assertRaisesRegex(InstallError, "service_install_failed"),
        ):
            self.systemd()
        forged = object.__new__(InstallLayout)
        for field in self.layout.__dataclass_fields__:
            object.__setattr__(forged, field, getattr(self.layout, field))
        object.__setattr__(forged, "state_home", Path(str(self.layout.state_home) + "\nunsafe"))
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            render_service_spec(forged, ServicePlatform.SYSTEMD_USER)

    def test_systemd_lifecycle_uses_exact_argv_closed_env_and_order(self) -> None:
        """shell、继承环境或命令乱序会偏离可审计 manager 契约。"""
        spec = self.systemd()
        runner = FakeSystemctlRunner()
        with mock.patch("miniclaw.install.service._systemd_analyze_available", return_value=True):
            digest = service_install(spec, runner)
        self.assertEqual(digest, hashlib.sha256(spec.content).hexdigest())
        self.assertEqual(stat.S_IMODE(spec.path.stat().st_mode), 0o600)
        self.assertEqual(runner.calls[0][0][:-1], ("/usr/bin/systemd-analyze", "--user", "verify"))
        self.assertEqual(
            [call[0] for call in runner.calls[1:]],
            [
                ("/usr/bin/systemctl", "--user", "daemon-reload"),
                ("/usr/bin/systemctl", "--user", "enable", "--now", spec.label),
                ("/usr/bin/systemctl", "--user", "is-active", spec.label),
            ],
        )
        # Validator temp 已原子发布；只校验其 argv 末项不是最终路径且其余 argv 固定。
        self.assertNotEqual(runner.calls[0][0][-1], str(spec.path))
        expected_env = {
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{self.uid}/bus",
            "HOME": str(self.home),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_RUNTIME_DIR": f"/run/user/{self.uid}",
        }
        self.assertTrue(all(call[1] == expected_env and call[2] == 30.0 for call in runner.calls))

        runner.calls.clear()
        self.assertTrue(service_status(spec, runner))
        service_logs(spec, runner)
        service_restart(spec, runner)
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/bin/systemctl", "--user", "is-active", spec.label),
                ("/usr/bin/journalctl", "--user-unit", spec.label),
                ("/usr/bin/systemctl", "--user", "restart", spec.label),
            ],
        )
        runner.calls.clear()
        service_uninstall(spec, runner, expected_sha256=digest)
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/bin/systemctl", "--user", "disable", "--now", spec.label),
                ("/usr/bin/systemctl", "--user", "daemon-reload"),
            ],
        )
        self.assertFalse(spec.path.exists())

    def test_launchd_lifecycle_binds_gui_domain_and_lints_before_publish(self) -> None:
        """user domain 不绑定 UID 或 publish 先于 plutil 会误控其他 session。"""
        spec = self.launchd()
        runner = FakeLaunchctlRunner()
        digest = service_install(spec, runner)
        domain = f"gui/{self.uid}"
        target = f"{domain}/{spec.label}"
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/bin/plutil", "-lint", runner.calls[0][0][-1]),
                ("/bin/launchctl", "bootstrap", domain, str(spec.path)),
                ("/bin/launchctl", "print", target),
            ],
        )
        self.assertNotEqual(runner.calls[0][0][-1], str(spec.path))
        self.assertEqual(stat.S_IMODE((self.layout.state_home / "logs").stat().st_mode), 0o700)
        runner.calls.clear()
        self.assertTrue(service_status(spec, runner))
        service_logs(spec, runner)
        service_restart(spec, runner)
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/bin/launchctl", "print", target),
                (
                    "/usr/bin/tail",
                    "-n",
                    "200",
                    str(self.layout.state_home / "logs/gateway.stdout.log"),
                    str(self.layout.state_home / "logs/gateway.stderr.log"),
                ),
                ("/bin/launchctl", "kickstart", "-k", target),
            ],
        )
        runner.calls.clear()
        service_uninstall(spec, runner, expected_sha256=digest)
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/bin/launchctl", "bootout", target),
            ],
        )

    def test_launchd_replacement_boots_out_active_target_before_bootstrap(self) -> None:
        """active label collision 必须先按 service target bootout，再发布新定义。"""
        spec = self.launchd()
        domain = f"gui/{self.uid}"
        target = f"{domain}/{spec.label}"
        old = plistlib.dumps({"Label": spec.label, "OldDefinition": True})
        spec.path.parent.mkdir(parents=True, mode=0o700)
        spec.path.write_bytes(old)
        spec.path.chmod(0o600)
        runner = FakeLaunchctlRunner(
            active_target=target,
            enforce_manager_state=True,
        )

        digest = service_install(
            spec,
            runner,
            expected_sha256=hashlib.sha256(old).hexdigest(),
        )

        self.assertEqual(digest, hashlib.sha256(spec.content).hexdigest())
        self.assertEqual(spec.path.read_bytes(), spec.content)
        self.assertEqual(
            [call[0] for call in runner.calls[1:]],
            [
                ("/bin/launchctl", "bootout", target),
                ("/bin/launchctl", "bootstrap", domain, str(spec.path)),
                ("/bin/launchctl", "print", target),
            ],
        )

    def test_launchd_replacement_failure_removes_new_and_restores_old_definition(self) -> None:
        """replace 任一步失败都必须 bootout new target 并恢复旧 file/job。"""
        domain = f"gui/{self.uid}"
        for case, outcomes, expected_actions in (
            (
                "old-bootout",
                (0, 1),
                [("bootout",), ("bootstrap",), ("print",)],
            ),
            (
                "new-bootstrap",
                (0, 0, 1, 0, 0, 0),
                [("bootout",), ("bootstrap",), ("bootout",), ("bootstrap",), ("print",)],
            ),
            (
                "new-health",
                (0, 0, 0, 1, 0, 0, 0),
                [
                    ("bootout",),
                    ("bootstrap",),
                    ("print",),
                    ("bootout",),
                    ("bootstrap",),
                    ("print",),
                ],
            ),
        ):
            with self.subTest(case=case):
                home = self.home.parent / f"launchd-{case}"
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.LAUNCHD,
                )
                target = f"{domain}/{spec.label}"
                old = plistlib.dumps({"Label": spec.label, "Case": case})
                spec.path.parent.mkdir(parents=True, mode=0o700)
                spec.path.write_bytes(old)
                spec.path.chmod(0o600)
                runner = FakeLaunchctlRunner(outcomes)

                with self.assertRaisesRegex(InstallError, "service_install_failed"):
                    service_install(
                        spec,
                        runner,
                        expected_sha256=hashlib.sha256(old).hexdigest(),
                    )

                self.assertEqual(spec.path.read_bytes(), old)
                manager_calls = [call[0] for call in runner.calls if call[0][0] == "/bin/launchctl"]
                self.assertEqual(
                    [(call[1],) for call in manager_calls],
                    expected_actions,
                )
                self.assertTrue(
                    all(
                        call[2] == target
                        for call in manager_calls
                        if call[1] in {"bootout", "print"}
                    )
                )
                self.assertTrue(
                    all(
                        call[2:] == (domain, str(spec.path))
                        for call in manager_calls
                        if call[1] == "bootstrap"
                    )
                )

    def test_launchd_fresh_health_failure_boots_out_target_before_delete(self) -> None:
        """fresh bootstrap 已成功但 health 失败时必须先清理 active target。"""
        spec = self.launchd()
        target = f"gui/{self.uid}/{spec.label}"
        runner = FakeLaunchctlRunner((0, 0, 1, 0))

        with self.assertRaisesRegex(InstallError, "service_install_failed"):
            service_install(spec, runner)

        self.assertFalse(spec.path.exists())
        self.assertEqual(
            [call[0] for call in runner.calls if call[0][0] == "/bin/launchctl"],
            [
                ("/bin/launchctl", "bootstrap", f"gui/{self.uid}", str(spec.path)),
                ("/bin/launchctl", "print", target),
                ("/bin/launchctl", "bootout", target),
            ],
        )

    def test_launchd_rollback_removes_new_plist_only_after_confirmed_inactive(self) -> None:
        """rollback bootout 失败时必须用 print 确认 inactive，否则保留 recovery evidence。"""
        for inactive_returncode, expected_code, keep_new in (
            (3, "service_install_failed", False),
            (0, "rollback_conflict", True),
        ):
            with self.subTest(inactive_returncode=inactive_returncode):
                home = self.home.parent / f"fresh-inactive-{inactive_returncode}"
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.LAUNCHD,
                )
                target = f"gui/{self.uid}/{spec.label}"
                runner = FakeLaunchctlRunner((0, 0, 1, 1, inactive_returncode))

                with self.assertRaises(InstallError) as caught:
                    service_install(spec, runner)

                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(spec.path.exists(), keep_new)
                if keep_new:
                    self.assertEqual(spec.path.read_bytes(), spec.content)
                self.assertEqual(
                    [call[0] for call in runner.calls if call[0][0] == "/bin/launchctl"],
                    [
                        ("/bin/launchctl", "bootstrap", f"gui/{self.uid}", str(spec.path)),
                        ("/bin/launchctl", "print", target),
                        ("/bin/launchctl", "bootout", target),
                        ("/bin/launchctl", "print", target),
                    ],
                )

        spec = self.launchd()
        target = f"gui/{self.uid}/{spec.label}"
        old = plistlib.dumps({"Label": spec.label, "OldDefinition": True})
        spec.path.parent.mkdir(parents=True, mode=0o700)
        spec.path.write_bytes(old)
        spec.path.chmod(0o600)
        runner = FakeLaunchctlRunner((0, 0, 0, 1, 1, 0))

        with self.assertRaises(InstallError) as caught:
            service_install(
                spec,
                runner,
                expected_sha256=hashlib.sha256(old).hexdigest(),
            )

        self.assertEqual(caught.exception.code, "rollback_conflict")
        self.assertEqual(spec.path.read_bytes(), spec.content)
        residues = list(spec.path.parent.glob(f".{spec.path.name}.quarantine-*"))
        self.assertEqual(len(residues), 1)
        self.assertEqual((residues[0] / "entry").read_bytes(), old)
        self.assertEqual(
            [call[0][1] for call in runner.calls if call[0][0] == "/bin/launchctl"],
            ["bootout", "bootstrap", "print", "bootout", "print"],
        )

    def test_launchd_uncertain_old_bootout_restores_old_job_with_checked_health(self) -> None:
        """old bootout 即使 nonzero/exception 有副作用，也必须恢复 bootstrap 与 print。"""
        for failure in (1, RuntimeError("sentinel-old-bootout")):
            with self.subTest(failure_type=type(failure).__name__):
                home = self.home.parent / f"old-uncertain-{type(failure).__name__}"
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.LAUNCHD,
                )
                target = f"gui/{self.uid}/{spec.label}"
                old = plistlib.dumps({"Label": spec.label, "Failure": type(failure).__name__})
                spec.path.parent.mkdir(parents=True, mode=0o700)
                spec.path.write_bytes(old)
                spec.path.chmod(0o600)
                runner = FakeLaunchctlRunner(
                    (0, failure, 0, 0),
                    active_target=target,
                    enforce_manager_state=True,
                    side_effecting_calls=frozenset({1}),
                )

                with self.assertRaises(InstallError) as caught:
                    service_install(
                        spec,
                        runner,
                        expected_sha256=hashlib.sha256(old).hexdigest(),
                    )

                self.assertEqual(caught.exception.code, "service_install_failed")
                self.assertNotIn("sentinel", str(caught.exception))
                self.assertEqual(spec.path.read_bytes(), old)
                self.assertEqual(runner.active_target, target)
                self.assertEqual(
                    [call[0][1] for call in runner.calls if call[0][0] == "/bin/launchctl"],
                    ["bootout", "bootstrap", "print"],
                )

    def test_launchd_old_restore_manager_failures_require_recovery(self) -> None:
        """旧文件恢复后 bootstrap 或 print 任一步失败都不得声称 rollback 完成。"""
        for stage, outcomes in (
            ("bootstrap", (0, 0, 0, 1, 0, 1, 0)),
            ("print", (0, 0, 0, 1, 0, 0, 1)),
        ):
            with self.subTest(stage=stage):
                home = self.home.parent / f"restore-{stage}"
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.LAUNCHD,
                )
                old = plistlib.dumps({"Label": spec.label, "Stage": stage})
                spec.path.parent.mkdir(parents=True, mode=0o700)
                spec.path.write_bytes(old)
                spec.path.chmod(0o600)

                with self.assertRaises(InstallError) as caught:
                    service_install(
                        spec,
                        FakeLaunchctlRunner(outcomes),
                        expected_sha256=hashlib.sha256(old).hexdigest(),
                    )

                self.assertEqual(caught.exception.code, "rollback_conflict")
                self.assertEqual(spec.path.read_bytes(), old)

    def test_launchd_replacement_quarantine_failure_restores_old_job(self) -> None:
        """old target 已 bootout 后若 quarantine 未提交，也必须恢复旧定义与 health。"""
        spec = self.launchd()
        target = f"gui/{self.uid}/{spec.label}"
        old = plistlib.dumps({"Label": spec.label, "OldDefinition": True})
        spec.path.parent.mkdir(parents=True, mode=0o700)
        spec.path.write_bytes(old)
        spec.path.chmod(0o600)
        real_fsync = receipt_module._fsync_directory
        fsync_calls = 0

        def fail_first_quarantine_fsync(path: Path) -> None:
            """仅让 old public quarantine 的 durability barrier 失败。"""
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 1:
                raise OSError("sentinel-launchd-quarantine")
            real_fsync(path)

        runner = FakeLaunchctlRunner((0, 0, 0, 0, 0))
        with (
            mock.patch(
                "miniclaw.install.receipt._fsync_directory",
                side_effect=fail_first_quarantine_fsync,
            ),
            self.assertRaises(InstallError),
        ):
            service_install(
                spec,
                runner,
                expected_sha256=hashlib.sha256(old).hexdigest(),
            )

        self.assertEqual(spec.path.read_bytes(), old)
        self.assertEqual(
            [call[0] for call in runner.calls if call[0][0] == "/bin/launchctl"],
            [
                ("/bin/launchctl", "bootout", target),
                ("/bin/launchctl", "bootstrap", f"gui/{self.uid}", str(spec.path)),
                ("/bin/launchctl", "print", target),
            ],
        )

    def test_existing_service_requires_receipt_hash_and_idempotency_preserves_inode(self) -> None:
        """内容相同不等于受管；receipt hash 匹配后重复 install 才可幂等。"""
        spec = self.systemd()
        runner = FakeSystemctlRunner()
        digest = service_install(spec, runner)
        identity = (spec.path.stat().st_dev, spec.path.stat().st_ino)
        runner.calls.clear()
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            service_install(spec, runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(service_install(spec, runner, expected_sha256=digest), digest)
        self.assertEqual((spec.path.stat().st_dev, spec.path.stat().st_ino), identity)
        self.assertEqual([call[0] for call in runner.calls], [spec.status_argv])

    def test_foreign_hash_mode_symlink_and_parent_are_never_overwritten(self) -> None:
        """foreign file、宽权限或 symlink parent 均必须在 manager 调用前失败。"""
        for case in ("foreign", "mode", "symlink-parent"):
            with self.subTest(case=case):
                home = self.home.parent / case
                home.mkdir(mode=0o700)
                layout = InstallLayout.user(home, version="0.7.0")
                spec = render_service_spec(layout, ServicePlatform.SYSTEMD_USER)
                if case == "symlink-parent":
                    target = home / "config-target"
                    target.mkdir(mode=0o700)
                    (home / ".config").symlink_to(target, target_is_directory=True)
                else:
                    spec.path.parent.mkdir(parents=True, mode=0o700)
                    spec.path.write_bytes(b"foreign")
                    spec.path.chmod(0o644 if case == "mode" else 0o600)
                runner = FakeSystemctlRunner()
                with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
                    expected = "0" * 64 if case != "symlink-parent" else None
                    service_install(spec, runner, expected_sha256=expected)
                self.assertEqual(runner.calls, [])
                if case != "symlink-parent":
                    self.assertEqual(spec.path.read_bytes(), b"foreign")

    def test_validator_failure_never_publishes_or_leaks_temp(self) -> None:
        """lint 失败时 service path 和 O_EXCL temp 都不得残留。"""
        spec = self.launchd()
        runner = FakeLaunchctlRunner((1,))
        with self.assertRaisesRegex(InstallError, "service_install_failed"):
            service_install(spec, runner)
        self.assertFalse(spec.path.exists())
        self.assertEqual(list(spec.path.parent.glob(f".{spec.path.name}.*.tmp")), [])
        self.assertEqual(len(runner.calls), 1)

    def test_register_failure_restores_old_owned_file_or_removes_new(self) -> None:
        """manager 注册失败不得把新文件留在旧 receipt 或空 namespace 下。"""
        for original in (None, b"old-owned-unit\n"):
            with self.subTest(original=original):
                home = self.home.parent / ("new" if original is None else "old")
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.SYSTEMD_USER,
                )
                expected = None
                if original is not None:
                    spec.path.parent.mkdir(parents=True, mode=0o700)
                    spec.path.write_bytes(original)
                    spec.path.chmod(0o600)
                    expected = hashlib.sha256(original).hexdigest()
                runner = FakeSystemctlRunner((0, 1))
                with self.assertRaisesRegex(InstallError, "service_install_failed"):
                    service_install(spec, runner, expected_sha256=expected)
                if original is None:
                    self.assertFalse(spec.path.exists())
                else:
                    self.assertEqual(spec.path.read_bytes(), original)
                    self.assertEqual(stat.S_IMODE(spec.path.stat().st_mode), 0o600)

    def test_install_replacement_restores_old_on_precommit_fsync_failures(self) -> None:
        """旧定义必须保留到 new publish durable；此前任一 fsync 失败都可恢复。"""
        durable_fsync = receipt_module._fsync_directory
        for phase in ("quarantine", "publish"):
            with self.subTest(phase=phase):
                home = self.home.parent / f"install-{phase}-fsync"
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.SYSTEMD_USER,
                )
                old = f"old-{phase}\n".encode()
                spec.path.parent.mkdir(parents=True, mode=0o700)
                spec.path.write_bytes(old)
                spec.path.chmod(0o600)
                expected = hashlib.sha256(old).hexdigest()

                if phase == "quarantine":
                    calls = 0

                    def fail_first_quarantine_fsync(path: Path) -> None:
                        """仅让 public quarantine 的首个 durability barrier 失败。"""
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            raise OSError("sentinel-quarantine-fsync")
                        durable_fsync(path)

                    patched = mock.patch(
                        "miniclaw.install.receipt._fsync_directory",
                        side_effect=fail_first_quarantine_fsync,
                    )
                else:
                    patched = mock.patch(
                        "miniclaw.install.service._fsync_directory",
                        side_effect=OSError("sentinel-publish-fsync"),
                    )
                with patched, self.assertRaises(InstallError) as caught:
                    service_install(spec, FakeSystemctlRunner(), expected_sha256=expected)

                self.assertNotIn("sentinel", str(caught.exception))
                self.assertEqual(spec.path.read_bytes(), old)

    def test_install_success_ignores_private_gc_failure_after_new_is_durable(self) -> None:
        """new file 与 manager 已 durable/healthy 后，旧 private residue 不得触发回滚。"""
        spec = self.systemd()
        old = b"old-owned-service\n"
        spec.path.parent.mkdir(parents=True, mode=0o700)
        spec.path.write_bytes(old)
        spec.path.chmod(0o600)

        with mock.patch(
            "miniclaw.install.service._service_private_gc_hook",
            create=True,
            side_effect=OSError("sentinel-private-gc"),
        ):
            digest = service_install(
                spec,
                FakeSystemctlRunner(),
                expected_sha256=hashlib.sha256(old).hexdigest(),
            )

        self.assertEqual(digest, hashlib.sha256(spec.content).hexdigest())
        self.assertEqual(spec.path.read_bytes(), spec.content)
        self.assertTrue(list(spec.path.parent.glob(f".{spec.path.name}.quarantine-*")))

    def test_uninstall_commit_never_restores_after_reload_or_private_gc_failure(self) -> None:
        """public delete durability 后的 manager/GC 失败不得恢复不存在的 token。"""
        for phase in ("manager-reload", "private-gc"):
            with self.subTest(phase=phase):
                home = self.home.parent / f"uninstall-{phase}"
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.SYSTEMD_USER,
                )
                digest = service_install(spec, FakeSystemctlRunner())
                runner = FakeSystemctlRunner((0, 1) if phase == "manager-reload" else ())
                patcher = (
                    mock.patch(
                        "miniclaw.install.service._service_private_gc_hook",
                        create=True,
                        side_effect=OSError("sentinel-private-gc"),
                    )
                    if phase == "private-gc"
                    else mock.patch(
                        "miniclaw.install.service._service_private_gc_hook",
                        create=True,
                    )
                )

                with patcher:
                    if phase == "manager-reload":
                        with self.assertRaisesRegex(InstallError, "service_install_failed"):
                            service_uninstall(spec, runner, expected_sha256=digest)
                    else:
                        service_uninstall(spec, runner, expected_sha256=digest)

                self.assertFalse(spec.path.exists())
                if phase == "private-gc":
                    self.assertTrue(list(spec.path.parent.glob(f".{spec.path.name}.quarantine-*")))
                retry = FakeSystemctlRunner()
                service_uninstall(spec, retry, expected_sha256=digest)
                self.assertEqual(
                    [call[0] for call in retry.calls],
                    [("/usr/bin/systemctl", "--user", "daemon-reload")],
                )

    def test_systemd_absent_uninstall_retries_only_daemon_reload(self) -> None:
        """committed delete 后只重试 daemon-reload，成功收敛、失败继续稳定报错。"""
        for retry_returncode in (0, 1):
            with self.subTest(retry_returncode=retry_returncode):
                home = self.home.parent / f"reload-retry-{retry_returncode}"
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.SYSTEMD_USER,
                )
                digest = service_install(spec, FakeSystemctlRunner())
                with self.assertRaisesRegex(InstallError, "service_install_failed"):
                    service_uninstall(
                        spec,
                        FakeSystemctlRunner((0, 1)),
                        expected_sha256=digest,
                    )
                self.assertFalse(spec.path.exists())

                retry = FakeSystemctlRunner((retry_returncode,))
                if retry_returncode == 0:
                    service_uninstall(spec, retry, expected_sha256=digest)
                else:
                    with self.assertRaises(InstallError) as caught:
                        service_uninstall(spec, retry, expected_sha256=digest)
                    self.assertEqual(caught.exception.code, "service_install_failed")
                self.assertEqual(
                    [call[0] for call in retry.calls],
                    [("/usr/bin/systemctl", "--user", "daemon-reload")],
                )

    def test_launchd_absent_uninstall_remains_noop(self) -> None:
        """LaunchAgent 没有 post-delete reload，absent owned path 不得调用 launchctl。"""
        spec = self.launchd()
        runner = FakeLaunchctlRunner()

        service_uninstall(spec, runner, expected_sha256="0" * 64)

        self.assertEqual(runner.calls, [])

    def test_launchd_uninstall_confirms_inactive_after_side_effecting_bootout_failure(
        self,
    ) -> None:
        """bootout 已使 job inactive 时，nonzero/exception 不得阻塞受管删除。"""
        for failure in (1, RuntimeError("sentinel-uninstall-bootout")):
            with self.subTest(failure_type=type(failure).__name__):
                home = self.home.parent / f"uninstall-side-effect-{type(failure).__name__}"
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.LAUNCHD,
                )
                digest = service_install(spec, FakeLaunchctlRunner())
                target = f"gui/{self.uid}/{spec.label}"
                runner = FakeLaunchctlRunner(
                    (failure,),
                    active_target=target,
                    enforce_manager_state=True,
                    side_effecting_calls=frozenset({0}),
                )

                service_uninstall(spec, runner, expected_sha256=digest)

                self.assertFalse(spec.path.exists())
                self.assertIsNone(runner.active_target)
                self.assertEqual(
                    [call[0] for call in runner.calls],
                    [
                        ("/bin/launchctl", "bootout", target),
                        ("/bin/launchctl", "print", target),
                    ],
                )
                retry = FakeLaunchctlRunner()
                service_uninstall(spec, retry, expected_sha256=digest)
                self.assertEqual(retry.calls, [])

    def test_launchd_uninstall_preserves_public_plist_when_inactive_is_unconfirmed(
        self,
    ) -> None:
        """bootout 后 target 仍 active 或 print 未知时必须保留 public recovery evidence。"""
        for status_outcome in (0, RuntimeError("sentinel-uninstall-status")):
            with self.subTest(status_type=type(status_outcome).__name__):
                home = self.home.parent / f"uninstall-unknown-{type(status_outcome).__name__}"
                home.mkdir(mode=0o700)
                spec = render_service_spec(
                    InstallLayout.user(home, version="0.7.0"),
                    ServicePlatform.LAUNCHD,
                )
                digest = service_install(spec, FakeLaunchctlRunner())
                target = f"gui/{self.uid}/{spec.label}"
                runner = FakeLaunchctlRunner(
                    (1, status_outcome),
                    active_target=target,
                    enforce_manager_state=True,
                )

                with self.assertRaises(InstallError) as caught:
                    service_uninstall(spec, runner, expected_sha256=digest)

                self.assertEqual(caught.exception.code, "service_install_failed")
                self.assertNotIn("sentinel", str(caught.exception))
                self.assertEqual(spec.path.read_bytes(), spec.content)
                self.assertEqual(runner.active_target, target)
                self.assertEqual(
                    [call[0] for call in runner.calls],
                    [
                        ("/bin/launchctl", "bootout", target),
                        ("/bin/launchctl", "print", target),
                    ],
                )

    def test_launchd_uninstall_precommit_restore_requires_checked_manager_health(
        self,
    ) -> None:
        """public plist 恢复后 bootstrap 失败必须显式报 recovery，并允许后续收敛。"""
        spec = self.launchd()
        digest = service_install(spec, FakeLaunchctlRunner())
        target = f"gui/{self.uid}/{spec.label}"
        real_fsync = receipt_module._fsync_directory
        fsync_calls = 0

        def fail_first_quarantine_fsync(path: Path) -> None:
            """仅让 launchd uninstall quarantine 的首个 parent fsync 失败。"""
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 1:
                raise OSError("sentinel-launchd-uninstall-fsync")
            real_fsync(path)

        runner = FakeLaunchctlRunner(
            (0, 1),
            active_target=target,
            enforce_manager_state=True,
        )
        with (
            mock.patch(
                "miniclaw.install.receipt._fsync_directory",
                side_effect=fail_first_quarantine_fsync,
            ),
            self.assertRaises(InstallError) as caught,
        ):
            service_uninstall(spec, runner, expected_sha256=digest)

        self.assertEqual(caught.exception.code, "rollback_conflict")
        self.assertNotIn("sentinel", str(caught.exception))
        self.assertEqual(spec.path.read_bytes(), spec.content)
        self.assertIsNone(runner.active_target)
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/bin/launchctl", "bootout", target),
                ("/bin/launchctl", "bootstrap", f"gui/{self.uid}", str(spec.path)),
                ("/bin/launchctl", "print", target),
            ],
        )

        retry = FakeLaunchctlRunner(active_target=None, enforce_manager_state=True)
        service_uninstall(spec, retry, expected_sha256=digest)
        self.assertFalse(spec.path.exists())
        self.assertEqual(
            [call[0] for call in retry.calls],
            [
                ("/bin/launchctl", "bootout", target),
                ("/bin/launchctl", "print", target),
            ],
        )

    def test_uninstall_precommit_quarantine_fsync_failure_restores_file_and_job(self) -> None:
        """public delete durability 前失败时必须保留 owned file 并恢复 manager。"""
        spec = self.systemd()
        digest = service_install(spec, FakeSystemctlRunner())
        real_fsync = receipt_module._fsync_directory
        calls = 0

        def fail_first_quarantine_fsync(path: Path) -> None:
            """仅让 uninstall quarantine 的首个 parent fsync 失败。"""
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("sentinel-uninstall-fsync")
            real_fsync(path)

        runner = FakeSystemctlRunner()
        with (
            mock.patch(
                "miniclaw.install.receipt._fsync_directory",
                side_effect=fail_first_quarantine_fsync,
            ),
            self.assertRaises(InstallError) as caught,
        ):
            service_uninstall(spec, runner, expected_sha256=digest)

        self.assertNotIn("sentinel", str(caught.exception))
        self.assertEqual(spec.path.read_bytes(), spec.content)
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/bin/systemctl", "--user", "disable", "--now", spec.label),
                ("/usr/bin/systemctl", "--user", "daemon-reload"),
                ("/usr/bin/systemctl", "--user", "enable", "--now", spec.label),
            ],
        )

    def test_uninstall_requires_owned_hash_and_manager_failure_preserves_file(self) -> None:
        """uninstall 不得在 hash 漂移或 manager 未停止时删除 service file。"""
        spec = self.systemd()
        digest = service_install(spec, FakeSystemctlRunner())
        runner = FakeSystemctlRunner()
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            service_uninstall(spec, runner, expected_sha256="0" * 64)
        self.assertEqual(runner.calls, [])
        self.assertTrue(spec.path.exists())
        with self.assertRaisesRegex(InstallError, "service_install_failed"):
            service_uninstall(spec, FakeSystemctlRunner((1,)), expected_sha256=digest)
        self.assertTrue(spec.path.exists())

    def test_runner_failures_and_invalid_results_are_redacted(self) -> None:
        """runner exception/output 不得进入稳定安装异常。"""
        spec = self.systemd()
        for outcome in (RuntimeError("sentinel-token-value"), object()):
            with self.subTest(outcome=outcome):
                with self.assertRaises(InstallError) as caught:
                    service_install(spec, FakeSystemctlRunner((outcome,)))
                self.assertEqual(caught.exception.code, "service_install_failed")
                self.assertNotIn("sentinel", str(caught.exception))

    def test_status_nonzero_is_inactive_but_logs_restart_fail_closed(self) -> None:
        """inactive 是 status 结果；logs/restart 非零仍是 lifecycle 失败。"""
        spec = self.systemd()
        self.assertFalse(service_status(spec, FakeSystemctlRunner((3,))))
        for action in (service_logs, service_restart):
            with (
                self.subTest(action=action.__name__),
                self.assertRaisesRegex(InstallError, "service_install_failed"),
            ):
                action(spec, FakeSystemctlRunner((1,)))

    def test_service_functions_reject_forged_spec_or_runner_before_side_effect(self) -> None:
        """所有 lifecycle 入口都必须重新校验 direct-constructor spec 与 runner。"""
        spec = self.systemd()
        for action in (service_status, service_logs, service_restart):
            with (
                self.subTest(action=action.__name__),
                self.assertRaisesRegex(InstallError, "service_install_failed"),
            ):
                action(spec, object())
        with self.assertRaisesRegex(InstallError, "service_install_failed"):
            service_install(object(), FakeSystemctlRunner())

        for canonical in (self.systemd(), self.launchd()):
            copied = object.__new__(ServiceSpec)
            for field in canonical.__dataclass_fields__:
                object.__setattr__(copied, field, getattr(canonical, field))
            runner = (
                FakeSystemctlRunner()
                if canonical.platform is ServicePlatform.SYSTEMD_USER
                else FakeLaunchctlRunner()
            )
            for action in ("install", "status", "logs", "restart", "uninstall"):
                with (
                    self.subTest(platform=canonical.platform, action=action),
                    self.assertRaisesRegex(InstallError, "service_install_failed"),
                ):
                    if action == "install":
                        service_install(copied, runner)
                    elif action == "status":
                        service_status(copied, runner)
                    elif action == "logs":
                        service_logs(copied, runner)
                    elif action == "restart":
                        service_restart(copied, runner)
                    else:
                        service_uninstall(copied, runner, expected_sha256="0" * 64)


if __name__ == "__main__":
    unittest.main()
