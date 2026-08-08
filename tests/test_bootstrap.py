"""MiniClaw 本地状态初始化的行为测试。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.bootstrap import BootstrapError, initialize_state
from miniclaw.config import load_config
from miniclaw.paths import build_state_paths


class BootstrapTest(unittest.TestCase):
    """验证首次初始化和重复初始化都保持用户数据安全。"""

    def setUp(self) -> None:
        """为每个测试创建独立状态根。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())

    def test_initialization_creates_loadable_state_with_private_files(self) -> None:
        """首次初始化应创建目录、模板、数据库和一个可加载配置。"""
        result = initialize_state(self.paths)
        config = load_config(self.paths, {}, {})

        self.assertEqual(result.applied_migrations, (1, 2))
        self.assertEqual(result.owner.display_name, "Owner")
        self.assertEqual(config.agent.model, "deepseek-v4-pro")
        self.assertEqual(config.provider.base_url, "https://api.deepseek.com")
        self.assertEqual(config.provider.api_key_env, "MINICLAW_MODEL_API_KEY")
        self.assertEqual(config.ui.language, "zh-CN")
        self.assertEqual(config.workspace.path, self.paths.workspace)
        self.assertEqual(config.permissions.profile, "personal")
        self.assertTrue(config.permissions.discover_user_executables)
        self.assertEqual(getattr(config.tools, "mode", None), "autopilot")
        self.assertIn(
            '[ui]\nlanguage = "zh-CN"',
            self.paths.config.read_text(encoding="utf-8"),
        )
        template = self.paths.config.read_text(encoding="utf-8")
        self.assertIn('[tools]\nmode = "autopilot"', template)
        self.assertIn("# [channels.feishu]", template)
        self.assertIn('# app_id_env = "MINICLAW_FEISHU_APP_ID"', template)
        self.assertIn('# app_secret_env = "MINICLAW_FEISHU_APP_SECRET"', template)
        self.assertIn("# [channels.telegram]", template)
        self.assertIn('# bot_token_env = "MINICLAW_TELEGRAM_BOT_TOKEN"', template)
        self.assertIn("# owner_user_id = 0", template)
        self.assertIn("# [channels.discord]", template)
        self.assertIn('# bot_token_env = "MINICLAW_DISCORD_BOT_TOKEN"', template)
        self.assertNotIn("cli_", template)
        env_example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("MINICLAW_TELEGRAM_BOT_TOKEN=\n", env_example)
        self.assertIn("MINICLAW_DISCORD_BOT_TOKEN=\n", env_example)
        self.assertEqual(
            set(result.created_files),
            {
                self.paths.config,
                self.paths.soul,
                self.paths.user,
                self.paths.memory_file,
                self.paths.skills / "feishu-lark-cli/SKILL.md",
                self.paths.skills / "github-cli/SKILL.md",
                self.paths.skills / "summarize/SKILL.md",
            },
        )
        self.assertTrue(all(path.is_dir() for path in self.paths.directories))
        self.assertTrue(self.paths.database.is_file())
        for path in result.created_files:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        feishu_skill = (
            self.paths.skills / "feishu-lark-cli/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("run_command", feishu_skill)
        self.assertIn("lark-cli drive +search", feishu_skill)
        self.assertNotIn("access_token", feishu_skill.casefold())
        github_skill = (self.paths.skills / "github-cli/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("gh auth status", github_skill)
        self.assertIn("pinnedItems", github_skill)
        self.assertIn("不得在命令参数中传入 Token", github_skill)

    def test_repeated_initialization_preserves_user_files_and_owner(self) -> None:
        """重复初始化不能覆盖 Markdown、重复迁移或插入第二个 Owner。"""
        first = initialize_state(self.paths)
        self.paths.user.write_text("My profile\n", encoding="utf-8")
        example = self.paths.skills / "summarize/SKILL.md"
        example.write_text("custom skill\n", encoding="utf-8")

        second = initialize_state(self.paths)

        self.assertEqual(first.owner.id, second.owner.id)
        self.assertEqual(second.applied_migrations, ())
        self.assertEqual(second.created_files, ())
        self.assertEqual(self.paths.user.read_text(encoding="utf-8"), "My profile\n")
        self.assertEqual(example.read_text(encoding="utf-8"), "custom skill\n")

    def test_repeated_initialization_preserves_workspace_permission_profile(self) -> None:
        """已有 Workspace Profile 不能被重复 init 静默扩大为 Personal。"""
        initialize_state(self.paths)
        self.paths.config.write_text(
            f'[workspace]\npath = "{self.paths.workspace}"\n'
            '[permissions]\nprofile = "workspace"\n',
            encoding="utf-8",
        )

        initialize_state(self.paths)

        config = load_config(self.paths, {}, {})
        self.assertEqual(config.permissions.profile, "workspace")
        self.assertFalse(config.permissions.discover_user_executables)

    def test_symbolic_link_state_directory_is_rejected(self) -> None:
        """预置符号链接不能把初始化写入重定向到非预期目录。"""
        target = self.paths.home / "redirect-target"
        target.mkdir()
        self.paths.workspace.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(BootstrapError, "symbolic link"):
            initialize_state(self.paths)


if __name__ == "__main__":
    unittest.main()
