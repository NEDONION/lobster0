"""MiniClaw 状态路径的行为测试。"""

import tempfile
import unittest
from pathlib import Path
from stat import S_IMODE

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
        self.assertEqual(paths.browser, home / "browser")
        self.assertEqual(paths.artifacts, home / "artifacts")
        self.assertEqual(paths.downloads, home / "downloads")
        self.assertFalse(paths.browser.is_relative_to(paths.workspace))
        self.assertTrue(all(path == home or home in path.parents for path in paths.directories))

    def test_browser_roots_are_private_and_outside_workspace(self) -> None:
        """Browser、Artifact 与 Download 目录必须私有且不落入 Agent Workspace。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            for path in paths.directories:
                path.mkdir(mode=0o700, parents=True, exist_ok=True)

            for path in (paths.browser, paths.artifacts, paths.downloads):
                self.assertEqual(S_IMODE(path.stat().st_mode), 0o700)
                self.assertFalse(path.is_relative_to(paths.workspace))

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
