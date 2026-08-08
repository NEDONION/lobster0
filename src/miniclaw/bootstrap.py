"""MiniClaw 状态目录、模板、数据库和 Owner 的幂等初始化。"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from miniclaw.config import load_config
from miniclaw.paths import StatePaths
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import Owner, OwnerRepository


class BootstrapError(RuntimeError):
    """表示初始化目标存在不安全或不兼容的文件系统对象。"""


@dataclass(frozen=True, slots=True)
class InitResult:
    """描述一次初始化实际创建或复用的状态。"""

    paths: StatePaths
    owner: Owner
    applied_migrations: tuple[int, ...]
    created_files: tuple[Path, ...]


def initialize_state(paths: StatePaths) -> InitResult:
    """幂等创建一个可加载、可迁移的单 Owner MiniClaw 状态目录。

    Args:
        paths: 已解析并限制在同一状态根下的路径集合。

    Returns:
        本次新建文件、迁移版本和持久化 Owner。

    Raises:
        BootstrapError: 目标目录或模板路径是符号链接或非预期文件类型。
        ConfigError: 已有或新建配置无法通过校验。
        MigrationError: SQLite Schema 无法完成迁移。
        OSError: 目录或文件无法安全创建。
    """
    for directory in paths.directories:
        _ensure_directory(directory)
    example_skill_directory = paths.skills / "summarize"
    _ensure_directory(example_skill_directory)

    templates = (
        (paths.config, _render_default_config(paths)),
        (paths.soul, "# MiniClaw\n"),
        (paths.user, "# User\n"),
        (paths.memory_file, "# Long-term Memory\n"),
        (
            example_skill_directory / "SKILL.md",
            "---\n"
            "name: summarize\n"
            "description: 总结长文本，提取决定、风险和下一步行动。\n"
            "version: 1\n"
            "---\n\n"
            "# Instructions\n\n"
            "先给结论，再列决定、风险和 action items。\n",
        ),
    )
    created_files = tuple(
        path for path, content in templates if _create_private_file(path, content)
    )

    config = load_config(paths)
    _ensure_directory(config.workspace.path)
    database = Database(paths.database)
    applied_migrations = apply_migrations(database)
    owner = OwnerRepository(database).get_or_create()
    return InitResult(
        paths=paths,
        owner=owner,
        applied_migrations=applied_migrations,
        created_files=created_files,
    )


def _ensure_directory(path: Path) -> None:
    """创建 owner-only 目录，并拒绝符号链接或同名普通文件。"""
    if path.is_symlink():
        raise BootstrapError(f"state path must not be a symbolic link: {path}")
    existed = path.exists()
    if existed and not path.is_dir():
        raise BootstrapError(f"state directory path is not a directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        path.chmod(0o700)


def _create_private_file(path: Path, content: str) -> bool:
    """只在模板不存在时以 owner-only 权限创建 UTF-8 文件。"""
    if path.is_symlink():
        raise BootstrapError(f"state file must not be a symbolic link: {path}")
    if path.exists():
        if not path.is_file():
            raise BootstrapError(f"state file path is not a regular file: {path}")
        return False

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
        os.fchmod(state_file.fileno(), 0o600)
        state_file.write(content)
    return True


def _render_default_config(paths: StatePaths) -> str:
    """生成包含 DeepSeek V4 Pro 默认 Provider 的稳定 TOML 配置。"""
    workspace = json.dumps(str(paths.workspace), ensure_ascii=False)
    return (
        '[agent]\nmodel = "deepseek-v4-pro"\nmax_tool_iterations = 8\n'
        "context_budget_tokens = 32000\ntool_result_max_chars = 20000\n\n"
        '[ui]\nlanguage = "zh-CN"\n\n'
        '[provider]\nbase_url = "https://api.deepseek.com"\n'
        'api_key_env = "MINICLAW_MODEL_API_KEY"\ntimeout_seconds = 120\n\n'
        f"[workspace]\npath = {workspace}\nread_only_roots = []\n\n"
        '[permissions]\nprofile = "personal"\nread_roots = []\nwrite_roots = []\n'
        "executable_roots = []\ndiscover_user_executables = true\n\n"
        '[tools]\nmode = "autopilot"\n\n'
        "# [channels.feishu]\n"
        "# enabled = false\n"
        '# account_id = "default"\n'
        '# app_id_env = "MINICLAW_FEISHU_APP_ID"\n'
        '# app_secret_env = "MINICLAW_FEISHU_APP_SECRET"\n'
        '# domain = "feishu"\n'
        '# owner_open_id = "ou_replace_with_owner_open_id"\n'
        '# allowed_open_ids = ["ou_replace_with_owner_open_id"]\n'
        "# allowed_chat_ids = []\n"
        "# allow_group_mentions = false\n"
        "# queue_size = 64\n"
        "# worker_count = 2\n"
        "# message_max_chars = 30000\n"
        "# streaming_card = true\n"
        "\n# [channels.telegram]\n"
        "# enabled = false\n"
        '# account_id = "default"\n'
        '# bot_token_env = "MINICLAW_TELEGRAM_BOT_TOKEN"\n'
        "# owner_user_id = 0\n"
        "# allowed_user_ids = []\n"
        "# allowed_chat_ids = []\n"
        "# allow_group_mentions = false\n"
        "# queue_size = 64\n"
        "# worker_count = 2\n"
        "# message_max_chars = 4096\n"
        "# progress_update_interval = 0.8\n"
        "\n# [channels.discord]\n"
        "# enabled = false\n"
        '# account_id = "default"\n'
        '# bot_token_env = "MINICLAW_DISCORD_BOT_TOKEN"\n'
        "# owner_user_id = 0\n"
        "# allowed_user_ids = []\n"
        "# allowed_guild_ids = []\n"
        "# allowed_channel_ids = []\n"
        "# allow_guild_mentions = false\n"
        "# queue_size = 64\n"
        "# worker_count = 2\n"
        "# message_max_chars = 2000\n"
        "# progress_update_interval = 1.0\n"
        "# typing_renew_interval = 8.0\n"
    )
