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
        self.assertEqual(metadata["build-system"]["requires"], ["setuptools==80.9.0"])
        self.assertEqual(
            metadata["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "miniclaw._version.__version__",
        )
        self.assertEqual(__version__, "0.7.0")

    def test_public_names_and_complete_extras_do_not_change(self) -> None:
        """CLI 名称和完整渠道依赖集合应保持公开兼容。"""
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
        optional_dependencies = project["optional-dependencies"]
        expected_channels = [
            "lark-channel-sdk>=1.2,<2",
            "python-telegram-bot>=21,<23",
            "discord.py>=2.4,<3",
        ]

        self.assertEqual(project["scripts"]["miniclaw"], "miniclaw.cli:main")
        self.assertEqual(optional_dependencies["channels"], expected_channels)
        self.assertEqual(optional_dependencies["all"], expected_channels)
