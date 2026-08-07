"""MiniClaw 配置加载与校验的行为测试。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.config import ConfigError, load_config
from miniclaw.paths import build_state_paths


class ConfigTest(unittest.TestCase):
    """验证默认值、覆盖顺序和不可信配置输入的边界。"""

    def setUp(self) -> None:
        """为每个测试创建独立的绝对状态目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.home = Path(self.temporary_directory.name).resolve()
        self.paths = build_state_paths(self.home)
        self.workspace = self.home / "custom-workspace"

    def test_missing_file_uses_safe_defaults(self) -> None:
        """尚未生成配置文件时应返回可预测且不含密钥值的默认配置。"""
        config = load_config(
            self.paths,
            {"MINICLAW_MODEL_API_KEY": "secret-must-stay-outside-config"},
            {},
        )

        self.assertEqual(config.agent.model, "provider/model")
        self.assertEqual(config.agent.max_tool_iterations, 8)
        self.assertEqual(config.provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(config.provider.api_key_env, "MINICLAW_MODEL_API_KEY")
        self.assertEqual(config.workspace.path, self.paths.workspace)
        self.assertNotIn("secret-must-stay-outside-config", repr(config))

    def test_environment_and_explicit_values_override_toml(self) -> None:
        """单字段覆盖必须遵循文件、环境和显式值的稳定优先级。"""
        self.paths.config.write_text(
            '[agent]\nmodel = "file-model"\nmax_tool_iterations = 4\n'
            '[provider]\nbase_url = "https://file.example/v1"\n'
            '[workspace]\npath = "' + self.workspace.as_posix() + '"\n',
            encoding="utf-8",
        )

        config = load_config(
            self.paths,
            {
                "MINICLAW_MODEL_NAME": "env-model",
                "MINICLAW_MAX_TOOL_ITERATIONS": "6",
            },
            {"model": "cli-model"},
        )

        self.assertEqual(config.agent.model, "cli-model")
        self.assertEqual(config.agent.max_tool_iterations, 6)
        self.assertEqual(config.provider.base_url, "https://file.example/v1")
        self.assertEqual(config.workspace.path, self.workspace)

    def test_relative_workspace_is_rejected(self) -> None:
        """相对 Workspace 必须失败，避免运行目录改变安全边界。"""
        self.paths.config.write_text('[workspace]\npath = "relative"\n', encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "workspace.path.*absolute"):
            load_config(self.paths, {}, {})

    def test_malformed_toml_reports_path_without_file_contents(self) -> None:
        """TOML 语法错误应指出文件，但不能回显可能包含密钥的原文。"""
        self.paths.config.write_text(
            '[provider\napi_key = "super-secret-value"\n',
            encoding="utf-8",
        )

        with self.assertRaises(ConfigError) as captured:
            load_config(self.paths, {}, {})

        message = str(captured.exception)
        self.assertIn("config.toml", message)
        self.assertNotIn("super-secret-value", message)

    def test_boolean_is_not_accepted_as_integer(self) -> None:
        """Python 中布尔值属于整数子类，但配置限制不能接受这种混淆。"""
        self.paths.config.write_text(
            "[agent]\nmax_tool_iterations = true\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "max_tool_iterations"):
            load_config(self.paths, {}, {})

    def test_unknown_key_is_rejected(self) -> None:
        """拼错的配置项必须立即失败，而不是静默使用默认值。"""
        self.paths.config.write_text("[agent]\nmax_iterations = 8\n", encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "agent.max_iterations"):
            load_config(self.paths, {}, {})

    def test_url_credentials_are_rejected(self) -> None:
        """Provider URL 不能携带会在日志中意外暴露的用户名或密码。"""
        self.paths.config.write_text(
            '[provider]\nbase_url = "https://user:password@example.com/v1"\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "provider.base_url"):
            load_config(self.paths, {}, {})

    def test_invalid_numeric_environment_value_is_rejected(self) -> None:
        """整数环境变量写错时必须指出变量名，不能回退到不透明的默认值。"""
        with self.assertRaisesRegex(ConfigError, "MINICLAW_MAX_TOOL_ITERATIONS"):
            load_config(self.paths, {"MINICLAW_MAX_TOOL_ITERATIONS": "many"}, {})

    def test_relative_read_only_root_is_rejected(self) -> None:
        """附加只读根也必须是绝对路径，不能成为 Workspace 逃逸入口。"""
        self.paths.config.write_text(
            '[workspace]\nread_only_roots = ["relative"]\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "workspace.read_only_roots"):
            load_config(self.paths, {}, {})


if __name__ == "__main__":
    unittest.main()
