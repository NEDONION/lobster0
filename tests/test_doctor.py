"""MiniClaw 离线本地诊断的行为测试。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.paths import build_state_paths


class DoctorTest(unittest.TestCase):
    """验证 doctor 检查真实状态、保持只读且不泄露配置内容。"""

    def setUp(self) -> None:
        """为每个诊断场景创建独立状态路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())

    def test_initialized_state_passes_all_local_checks(self) -> None:
        """完整初始化后五项本地检查都应实际通过。"""
        initialize_state(self.paths)

        results = run_local_checks(self.paths, {})

        self.assertEqual(
            {result.name for result in results},
            {"state_home", "config", "workspace", "database", "permissions"},
        )
        self.assertTrue(all(result.status is CheckStatus.PASS for result in results))

    def test_corrupt_config_fails_without_exposing_file_contents(self) -> None:
        """损坏配置必须失败，诊断消息不能回显其中的密钥样例。"""
        initialize_state(self.paths)
        self.paths.config.write_text(
            '[provider\napi_key = "super-secret-value"\n',
            encoding="utf-8",
        )

        results = run_local_checks(self.paths, {})
        config_result = next(result for result in results if result.name == "config")

        self.assertIs(config_result.status, CheckStatus.FAIL)
        self.assertNotIn("super-secret-value", config_result.message)

    def test_missing_state_is_reported_without_creating_it(self) -> None:
        """doctor 是只读检查，不能为了诊断而创建不存在的状态目录。"""
        missing_paths = build_state_paths(self.paths.home / "missing")

        results = run_local_checks(missing_paths, {})

        self.assertTrue(any(result.status is CheckStatus.FAIL for result in results))
        self.assertFalse(missing_paths.home.exists())

    def test_permissive_config_mode_fails_permission_check(self) -> None:
        """配置对 group 或 other 可读时必须报告权限失败。"""
        initialize_state(self.paths)
        self.paths.config.chmod(0o644)

        results = run_local_checks(self.paths, {})
        permission_result = next(result for result in results if result.name == "permissions")

        self.assertIs(permission_result.status, CheckStatus.FAIL)


if __name__ == "__main__":
    unittest.main()
