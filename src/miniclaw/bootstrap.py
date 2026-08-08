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
    feishu_skill_directory = paths.skills / "feishu-lark-cli"
    github_skill_directory = paths.skills / "github-cli"
    _ensure_directory(example_skill_directory)
    _ensure_directory(feishu_skill_directory)
    _ensure_directory(github_skill_directory)

    templates = (
        (paths.config, _render_default_config(paths)),
        (paths.soul, "# MiniClaw\n"),
        (paths.user, "# User\n"),
        (paths.memory_file, "# Long-term Memory\n"),
        (
            github_skill_directory / "SKILL.md",
            "---\n"
            "name: github-cli\n"
            "description: GitHub github 仓库 repository repo pinned 置顶 私有 public private "
            "issue pull request PR workflow actions；使用本机 gh 和 git CLI 查询或操作。\n"
            "version: 1\n"
            "---\n\n"
            "# GitHub CLI instructions\n\n"
            "GitHub 是外部服务。用户询问 GitHub 账号、仓库、pinned repositories、Issue、"
            "Pull Request、Actions 或要求操作仓库时，必须使用现有 `run_command` 调用本机 "
            "`gh` 或 `git`，并依据真实 Tool 结果回答。不能在尚未调用 Tool 时声称网络、权限、"
            "认证或 GitHub 不可用。\n\n"
            "## 基本规则\n\n"
            "- 查询 Owner 的远端 GitHub 数据前，先调用 `gh auth status --hostname github.com`。"
            "检查返回的 `exit_code` 和输出；登录失效时只报告需要重新执行安全登录流程。\n"
            "- GitHub 远端读取和写入优先使用 `gh`；本地仓库状态、日志和差异使用 `git`。"
            "两者都通过 `run_command` 传入单个 program 和精确 `args`，不得拼 Shell。\n"
            "- 不得在命令参数中传入 Token、PAT、Authorization Header 或其他凭据，也不得读取、"
            "打印或记忆凭据文件。让 `gh` 使用它自己的本机认证存储。\n"
            "- Tool 返回非零 `exit_code` 时，以实际错误为准；只有 Tool 明确返回 Policy "
            "或网络错误时，"
            "才能说请求被相应边界阻止。\n"
            "- 创建、删除、合并、push、修改 Issue/PR/Workflow 等外部变更只在用户明确要求后执行；"
            "只读查询可以直接执行。\n\n"
            "## Pinned repositories\n\n"
            "用户询问自己的 pinned/置顶仓库时，在认证成功后调用：\n\n"
            "```text\n"
            "gh api graphql -f query='query { viewer { pinnedItems(first: 6, "
            "types: [REPOSITORY]) { nodes { ... on Repository { nameWithOwner description "
            "url isPrivate } } } } }'\n"
            "```\n\n"
            "在 `run_command` 中把 `gh` 作为 program，把 `api`、`graphql`、`-f` 和完整 query "
            "分别作为 args 元素；不要把单引号本身传给 `gh`。"
            "只根据返回的仓库字段生成 bullet points。\n",
        ),
        (
            feishu_skill_directory / "SKILL.md",
            "---\n"
            "name: feishu-lark-cli\n"
            "description: 飞书 Lark 文档 云盘 Wiki 表格 Base 消息 日历 任务 审批 邮件；"
            "把自然语言映射为 official lark-cli 命令。\n"
            "version: 1\n"
            "---\n\n"
            "# Feishu / Lark CLI instructions\n\n"
            "飞书是外部云服务，不是本地文件。用户询问飞书文档、云盘、Wiki、表格、消息、"
            "日历、任务、审批或邮件时，使用现有 `run_command` 直接调用官方 `lark-cli`；"
            "不要搜索本地 Workspace，也不能在 `run_command` 已提供时声称没有飞书 API 工具。\n\n"
            "## 基本规则\n\n"
            "- `run_command.program` 固定写 `lark-cli`，完整参数放进字符串数组 `args`。\n"
            "- 访问 Owner 自己的数据时使用 `--as user`；"
            "不要把凭据交给模型或写入参数。\n"
            "- 不确定命令时，先调用 `lark-cli <domain> --help` 或 "
            "`lark-cli schema ... --format json`，"
            "再执行目标命令；不得猜参数。\n"
            "- 只依据 CLI 返回的标题、时间、URL 和正文回答，不能编造结果。CLI 错误是权威边界。\n"
            "- 写入、删除、发送、移动等动作仍通过同一个 `run_command` 进入 Policy / Approval。"
            "用户未明确批准高风险动作时，不得自行追加 `--yes`。\n"
            "- 登录失效时只报告需要重新完成 `lark-cli` 用户认证，不索要或读取凭据文件。\n\n"
            "## 最近修改的文档\n\n"
            "用户没有提供业务关键词，只问最近修改、编辑或更改的文档时，用空 query 和编辑时间过滤。"
            "例如“最近更改的两个飞书文档”直接调用：\n\n"
            "```text\n"
            "lark-cli drive +search --as user --query \"\" --edited-since 30d "
            "--sort edit_time --page-size 2 --format json\n"
            "```\n\n"
            "对应 `run_command` 参数：\n\n"
            "```json\n"
            "{\"program\":\"lark-cli\",\"args\":[\"drive\",\"+search\",\"--as\",\"user\","
            "\"--query\",\"\",\"--edited-since\",\"30d\",\"--sort\",\"edit_time\","
            "\"--page-size\",\"2\",\"--format\",\"json\"]}\n"
            "```\n\n"
            "如果用户给了标题或内容关键词，把精简后的关键词放入 `--query`；"
            "纯列表请求保持空字符串。\n",
        ),
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
