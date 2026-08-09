"""Managed Node 与 pi-tui Release bundle 的可复现和隔离测试。"""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.build_node_bundle import NodeBundleError, build_node_bundle
from scripts.build_tui_bundle import TuiBundleError, build_tui_bundle, materialize_tree

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "macos-arm64"


class NodeBundleTest(unittest.TestCase):
    """验证官方 Node 输入只产生精确、最小、确定性的 managed bundle。"""

    def setUp(self) -> None:
        """创建隔离的上游 archive、pins 与输出目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.archive = self.root / "node-v24.18.0-darwin-arm64.tar.gz"
        self.pins = self.root / "runtime-versions.json"
        self._write_archive("v24.18.0")
        self._write_pins(hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def _write_archive(self, reported_version: str, *, node_type: bytes | None = None) -> None:
        """写入带真实可执行 version probe 的最小官方 archive fixture。"""
        program = (
            "#!/bin/sh\n"
            "if [ \"${1-}\" = --version ]; then "
            f"printf '%s\\n' '{reported_version}'; exit 0; fi\n"
            "exit 2\n"
        ).encode()
        with tarfile.open(self.archive, "w:gz") as archive:
            license_member = tarfile.TarInfo("node-v24.18.0-darwin-arm64/LICENSE")
            license_member.size = len(b"Node license\n")
            license_member.mode = 0o644
            archive.addfile(license_member, io.BytesIO(b"Node license\n"))
            node_member = tarfile.TarInfo("node-v24.18.0-darwin-arm64/bin/node")
            node_member.mode = 0o755
            if node_type is None:
                node_member.size = len(program)
                archive.addfile(node_member, io.BytesIO(program))
            else:
                node_member.type = node_type
                node_member.linkname = "../../outside"
                archive.addfile(node_member)

    def _write_pins(self, digest: str) -> None:
        """写入与 fixture archive 精确绑定的最小 runtime pins。"""
        document = {
            "node": {
                "version": "24.18.0",
                "archives": {
                    PLATFORM: {
                        "url": (
                            "https://nodejs.org/dist/v24.18.0/"
                            "node-v24.18.0-darwin-arm64.tar.gz"
                        ),
                        "sha256": digest,
                    }
                },
            }
        }
        self.pins.write_text(json.dumps(document), encoding="utf-8")

    def test_node_bundle_is_minimal_exact_and_byte_reproducible(self) -> None:
        """相同已校验上游输入必须产生相同 gzip bytes 与精确 Node 版本。"""
        first = build_node_bundle(self.pins, self.archive, PLATFORM, self.root / "a")
        second = build_node_bundle(self.pins, self.archive, PLATFORM, self.root / "b")

        self.assertEqual(first.name, "miniclaw-node-24.18.0-macos-arm64.tar.gz")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.read_bytes()[4:8], b"\0\0\0\0")
        with tarfile.open(first, "r:gz") as bundle:
            members = bundle.getmembers()
            self.assertEqual(
                [member.name for member in members],
                [
                    "node",
                    "node/LICENSE",
                    "node/bin",
                    "node/bin/node",
                    "node/release-component.json",
                ],
            )
            self.assertTrue(all(member.isreg() or member.isdir() for member in members))
            self.assertTrue(
                all(member.uid == member.gid == member.mtime == 0 for member in members)
            )
            bundle.extractall(self.root / "unpacked", filter="data")
        completed = subprocess.run(
            [str(self.root / "unpacked/node/bin/node"), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((completed.returncode, completed.stdout), (0, "v24.18.0\n"))
        component = json.loads(
            (self.root / "unpacked/node/release-component.json").read_text(encoding="utf-8")
        )
        self.assertEqual(component["version"], "24.18.0")
        self.assertEqual(
            component["upstream_sha256"],
            hashlib.sha256(self.archive.read_bytes()).hexdigest(),
        )

    def test_node_bundle_rejects_hash_mismatch_before_extracting(self) -> None:
        """Runtime pin 与 archive bytes 不匹配时不得产生 artifact。"""
        self._write_pins("0" * 64)

        with self.assertRaisesRegex(NodeBundleError, "archive hash mismatch"):
            build_node_bundle(self.pins, self.archive, PLATFORM, self.root / "output")

        self.assertFalse((self.root / "output").exists())

    def test_node_bundle_rejects_linked_executable_and_wrong_version(self) -> None:
        """Node executable 必须为 regular file 且报告 manifest 精确补丁版。"""
        self._write_archive("v24.18.0", node_type=tarfile.SYMTYPE)
        self._write_pins(hashlib.sha256(self.archive.read_bytes()).hexdigest())
        with self.assertRaisesRegex(NodeBundleError, "regular"):
            build_node_bundle(self.pins, self.archive, PLATFORM, self.root / "linked")

        self._write_archive("v24.17.0")
        self._write_pins(hashlib.sha256(self.archive.read_bytes()).hexdigest())
        with self.assertRaisesRegex(NodeBundleError, "version mismatch"):
            build_node_bundle(self.pins, self.archive, PLATFORM, self.root / "wrong-version")


class TuiBundleTest(unittest.TestCase):
    """验证 production deploy 在 checkout 与全局 module 隔离后仍可真实 smoke。"""

    @classmethod
    def setUpClass(cls) -> None:
        """用真实 pnpm production deploy 构建两份可复现 bundle。"""
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary_directory.name)
        cls.first = build_tui_bundle(ROOT / "tui", cls.root / "a", PLATFORM, "0.7.0")
        cls.second = build_tui_bundle(ROOT / "tui", cls.root / "b", PLATFORM, "0.7.0")

    @classmethod
    def tearDownClass(cls) -> None:
        """删除 bundle 与解包目录。"""
        cls.temporary_directory.cleanup()

    def test_tui_bundle_has_no_link_dev_cache_or_dev_dependency(self) -> None:
        """Bundle 仅含 regular production tree、license inventory 与编译入口。"""
        with tarfile.open(self.first, "r:gz") as archive:
            members = archive.getmembers()
            names = {member.name for member in members}
        self.assertTrue(all(member.isreg() or member.isdir() for member in members))
        self.assertTrue(all(member.uid == member.gid == member.mtime == 0 for member in members))
        self.assertIn("tui/dist/main.js", names)
        self.assertIn("tui/licenses.json", names)
        self.assertIn("tui/node_modules/@earendil-works/pi-tui/package.json", names)
        forbidden = (".pnpm-store", "/.pnpm/", "/typescript/", "/@types/", ".modules.yaml")
        self.assertFalse(any(any(token in name for token in forbidden) for name in names))

    def test_tui_bundle_is_byte_reproducible(self) -> None:
        """不同 staging/output 目录不得改变 gzip bytes。"""
        self.assertEqual(self.first.name, "miniclaw-tui-0.7.0-macos-arm64.tar.gz")
        self.assertEqual(self.first.read_bytes(), self.second.read_bytes())
        self.assertEqual(self.first.read_bytes()[4:8], b"\0\0\0\0")

    def test_unpacked_tui_smoke_uses_only_managed_paths(self) -> None:
        """隐藏 checkout、PATH 与全局 modules 后，真实 pi-tui import 仍应成功。"""
        unpacked = self.root / "unpacked"
        with tarfile.open(self.first, "r:gz") as archive:
            archive.extractall(unpacked, filter="data")
        managed_node = unpacked / "node/bin/node"
        managed_node.parent.mkdir(parents=True)
        system_node = shutil.which("node")
        if system_node is None:
            self.skipTest("Node is required for the real bundle smoke")
        shutil.copyfile(Path(system_node).resolve(), managed_node)
        managed_node.chmod(0o755)
        home = unpacked / "home"
        home.mkdir()
        entry = unpacked / "tui/dist/main.js"
        completed = subprocess.run(
            [str(managed_node), str(entry), "--smoke"],
            cwd=self.root,
            env={
                "MINICLAW_HOME": str(home),
                "MINICLAW_NODE": str(managed_node),
                "MINICLAW_PYTHON": sys.executable,
                "MINICLAW_TUI_ENTRY": str(entry),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout,
            '{"component":"pi-tui","version":"0.7.0","status":"ok"}\n',
        )
        self.assertEqual(completed.stderr, "")

    def test_license_inventory_contains_only_production_packages(self) -> None:
        """License inventory 必须保留真实 prod 包且不泄漏 checkout path/dev 包。"""
        unpacked = self.root / "licenses"
        with tarfile.open(self.first, "r:gz") as archive:
            archive.extract("tui/licenses.json", unpacked, filter="data")
        licenses = (unpacked / "tui/licenses.json").read_text(encoding="utf-8")
        self.assertIn("@earendil-works/pi-tui", licenses)
        self.assertNotIn("typescript", licenses)
        self.assertNotIn("@types/node", licenses)
        self.assertNotIn(str(ROOT), licenses)
        json.loads(licenses)


class SymlinkMaterializationTest(unittest.TestCase):
    """验证 deploy tree 的 link 只能解析为 staging 内 regular 内容。"""

    def setUp(self) -> None:
        """创建隔离的 source/destination/outside 目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "destination"

    def test_internal_symlink_is_materialized_as_regular_content(self) -> None:
        """合法内部 link 的最终 artifact 不得保留 link metadata。"""
        package = self.source / "store/package"
        package.mkdir(parents=True)
        (package / "index.js").write_text("export const ok = true;\n", encoding="utf-8")
        modules = self.source / "node_modules"
        modules.mkdir()
        (modules / "package").symlink_to("../store/package", target_is_directory=True)

        materialize_tree(self.source, self.destination)

        deployed = self.destination / "node_modules/package"
        self.assertTrue((deployed / "index.js").is_file())
        self.assertFalse(any(path.is_symlink() for path in self.destination.rglob("*")))

    def test_escape_and_cycle_symlinks_are_rejected(self) -> None:
        """任一逃逸或循环 link 都必须让整个 materialization fail closed。"""
        outside = self.root / "outside"
        outside.write_text("secret", encoding="utf-8")
        (self.source / "escape").symlink_to(outside)
        with self.assertRaisesRegex(TuiBundleError, "symlink"):
            materialize_tree(self.source, self.destination)

        (self.source / "escape").unlink()
        (self.source / "a").symlink_to("b")
        (self.source / "b").symlink_to("a")
        with self.assertRaisesRegex(TuiBundleError, "symlink"):
            materialize_tree(self.source, self.destination)


if __name__ == "__main__":
    unittest.main()
