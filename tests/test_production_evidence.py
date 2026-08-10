"""生产 Live Evidence 的共享文件与隐私边界测试。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniclaw.evals.production_evidence import (
    ProductionEvidenceError,
    scan_secret_matches,
    utc_timestamp,
    validate_commit,
    write_private_json,
)


class ProductionEvidenceTest(unittest.TestCase):
    """验证共享 primitive 不接受泄密字段或不安全文件目标。"""

    def setUp(self) -> None:
        """创建 owner-only 临时目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_commit_timestamp_and_json_are_strict(self) -> None:
        """commit、UTC 和 JSON 都必须是可复核的标准值。"""
        self.assertEqual(validate_commit("A" * 40), "a" * 40)
        self.assertRegex(utc_timestamp(), r"^\d{4}-\d{2}-\d{2}T.*Z$")
        for value in ("dirty", "a" * 39, "g" * 40):
            with self.subTest(value=value), self.assertRaises(ProductionEvidenceError):
                validate_commit(value)

        for payload in (
            {"message_content": "private"},
            {"chat_id": "private"},
            {"nested": {"workspace_path": "/private"}},
            {"value": float("nan")},
            {"value": object()},
        ):
            with self.subTest(payload=payload), self.assertRaises(ProductionEvidenceError):
                write_private_json(self.root / "unsafe.json", payload)

    def test_write_is_owner_only_exclusive_no_follow_and_cleans_fsync_failure(self) -> None:
        """Evidence 只能新建为 0600，不能覆盖或跟随 symlink。"""
        target = self.root / "evidence.json"
        write_private_json(target, {"schema_version": 1, "count": 1})

        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o077, 0)
        with self.assertRaisesRegex(ProductionEvidenceError, "evidence_already_exists"):
            write_private_json(target, {"schema_version": 1})

        link = self.root / "link.json"
        link.symlink_to(target)
        with self.assertRaises(ProductionEvidenceError):
            write_private_json(link, {"schema_version": 1})

        failed = self.root / "failed.json"
        with patch("miniclaw.evals.production_evidence.os.fsync", side_effect=OSError):
            with self.assertRaisesRegex(ProductionEvidenceError, "evidence_write_failed"):
                write_private_json(failed, {"schema_version": 1})
        self.assertFalse(failed.exists())

    def test_secret_scan_is_bounded_and_skips_symlink_large_and_non_regular(self) -> None:
        """扫描只统计前 1000 个普通小文件中的 exact match。"""
        scan_root = self.root / "scan"
        scan_root.mkdir(mode=0o700)
        secrets = ("MODEL_SECRET_SENTINEL", "CHANNEL_SECRET_SENTINEL")
        (scan_root / "0000.txt").write_text("\n".join(secrets), encoding="utf-8")
        large = scan_root / "large.txt"
        large.write_bytes(b"x" * (1024 * 1024 + 1) + secrets[0].encode())
        external = self.root / "external.txt"
        external.write_text(secrets[0], encoding="utf-8")
        (scan_root / "linked.txt").symlink_to(external)

        self.assertEqual(scan_secret_matches((scan_root,), secrets), 2)

        limited = self.root / "limited"
        limited.mkdir(mode=0o700)
        for index in range(1001):
            content = secrets[0] if index == 1000 else "safe"
            (limited / f"{index:04d}.txt").write_text(content, encoding="utf-8")
        self.assertEqual(scan_secret_matches((limited,), (secrets[0],)), 0)

        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        self.assertEqual(scan_secret_matches((fifo,), secrets), 0)

    def test_secret_scan_rejects_invalid_or_unbounded_needles(self) -> None:
        """Secret needle 必须是有界非空字符串。"""
        for secrets in (("",), ("x" * 4097,), (1,)):
            with self.subTest(secrets=secrets), self.assertRaisesRegex(
                ProductionEvidenceError, "invalid_secret_scan"
            ):
                scan_secret_matches((self.root,), secrets)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
