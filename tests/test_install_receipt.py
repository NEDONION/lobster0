"""验证 owner-only install receipt 与 managed-file ownership hash。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miniclaw.install import receipt as receipt_module
from miniclaw.install.models import InstallError, PlatformKey
from miniclaw.install.receipt import (
    InstallReceipt,
    managed_file_sha256,
    verify_managed_file,
)


class InstallReceiptTests(unittest.TestCase):
    """覆盖 receipt strict JSON、atomic write 与 no-follow hash。"""

    def setUp(self) -> None:
        """创建 owner-only receipt 目录。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.path = self.root / "install-receipt.json"

    def tearDown(self) -> None:
        """清理临时文件。"""
        self.temporary.cleanup()

    def receipt(self, **changes: object) -> InstallReceipt:
        """构造最小有效 receipt。"""
        values: dict[str, object] = {
            "schema_version": 1,
            "version": "0.7.0",
            "git_commit": "a" * 40,
            "platform": PlatformKey("linux", "x86_64"),
            "installed_at": "2026-08-10T01:02:03Z",
            "managed_files": (("bin/miniclaw", "b" * 64),),
            "current_runtime": "runtimes/0.7.0",
            "previous_runtime": None,
            "service_label": None,
            "service_file": None,
            "service_file_sha256": None,
        }
        values.update(changes)
        return InstallReceipt(**values)  # type: ignore[arg-type]

    def test_receipt_round_trip_is_exact_private_and_platform_bound(self) -> None:
        """开放 schema、宽松类型/权限或平台漂移会信任伪造 ownership。"""
        expected = self.receipt()
        expected.write(self.path)

        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(document),
            {
                "schema_version",
                "version",
                "git_commit",
                "platform",
                "installed_at",
                "managed_files",
                "current_runtime",
                "previous_runtime",
                "service_label",
                "service_file",
                "service_file_sha256",
            },
        )
        self.assertEqual(stat.S_IMODE(self.path.lstat().st_mode), 0o600)
        self.assertEqual(
            InstallReceipt.load(
                self.path,
                expected_uid=os.geteuid(),
                expected_platform=PlatformKey("linux", "x86_64"),
            ),
            expected,
        )
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            InstallReceipt.load(
                self.path,
                expected_platform=PlatformKey("macos", "x86_64"),
            )

    def test_receipt_rejects_unknown_duplicate_wrong_type_and_corrupt_json(self) -> None:
        """宽松 JSON 会接受未来/重复字段或 bool-as-int 并扩大删除集合。"""
        valid = json.loads(self.receipt().to_bytes())
        payloads = (
            b"not-json",
            json.dumps({**valid, "unknown": True}).encode(),
            json.dumps({**valid, "schema_version": True}).encode(),
            self.receipt().to_bytes().replace(
                b'"schema_version":1', b'"schema_version":1,"schema_version":1'
            ),
        )
        for payload in payloads:
            self.path.write_bytes(payload)
            self.path.chmod(0o600)
            with self.subTest(payload=payload), self.assertRaisesRegex(
                InstallError, "uninstall_ownership_mismatch"
            ):
                InstallReceipt.load(self.path)

    def test_receipt_rejects_wrong_uid_mode_symlink_and_hash(self) -> None:
        """receipt 必须由预期 owner 持有且为 no-follow regular 0600。"""
        self.receipt().write(self.path)
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            InstallReceipt.load(self.path, expected_uid=os.geteuid() + 1)
        self.path.chmod(0o640)
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            InstallReceipt.load(self.path)
        self.path.unlink()
        target = self.root / "target.json"
        self.receipt().write(target)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            InstallReceipt.load(self.path)

        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            self.receipt(managed_files=(("bin/miniclaw", "A" * 64),))

    def test_receipt_allows_only_relative_nonsensitive_managed_paths(self) -> None:
        """绝对/逃逸路径或用户数据路径进入 receipt 会让 uninstall 可删除用户数据。"""
        unsafe = (
            "/usr/local/bin/miniclaw",
            "../bin/miniclaw",
            "config.toml",
            "secrets.env",
            "miniclaw.db",
            "memory/MEMORY.md",
            "skills/tool/SKILL.md",
            "workspace/file",
            "logs/gateway.log",
            "bin/bad\nname",
        )
        for path in unsafe:
            with self.subTest(path=path), self.assertRaisesRegex(
                InstallError, "uninstall_ownership_mismatch"
            ):
                self.receipt(managed_files=((path, "b" * 64),))

    def test_service_receipt_fields_are_all_or_none_and_relative(self) -> None:
        """未绑定 label/path/hash 的 service 文件不得获得 managed ownership。"""
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            self.receipt(service_label="io.miniclaw.gateway")
        service = self.receipt(
            service_label="io.miniclaw.gateway",
            service_file="services/io.miniclaw.gateway.plist",
            service_file_sha256="c" * 64,
        )
        self.assertEqual(service.service_label, "io.miniclaw.gateway")

    def test_managed_hash_is_no_follow_for_regular_and_relative_symlink(self) -> None:
        """跟随 symlink 会把目标内容而非受管 link identity 当作 ownership。"""
        regular = self.root / "launcher"
        regular.write_bytes(b"launcher")
        link = self.root / "command"
        link.symlink_to("launcher")

        regular_hash = managed_file_sha256(regular)
        link_hash = managed_file_sha256(link)

        self.assertNotEqual(regular_hash, link_hash)
        verify_managed_file(regular, regular_hash)
        verify_managed_file(link, link_hash)
        link.unlink()
        link.symlink_to("other")
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            verify_managed_file(link, link_hash)

    def test_managed_hash_rejects_special_control_and_user_data_paths(self) -> None:
        """special file 读取可阻塞，控制 link 或用户数据 hash 会污染 receipt。"""
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            managed_file_sha256(fifo)
        control = self.root / "control"
        control.symlink_to("bad\nlink")
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            managed_file_sha256(control)
        secrets = self.root / "secrets.env"
        secrets.write_text("SECRET=value", encoding="utf-8")
        with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
            managed_file_sha256(secrets)

    def test_atomic_write_failure_cleans_temp_and_preserves_original(self) -> None:
        """replace 前异常不得泄漏 temp，replace 后 durability 异常必须恢复旧 receipt。"""
        original = self.receipt()
        original.write(self.path)
        original_bytes = self.path.read_bytes()
        updated = self.receipt(version="0.8.0", current_runtime="runtimes/0.8.0")

        with mock.patch.object(receipt_module.os, "replace", side_effect=OSError("CRASH")):
            with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
                updated.write(self.path)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.root.iterdir()))

        calls = 0
        real_fsync_directory = receipt_module._fsync_directory

        def fail_once(path: Path) -> None:
            """仅模拟新 receipt replace 后第一次 parent fsync crash。"""
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("CRASH_AFTER_REPLACE")
            real_fsync_directory(path)

        with mock.patch.object(receipt_module, "_fsync_directory", side_effect=fail_once):
            with self.assertRaisesRegex(InstallError, "uninstall_ownership_mismatch"):
                updated.write(self.path)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
