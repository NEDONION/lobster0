"""MiniClaw 的 TOML 配置、环境变量覆盖与边界校验。"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from miniclaw.paths import StatePaths

type OverrideValue = str | int | Path

BUILTIN_TOOL_NAMES = (
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
)

_TOP_LEVEL_KEYS = frozenset(
    {"agent", "provider", "workspace", "tools", "ui", "channels"}
)
_AGENT_KEYS = frozenset(
    {"model", "max_tool_iterations", "context_budget_tokens", "tool_result_max_chars"}
)
_PROVIDER_KEYS = frozenset({"base_url", "api_key_env", "timeout_seconds"})
_WORKSPACE_KEYS = frozenset({"path", "read_only_roots"})
_TOOLS_KEYS = frozenset(
    {"enabled", "security", "ask", "approval_ttl_seconds", "run_command", "http_get"}
)
_RUN_COMMAND_KEYS = frozenset(
    {"allow_commands", "timeout_seconds", "max_timeout_seconds"}
)
_HTTP_GET_KEYS = frozenset({"allow_hosts", "timeout_seconds", "max_response_bytes"})
_UI_KEYS = frozenset({"language"})
_CHANNELS_KEYS = frozenset({"feishu"})
_FEISHU_KEYS = frozenset(
    {
        "enabled",
        "account_id",
        "app_id_env",
        "app_secret_env",
        "domain",
        "owner_open_id",
        "allowed_open_ids",
        "allowed_chat_ids",
        "allow_group_mentions",
        "queue_size",
        "worker_count",
        "message_max_chars",
        "streaming_card",
    }
)
_OVERRIDE_KEYS = frozenset({"model", "base_url", "api_key_env", "workspace"})
_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_FEISHU_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_-]{1,128}\Z")
_FEISHU_CHAT_ID = re.compile(r"oc_[A-Za-z0-9_-]{1,128}\Z")


class ConfigError(ValueError):
    """表示配置文件、环境变量或显式覆盖值无效。"""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """保存 Agent 运行预算与模型名称。"""

    model: str = "provider/model"
    max_tool_iterations: int = 8
    context_budget_tokens: int = 32_000
    tool_result_max_chars: int = 20_000


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """保存模型端点和密钥环境变量名，不保存密钥值。"""

    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "MINICLAW_MODEL_API_KEY"
    timeout_seconds: int = 120


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """保存可写 Workspace 和额外只读根目录。"""

    path: Path
    read_only_roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandRule:
    """保存一条尚未解析 executable 的精确命令配置。"""

    program: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunCommandConfig:
    """保存命令 allowlist 与不可由模型放大的超时上限。"""

    allow_commands: tuple[CommandRule, ...] = ()
    timeout_seconds: int = 30
    max_timeout_seconds: int = 120


@dataclass(frozen=True, slots=True)
class HttpGetConfig:
    """保存 HTTPS 精确 hostname 和响应预算。"""

    allow_hosts: tuple[str, ...] = ()
    timeout_seconds: int = 20
    max_response_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ToolConfig:
    """保存 Phase 2 Tool 能力上限和审批默认值。"""

    enabled: tuple[str, ...] = BUILTIN_TOOL_NAMES
    security: str = "allowlist"
    ask: str = "on-miss"
    approval_ttl_seconds: int = 600
    run_command: RunCommandConfig = RunCommandConfig()
    http_get: HttpGetConfig = HttpGetConfig()


@dataclass(frozen=True, slots=True)
class UIConfig:
    """保存本地 TUI 的有限展示偏好。"""

    language: str = "zh-CN"


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    """保存飞书 Channel 的非秘密配置和本地资源预算。"""

    enabled: bool = False
    account_id: str = "default"
    app_id_env: str = "MINICLAW_FEISHU_APP_ID"
    app_secret_env: str = "MINICLAW_FEISHU_APP_SECRET"
    domain: str = "feishu"
    owner_open_id: str = ""
    allowed_open_ids: tuple[str, ...] = ()
    allowed_chat_ids: tuple[str, ...] = ()
    allow_group_mentions: bool = False
    queue_size: int = 64
    worker_count: int = 2
    message_max_chars: int = 30_000
    streaming_card: bool = True


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    """汇总当前实例启用的 IM Channel 配置。"""

    feishu: FeishuConfig = FeishuConfig()


@dataclass(frozen=True, slots=True)
class AppConfig:
    """汇总 Phase 0 已实现的强类型配置。"""

    agent: AgentConfig
    provider: ProviderConfig
    workspace: WorkspaceConfig
    tools: ToolConfig = ToolConfig()
    ui: UIConfig = UIConfig()
    channels: ChannelConfig = ChannelConfig()


def load_config(
    paths: StatePaths,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, OverrideValue] | None = None,
) -> AppConfig:
    """按默认值、TOML、环境变量和显式值的顺序加载配置。

    Args:
        paths: 当前实例的状态路径。
        environ: 环境变量映射；默认使用当前进程环境。
        overrides: 调用方显式提供的单字段覆盖。

    Returns:
        已完成类型和安全校验的应用配置。

    Raises:
        ConfigError: 文件无法读取、TOML 无效、字段未知或值不满足约束。
    """
    source = os.environ if environ is None else environ
    explicit = {} if overrides is None else dict(overrides)
    raw = _read_config(paths.config)
    _reject_unknown(raw, _TOP_LEVEL_KEYS, "")
    agent_raw = _section(raw, "agent", _AGENT_KEYS)
    provider_raw = _section(raw, "provider", _PROVIDER_KEYS)
    workspace_raw = _section(raw, "workspace", _WORKSPACE_KEYS)
    tools_raw = _section(raw, "tools", _TOOLS_KEYS)
    ui_raw = _section(raw, "ui", _UI_KEYS)
    channels_raw = _section(raw, "channels", _CHANNELS_KEYS)
    feishu_raw = _section(
        channels_raw,
        "feishu",
        _FEISHU_KEYS,
        parent="channels",
    )
    run_command_raw = _section(
        tools_raw,
        "run_command",
        _RUN_COMMAND_KEYS,
        parent="tools",
    )
    http_get_raw = _section(
        tools_raw,
        "http_get",
        _HTTP_GET_KEYS,
        parent="tools",
    )
    _reject_unknown(explicit, _OVERRIDE_KEYS, "override")

    model = _non_empty_string(agent_raw.get("model", "provider/model"), "agent.model")
    max_tool_iterations = _positive_integer(
        agent_raw.get("max_tool_iterations", 8), "agent.max_tool_iterations"
    )
    context_budget_tokens = _positive_integer(
        agent_raw.get("context_budget_tokens", 32_000), "agent.context_budget_tokens"
    )
    tool_result_max_chars = _positive_integer(
        agent_raw.get("tool_result_max_chars", 20_000), "agent.tool_result_max_chars"
    )
    base_url = _provider_url(
        provider_raw.get("base_url", "https://api.openai.com/v1"), "provider.base_url"
    )
    api_key_env = _environment_variable_name(
        provider_raw.get("api_key_env", "MINICLAW_MODEL_API_KEY"),
        "provider.api_key_env",
    )
    timeout_seconds = _positive_integer(
        provider_raw.get("timeout_seconds", 120), "provider.timeout_seconds"
    )
    workspace_path = _absolute_path(
        workspace_raw.get("path", paths.workspace), "workspace.path"
    )
    read_only_roots = _absolute_path_list(
        workspace_raw.get("read_only_roots", []), "workspace.read_only_roots"
    )
    enabled_tools = _enabled_tools(tools_raw.get("enabled", list(BUILTIN_TOOL_NAMES)))
    tool_security = _enum_string(
        tools_raw.get("security", "allowlist"),
        "tools.security",
        frozenset({"deny", "allowlist", "full"}),
    )
    tool_ask = _enum_string(
        tools_raw.get("ask", "on-miss"),
        "tools.ask",
        frozenset({"off", "on-miss", "always"}),
    )
    approval_ttl_seconds = _positive_integer(
        tools_raw.get("approval_ttl_seconds", 600),
        "tools.approval_ttl_seconds",
    )
    command_timeout = _positive_integer(
        run_command_raw.get("timeout_seconds", 30),
        "tools.run_command.timeout_seconds",
    )
    command_max_timeout = _bounded_positive_integer(
        run_command_raw.get("max_timeout_seconds", 120),
        "tools.run_command.max_timeout_seconds",
        maximum=120,
    )
    if command_timeout > command_max_timeout:
        raise ConfigError(
            "tools.run_command.timeout_seconds must not exceed "
            "tools.run_command.max_timeout_seconds"
        )
    command_rules = _command_rules(run_command_raw.get("allow_commands", []))
    http_timeout = _bounded_positive_integer(
        http_get_raw.get("timeout_seconds", 20),
        "tools.http_get.timeout_seconds",
        maximum=120,
    )
    http_max_response_bytes = _bounded_positive_integer(
        http_get_raw.get("max_response_bytes", 2 * 1024 * 1024),
        "tools.http_get.max_response_bytes",
        maximum=2 * 1024 * 1024,
    )
    http_hosts = _string_list(
        http_get_raw.get("allow_hosts", []),
        "tools.http_get.allow_hosts",
        allow_empty=False,
    )
    ui_language = _enum_string(
        ui_raw.get("language", "zh-CN"),
        "ui.language",
        frozenset({"zh-CN", "en"}),
    )
    feishu_enabled = _boolean(
        feishu_raw.get("enabled", False),
        "channels.feishu.enabled",
    )
    feishu_account_id = _account_id(
        feishu_raw.get("account_id", "default"),
        "channels.feishu.account_id",
    )
    feishu_app_id_env = _environment_variable_name(
        feishu_raw.get("app_id_env", "MINICLAW_FEISHU_APP_ID"),
        "channels.feishu.app_id_env",
    )
    feishu_app_secret_env = _environment_variable_name(
        feishu_raw.get("app_secret_env", "MINICLAW_FEISHU_APP_SECRET"),
        "channels.feishu.app_secret_env",
    )
    feishu_domain = _enum_string(
        feishu_raw.get("domain", "feishu"),
        "channels.feishu.domain",
        frozenset({"feishu", "lark"}),
    )
    feishu_owner_open_id = _optional_platform_id(
        feishu_raw.get("owner_open_id", ""),
        "channels.feishu.owner_open_id",
        _FEISHU_OPEN_ID,
    )
    feishu_allowed_open_ids = _platform_id_list(
        feishu_raw.get("allowed_open_ids", []),
        "channels.feishu.allowed_open_ids",
        _FEISHU_OPEN_ID,
    )
    feishu_allowed_chat_ids = _platform_id_list(
        feishu_raw.get("allowed_chat_ids", []),
        "channels.feishu.allowed_chat_ids",
        _FEISHU_CHAT_ID,
    )
    feishu_allow_group_mentions = _boolean(
        feishu_raw.get("allow_group_mentions", False),
        "channels.feishu.allow_group_mentions",
    )
    feishu_queue_size = _bounded_integer(
        feishu_raw.get("queue_size", 64),
        "channels.feishu.queue_size",
        minimum=1,
        maximum=1024,
    )
    feishu_worker_count = _bounded_integer(
        feishu_raw.get("worker_count", 2),
        "channels.feishu.worker_count",
        minimum=1,
        maximum=8,
    )
    feishu_message_max_chars = _bounded_integer(
        feishu_raw.get("message_max_chars", 30_000),
        "channels.feishu.message_max_chars",
        minimum=1000,
        maximum=30_000,
    )
    feishu_streaming_card = _boolean(
        feishu_raw.get("streaming_card", True),
        "channels.feishu.streaming_card",
    )
    _validate_feishu_relationships(
        enabled=feishu_enabled,
        owner_open_id=feishu_owner_open_id,
        allowed_open_ids=feishu_allowed_open_ids,
        allowed_chat_ids=feishu_allowed_chat_ids,
        allow_group_mentions=feishu_allow_group_mentions,
    )

    model = _environment_string(source, "MINICLAW_MODEL_NAME", model)
    max_tool_iterations = _environment_integer(
        source, "MINICLAW_MAX_TOOL_ITERATIONS", max_tool_iterations
    )
    context_budget_tokens = _environment_integer(
        source, "MINICLAW_CONTEXT_BUDGET_TOKENS", context_budget_tokens
    )
    tool_result_max_chars = _environment_integer(
        source, "MINICLAW_TOOL_RESULT_MAX_CHARS", tool_result_max_chars
    )
    base_url = _environment_url(source, "MINICLAW_MODEL_BASE_URL", base_url)
    api_key_env = _environment_name(
        source, "MINICLAW_MODEL_API_KEY_ENV", api_key_env
    )
    timeout_seconds = _environment_integer(
        source, "MINICLAW_MODEL_TIMEOUT_SECONDS", timeout_seconds
    )
    workspace_path = _environment_path(source, "MINICLAW_WORKSPACE", workspace_path)

    if "model" in explicit:
        model = _non_empty_string(explicit["model"], "override.model")
    if "base_url" in explicit:
        base_url = _provider_url(explicit["base_url"], "override.base_url")
    if "api_key_env" in explicit:
        api_key_env = _environment_variable_name(
            explicit["api_key_env"], "override.api_key_env"
        )
    if "workspace" in explicit:
        workspace_path = _absolute_path(explicit["workspace"], "override.workspace")

    return AppConfig(
        agent=AgentConfig(
            model=model,
            max_tool_iterations=max_tool_iterations,
            context_budget_tokens=context_budget_tokens,
            tool_result_max_chars=tool_result_max_chars,
        ),
        provider=ProviderConfig(
            base_url=base_url,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
        ),
        workspace=WorkspaceConfig(path=workspace_path, read_only_roots=read_only_roots),
        tools=ToolConfig(
            enabled=enabled_tools,
            security=tool_security,
            ask=tool_ask,
            approval_ttl_seconds=approval_ttl_seconds,
            run_command=RunCommandConfig(
                allow_commands=command_rules,
                timeout_seconds=command_timeout,
                max_timeout_seconds=command_max_timeout,
            ),
            http_get=HttpGetConfig(
                allow_hosts=http_hosts,
                timeout_seconds=http_timeout,
                max_response_bytes=http_max_response_bytes,
            ),
        ),
        ui=UIConfig(language=ui_language),
        channels=ChannelConfig(
            feishu=FeishuConfig(
                enabled=feishu_enabled,
                account_id=feishu_account_id,
                app_id_env=feishu_app_id_env,
                app_secret_env=feishu_app_secret_env,
                domain=feishu_domain,
                owner_open_id=feishu_owner_open_id,
                allowed_open_ids=feishu_allowed_open_ids,
                allowed_chat_ids=feishu_allowed_chat_ids,
                allow_group_mentions=feishu_allow_group_mentions,
                queue_size=feishu_queue_size,
                worker_count=feishu_worker_count,
                message_max_chars=feishu_message_max_chars,
                streaming_card=feishu_streaming_card,
            )
        ),
    )


def _read_config(path: Path) -> dict[str, object]:
    """读取 TOML 文件；文件尚不存在时返回空配置。"""
    try:
        with path.open("rb") as config_file:
            loaded = tomllib.load(config_file)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}") from error
    except OSError as error:
        raise ConfigError(f"cannot read configuration file {path}: {error.strerror}") from error
    return cast(dict[str, object], loaded)


def _section(
    raw: Mapping[str, object],
    name: str,
    allowed_keys: frozenset[str],
    *,
    parent: str = "",
) -> dict[str, object]:
    """读取并校验一个命名 TOML 表。"""
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table")
    section = cast(dict[str, object], value)
    prefix = f"{parent}.{name}" if parent else name
    _reject_unknown(section, allowed_keys, prefix)
    return section


def _reject_unknown(
    values: Mapping[str, object],
    allowed: frozenset[str],
    prefix: str,
) -> None:
    """拒绝未知字段，避免拼写错误被静默忽略。"""
    unknown = sorted(set(values) - allowed)
    if unknown:
        key = f"{prefix}.{unknown[0]}" if prefix else unknown[0]
        raise ConfigError(f"unknown configuration key: {key}")


def _non_empty_string(value: object, name: str) -> str:
    """校验并规范化非空字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_integer(value: object, name: str) -> int:
    """校验严格正整数，并显式排除布尔值。"""
    if type(value) is not int or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _bounded_positive_integer(value: object, name: str, *, maximum: int) -> int:
    """校验正整数没有超过固定安全上限。"""
    number = _positive_integer(value, name)
    if number > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return number


def _bounded_integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """校验整数同时位于不可由配置突破的上下界内。"""
    if type(value) is not int or value < minimum or value > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(value: object, name: str) -> bool:
    """严格接受 TOML 布尔值，拒绝整数等 Python 真值。"""
    if type(value) is not bool:
        raise ConfigError(f"{name} must be a boolean")
    return value


def _enum_string(value: object, name: str, allowed: frozenset[str]) -> str:
    """校验配置字符串属于显式枚举。"""
    normalized = _non_empty_string(value, name)
    if normalized not in allowed:
        raise ConfigError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _enabled_tools(value: object) -> tuple[str, ...]:
    """保留用户顺序，同时拒绝未知或重复的内置 Tool 名。"""
    names = _string_list(value, "tools.enabled", allow_empty=True)
    if len(set(names)) != len(names) or any(name not in BUILTIN_TOOL_NAMES for name in names):
        raise ConfigError("tools.enabled must contain unique built-in tool names")
    return names


def _string_list(value: object, name: str, *, allow_empty: bool) -> tuple[str, ...]:
    """校验一个不含重复值的字符串列表。"""
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of strings")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or (not allow_empty and not item.strip()):
            raise ConfigError(f"{name} must be a list of strings")
        values.append(item.strip() if not allow_empty else item)
    if len(set(values)) != len(values):
        raise ConfigError(f"{name} must not contain duplicates")
    return tuple(values)


def _account_id(value: object, name: str) -> str:
    """校验仅用于本地分区的非秘密 Channel account 标识。"""
    account_id = _non_empty_string(value, name)
    if _ACCOUNT_ID.fullmatch(account_id) is None:
        raise ConfigError(f"{name} must be a lowercase account identifier")
    return account_id


def _optional_platform_id(
    value: object,
    name: str,
    pattern: re.Pattern[str],
) -> str:
    """校验一个允许在 Channel 未启用时为空的平台 ID。"""
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a platform identifier")
    normalized = value.strip()
    if normalized and pattern.fullmatch(normalized) is None:
        raise ConfigError(f"{name} must be a valid platform identifier")
    return normalized


def _platform_id_list(
    value: object,
    name: str,
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    """校验唯一、非空且前缀正确的平台 ID 列表。"""
    identifiers = _string_list(value, name, allow_empty=False)
    if any(pattern.fullmatch(identifier) is None for identifier in identifiers):
        raise ConfigError(f"{name} must contain valid platform identifiers")
    return identifiers


def _validate_feishu_relationships(
    *,
    enabled: bool,
    owner_open_id: str,
    allowed_open_ids: tuple[str, ...],
    allowed_chat_ids: tuple[str, ...],
    allow_group_mentions: bool,
) -> None:
    """校验飞书开关、Owner 与两层白名单之间的组合关系。"""
    if not enabled:
        return
    if not owner_open_id:
        raise ConfigError("channels.feishu.owner_open_id is required when enabled")
    if owner_open_id not in allowed_open_ids:
        raise ConfigError(
            "channels.feishu.owner_open_id must be present in "
            "channels.feishu.allowed_open_ids"
        )
    if allow_group_mentions and not allowed_chat_ids:
        raise ConfigError(
            "channels.feishu.allowed_chat_ids is required when group mentions are enabled"
        )


def _argument_list(value: object, name: str) -> tuple[str, ...]:
    """校验 argv 字符串列表，同时保留有语义的重复和空参数。"""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be a list of strings")
    return tuple(value)


def _command_rules(value: object) -> tuple[CommandRule, ...]:
    """校验 exact command 配置，不在配置层解析本机 executable。"""
    if not isinstance(value, list):
        raise ConfigError("tools.run_command.allow_commands must be a list")
    rules: list[CommandRule] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("tools.run_command.allow_commands must contain tables")
        _reject_unknown(item, frozenset({"program", "args"}), "tools.run_command.allow_commands")
        if set(item) != {"program", "args"}:
            raise ConfigError("tools.run_command.allow_commands requires program and args")
        program = _non_empty_string(item["program"], "tools.run_command.allow_commands.program")
        args = _argument_list(item["args"], "tools.run_command.allow_commands.args")
        rule = CommandRule(program, args)
        if rule in rules:
            raise ConfigError("tools.run_command.allow_commands must not contain duplicates")
        rules.append(rule)
    return tuple(rules)


def _absolute_path(value: object, name: str) -> Path:
    """校验并解析一个绝对路径。"""
    if not isinstance(value, (str, Path)):
        raise ConfigError(f"{name} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return path.resolve(strict=False)


def _absolute_path_list(value: object, name: str) -> tuple[Path, ...]:
    """校验绝对路径列表并保持原有顺序。"""
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of absolute paths")
    return tuple(_absolute_path(item, name) for item in value)


def _provider_url(value: object, name: str) -> str:
    """校验不含凭证、查询或片段的 HTTP(S) Provider URL。"""
    url = _non_empty_string(value, name)
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(f"{name} must be an HTTP(S) URL without credentials or query data")
    return url.rstrip("/")


def _environment_variable_name(value: object, name: str) -> str:
    """校验仅由安全字符组成的环境变量名。"""
    variable_name = _non_empty_string(value, name)
    if _ENVIRONMENT_NAME.fullmatch(variable_name) is None:
        raise ConfigError(f"{name} must be an uppercase environment variable name")
    return variable_name


def _environment_string(source: Mapping[str, str], key: str, default: str) -> str:
    """读取可选的非空字符串环境变量。"""
    return default if key not in source else _non_empty_string(source[key], key)


def _environment_integer(source: Mapping[str, str], key: str, default: int) -> int:
    """读取可选的正整数环境变量。"""
    if key not in source:
        return default
    try:
        value = int(source[key])
    except ValueError as error:
        raise ConfigError(f"{key} must be a positive integer") from error
    return _positive_integer(value, key)


def _environment_url(source: Mapping[str, str], key: str, default: str) -> str:
    """读取可选的 Provider URL 环境变量。"""
    return default if key not in source else _provider_url(source[key], key)


def _environment_name(source: Mapping[str, str], key: str, default: str) -> str:
    """读取可选的密钥环境变量名覆盖。"""
    return default if key not in source else _environment_variable_name(source[key], key)


def _environment_path(source: Mapping[str, str], key: str, default: Path) -> Path:
    """读取可选的绝对路径环境变量。"""
    return default if key not in source else _absolute_path(source[key], key)
