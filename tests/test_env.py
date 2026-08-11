"""Lobster0 本地 ``.env`` 文件的安全解析测试。"""

import tempfile
import unittest
from pathlib import Path

from lobster0.env import DotEnvError, load_dotenv, resolve_dotenv_path
from lobster0.paths import build_state_paths


class DotEnvTest(unittest.TestCase):
    """验证本地凭据加载不执行 Shell，也不覆盖显式环境。"""

    def setUp(self) -> None:
        """为每个用例创建不会接触真实凭据的临时目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.paths = build_state_paths(self.root)
        self.other = self.root / "other"

    def test_installed_env_file_must_be_absolute_and_wins_over_cwd(self) -> None:
        """安装态只接受绝对 Secret 文件，且不回退到当前工作目录。"""
        private = self.root / "secrets.env"

        self.assertEqual(
            resolve_dotenv_path(
                self.paths,
                {"LOBSTER0_ENV_FILE": str(private)},
                cwd=self.other,
            ),
            private.resolve(),
        )
        with self.assertRaisesRegex(DotEnvError, "must be absolute"):
            resolve_dotenv_path(
                self.paths,
                {"LOBSTER0_ENV_FILE": "relative.env"},
                cwd=self.other,
            )

    def test_development_keeps_cwd_dotenv_when_state_home_has_no_secrets(self) -> None:
        """状态目录没有 secrets.env 时，保持加载 cwd/.env 的开发语义。"""
        self.assertEqual(resolve_dotenv_path(self.paths, {}, cwd=self.other), self.other / ".env")

    def test_state_home_secrets_are_loaded_without_an_explicit_env_file(self) -> None:
        """`setup` 写入的 secrets.env 必须无需额外配置就被运行时读到。

        实机部署中踩到的真实断裂：`lobster0 setup` 把三个凭据写进
        ``<home>/secrets.env``（文件权限 0600、内容俱全），但运行时默认只看
        ``cwd/.env``，于是 doctor 报 ``missing environment variable(s)``、
        gateway 报 ``LOBSTER0_MODEL_API_KEY is not configured``——除非用户
        自己猜到要设 ``LOBSTER0_ENV_FILE``。配置好的部署因此启动不了。
        """
        secrets = Path(self.paths.secrets_file)
        secrets.write_text("LOBSTER0_MODEL_API_KEY=k\n", encoding="utf-8")
        secrets.chmod(0o600)

        self.assertEqual(resolve_dotenv_path(self.paths, {}, cwd=self.other), secrets)

    def test_explicit_env_file_still_wins_over_state_home_secrets(self) -> None:
        """显式 LOBSTER0_ENV_FILE 的优先级高于状态目录回退。"""
        secrets = Path(self.paths.secrets_file)
        secrets.write_text("LOBSTER0_MODEL_API_KEY=k\n", encoding="utf-8")
        secrets.chmod(0o600)
        self.other.mkdir(parents=True, exist_ok=True)
        explicit = self.other / "explicit.env"
        explicit.write_text("LOBSTER0_MODEL_API_KEY=other\n", encoding="utf-8")
        explicit.chmod(0o600)

        self.assertEqual(
            resolve_dotenv_path(self.paths, {"LOBSTER0_ENV_FILE": str(explicit)}, cwd=self.other),
            explicit.resolve(),
        )

    def test_missing_file_loads_nothing(self) -> None:
        """尚未创建 ``.env`` 时应保持环境不变，方便纯 Shell 配置。"""
        environ = {"EXISTING": "value"}

        loaded = load_dotenv(self.root / ".env", environ)

        self.assertEqual(loaded, ())
        self.assertEqual(environ, {"EXISTING": "value"})

    def test_values_are_unquoted_without_overriding_environment(self) -> None:
        """文件值应支持常见引号，同时显式进程环境始终拥有更高优先级。"""
        path = self.root / ".env"
        path.write_text(
            "# local model\n"
            "LOBSTER0_MODEL_API_KEY='from-file'\n"
            'LOBSTER0_MODEL_NAME="deepseek-v4-pro"\n'
            "LOBSTER0_MODEL_BASE_URL=https://api.deepseek.com\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        environ = {"LOBSTER0_MODEL_NAME": "from-shell"}

        loaded = load_dotenv(path, environ)

        self.assertEqual(
            loaded,
            ("LOBSTER0_MODEL_API_KEY", "LOBSTER0_MODEL_BASE_URL"),
        )
        self.assertEqual(environ["LOBSTER0_MODEL_API_KEY"], "from-file")
        self.assertEqual(environ["LOBSTER0_MODEL_NAME"], "from-shell")
        self.assertEqual(environ["LOBSTER0_MODEL_BASE_URL"], "https://api.deepseek.com")

    def test_shell_export_syntax_is_rejected_without_exposing_value(self) -> None:
        """解析器不得悄悄接受可执行 Shell 语法，错误也不能回显凭据。"""
        path = self.root / ".env"
        path.write_text(
            "export LOBSTER0_MODEL_API_KEY=never-print-this\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

        with self.assertRaises(DotEnvError) as caught:
            load_dotenv(path, {})

        message = str(caught.exception)
        self.assertIn(f"{path}:1", message)
        self.assertNotIn("never-print-this", message)

    def test_invalid_key_and_unmatched_quote_are_rejected(self) -> None:
        """非法键名和不完整引号必须失败，避免生成调用方无法引用的环境变量。"""
        cases = (
            "lobster0_key=value\n",
            "LOBSTER0_MODEL_API_KEY='unfinished\n",
        )

        for content in cases:
            with self.subTest(content=content.split("=", 1)[0]):
                path = self.root / ".env"
                path.write_text(content, encoding="utf-8")
                path.chmod(0o600)

                with self.assertRaises(DotEnvError):
                    load_dotenv(path, {})

    def test_nul_byte_is_rejected_without_partial_environment_update(self) -> None:
        """文件含 NUL 时不能加载前面部分，避免半配置状态继续调用模型。"""
        path = self.root / ".env"
        path.write_text(
            "SAFE_VALUE=loaded\nLOBSTER0_MODEL_API_KEY=secret\0tail\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        environ: dict[str, str] = {}

        with self.assertRaises(DotEnvError):
            load_dotenv(path, environ)

        self.assertEqual(environ, {})

    def test_group_readable_file_is_rejected(self) -> None:
        """凭据文件对 group 或 other 可见时必须失败，不能只依赖使用说明。"""
        path = self.root / ".env"
        path.write_text("LOBSTER0_MODEL_API_KEY=secret\n", encoding="utf-8")
        path.chmod(0o640)

        with self.assertRaisesRegex(DotEnvError, "private"):
            load_dotenv(path, {})

    def test_symbolic_link_file_is_rejected_before_reading_credentials(self) -> None:
        """即使目标为 0600 regular file，也不能通过 symlink 重定向凭据读取。"""
        target = self.root / "secrets.env"
        target.write_text("LOBSTER0_MODEL_API_KEY=never-load\n", encoding="utf-8")
        target.chmod(0o600)
        link = self.root / ".env"
        link.symlink_to(target)
        environ: dict[str, str] = {}

        with self.assertRaisesRegex(DotEnvError, "private regular"):
            load_dotenv(link, environ)

        self.assertEqual(environ, {})


if __name__ == "__main__":
    unittest.main()
