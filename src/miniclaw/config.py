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

_TOP_LEVEL_KEYS = frozenset({"agent", "provider", "workspace"})
_AGENT_KEYS = frozenset(
    {"model", "max_tool_iterations", "context_budget_tokens", "tool_result_max_chars"}
)
_PROVIDER_KEYS = frozenset({"base_url", "api_key_env", "timeout_seconds"})
_WORKSPACE_KEYS = frozenset({"path", "read_only_roots"})
_OVERRIDE_KEYS = frozenset({"model", "base_url", "api_key_env", "workspace"})
_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")


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
class AppConfig:
    """汇总 Phase 0 已实现的强类型配置。"""

    agent: AgentConfig
    provider: ProviderConfig
    workspace: WorkspaceConfig


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
) -> dict[str, object]:
    """读取并校验一个命名 TOML 表。"""
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table")
    section = cast(dict[str, object], value)
    _reject_unknown(section, allowed_keys, name)
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
