"""MiniClaw 状态路径的行为测试。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.paths import PathConfigurationError, build_state_paths, resolve_home


class StatePathsTest(unittest.TestCase):
    """验证状态根目录的优先级、绝对路径约束和派生结果。"""

    def test_explicit_home_overrides_environment(self) -> None:
        """显式目录必须优先于环境变量，避免 CLI 参数被静默忽略。"""
        with tempfile.TemporaryDirectory() as explicit_directory:
            resolved = resolve_home(
                explicit_directory,
                {"MINICLAW_HOME": "/ignored/by/explicit/value"},
            )

        self.assertEqual(resolved, Path(explicit_directory).resolve())

    def test_environment_home_is_expanded_and_all_paths_stay_under_it(self) -> None:
        """环境目录派生出的文件和目录必须全部位于同一个状态根下。"""
        with tempfile.TemporaryDirectory() as directory:
            home = resolve_home(None, {"MINICLAW_HOME": directory})
            paths = build_state_paths(home)

        self.assertEqual(home, Path(directory).resolve())
        self.assertEqual(paths.database, home / "miniclaw.db")
        self.assertEqual(paths.workspace, home / "workspace")
        self.assertTrue(all(path == home or home in path.parents for path in paths.directories))

    def test_default_home_uses_dot_miniclaw(self) -> None:
        """没有覆盖值时应使用当前用户主目录下的固定状态目录。"""
        resolved = resolve_home(None, {})

        self.assertEqual(resolved, (Path.home() / ".miniclaw").resolve())

    def test_relative_home_is_rejected(self) -> None:
        """相对状态目录必须失败，避免不同工作目录产生不同数据位置。"""
        with self.assertRaisesRegex(PathConfigurationError, "absolute"):
            resolve_home("relative/state", {})


if __name__ == "__main__":
    unittest.main()
