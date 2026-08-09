"""验证 MiniClaw 的 Python 分发元数据契约。"""

import tomllib
import unittest
from pathlib import Path

from miniclaw import __version__


class PackageMetadataTest(unittest.TestCase):
    """验证公开分发名、版本来源与可选依赖保持稳定。"""

    def test_version_has_one_python_source(self) -> None:
        """版本应由唯一 Python 模块提供给构建元数据和运行时。"""
        metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(metadata["project"]["name"], "miniclaw-agent")
        self.assertEqual(metadata["project"]["dynamic"], ["version"])
        self.assertNotIn("version", metadata["project"])
        self.assertEqual(__version__, "0.7.0")

    def test_public_names_and_complete_extras_do_not_change(self) -> None:
        """CLI 名称和完整渠道依赖集合应保持公开兼容。"""
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
        optional_dependencies = project["optional-dependencies"]

        self.assertEqual(project["scripts"]["miniclaw"], "miniclaw.cli:main")
        self.assertIn("all", optional_dependencies)
        self.assertEqual(
            set(optional_dependencies["all"]),
            set(optional_dependencies["channels"]),
        )
