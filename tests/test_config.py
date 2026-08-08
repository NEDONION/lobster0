"""MiniClaw 配置加载与校验的行为测试。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.config import ConfigError, load_config, resolve_permission_roots
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
        self.assertEqual(config.permissions.profile, "workspace")
        self.assertEqual(config.permissions.read_roots, ())
        self.assertEqual(config.permissions.write_roots, ())
        self.assertEqual(config.permissions.executable_roots, ())
        self.assertFalse(config.permissions.discover_user_executables)
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

    def test_personal_permissions_load_existing_unique_roots(self) -> None:
        """Personal Profile 应保留已存在的显式 Roots 和 CLI 发现开关。"""
        read_root = self.home / "read-root"
        write_root = self.home / "write-root"
        executable_root = self.home / "bin"
        for root in (read_root, write_root, executable_root):
            root.mkdir()
        self.paths.config.write_text(
            "[permissions]\n"
            'profile = "personal"\n'
            f'read_roots = ["{read_root}"]\n'
            f'write_roots = ["{write_root}"]\n'
            f'executable_roots = ["{executable_root}"]\n'
            "discover_user_executables = true\n",
            encoding="utf-8",
        )

        config = load_config(self.paths, {}, {})

        self.assertEqual(config.permissions.profile, "personal")
        self.assertEqual(config.permissions.read_roots, (read_root,))
        self.assertEqual(config.permissions.write_roots, (write_root,))
        self.assertEqual(config.permissions.executable_roots, (executable_root,))
        self.assertTrue(config.permissions.discover_user_executables)

    def test_invalid_permission_roots_and_profile_fail_closed(self) -> None:
        """Personal Roots 必须真实、唯一且为非 symlink 绝对目录。"""
        directory = self.home / "root"
        directory.mkdir()
        file_path = self.home / "file"
        file_path.write_text("x", encoding="utf-8")
        link = self.home / "link"
        link.symlink_to(directory, target_is_directory=True)
        invalid = (
            ("[permissions]\nunknown = true\n", "permissions.unknown"),
            ('[permissions]\nprofile = "full"\n', "permissions.profile"),
            (
                '[permissions]\nprofile = "workspace"\n'
                "discover_user_executables = true\n",
                "discover_user_executables",
            ),
            (
                '[permissions]\nprofile = "personal"\nread_roots = ["relative"]\n',
                "permissions.read_roots",
            ),
            (
                '[permissions]\nprofile = "personal"\n'
                f'read_roots = ["{self.home / "missing"}"]\n',
                "permissions.read_roots",
            ),
            (
                '[permissions]\nprofile = "personal"\n'
                f'write_roots = ["{file_path}"]\n',
                "permissions.write_roots",
            ),
            (
                '[permissions]\nprofile = "personal"\n'
                f'executable_roots = ["{link}"]\n',
                "permissions.executable_roots",
            ),
            (
                '[permissions]\nprofile = "personal"\n'
                f'read_roots = ["{directory}", "{directory}"]\n',
                "permissions.read_roots",
            ),
        )
        for content, expected in invalid:
            with self.subTest(expected=expected):
                self.paths.config.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(self.paths, {}, {})

    def test_personal_root_resolution_is_stable_and_ignores_missing_defaults(self) -> None:
        """Personal 默认根只纳入真实目录，并保持显式根和 Workspace 去重。"""
        documents = self.home / "Documents"
        projects = self.home / "PycharmProjects"
        explicit_read = self.home / "shared"
        explicit_write = self.home / "published"
        for root in (documents, projects, explicit_read, explicit_write, self.workspace):
            root.mkdir()
        self.paths.config.write_text(
            "[permissions]\n"
            'profile = "personal"\n'
            f'read_roots = ["{explicit_read}", "{self.workspace}"]\n'
            f'write_roots = ["{explicit_write}", "{self.workspace}"]\n',
            encoding="utf-8",
        )
        config = load_config(self.paths, {}, {})

        roots = resolve_permission_roots(
            config.permissions,
            self.workspace,
            home=self.home,
            platform_name="darwin",
        )

        self.assertEqual(roots.owner_home, self.home)
        self.assertEqual(roots.read_roots[:2], (self.home, explicit_read))
        self.assertNotIn(self.workspace, roots.read_roots)
        self.assertEqual(
            roots.write_roots,
            (documents, projects, explicit_write),
        )


if __name__ == "__main__":
    unittest.main()
