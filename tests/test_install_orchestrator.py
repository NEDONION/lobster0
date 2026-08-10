"""验证安装编排状态机、bootstrap 信任边界与 CLI 输出。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pty
import pwd
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.request import Request

from lobster0.config import ConfigError, load_config
from lobster0.install import __main__ as installer_cli
from lobster0.install import orchestrator as install_orchestrator
from lobster0.install.__main__ import _public_argv, main
from lobster0.install.layout import InstallLayout, InstallLock
from lobster0.install.models import (
    InstallError,
    InstallEvent,
    InstallPlan,
    InstallRequest,
    PlatformKey,
    ReleaseManifest,
)
from lobster0.install.orchestrator import (
    BootstrapInputs,
    Installer,
    InstallResult,
    _checked_owner_command,
    _execute_dependency_actions,
    _import_secrets,
    _install_service_as_owner,
    _remove_created_install_metadata,
    _service_inputs_complete,
    _service_inputs_complete_as_owner,
    _SystemOperations,
    _validate_and_import_config,
    emit_event,
    verify_bootstrap_inputs,
)
from lobster0.install.platforms import DependencyPlan, DetectedPlatform, PrivilegeAction
from lobster0.install.receipt import InstallReceipt, managed_file_sha256
from lobster0.install.runtime import CommandResult
from lobster0.install.service import ServicePlatform
from lobster0.paths import build_state_paths


class _Manifest:
    """提供状态机测试需要的最小 manifest 视图。"""

    version = "0.7.0"


class _TargetManifest:
    """提供 target handoff 测试的不同 bootstrap 版本。"""

    version = "0.6.0"


class _TargetPlan:
    """提供 production target selection 需要的最小 plan 视图。"""

    manifest = _TargetManifest()
    platform = PlatformKey("linux", "x86_64")


class _Response(io.BytesIO):
    """提供 verified transport 所需的离线 HTTP response。"""

    def __init__(self, body: bytes) -> None:
        """保存 exact body、200 status 与 Content-Length。"""
        super().__init__(body)
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}

    def getcode(self) -> int:
        """返回 HTTP status。"""
        return self.status


class _Opener:
    """按顺序返回 manifest 与 installer body。"""

    def __init__(self, *bodies: bytes) -> None:
        """把离线 body 转换为独立 response。"""
        self.bodies = list(bodies)
        self.urls: list[str] = []

    def open(self, request: Request, timeout: float | None = None) -> _Response:
        """记录 allowlisted URL 并返回下一响应。"""
        del timeout
        self.urls.append(request.full_url)
        if not self.bodies:
            raise AssertionError("unexpected HTTP request")
        return _Response(self.bodies.pop(0))


class _Plan:
    """提供状态机测试需要的最小安全计划。"""

    manifest = _Manifest()
    platform = PlatformKey("linux", "x86_64")
    install_service = True


class _Lock:
    """记录 context manager 是否完整释放。"""

    def __init__(self, calls: list[str]) -> None:
        """保存共享调用记录。"""
        self.calls = calls

    def __enter__(self) -> _Lock:
        """记录 lock 获取。"""
        self.calls.append("lock.enter")
        return self

    def __exit__(self, *_args: object) -> None:
        """记录 lock 释放。"""
        self.calls.append("lock.exit")


class _Operations:
    """离线记录编排顺序并支持按阶段注入稳定失败。"""

    def __init__(self, *, fail: str | None = None) -> None:
        """设置可选失败阶段并初始化可观察状态。"""
        self.fail = fail
        self.calls: list[str] = []
        self.handoff: tuple[str, tuple[str, ...], dict[str, str]] | None = None
        self.service_ready = True
        self.current = "old"
        self.service = "old"
        self.held_lock: _Lock | None = None
        self.recovery_manifest: object | None = None

    def _call(self, name: str) -> None:
        """记录一次调用并在命中注入点时失败。"""
        self.calls.append(name)
        if self.fail == name:
            code = {
                "preflight": "unsupported_platform",
                "sandbox": "artifact_download_failed",
                "system_plan": "system_dependency_missing",
                "download": "artifact_download_failed",
                "hash": "artifact_hash_mismatch",
                "extract": "manifest_invalid",
                "venv": "runtime_install_failed",
                "wheel": "runtime_install_failed",
                "tui": "tui_smoke_failed",
                "setup": "doctor_blocked",
                "doctor": "doctor_blocked",
                "activate": "activation_failed",
                "commit": "installer_error",
                "service": "service_install_failed",
                "service_receipt": "installer_error",
                "retain": "runtime_install_failed",
            }[name]
            raise InstallError(code, "manifest")

    def preflight(self, _request: InstallRequest, _bootstrap: BootstrapInputs) -> _Plan:
        """返回固定安全计划。"""
        self._call("preflight")
        return _Plan()

    def select_target(
        self,
        _request: InstallRequest,
        _plan: _Plan,
        _bootstrap: BootstrapInputs,
        _public_argv: tuple[str, ...],
        _environ: dict[str, str],
    ) -> tuple[str, tuple[str, ...], dict[str, str]] | None:
        """返回可选 installer handoff。"""
        self.calls.append("select_target")
        return self.handoff

    def prepare_dependencies(
        self,
        _plan: _Plan,
        _bootstrap: BootstrapInputs,
    ) -> _Plan:
        """在 persistent layout 前记录 sandbox 下载和 live dependency plan。"""
        self._call("sandbox")
        self._call("system_plan")
        return _plan

    def layout(self, _plan: _Plan) -> object:
        """返回不触碰真实文件系统的 layout token。"""
        self.calls.append("layout")
        return object()

    def lock(self, _layout: object) -> _Lock:
        """返回记录型 owner lock。"""
        self.held_lock = _Lock(self.calls)
        return self.held_lock

    def recover(
        self,
        _layout: object,
        lock: _Lock,
        manifest: object,
    ) -> None:
        """记录 lock-bound Runtime 与 downloads 恢复边界。"""
        if lock is not self.held_lock:
            raise AssertionError("recovery did not receive the acquired lock")
        self.recovery_manifest = manifest
        self.calls.append("recover")

    def previous_current(self, _layout: object) -> str:
        """返回事务开始前的 current。"""
        self.calls.append("previous_current")
        return self.current

    def download(self, _plan: _Plan, _layout: object) -> object:
        """记录下载及其内部 hash/extract 边界。"""
        self._call("download")
        self._call("hash")
        self._call("extract")
        return object()

    def build(self, _plan: _Plan, _layout: object, _downloaded: object) -> object:
        """记录 venv/wheel/TUI 构建边界。"""
        self._call("venv")
        self._call("wheel")
        self._call("tui")
        return object()

    def setup(self, _plan: _Plan, _layout: object, _built: object) -> None:
        """记录 staged setup/init。"""
        self._call("setup")

    def doctor(self, _plan: _Plan, _layout: object, _built: object) -> bool:
        """返回 service 配置是否完整。"""
        self._call("doctor")
        return self.service_ready

    def activate(self, _plan: _Plan, _layout: object, _built: object) -> None:
        """切换 fake current。"""
        self._call("activate")
        self.current = "new"

    def commit(self, _plan: _Plan, _layout: object, _built: object) -> None:
        """记录 launcher/receipt commit。"""
        self._call("commit")

    def install_service(self, _plan: _Plan, _layout: object) -> None:
        """启动 fake service。"""
        self._call("service")
        self.service = "new"
        self._call("service_receipt")

    def retain(self, _layout: object) -> None:
        """记录 Runtime retention。"""
        self._call("retain")

    def rollback(self, _layout: object, previous: str, _stage: str) -> None:
        """恢复 fake current/service。"""
        self.calls.append("rollback")
        self.current = previous
        self.service = "old"

    def cleanup(self, _layout: object, lock: _Lock) -> None:
        """记录 actual fake lock 下的 staging cleanup。"""
        if lock is not self.held_lock:
            raise AssertionError("cleanup did not receive the acquired lock")
        self.calls.append("cleanup")


class InstallerStateMachineTests(unittest.TestCase):
    """覆盖原子顺序、失败矩阵、dry-run 与 installer handoff。"""

    def setUp(self) -> None:
        """创建只供 fake operations 使用的 canonical 请求。"""
        self.request = InstallRequest(
            action="install",
            version=None,
            channel="stable",
            prefix=None,
            state_home=Path("/tmp/lobster0-state"),
            system_prefix=False,
            onboard=True,
            config_file=None,
            secrets_file=None,
            service=True,
            allow_system_packages=False,
            dry_run=False,
            json_output=False,
            verbose=False,
            purge_data=False,
            confirm_data_loss=False,
        )
        self.bootstrap = BootstrapInputs(
            Path("/tmp/bootstrap/release-manifest.json"),
            "1" * 64,
            Path("/tmp/bootstrap/uv/uv"),
            Path("/tmp/bootstrap/python"),
            Path("/tmp/bootstrap/python/bin/python3.12"),
            (),
        )

    def test_install_runs_preflight_stage_smoke_activate_service_in_order(self) -> None:
        """成功安装必须按固定顺序产出状态事件。"""
        operations = _Operations()
        result = Installer(self.bootstrap, operations=operations).run(self.request)
        self.assertEqual(
            [event.name for event in result.events],
            [
                "install.preflight",
                "install.download",
                "install.staged",
                "install.smoke",
                "install.activated",
                "service.installed",
                "install.complete",
            ],
        )
        self.assertEqual(
            result,
            InstallResult(
                "install",
                "0.7.0",
                PlatformKey("linux", "x86_64"),
                True,
                result.events,
            ),
        )
        self.assertLess(operations.calls.index("doctor"), operations.calls.index("activate"))
        self.assertLess(operations.calls.index("system_plan"), operations.calls.index("layout"))
        self.assertLess(operations.calls.index("system_plan"), operations.calls.index("lock.enter"))
        self.assertEqual(result.events[0].detail, "manifest")
        self.assertEqual(operations.calls[-1], "lock.exit")

    def test_dry_run_has_zero_side_effect(self) -> None:
        """dry-run 只允许只读 preflight，不获取 lock 或选择下载目标。"""
        operations = _Operations()
        result = Installer(self.bootstrap, operations=operations).run(
            replace(self.request, dry_run=True, json_output=True)
        )
        self.assertEqual(operations.calls, ["preflight"])
        self.assertFalse(result.changed)
        self.assertEqual(
            [event.name for event in result.events],
            ["install.preflight", "install.dependencies.deferred"],
        )
        self.assertEqual(result.events[-1].detail, "system_argvs")

    def test_dry_run_reports_explicit_target_without_downloading_manifest(self) -> None:
        """显式 fixed target 可直接输出，但仍不得调用 target discovery。"""
        operations = _Operations()
        result = Installer(self.bootstrap, operations=operations).run(
            replace(self.request, version="0.8.0", dry_run=True, json_output=True)
        )
        self.assertEqual(result.version, "0.8.0")
        self.assertEqual(result.platform, PlatformKey("linux", "x86_64"))
        self.assertEqual(operations.calls, ["preflight"])

    def test_unsupported_actions_and_update_dry_run_fail_without_operations(self) -> None:
        """Task11 不得让 uninstall 或假 update dry-run 进入任一副作用边界。"""
        for action, dry_run, hop in (
            ("uninstall", False, None),
            ("uninstall", True, "1"),
            ("update", True, None),
            ("update", True, "1"),
        ):
            with self.subTest(action=action, dry_run=dry_run, hop=hop):
                operations = _Operations()
                environment = {} if hop is None else {"LOBSTER0_INSTALLER_HOPS": hop}
                with self.assertRaises(InstallError) as raised:
                    Installer(
                        self.bootstrap,
                        operations=operations,
                        environ=environment,
                    ).run(replace(self.request, action=action, dry_run=dry_run))
                self.assertEqual((raised.exception.code, raised.exception.detail), (
                    "request_invalid",
                    "action",
                ))
                self.assertEqual(operations.calls, [])

    def test_update_target_hop_fails_after_preflight_before_pipeline(self) -> None:
        """已跳转的 update target 只验证 bootstrap，不再 discovery 或安装。"""
        operations = _Operations()
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            Installer(
                self.bootstrap,
                operations=operations,
                environ={"LOBSTER0_INSTALLER_HOPS": "1"},
            ).run(replace(self.request, action="update"))
        self.assertEqual(operations.calls, ["preflight"])

    def test_update_without_verified_handoff_never_runs_install_pipeline(self) -> None:
        """第一跳 update 缺少 target handoff 时必须 fail closed。"""
        operations = _Operations()
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            Installer(self.bootstrap, operations=operations).run(
                replace(self.request, action="update")
            )
        self.assertEqual(operations.calls, ["preflight", "select_target"])

    def test_update_first_hop_execs_verified_target_once(self) -> None:
        """第一跳 update 只能执行一次 target installer，不能进入 Task11 pipeline。"""
        operations = _Operations()
        handoff = (
            "/managed/python",
            ("/managed/python", "/bootstrap/target.pyz", "update"),
            {"PATH": "/usr/bin:/bin", "LOBSTER0_INSTALLER_HOPS": "1"},
        )
        operations.handoff = handoff
        calls: list[tuple[str, tuple[str, ...], dict[str, str]]] = []
        with self.assertRaisesRegex(InstallError, "installer_error"):
            Installer(
                self.bootstrap,
                operations=operations,
                execve=lambda path, argv, env: calls.append((path, argv, env)),
            ).run(replace(self.request, action="update"))
        self.assertEqual(calls, [handoff])
        self.assertEqual(operations.calls, ["preflight", "select_target"])

    def test_dependency_preparation_failure_never_creates_persistent_layout(self) -> None:
        """sandbox/live probe 失败必须发生在 layout、lock 与 current 读取之前。"""
        for stage, code in (
            ("sandbox", "artifact_download_failed"),
            ("system_plan", "system_dependency_missing"),
        ):
            with self.subTest(stage=stage):
                operations = _Operations(fail=stage)
                with self.assertRaises(InstallError) as raised:
                    Installer(self.bootstrap, operations=operations).run(self.request)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("layout", operations.calls)
                self.assertNotIn("lock.enter", operations.calls)

    def test_lock_bound_recovery_precedes_current_download_and_build(self) -> None:
        """同版本重试恢复必须使用当前 with 返回的 actual lock。"""
        operations = _Operations()
        Installer(self.bootstrap, operations=operations).run(self.request)
        self.assertIs(operations.recovery_manifest, _Plan.manifest)
        self.assertLess(
            operations.calls.index("recover"),
            operations.calls.index("previous_current"),
        )
        self.assertLess(operations.calls.index("recover"), operations.calls.index("download"))
        self.assertLess(operations.calls.index("recover"), operations.calls.index("venv"))

    def test_zero_channel_state_skips_gateway_service(self) -> None:
        """Doctor 判定无完整 Channel 时仍安装 TUI，但不创建 Gateway service。"""
        operations = _Operations()
        operations.service_ready = False
        result = Installer(self.bootstrap, operations=operations).run(
            replace(self.request, service=None)
        )
        self.assertNotIn("service", operations.calls)
        self.assertIn("service.skipped", [event.name for event in result.events])

    def test_explicit_service_fails_before_activation_when_state_is_incomplete(self) -> None:
        """显式 service 请求遇到不完整配置必须在 activation 前失败。"""
        operations = _Operations()
        operations.service_ready = False
        with self.assertRaisesRegex(InstallError, "doctor_blocked"):
            Installer(self.bootstrap, operations=operations).run(self.request)
        self.assertNotIn("activate", operations.calls)
        self.assertEqual(operations.current, "old")

    def test_failure_matrix_restores_old_current_and_releases_lock(self) -> None:
        """每个事务失败点都清理 staging、恢复 current/service 并释放 lock。"""
        expected = {
            "download": "artifact_download_failed",
            "hash": "artifact_hash_mismatch",
            "extract": "manifest_invalid",
            "venv": "runtime_install_failed",
            "wheel": "runtime_install_failed",
            "tui": "tui_smoke_failed",
            "setup": "doctor_blocked",
            "doctor": "doctor_blocked",
            "activate": "activation_failed",
            "commit": "installer_error",
            "service": "service_install_failed",
            "service_receipt": "installer_error",
            "retain": "runtime_install_failed",
        }
        for stage, code in expected.items():
            with self.subTest(stage=stage):
                operations = _Operations(fail=stage)
                with self.assertRaises(InstallError) as raised:
                    Installer(self.bootstrap, operations=operations).run(self.request)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(operations.current, "old")
                self.assertEqual(operations.service, "old")
                self.assertIn("cleanup", operations.calls)
                self.assertEqual(operations.calls[-1], "lock.exit")

    def test_preflight_failure_never_acquires_lock(self) -> None:
        """平台或 manifest preflight 失败不得产生持久副作用。"""
        operations = _Operations(fail="preflight")
        with self.assertRaisesRegex(InstallError, "unsupported_platform"):
            Installer(self.bootstrap, operations=operations).run(self.request)
        self.assertEqual(operations.calls, ["preflight"])

    def test_target_installer_execve_happens_once_before_lock(self) -> None:
        """版本跳转只执行一次 exact execve，且发生于任一持久写之前。"""
        operations = _Operations()
        python = "/private/bootstrap/python/bin/python3.12"
        operations.handoff = (
            python,
            (python, "/private/bootstrap/target.pyz", "install"),
            {"PATH": "/usr/bin:/bin", "LOBSTER0_INSTALLER_HOPS": "1"},
        )
        calls: list[tuple[str, tuple[str, ...], dict[str, str]]] = []

        def execve(path: str, argv: tuple[str, ...], env: dict[str, str]) -> None:
            calls.append((path, argv, env))

        with self.assertRaisesRegex(InstallError, "installer_error"):
            Installer(self.bootstrap, operations=operations, execve=execve).run(self.request)
        self.assertEqual(calls, [operations.handoff])
        self.assertEqual(operations.calls, ["preflight", "select_target"])

    def test_second_installer_hop_fails_closed(self) -> None:
        """已有 hop 标记时不得再解析、下载或执行目标 installer。"""
        operations = _Operations()
        operations.handoff = ("/python", ("/python", "/target.pyz"), {})
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            Installer(
                self.bootstrap,
                operations=operations,
                environ={"LOBSTER0_INSTALLER_HOPS": "1"},
            ).run(self.request)
        self.assertEqual(operations.calls, ["preflight", "select_target"])

    def test_target_installer_does_not_rediscover_update_manifest(self) -> None:
        """目标 installer 必须直接消费 verified manifest，不能联网发现第三跳。"""
        operations = _SystemOperations(self.bootstrap)
        selected = operations.select_target(
            replace(self.request, action="update"),
            _Plan(),  # type: ignore[arg-type]
            self.bootstrap,
            (),
            {"LOBSTER0_INSTALLER_HOPS": "1"},
        )
        self.assertIsNone(selected)

    def test_rollback_failure_still_cleans_and_releases_lock(self) -> None:
        """恢复 current 失败也必须执行 staging cleanup 与 lock release。"""
        operations = _Operations(fail="service")

        def fail_rollback(_layout: object, _previous: str, _stage: str) -> None:
            operations.calls.append("rollback")
            raise InstallError("rollback_conflict", "manifest")

        operations.rollback = fail_rollback  # type: ignore[method-assign]
        with self.assertRaisesRegex(InstallError, "rollback_conflict"):
            Installer(self.bootstrap, operations=operations).run(self.request)
        self.assertIn("cleanup", operations.calls)
        self.assertEqual(operations.calls[-1], "lock.exit")


class TargetHandoffTests(unittest.TestCase):
    """覆盖真实 manifest/installer 验证与 exact execve 参数。"""

    def test_target_handoff_uses_verified_artifacts_and_validated_sudo_identity(self) -> None:
        """handoff 不得信任调用方伪造的 SUDO identity 或 PATH。"""
        installer = b"verified target installer"
        document = json.loads(Path("tests/install/manifest_v1.json").read_text(encoding="utf-8"))
        document["artifacts"].append(
            {
                "kind": "installer",
                "filename": "lobster0-installer.pyz",
                "url": (
                    "https://github.com/NEDONION/lobster0/releases/download/"
                    "v0.7.0/lobster0-installer.pyz"
                ),
                "sha256": hashlib.sha256(installer).hexdigest(),
                "size": len(installer),
                "media_type": "application/zip",
                "platform": {"os": "any", "arch": "any"},
                "component_version": "0.7.0",
                "source_repository": "https://github.com/NEDONION/lobster0",
                "license_ref": "MIT",
                "upstream_sha256": None,
            }
        )
        manifest = json.dumps(document, separators=(",", ":")).encode()
        opener = _Opener(manifest, installer)
        request = InstallRequest(
            action="install",
            version="0.7.0",
            channel="stable",
            prefix=None,
            state_home=Path("/home/alice/.lobster0"),
            system_prefix=True,
            onboard=True,
            config_file=None,
            secrets_file=None,
            service=True,
            allow_system_packages=False,
            dry_run=False,
            json_output=False,
            verbose=False,
            purge_data=False,
            confirm_data_loss=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            manifest_path = root / "release-manifest.json"
            manifest_path.write_bytes(b"bootstrap")
            python_root = root / "python"
            bootstrap = BootstrapInputs(
                manifest_path,
                hashlib.sha256(b"bootstrap").hexdigest(),
                root / "uv" / "uv",
                python_root,
                python_root / "bin" / "python3.12",
                ("--system-prefix", "--version", "0.7.0"),
            )
            account = pwd.struct_passwd(
                ("alice", "x", 1001, 1001, "Alice", "/home/alice", "/bin/sh")
            )
            operations = _SystemOperations(bootstrap, opener=opener)
            with mock.patch(
                "lobster0.install.orchestrator._invoking_account",
                return_value=account,
            ):
                selected = operations.select_target(
                    request,
                    _TargetPlan(),  # type: ignore[arg-type]
                    bootstrap,
                    bootstrap.public_argv,
                    {
                        "PATH": "/attacker/bin",
                        "SUDO_USER": "mallory",
                        "SUDO_UID": "9999",
                    },
                )
            self.assertIsNotNone(selected)
            executable, argv, environment = selected  # type: ignore[misc]
            self.assertEqual(executable, str(bootstrap.managed_python_executable))
            self.assertEqual(argv[0], executable)
            self.assertEqual(argv[2:6], ("install", "--system-prefix", "--version", "0.7.0"))
            self.assertIn("--managed-uv", argv)
            self.assertIn(str(bootstrap.managed_uv), argv)
            self.assertEqual(
                environment,
                {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "LOBSTER0_INSTALLER_HOPS": "1",
                    "SUDO_USER": "alice",
                    "SUDO_UID": "1001",
                },
            )
            target_manifest = root / "release-manifest-0.7.0.json"
            target_installer = root / "target-0.7.0" / "lobster0-installer.pyz"
            self.assertEqual(target_manifest.read_bytes(), manifest)
            self.assertEqual(target_installer.read_bytes(), installer)


class DependencyActionTests(unittest.TestCase):
    """覆盖 Task5 capability 的确认、exact 执行与 follow-up 收敛。"""

    def setUp(self) -> None:
        """创建严格 request/platform 与两个 allowlisted linger actions。"""
        self.request = InstallRequest(
            action="install",
            version="0.7.0",
            channel="stable",
            prefix=None,
            state_home=Path("/home/alice/.lobster0"),
            system_prefix=False,
            onboard=True,
            config_file=None,
            secrets_file=None,
            service=True,
            allow_system_packages=False,
            dry_run=False,
            json_output=False,
            verbose=False,
            purge_data=False,
            confirm_data_loss=False,
        )
        self.platform = DetectedPlatform(
            os="linux",
            distro_id="ubuntu",
            distro_version="24.04",
            arch="x86_64",
            service_manager="systemd-user",
            artifact_platform=PlatformKey("linux", "x86_64"),
            sandbox_backend="docker-rootless",
        )
        self.first = PrivilegeAction(
            category="linger",
            argv=("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger", "alice"),
            requires_sudo=True,
            reason="enable confirmed headless user service",
        )
        self.followup = PrivilegeAction(
            category="linger",
            argv=("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger", "bob"),
            requires_sudo=True,
            reason="enable confirmed headless user service",
        )

    def test_each_action_is_verified_before_and_after_exact_execution(self) -> None:
        """Task5 follow-up 不得跳过同一 verify/execute/verify 门禁。"""
        class Runner:
            """记录 exact argv 与 minimal environment。"""

            def __init__(self) -> None:
                """初始化调用记录。"""
                self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

            def run(
                self,
                argv: tuple[str, ...],
                *,
                env: dict[str, str],
                timeout: float,
            ) -> CommandResult:
                """模拟成功 privilege command。"""
                del timeout
                self.calls.append((argv, env))
                return CommandResult(0, b"", b"")

        runner = Runner()
        verifier = mock.Mock(side_effect=(None, (self.followup,), None, None))
        account = pwd.struct_passwd(
            ("alice", "x", 1001, 1001, "Alice", "/home/alice", "/bin/sh")
        )
        with (
            mock.patch(
                "lobster0.install.orchestrator.verify_privilege_action",
                verifier,
            ),
            mock.patch(
                "lobster0.install.orchestrator._invoking_account",
                return_value=account,
            ),
        ):
            executed = _execute_dependency_actions(
                (self.first,),
                self.platform,
                self.request,
                mock.sentinel.manifest,  # type: ignore[arg-type]
                Path("/bootstrap/sandbox-image.txt"),
                runner,  # type: ignore[arg-type]
            )
        self.assertEqual(executed, (self.first.argv, self.followup.argv))
        self.assertEqual(
            runner.calls,
            [
                (
                    self.first.argv,
                    {
                        "HOME": "/home/alice",
                        "LOGNAME": "alice",
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        "USER": "alice",
                        "XDG_RUNTIME_DIR": "/run/user/1001",
                    },
                ),
                (
                    self.followup.argv,
                    {
                        "HOME": "/home/alice",
                        "LOGNAME": "alice",
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        "USER": "alice",
                        "XDG_RUNTIME_DIR": "/run/user/1001",
                    },
                ),
            ],
        )
        self.assertEqual(
            [call.kwargs["after_execution"] for call in verifier.call_args_list],
            [False, True, False, True],
        )

    def test_failed_sudo_action_stops_before_post_verification(self) -> None:
        """拒绝 sudo 必须返回 privilege_denied 且不能伪造 postcondition。"""
        class Runner:
            """返回 bounded non-zero command result。"""

            def run(
                self,
                argv: tuple[str, ...],
                *,
                env: dict[str, str],
                timeout: float,
            ) -> CommandResult:
                """模拟操作员拒绝 sudo。"""
                del argv, env, timeout
                return CommandResult(1, b"", b"denied")

        verifier = mock.Mock(return_value=None)
        with (
            mock.patch(
                "lobster0.install.orchestrator.verify_privilege_action",
                verifier,
            ),
            self.assertRaisesRegex(InstallError, "privilege_denied"),
        ):
            _execute_dependency_actions(
                (self.first,),
                self.platform,
                self.request,
                mock.sentinel.manifest,  # type: ignore[arg-type]
                Path("/bootstrap/sandbox-image.txt"),
                Runner(),  # type: ignore[arg-type]
            )
        self.assertEqual(len(verifier.call_args_list), 1)

    def test_setup_actions_use_validated_target_environment_and_one_timeout(self) -> None:
        """direct/sudo setup 都不得继承 root HOME、Secret 或动态 PATH。"""
        class Runner:
            """记录每项 action 的 exact env 与 timeout。"""

            def __init__(self) -> None:
                """初始化有序调用记录。"""
                self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []

            def run(
                self,
                argv: tuple[str, ...],
                *,
                env: dict[str, str],
                timeout: float,
            ) -> CommandResult:
                """返回成功且不产生 raw output。"""
                self.calls.append((argv, env, timeout))
                return CommandResult(0, b"", b"")

        tool = "/usr/bin/dockerd-rootless-setuptool.sh"
        direct = PrivilegeAction(
            category="system-package",
            argv=(tool, "install"),
            requires_sudo=False,
            reason="configure rootless Docker for target user",
        )
        sudo = PrivilegeAction(
            category="system-package",
            argv=("/usr/bin/sudo", "-u", "alice", "--", tool, "install"),
            requires_sudo=True,
            reason="configure rootless Docker for target user",
        )
        account = pwd.struct_passwd(
            ("alice", "x", 1001, 1001, "Alice", "/home/alice", "/bin/sh")
        )
        runner = Runner()
        with (
            mock.patch(
                "lobster0.install.orchestrator.verify_privilege_action",
                return_value=None,
            ),
            mock.patch(
                "lobster0.install.orchestrator._invoking_account",
                return_value=account,
            ),
            mock.patch.dict(
                os.environ,
                {"HOME": "/root", "SECRET_SENTINEL": "must-not-inherit"},
                clear=True,
            ),
        ):
            _execute_dependency_actions(
                (direct, sudo),
                self.platform,
                replace(self.request, allow_system_packages=True),
                mock.sentinel.manifest,  # type: ignore[arg-type]
                Path("/bootstrap/sandbox-image.txt"),
                runner,  # type: ignore[arg-type]
            )
        expected = {
            "HOME": "/home/alice",
            "LOGNAME": "alice",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "USER": "alice",
            "XDG_RUNTIME_DIR": "/run/user/1001",
        }
        self.assertEqual(
            runner.calls,
            [
                (direct.argv, expected, 300.0),
                (sudo.argv, expected, 300.0),
            ],
        )


class DependencyPreparationTests(unittest.TestCase):
    """覆盖 bootstrap-private sandbox 下载与 live Task5 plan。"""

    def setUp(self) -> None:
        """创建只含测试所需 sandbox artifact 的 strict plan。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.image = b"example/lobster0@sha256:" + b"a" * 64 + b"\n"
        document = json.loads(Path("tests/install/manifest_v1.json").read_text(encoding="utf-8"))
        document["artifacts"].append(
            {
                "kind": "sandbox-image",
                "filename": "lobster0-sandbox-image-0.7.0.txt",
                "url": (
                    "https://github.com/NEDONION/lobster0/releases/download/"
                    "v0.7.0/lobster0-sandbox-image-0.7.0.txt"
                ),
                "sha256": hashlib.sha256(self.image).hexdigest(),
                "size": len(self.image),
                "media_type": "text/plain",
                "platform": {"os": "any", "arch": "any"},
                "component_version": "0.7.0",
                "source_repository": "https://github.com/NEDONION/lobster0",
                "license_ref": "MIT",
                "upstream_sha256": None,
            }
        )
        self.manifest = ReleaseManifest.from_bytes(json.dumps(document).encode())
        self.request = InstallRequest(
            action="install",
            version="0.7.0",
            channel="stable",
            prefix=self.root / "program",
            state_home=self.root / "state",
            system_prefix=False,
            onboard=True,
            config_file=None,
            secrets_file=None,
            service=False,
            allow_system_packages=False,
            dry_run=False,
            json_output=False,
            verbose=False,
            purge_data=False,
            confirm_data_loss=False,
        )
        self.platform = DetectedPlatform(
            os="linux",
            distro_id="ubuntu",
            distro_version="24.04",
            arch="x86_64",
            service_manager="systemd-user",
            artifact_platform=PlatformKey("linux", "x86_64"),
            sandbox_backend="docker-rootless",
        )
        self.plan = InstallPlan(
            request=self.request,
            manifest=self.manifest,
            platform=self.platform.artifact_platform,
            distro_id=self.platform.distro_id,
            distro_version=self.platform.distro_version,
            service_manager=self.platform.service_manager,
            program_prefix=self.root / "program",
            state_home=self.root / "state",
            artifact_filenames=tuple(
                artifact.filename for artifact in self.manifest.artifacts
            ),
            system_argvs=(),
            install_service=False,
            run_onboarding=True,
        )
        python_root = self.root / "python"
        self.bootstrap = BootstrapInputs(
            self.root / "release-manifest.json",
            "1" * 64,
            self.root / "uv" / "uv",
            python_root,
            python_root / "bin" / "python3.12",
            (),
        )

    def test_prepare_downloads_sandbox_before_building_live_dependency_plan(self) -> None:
        """Task5 build 必须接收 bootstrap tree 内已验证的 exact artifact。"""
        operations = _SystemOperations(self.bootstrap, opener=_Opener(self.image))
        operations._platform = self.platform
        with mock.patch(
            "lobster0.install.orchestrator.build_dependency_actions",
            return_value=DependencyPlan(()),
        ) as built:
            prepared = operations.prepare_dependencies(self.plan, self.bootstrap)
        sandbox = self.root / "lobster0-sandbox-image-0.7.0.txt"
        self.assertEqual(sandbox.read_bytes(), self.image)
        self.assertEqual(prepared.system_argvs, ())
        built.assert_called_once_with(
            self.platform,
            self.request,
            manifest=self.manifest,
            sandbox_artifact_path=sandbox,
        )

    def test_failed_live_plan_removes_ephemeral_sandbox_artifact(self) -> None:
        """Task5 probe 失败不得在 bootstrap tree 留下误导性 verified artifact。"""
        operations = _SystemOperations(self.bootstrap, opener=_Opener(self.image))
        operations._platform = self.platform
        with (
            mock.patch(
                "lobster0.install.orchestrator.build_dependency_actions",
                side_effect=InstallError("system_dependency_missing", "platform"),
            ),
            self.assertRaisesRegex(InstallError, "system_dependency_missing"),
        ):
            operations.prepare_dependencies(self.plan, self.bootstrap)
        self.assertFalse((self.root / "lobster0-sandbox-image-0.7.0.txt").exists())


class CommitRollbackTests(unittest.TestCase):
    """覆盖 fresh launcher 已创建但 receipt 未提交的 crash window。"""

    def test_exact_fresh_launcher_and_link_are_removed_without_receipt(self) -> None:
        """rollback 只删除本事务 exact-hash metadata。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            command_dir = root / "command"
            bin_dir.mkdir()
            command_dir.mkdir()
            launcher = bin_dir / "lobster0"
            launcher.write_bytes(b"launcher")
            launcher.chmod(0o700)
            command = command_dir / "lobster0"
            command.symlink_to(Path("../bin/lobster0"))
            layout = SimpleNamespace(launcher=launcher, command_link=command)
            _remove_created_install_metadata(
                layout,  # type: ignore[arg-type]
                launcher_sha256=managed_file_sha256(launcher),
                command_sha256=managed_file_sha256(command, require_symlink=True),
                launcher_existed=False,
                command_existed=False,
            )
            self.assertFalse(launcher.exists())
            self.assertFalse(command.is_symlink())

    def test_drifted_launcher_is_preserved_and_reports_rollback_conflict(self) -> None:
        """foreign replacement 不能被 cleanup 当成本事务文件删除。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "lobster0"
            launcher.write_bytes(b"foreign")
            launcher.chmod(0o700)
            layout = SimpleNamespace(launcher=launcher, command_link=root / "command")
            with self.assertRaisesRegex(InstallError, "rollback_conflict"):
                _remove_created_install_metadata(
                    layout,  # type: ignore[arg-type]
                    launcher_sha256="0" * 64,
                    command_sha256="1" * 64,
                    launcher_existed=False,
                    command_existed=True,
                )
            self.assertEqual(launcher.read_bytes(), b"foreign")


class RetryRecoveryTests(unittest.TestCase):
    """覆盖 Task8 public Runtime recovery 与 Task11 downloads quarantine。"""

    def setUp(self) -> None:
        """创建当前 owner 的真实 user layout。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.home.chmod(0o700)
        self.layout = InstallLayout.user(self.home, version="0.7.0")
        self.layout.runtimes_dir.mkdir(mode=0o700, parents=True)
        self.layout.program_prefix.chmod(0o700)
        self.operations = _SystemOperations(mock.sentinel.bootstrap)  # type: ignore[arg-type]
        self.manifest = ReleaseManifest.from_bytes(
            (Path(__file__).parent / "install" / "manifest_v1.json").read_bytes()
        )

    def _receipt(self, *, service: bool) -> InstallReceipt:
        """返回可写入当前 layout 的 valid fresh/managed receipt。"""
        return InstallReceipt(
            schema_version=1,
            version="0.7.0",
            git_commit="a" * 40,
            platform=PlatformKey("macos", "arm64"),
            installed_at="2026-08-10T00:00:00Z",
            managed_files=(("bin/lobster0", "b" * 64),),
            current_runtime="runtimes/0.7.0",
            previous_runtime=None,
            service_label="io.lobster0.gateway" if service else None,
            service_file="Library/LaunchAgents/io.lobster0.gateway.plist" if service else None,
            service_file_sha256="c" * 64 if service else None,
        )

    def test_fresh_layout_recovery_treats_missing_runtimes_as_empty(self) -> None:
        """首次安装缺少 runtimes/ 必须返回 empty 并通过真实 lock recovery。"""
        fresh_home = self.home / "fresh"
        fresh_home.mkdir(mode=0o700)
        layout = InstallLayout.user(fresh_home, version="0.7.0")
        with mock.patch(
            "lobster0.install.layout._probe_process",
            return_value=("alive", "2026-08-10T00:00:00Z"),
        ):
            with InstallLock.acquire(layout) as lock:
                try:
                    recovered = install_orchestrator._discard_stale_downloads(layout, lock)
                except InstallError as error:
                    self.fail(f"missing runtimes was not empty: {error.code}")
                self.assertIs(recovered, False)
                self.operations.recover(layout, lock, self.manifest)
        self.assertFalse(layout.runtimes_dir.exists())

    def test_interrupted_staging_recovery_allows_same_version_retry(self) -> None:
        """Task8D public recovery 必须收到 exact facts，并让同版本 retry。"""
        staging = self.layout.staging
        staging.mkdir(mode=0o700)
        partial = staging / "partial"
        partial.mkdir(mode=0o700)
        data = partial / "data"
        data.write_bytes(b"interrupted")
        data.chmod(0o600)
        order: list[str] = []

        def discard_staging(*_args: object) -> bool:
            """模拟已由 Task8D 验证并 durable 删除 marker-bound residue。"""
            order.append("staging")
            shutil.rmtree(staging)
            return True

        def discard_runtime(*_args: object) -> bool:
            """记录 final Runtime recovery 必须发生在 staging 之后。"""
            order.append("runtime")
            return False

        with (
            mock.patch(
                "lobster0.install.orchestrator.discard_interrupted_runtime_staging",
                side_effect=discard_staging,
            ) as recover_staging,
            mock.patch(
                "lobster0.install.orchestrator.discard_unactivated_runtime",
                side_effect=discard_runtime,
            ) as recover_runtime,
            mock.patch(
                "lobster0.install.layout._probe_process",
                return_value=("alive", "2026-08-10T00:00:00Z"),
            ),
        ):
            with InstallLock.acquire(self.layout) as lock:
                self.operations.recover(self.layout, lock, self.manifest)
                recover_staging.assert_called_once_with(self.layout, lock, self.manifest)
                recover_runtime.assert_called_once_with(self.layout, lock)
        self.assertEqual(order, ["staging", "runtime"])
        self.assertFalse(staging.exists())
        staging.mkdir(mode=0o700)
        self.assertTrue(staging.is_dir())

    def test_staging_recovery_propagates_reference_foreign_and_lock_errors(self) -> None:
        """Task8D 的 reference、foreign tree 与 lock 错误均不得被 Task11 吞掉。"""
        for case in ("reference", "foreign", "lock"):
            with self.subTest(case=case):
                home = self.home / case
                home.mkdir(mode=0o700)
                layout = InstallLayout.user(home, version=self.manifest.version)
                layout.runtimes_dir.mkdir(mode=0o700, parents=True)
                layout.program_prefix.chmod(0o700)
                marker: Path | None = None
                if case == "reference":
                    layout.current.symlink_to(f"runtimes/{self.manifest.version}")
                elif case == "foreign":
                    external = home / "external"
                    external.mkdir(mode=0o700)
                    marker = external / "marker"
                    marker.write_bytes(b"foreign")
                    layout.staging.symlink_to(external, target_is_directory=True)
                with mock.patch(
                    "lobster0.install.layout._probe_process",
                    return_value=("alive", "2026-08-10T00:00:00Z"),
                ):
                    lock = InstallLock.acquire(layout)
                    if case == "lock":
                        lock.close()
                    try:
                        with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
                            self.operations.recover(layout, lock, self.manifest)
                    finally:
                        lock.close()
                if marker is not None:
                    self.assertEqual(marker.read_bytes(), b"foreign")

    def test_recovery_calls_public_runtime_api_and_removes_exact_stale_downloads(self) -> None:
        """lock 内恢复必须复用 Task8 public API 并清理 exact owner downloads。"""
        stale = self.layout.runtimes_dir / ".0.7.0.downloads"
        stale.mkdir(mode=0o700)
        (stale / "partial").write_bytes(b"partial")
        with (
            mock.patch(
                "lobster0.install.orchestrator.discard_interrupted_runtime_staging",
                return_value=False,
            ) as discard_staging,
            mock.patch(
                "lobster0.install.orchestrator.discard_unactivated_runtime",
                return_value=True,
            ) as discard_runtime,
            mock.patch(
                "lobster0.install.layout._probe_process",
                return_value=("alive", "2026-08-10T00:00:00Z"),
            ),
        ):
            with InstallLock.acquire(self.layout) as held_lock:
                self.operations.recover(self.layout, held_lock, self.manifest)
                discard_staging.assert_called_once_with(
                    self.layout,
                    held_lock,
                    self.manifest,
                )
                discard_runtime.assert_called_once_with(self.layout, held_lock)
        self.assertFalse(stale.exists())

    def test_recovery_preserves_downloads_when_actual_lock_is_closed(self) -> None:
        """closed actual lock 不得先删除 downloads 再由 Task8 报错。"""
        stale = self.layout.runtimes_dir / ".0.7.0.downloads"
        stale.mkdir(mode=0o700)
        marker = stale / "partial"
        marker.write_bytes(b"preserve")
        identity = (stale.lstat().st_dev, stale.lstat().st_ino)
        with mock.patch(
            "lobster0.install.layout._probe_process",
            return_value=("alive", "2026-08-10T00:00:00Z"),
        ):
            lock = InstallLock.acquire(self.layout)
            lock.close()
            with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
                self.operations.recover(self.layout, lock, self.manifest)
        self.assertTrue(stale.is_dir())
        self.assertEqual((stale.lstat().st_dev, stale.lstat().st_ino), identity)
        self.assertEqual(marker.read_bytes(), b"preserve")

    def test_recovery_preserves_downloads_when_lock_path_is_replaced(self) -> None:
        """lock pathname replacement 必须在 downloads rename 前 fail closed。"""
        stale = self.layout.runtimes_dir / ".0.7.0.downloads"
        stale.mkdir(mode=0o700)
        marker = stale / "partial"
        marker.write_bytes(b"preserve")
        identity = (stale.lstat().st_dev, stale.lstat().st_ino)
        with mock.patch(
            "lobster0.install.layout._probe_process",
            return_value=("alive", "2026-08-10T00:00:00Z"),
        ):
            lock = InstallLock.acquire(self.layout)
            self.layout.lock.unlink()
            self.layout.lock.write_bytes(b"foreign\n")
            self.layout.lock.chmod(0o600)
            try:
                with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
                    self.operations.recover(self.layout, lock, self.manifest)
            finally:
                lock.close()
        self.assertTrue(stale.is_dir())
        self.assertEqual((stale.lstat().st_dev, stale.lstat().st_ino), identity)
        self.assertEqual(marker.read_bytes(), b"preserve")
        self.assertEqual(self.layout.lock.read_bytes(), b"foreign\n")

    def test_recovery_preserves_private_downloads_evidence_after_lock_drift(self) -> None:
        """quarantine 后 lock 漂移必须保留 exact private downloads evidence。"""
        stale = self.layout.runtimes_dir / ".0.7.0.downloads"
        stale.mkdir(mode=0o700)
        marker = stale / "partial"
        marker.write_bytes(b"preserve")
        identity = (stale.lstat().st_dev, stale.lstat().st_ino)
        real_rename = os.rename

        def replace_lock_after_quarantine(source: object, destination: object) -> None:
            """完成 exact downloads rename 后替换 lock pathname。"""
            real_rename(source, destination)
            if Path(source) == stale:
                self.layout.lock.unlink()
                self.layout.lock.write_bytes(b"foreign\n")
                self.layout.lock.chmod(0o600)

        with mock.patch(
            "lobster0.install.layout._probe_process",
            return_value=("alive", "2026-08-10T00:00:00Z"),
        ):
            lock = InstallLock.acquire(self.layout)
            try:
                with (
                    mock.patch.object(
                        install_orchestrator.os,
                        "rename",
                        side_effect=replace_lock_after_quarantine,
                    ),
                    self.assertRaisesRegex(InstallError, "runtime_install_failed"),
                ):
                    self.operations.recover(self.layout, lock, self.manifest)
            finally:
                lock.close()
        evidence = tuple(
            self.layout.runtimes_dir.glob(".0.7.0.downloads-quarantine-*/payload")
        )
        self.assertEqual(len(evidence), 1)
        self.assertEqual((evidence[0].lstat().st_dev, evidence[0].lstat().st_ino), identity)
        self.assertEqual((evidence[0] / "partial").read_bytes(), b"preserve")
        self.assertEqual(self.layout.lock.read_bytes(), b"foreign\n")

    def test_recovery_never_follows_foreign_downloads_symlink(self) -> None:
        """伪造 downloads symlink 必须保留其 target 并稳定失败。"""
        external = self.home / "foreign"
        external.mkdir(mode=0o700)
        marker = external / "marker"
        marker.write_bytes(b"foreign")
        stale = self.layout.runtimes_dir / ".0.7.0.downloads"
        stale.symlink_to(external, target_is_directory=True)
        with mock.patch(
            "lobster0.install.layout._probe_process",
            return_value=("alive", "2026-08-10T00:00:00Z"),
        ):
            with InstallLock.acquire(self.layout) as lock:
                with self.assertRaisesRegex(InstallError, "runtime_install_failed"):
                    self.operations.recover(self.layout, lock, self.manifest)
        self.assertTrue(stale.is_symlink())
        self.assertEqual(marker.read_bytes(), b"foreign")

    def test_cleanup_removes_only_the_recorded_downloads_inode(self) -> None:
        """事务 cleanup 只能删除 download 时绑定的 exact inode。"""
        downloads = self.layout.runtimes_dir / ".0.7.0.downloads"
        downloads.mkdir(mode=0o700)
        metadata = downloads.lstat()
        self.operations._download_roots[self.layout.program_prefix] = (
            downloads,
            (metadata.st_dev, metadata.st_ino),
        )
        with mock.patch(
            "lobster0.install.layout._probe_process",
            return_value=("alive", "2026-08-10T00:00:00Z"),
        ):
            with InstallLock.acquire(self.layout) as lock:
                self.operations.cleanup(self.layout, lock)
        self.assertFalse(downloads.exists())

    def test_cleanup_preserves_replaced_downloads_inode(self) -> None:
        """同路径 foreign replacement 不得被事务 finally 当作 staging 删除。"""
        downloads = self.layout.runtimes_dir / ".0.7.0.downloads"
        downloads.mkdir(mode=0o700)
        metadata = downloads.lstat()
        downloads.rmdir()
        downloads.mkdir(mode=0o700)
        marker = downloads / "foreign"
        marker.write_bytes(b"foreign")
        self.operations._download_roots[self.layout.program_prefix] = (
            downloads,
            (metadata.st_dev, metadata.st_ino),
        )
        with mock.patch(
            "lobster0.install.layout._probe_process",
            return_value=("alive", "2026-08-10T00:00:00Z"),
        ):
            with InstallLock.acquire(self.layout) as lock:
                self.operations.cleanup(self.layout, lock)
        self.assertEqual(marker.read_bytes(), b"foreign")

    def test_existing_managed_receipt_stops_fresh_install_before_recovery(self) -> None:
        """Task11 fresh install 不得覆盖 Task13 才能迁移的 managed receipt。"""
        receipt = self._receipt(service=True)
        receipt.write(self.layout.receipt)
        before = self.layout.receipt.read_bytes()
        with (
            mock.patch(
                "lobster0.install.orchestrator.discard_unactivated_runtime",
            ) as discard,
            self.assertRaisesRegex(InstallError, "request_invalid"),
        ):
            self.operations.recover(
                self.layout,
                mock.sentinel.held_lock,  # type: ignore[arg-type]
                self.manifest,
            )
        discard.assert_not_called()
        self.assertEqual(self.layout.receipt.read_bytes(), before)

    def test_commit_never_clears_existing_service_receipt(self) -> None:
        """即使绕过 recovery，commit 也不能先清空已有 service metadata。"""
        receipt = self._receipt(service=True)
        receipt.write(self.layout.receipt)
        before = self.layout.receipt.read_bytes()
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            self.operations.commit(
                mock.sentinel.plan,  # type: ignore[arg-type]
                self.layout,
                mock.sentinel.runtime,  # type: ignore[arg-type]
            )
        self.assertEqual(self.layout.receipt.read_bytes(), before)


class ServiceTransactionTests(unittest.TestCase):
    """覆盖 fresh service 与 receipt/retention 的原子回滚。"""

    def setUp(self) -> None:
        """创建 user layout、fresh receipt 与离线 service plan。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.home.chmod(0o700)
        self.layout = InstallLayout.user(self.home, version="0.7.0")
        self.layout.runtimes_dir.mkdir(mode=0o700, parents=True)
        self.layout.program_prefix.chmod(0o700)
        InstallReceipt(
            schema_version=1,
            version="0.7.0",
            git_commit="a" * 40,
            platform=PlatformKey("linux", "x86_64"),
            installed_at="2026-08-10T00:00:00Z",
            managed_files=(("bin/lobster0", "b" * 64),),
            current_runtime="runtimes/0.7.0",
            previous_runtime=None,
            service_label=None,
            service_file=None,
            service_file_sha256=None,
        ).write(self.layout.receipt)
        self.operations = _SystemOperations(mock.sentinel.bootstrap)  # type: ignore[arg-type]
        self.plan = SimpleNamespace(
            service_manager="systemd-user",
            request=SimpleNamespace(system_prefix=False),
        )
        self.account = pwd.struct_passwd(
            ("alice", "x", os.geteuid(), os.getegid(), "Alice", str(self.home), "/bin/sh")
        )
        self.spec = SimpleNamespace(
            label="lobster0-gateway.service",
            path=self.home / ".config/systemd/user/lobster0-gateway.service",
        )

    def test_receipt_write_failure_uninstalls_new_service_with_exact_hash(self) -> None:
        """service 成功后 receipt 失败必须立即调用 Task10 public uninstall。"""
        digest = "d" * 64
        with (
            mock.patch(
                "lobster0.install.orchestrator._invoking_account",
                return_value=self.account,
            ),
            mock.patch(
                "lobster0.install.orchestrator.render_service_spec",
                return_value=self.spec,
            ),
            mock.patch(
                "lobster0.install.orchestrator.service_install",
                return_value=digest,
            ),
            mock.patch(
                "lobster0.install.orchestrator.service_uninstall",
            ) as uninstall,
            mock.patch.object(
                InstallReceipt,
                "write",
                side_effect=InstallError("installer_error", "manifest"),
            ),
            self.assertRaisesRegex(InstallError, "installer_error"),
        ):
            self.operations.install_service(
                self.plan,  # type: ignore[arg-type]
                self.layout,
            )
        uninstall.assert_called_once_with(
            self.spec,
            self.operations.runner,
            expected_sha256=digest,
        )

    def test_invalid_service_destination_fails_before_install(self) -> None:
        """目标用户或相对路径无法验证时不得先创建无 receipt 的 service。"""
        with (
            mock.patch(
                "lobster0.install.orchestrator.render_service_spec",
                return_value=self.spec,
            ),
            mock.patch(
                "lobster0.install.orchestrator._invoking_account",
                side_effect=InstallError("privilege_denied", "platform"),
            ),
            mock.patch(
                "lobster0.install.orchestrator.service_install",
            ) as install,
            self.assertRaisesRegex(InstallError, "privilege_denied"),
        ):
            self.operations.install_service(
                self.plan,  # type: ignore[arg-type]
                self.layout,
            )
        install.assert_not_called()

    def test_retention_failure_rollback_uninstalls_service_before_metadata(self) -> None:
        """retention crash 的 rollback 必须先撤销已落地 service。"""
        digest = "e" * 64
        self.operations._installed_services[self.layout.program_prefix] = (
            ServicePlatform.SYSTEMD_USER,
            digest,
            False,
        )
        order: list[str] = []
        with (
            mock.patch(
                "lobster0.install.orchestrator.render_service_spec",
                return_value=self.spec,
            ),
            mock.patch(
                "lobster0.install.orchestrator.service_uninstall",
                side_effect=lambda *_args, **_kwargs: order.append("service"),
            ) as uninstall,
            mock.patch(
                "lobster0.install.orchestrator._restore_current",
                side_effect=lambda *_args: order.append("metadata"),
            ),
        ):
            self.operations.rollback(self.layout, None, "service")
        uninstall.assert_called_once_with(
            self.spec,
            self.operations.runner,
            expected_sha256=digest,
        )
        self.assertEqual(order, ["service", "metadata"])


class BootstrapTrustTests(unittest.TestCase):
    """验证 manifest/uv/Python 必须绑定同一 private bootstrap tree。"""

    def setUp(self) -> None:
        """创建 owner-only、无 symlink 的最小 bootstrap tree。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.manifest = self.root / "release-manifest.json"
        self.manifest.write_bytes(b"{}")
        self.manifest.chmod(0o600)
        self.uv_dir = self.root / "uv"
        self.uv_dir.mkdir(mode=0o700)
        self.uv = self.uv_dir / "uv"
        self.uv.write_bytes(b"uv")
        self.uv.chmod(0o700)
        self.python_root = self.root / "python"
        (self.python_root / "bin").mkdir(parents=True, mode=0o700)
        self.python = self.python_root / "bin" / "python3.12"
        self.python.write_bytes(b"python")
        self.python.chmod(0o700)
        self.inputs = BootstrapInputs(
            self.manifest,
            hashlib.sha256(b"{}").hexdigest(),
            self.uv,
            self.python_root,
            self.python,
            (),
        )

    def test_verified_manifest_hash_and_runtime_inputs_are_accepted(self) -> None:
        """同一 current-owner 0700 tree 内的 exact inputs 才能进入 Runtime。"""
        manifest = verify_bootstrap_inputs(self.inputs)
        self.assertEqual(manifest, b"{}")

    def test_manifest_hash_mismatch_is_rejected(self) -> None:
        """manifest bytes 与 bootstrap hash 不一致时必须稳定失败。"""
        with self.assertRaisesRegex(InstallError, "artifact_hash_mismatch"):
            verify_bootstrap_inputs(replace(self.inputs, manifest_sha256="0" * 64))

    def test_symlink_or_different_tree_runtime_input_is_rejected(self) -> None:
        """任一 symlink 或不同 bootstrap root 都不能进入 RuntimeBuilder。"""
        link = self.root / "uv-link"
        link.symlink_to(self.uv)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            verify_bootstrap_inputs(replace(self.inputs, managed_uv=link))
        other = self.root.parent / f"{self.root.name}-other-uv"
        other.write_bytes(b"uv")
        other.chmod(0o700)
        self.addCleanup(other.unlink)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            verify_bootstrap_inputs(replace(self.inputs, managed_uv=other))

    def test_group_writable_parent_or_non_executable_input_is_rejected(self) -> None:
        """bootstrap tree 目录必须 0700，uv/Python 必须 owner-only executable。"""
        self.uv_dir.chmod(0o770)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            verify_bootstrap_inputs(self.inputs)
        self.uv_dir.chmod(0o700)
        self.python.chmod(0o600)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            verify_bootstrap_inputs(self.inputs)


class InstallerCliTests(unittest.TestCase):
    """覆盖 CLI 冲突、非 TTY、退出码和输出流纯度。"""

    INTERNAL = (
        "--manifest-file",
        "/tmp/bootstrap/release-manifest.json",
        "--manifest-sha256",
        "1" * 64,
        "--managed-uv",
        "/tmp/bootstrap/uv/uv",
        "--managed-python-root",
        "/tmp/bootstrap/python",
        "--managed-python-executable",
        "/tmp/bootstrap/python/bin/python3.12",
    )

    def _main(self, public: tuple[str, ...], *, tty: bool = False) -> tuple[int, str, str]:
        """运行 parser，但用 fake installer 避免触碰真实主机。"""
        class FakeInstaller:
            """返回一条固定安全事件。"""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                """接受 production factory 参数。"""

            def run(self, request: InstallRequest) -> InstallResult:
                """返回与请求 action 一致的结果。"""
                event = InstallEvent("install.complete", "ok", None, "version")
                return InstallResult(
                    request.action,
                    "0.7.0",
                    PlatformKey("linux", "x86_64"),
                    not request.dry_run,
                    (event,),
                )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                (*public, *self.INTERNAL),
                stdin_isatty=tty,
                installer_factory=FakeInstaller,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_mutually_exclusive_public_flags_return_request_exit(self) -> None:
        """version/channel、prefix、service 与 onboarding 冲突统一返回 2。"""
        cases = (
            ("--version", "0.7.0-rc.1", "--channel", "stable"),
            ("--prefix", "/tmp/p", "--system-prefix"),
            ("--install-service", "--no-install-service"),
            ("--onboard", "--no-onboard"),
        )
        for public in cases:
            with self.subTest(public=public):
                code, stdout, _stderr = self._main(public)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")

    def test_duplicate_internal_flag_is_rejected(self) -> None:
        """内部 bootstrap flags 重复时不得采用 last-wins。"""
        code, stdout, _stderr = self._main(
            ("--manifest-sha256", "2" * 64,)
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")

    def test_public_argv_removes_action_even_when_an_option_precedes_it(self) -> None:
        """target handoff 不能把合法的非首位 action 当作第二个 positional。"""
        self.assertEqual(
            _public_argv(("--json", "update", *self.INTERNAL), "update"),
            ("--json",),
        )

    def test_system_prefix_default_home_belongs_to_validated_invoking_user(self) -> None:
        """sudo 安装未显式 --home 时不得采用 root 的 Path.home。"""
        requests: list[InstallRequest] = []

        class CapturingInstaller:
            """记录 CLI 构造的 strict request。"""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                """接受 bootstrap inputs。"""

            def run(self, request: InstallRequest) -> InstallResult:
                """保存请求并返回安全结果。"""
                requests.append(request)
                return InstallResult(
                    "install",
                    "0.7.0",
                    PlatformKey("linux", "x86_64"),
                    False,
                    (),
                )

        account = pwd.struct_passwd(
            ("alice", "x", 1001, 1001, "Alice", "/home/alice", "/bin/sh")
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            installer_cli,
            "_invoking_account",
            return_value=account,
            create=True,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                ("--system-prefix", *self.INTERNAL),
                stdin_isatty=True,
                installer_factory=CapturingInstaller,
            )
        self.assertEqual(code, 0)
        self.assertEqual(requests[0].state_home, Path("/home/alice/.lobster0"))
        self.assertEqual(stdout.getvalue(), "")

    def test_non_tty_requires_complete_import_or_onboarding(self) -> None:
        """stdin 非 TTY 且关闭 onboarding 时必须同时提供 config 与 Secret。"""
        code, stdout, _stderr = self._main(("--no-onboard",), tty=False)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")

    def test_imports_conflict_with_onboarding_and_channel_must_match_version(self) -> None:
        """导入只能配 no-onboard，显式版本 prerelease 属性必须匹配 channel。"""
        cases = (
            ("--config", "/tmp/c", "--secrets-file", "/tmp/s"),
            ("--version", "0.7.1", "--channel", "dev"),
        )
        for public in cases:
            with self.subTest(public=public):
                code, stdout, _stderr = self._main(public)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")

    def test_non_tty_purge_requires_explicit_confirmation(self) -> None:
        """无 TTY 的 purge 不接受隐式确认。"""
        code, stdout, _stderr = self._main(("uninstall", "--purge-data"), tty=False)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")

    def test_transaction_failure_returns_three_with_one_redacted_json_event(self) -> None:
        """事务错误必须返回 3，且 JSON stdout 只含 stable failure event。"""
        class FailingInstaller:
            """在事务入口返回不含底层异常的稳定失败。"""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                """接受 bootstrap inputs。"""

            def run(self, _request: InstallRequest) -> InstallResult:
                """模拟 Doctor gate 阻断。"""
                raise InstallError("doctor_blocked", "service")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                ("--json", *self.INTERNAL),
                stdin_isatty=True,
                installer_factory=FailingInstaller,
            )
        self.assertEqual(code, 3)
        self.assertEqual(
            stdout.getvalue(),
            '{"code":"doctor_blocked","detail":"service",'
            '"event":"install.failed","status":"error"}\n',
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_json_mode_writes_only_compact_ndjson_to_stdout(self) -> None:
        """JSON 模式按序输出 event，最后输出 exact structured target。"""
        code, stdout, stderr = self._main(("--json", "--dry-run"), tty=False)
        self.assertEqual(code, 0)
        self.assertEqual(
            stdout,
            '{"code":null,"detail":"version","event":"install.complete","status":"ok"}\n'
            '{"action":"install","changed":false,"event":"install.result",'
            '"platform":{"arch":"x86_64","os":"linux"},"version":"0.7.0"}\n',
        )
        self.assertEqual(stderr, "")

    def test_structured_result_never_contains_import_source_paths(self) -> None:
        """final JSON 只含 strict fields，不得回显 config/Secret source path。"""
        code, stdout, stderr = self._main(
            (
                "--json",
                "--no-onboard",
                "--config",
                "/tmp/SECRET_SENTINEL-config.toml",
                "--secrets-file",
                "/tmp/SECRET_SENTINEL-secrets.env",
            ),
            tty=False,
        )
        self.assertEqual(code, 0)
        self.assertNotIn("SECRET_SENTINEL", stdout + stderr)

    def test_human_mode_renders_same_structured_result_fields(self) -> None:
        """human stderr 必须展示与 JSON 相同的 target facts。"""
        code, stdout, stderr = self._main(("--dry-run",), tty=True)
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn(
            "[RESULT] action=install version=0.7.0 platform=linux/x86_64 changed=false\n",
            stderr,
        )

    def test_install_result_rejects_noncanonical_structured_fields(self) -> None:
        """result version/platform 不得成为绕过 InstallEvent 的任意输出通道。"""
        with self.assertRaisesRegex(InstallError, "installer_error"):
            InstallResult(
                "install",
                "version=SECRET_SENTINEL",
                PlatformKey("linux", "x86_64"),
                False,
                (),
            )

    def test_human_mode_uses_stderr_and_bounds_detail(self) -> None:
        """人类输出不污染 stdout，且不转发超长原始诊断。"""
        stdout = io.StringIO()
        stderr = io.StringIO()
        event = InstallEvent("install.complete", "ok", None, "x" * 100_000)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            emit_event(event, json_output=False)
        self.assertEqual(stdout.getvalue(), "")
        self.assertLessEqual(len(stderr.getvalue().encode()), 4096)


class SystemPrefixCommandTests(unittest.TestCase):
    """覆盖 root 父进程到 target-user Runtime 命令的统一边界。"""

    def test_setup_doctor_and_service_use_same_validated_target_user(self) -> None:
        """三类 state/service 命令都必须 sudo user + env -i 且不传 Secret 值。"""
        class Runner:
            """记录 bounded exact commands 并提供 service helper JSON。"""

            def __init__(self) -> None:
                """初始化调用记录。"""
                self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

            def run(
                self,
                argv: tuple[str, ...],
                *,
                env: dict[str, str],
                timeout: float,
            ) -> CommandResult:
                """setup/Doctor 成功，service 返回 bounded metadata。"""
                del timeout
                self.calls.append((argv, env))
                if len(self.calls) == 3:
                    payload = json.dumps(
                        [
                            "a" * 64,
                            "lobster0.service",
                            "/home/alice/.config/systemd/user/lobster0.service",
                        ]
                    ).encode()
                    return CommandResult(0, payload, b"")
                return CommandResult(0, b"", b"")

        runtime = Path("/usr/local/lib/lobster0/runtimes/0.7.0")
        layout = SimpleNamespace(
            program_prefix=Path("/usr/local/lib/lobster0"),
            state_home=Path("/home/alice/.lobster0"),
            command_link=Path("/usr/local/bin/lobster0"),
            runtime=runtime,
            secrets_file=Path("/home/alice/.lobster0/secrets.env"),
        )
        account = pwd.struct_passwd(
            ("alice", "x", 1001, 1001, "Alice", "/home/alice", "/bin/sh")
        )
        runner = Runner()
        python = runtime / "venv" / "bin" / "python"
        with mock.patch(
            "lobster0.install.orchestrator._invoking_account",
            return_value=account,
        ):
            for command in ("setup", "doctor"):
                _checked_owner_command(
                    runner,  # type: ignore[arg-type]
                    (str(python), "-I", "-m", "lobster0", command),
                    layout,  # type: ignore[arg-type]
                    system_prefix=True,
                )
            digest, label, relative = _install_service_as_owner(
                layout,  # type: ignore[arg-type]
                ServicePlatform.SYSTEMD_USER,
                runner,  # type: ignore[arg-type]
            )
        self.assertEqual((digest, label), ("a" * 64, "lobster0.service"))
        self.assertEqual(relative, ".config/systemd/user/lobster0.service")
        base_environment = {
            "HOME": "/home/alice",
            "LOGNAME": "alice",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "USER": "alice",
            "XDG_RUNTIME_DIR": "/run/user/1001",
        }
        for index, (argv, environment) in enumerate(runner.calls):
            self.assertEqual(
                argv[:8],
                (
                    "/usr/bin/sudo",
                    "-u",
                    "alice",
                    "--",
                    "/usr/bin/env",
                    "-i",
                    "HOME=/home/alice",
                    "PATH=/usr/local/bin:/usr/bin:/bin",
                ),
            )
            self.assertIn("USER=alice", argv)
            self.assertIn("LOGNAME=alice", argv)
            self.assertIn("XDG_RUNTIME_DIR=/run/user/1001", argv)
            self.assertIn(str(python), argv)
            expected = dict(base_environment)
            if index < 2:
                expected.update(
                    {
                        "LOBSTER0_ENV_FILE": "/home/alice/.lobster0/secrets.env",
                        "LOBSTER0_HOME": "/home/alice/.lobster0",
                    }
                )
            self.assertEqual(environment, expected)
            self.assertNotIn("SECRET_SENTINEL", repr((argv, environment)))


class InteractiveSetupRunnerTests(unittest.TestCase):
    """覆盖 installer setup 的 controlling TTY 与 init stdin 隔离。"""

    def _setup_case(
        self,
        *,
        run_onboarding: bool,
    ) -> tuple[_SystemOperations, SimpleNamespace, SimpleNamespace]:
        """构造只运行 setup/init 选择逻辑的最小完整 case。"""
        operations = _SystemOperations(mock.sentinel.bootstrap)  # type: ignore[arg-type]
        operations.runner = mock.sentinel.closed_stdin_runner  # type: ignore[assignment]
        operations.interactive_runner = mock.sentinel.tty_runner  # type: ignore[attr-defined]
        layout = SimpleNamespace(
            program_prefix=Path("/program"),
            runtime=Path("/program/runtimes/0.7.0"),
            state_home=Path("/state"),
            secrets_file=Path("/state/secrets.env"),
        )
        operations._downloaded[layout.program_prefix] = SimpleNamespace(
            root=Path("/scratch"),
            sandbox_image="example/lobster0@sha256:" + "a" * 64,
        )
        plan = SimpleNamespace(
            request=SimpleNamespace(
                config_file=None,
                secrets_file=None,
                system_prefix=False,
            ),
            run_onboarding=run_onboarding,
        )
        return operations, layout, plan

    def test_real_interactive_setup_can_open_controlling_tty(self) -> None:
        """交互 runner 不得创建新 session 或关闭 setup 的 stdin。"""
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            code = (
                "import sys;from pathlib import Path;"
                "from lobster0.paths import build_state_paths;"
                "from lobster0.setup import run_interactive_setup;"
                "run_interactive_setup(build_state_paths(Path(sys.argv[1])),"
                "sandbox_image='ghcr.io/nedonion/lobster0-sandbox@sha256:"
                + "a" * 64
                + "')"
            )
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"This process .* forkpty\(\) may lead to deadlocks",
                    category=DeprecationWarning,
                )
                pid, descriptor = pty.fork()
            if pid == 0:
                runner = install_orchestrator._InteractiveSubprocessRunner()
                try:
                    result = runner.run(
                        (sys.executable, "-I", "-c", code, str(state)),
                        env={"HOME": temporary, "PATH": "/usr/bin:/bin"},
                        timeout=10.0,
                    )
                except BaseException as error:
                    os.write(2, type(error).__name__.encode() + b"\n")
                    os._exit(1)
                if result.returncode != 0:
                    os.write(2, result.stdout + result.stderr)
                os._exit(0 if result.returncode == 0 else 1)
            try:
                exchanges = (
                    (b"Enable Feishu?", b"n\n"),
                    (b"Enable Telegram?", b"n\n"),
                    (b"Enable Discord?", b"n\n"),
                    (b"Model API key:", b"model-secret\n"),
                )
                next_exchange = 0
                deadline = time.monotonic() + 15.0
                status: int | None = None
                transcript = bytearray()
                while time.monotonic() < deadline:
                    waited, candidate = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        status = candidate
                        break
                    ready, _, _ = select.select([descriptor], [], [], 0.05)
                    if ready:
                        try:
                            transcript.extend(os.read(descriptor, 4096))
                        except OSError:
                            pass
                        if (
                            next_exchange < len(exchanges)
                            and exchanges[next_exchange][0] in transcript
                        ):
                            os.write(descriptor, exchanges[next_exchange][1])
                            next_exchange += 1
                if status is None:
                    os.kill(pid, 9)
                    _, status = os.waitpid(pid, 0)
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(
                    os.WEXITSTATUS(status),
                    0,
                    transcript.decode("utf-8", errors="replace"),
                )
                self.assertTrue((state / "config.toml").is_file())
                self.assertTrue((state / "secrets.env").is_file())
            finally:
                os.close(descriptor)

    def test_noninteractive_init_keeps_closed_stdin_runner(self) -> None:
        """init 不能误用继承 controlling TTY 的交互 runner。"""
        operations, layout, plan = self._setup_case(run_onboarding=False)
        with mock.patch("lobster0.install.orchestrator._checked_owner_command") as checked:
            operations.setup(
                plan,  # type: ignore[arg-type]
                layout,  # type: ignore[arg-type]
                mock.sentinel.receipt,  # type: ignore[arg-type]
            )
        self.assertIs(checked.call_args.args[0], mock.sentinel.closed_stdin_runner)
        self.assertIn("init", checked.call_args.args[1])

    def test_interactive_setup_uses_controlling_tty_runner(self) -> None:
        """setup 必须显式选择保留 controlling TTY 的 runner。"""
        operations, layout, plan = self._setup_case(run_onboarding=True)
        with mock.patch("lobster0.install.orchestrator._checked_owner_command") as checked:
            operations.setup(
                plan,  # type: ignore[arg-type]
                layout,  # type: ignore[arg-type]
                mock.sentinel.receipt,  # type: ignore[arg-type]
            )
        self.assertIs(checked.call_args.args[0], mock.sentinel.tty_runner)
        self.assertIn("setup", checked.call_args.args[1])

    def test_runner_interrupt_kills_descendant_process_group(self) -> None:
        """父进程异步中断也必须清理 setup process group 与 pipe。"""
        class Stream:
            """提供 selector 注册所需的最小 pipe 视图。"""

            def __init__(self, descriptor: int) -> None:
                """保存固定 descriptor 与关闭状态。"""
                self.descriptor = descriptor
                self.closed = False

            def fileno(self) -> int:
                """返回 fixed pipe descriptor。"""
                return self.descriptor

            def close(self) -> None:
                """记录 pipe 已关闭。"""
                self.closed = True

        class Selector:
            """在首次等待时注入异步中断。"""

            def register(self, *_args: object) -> None:
                """接受两个 pipe 的注册。"""

            def get_map(self) -> dict[int, int]:
                """保持循环进入 selector 等待。"""
                return {1: 1}

            def select(self, _timeout: float) -> list[object]:
                """模拟调用方收到 KeyboardInterrupt。"""
                raise KeyboardInterrupt

            def close(self) -> None:
                """关闭 fake selector。"""

        stdout = Stream(10)
        stderr = Stream(11)
        process = SimpleNamespace(
            pid=1234,
            stdout=stdout,
            stderr=stderr,
            wait=mock.Mock(return_value=0),
        )
        with (
            mock.patch("lobster0.install.orchestrator.os.open", return_value=9),
            mock.patch("lobster0.install.orchestrator.os.close") as close,
            mock.patch("lobster0.install.orchestrator.os.tcgetpgrp", return_value=77),
            mock.patch(
                "lobster0.install.orchestrator.os.set_blocking",
                side_effect=KeyboardInterrupt,
            ),
            mock.patch("lobster0.install.orchestrator.os.killpg") as killpg,
            mock.patch(
                "lobster0.install.orchestrator.subprocess.Popen",
                return_value=process,
            ),
            mock.patch(
                "lobster0.install.orchestrator.selectors.DefaultSelector",
                return_value=Selector(),
            ),
            mock.patch("lobster0.install.orchestrator._set_foreground_process_group") as fg,
            self.assertRaises(KeyboardInterrupt),
        ):
            install_orchestrator._InteractiveSubprocessRunner().run(
                ("/runtime/bin/lobster0", "setup"),
                env={"HOME": "/home/alice", "PATH": "/usr/bin:/bin"},
                timeout=10.0,
            )
        killpg.assert_called_once_with(1234, 9)
        process.wait.assert_called_once_with()
        self.assertTrue(stdout.closed)
        self.assertTrue(stderr.closed)
        self.assertEqual(fg.call_args_list, [mock.call(9, 1234), mock.call(9, 77)])
        close.assert_called_once_with(9)


class _SchemaRunner:
    """用真实 strict config parser 模拟 staged Runtime validation。"""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        """解析 argv 末尾的 candidate state root 并返回有界结果。"""
        del env, timeout
        try:
            load_config(build_state_paths(Path(argv[-1])), {}, {})
        except ConfigError:
            return CommandResult(1, b"", b"")
        return CommandResult(0, b"", b"")


class _OwnerHelperRunner:
    """以当前测试用户真实执行 system-prefix staged helper body。"""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        """跳过 sudo/env wrapper，只执行同一 Python -I helper argv。"""
        isolated = argv.index("-I")
        completed = subprocess.run(
            argv[isolated - 1 :],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class ImportSafetyTests(unittest.TestCase):
    """覆盖 config 与 Secret 导入的 no-follow、schema 和权限边界。"""

    def setUp(self) -> None:
        """创建 current-owner private source、scratch 与 state roots。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.layout = SimpleNamespace(
            state_home=self.state,
            secrets_file=self.state / "secrets.env",
        )

    def test_import_sources_reject_symlink_wrong_owner_and_secret_mode(self) -> None:
        """symlink/非 owner config 与非 0600 Secret 都不得被 Runtime 消费。"""
        config = self.root / "config.toml"
        config.write_text("", encoding="utf-8")
        config.chmod(0o600)
        linked = self.root / "linked.toml"
        linked.symlink_to(config)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            _validate_and_import_config(
                linked,
                self.layout,  # type: ignore[arg-type]
                Path("/runtime/python"),
                self.scratch,
                mock.sentinel.runner,  # type: ignore[arg-type]
                False,
            )
        foreign_uid = config.stat().st_uid + 1
        foreign = pwd.struct_passwd(
            ("foreign", "x", foreign_uid, foreign_uid, "Foreign", str(self.root), "/bin/sh")
        )
        with (
            mock.patch(
                "lobster0.install.orchestrator._invoking_account",
                return_value=foreign,
            ),
            self.assertRaisesRegex(InstallError, "request_invalid"),
        ):
            _validate_and_import_config(
                config,
                self.layout,  # type: ignore[arg-type]
                Path("/runtime/python"),
                self.scratch,
                mock.sentinel.runner,  # type: ignore[arg-type]
                False,
            )

        secrets = self.root / "secrets.env"
        secrets.write_text("LOBSTER0_MODEL_API_KEY=value\n", encoding="utf-8")
        secrets.chmod(0o644)
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            _import_secrets(
                secrets,
                self.layout,  # type: ignore[arg-type]
                Path("/runtime/python"),
                mock.sentinel.runner,  # type: ignore[arg-type]
                system_prefix=False,
            )
        secrets.write_text('LOBSTER0_MODEL_API_KEY="SECRET_SENTINEL"\n', encoding="utf-8")
        secrets.chmod(0o600)
        with self.assertRaisesRegex(InstallError, "doctor_blocked"):
            _import_secrets(
                secrets,
                self.layout,  # type: ignore[arg-type]
                Path("/runtime/python"),
                mock.sentinel.runner,  # type: ignore[arg-type]
                system_prefix=False,
            )
        self.assertFalse(self.state.exists())

    def test_invalid_config_schema_is_rejected_before_copy(self) -> None:
        """staged strict parser 拒绝未知配置字段时不得创建目标 config。"""
        source = self.root / "invalid.toml"
        source.write_text('[provider]\napi_key="SECRET_SENTINEL"\n', encoding="utf-8")
        source.chmod(0o600)
        with self.assertRaisesRegex(InstallError, "doctor_blocked"):
            _validate_and_import_config(
                source,
                self.layout,  # type: ignore[arg-type]
                Path(".venv/bin/python").resolve(),
                self.scratch,
                _SchemaRunner(),  # type: ignore[arg-type]
                False,
            )
        self.assertFalse((self.state / "config.toml").exists())

    def test_valid_imports_are_copied_owner_only_without_value_output(self) -> None:
        """通过 schema 的 config/Secret 只以 0600 落盘且不返回 Secret 值。"""
        config = self.root / "valid.toml"
        config.write_text("[channels.telegram]\nenabled=false\n", encoding="utf-8")
        config.chmod(0o600)
        secrets = self.root / "valid.env"
        secrets.write_text("LOBSTER0_MODEL_API_KEY=SECRET_SENTINEL\n", encoding="utf-8")
        secrets.chmod(0o600)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            _validate_and_import_config(
                config,
                self.layout,  # type: ignore[arg-type]
                Path(".venv/bin/python").resolve(),
                self.scratch,
                _SchemaRunner(),  # type: ignore[arg-type]
                False,
            )
            _import_secrets(
                secrets,
                self.layout,  # type: ignore[arg-type]
                Path(".venv/bin/python").resolve(),
                _SchemaRunner(),  # type: ignore[arg-type]
                system_prefix=False,
            )
        self.assertEqual((self.state / "config.toml").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "secrets.env").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("SECRET_SENTINEL", stdout.getvalue() + stderr.getvalue())

    def test_system_prefix_secret_helper_rejects_duplicate_and_quoted_values(self) -> None:
        """target-user helper 必须复用 normal strict dotenv schema 后才可落盘。"""
        source = self.root / "system.env"
        account = pwd.struct_passwd(
            (
                "alice",
                "x",
                os.geteuid(),
                os.getegid(),
                "Alice",
                str(self.root),
                "/bin/sh",
            )
        )
        for index, payload in enumerate(
            (
                "LOBSTER0_MODEL_API_KEY=first\nLOBSTER0_MODEL_API_KEY=second\n",
                'LOBSTER0_MODEL_API_KEY="quoted-secret"\n',
            )
        ):
            with self.subTest(index=index):
                source.write_text(payload, encoding="utf-8")
                source.chmod(0o600)
                state = self.root / f"system-state-{index}"
                layout = SimpleNamespace(
                    state_home=state,
                    secrets_file=state / "secrets.env",
                )
                with (
                    mock.patch(
                        "lobster0.install.orchestrator._invoking_account",
                        return_value=account,
                    ),
                    self.assertRaisesRegex(InstallError, "doctor_blocked"),
                ):
                    _import_secrets(
                        source,
                        layout,  # type: ignore[arg-type]
                        Path(sys.executable),
                        _OwnerHelperRunner(),  # type: ignore[arg-type]
                        system_prefix=True,
                    )
                self.assertFalse(layout.secrets_file.exists())

    def test_system_prefix_config_helper_rejects_credentials_before_copy(self) -> None:
        """target-user config helper 必须在 credential-bearing schema 落盘前失败。"""
        source = self.root / "system.toml"
        source.write_text('[provider]\napi_key="SECRET_SENTINEL"\n', encoding="utf-8")
        source.chmod(0o600)
        account = pwd.struct_passwd(
            (
                "alice",
                "x",
                os.geteuid(),
                os.getegid(),
                "Alice",
                str(self.root),
                "/bin/sh",
            )
        )
        with (
            mock.patch(
                "lobster0.install.orchestrator._invoking_account",
                return_value=account,
            ),
            self.assertRaisesRegex(InstallError, "doctor_blocked"),
        ):
            _validate_and_import_config(
                source,
                self.layout,  # type: ignore[arg-type]
                Path(sys.executable),
                self.scratch,
                _OwnerHelperRunner(),  # type: ignore[arg-type]
                True,
            )
        self.assertFalse((self.state / "config.toml").exists())


class ServiceReadinessTests(unittest.TestCase):
    """覆盖 enabled Channel 与 owner-only Secret names 的完整性门禁。"""

    def setUp(self) -> None:
        """创建 private config/Secret 文件。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.config = self.root / "config.toml"
        self.secrets = self.root / "secrets.env"

    def _write(self, config: str, secrets: str) -> None:
        """写入 0600 service readiness fixtures。"""
        self.config.write_text(config, encoding="utf-8")
        self.secrets.write_text(secrets, encoding="utf-8")
        self.config.chmod(0o600)
        self.secrets.chmod(0o600)

    def test_enabled_channel_requires_model_and_channel_secret_names(self) -> None:
        """Telegram enabled 时模型 key 与 bot token name 缺一不可。"""
        self._write(
            "[channels.telegram]\nenabled=true\n"
            'bot_token_env="LOBSTER0_TELEGRAM_BOT_TOKEN"\n',
            "LOBSTER0_MODEL_API_KEY=model\nLOBSTER0_TELEGRAM_BOT_TOKEN=token\n",
        )
        self.assertTrue(_service_inputs_complete(self.config, self.secrets))
        self.secrets.write_text("LOBSTER0_MODEL_API_KEY=model\n", encoding="utf-8")
        self.secrets.chmod(0o600)
        self.assertFalse(_service_inputs_complete(self.config, self.secrets))

    def test_zero_enabled_channel_never_creates_service(self) -> None:
        """仅有模型 Secret、没有 enabled Channel 时 readiness 为 false。"""
        self._write("[channels.telegram]\nenabled=false\n", "LOBSTER0_MODEL_API_KEY=model\n")
        self.assertFalse(_service_inputs_complete(self.config, self.secrets))

    def test_provider_custom_api_key_environment_name_is_required(self) -> None:
        """readiness 必须采用 strict config 的 provider.api_key_env。"""
        self._write(
            "[provider]\napi_key_env=\"CUSTOM_MODEL_KEY\"\n"
            "[channels.telegram]\nenabled=true\n"
            'bot_token_env="LOBSTER0_TELEGRAM_BOT_TOKEN"\n',
            "CUSTOM_MODEL_KEY=model\nLOBSTER0_TELEGRAM_BOT_TOKEN=token\n",
        )
        self.assertTrue(_service_inputs_complete(self.config, self.secrets))

    def test_system_prefix_readiness_is_computed_only_by_target_user(self) -> None:
        """UID 0 父进程不得打开 config 或 Secret，只接收 bounded readiness。"""
        class Runner:
            """记录 fixed sudo helper 并返回单个 readiness token。"""

            def __init__(self) -> None:
                """初始化调用记录。"""
                self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

            def run(
                self,
                argv: tuple[str, ...],
                *,
                env: dict[str, str],
                timeout: float,
            ) -> CommandResult:
                """不读取不存在的输入路径，返回 bounded ready token。"""
                del timeout
                self.calls.append((argv, env))
                return CommandResult(0, b"ready\n", b"")

        runner = Runner()
        account = pwd.struct_passwd(("alice", "x", 1001, 1001, "Alice", "/home/alice", "/bin/sh"))
        with mock.patch(
            "lobster0.install.orchestrator._invoking_account",
            return_value=account,
        ):
            ready = _service_inputs_complete_as_owner(
                Path("/missing/config.toml"),
                Path("/missing/secrets.env"),
                Path("/runtime/bin/python"),
                runner,  # type: ignore[arg-type]
            )
        self.assertTrue(ready)
        argv, environment = runner.calls[0]
        self.assertEqual(
            argv[:7],
            ("/usr/bin/sudo", "-u", "alice", "--", "/usr/bin/env", "-i", "HOME=/home/alice"),
        )
        self.assertEqual(
            environment,
            {
                "HOME": "/home/alice",
                "LOGNAME": "alice",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "USER": "alice",
                "XDG_RUNTIME_DIR": "/run/user/1001",
            },
        )

    def test_system_prefix_config_import_does_not_read_source_as_root(self) -> None:
        """system-prefix config source 只能由 target-user helper 打开。"""
        account = pwd.struct_passwd(("alice", "x", 1001, 1001, "Alice", "/home/alice", "/bin/sh"))
        with (
            mock.patch(
                "lobster0.install.orchestrator._invoking_account",
                return_value=account,
            ),
            mock.patch(
                "lobster0.install.orchestrator._read_import_file",
                side_effect=AssertionError("root opened config"),
            ),
            mock.patch("lobster0.install.orchestrator._import_config_as_owner") as imported,
        ):
            _validate_and_import_config(
                Path("/source/config.toml"),
                mock.sentinel.layout,  # type: ignore[arg-type]
                Path("/runtime/bin/python"),
                Path("/bootstrap"),
                mock.sentinel.runner,  # type: ignore[arg-type]
                True,
            )
        imported.assert_called_once()


if __name__ == "__main__":
    unittest.main()
