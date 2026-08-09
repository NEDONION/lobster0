"""验证 Tier 1 平台检测、Runtime pin 与显式权限计划。"""

import hashlib
import inspect
import json
import os
import pwd
import runpy
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from miniclaw.install import platforms as platforms_module
from miniclaw.install.models import InstallError, InstallRequest, PlatformKey, ReleaseManifest
from miniclaw.install.platforms import (
    DependencyPlan,
    DetectedPlatform,
    LocalPlatformProbe,
    ManualRerunInstruction,
    PrivilegeAction,
    _build_dependency_actions_with_probe,
    _verify_activation_ready_with_probe,
    detect_linux,
    detect_macos,
    detect_platform,
    node_version_supported,
    verify_activation_ready,
)
from miniclaw.install.platforms import (
    _verify_privilege_action_with_probe as verify_privilege_action,
)
from miniclaw.install.platforms import (
    build_dependency_actions as production_build_dependency_actions,
)
from miniclaw.install.platforms import (
    verify_privilege_action as production_verify_privilege_action,
)


def _account(name: str = "alice", uid: int = 1001) -> pwd.struct_passwd:
    """返回完整的 passwd fake 记录。"""
    return pwd.struct_passwd((name, "x", uid, uid, "Fixture", f"/home/{name}", "/bin/sh"))


def _file_fact(mode: int, uid: int = 0, gid: int = 0) -> SimpleNamespace:
    """返回 no-follow lstat 所需的最小文件事实。"""
    return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)


def build_dependency_plan(*args: object, **kwargs: object) -> DependencyPlan:
    """调用 private offline seam 并保留 manual instruction。"""
    return _build_dependency_actions_with_probe(*args, **kwargs)  # type: ignore[arg-type]


def build_dependency_actions(*args: object, **kwargs: object) -> tuple[PrivilegeAction, ...]:
    """为既有 action 断言只投影可执行 capability 集合。"""
    return build_dependency_plan(*args, **kwargs).actions


class _BackendProbe:
    """显式离线 backend probe；成功返回 None，失败抛稳定错误。"""

    def __init__(
        self,
        *,
        ready: bool,
        files: dict[Path, SimpleNamespace] | None = None,
    ) -> None:
        """保存 backend 结论和只允许固定 path 的 lstat facts。"""
        self.ready = ready
        self.files = {} if files is None else files
        self.required: list[tuple[str, str, int]] = []

    def require_backend(self, platform: DetectedPlatform, account: pwd.struct_passwd) -> None:
        """记录完整 identity；没有 containment 证据时抛稳定错误。"""
        self.required.append((platform.sandbox_backend, account.pw_name, account.pw_uid))
        if not self.ready:
            raise InstallError("system_dependency_missing", "platform")

    def lstat(self, path: Path) -> SimpleNamespace:
        """只返回测试显式提供的 no-follow 文件事实。"""
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None


class InstallPlatformsTest(unittest.TestCase):
    """覆盖所有受支持平台与 fail-closed 主机事实。"""

    runtime_versions = Path("release/runtime-versions.json")

    def setUp(self) -> None:
        """创建 manifest-bound installer 与 sandbox-image artifact fixtures。"""
        self.installer_directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.installer_directory.cleanup)
        self.installer = Path(self.installer_directory.name) / "miniclaw-installer.pyz"
        body = b"verified installer fixture\n"
        self.installer.write_bytes(body)
        self.installer.chmod(0o755)
        self.sandbox_artifact = (
            Path(self.installer_directory.name) / "miniclaw-sandbox-image-digest.txt"
        )
        self.sandbox_artifact.write_bytes(b"example/miniclaw@sha256:" + b"a" * 64 + b"\n")
        self.manifest = self.release_manifest()

    def release_manifest(self, version: str = "0.7.0") -> ReleaseManifest:
        """按当前 fixture bytes 构造 Task4 strict ReleaseManifest。"""
        artifacts = []
        for kind, path, media_type in (
            ("installer", self.installer, "application/zip"),
            ("sandbox-image", self.sandbox_artifact, "text/plain"),
        ):
            body = path.read_bytes()
            artifacts.append(
                {
                    "kind": kind,
                    "filename": path.name,
                    "url": (
                        "https://github.com/NEDONION/miniclaw/releases/download/"
                        f"v{version}/{path.name}"
                    ),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body),
                    "media_type": media_type,
                    "platform": {"os": "any", "arch": "any"},
                    "component_version": version,
                    "source_repository": "https://github.com/NEDONION/miniclaw",
                    "license_ref": "MIT",
                    "upstream_sha256": None,
                }
            )
        document = {
            "schema_version": 1,
            "product": "miniclaw",
            "version": version,
            "git_commit": "0" * 40,
            "python": "3.12",
            "node": {
                "default": "24.18.0",
                "accepted": [
                    {"minimum": "22.22.3", "maximum_exclusive": "23.0.0"},
                    {"minimum": "24.15.0", "maximum_exclusive": "25.0.0"},
                ],
            },
            "artifacts": artifacts,
            "supported_platforms": [
                {"os": "linux", "arch": "x86_64"},
                {"os": "linux", "arch": "arm64"},
                {"os": "macos", "arch": "x86_64"},
                {"os": "macos", "arch": "arm64"},
            ],
            "features": [],
            "database_schema": 5,
            "minimum_readable_schema": 5,
        }
        return ReleaseManifest.from_bytes(json.dumps(document).encode("utf-8"))

    @staticmethod
    def os_release(distro: str, version: str) -> str:
        """返回不需要 shell 执行的最小 os-release 文本。"""
        return f'NAME="Fixture"\nID={distro}\nVERSION_ID="{version}"\n'

    @staticmethod
    def request(**changes: object) -> InstallRequest:
        """构造供顶层检测器消费的有效安装请求。"""
        values: dict[str, object] = {
            "action": "install",
            "version": "0.7.0",
            "channel": "stable",
            "prefix": Path("/opt/miniclaw"),
            "state_home": Path("/var/lib/miniclaw"),
            "system_prefix": False,
            "onboard": True,
            "config_file": None,
            "secrets_file": None,
            "service": False,
            "allow_system_packages": False,
            "dry_run": True,
            "json_output": False,
            "verbose": False,
            "purge_data": False,
            "confirm_data_loss": False,
        }
        values.update(changes)
        return InstallRequest(**values)  # type: ignore[arg-type]

    def test_every_tier1_id_maps_to_one_release_platform(self) -> None:
        """Tier 1 Linux ID/version 与两种架构应精确映射到 Release key。"""
        for distro, version in (
            ("ubuntu", "22.04"),
            ("ubuntu", "24.04"),
            ("debian", "12"),
            ("debian", "13"),
            ("rhel", "9.6"),
            ("rocky", "10.0"),
            ("almalinux", "9.5"),
        ):
            for machine, arch in (("x86_64", "x86_64"), ("aarch64", "arm64")):
                with self.subTest(distro=distro, version=version, machine=machine):
                    detected = detect_linux(self.os_release(distro, version), machine)
                    self.assertEqual(detected.artifact_platform, PlatformKey("linux", arch))
                    expected = (
                        "docker-rootless" if distro in {"ubuntu", "debian"} else "podman-rootless"
                    )
                    self.assertEqual(detected.sandbox_backend, expected)

    def test_macos_13_and_newer_support_both_release_architectures(self) -> None:
        """macOS 13+ 的 Intel/Apple Silicon 都应选择内建 Seatbelt。"""
        for version in ("13.0", "14.7.1", "26.0"):
            for machine, arch in (("amd64", "x86_64"), ("arm64", "arm64")):
                with self.subTest(version=version, machine=machine):
                    detected = detect_macos(version, machine)
                    self.assertEqual(detected.artifact_platform, PlatformKey("macos", arch))
                    self.assertEqual(detected.service_manager, "launchd")
                    self.assertEqual(detected.sandbox_backend, "seatbelt")
        with self.assertRaisesRegex(InstallError, "unsupported_platform"):
            detect_macos("12.7.6", "arm64")

    def test_node_policy_rejects_eol_odd_and_unvalidated_major(self) -> None:
        """只复用完成验证的 Node 22/24 LTS 范围。"""
        accepted = ((22, 22, 3), (22, 99, 0), (24, 15, 0), (24, 18, 0))
        rejected = (
            (20, 99, 0),
            (22, 22, 2),
            (23, 9, 0),
            (24, 14, 9),
            (25, 1, 0),
            (26, 0, 0),
        )
        self.assertTrue(all(node_version_supported(version) for version in accepted))
        self.assertTrue(all(not node_version_supported(version) for version in rejected))
        for malformed in (True, (24, True, 0), (24, 18), "24.18.0", (24, 18, -1)):
            with self.subTest(malformed=malformed):
                self.assertFalse(node_version_supported(malformed))  # type: ignore[arg-type]

    def test_public_models_reject_cross_field_and_privilege_forgery(self) -> None:
        """公开 dataclass 不能构造错配 backend、shell argv 或预批准动作。"""
        with self.assertRaisesRegex(InstallError, "unsupported_platform"):
            DetectedPlatform(
                os="linux",
                distro_id="ubuntu",
                distro_version="24.04",
                arch="x86_64",
                service_manager="systemd-user",
                artifact_platform=PlatformKey("linux", "x86_64"),
                sandbox_backend="seatbelt",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            PrivilegeAction(
                category="system-package",
                argv=("/bin/sh", "-c", "touch /tmp/owned"),
                requires_sudo=False,
                reason="injected",
            )
        with self.assertRaises((TypeError, InstallError)):
            PrivilegeAction(
                category="linger",
                argv=("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger", "alice"),
                requires_sudo=True,
                reason="preapproved",
                approved=True,
            )
        invalid_models = (
            lambda: DetectedPlatform(
                os="linux",
                distro_id=[],  # type: ignore[arg-type]
                distro_version="24.04",
                arch="x86_64",
                service_manager="systemd-user",
                artifact_platform=PlatformKey("linux", "x86_64"),
                sandbox_backend="docker-rootless",
            ),
            lambda: PrivilegeAction(
                category="system-prefix",
                argv=(
                    "/usr/bin/sudo",
                    "/usr/bin/install",
                    "-d",
                    "-m",
                    "0755",
                    "/usr/local/lib/miniclaw",
                ),
                requires_sudo=True,
                reason="old direct write",
            ),
            lambda: PrivilegeAction(
                category="system-prefix",
                argv=(
                    "/usr/bin/sudo",
                    "--",
                    "/bin/sh",
                    "install",
                    "--system-prefix",
                ),
                requires_sudo=True,
                reason="shell rerun",
            ),
        )
        for create in invalid_models:
            with (
                self.subTest(create=create),
                self.assertRaisesRegex(
                    InstallError, "system_dependency_missing|unsupported_platform"
                ),
            ):
                create()

        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            PrivilegeAction(
                category="system-package",
                argv=("/usr/bin/sudo", "/usr/bin/apt-get", "update"),
                requires_sudo=True,
                reason="printable but attacker-controlled reason",
            )

    def test_public_detectors_require_a_real_nonroot_account(self) -> None:
        """public Linux/macOS detector 不得接受不存在的非 root UID。"""
        missing_uid = 424_242
        missing_lookup = mock.Mock(side_effect=KeyError(missing_uid))
        for detect in (
            lambda: detect_linux(
                self.os_release("ubuntu", "24.04"),
                "x86_64",
                effective_uid=missing_uid,
                getpwuid=missing_lookup,
            ),
            lambda: detect_macos(
                "15.0",
                "arm64",
                effective_uid=missing_uid,
                getpwuid=missing_lookup,
            ),
        ):
            with (
                self.subTest(detect=detect),
                self.assertRaisesRegex(InstallError, "privilege_denied"),
            ):
                detect()

    def test_parser_and_sudo_uid_normalize_hostile_scalar_errors(self) -> None:
        """surrogate 与超长 SUDO_UID 只返回稳定 InstallError。"""
        with self.assertRaisesRegex(InstallError, "unsupported_platform"):
            detect_linux("ID=ubuntu\nVERSION_ID=24.04\n#\ud800", "x86_64")

        current = pwd.getpwuid(os.geteuid())
        with (
            mock.patch.dict(
                os.environ,
                {"SUDO_USER": current.pw_name, "SUDO_UID": "9" * 5_000},
                clear=True,
            ),
            self.assertRaisesRegex(InstallError, "privilege_denied"),
        ):
            detect_platform(
                self.request(),
                system="Darwin",
                machine="arm64",
                macos_version="15.0",
                effective_uid=0,
            )

    def test_root_identity_requires_lookup_name_and_uid_to_match(self) -> None:
        """getpwnam 返回另一用户名时不能绑定 root handoff。"""
        with self.assertRaisesRegex(InstallError, "privilege_denied"):
            detect_linux(
                self.os_release("ubuntu", "24.04"),
                "x86_64",
                effective_uid=0,
                original_user="alice",
                original_uid=1001,
                getpwnam=lambda name: _account(name="mallory"),
            )

    def test_unsupported_linux_matrix_fails_closed(self) -> None:
        """旧版、未来未验证版、未知发行版、musl、WSL 与 32 位一律拒绝。"""
        cases = (
            (self.os_release("ubuntu", "20.04"), "x86_64", {}),
            (self.os_release("ubuntu", "26.04"), "x86_64", {}),
            (self.os_release("debian", "11"), "x86_64", {}),
            (self.os_release("rhel", "8.10"), "x86_64", {}),
            (self.os_release("nixos", "26.05"), "x86_64", {}),
            (self.os_release("ubuntu", "24.04"), "i686", {}),
            (self.os_release("ubuntu", "24.04"), "x86_64", {"libc": "musl"}),
            (self.os_release("ubuntu", "24.04"), "x86_64", {"wsl": True}),
        )
        for text, machine, facts in cases:
            with (
                self.subTest(text=text, machine=machine, facts=facts),
                self.assertRaisesRegex(InstallError, "unsupported_platform"),
            ):
                detect_linux(text, machine, **facts)

    def test_detection_rejects_malformed_and_executable_os_release_text(self) -> None:
        """os-release 只解析静态 ID/VERSION_ID，拒绝重复键与 shell 表达式。"""
        cases = (
            "ID=ubuntu\nID=debian\nVERSION_ID=24.04\n",
            "ID=$(touch /tmp/owned)\nVERSION_ID=24.04\n",
            "ID=ubuntu;touch-owned\nVERSION_ID=24.04\n",
            "ID=ubuntu\nVERSION_ID='24.04\n",
            "ID=ubuntu\n",
        )
        for text in cases:
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(InstallError, "unsupported_platform"),
            ):
                detect_linux(text, "x86_64")

    def test_os_release_ignores_unconsumed_static_and_shell_like_values(self) -> None:
        """真实 os-release 的空格、URL 与未消费 shell 文本不得被求值或误拒绝。"""
        text = (
            'PRETTY_NAME="Ubuntu 24.04.2 LTS"\n'
            'HOME_URL="https://www.ubuntu.com/"\n'
            'UNCONSUMED="$(touch /tmp/never-executed)"\n'
            "ID=ubuntu\n"
            'VERSION_ID="24.04"\n'
        )
        detected = detect_linux(text, "x86_64")
        self.assertEqual(detected.distro_id, "ubuntu")
        self.assertFalse(Path("/tmp/never-executed").exists())

    def test_service_and_root_facts_fail_closed(self) -> None:
        """非 systemd service 与无真实原用户的 root 调用不可继续。"""
        linux = self.os_release("ubuntu", "24.04")
        with self.assertRaisesRegex(InstallError, "unsupported_platform"):
            detect_linux(linux, "x86_64", service_requested=True, service_manager="openrc")
        for facts in (
            {"effective_uid": 0},
            {"effective_uid": 0, "original_user": "root", "original_uid": 0},
            {"effective_uid": 0, "original_user": "bad user", "original_uid": 1000},
            {"effective_uid": 0, "original_user": "alice", "original_uid": True},
        ):
            with (
                self.subTest(facts=facts),
                self.assertRaisesRegex(InstallError, "privilege_denied"),
            ):
                detect_linux(linux, "x86_64", **facts)

    def test_top_level_detection_consumes_request_without_host_writes(self) -> None:
        """顶层 adapter 应从请求派生 service 约束并返回同一不可变平台值。"""
        detected = detect_platform(
            self.request(service=True),
            system="Linux",
            machine="aarch64",
            os_release_text=self.os_release("debian", "13"),
            libc="glibc",
            wsl=False,
            service_manager="systemd-user",
            effective_uid=1000,
            getpwuid=lambda uid: _account(uid=uid),
        )
        self.assertEqual(detected.artifact_platform, PlatformKey("linux", "arm64"))
        with self.assertRaisesRegex(InstallError, "unsupported_platform"):
            detect_platform(
                self.request(service=True),
                system="Linux",
                machine="x86_64",
                os_release_text=self.os_release("debian", "13"),
                service_manager="openrc",
                effective_uid=1000,
                getpwuid=lambda uid: _account(uid=uid),
            )

    def test_dependency_dry_run_is_closed_world_exact_argv(self) -> None:
        """系统依赖只产生固定 argv，sudo 动作必须等待单独批准。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        probe = _BackendProbe(
            ready=False,
            files={
                Path("/usr/bin/dockerd-rootless-setuptool.sh"): _file_fact(stat.S_IFREG | 0o755)
            },
        )
        plan = build_dependency_plan(
            ubuntu,
            self.request(
                allow_system_packages=True,
                service=True,
                system_prefix=True,
                prefix=None,
            ),
            probe=probe,
            manifest=self.manifest,
            installer_artifact_path=self.installer,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
        )
        self.assertEqual(
            tuple(action.argv for action in plan.actions),
            (
                ("/usr/bin/sudo", "/usr/bin/apt-get", "update"),
                (
                    "/usr/bin/sudo",
                    "/usr/bin/apt-get",
                    "install",
                    "-y",
                    "docker.io",
                    "rootlesskit",
                    "uidmap",
                    "dbus-user-session",
                    "slirp4netns",
                    "fuse-overlayfs",
                ),
                ("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger", "alice"),
            ),
        )
        self.assertEqual(
            plan.manual_rerun.argv if plan.manual_rerun else None,
            (
                "/usr/bin/sudo",
                "--",
                str(self.installer),
                "install",
                "--version",
                "0.7.0",
                "--channel",
                "stable",
                "--state-home",
                "/var/lib/miniclaw",
                "--system-prefix",
                "--install-service",
                "--allow-system-packages",
                "--dry-run",
            ),
        )
        self.assertTrue(all(isinstance(action, PrivilegeAction) for action in plan.actions))
        self.assertTrue(all(not hasattr(action, "approved") for action in plan.actions))
        self.assertTrue(
            all(action.requires_sudo for action in plan.actions if action.argv[0].endswith("sudo"))
        )
        rendered = json.dumps(
            [list(action.argv) for action in plan.actions]
            + ([list(plan.manual_rerun.argv)] if plan.manual_rerun else [])
        )
        self.assertNotIn(";", rendered)
        self.assertNotIn("docker group", rendered)
        self.assertNotIn("/var/run/docker.sock", rendered)
        self.assertEqual(probe.required, [("docker-rootless", "alice", 1001)])

    def test_root_setup_and_linger_bind_one_validated_original_user(self) -> None:
        """root 的 setup、linger 与 backend probe 必须共享真实 SUDO identity。"""
        ubuntu = detect_linux(
            self.os_release("ubuntu", "24.04"),
            "x86_64",
            effective_uid=0,
            original_user="alice",
            original_uid=1001,
            getpwnam=lambda name: _account(name=name),
        )
        probe = _BackendProbe(
            ready=False,
            files={
                Path("/usr/bin/dockerd-rootless-setuptool.sh"): _file_fact(stat.S_IFREG | 0o755)
            },
        )
        actions = build_dependency_actions(
            ubuntu,
            self.request(
                allow_system_packages=True,
                service=True,
            ),
            probe=probe,
            effective_uid=0,
            original_user="alice",
            original_uid=1001,
            getpwnam=lambda name: _account(name=name),
        )
        install_packages = next(
            action
            for action in actions
            if "/usr/bin/apt-get" in action.argv and "install" in action.argv
        )
        setup_actions = verify_privilege_action(
            install_packages,
            ubuntu,
            self.request(allow_system_packages=True, service=True),
            probe=probe,
            effective_uid=0,
            original_user="alice",
            original_uid=1001,
            getpwnam=lambda name: _account(name=name),
            after_execution=True,
        )
        self.assertIsInstance(setup_actions, tuple)
        self.assertIn(
            (
                "/usr/bin/sudo",
                "-u",
                "alice",
                "--",
                "/usr/bin/dockerd-rootless-setuptool.sh",
                "install",
            ),
            tuple(action.argv for action in setup_actions),
        )
        self.assertIn(
            ("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger", "alice"),
            tuple(action.argv for action in actions),
        )
        self.assertFalse(any(action.category == "system-prefix" for action in actions))
        self.assertEqual(
            probe.required,
            [
                ("docker-rootless", "alice", 1001),
                ("docker-rootless", "alice", 1001),
            ],
        )

    def test_rhel_dependency_plan_uses_only_rootless_podman_compatibility(self) -> None:
        """RHEL family 只计划固定 podman-docker 包集，绝不启动 root Docker。"""
        rhel = detect_linux(self.os_release("rocky", "10.0"), "arm64")
        probe = _BackendProbe(ready=False)
        actions = build_dependency_actions(
            rhel,
            self.request(allow_system_packages=True),
            probe=probe,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
        )
        self.assertEqual(
            tuple(action.argv for action in actions),
            (
                (
                    "/usr/bin/sudo",
                    "/usr/bin/dnf",
                    "install",
                    "-y",
                    "podman-docker",
                    "slirp4netns",
                    "fuse-overlayfs",
                    "shadow-utils",
                    "dbus-daemon",
                ),
            ),
        )
        self.assertEqual(rhel.sandbox_backend, "podman-rootless")
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            build_dependency_actions(
                rhel,
                self.request(allow_system_packages=False),
                probe=probe,
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )
        self.assertEqual(
            build_dependency_actions(
                rhel,
                self.request(),
                probe=_BackendProbe(ready=True),
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            ),
            (),
        )

    def test_plain_mapping_and_unapproved_packages_cannot_forge_readiness(self) -> None:
        """普通 Mapping bool 和未授权 system packages 都不能生成 sudo 动作。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        for request, probe in (
            (self.request(), {"backend_ready": True}),
            (self.request(allow_system_packages=False), _BackendProbe(ready=False)),
        ):
            with (
                self.subTest(request=request, probe=probe),
                self.assertRaisesRegex(InstallError, "system_dependency_missing"),
            ):
                build_dependency_actions(
                    ubuntu,
                    request,
                    probe=probe,
                    effective_uid=1001,
                    getpwuid=lambda uid: _account(uid=uid),
                )

        class ExplodingProbe(_BackendProbe):
            """模拟本地 adapter 泄漏动态异常。"""

            def require_backend(
                self,
                platform: DetectedPlatform,
                account: pwd.struct_passwd,
            ) -> None:
                """抛出不应越过 installer boundary 的异常。"""
                raise ValueError("SECRET_DYNAMIC_DETAIL")

        try:
            build_dependency_actions(
                ubuntu,
                self.request(),
                probe=ExplodingProbe(ready=False),
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )
        except InstallError as error:
            self.assertRegex(str(error), "system_dependency_missing")
            self.assertIsNone(error.__cause__)
        else:
            self.fail("dynamic probe failure must be normalized")

    def test_public_dependency_api_rejects_duck_typed_probe(self) -> None:
        """production build 不能让调用方用 no-op fake 伪造 backend readiness。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        with self.assertRaises(TypeError):
            production_build_dependency_actions(
                ubuntu,
                self.request(),
                manifest=self.manifest,
                sandbox_artifact_path=self.sandbox_artifact,
                probe=_BackendProbe(ready=True),
            )
        action = PrivilegeAction(
            category="linger",
            argv=("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger", "alice"),
            requires_sudo=True,
            reason="enable confirmed headless user service",
        )
        with self.assertRaises(TypeError):
            production_verify_privilege_action(
                action,
                ubuntu,
                self.request(service=True),
                manifest=self.manifest,
                sandbox_artifact_path=self.sandbox_artifact,
                probe=_BackendProbe(ready=True),
            )
        with self.assertRaises(TypeError):
            verify_activation_ready(
                ubuntu,
                manifest=self.manifest,
                sandbox_artifact_path=self.sandbox_artifact,
                probe=_BackendProbe(ready=True),
            )

    def test_platform_module_runs_with_install_local_and_stdlib_imports_only(self) -> None:
        """installer zipapp isolation 中不能依赖 policy/sandbox 主包模块。"""
        blocked = {
            "miniclaw.policy.command": None,
            "miniclaw.sandbox.base": None,
            "miniclaw.sandbox.docker": None,
            "miniclaw.sandbox.seatbelt": None,
        }
        with mock.patch.dict("sys.modules", blocked):
            runpy.run_path("src/miniclaw/install/platforms.py", run_name="installer_platforms")

    def test_setup_tool_is_derived_from_no_follow_fixed_path_facts(self) -> None:
        """symlink/非执行文件被跳过，只选择第二个真实 executable regular candidate。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        probe = _BackendProbe(
            ready=False,
            files={
                Path("/usr/bin/dockerd-rootless-setuptool.sh"): _file_fact(stat.S_IFLNK | 0o777),
                Path("/usr/share/docker.io/contrib/dockerd-rootless-setuptool.sh"): (
                    _file_fact(stat.S_IFREG | 0o755)
                ),
            },
        )
        actions = build_dependency_actions(
            ubuntu,
            self.request(allow_system_packages=True),
            probe=probe,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
        )
        install_packages = next(
            action
            for action in actions
            if "/usr/bin/apt-get" in action.argv and "install" in action.argv
        )
        setup_actions = verify_privilege_action(
            install_packages,
            ubuntu,
            self.request(allow_system_packages=True),
            probe=probe,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
            after_execution=True,
        )
        self.assertIsInstance(setup_actions, tuple)
        self.assertIn(
            (
                "/usr/share/docker.io/contrib/dockerd-rootless-setuptool.sh",
                "install",
            ),
            tuple(action.argv for action in setup_actions),
        )
        root_only = _BackendProbe(
            ready=False,
            files={
                Path("/usr/bin/dockerd-rootless-setuptool.sh"): _file_fact(stat.S_IFREG | 0o700)
            },
        )
        restricted = build_dependency_actions(
            ubuntu,
            self.request(allow_system_packages=True),
            probe=root_only,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
        )
        restricted_install = next(
            action
            for action in restricted
            if "/usr/bin/apt-get" in action.argv and "install" in action.argv
        )
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            verify_privilege_action(
                restricted_install,
                ubuntu,
                self.request(allow_system_packages=True),
                probe=root_only,
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
                after_execution=True,
            )

    def test_setup_tool_is_revalidated_before_and_after_execution(self) -> None:
        """plan 后换成 symlink 必须阻断执行，setup 后还需 backend containment 证据。"""
        path = Path("/usr/bin/dockerd-rootless-setuptool.sh")
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        probe = _BackendProbe(
            ready=False,
            files={path: _file_fact(stat.S_IFREG | 0o755)},
        )
        request = self.request(allow_system_packages=True)
        actions = build_dependency_actions(
            ubuntu,
            request,
            probe=probe,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
        )
        install_packages = next(
            action
            for action in actions
            if "/usr/bin/apt-get" in action.argv and "install" in action.argv
        )
        setup_actions = verify_privilege_action(
            install_packages,
            ubuntu,
            request,
            probe=probe,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
            after_execution=True,
        )
        self.assertIsInstance(setup_actions, tuple)
        setup = next(action for action in setup_actions if path.as_posix() in action.argv)
        probe.files[path] = _file_fact(stat.S_IFLNK | 0o777)
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            verify_privilege_action(
                setup,
                ubuntu,
                request,
                probe=probe,
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )
        probe.files[path] = _file_fact(stat.S_IFREG | 0o755)
        probe.ready = True
        verify_privilege_action(
            setup,
            ubuntu,
            request,
            probe=probe,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
            after_execution=True,
        )

    def test_verifier_rebinds_every_action_to_platform_and_request(self) -> None:
        """执行边界不得把包、linger 或 rerun 动作移植到另一平台/请求。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        rocky = detect_linux(self.os_release("rocky", "10.0"), "x86_64")
        probe = _BackendProbe(ready=True)
        cases = (
            (
                PrivilegeAction(
                    category="system-package",
                    argv=("/usr/bin/sudo", "/usr/bin/apt-get", "update"),
                    requires_sudo=True,
                    reason="install fixed Debian rootless dependencies",
                ),
                rocky,
                self.request(allow_system_packages=True),
            ),
            (
                PrivilegeAction(
                    category="linger",
                    argv=(
                        "/usr/bin/sudo",
                        "/usr/bin/loginctl",
                        "enable-linger",
                        "alice",
                    ),
                    requires_sudo=True,
                    reason="enable confirmed headless user service",
                ),
                ubuntu,
                self.request(service=False),
            ),
        )
        for action, platform, request in cases:
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(InstallError, "privilege_denied|system_dependency_missing"),
            ):
                verify_privilege_action(
                    action,
                    platform,
                    request,
                    probe=probe,
                    effective_uid=1001,
                    getpwuid=lambda uid: _account(uid=uid),
                )

    def test_system_prefix_rejects_arbitrary_unverified_rerun_executable(self) -> None:
        """non-root system-prefix 不能接受 caller 自选 executable 或漂移 argv。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        with self.assertRaisesRegex(InstallError, "privilege_denied"):
            build_dependency_actions(
                ubuntu,
                self.request(system_prefix=True, prefix=None),
                probe=_BackendProbe(ready=True),
                manifest=self.manifest,
                installer_artifact_path=Path("/tmp/caller-selected"),
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )

    def test_system_prefix_binds_hash_and_every_canonical_request_field(self) -> None:
        """rerun exact argv 必须覆盖完整 request，且执行前 rehash 阻断 artifact 替换。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        request = self.request(
            action="uninstall",
            system_prefix=True,
            prefix=None,
            onboard=False,
            config_file=Path("/safe/config.toml"),
            secrets_file=Path("/safe/secrets.env"),
            service=None,
            allow_system_packages=True,
            json_output=True,
            verbose=True,
            purge_data=True,
            confirm_data_loss=True,
        )
        plan = build_dependency_plan(
            ubuntu,
            request,
            probe=_BackendProbe(ready=True),
            manifest=self.manifest,
            installer_artifact_path=self.installer,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
        )
        system_prefix = plan.manual_rerun
        self.assertIsInstance(system_prefix, ManualRerunInstruction)
        assert system_prefix is not None
        self.assertEqual(
            system_prefix.argv,
            (
                "/usr/bin/sudo",
                "--",
                str(self.installer),
                "uninstall",
                "--version",
                "0.7.0",
                "--channel",
                "stable",
                "--state-home",
                "/var/lib/miniclaw",
                "--system-prefix",
                "--no-onboard",
                "--config",
                "/safe/config.toml",
                "--secrets-file",
                "/safe/secrets.env",
                "--allow-system-packages",
                "--dry-run",
                "--json",
                "--verbose",
                "--purge-data",
                "--yes-i-understand-data-loss",
            ),
        )
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            verify_privilege_action(
                system_prefix,  # type: ignore[arg-type]
                ubuntu,
                request,
                probe=_BackendProbe(ready=True),
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )
        self.installer.write_bytes(b"replacement after plan\n")
        try:
            build_dependency_plan(
                ubuntu,
                request,
                probe=_BackendProbe(ready=True),
                manifest=self.manifest,
                installer_artifact_path=self.installer,
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )
        except InstallError as error:
            self.assertRegex(str(error), "privilege_denied")
            self.assertIsNone(error.__cause__)
        else:
            self.fail("artifact replacement must fail execution-time rehash")

    def test_system_prefix_rejects_symlink_hash_and_semantic_argv_drift(self) -> None:
        """symlink/wrong hash 及 missing/reordered canonical flags 都不能进入执行边界。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        request = self.request(system_prefix=True, prefix=None, json_output=True)
        original = self.installer.read_bytes()
        self.installer.write_bytes(b"wrong hash\n")
        with self.assertRaisesRegex(InstallError, "privilege_denied"):
            build_dependency_plan(
                ubuntu,
                request,
                probe=_BackendProbe(ready=True),
                manifest=self.manifest,
                installer_artifact_path=self.installer,
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )
        self.installer.write_bytes(original)
        target = self.installer.with_name("real-installer.pyz")
        self.installer.rename(target)
        self.installer.symlink_to(target)
        with self.assertRaisesRegex(InstallError, "privilege_denied"):
            build_dependency_plan(
                ubuntu,
                request,
                probe=_BackendProbe(ready=True),
                manifest=self.manifest,
                installer_artifact_path=self.installer,
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )
        forged = object.__new__(ManualRerunInstruction)
        object.__setattr__(forged, "argv", ("/usr/bin/sudo", "--", "/tmp/evil"))
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            verify_privilege_action(
                forged,  # type: ignore[arg-type]
                ubuntu,
                request,
                probe=_BackendProbe(ready=True),
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )

    def test_every_completed_dependency_action_forces_backend_reprobe(self) -> None:
        """package/setup 完成后必须进入同用户 backend re-probe，不能只检查 setup。"""
        rocky = detect_linux(self.os_release("rocky", "10.0"), "x86_64")
        request = self.request(allow_system_packages=True)
        probe = _BackendProbe(ready=True)
        package = PrivilegeAction(
            category="system-package",
            argv=(
                "/usr/bin/sudo",
                "/usr/bin/dnf",
                "install",
                "-y",
                "podman-docker",
                "slirp4netns",
                "fuse-overlayfs",
                "shadow-utils",
                "dbus-daemon",
            ),
            requires_sudo=True,
            reason="install fixed RHEL rootless dependencies",
        )
        verify_privilege_action(
            package,
            rocky,
            request,
            probe=probe,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
            after_execution=True,
        )
        self.assertEqual(probe.required, [("podman-rootless", "alice", 1001)])

    def test_debian_package_completion_replans_to_revalidated_setup(self) -> None:
        """apt install 后 backend 仍缺失时，只返回 fresh-lstat target-user setup action。"""
        path = Path("/usr/bin/dockerd-rootless-setuptool.sh")
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        probe = _BackendProbe(
            ready=False,
            files={path: _file_fact(stat.S_IFREG | 0o755)},
        )
        package = PrivilegeAction(
            category="system-package",
            argv=(
                "/usr/bin/sudo",
                "/usr/bin/apt-get",
                "install",
                "-y",
                "docker.io",
                "rootlesskit",
                "uidmap",
                "dbus-user-session",
                "slirp4netns",
                "fuse-overlayfs",
            ),
            requires_sudo=True,
            reason="install fixed Debian rootless dependencies",
        )
        result = verify_privilege_action(
            package,
            ubuntu,
            self.request(allow_system_packages=True),
            probe=probe,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
            after_execution=True,
        )
        self.assertEqual(
            result,
            (
                PrivilegeAction(
                    category="system-package",
                    argv=(str(path), "install"),
                    requires_sudo=False,
                    reason="configure rootless Docker for target user",
                ),
            ),
        )

    def test_post_execution_probe_and_malformed_passwd_fail_stably(self) -> None:
        """动态 probe 与 passwd adapter 错误不得越过稳定 installer boundary。"""

        class ExplodingProbe(_BackendProbe):
            """模拟 setup 完成后的动态 adapter 失败。"""

            def require_backend(
                self,
                platform: DetectedPlatform,
                account: pwd.struct_passwd,
            ) -> None:
                """抛出不可信动态异常。"""
                raise TypeError("SECRET_DYNAMIC_DETAIL")

        path = Path("/usr/bin/dockerd-rootless-setuptool.sh")
        setup = PrivilegeAction(
            category="system-package",
            argv=(path.as_posix(), "install"),
            requires_sudo=False,
            reason="configure rootless Docker for target user",
        )
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            verify_privilege_action(
                setup,
                ubuntu,
                self.request(allow_system_packages=True),
                probe=ExplodingProbe(
                    ready=True,
                    files={path: _file_fact(stat.S_IFREG | 0o755)},
                ),
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
                after_execution=True,
            )
        with self.assertRaisesRegex(InstallError, "privilege_denied"):
            detect_macos(
                "15.0",
                "arm64",
                effective_uid=1001,
                getpwuid=lambda uid: SimpleNamespace(pw_uid="1001"),
            )

    def test_macos_never_plans_homebrew_install(self) -> None:
        """Seatbelt 必须有显式 probe 证据且永不计划 Homebrew。"""
        macos = detect_macos("15.0", "arm64")
        self.assertEqual(
            build_dependency_actions(
                macos,
                self.request(),
                probe=_BackendProbe(ready=True),
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            ),
            (),
        )
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            build_dependency_actions(
                macos,
                self.request(allow_system_packages=True),
                probe=_BackendProbe(ready=False),
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )

    def test_local_linux_probe_derives_socket_cli_and_containment(self) -> None:
        """production Linux probe 必须本地验证 socket、CLI 与 hardened smoke。"""
        docker = Path(self.installer_directory.name) / "docker"
        docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        docker.chmod(0o755)
        runtime = Path("/run/user/1001")
        socket = runtime / "docker.sock"
        real_lstat = os.lstat

        def lstat(path: Path) -> os.stat_result | SimpleNamespace:
            """提供完整 rootless runtime facts，并保留真实 executable fact。"""
            facts = {
                runtime: _file_fact(stat.S_IFDIR | 0o700, uid=1001, gid=1001),
                socket: _file_fact(stat.S_IFSOCK | 0o600, uid=1001, gid=1001),
            }
            if Path(path) in facts:
                return facts[Path(path)]
            return real_lstat(path)

        completed = subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=b"root-write-denied\nnetwork-denied\n",
            stderr=b"",
        )
        platform = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        local_probe = LocalPlatformProbe(
            platform,
            manifest=self.manifest,
            sandbox_artifact_path=self.sandbox_artifact,
        )
        with (
            mock.patch.object(platforms_module.shutil, "which", return_value=str(docker)),
            mock.patch.object(platforms_module.os, "lstat", side_effect=lstat),
            mock.patch.object(platforms_module.os, "geteuid", return_value=1001),
            mock.patch.object(platforms_module, "_run_local_probe", return_value=completed) as run,
        ):
            local_probe.require_backend(platform, _account())
        argv = run.call_args.args[0]
        self.assertIn(("--network", "none"), tuple(zip(argv, argv[1:], strict=False)))
        self.assertIn("--read-only", argv)
        entrypoint = argv.index("--entrypoint")
        self.assertEqual(
            argv[entrypoint : entrypoint + 5],
            (
                "--entrypoint",
                "python",
                "--",
                self.sandbox_artifact.read_text(encoding="utf-8").strip(),
                "-c",
            ),
        )
        self.assertNotIn("/var/run/docker.sock", repr(run.call_args))
        with self.assertRaises(TypeError):
            LocalPlatformProbe(  # type: ignore[call-arg]
                platform,
                manifest=self.manifest,
                sandbox_artifact_path=self.sandbox_artifact,
                runner=lambda argv: completed,
            )
        renamed = docker.with_name("podman")
        renamed.write_bytes(docker.read_bytes())
        renamed.chmod(0o755)
        with (
            mock.patch.object(platforms_module.shutil, "which", return_value=str(renamed)),
            mock.patch.object(platforms_module.os, "lstat", side_effect=lstat),
            mock.patch.object(platforms_module.os, "geteuid", return_value=1001),
            mock.patch.object(platforms_module, "_run_local_probe", return_value=completed),
            self.assertRaisesRegex(InstallError, "system_dependency_missing"),
        ):
            local_probe.require_backend(platform, _account())

    def test_activation_evidence_requires_successful_private_or_production_probe(self) -> None:
        """private fake 只返回测试结果，production 不暴露可消费 capability。"""
        platform = detect_macos("15.0", "arm64")
        evidence = _verify_activation_ready_with_probe(
            platform,
            _account(),
            probe=_BackendProbe(ready=True),
        )
        self.assertEqual((evidence.backend, evidence.uid), ("seatbelt", 1001))
        self.assertFalse(hasattr(platforms_module, "ActivationEvidence"))

    def test_round3_system_prefix_is_manual_instruction_not_privilege_capability(self) -> None:
        """non-root system-prefix 只返回展示指令，不能进入自动高权限执行器。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        result = build_dependency_plan(
            ubuntu,
            self.request(system_prefix=True, prefix=None),
            probe=_BackendProbe(ready=True),
            manifest=self.manifest,
            installer_artifact_path=self.installer,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
        )
        manual = result.manual_rerun
        assert manual is not None
        self.assertNotIsInstance(manual, PrivilegeAction)
        with self.assertRaisesRegex(InstallError, "privilege_denied|system_dependency_missing"):
            verify_privilege_action(
                manual,  # type: ignore[arg-type]
                ubuntu,
                self.request(system_prefix=True, prefix=None),
                probe=_BackendProbe(ready=True),
                effective_uid=1001,
                getpwuid=lambda uid: _account(uid=uid),
            )

    def test_round3_system_prefix_privilege_category_is_closed(self) -> None:
        """即使 argv 形状正确，system-prefix 也不能构造自动执行 capability。"""
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            PrivilegeAction(
                category="system-prefix",
                argv=(
                    "/usr/bin/sudo",
                    "--",
                    str(self.installer),
                    "install",
                    "--channel",
                    "stable",
                    "--state-home",
                    "/var/lib/miniclaw",
                    "--system-prefix",
                ),
                requires_sudo=True,
                reason="rerun verified installer for explicit system prefix",
            )

    def test_round3_root_system_prefix_waits_for_bootstrap_loaded_receipt(self) -> None:
        """Task12 未提供 current-loaded receipt 前，root system-prefix 必须 fail closed。"""
        ubuntu = detect_linux(
            self.os_release("ubuntu", "24.04"),
            "x86_64",
            effective_uid=0,
            original_user="alice",
            original_uid=1001,
            getpwnam=lambda name: _account(name=name),
        )
        with self.assertRaisesRegex(InstallError, "privilege_denied"):
            build_dependency_plan(
                ubuntu,
                self.request(system_prefix=True, prefix=None),
                probe=_BackendProbe(ready=True),
                effective_uid=0,
                original_user="alice",
                original_uid=1001,
                getpwnam=lambda name: _account(name=name),
            )

    def test_round3_receipts_cannot_be_caller_self_signed(self) -> None:
        """caller 不能用任意 hash/image 公开铸造 installer 或 sandbox receipt。"""
        self.assertFalse(hasattr(platforms_module, "InstallerArtifactEvidence"))
        self.assertFalse(hasattr(platforms_module, "SandboxVerification"))

    def test_round3_dev_prerelease_uses_install_request_semver_contract(self) -> None:
        """manual rerun 复用 Task4 dev prerelease，不再做第二套窄版本解析。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        result = build_dependency_plan(
            ubuntu,
            self.request(
                version="0.8.0-rc.1+build.7",
                channel="dev",
                system_prefix=True,
                prefix=None,
            ),
            probe=_BackendProbe(ready=True),
            manifest=self.release_manifest("0.8.0-rc.1+build.7"),
            installer_artifact_path=self.installer,
            effective_uid=1001,
            getpwuid=lambda uid: _account(uid=uid),
        )
        manual = result.manual_rerun
        assert manual is not None
        self.assertIn("0.8.0-rc.1+build.7", manual.argv)
        self.assertNotIsInstance(manual, PrivilegeAction)

    def test_round3_production_activation_has_no_evidence_injection(self) -> None:
        """production activation 必须当场构造 LocalPlatformProbe，不能消费 evidence。"""
        parameters = inspect.signature(verify_activation_ready).parameters
        self.assertNotIn("verification", parameters)
        self.assertNotIn("evidence", parameters)
        macos = detect_macos("15.0", "arm64")
        account = _account()
        with (
            mock.patch.object(
                platforms_module,
                "_production_identity",
                return_value=(1001, None, None),
            ),
            mock.patch.object(
                platforms_module,
                "_resolve_invoking_user",
                return_value=account,
            ),
            mock.patch.object(platforms_module, "LocalPlatformProbe") as probe_type,
        ):
            self.assertIsNone(verify_activation_ready(macos))
        probe_type.assert_called_once_with(
            macos,
            manifest=None,
            sandbox_artifact_path=None,
        )
        probe_type.return_value.require_backend.assert_called_once_with(macos, account)

    def test_round3_containment_uses_entrypoint_python(self) -> None:
        """容器 smoke 用 exact entrypoint，image 后不再接受可变程序名。"""
        source = inspect.getsource(platforms_module._verify_rootless_containment)
        self.assertIn('"--entrypoint",\n            "python"', source)
        self.assertNotIn('image,\n            "python",', source)

    def test_round3_probe_does_not_capture_unbounded_output_with_run(self) -> None:
        """本地 probe 必须从 PIPE 流式限额，禁止 subprocess.run capture 后检查。"""
        source = inspect.getsource(platforms_module._run_local_probe)
        self.assertIn("subprocess.Popen", source)
        self.assertNotIn("capture_output=True", source)

    def test_round3_probe_terminates_and_reaps_on_stream_budget_overrun(self) -> None:
        """stdout 超限时立即终止仍在 sleep 的 child，并隐藏动态 cause。"""
        programs = (
            "import sys,time;sys.stdout.write('x'*5000);sys.stdout.flush();time.sleep(10)",
            "import sys,time;sys.stdout.write('x'*2500);sys.stderr.write('y'*2500);"
            "sys.stdout.flush();sys.stderr.flush();time.sleep(10)",
        )
        for program in programs:
            with self.subTest(program=program):
                started = time.monotonic()
                try:
                    with mock.patch.object(platforms_module.os, "geteuid", return_value=1001):
                        platforms_module._run_local_probe(
                            (sys.executable, "-c", program),
                            _account(),
                            {},
                        )
                except InstallError as error:
                    self.assertRegex(str(error), "system_dependency_missing")
                    self.assertIsNone(error.__cause__)
                else:
                    self.fail("stream budget overrun must fail closed")
                self.assertLess(time.monotonic() - started, 3)

    def test_round3_sandbox_receipt_rehashes_before_live_probe(self) -> None:
        """receipt 构造后同路径替换必须在 socket/CLI probe 前被 hash 重验阻断。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        probe = LocalPlatformProbe(
            ubuntu,
            manifest=self.manifest,
            sandbox_artifact_path=self.sandbox_artifact,
        )
        self.sandbox_artifact.write_bytes(b"example/miniclaw@sha256:" + b"b" * 64 + b"\n")
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            probe.require_backend(ubuntu, _account())

    def test_local_macos_probe_uses_fixed_seatbelt_containment(self) -> None:
        """production macOS probe 必须执行 fixed sandbox-exec deny-default smoke。"""
        macos = detect_macos("15.0", "arm64")
        completed = subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=b"",
            stderr=b"",
        )
        seatbelt = Path("/usr/bin/sandbox-exec")
        with (
            mock.patch.object(platforms_module.host_platform, "system", return_value="Darwin"),
            mock.patch.object(
                platforms_module.os,
                "lstat",
                return_value=_file_fact(stat.S_IFREG | 0o755),
            ),
            mock.patch.object(platforms_module.os, "geteuid", return_value=1001),
            mock.patch.object(platforms_module, "_run_local_probe", return_value=completed) as run,
        ):
            LocalPlatformProbe(macos).require_backend(macos, _account())
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], str(seatbelt))
        self.assertIn("(deny default)", argv[2])
        self.assertIn("(deny network*)", argv[2])

    def test_rhel_probe_requires_usr_bin_docker_to_exact_podman(self) -> None:
        """RHEL compatibility 只接受 /usr/bin/docker 精确解析到 /usr/bin/podman。"""
        rocky = detect_linux(self.os_release("rocky", "10.0"), "x86_64")
        local_probe = LocalPlatformProbe(
            rocky,
            manifest=self.manifest,
            sandbox_artifact_path=self.sandbox_artifact,
        )
        runtime = Path("/run/user/1001")
        podman_runtime = runtime / "podman"
        socket = podman_runtime / "podman.sock"
        facts = {
            runtime: _file_fact(stat.S_IFDIR | 0o700, uid=1001, gid=1001),
            podman_runtime: _file_fact(stat.S_IFDIR | 0o700, uid=1001, gid=1001),
            socket: _file_fact(stat.S_IFSOCK | 0o600, uid=1001, gid=1001),
            Path("/usr/bin/podman"): _file_fact(stat.S_IFREG | 0o755),
            Path("/tmp/podman"): _file_fact(stat.S_IFREG | 0o755),
        }
        completed = (
            subprocess.CompletedProcess((), 0, b"podman version 5.0\n", b""),
            subprocess.CompletedProcess(
                (),
                0,
                b"root-write-denied\nnetwork-denied\n",
                b"",
            ),
        )
        real_lstat = os.lstat
        real_resolve = Path.resolve

        def resolve(path: Path, strict: bool = False) -> Path:
            """把 fixed compatibility path 解析到测试指定目标。"""
            if path == Path("/usr/bin/docker"):
                return Path("/usr/bin/podman")
            return real_resolve(path, strict=strict)

        with (
            mock.patch.object(
                platforms_module.os,
                "lstat",
                side_effect=lambda path: (
                    facts[Path(path)] if Path(path) in facts else real_lstat(path)
                ),
            ),
            mock.patch.object(platforms_module.os, "geteuid", return_value=1001),
            mock.patch.object(platforms_module.Path, "resolve", autospec=True, side_effect=resolve),
            mock.patch.object(platforms_module, "_run_local_probe", side_effect=completed),
        ):
            local_probe.require_backend(rocky, _account())

        def escape(path: Path, strict: bool = False) -> Path:
            """模拟 /usr/bin/docker 被替换为受信目录外 podman symlink。"""
            if path == Path("/usr/bin/docker"):
                return Path("/tmp/podman")
            return real_resolve(path, strict=strict)

        with (
            mock.patch.object(
                platforms_module.os,
                "lstat",
                side_effect=lambda path: (
                    facts[Path(path)] if Path(path) in facts else real_lstat(path)
                ),
            ),
            mock.patch.object(platforms_module.os, "geteuid", return_value=1001),
            mock.patch.object(platforms_module.Path, "resolve", autospec=True, side_effect=escape),
            mock.patch.object(platforms_module, "_run_local_probe", side_effect=completed),
            self.assertRaisesRegex(InstallError, "system_dependency_missing"),
        ):
            local_probe.require_backend(rocky, _account())

    def test_runtime_versions_are_exact_and_hash_bound(self) -> None:
        """bootstrap Runtime 版本、官方 URL 与四平台 hash 必须是唯一固定事实。"""
        document = json.loads(self.runtime_versions.read_text(encoding="utf-8"))
        self.assertEqual(set(document), {"uv", "node", "pnpm"})
        self.assertEqual(document["uv"]["version"], "0.12.0")
        self.assertEqual(document["node"]["version"], "24.18.0")
        self.assertEqual(document["pnpm"], {"version": "10.14.0"})
        self.assertEqual(
            {key: value["sha256"] for key, value in document["uv"]["archives"].items()},
            {
                "linux-x86_64": "eaf842262aa1c418d8ecc5605f02ee1ebfd369124fa48548e85f9481a47831a9",
                "linux-arm64": "2c5d6e3092cc5223b10ff403880cc75121bf64e84644e7a0c69f643b0d89ac95",
                "macos-x86_64": "d41593beaefc54bab7d062af0ef6ca093bfb81d001d58ebbef39e44423f9c496",
                "macos-arm64": "2b9e582af54f84fa50c115427451a6c13e80f43b52f8282b8af5791077317bbf",
            },
        )
        self.assertEqual(
            {key: value["sha256"] for key, value in document["node"]["archives"].items()},
            {
                "linux-x86_64": "783130984963db7ba9cbd01089eaf2c2efb055c7c1693c943174b967b3050cb8",
                "linux-arm64": "6b4484c2190274175df9aa8f28e2d758a819cb1c1fe6ab481e2f95b463ab8508",
                "macos-x86_64": "dfd0dbd3e721503434df7b7205e719f61b3a3a31b2bcf9729b8b91fea240f080",
                "macos-arm64": "e1a97e14c99c803e96c7339403282ea05a499c32f8d83defe9ef5ec66f979ed1",
            },
        )
        for runtime in (document["uv"], document["node"]):
            self.assertEqual(
                set(runtime["archives"]),
                {"linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64"},
            )
            self.assertTrue(
                all(item["url"].startswith("https://") for item in runtime["archives"].values())
            )


if __name__ == "__main__":
    unittest.main()
