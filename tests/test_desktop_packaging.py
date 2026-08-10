"""Desktop 分发打包配置、版本一致性与签名姿态的离线契约测试。

这些用例不执行任何真实打包：它们只对 `desktop/electron-builder.json`、
`desktop/package.json` 与 `desktop/pnpm-lock.yaml` 的内容断言，风格与
`tests/test_deploy_image.py` 对 Dockerfile 的离线断言一致。
"""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

import yaml

from lobster0 import __version__

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DESKTOP = _REPO_ROOT / "desktop"
_PACKAGE_JSON = _DESKTOP / "package.json"
_BUILDER_CONFIG = _DESKTOP / "electron-builder.json"
_LOCKFILE = _DESKTOP / "pnpm-lock.yaml"
_GITIGNORE = _REPO_ROOT / ".gitignore"

_EXACT_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
# 仓库内工作区链接是唯一允许的非精确 specifier：它不来自 registry，也没有版本漂移面。
_WORKSPACE_LINKS = {"@lobster0/pi-tui": "file:../tui"}
_ARTIFACT_PREFIX = "lobster0-desktop-${version}-"
_ARTIFACT_SUFFIX = ".${ext}"
# 任何签名凭据一旦出现在仓库文件里就是泄露；本仓库当前的桌面产物明确未签名。
_SIGNING_MARKERS = (
    "csc_link",
    "csclink",
    "csc_key_password",
    "csckeypassword",
    "appleid",
    "apple_id",
    "appleidpassword",
    "apple_app_specific_password",
    "teamid",
    "team_id",
    "certificatefile",
    "certificatepassword",
    "certificatesubjectname",
    "certificatesha1",
    "keychain",
    "provisioningprofile",
    "-----begin",
)
_CREDENTIAL_SUFFIXES = (".p12", ".pfx", ".cer", ".der", ".pem", ".key", ".mobileprovision")
# 这两个目录只存在于本地开发机，永远不进版本库，扫描时直接剪枝。
_UNTRACKED_DIRECTORIES = {"node_modules", "dist", "out"}


def _load_json(path: Path) -> dict[str, object]:
    """读取一个必须是 JSON 对象的仓库文件。"""
    document = json.loads(path.read_text(encoding="utf-8"))
    if type(document) is not dict:
        raise AssertionError(f"{path.name} is not a JSON object")
    return document


def _mapping(document: dict[str, object], key: str) -> dict[str, object]:
    """返回一个必须存在且为对象的子映射。"""
    value = document.get(key)
    if type(value) is not dict:
        raise AssertionError(f"missing object field {key!r}")
    return value


class DesktopPackagingFilesTest(unittest.TestCase):
    """打包所需的三个文件必须真实存在且可解析。"""

    def test_packaging_files_exist_as_regular_files(self) -> None:
        """package.json、electron-builder.json 与 lockfile 必须同时交付。"""
        for path in (_PACKAGE_JSON, _BUILDER_CONFIG, _LOCKFILE):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"missing {path}")
                self.assertFalse(path.is_symlink(), f"{path} must be a regular file")

    def test_packaging_configuration_is_well_formed_json(self) -> None:
        """electron-builder 配置必须是可解析的 JSON 对象。"""
        self.assertIsInstance(_load_json(_BUILDER_CONFIG), dict)


class DesktopVersionAgreementTest(unittest.TestCase):
    """桌面包版本必须与 `src/lobster0/_version.py` 完全一致。

    产物文件名由 electron-builder 的 `${version}` 模板渲染，而 `${version}`
    只来自 `desktop/package.json`。一旦这里漂移，Release 上就会出现
    `lobster0-desktop-0.1.0-*` 这种与 tag 不符的包，且没有任何其他门禁会发现。
    """

    def test_desktop_package_version_equals_the_release_constant(self) -> None:
        """desktop/package.json 的 version 必须等于唯一版本常量。"""
        package = _load_json(_PACKAGE_JSON)
        self.assertRegex(str(package["version"]), _EXACT_VERSION)
        self.assertEqual(package["version"], __version__)

    def test_artifact_names_carry_the_version_template(self) -> None:
        """每个 artifactName 模板都必须带 `${version}` 与 `${arch}`。"""
        config = _load_json(_BUILDER_CONFIG)
        templates = [str(config["artifactName"])]
        for section in ("mac", "dmg", "linux", "appImage"):
            templates.append(str(_mapping(config, section)["artifactName"]))
        for template in templates:
            with self.subTest(template=template):
                self.assertTrue(template.startswith(_ARTIFACT_PREFIX), template)
                self.assertTrue(template.endswith(_ARTIFACT_SUFFIX), template)
                self.assertIn("${arch}", template)


class DesktopDependencyPinningTest(unittest.TestCase):
    """桌面依赖不得引入任何浮动区间，lockfile 必须与 package.json 同步。"""

    def setUp(self) -> None:
        """读取 package.json 与 lockfile。"""
        self.package = _load_json(_PACKAGE_JSON)
        self.dependencies = _mapping(self.package, "dependencies")
        self.development = _mapping(self.package, "devDependencies")
        self.lockfile = yaml.safe_load(_LOCKFILE.read_text(encoding="utf-8"))

    def test_every_declared_dependency_is_pinned_to_an_exact_version(self) -> None:
        """所有 dependency/devDependency 只能是精确版本或仓库内链接。"""
        for section in (self.dependencies, self.development):
            for name, specifier in section.items():
                with self.subTest(dependency=name):
                    if name in _WORKSPACE_LINKS:
                        self.assertEqual(specifier, _WORKSPACE_LINKS[name])
                        continue
                    self.assertRegex(str(specifier), _EXACT_VERSION)

    def test_the_packaging_tool_is_a_pinned_development_dependency(self) -> None:
        """electron-builder 必须固定在 devDependencies，不进入运行时依赖。"""
        self.assertIn("electron-builder", self.development)
        self.assertRegex(str(self.development["electron-builder"]), _EXACT_VERSION)
        self.assertNotIn("electron-builder", self.dependencies)

    def test_the_lockfile_matches_every_declared_specifier(self) -> None:
        """lockfile 的 importer specifier 必须与 package.json 逐条一致。

        CI 与 Release 都用 `--frozen-lockfile` 安装；lockfile 一旦落后于
        package.json，桌面打包作业会在发布当天才失败。
        """
        importer = self.lockfile["importers"]["."]
        for field, declared in (
            ("dependencies", self.dependencies),
            ("devDependencies", self.development),
        ):
            locked = importer.get(field, {})
            with self.subTest(field=field):
                self.assertEqual(set(locked), set(declared))
            for name, specifier in declared.items():
                with self.subTest(field=field, dependency=name):
                    self.assertEqual(locked[name]["specifier"], specifier)

    def test_the_desktop_package_is_never_publishable_to_a_registry(self) -> None:
        """桌面包只做分发产物，不得被误发到 npm。"""
        self.assertIs(self.package["private"], True)


class ElectronBuilderConfigTest(unittest.TestCase):
    """打包配置必须声明完整的应用身份与确定性产物命名。"""

    def setUp(self) -> None:
        """读取 electron-builder 配置。"""
        self.config = _load_json(_BUILDER_CONFIG)
        self.raw = _BUILDER_CONFIG.read_text(encoding="utf-8")

    def _targets(self, section: str) -> dict[str, tuple[str, ...]]:
        """把一个平台段的 target 列表规范化为 ``{target: (arch, ...)}``。"""
        entries = _mapping(self.config, section)["target"]
        self.assertEqual(type(entries), list)
        return {
            str(entry["target"]): tuple(str(arch) for arch in entry["arch"])
            for entry in entries
        }

    def test_application_identity_is_declared(self) -> None:
        """appId、productName 与两个平台的分类必须齐备。"""
        self.assertEqual(self.config["productName"], "Lobster0")
        app_id = str(self.config["appId"])
        self.assertRegex(app_id, r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+$")
        self.assertIn("lobster0", app_id)
        self.assertEqual(
            _mapping(self.config, "mac")["category"],
            "public.app-category.developer-tools",
        )
        self.assertEqual(_mapping(self.config, "linux")["category"], "Development")

    def test_macos_produces_dmg_and_zip_for_both_architectures(self) -> None:
        """macOS 必须同时产出 Intel 与 Apple Silicon 的 dmg 与 zip。"""
        targets = self._targets("mac")
        self.assertEqual(set(targets), {"dmg", "zip"})
        for target, arches in targets.items():
            with self.subTest(target=target):
                self.assertEqual(set(arches), {"arm64", "x64"})

    def test_linux_produces_appimage_for_both_architectures(self) -> None:
        """Linux 只产出 AppImage，但必须覆盖 x64 与 arm64。

        AppImage 是唯一一种在全部 Tier 1 Linux 发行版（Ubuntu/Debian/RHEL/
        Rocky/AlmaLinux）上都能直接运行的单文件格式；`.deb` 只覆盖其中一半，
        因此不做半套支持。
        """
        targets = self._targets("linux")
        self.assertEqual(set(targets), {"AppImage"})
        self.assertEqual(set(targets["AppImage"]), {"x64", "arm64"})

    def test_windows_is_deliberately_not_packaged(self) -> None:
        """Tier 1 明确不含 Windows，配置里不得出现任何 Windows 目标。"""
        self.assertNotIn("win", self.config)
        lowered = self.raw.lower()
        for marker in ("nsis", "squirrel", "appx", "portable", "\"win\""):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, lowered)

    def test_packaging_never_publishes_by_itself(self) -> None:
        """`publish` 必须为 null：产物只能由 Release 工作流上传。"""
        self.assertIsNone(self.config["publish"])
        self.assertIn("publish", self.config)

    def test_packaged_contents_are_an_explicit_allowlist(self) -> None:
        """打包内容必须是显式白名单，且不重建原生依赖。"""
        files = self.config["files"]
        self.assertEqual(type(files), list)
        self.assertIn("out/**/*", files)
        self.assertIn("package.json", files)
        self.assertIs(self.config["asar"], True)
        self.assertIs(self.config["npmRebuild"], False)

    def test_the_packaging_output_directory_is_ignored_by_git(self) -> None:
        """打包输出目录必须被 .gitignore 覆盖，避免把上百 MB 产物提交进仓库。"""
        output = str(_mapping(self.config, "directories")["output"])
        self.assertEqual(output, "dist")
        rules = _GITIGNORE.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"{output}/", rules)


class DesktopSigningPostureTest(unittest.TestCase):
    """签名姿态必须是「明确未签名」，且仓库里没有任何签名凭据。"""

    def test_no_signing_credential_appears_in_any_packaging_file(self) -> None:
        """package.json 与 electron-builder.json 不得携带任何签名凭据。"""
        for path in (_PACKAGE_JSON, _BUILDER_CONFIG):
            lowered = path.read_text(encoding="utf-8").lower()
            for marker in _SIGNING_MARKERS:
                with self.subTest(path=path.name, marker=marker):
                    self.assertNotIn(marker, lowered)

    def test_no_certificate_material_is_committed_under_desktop(self) -> None:
        """`desktop/` 下不得出现任何证书或私钥文件。"""
        for directory, names, files in os.walk(_DESKTOP):
            names[:] = [name for name in names if name not in _UNTRACKED_DIRECTORIES]
            for name in files:
                with self.subTest(path=f"{directory}/{name}"):
                    self.assertNotIn(Path(name).suffix.lower(), _CREDENTIAL_SUFFIXES)

    def test_notarization_is_explicitly_disabled(self) -> None:
        """公证必须显式关闭：没有 Apple 凭据时绝不能假装走过公证。"""
        self.assertIs(_mapping(_load_json(_BUILDER_CONFIG), "mac")["notarize"], False)


if __name__ == "__main__":
    unittest.main()
