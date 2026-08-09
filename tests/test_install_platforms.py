"""验证 Tier 1 平台检测、Runtime pin 与显式权限计划。"""

import json
import unittest
from pathlib import Path

from miniclaw.install.models import InstallError, InstallRequest, PlatformKey
from miniclaw.install.platforms import (
    PrivilegeAction,
    build_dependency_actions,
    detect_linux,
    detect_macos,
    detect_platform,
    node_version_supported,
)


class InstallPlatformsTest(unittest.TestCase):
    """覆盖所有受支持平台与 fail-closed 主机事实。"""

    runtime_versions = Path("release/runtime-versions.json")

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
                        "docker-rootless"
                        if distro in {"ubuntu", "debian"}
                        else "podman-rootless"
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
            with self.subTest(text=text, machine=machine, facts=facts), self.assertRaisesRegex(
                InstallError, "unsupported_platform"
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
            with self.subTest(text=text), self.assertRaisesRegex(
                InstallError, "unsupported_platform"
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
            with self.subTest(facts=facts), self.assertRaisesRegex(
                InstallError, "privilege_denied"
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
            )

    def test_dependency_dry_run_is_closed_world_exact_argv(self) -> None:
        """系统依赖只产生固定 argv，sudo 动作必须等待单独批准。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        actions = build_dependency_actions(
            ubuntu,
            {
                "system_packages_missing": True,
                "rootless_setup_tool": "/usr/bin/dockerd-rootless-setuptool.sh",
                "rootless_setup_tool_regular": True,
                "rootless_setup_tool_executable": True,
                "target_user": "alice",
                "linger_user": "alice",
                "system_prefix": True,
            },
        )
        self.assertEqual(
            tuple(action.argv for action in actions),
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
                (
                    "/usr/bin/sudo",
                    "-u",
                    "alice",
                    "--",
                    "/usr/bin/dockerd-rootless-setuptool.sh",
                    "install",
                ),
                ("/usr/bin/sudo", "/usr/bin/loginctl", "enable-linger", "alice"),
                (
                    "/usr/bin/sudo",
                    "/usr/bin/install",
                    "-d",
                    "-m",
                    "0755",
                    "/usr/local/lib/miniclaw",
                ),
            ),
        )
        self.assertTrue(all(isinstance(action, PrivilegeAction) for action in actions))
        self.assertTrue(all(not action.approved for action in actions))
        self.assertTrue(
            all(action.requires_sudo for action in actions if action.argv[0].endswith("sudo"))
        )
        rendered = json.dumps([list(action.argv) for action in actions])
        self.assertNotIn(";", rendered)
        self.assertNotIn("docker group", rendered)
        self.assertNotIn("/var/run/docker.sock", rendered)

    def test_rhel_dependency_plan_uses_only_rootless_podman_compatibility(self) -> None:
        """RHEL family 只计划固定 podman-docker 包集，绝不启动 root Docker。"""
        rhel = detect_linux(self.os_release("rocky", "10.0"), "arm64")
        actions = build_dependency_actions(rhel, {"system_packages_missing": True})
        self.assertEqual(
            tuple(action.argv for action in actions),
            ((
                "/usr/bin/sudo",
                "/usr/bin/dnf",
                "install",
                "-y",
                "podman-docker",
                "slirp4netns",
                "fuse-overlayfs",
                "shadow-utils",
                "dbus-daemon",
            ),),
        )
        self.assertEqual(rhel.sandbox_backend, "podman-rootless")
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            build_dependency_actions(rhel, {})
        self.assertEqual(
            build_dependency_actions(
                rhel,
                {"backend_ready": True, "podman_docker_compatible": True},
            ),
            (),
        )

    def test_linux_backend_readiness_is_never_assumed(self) -> None:
        """没有 rootless readiness 事实时不得把稳定完整安装伪装成可继续。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            build_dependency_actions(ubuntu, {})
        self.assertEqual(build_dependency_actions(ubuntu, {"backend_ready": True}), ())

    def test_dependency_facts_reject_bool_and_injected_package_or_tool(self) -> None:
        """fact 类型错误、额外 package 和非候选 setup tool 都不得进入 argv。"""
        ubuntu = detect_linux(self.os_release("ubuntu", "24.04"), "x86_64")
        for facts in (
            {"system_packages_missing": 1},
            {"system_packages_missing": True, "package": "curl;touch /tmp/owned"},
            {"rootless_setup_tool": "/tmp/tool"},
            {"linger_user": "alice;id"},
            {"system_prefix": "yes"},
        ):
            with self.subTest(facts=facts), self.assertRaisesRegex(
                InstallError, "system_dependency_missing"
            ):
                build_dependency_actions(ubuntu, facts)

    def test_macos_never_plans_homebrew_install(self) -> None:
        """Seatbelt 不需要系统包，任何注入的 Homebrew 事实都必须拒绝。"""
        macos = detect_macos("15.0", "arm64")
        self.assertEqual(build_dependency_actions(macos, {}), ())
        with self.assertRaisesRegex(InstallError, "system_dependency_missing"):
            build_dependency_actions(macos, {"homebrew_install": True})

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
                all(
                    item["url"].startswith("https://")
                    for item in runtime["archives"].values()
                )
            )


if __name__ == "__main__":
    unittest.main()
