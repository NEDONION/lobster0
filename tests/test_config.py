"""MiniClaw 配置加载与校验的行为测试。"""

import tempfile
import unittest
from dataclasses import FrozenInstanceError
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
        self.assertFalse(config.channels.feishu.enabled)
        self.assertFalse(config.channels.telegram.enabled)
        self.assertEqual(config.channels.telegram.bot_token_env, "MINICLAW_TELEGRAM_BOT_TOKEN")
        self.assertEqual(config.channels.telegram.message_max_chars, 4096)
        self.assertFalse(config.channels.discord.enabled)
        self.assertEqual(config.channels.discord.bot_token_env, "MINICLAW_DISCORD_BOT_TOKEN")
        self.assertEqual(config.channels.discord.message_max_chars, 2000)
        self.assertEqual(config.channels.feishu.account_id, "default")
        self.assertEqual(config.channels.feishu.owner_open_id, "")
        self.assertEqual(
            config.channels.feishu.app_secret_env,
            "MINICLAW_FEISHU_APP_SECRET",
        )
        self.assertNotIn("secret-must-stay-outside-config", repr(config))

    def test_feishu_section_loads_typed_limits_without_secret_values(self) -> None:
        """合法飞书配置应保留白名单和预算，但密钥值不能进入 AppConfig。"""
        self.paths.config.write_text(
            "[channels.feishu]\n"
            "enabled = true\n"
            'account_id = "personal"\n'
            'app_id_env = "MY_FEISHU_APP_ID"\n'
            'app_secret_env = "MY_FEISHU_APP_SECRET"\n'
            'domain = "feishu"\n'
            'owner_open_id = "ou_owner123"\n'
            'allowed_open_ids = ["ou_owner123", "ou_friend456"]\n'
            'allowed_chat_ids = ["oc_project789"]\n'
            "allow_group_mentions = true\n"
            "queue_size = 32\n"
            "worker_count = 3\n"
            "message_max_chars = 24000\n"
            "streaming_card = false\n",
            encoding="utf-8",
        )

        config = load_config(
            self.paths,
            {
                "MY_FEISHU_APP_ID": "cli_test_app_id",
                "MY_FEISHU_APP_SECRET": "secret-that-must-not-enter-config",
            },
            {},
        )

        feishu = config.channels.feishu
        self.assertTrue(feishu.enabled)
        self.assertEqual(feishu.account_id, "personal")
        self.assertEqual(feishu.app_id_env, "MY_FEISHU_APP_ID")
        self.assertEqual(feishu.app_secret_env, "MY_FEISHU_APP_SECRET")
        self.assertEqual(feishu.owner_open_id, "ou_owner123")
        self.assertEqual(feishu.allowed_open_ids, ("ou_owner123", "ou_friend456"))
        self.assertEqual(feishu.allowed_chat_ids, ("oc_project789",))
        self.assertTrue(feishu.allow_group_mentions)
        self.assertEqual(feishu.queue_size, 32)
        self.assertEqual(feishu.worker_count, 3)
        self.assertEqual(feishu.message_max_chars, 24_000)
        self.assertFalse(feishu.streaming_card)
        self.assertNotIn("secret-that-must-not-enter-config", repr(config))

    def test_invalid_feishu_configuration_fails_closed(self) -> None:
        """未知字段、无 Owner、非法 ID 与越界预算都不能静默回退。"""
        base = (
            "[channels.feishu]\n"
            "enabled = true\n"
            'owner_open_id = "ou_owner123"\n'
            'allowed_open_ids = ["ou_owner123"]\n'
            "allow_group_mentions = false\n"
        )
        invalid_configs = (
            ("[channels]\nunknown = true\n", "channels.unknown"),
            ("[channels.feishu]\nunknown = true\n", "channels.feishu.unknown"),
            (
                "[channels.feishu]\nenabled = true\nallow_group_mentions = false\n",
                "owner_open_id",
            ),
            (
                base.replace("ou_owner123", "owner", 1),
                "owner_open_id",
            ),
            (
                base.replace('["ou_owner123"]', '["ou_someone_else"]'),
                "owner_open_id.*allowed_open_ids",
            ),
            (
                base.replace("allow_group_mentions = false", "allow_group_mentions = true"),
                "allowed_chat_ids",
            ),
            (base + 'allowed_chat_ids = ["not-a-chat"]\n', "allowed_chat_ids"),
            (base + 'account_id = "UPPER CASE"\n', "account_id"),
            (base + 'app_secret_env = "literal-secret"\n', "app_secret_env"),
            (base + "queue_size = 0\n", "queue_size"),
            (base + "queue_size = 1025\n", "queue_size"),
            (base + "worker_count = 9\n", "worker_count"),
            (base + "message_max_chars = 999\n", "message_max_chars"),
            (base + "streaming_card = 1\n", "streaming_card"),
            (base + 'domain = "unknown"\n', "domain"),
        )
        for content, expected in invalid_configs:
            with self.subTest(expected=expected):
                self.paths.config.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(self.paths, {}, {})

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


class TelegramConfigTest(unittest.TestCase):
    """验证 Telegram numeric identity、群聊关系和资源预算。"""

    def setUp(self) -> None:
        """为每条配置建立独立状态路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())

    def test_complete_section_loads_typed_values_without_token(self) -> None:
        """合法配置只保存 Token 变量名，并生成冻结的 typed config。"""
        self.paths.config.write_text(
            "[channels.telegram]\n"
            "enabled = true\n"
            'account_id = "personal"\n'
            'bot_token_env = "MY_TELEGRAM_TOKEN"\n'
            "owner_user_id = 123456\n"
            "allowed_user_ids = [123456, 654321]\n"
            "allowed_chat_ids = [-1001234567890, 123456]\n"
            "allow_group_mentions = true\n"
            "queue_size = 32\n"
            "worker_count = 3\n"
            "message_max_chars = 4000\n"
            "progress_update_interval = 1.25\n",
            encoding="utf-8",
        )

        config = load_config(
            self.paths,
            {"MY_TELEGRAM_TOKEN": "123456:secret-must-stay-outside-config"},
            {},
        )

        telegram = config.channels.telegram
        self.assertTrue(telegram.enabled)
        self.assertEqual(telegram.account_id, "personal")
        self.assertEqual(telegram.bot_token_env, "MY_TELEGRAM_TOKEN")
        self.assertEqual(telegram.owner_user_id, 123456)
        self.assertEqual(telegram.allowed_user_ids, (123456, 654321))
        self.assertEqual(telegram.allowed_chat_ids, (-1001234567890, 123456))
        self.assertTrue(telegram.allow_group_mentions)
        self.assertEqual((telegram.queue_size, telegram.worker_count), (32, 3))
        self.assertEqual(telegram.message_max_chars, 4000)
        self.assertEqual(telegram.progress_update_interval, 1.25)
        self.assertNotIn("secret-must-stay-outside-config", repr(config))
        with self.assertRaises(FrozenInstanceError):
            telegram.owner_user_id = 999  # type: ignore[misc]

    def test_invalid_telegram_configuration_fails_closed(self) -> None:
        """未知字段、bool/越界 ID、重复 allowlist 和关系错误必须拒绝。"""
        base = (
            "[channels.telegram]\n"
            "enabled = true\n"
            "owner_user_id = 123456\n"
            "allowed_user_ids = [123456]\n"
            "allow_group_mentions = false\n"
        )
        invalid_configs = (
            ("[channels.telegram]\nunknown = true\n", "channels.telegram.unknown"),
            (base.replace("owner_user_id = 123456", "owner_user_id = true"), "owner_user_id"),
            (base.replace("owner_user_id = 123456", "owner_user_id = 0"), "owner_user_id"),
            (base.replace("[123456]", "[654321]"), "owner_user_id.*allowed_user_ids"),
            (base.replace("[123456]", "[123456, 123456]"), "allowed_user_ids"),
            (
                base.replace("allow_group_mentions = false", "allow_group_mentions = true"),
                "allowed_chat_ids",
            ),
            (base + "allowed_chat_ids = [0]\n", "allowed_chat_ids"),
            (base + f"allowed_chat_ids = [{2**63}]\n", "allowed_chat_ids"),
            (base + "message_max_chars = 4097\n", "message_max_chars"),
            (base + "progress_update_interval = 0.05\n", "progress_update_interval"),
            (base + "queue_size = true\n", "queue_size"),
        )
        for content, expected in invalid_configs:
            with self.subTest(expected=expected):
                self.paths.config.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(self.paths, {}, {})


class DiscordConfigTest(unittest.TestCase):
    """验证 Discord snowflake、Guild admission 和体验预算。"""

    def setUp(self) -> None:
        """为每条配置建立独立状态路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())

    def test_complete_section_loads_typed_values_without_token(self) -> None:
        """合法 Discord section 应保留 snowflake allowlist，但不保存 Token。"""
        self.paths.config.write_text(
            "[channels.discord]\n"
            "enabled = true\n"
            'account_id = "personal"\n'
            'bot_token_env = "MY_DISCORD_TOKEN"\n'
            "owner_user_id = 111111111111111111\n"
            "allowed_user_ids = [111111111111111111, 222222222222222222]\n"
            "allowed_guild_ids = [333333333333333333]\n"
            "allowed_channel_ids = [444444444444444444]\n"
            "allow_guild_mentions = true\n"
            "queue_size = 48\n"
            "worker_count = 4\n"
            "message_max_chars = 1900\n"
            "progress_update_interval = 1.5\n"
            "typing_renew_interval = 9.0\n",
            encoding="utf-8",
        )

        config = load_config(
            self.paths,
            {"MY_DISCORD_TOKEN": "secret-must-stay-outside-config"},
            {},
        )

        discord = config.channels.discord
        self.assertTrue(discord.enabled)
        self.assertEqual(discord.account_id, "personal")
        self.assertEqual(discord.bot_token_env, "MY_DISCORD_TOKEN")
        self.assertEqual(discord.owner_user_id, 111111111111111111)
        self.assertEqual(
            discord.allowed_user_ids,
            (111111111111111111, 222222222222222222),
        )
        self.assertEqual(discord.allowed_guild_ids, (333333333333333333,))
        self.assertEqual(discord.allowed_channel_ids, (444444444444444444,))
        self.assertTrue(discord.allow_guild_mentions)
        self.assertEqual((discord.queue_size, discord.worker_count), (48, 4))
        self.assertEqual(discord.message_max_chars, 1900)
        self.assertEqual(discord.progress_update_interval, 1.5)
        self.assertEqual(discord.typing_renew_interval, 9.0)
        self.assertNotIn("secret-must-stay-outside-config", repr(config))

    def test_invalid_discord_configuration_fails_closed(self) -> None:
        """未知 key、非法 snowflake、缺 Guild/Channel allowlist 和预算必须拒绝。"""
        base = (
            "[channels.discord]\n"
            "enabled = true\n"
            "owner_user_id = 111111111111111111\n"
            "allowed_user_ids = [111111111111111111]\n"
            "allow_guild_mentions = false\n"
        )
        invalid_configs = (
            ("[channels.discord]\nunknown = true\n", "channels.discord.unknown"),
            (
                base.replace(
                    "owner_user_id = 111111111111111111", "owner_user_id = true"
                ),
                "owner_user_id",
            ),
            (
                base.replace(
                    "owner_user_id = 111111111111111111", "owner_user_id = -1"
                ),
                "owner_user_id",
            ),
            (
                base.replace("[111111111111111111]", "[222222222222222222]"),
                "owner_user_id.*allowed_user_ids",
            ),
            (base + f"allowed_guild_ids = [{2**64}]\n", "allowed_guild_ids"),
            (
                base.replace("allow_guild_mentions = false", "allow_guild_mentions = true"),
                "allowed_guild_ids",
            ),
            (
                base.replace("allow_guild_mentions = false", "allow_guild_mentions = true")
                + "allowed_guild_ids = [333333333333333333]\n",
                "allowed_channel_ids",
            ),
            (base + "allowed_channel_ids = [1, 1]\n", "allowed_channel_ids"),
            (base + "message_max_chars = 2001\n", "message_max_chars"),
            (base + "typing_renew_interval = 31.0\n", "typing_renew_interval"),
            (base + "worker_count = 0\n", "worker_count"),
        )
        for content, expected in invalid_configs:
            with self.subTest(expected=expected):
                self.paths.config.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(self.paths, {}, {})


if __name__ == "__main__":
    unittest.main()
