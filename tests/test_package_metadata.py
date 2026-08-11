"""验证 Lobster0 的 Python 分发元数据契约。"""

import re
import tomllib
import unittest
from pathlib import Path

from lobster0 import __version__


class PackageMetadataTest(unittest.TestCase):
    """验证公开分发名、版本来源与可选依赖保持稳定。"""

    def test_version_has_one_python_source(self) -> None:
        """版本应由唯一 Python 模块提供给构建元数据和运行时。"""
        metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(metadata["project"]["name"], "lobster0-agent")
        self.assertEqual(metadata["project"]["dynamic"], ["version"])
        self.assertNotIn("version", metadata["project"])
        self.assertEqual(metadata["build-system"]["requires"], ["setuptools==80.9.0"])
        self.assertEqual(
            metadata["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "lobster0._version.__version__",
        )
        self.assertEqual(__version__, "0.7.0")

    def test_timezone_database_is_a_runtime_dependency(self) -> None:
        """必须显式依赖 tzdata，否则纯净 Linux 服务器上 init 会直接失败。

        uv 提供的 python-build-standalone 解释器不自带时区数据库，也不会回退
        去读系统的 /usr/share/zoneinfo：即便宿主装了系统 tzdata，
        ``ZoneInfo("Asia/Shanghai")`` 仍抛 ZoneInfoNotFoundError，
        ``lobster0 init`` 因此以 "heartbeat.timezone must be a valid IANA
        timezone" 退出。实测于纯净 ubuntu:24.04 + uv managed CPython 3.12。
        zoneinfo 会自动回退到这个纯 Python 包，所以它必须是运行时依赖，
        不能只依赖宿主系统。
        """
        document = tomllib.loads(
            Path("pyproject.toml").read_text(encoding="utf-8")
        )
        dependencies = document["project"]["dependencies"]
        self.assertTrue(
            any(str(entry).startswith("tzdata") for entry in dependencies),
            "tzdata 必须留在运行时依赖里；移除它会让纯净 Linux 服务器无法完成 init",
        )

    def test_default_config_timezone_resolves_with_the_declared_dependency(self) -> None:
        """init 模板写入的时区必须能被当前解释器真实解析。"""
        from zoneinfo import ZoneInfo

        source = Path("src/lobster0/bootstrap.py").read_text(encoding="utf-8")
        match = re.search(r"timezone = .([A-Za-z]+/[A-Za-z_]+)", source)
        self.assertIsNotNone(match, "未能在 init 模板中定位 timezone 默认值")
        ZoneInfo(match.group(1))

    def test_public_names_and_complete_extras_do_not_change(self) -> None:
        """CLI 名称和完整渠道依赖集合应保持公开兼容。"""
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
        optional_dependencies = project["optional-dependencies"]
        expected_channels = [
            "lark-channel-sdk>=1.2,<2",
            "python-telegram-bot>=21,<23",
            "discord.py>=2.4,<3",
        ]

        self.assertEqual(project["scripts"]["lobster0"], "lobster0.cli:main")
        self.assertEqual(optional_dependencies["channels"], expected_channels)
        self.assertEqual(optional_dependencies["all"], expected_channels)
