"""Lobster0 配置加载与校验的行为测试。"""

import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from lobster0.config import (
    ConfigError,
    ProviderConfig,
    load_config,
    resolve_permission_roots,
    update_providers,
)
from lobster0.paths import build_state_paths


class ConfigTest(unittest.TestCase):
    """验证默认值、覆盖顺序和不可信配置输入的边界。"""

    def setUp(self) -> None:
        """为每个测试创建独立的绝对状态目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.home = Path(self.temporary_directory.name).resolve()
        self.paths = build_state_paths(self.home)
        self.workspace = self.home / "custom-workspace"

    def test_missing_file_uses_adaptive_agent_defaults(self) -> None:
        """尚未生成配置文件时应使用自适应 Agent loop 默认预算。"""
        config = load_config(
            self.paths,
            {"LOBSTER0_MODEL_API_KEY": "secret-must-stay-outside-config"},
            {},
        )

        self.assertEqual(config.agent.model, "provider/model")
        self.assertEqual(config.agent.max_tool_iterations, 32)
        self.assertEqual(config.agent.max_tool_iterations_hard, 64)
        self.assertEqual(config.agent.max_no_progress_iterations, 3)
        self.assertEqual(config.agent.max_turn_seconds, 90)
        self.assertEqual(config.ui.language, "zh-CN")
        self.assertEqual(config.provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(config.provider.api_key_env, "LOBSTER0_MODEL_API_KEY")
        self.assertEqual(config.workspace.path, self.paths.workspace)
        self.assertEqual(config.permissions.profile, "workspace")
        self.assertEqual(config.permissions.read_roots, ())
        self.assertEqual(config.permissions.write_roots, ())
        self.assertEqual(config.permissions.executable_roots, ())
        self.assertFalse(config.permissions.discover_user_executables)
        self.assertEqual(getattr(config.tools, "mode", None), "autopilot")
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
                "memory_remember",
                "memory_search",
                "memory_get",
                "memory_list",
                "memory_flush",
                "memory_forget",
                "memory_correct",
                "memory_review_list",
                "manage_task",
                "read_artifact",
            ),
        )
        self.assertEqual(config.tools.run_command.timeout_seconds, 30)
        self.assertEqual(config.tools.http_get.max_response_bytes, 2 * 1024 * 1024)
        self.assertFalse(config.channels.feishu.enabled)
        self.assertFalse(config.channels.telegram.enabled)
        self.assertEqual(config.channels.telegram.bot_token_env, "LOBSTER0_TELEGRAM_BOT_TOKEN")
        self.assertEqual(config.channels.telegram.message_max_chars, 4096)
        self.assertFalse(config.channels.discord.enabled)
        self.assertEqual(config.channels.discord.bot_token_env, "LOBSTER0_DISCORD_BOT_TOKEN")
        self.assertEqual(config.channels.discord.message_max_chars, 2000)
        self.assertEqual(config.channels.feishu.account_id, "default")
        self.assertEqual(config.channels.feishu.owner_open_id, "")
        self.assertFalse(config.browser.enabled)
        self.assertEqual(config.browser.backend, "local")
        self.assertEqual(config.browser.profile, "lobster0")
        self.assertTrue(config.browser.headed)
        self.assertFalse(config.browser.allow_personal_profile)
        self.assertEqual(config.browser.max_tabs, 8)
        self.assertEqual(config.browser.max_snapshot_chars, 20_000)
        self.assertEqual(config.browser.inactivity_timeout_seconds, 120)
        self.assertEqual(config.browser.download_max_bytes, 20 * 1024 * 1024)
        self.assertEqual(
            config.channels.feishu.app_secret_env,
            "LOBSTER0_FEISHU_APP_SECRET",
        )
        self.assertNotIn("secret-must-stay-outside-config", repr(config))

    def test_browser_section_is_bounded_and_personal_profile_requires_opt_in(self) -> None:
        """Browser 预算必须有界，Personal Profile 必须显式双重确认。"""
        valid = (
            "[browser]\n"
            "enabled = true\n"
            'backend = "local"\n'
            'profile = "personal"\n'
            "headed = false\n"
            "allow_personal_profile = true\n"
            "max_tabs = 4\n"
            "max_snapshot_chars = 10000\n"
            "inactivity_timeout_seconds = 60\n"
            "download_max_bytes = 1048576\n"
        )
        self.paths.config.write_text(valid, encoding="utf-8")

        browser = load_config(self.paths, {}, {}).browser

        self.assertTrue(browser.enabled)
        self.assertEqual(browser.profile, "personal")
        self.assertFalse(browser.headed)
        self.assertEqual(browser.max_tabs, 4)

        invalid = (
            ("[browser]\nunknown = true\n", "browser.unknown"),
            ('[browser]\nbackend = "remote"\n', "browser.backend"),
            ('[browser]\nprofile = "personal"\n', "allow_personal_profile"),
            ("[browser]\nmax_tabs = 0\n", "browser.max_tabs"),
            ("[browser]\nmax_snapshot_chars = 999\n", "browser.max_snapshot_chars"),
            (
                "[browser]\ninactivity_timeout_seconds = 29\n",
                "browser.inactivity_timeout_seconds",
            ),
            ("[browser]\ndownload_max_bytes = 104857601\n", "browser.download_max_bytes"),
        )
        for content, expected in invalid:
            with self.subTest(expected=expected):
                self.paths.config.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
                    load_config(self.paths, {}, {})

    def test_agent_budget_rejects_hard_limit_below_soft_limit(self) -> None:
        """hard tool loop 上限不能低于常规 soft 上限。"""
        self.paths.config.write_text(
            "[agent]\nmax_tool_iterations = 40\nmax_tool_iterations_hard = 32\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "max_tool_iterations_hard"):
            load_config(self.paths, {}, {})

    def test_agent_budget_rejects_hard_limit_below_environment_soft_limit(self) -> None:
        """环境变量覆盖后 hard tool loop 上限仍不能低于 soft 上限。"""
        with self.assertRaisesRegex(ConfigError, "max_tool_iterations_hard"):
            load_config(
                self.paths,
                {
                    "LOBSTER0_MAX_TOOL_ITERATIONS": "40",
                    "LOBSTER0_MAX_TOOL_ITERATIONS_HARD": "32",
                },
                {},
            )

    def test_legacy_toml_soft_budget_expands_implicit_hard_budget(self) -> None:
        """旧 TOML 只配置较大 soft 时应自动把隐式 hard 提升到同值。"""
        self.paths.config.write_text(
            "[agent]\nmax_tool_iterations = 100\n",
            encoding="utf-8",
        )

        config = load_config(self.paths, {}, {})

        self.assertEqual(config.agent.max_tool_iterations, 100)
        self.assertEqual(config.agent.max_tool_iterations_hard, 100)

    def test_legacy_environment_soft_budget_expands_implicit_hard_budget(self) -> None:
        """旧环境变量只配置较大 soft 时应自动把隐式 hard 提升到同值。"""
        config = load_config(
            self.paths,
            {"LOBSTER0_MAX_TOOL_ITERATIONS": "100"},
            {},
        )

        self.assertEqual(config.agent.max_tool_iterations, 100)
        self.assertEqual(config.agent.max_tool_iterations_hard, 100)

    def test_toml_hard_remains_explicit_when_environment_overrides_soft(self) -> None:
        """TOML 显式 hard 不得被环境 soft 静默提升。"""
        self.paths.config.write_text(
            "[agent]\nmax_tool_iterations_hard = 80\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "max_tool_iterations_hard"):
            load_config(
                self.paths,
                {"LOBSTER0_MAX_TOOL_ITERATIONS": "100"},
                {},
            )

    def test_explicit_safe_tool_mode_overrides_autopilot_default(self) -> None:
        """用户显式选择 safe 时必须保留审批模式。"""
        self.paths.config.write_text('[tools]\nmode = "safe"\n', encoding="utf-8")

        config = load_config(self.paths, {}, {})

        self.assertEqual(config.tools.mode, "safe")

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
            'mode = "autopilot"\n'
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
        self.assertEqual(getattr(config.tools, "mode", None), "autopilot")
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
            ('[tools]\nmode = "unrestricted"\n', "tools.mode"),
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
                "LOBSTER0_MODEL_NAME": "env-model",
                "LOBSTER0_MAX_TOOL_ITERATIONS": "6",
                "LOBSTER0_MAX_TOOL_ITERATIONS_HARD": "12",
                "LOBSTER0_MAX_NO_PROGRESS_ITERATIONS": "4",
                "LOBSTER0_MAX_TURN_SECONDS": "240",
            },
            {"model": "cli-model"},
        )

        self.assertEqual(config.agent.model, "cli-model")
        self.assertEqual(config.agent.max_tool_iterations, 6)
        self.assertEqual(config.agent.max_tool_iterations_hard, 12)
        self.assertEqual(config.agent.max_no_progress_iterations, 4)
        self.assertEqual(config.agent.max_turn_seconds, 240)
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

    def test_new_agent_budget_keys_reject_invalid_toml_integers(self) -> None:
        """新增 hard 与无进展预算必须拒绝零值和 bool TOML。"""
        for content, expected in (
            ("[agent]\nmax_tool_iterations_hard = 0\n", "max_tool_iterations_hard"),
            (
                "[agent]\nmax_no_progress_iterations = true\n",
                "max_no_progress_iterations",
            ),
            ("[agent]\nmax_turn_seconds = 0\n", "max_turn_seconds"),
            ("[agent]\nmax_turn_seconds = true\n", "max_turn_seconds"),
        ):
            with self.subTest(content=content):
                self.paths.config.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, expected):
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
        with self.assertRaisesRegex(ConfigError, "LOBSTER0_MAX_TOOL_ITERATIONS"):
            load_config(self.paths, {"LOBSTER0_MAX_TOOL_ITERATIONS": "many"}, {})

    def test_new_agent_budget_environment_values_must_be_integers(self) -> None:
        """新增 Agent 预算环境变量不能把非整数静默回退为默认值。"""
        for key in (
            "LOBSTER0_MAX_TOOL_ITERATIONS_HARD",
            "LOBSTER0_MAX_NO_PROGRESS_ITERATIONS",
            "LOBSTER0_MAX_TURN_SECONDS",
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigError, key):
                    load_config(self.paths, {key: "many"}, {})

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

    def test_phase6_defaults_are_safe_typed_and_inert(self) -> None:
        """Phase 6 默认不能启动后台任务，Sandbox 与 Checkpoint 必须使用有界值。"""
        config = load_config(self.paths, {}, {})

        self.assertFalse(config.automation.enabled)
        self.assertEqual(config.automation.max_active_tasks, 50)
        self.assertEqual(config.automation.max_concurrent_runs, 2)
        self.assertEqual(config.automation.misfire_grace_seconds, 300)
        self.assertEqual(config.automation.lease_seconds, 60)
        self.assertFalse(config.heartbeat.enabled)
        self.assertEqual(config.heartbeat.timezone, "Asia/Shanghai")
        self.assertEqual(config.heartbeat.active_hours_start, "08:00")
        self.assertEqual(config.heartbeat.active_hours_end, "23:00")
        self.assertEqual(config.sandbox.backend, "docker")
        self.assertEqual(config.sandbox.container_engine, "docker-rootless")
        self.assertEqual(config.sandbox.network, "none")
        self.assertEqual(config.sandbox.memory_mib, 512)
        self.assertTrue(config.checkpoint.enabled)
        self.assertEqual(config.checkpoint.max_total_bytes, 64 * 1024 * 1024)

    def test_phase6_sections_load_all_strict_values(self) -> None:
        """合法 Phase 6 配置应保留精确预算、时区与 Sandbox 后端。"""
        self.paths.config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.config.write_text(
            "[automation]\n"
            "enabled = true\nmax_active_tasks = 75\nmax_concurrent_runs = 3\n"
            "misfire_grace_seconds = 600\nlease_seconds = 90\n\n"
            "[heartbeat]\n"
            'enabled = true\ninterval_seconds = 3600\ntimezone = "America/New_York"\n'
            'active_hours_start = "09:15"\nactive_hours_end = "18:45"\n\n'
            "[sandbox]\n"
            'backend = "seatbelt"\ncontainer_engine = "podman-rootless"\n'
            'image = "lobster0-sandbox@sha256:'
            + "a" * 64
            + '"\nnetwork = "none"\nmemory_mib = 1024\ncpu_seconds = 120\n'
            "pids_limit = 256\n\n"
            "[checkpoint]\n"
            "enabled = false\nmax_entries = 500\nmax_total_bytes = 16777216\n"
            "max_file_bytes = 1048576\nmax_count = 25\n",
            encoding="utf-8",
        )

        config = load_config(self.paths, {}, {})

        self.assertTrue(config.automation.enabled)
        self.assertEqual(config.automation.max_active_tasks, 75)
        self.assertEqual(config.heartbeat.timezone, "America/New_York")
        self.assertEqual(config.sandbox.backend, "seatbelt")
        self.assertEqual(config.sandbox.container_engine, "podman-rootless")
        self.assertEqual(config.sandbox.pids_limit, 256)
        self.assertFalse(config.checkpoint.enabled)
        self.assertEqual(config.checkpoint.max_count, 25)

    def test_phase6_unknown_invalid_and_inconsistent_values_fail_closed(self) -> None:
        """拼错字段、坏类型、时区和越权预算不能回退为默认值。"""
        invalid_configs = (
            ("[automation]\nmystery = true\n", "automation.mystery"),
            ("[automation]\nmax_active_tasks = true\n", "automation.max_active_tasks"),
            ("[automation]\nmax_concurrent_runs = 17\n", "max_concurrent_runs"),
            ("[automation]\nlease_seconds = 9\n", "automation.lease_seconds"),
            ("[heartbeat]\ninterval_seconds = 59\n", "heartbeat.interval_seconds"),
            ('[heartbeat]\ntimezone = "Mars/Olympus"\n', "heartbeat.timezone"),
            ('[heartbeat]\nactive_hours_start = "8:00"\n', "active_hours_start"),
            ('[heartbeat]\nactive_hours_start = "09:00"\nactive_hours_end = "09:00"\n',
             "active hours"),
            ("[heartbeat]\nenabled = true\n", "requires automation.enabled"),
            ('[sandbox]\nbackend = "process"\n', "sandbox.backend"),
            ('[sandbox]\ncontainer_engine = "docker"\n', "sandbox.container_engine"),
            ('[sandbox]\nnetwork = "host"\n', "sandbox.network"),
            ('[sandbox]\nimage = "bad image"\n', "sandbox.image"),
            (
                '[automation]\nenabled = true\n\n[sandbox]\nimage = "lobster0-sandbox:phase6"\n',
                "sha256 digest",
            ),
            ("[sandbox]\nmemory_mib = 0\n", "sandbox.memory_mib"),
            ("[checkpoint]\nmax_entries = 10001\n", "checkpoint.max_entries"),
            ("[checkpoint]\nmax_file_bytes = 2097152\nmax_total_bytes = 1048576\n",
             "checkpoint.max_file_bytes"),
            ("[checkpoint]\nenabled = 1\n", "checkpoint.enabled"),
        )
        for content, expected in invalid_configs:
            with self.subTest(content=content):
                self.paths.config.parent.mkdir(parents=True, exist_ok=True)
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

    def _write_config(self, body: str) -> None:
        """写入一份带合法 workspace 的配置。"""
        self.paths.config.write_text(
            body + '[workspace]\npath = "' + self.workspace.as_posix() + '"\n',
            encoding="utf-8",
        )

    def test_legacy_single_provider_table_still_loads_unchanged(self) -> None:
        """旧的 [provider] 单表必须继续可用，且不被悄悄改写成新格式。"""
        self._write_config(
            '[agent]\nmodel = "legacy-model"\n'
            '[provider]\nbase_url = "https://legacy.example/v1"\n'
            'api_key_env = "LOBSTER0_MODEL_API_KEY"\n'
        )
        before = self.paths.config.read_text(encoding="utf-8")

        config = load_config(self.paths, {})

        self.assertEqual(config.provider.base_url, "https://legacy.example/v1")
        self.assertEqual(len(config.providers), 1)
        self.assertEqual(config.providers[0].id, "default")
        self.assertEqual(config.providers[0].base_url, "https://legacy.example/v1")
        self.assertEqual(config.agent.provider, "default")
        # 读取路径永不写盘。
        self.assertEqual(self.paths.config.read_text(encoding="utf-8"), before)

    def test_http_get_trusted_cidrs_default_to_empty(self) -> None:
        """默认不豁免任何网段，保持严格。"""
        self._write_config('[agent]\nmodel = "m"\n')

        config = load_config(self.paths, {})

        self.assertEqual(config.tools.http_get.trusted_cidrs, ())

    def test_http_get_trusted_cidrs_accepts_a_proxy_range(self) -> None:
        """fake-IP 代理环境下用户可以显式声明自己的网段。"""
        self._write_config(
            '[agent]\nmodel = "m"\n'
            '[tools.http_get]\ntrusted_cidrs = ["198.18.0.0/15"]\n'
        )

        config = load_config(self.paths, {})

        self.assertEqual(
            [str(item) for item in config.tools.http_get.trusted_cidrs],
            ["198.18.0.0/15"],
        )

    def test_http_get_trusted_cidrs_rejects_a_malformed_value(self) -> None:
        """写错的网段要在加载时就报错，而不是运行期静默失效。"""
        for value in ('["not-a-cidr"]', '["198.18.0.0/15", 5]', '"198.18.0.0/15"'):
            self._write_config(
                '[agent]\nmodel = "m"\n'
                f"[tools.http_get]\ntrusted_cidrs = {value}\n"
            )
            with self.assertRaises(ConfigError):
                load_config(self.paths, {})

    def test_attachment_limit_defaults_below_the_artifact_store_bound(self) -> None:
        """未配置时给一个比 Store 上限更小的默认值。"""
        self._write_config('[agent]\nmodel = "m"\n')

        config = load_config(self.paths, {})

        self.assertEqual(config.attachments.max_bytes, 10 * 1024 * 1024)
        self.assertLessEqual(config.attachments.max_bytes, config.browser.download_max_bytes)

    def test_attachment_limit_above_the_store_bound_is_refused(self) -> None:
        """附件上限不能超过 Store 的硬边界。

        静默取 min 会让用户以为自己设的值生效了，所以这里拒绝加载而不是降级。
        """
        self._write_config(
            '[agent]\nmodel = "m"\n'
            "[browser]\ndownload_max_bytes = 1048576\n"
            "[attachments]\nmax_bytes = 2097152\n"
        )

        with self.assertRaises(ConfigError) as raised:
            load_config(self.paths, {})

        self.assertIn("attachments.max_bytes", str(raised.exception))

    def test_attachment_limit_within_the_store_bound_is_accepted(self) -> None:
        """不超过 Store 上限时按用户设置生效。"""
        self._write_config(
            '[agent]\nmodel = "m"\n'
            "[browser]\ndownload_max_bytes = 8388608\n"
            "[attachments]\nmax_bytes = 4194304\n"
        )

        config = load_config(self.paths, {})

        self.assertEqual(config.attachments.max_bytes, 4 * 1024 * 1024)

    def test_provider_array_selects_the_agent_referenced_entry(self) -> None:
        """有数组表时，agent.provider 决定当前生效的那一条。"""
        self._write_config(
            '[agent]\nmodel = "m"\nprovider = "second"\n'
            '[[providers]]\nid = "first"\n'
            'base_url = "https://first.example/v1"\n'
            'api_key_env = "LOBSTER0_PROVIDER_FIRST_KEY"\n'
            '[[providers]]\nid = "second"\n'
            'base_url = "https://second.example/v1"\n'
            'api_key_env = "LOBSTER0_PROVIDER_SECOND_KEY"\n'
        )

        config = load_config(self.paths, {})

        self.assertEqual([item.id for item in config.providers], ["first", "second"])
        # config.provider 始终指向当前生效项，运行时代码无需改动。
        self.assertEqual(config.provider.base_url, "https://second.example/v1")
        self.assertEqual(config.provider.api_key_env, "LOBSTER0_PROVIDER_SECOND_KEY")

    def test_provider_array_without_selection_uses_the_first_entry(self) -> None:
        """未指定 agent.provider 时取第一条，不报错。"""
        self._write_config(
            '[agent]\nmodel = "m"\n'
            '[[providers]]\nid = "only"\nbase_url = "https://only.example/v1"\n'
        )

        config = load_config(self.paths, {})

        self.assertEqual(config.provider.base_url, "https://only.example/v1")
        self.assertEqual(config.agent.provider, "only")

    def test_rejects_both_provider_forms_at_once(self) -> None:
        """两种写法并存时不猜测意图，直接拒绝。"""
        self._write_config(
            '[provider]\nbase_url = "https://old.example/v1"\n'
            '[[providers]]\nid = "new"\nbase_url = "https://new.example/v1"\n'
        )

        with self.assertRaisesRegex(ConfigError, "providers"):
            load_config(self.paths, {})

    def test_rejects_invalid_duplicate_and_dangling_provider_ids(self) -> None:
        """id 参与环境变量名生成，必须是安全字符集且唯一、可被引用。"""
        cases = (
            # 非法字符
            '[[providers]]\nid = "Bad Id"\nbase_url = "https://a.example/v1"\n',
            '[[providers]]\nid = "UPPER"\nbase_url = "https://a.example/v1"\n',
            '[[providers]]\nid = ""\nbase_url = "https://a.example/v1"\n',
            # 重复 id
            '[[providers]]\nid = "same"\nbase_url = "https://a.example/v1"\n'
            '[[providers]]\nid = "same"\nbase_url = "https://b.example/v1"\n',
            # agent.provider 指向不存在的条目
            '[agent]\nprovider = "missing"\n'
            '[[providers]]\nid = "real"\nbase_url = "https://a.example/v1"\n',
        )
        for body in cases:
            with self.subTest(body=body[:40]):
                self._write_config(body)
                with self.assertRaises(ConfigError):
                    load_config(self.paths, {})

    def test_provider_entries_reuse_the_existing_url_validation(self) -> None:
        """数组表里的 base_url 同样不接受凭据内嵌等不安全形式。"""
        self._write_config(
            '[[providers]]\nid = "x"\n'
            'base_url = "https://user:password@example.com/v1"\n'
        )
        with self.assertRaises(ConfigError):
            load_config(self.paths, {})


    def test_update_providers_rewrites_only_provider_and_agent_fields(self) -> None:
        """就地更新只动 providers 与 agent 的两个字段，其余段落原样保留。"""
        self._write_config(
            '[agent]\nmodel = "old-model"\nmax_tool_iterations = 7\n'
            '[ui]\nlanguage = "zh-CN"\n'
            '[provider]\nbase_url = "https://old.example/v1"\n'
            '[tools]\nmode = "safe"\n'
        )

        update_providers(
            self.paths,
            providers=(
                ProviderConfig(id="a", base_url="https://a.example/v1"),
                ProviderConfig(id="b", base_url="https://b.example/v1"),
            ),
            selected="b",
            model="new-model",
        )

        config = load_config(self.paths, {})
        self.assertEqual([item.id for item in config.providers], ["a", "b"])
        self.assertEqual(config.provider.base_url, "https://b.example/v1")
        self.assertEqual(config.agent.model, "new-model")
        # 其他段落与字段必须存活。
        self.assertEqual(config.agent.max_tool_iterations, 7)
        self.assertEqual(config.ui.language, "zh-CN")
        self.assertEqual(config.tools.mode, "safe")
        # 旧的单表被数组表取代，不能两者并存（并存会导致加载直接失败）。
        text = self.paths.config.read_text(encoding="utf-8")
        self.assertIn("[[providers]]", text)
        self.assertNotIn("\n[provider]\n", text)

    def test_update_providers_keeps_file_intact_when_result_would_not_load(self) -> None:
        """写入前先验证新内容能被加载，校验不过就不落盘。"""
        self._write_config('[agent]\nmodel = "keep"\n[provider]\nbase_url = "https://k.example/v1"\n')
        before = self.paths.config.read_text(encoding="utf-8")

        with self.assertRaises(ConfigError):
            update_providers(
                self.paths,
                providers=(ProviderConfig(id="a", base_url="https://a.example/v1"),),
                selected="does-not-exist",
                model="m",
            )

        self.assertEqual(self.paths.config.read_text(encoding="utf-8"), before)

    def test_update_providers_backs_up_the_previous_file(self) -> None:
        """覆盖前留一份备份，便于用户回滚。"""
        self._write_config('[agent]\nmodel = "v1"\n[provider]\nbase_url = "https://k.example/v1"\n')

        update_providers(
            self.paths,
            providers=(ProviderConfig(id="a", base_url="https://a.example/v1"),),
            selected="a",
            model="v2",
        )

        backup = self.paths.config.with_suffix(".toml.bak")
        self.assertTrue(backup.is_file())
        self.assertIn('model = "v1"', backup.read_text(encoding="utf-8"))

    def test_update_providers_requires_at_least_one_entry(self) -> None:
        """空列表会让应用没有可用模型端点，直接拒绝。"""
        self._write_config('[agent]\nmodel = "m"\n[provider]\nbase_url = "https://k.example/v1"\n')

        with self.assertRaises(ConfigError):
            update_providers(self.paths, providers=(), selected="a", model="m")


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
