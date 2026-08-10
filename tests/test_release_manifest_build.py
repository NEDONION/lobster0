"""Release manifest、SBOM、checksums 的封闭世界与可复现构建测试。"""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from lobster0.install import models as install_models
from lobster0.install.models import ReleaseManifest
from lobster0.storage import migrations as storage_migrations
from lobster0.storage.migrations import LATEST_SCHEMA_VERSION
from scripts.build_release_manifest import (
    ReleaseBuildError,
    ReleaseInputs,
    build_checksums,
    build_manifest,
    build_release_inputs_document,
    build_release_outputs,
    collect_release_inputs,
    load_features,
)
from scripts.build_sbom import SbomBuildError, SbomInputs, build_sbom
from scripts.render_install_script import ReleaseInputs as BootstrapReleaseInputs
from scripts.verify_release_artifacts import ReleaseVerifyError, verify_release

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_VERSIONS = ROOT / "release/runtime-versions.json"
FEATURES_REGISTRY = ROOT / "release/features.json"
SCHEMA = ROOT / "release/manifest.schema.json"
VERSION = "0.7.0"
COMMIT = "1a2b3c4d" * 5
EPOCH = 1_754_784_000
PLATFORMS = ("linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64")
RUNTIME_DIGEST = "a" * 64
SANDBOX_DIGEST = "b" * 64
LICENSES = {
    "MIT": [{"name": "@earendil-works/pi-tui", "versions": ["0.4.0"], "license": "MIT"}],
    "ISC": [{"name": "yallist", "versions": ["4.0.0"], "license": "ISC"}],
}
PYTHON_PACKAGES = (
    ("croniter", "6.2.0", "MIT"),
    ("httpx", "0.28.1", "BSD-3-Clause"),
)


def _node_pins() -> tuple[str, dict[str, dict[str, str]]]:
    """读取仓库真实 Node pin，使 fixture bundle 与生产 pin 精确一致。"""
    document = json.loads(RUNTIME_VERSIONS.read_text(encoding="utf-8"))
    return document["node"]["version"], document["node"]["archives"]


def _write_tar(path: Path, members: dict[str, bytes], *, link: str | None = None) -> None:
    """写入只含 regular file 的最小 tar.gz fixture，可选注入一个 symlink。"""
    with tarfile.open(path, "w:gz") as archive:
        for name in sorted(members):
            payload = members[name]
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(payload))
        if link is not None:
            member = tarfile.TarInfo(link)
            member.type = tarfile.SYMTYPE
            member.linkname = "../outside"
            archive.addfile(member)


def _write_wheel(path: Path, *, name: str = "lobster0-agent", version: str = VERSION) -> None:
    """写入带 dist-info metadata 与 console script 的最小 wheel fixture。"""
    dist_info = f"lobster0_agent-{version}.dist-info"
    entries = {
        "lobster0/__init__.py": b"",
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        ).encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n",
        f"{dist_info}/entry_points.txt": b"[console_scripts]\nlobster0 = lobster0.cli:main\n",
        f"{dist_info}/RECORD": (
            f"lobster0/__init__.py,,\n{dist_info}/METADATA,,\n{dist_info}/RECORD,,\n"
        ).encode(),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(entries):
            info = zipfile.ZipInfo(entry, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[entry])


class ReleaseFixture:
    """在临时目录里物化一整套 Tier 1 Release artifact。"""

    def __init__(self, root: Path) -> None:
        """写入 14 个基础 artifact，再用真实 SBOM builder 补齐两份 SBOM。"""
        self.root = root
        self.artifacts = root / "artifacts"
        self.artifacts.mkdir(parents=True)
        self.node_version, self.node_archives = _node_pins()
        self.install_script = root / "install.sh"
        self.install_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.python_components = root / "uv-cyclonedx.json"
        self.python_components.write_bytes(self._python_components())
        _write_wheel(self.artifacts / f"lobster0_agent-{VERSION}-py3-none-any.whl")
        _write_tar(
            self.artifacts / f"lobster0_agent-{VERSION}.tar.gz",
            {
                f"lobster0_agent-{VERSION}/PKG-INFO": (
                    f"Metadata-Version: 2.1\nName: lobster0-agent\nVersion: {VERSION}\n"
                ).encode(),
                f"lobster0_agent-{VERSION}/pyproject.toml": b"[project]\n",
            },
        )
        (self.artifacts / "requirements-all.lock").write_bytes(self._requirements())
        (self.artifacts / "lobster0-installer.pyz").write_bytes(self._installer())
        for platform in PLATFORMS:
            self.write_node_bundle(platform, self.node_archives[platform]["sha256"])
            self.write_tui_bundle(platform)
        (self.artifacts / f"lobster0-runtime-image-{VERSION}.txt").write_text(
            f"ghcr.io/nedonion/lobster0@sha256:{RUNTIME_DIGEST}\n", encoding="utf-8"
        )
        (self.artifacts / f"lobster0-sandbox-image-{VERSION}.txt").write_text(
            f"ghcr.io/nedonion/lobster0-sandbox@sha256:{SANDBOX_DIGEST}\n", encoding="utf-8"
        )
        self.write_sboms()

    def write_node_bundle(self, platform: str, upstream: str) -> Path:
        """写入一个带 release-component.json 的 managed Node bundle fixture。"""
        component = {
            "name": "node",
            "platform": platform,
            "upstream_sha256": upstream,
            "upstream_url": self.node_archives[platform]["url"],
            "version": self.node_version,
        }
        encoded = json.dumps(component, sort_keys=True, separators=(",", ":"))
        path = self.artifacts / f"lobster0-node-{self.node_version}-{platform}.tar.gz"
        path.unlink(missing_ok=True)
        _write_tar(
            path,
            {
                "node/LICENSE": b"Node license\n",
                "node/bin/node": b"#!/bin/sh\nexit 0\n",
                "node/release-component.json": f"{encoded}\n".encode(),
            },
        )
        return path

    def write_tui_bundle(self, platform: str, *, link: str | None = None) -> Path:
        """写入一个带 production licenses.json 的 TUI bundle fixture。"""
        encoded = json.dumps(LICENSES, sort_keys=True, separators=(",", ":"))
        path = self.artifacts / f"lobster0-tui-{VERSION}-{platform}.tar.gz"
        path.unlink(missing_ok=True)
        _write_tar(
            path,
            {
                "tui/licenses.json": f"{encoded}\n".encode(),
                "tui/package.json": b'{"name":"lobster0-tui","private":true}\n',
                "tui/dist/index.js": b"process.exit(0);\n",
            },
            link=link,
        )
        return path

    def write_sboms(self, *, version: str = VERSION) -> None:
        """用真实 SBOM builder 生成 CycloneDX 与 SPDX 两份 Release 文档。"""
        documents = build_sbom(self.sbom_inputs(version=version))
        (self.artifacts / f"lobster0-{VERSION}-sbom.cyclonedx.json").write_bytes(
            documents.cyclonedx
        )
        (self.artifacts / f"lobster0-{VERSION}-sbom.spdx.json").write_bytes(documents.spdx)

    def sbom_inputs(self, *, version: str = VERSION) -> SbomInputs:
        """返回覆盖全部可散列 artifact 的 SBOM 输入。"""
        return SbomInputs(
            version=version,
            git_commit=COMMIT,
            source_date_epoch=EPOCH,
            python_components=self.python_components,
            requirements_lock=self.artifacts / "requirements-all.lock",
            artifacts=tuple(
                sorted(
                    path
                    for path in self.artifacts.iterdir()
                    if not path.name.endswith((".json", ".txt"))
                )
            ),
            image_digests=tuple(sorted(self.artifacts.glob("lobster0-*-image-*.txt"))),
        )

    def inputs(self, **overrides: object) -> ReleaseInputs:
        """返回默认完整、可按字段覆写的 manifest 输入。"""
        fields: dict[str, object] = {
            "version": VERSION,
            "release_tag": f"v{VERSION}",
            "git_commit": COMMIT,
            "source_date_epoch": EPOCH,
            "node_version": self.node_version,
            "minimum_readable_schema": 1,
            "features": load_features(FEATURES_REGISTRY),
            "artifacts": tuple(sorted(self.artifacts.iterdir())),
            "install_script": self.install_script,
            "runtime_versions": RUNTIME_VERSIONS,
        }
        fields.update(overrides)
        return ReleaseInputs(**fields)  # type: ignore[arg-type]

    def inputs_without(self, kind: str, os_name: str, arch: str) -> ReleaseInputs:
        """返回缺少一个具体平台 artifact 的输入。"""
        dropped = f"-{os_name}-{arch}.tar.gz"
        remaining = tuple(
            path
            for path in sorted(self.artifacts.iterdir())
            if not (path.name.startswith(f"lobster0-{kind}-") and path.name.endswith(dropped))
        )
        return self.inputs(artifacts=remaining)

    def _requirements(self) -> bytes:
        """写入两条全部带 hash 的 locked requirement。"""
        lines = [
            "# This file was autogenerated by uv.",
        ]
        for name, version, _license in PYTHON_PACKAGES:
            digest = hashlib.sha256(f"{name}=={version}".encode()).hexdigest()
            lines.append(f"{name}=={version} \\")
            lines.append(f"    --hash=sha256:{digest}")
        return ("\n".join(lines) + "\n").encode()

    def _python_components(self) -> bytes:
        """写入与 lock 精确对齐的 uv CycloneDX export fixture。"""
        document = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "version": 1,
            "components": [
                {
                    "type": "library",
                    "name": name,
                    "version": version,
                    "purl": f"pkg:pypi/{name}@{version}",
                    "licenses": [{"license": {"id": license_id}}],
                }
                for name, version, license_id in PYTHON_PACKAGES
            ],
        }
        return json.dumps(document, sort_keys=True, indent=2).encode() + b"\n"

    def _installer(self) -> bytes:
        """写入带固定 shebang 的最小 installer zipapp fixture。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("__main__.py", (1980, 1, 1, 0, 0, 0))
            archive.writestr(info, b"raise SystemExit(0)\n")
        return b"#!/usr/bin/env python3\n" + buffer.getvalue()


class ManifestBuildTest(unittest.TestCase):
    """验证 manifest 封闭世界、精确一致性与字节级可复现性。"""

    def setUp(self) -> None:
        """在隔离临时目录构建一整套 Release artifact fixture。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.fixture = ReleaseFixture(self.root)
        self.inputs = self.fixture.inputs()

    def test_manifest_builder_requires_every_tier1_component(self) -> None:
        """缺任一 Tier 1 组件必须以精确平台消息失败。"""
        with self.assertRaisesRegex(ReleaseBuildError, "missing tui artifact for macos-arm64"):
            build_manifest(self.fixture.inputs_without("tui", "macos", "arm64"))
        with self.assertRaisesRegex(ReleaseBuildError, "missing node artifact for linux-arm64"):
            build_manifest(self.fixture.inputs_without("node", "linux", "arm64"))
        for filename, message in (
            (f"lobster0_agent-{VERSION}-py3-none-any.whl", "missing wheel artifact"),
            (f"lobster0_agent-{VERSION}.tar.gz", "missing sdist artifact"),
            ("requirements-all.lock", "missing requirements artifact"),
            ("lobster0-installer.pyz", "missing installer artifact"),
            (f"lobster0-{VERSION}-sbom.spdx.json", "missing sbom artifact for spdx"),
            (
                f"lobster0-{VERSION}-sbom.cyclonedx.json",
                "missing sbom artifact for cyclonedx",
            ),
            (f"lobster0-runtime-image-{VERSION}.txt", "missing runtime-image artifact"),
            (f"lobster0-sandbox-image-{VERSION}.txt", "missing sandbox-image artifact"),
        ):
            remaining = tuple(
                path for path in self.inputs.artifacts if path.name != filename
            )
            with self.assertRaisesRegex(ReleaseBuildError, message):
                build_manifest(self.fixture.inputs(artifacts=remaining))

    def test_same_inputs_produce_byte_identical_manifest_and_checksums(self) -> None:
        """相同输入必须产生逐字节相同的 manifest 与 checksums。"""
        first = build_release_outputs(self.inputs)
        second = build_release_outputs(self.inputs)

        self.assertEqual(first.manifest, second.manifest)
        self.assertEqual(first.checksums, second.checksums)
        self.assertTrue(first.manifest.endswith(b"\n"))
        self.assertNotIn(str(self.root).encode(), first.manifest)
        self.assertNotIn(str(self.root).encode(), first.checksums)

    def test_manifest_order_and_release_facts_are_exact(self) -> None:
        """artifact 顺序、commit/tag/version/schema/features 必须精确一致。"""
        manifest = build_manifest(self.inputs)
        document = json.loads(manifest.decode("utf-8"))

        order = [
            (item["kind"], item["platform"]["os"], item["platform"]["arch"], item["filename"])
            for item in document["artifacts"]
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(order), 16)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["product"], "lobster0")
        self.assertEqual(document["version"], VERSION)
        self.assertEqual(document["git_commit"], COMMIT)
        self.assertEqual(document["python"], "3.12")
        self.assertEqual(document["database_schema"], LATEST_SCHEMA_VERSION)
        self.assertEqual(document["minimum_readable_schema"], 1)
        self.assertEqual(tuple(document["features"]), load_features(FEATURES_REGISTRY))
        self.assertEqual(
            {item["url"].rsplit("/", 2)[-2] for item in document["artifacts"]},
            {f"v{VERSION}"},
        )
        parsed = ReleaseManifest.from_bytes(manifest)
        self.assertEqual(parsed.version, VERSION)
        self.assertEqual(len(parsed.artifacts), 16)

    def test_database_schema_tracks_the_migration_source_of_truth(self) -> None:
        """manifest、strict model 与 JSON schema 必须跟随迁移单一来源。"""
        document = json.loads(build_manifest(self.inputs).decode("utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        self.assertEqual(
            max(storage_migrations._MIGRATION_RESOURCES), LATEST_SCHEMA_VERSION
        )
        self.assertEqual(
            sorted(storage_migrations._MIGRATION_RESOURCES),
            list(range(1, LATEST_SCHEMA_VERSION + 1)),
        )
        self.assertEqual(document["database_schema"], LATEST_SCHEMA_VERSION)
        self.assertEqual(
            install_models._DATABASE_SCHEMA_VERSION,
            LATEST_SCHEMA_VERSION,
            "install/models.py cannot import migrations across the stdlib-only zipapp "
            "boundary, so its literal must be updated with every new migration",
        )
        self.assertEqual(
            schema["properties"]["database_schema"]["const"],
            LATEST_SCHEMA_VERSION,
            "release/manifest.schema.json is static and must be updated with every "
            "new migration",
        )
        self.assertEqual(
            schema["properties"]["minimum_readable_schema"]["maximum"],
            LATEST_SCHEMA_VERSION,
            "minimum_readable_schema 的上界与 database_schema 同源，漏改会让 schema "
            "自相矛盾：允许的最早可读版本上限低于本 Runtime 实际写入的版本",
        )
        self.assertLessEqual(document["minimum_readable_schema"], LATEST_SCHEMA_VERSION)
        ReleaseManifest.from_bytes(build_manifest(self.inputs))

    def test_manifest_rejects_release_identity_disagreement(self) -> None:
        """tag、version、commit 与 schema 边界不一致必须拒绝。"""
        with self.assertRaisesRegex(ReleaseBuildError, "release tag"):
            self.fixture.inputs(release_tag="v0.6.9")
        with self.assertRaisesRegex(ReleaseBuildError, "git commit"):
            self.fixture.inputs(git_commit="not-a-commit")
        with self.assertRaisesRegex(ReleaseBuildError, "version"):
            self.fixture.inputs(version="0.7", release_tag="v0.7")
        with self.assertRaisesRegex(ReleaseBuildError, "minimum readable schema"):
            self.fixture.inputs(minimum_readable_schema=LATEST_SCHEMA_VERSION + 1)
        with self.assertRaisesRegex(ReleaseBuildError, "feature"):
            self.fixture.inputs(features=("agent", "agent"))
        with self.assertRaisesRegex(ReleaseBuildError, "feature"):
            self.fixture.inputs(features=("browser",))

    def test_manifest_rejects_unexpected_and_duplicate_artifacts(self) -> None:
        """未登记文件与重复文件名必须以精确消息拒绝。"""
        stray = self.fixture.artifacts / "lobster0-extra.tar.gz"
        stray.write_bytes(b"stray\n")
        with self.assertRaisesRegex(ReleaseBuildError, "unexpected artifact lobster0-extra"):
            build_manifest(self.fixture.inputs(artifacts=tuple(sorted(
                self.fixture.artifacts.iterdir()
            ))))
        stray.unlink()

        duplicate_root = self.root / "duplicate"
        duplicate_root.mkdir()
        wheel = duplicate_root / f"lobster0_agent-{VERSION}-py3-none-any.whl"
        _write_wheel(wheel)
        with self.assertRaisesRegex(ReleaseBuildError, "duplicate artifact filename"):
            build_manifest(self.fixture.inputs(artifacts=self.inputs.artifacts + (wheel,)))

    def test_manifest_rejects_wrong_wheel_metadata(self) -> None:
        """wheel 的分发名或版本与 Release 不一致必须拒绝。"""
        wheel = self.fixture.artifacts / f"lobster0_agent-{VERSION}-py3-none-any.whl"
        _write_wheel(wheel, name="lobster0")
        with self.assertRaisesRegex(ReleaseBuildError, "wheel metadata mismatch"):
            build_manifest(self.inputs)

    def test_manifest_rejects_requirements_without_hashes(self) -> None:
        """lock 中出现无 hash 的 requirement 必须拒绝。"""
        lock = self.fixture.artifacts / "requirements-all.lock"
        lock.write_text("croniter==6.2.0\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseBuildError, "requirements entry without hash"):
            build_manifest(self.inputs)

    def test_manifest_rejects_node_upstream_hash_mismatch(self) -> None:
        """Node bundle 记录的上游 hash 必须与固定 pin 精确一致。"""
        self.fixture.write_node_bundle("linux-x86_64", "c" * 64)
        with self.assertRaisesRegex(
            ReleaseBuildError, "node upstream hash mismatch for linux-x86_64"
        ):
            build_manifest(self.inputs)

    def test_manifest_rejects_archive_links(self) -> None:
        """Release archive 中出现 link entry 必须拒绝。"""
        self.fixture.write_tui_bundle("linux-x86_64", link="tui/dist/link.js")
        with self.assertRaisesRegex(ReleaseBuildError, "archive contains an unsafe entry"):
            build_manifest(self.inputs)

    def test_manifest_rejects_sbom_subject_mismatch(self) -> None:
        """SBOM 主体版本或 wheel hash 与 Release 不一致必须拒绝。"""
        self.fixture.write_sboms(version="0.6.9")
        with self.assertRaisesRegex(ReleaseBuildError, "sbom subject mismatch"):
            build_manifest(self.inputs)

    def test_manifest_rejects_image_without_digest(self) -> None:
        """GHCR digest 文件必须是精确的 repository@sha256 单行。"""
        digest_file = self.fixture.artifacts / f"lobster0-runtime-image-{VERSION}.txt"
        digest_file.write_text("ghcr.io/nedonion/lobster0:latest\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseBuildError, "image digest missing"):
            build_manifest(self.inputs)

    def test_checksums_cover_trust_root_files_in_sorted_order(self) -> None:
        """SHA256SUMS 必须按文件名排序并覆盖 manifest、install.sh 与全部 artifact。"""
        outputs = build_release_outputs(self.inputs)
        lines = outputs.checksums.decode("utf-8").splitlines()
        names = [line.split("  ", 1)[1] for line in lines]

        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), 18)
        self.assertIn("release-manifest.json", names)
        self.assertIn("install.sh", names)
        self.assertEqual(
            lines[names.index("release-manifest.json")].split("  ", 1)[0],
            hashlib.sha256(outputs.manifest).hexdigest(),
        )
        self.assertEqual(
            lines[names.index("install.sh")].split("  ", 1)[0],
            hashlib.sha256(self.fixture.install_script.read_bytes()).hexdigest(),
        )
        document = json.loads(outputs.manifest.decode("utf-8"))
        manifest_names = {item["filename"] for item in document["artifacts"]}
        self.assertNotIn("install.sh", manifest_names)
        self.assertNotIn("SHA256SUMS", manifest_names)
        self.assertTrue(manifest_names.issubset(set(names)))
        self.assertTrue(outputs.checksums.endswith(b"\n"))

    def test_release_inputs_document_binds_manifest_and_installer(self) -> None:
        """release-inputs.json 必须精确绑定 manifest 与 installer 事实。"""
        outputs = build_release_outputs(self.inputs)
        payload = build_release_inputs_document(outputs.manifest, self.inputs)
        document = json.loads(payload.decode("utf-8"))
        installer = self.fixture.artifacts / "lobster0-installer.pyz"

        self.assertEqual(document["release_tag"], f"v{VERSION}")
        self.assertEqual(document["manifest_filename"], "release-manifest.json")
        self.assertEqual(
            document["manifest_sha256"], hashlib.sha256(outputs.manifest).hexdigest()
        )
        self.assertEqual(document["manifest_size"], len(outputs.manifest))
        self.assertEqual(document["installer_filename"], "lobster0-installer.pyz")
        self.assertEqual(document["installer_size"], installer.stat().st_size)
        self.assertEqual(BootstrapReleaseInputs(**document).release_tag, f"v{VERSION}")

    def test_checksums_require_the_canonical_install_script_name(self) -> None:
        """checksums 只接受名为 install.sh 的 bootstrap 信任根文件。"""
        renamed = self.root / "bootstrap.sh"
        renamed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseBuildError, "install script"):
            build_checksums(b"{}\n", self.fixture.inputs(install_script=renamed))

    def test_checksums_refuse_an_incomplete_release(self) -> None:
        """checksums 与 manifest 共用同一封闭世界完整性门禁。"""
        with self.assertRaisesRegex(ReleaseBuildError, "missing tui artifact for macos-arm64"):
            build_checksums(b"{}\n", self.fixture.inputs_without("tui", "macos", "arm64"))


class SbomBuildTest(unittest.TestCase):
    """验证 CycloneDX/SPDX 输出覆盖全部 locked 与 production 依赖。"""

    def setUp(self) -> None:
        """物化一整套 artifact 以便直接驱动 SBOM builder。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.fixture = ReleaseFixture(self.root)
        self.documents = build_sbom(self.fixture.sbom_inputs())

    def test_sbom_lists_every_locked_and_production_package_once(self) -> None:
        """每个 locked Python 包与 production Node 包必须恰好出现一次。"""
        cyclonedx = json.loads(self.documents.cyclonedx.decode("utf-8"))
        spdx = json.loads(self.documents.spdx.decode("utf-8"))
        purls = [
            component["purl"]
            for component in cyclonedx["components"]
            if "purl" in component
        ]
        spdx_purls = [
            reference["referenceLocator"]
            for package in spdx["packages"]
            for reference in package.get("externalRefs", ())
        ]

        for name, version, _license in PYTHON_PACKAGES:
            self.assertEqual(purls.count(f"pkg:pypi/{name}@{version}"), 1)
            self.assertEqual(spdx_purls.count(f"pkg:pypi/{name}@{version}"), 1)
        for entries in LICENSES.values():
            for entry in entries:
                purl = f"pkg:npm/{entry['name']}@{entry['versions'][0]}"
                self.assertEqual(purls.count(purl), 1)
                self.assertEqual(spdx_purls.count(purl), 1)
        self.assertEqual(len(purls), len(set(purls)))
        self.assertEqual(len(spdx_purls), len(set(spdx_purls)))

    def test_sbom_is_canonical_and_records_artifact_hashes(self) -> None:
        """SBOM 必须字节可复现并携带每个 artifact 的 SHA-256。"""
        again = build_sbom(self.fixture.sbom_inputs())
        cyclonedx = json.loads(self.documents.cyclonedx.decode("utf-8"))
        spdx = json.loads(self.documents.spdx.decode("utf-8"))
        wheel = self.fixture.artifacts / f"lobster0_agent-{VERSION}-py3-none-any.whl"
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        hashes = {
            component["name"]: component["hashes"][0]["content"]
            for component in cyclonedx["components"]
            if "hashes" in component
        }
        spdx_hashes = {
            package["name"]: package["checksums"][0]["checksumValue"]
            for package in spdx["packages"]
            if "checksums" in package
        }

        self.assertEqual(self.documents.cyclonedx, again.cyclonedx)
        self.assertEqual(self.documents.spdx, again.spdx)
        self.assertEqual(hashes[wheel.name], digest)
        self.assertEqual(spdx_hashes[wheel.name], digest)
        self.assertEqual(cyclonedx["specVersion"], "1.5")
        self.assertEqual(spdx["spdxVersion"], "SPDX-2.3")
        self.assertEqual(cyclonedx["metadata"]["component"]["version"], VERSION)
        self.assertTrue(
            any(
                relationship["relationshipType"] == "DESCRIBES"
                for relationship in spdx["relationships"]
            )
        )
        self.assertNotIn(str(self.root).encode(), self.documents.cyclonedx)
        self.assertNotIn(str(self.root).encode(), self.documents.spdx)

    def test_sbom_rejects_python_package_set_mismatch(self) -> None:
        """uv export 与 lock 的包集合不一致必须拒绝。"""
        lock = self.fixture.artifacts / "requirements-all.lock"
        lock.write_text(
            "croniter==6.2.0 \\\n    --hash=sha256:" + "d" * 64 + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(SbomBuildError, "python package set mismatch"):
            build_sbom(self.fixture.sbom_inputs())

    def test_sbom_rejects_divergent_tui_license_inventories(self) -> None:
        """四个平台 TUI bundle 的 production license 清单必须完全一致。"""
        path = self.fixture.artifacts / f"lobster0-tui-{VERSION}-macos-arm64.tar.gz"
        path.unlink()
        _write_tar(
            path,
            {
                "tui/licenses.json": b'{"MIT":[{"name":"other","versions":["1.0.0"]}]}\n',
                "tui/package.json": b"{}\n",
            },
        )
        with self.assertRaisesRegex(SbomBuildError, "license inventory mismatch"):
            build_sbom(self.fixture.sbom_inputs())


class ReleaseInputCollectionTest(unittest.TestCase):
    """验证从 Git 仓库收集 Release 事实的门禁。"""

    def setUp(self) -> None:
        """构造一个带 v0.7.0 tag 的离线临时仓库。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.fixture = ReleaseFixture(self.root)
        (self.repository / "src/lobster0").mkdir(parents=True)
        (self.repository / "release").mkdir(parents=True)
        (self.repository / "src/lobster0/_version.py").write_text(
            f'__version__: str = "{VERSION}"\n', encoding="utf-8"
        )
        (self.repository / "release/features.json").write_bytes(
            FEATURES_REGISTRY.read_bytes()
        )
        (self.repository / "release/runtime-versions.json").write_bytes(
            RUNTIME_VERSIONS.read_bytes()
        )
        self._git("init", "--quiet", "--initial-branch=main")
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "release fixture")
        self._git("tag", f"v{VERSION}")
        self.commit_epoch = int(
            self._git("show", "-s", "--format=%ct", f"v{VERSION}^{{commit}}").strip()
        )

    def _git(self, *arguments: str) -> str:
        """在固定身份与时间下运行一个受控 git 子命令。"""
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Lobster0 Release",
                "GIT_AUTHOR_EMAIL": "release@example.invalid",
                "GIT_COMMITTER_NAME": "Lobster0 Release",
                "GIT_COMMITTER_EMAIL": "release@example.invalid",
                "GIT_AUTHOR_DATE": f"{EPOCH} +0000",
                "GIT_COMMITTER_DATE": f"{EPOCH} +0000",
                "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
                "GIT_CONFIG_SYSTEM": str(self.root / "gitconfig"),
            }
        )
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            shell=False,
        )
        return completed.stdout

    def test_collect_requires_clean_tree_and_matching_source_date_epoch(self) -> None:
        """脏工作区、错 tag 时间与错版本必须拒绝，干净仓库产生精确事实。"""
        inputs = collect_release_inputs(
            self.repository,
            self.fixture.artifacts,
            self.fixture.install_script,
            {"SOURCE_DATE_EPOCH": str(self.commit_epoch)},
        )
        self.assertEqual(inputs.version, VERSION)
        self.assertEqual(inputs.release_tag, f"v{VERSION}")
        self.assertEqual(inputs.source_date_epoch, self.commit_epoch)
        self.assertEqual(len(inputs.artifacts), 16)

        with self.assertRaisesRegex(ReleaseBuildError, "SOURCE_DATE_EPOCH"):
            collect_release_inputs(
                self.repository,
                self.fixture.artifacts,
                self.fixture.install_script,
                {"SOURCE_DATE_EPOCH": str(self.commit_epoch + 1)},
            )
        with self.assertRaisesRegex(ReleaseBuildError, "SOURCE_DATE_EPOCH"):
            collect_release_inputs(
                self.repository, self.fixture.artifacts, self.fixture.install_script, {}
            )

        (self.repository / "src/lobster0/_version.py").write_text(
            '__version__: str = "0.7.0"\n# dirty\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ReleaseBuildError, "git worktree is not clean"):
            collect_release_inputs(
                self.repository,
                self.fixture.artifacts,
                self.fixture.install_script,
                {"SOURCE_DATE_EPOCH": str(self.commit_epoch)},
            )

    def test_collect_rejects_tag_that_is_not_head(self) -> None:
        """tag 未指向 HEAD 的候选提交必须拒绝。"""
        self._git("commit", "--quiet", "--allow-empty", "-m", "after tag")
        with self.assertRaisesRegex(ReleaseBuildError, "release tag"):
            collect_release_inputs(
                self.repository,
                self.fixture.artifacts,
                self.fixture.install_script,
                {"SOURCE_DATE_EPOCH": str(self.commit_epoch)},
            )


class VerifyReleaseTest(unittest.TestCase):
    """验证独立 verifier 能重新发现 builder 输出中的任何漂移。"""

    def setUp(self) -> None:
        """构建一份完整候选 Release 并落盘 manifest 与 checksums。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.fixture = ReleaseFixture(self.root)
        outputs = build_release_outputs(self.fixture.inputs())
        self.manifest = self.root / "release-manifest.json"
        self.checksums = self.root / "SHA256SUMS"
        self.manifest.write_bytes(outputs.manifest)
        self.checksums.write_bytes(outputs.checksums)

    def _verify(self) -> tuple[str, ...]:
        """用磁盘上的文件独立复核一次候选 Release。"""
        return verify_release(
            manifest=self.manifest,
            schema=SCHEMA,
            artifact_directory=self.fixture.artifacts,
            checksums=self.checksums,
            install_script=self.fixture.install_script,
        )

    def test_verifier_accepts_a_complete_candidate_release(self) -> None:
        """完整候选 Release 必须通过 schema、hash 与 checksums 复核。"""
        report = self._verify()
        self.assertIn(f"manifest {VERSION} verified", report)
        self.assertIn("artifacts 16 verified", report)
        self.assertIn("checksums 18 verified", report)

    def test_verifier_rejects_tampered_artifact_and_broken_schema(self) -> None:
        """任一 artifact 被改写或 manifest 违反 schema 都必须失败。"""
        wheel = self.fixture.artifacts / f"lobster0_agent-{VERSION}-py3-none-any.whl"
        wheel.write_bytes(wheel.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ReleaseVerifyError, "artifact hash mismatch"):
            self._verify()

        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        document["database_schema"] = 4
        self.manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseVerifyError, "manifest schema violation"):
            self._verify()


class FeatureRegistryTest(unittest.TestCase):
    """验证 features 注册表只声明已实现且可断言的能力。"""

    def test_registry_maps_each_feature_to_one_assertion(self) -> None:
        """每个 feature 必须在 schema 枚举内且恰好一条 import 断言。"""
        document = json.loads(FEATURES_REGISTRY.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        allowed = set(schema["properties"]["features"]["items"]["enum"])
        names = [entry["feature"] for entry in document["features"]]

        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(set(names).issubset(allowed))
        self.assertNotIn("evolution", names)
        for entry in document["features"]:
            self.assertEqual(set(entry), {"feature", "module", "attribute"})
            self.assertTrue(entry["module"].startswith("lobster0."))
            self.assertTrue(entry["attribute"].isidentifier())
        self.assertEqual(load_features(FEATURES_REGISTRY), tuple(names))


if __name__ == "__main__":
    unittest.main()
