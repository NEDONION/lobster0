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
        self.assertEqual(config.ui.language, "zh-CN")
        self.assertEqual(config.provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(config.provider.api_key_env, "MINICLAW_MODEL_API_KEY")
        self.assertEqual(config.workspace.path, self.paths.workspace)
        self.assertEqual(config.tools.security, "allowlist")
        self.assertEqual(config.tools.ask, "on-miss")
        self.assertEqual(config.tools.approval_ttl_seconds, 600)
        self.assertEqual(
            config.tools.enabled,
            (
                "system_info",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "http_get",
                "run_command",
                "read_memory",
                "propose_memory",
            ),
        )
        self.assertEqual(config.tools.run_command.timeout_seconds, 30)
        self.assertEqual(config.tools.http_get.max_response_bytes, 2 * 1024 * 1024)
        self.assertNotIn("secret-must-stay-outside-config", repr(config))

    def test_ui_language_defaults_to_chinese_and_accepts_only_english(self) -> None:
        """界面默认中文，并且持久配置只能在中英文之间选择。"""
        self.paths.config.write_text('[ui]\nlanguage = "en"\n', encoding="utf-8")

        self.assertEqual(load_config(self.paths, {}, {}).ui.language, "en")

        for content, expected in (
            ('[ui]\nlanguage = "fr"\n', "ui.language"),
            ("[ui]\nlocale = \"en\"\n", "ui.locale"),
        ):
            with self.subTest(content=content):
                self.paths.config.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(self.paths, {}, {})

    def test_tools_sections_are_strict_and_load_exact_rules(self) -> None:
        """Tool 配置拼错时必须失败，合法 exact 规则必须保留参数边界。"""
        self.paths.config.write_text(
            "[tools]\n"
            'enabled = ["system_info", "run_command", "http_get"]\n'
            'security = "full"\n'
            'ask = "always"\n'
            "approval_ttl_seconds = 90\n"
            "[tools.run_command]\n"
            'allow_commands = [{ program = "git", args = ["status", "--short"] }]\n'
            "timeout_seconds = 12\n"
            "max_timeout_seconds = 45\n"
            "[tools.http_get]\n"
            'allow_hosts = ["example.com"]\n'
            "timeout_seconds = 9\n"
            "max_response_bytes = 4096\n",
            encoding="utf-8",
        )

        config = load_config(self.paths, {}, {})

        self.assertEqual(config.tools.enabled, ("system_info", "run_command", "http_get"))
        self.assertEqual((config.tools.security, config.tools.ask), ("full", "always"))
        self.assertEqual(config.tools.approval_ttl_seconds, 90)
        self.assertEqual(config.tools.run_command.allow_commands[0].program, "git")
        self.assertEqual(
            config.tools.run_command.allow_commands[0].args,
            ("status", "--short"),
        )
        self.assertEqual(config.tools.run_command.max_timeout_seconds, 45)
        self.assertEqual(config.tools.http_get.allow_hosts, ("example.com",))
        self.assertEqual(config.tools.http_get.max_response_bytes, 4096)

    def test_invalid_tool_config_is_rejected_without_fallback(self) -> None:
        """未知字段、重复工具和越界超时不能静默降级为安全默认值。"""
        invalid_configs = (
            ("[tools.run_command]\nunknown = true\n", "tools.run_command.unknown"),
            ('[tools]\nenabled = ["grep", "grep"]\n', "tools.enabled"),
            ('[tools]\nsecurity = "unsafe"\n', "tools.security"),
            (
                "[tools.run_command]\ntimeout_seconds = 121\nmax_timeout_seconds = 120\n",
                "tools.run_command.timeout_seconds",
            ),
            ("[tools.http_get]\nmax_response_bytes = true\n", "max_response_bytes"),
        )
        for content, expected in invalid_configs:
            with self.subTest(content=content):
                self.paths.config.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(self.paths, {}, {})

    def test_exact_command_rule_preserves_repeated_arguments(self) -> None:
        """合法 argv 中重复值不能被配置去重或误判为重复规则。"""
        self.paths.config.write_text(
            "[tools.run_command]\n"
            'allow_commands = [{ program = "printf", args = ["%s%s", "x", "x"] }]\n',
            encoding="utf-8",
        )

        config = load_config(self.paths, {}, {})

        self.assertEqual(
            config.tools.run_command.allow_commands[0].args,
            ("%s%s", "x", "x"),
        )

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
