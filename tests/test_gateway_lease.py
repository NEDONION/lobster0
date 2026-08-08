"""Gateway 单实例 lease 与本地 provenance 测试。"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from miniclaw.gateway_lease import GatewayLease, GatewayLeaseError


class GatewayLeaseTest(unittest.TestCase):
    """验证同一状态目录只允许一个 Gateway 持有运行 lease。"""

    def setUp(self) -> None:
        """创建 owner-only 临时运行目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.root.chmod(0o700)
        self.path = self.root / "gateway.lock"

    def test_second_lease_fails_closed_until_first_releases(self) -> None:
        """同一路径的第二个活跃 Gateway 必须在首个释放前被拒绝。"""
        first = GatewayLease.acquire(self.path, commit="a" * 40)
        self.addCleanup(first.close)

        with self.assertRaises(GatewayLeaseError) as raised:
            GatewayLease.acquire(self.path, commit="a" * 40)

        self.assertEqual(raised.exception.code, "gateway_already_running")
        self.assertEqual(str(raised.exception), "gateway_already_running")
        self.assertNotIn(str(self.path), str(raised.exception))
        first.close()
        second = GatewayLease.acquire(self.path, commit="b" * 40)
        second.close()

    def test_payload_is_private_bounded_and_contains_only_local_provenance(self) -> None:
        """lease 文件只保存 PID、UTC 和 commit，且权限必须为 0600。"""
        lease = GatewayLease.acquire(self.path, commit="c" * 40)
        self.addCleanup(lease.close)
        payload = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(
            set(payload),
            {"schema_version", "pid", "started_at", "commit"},
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["commit"], "c" * 40)
        self.assertRegex(
            payload["started_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
        )
        self.assertEqual(lease.provenance.pid, os.getpid())
        self.assertEqual(lease.provenance.commit, "c" * 40)
        self.assertLess(self.path.stat().st_size, 512)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_stale_file_is_reused_and_unknown_commit_is_explicit(self) -> None:
        """残留普通文件不等于活锁，重新获取后应覆盖旧内容。"""
        self.path.write_text("PRIVATE_STALE_SENTINEL", encoding="utf-8")
        self.path.chmod(0o600)

        lease = GatewayLease.acquire(self.path, commit="not-a-commit")
        self.addCleanup(lease.close)
        payload = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(payload["commit"], "unknown")
        self.assertNotIn("PRIVATE_STALE_SENTINEL", self.path.read_text(encoding="utf-8"))
        lease.close()
        lease.close()

    def test_symlink_directory_and_wide_permissions_fail_closed(self) -> None:
        """symlink、目录和 group/world 可读旧文件不能成为 lease。"""
        outside = self.root / "outside"
        outside.write_text("must-not-change", encoding="utf-8")
        self.path.symlink_to(outside)
        with self.assertRaises(GatewayLeaseError) as symlink_error:
            GatewayLease.acquire(self.path, commit="d" * 40)
        self.assertEqual(symlink_error.exception.code, "gateway_lease_unsafe")
        self.assertEqual(outside.read_text(encoding="utf-8"), "must-not-change")
        self.path.unlink()

        self.path.mkdir()
        with self.assertRaises(GatewayLeaseError) as directory_error:
            GatewayLease.acquire(self.path, commit="d" * 40)
        self.assertEqual(directory_error.exception.code, "gateway_lease_unsafe")
        self.path.rmdir()

        self.path.write_text("stale", encoding="utf-8")
        self.path.chmod(0o640)
        with self.assertRaises(GatewayLeaseError) as permission_error:
            GatewayLease.acquire(self.path, commit="d" * 40)
        self.assertEqual(permission_error.exception.code, "gateway_lease_unsafe")


if __name__ == "__main__":
    unittest.main()
