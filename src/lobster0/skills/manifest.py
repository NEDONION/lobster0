"""Skill manifest v2：声明式权限、依赖与调用面，并保持 v1 向后兼容。

## 设计边界

manifest 只**声明**一个 Skill 需要什么，它本身不授予任何权限。真正的放行仍然由
``PolicyEngine`` 与 ``ToolExecutor`` 决定——manifest 的价值在于让 Owner 在安装前
一眼看清"这个 Skill 想要什么"，以及让 Core 能在安装期就拒绝明显越界的声明。

几条硬约束：

* ``required_env`` 只接受**变量名**。写成 ``NAME=value`` 一律拒绝，避免第三方 Skill
  把 Secret 明文写进仓库里的 Markdown。
* ``required_tools`` 只能引用 Core 真实注册过的 Tool。白名单从 Tool 模块实际声明的
  ``name=`` 处派生而不是手写副本，否则改名后白名单会静默漂移。
* 未知字段一律拒绝。新字段很可能是一个新的权限维度，静默忽略等于默认放行。
* 只有 ``name``/``description``/``version`` 的旧 Skill 继续按 v1 解析，且默认不声明
  任何能力——升级 manifest 不能让现有 Skill 突然获得权限。
"""

import hashlib
import re
from dataclasses import dataclass

from lobster0.skills.loader import SkillError

_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_BINARY_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_MAX_DESCRIPTION_CHARS = 500
_MAX_LIST_ITEMS = 16

_V1_FIELDS = frozenset({"name", "description", "version"})
_V2_OPTIONAL_FIELDS = frozenset(
    {
        "manifest_version",
        "license",
        "homepage",
        "required_tools",
        "required_binaries",
        "required_env",
        "supported_platforms",
        "model_invocable",
        "user_invocable",
    }
)
_SUPPORTED_PLATFORMS = frozenset({"darwin", "linux", "windows"})

KNOWN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "complete_task",
        "edit_file",
        "glob",
        "grep",
        "http_get",
        "manage_task",
        "memory_correct",
        "memory_flush",
        "memory_forget",
        "memory_get",
        "memory_list",
        "memory_remember",
        "memory_review_list",
        "memory_search",
        "propose_memory",
        "read_artifact",
        "read_file",
        "read_memory",
        "run_command",
        "system_info",
        "write_file",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class SkillManifest:
    """一个 Skill 的声明式能力需求；只含名称，不含任何 Secret 值。"""

    manifest_version: int
    name: str
    description: str
    version: int
    license: str | None
    homepage: str | None
    required_tools: tuple[str, ...]
    required_binaries: tuple[str, ...]
    required_env: tuple[str, ...]
    supported_platforms: tuple[str, ...]
    model_invocable: bool
    user_invocable: bool
    content_hash: str

    def __repr__(self) -> str:
        """只显示身份与版本；不展开依赖清单，避免日志里出现环境变量名清单。"""
        return (
            "SkillManifest("
            f"name={self.name!r}, version={self.version}, "
            f"manifest_version={self.manifest_version})"
        )


def parse_manifest(document: str, *, directory_name: str) -> SkillManifest:
    """把完整 ``SKILL.md`` 文本解析为严格校验后的 manifest。

    Args:
        document: 含 frontmatter 的完整 Skill 文本。
        directory_name: Skill 所在目录名；必须与 ``name`` 一致。

    Returns:
        v1 或 v2 manifest；v1 不声明任何能力。

    Raises:
        SkillError: frontmatter 缺失、字段未知、值越界或声明了不存在的 Tool。
    """
    values = _frontmatter(document)
    unknown = set(values) - _V1_FIELDS - _V2_OPTIONAL_FIELDS
    if unknown:
        raise SkillError(
            "invalid_skill_frontmatter",
            f"unsupported skill field: {sorted(unknown)[0]}",
        )
    missing = _V1_FIELDS - set(values)
    if missing:
        raise SkillError(
            "invalid_skill_frontmatter", f"skill field is missing: {sorted(missing)[0]}"
        )

    name = values["name"]
    if _SKILL_NAME.fullmatch(name) is None or name != directory_name:
        raise SkillError("invalid_skill_name", "skill name must match its directory")
    description = values["description"]
    if len(description) > _MAX_DESCRIPTION_CHARS:
        raise SkillError("invalid_skill_frontmatter", "skill description is too long")
    version = _positive_int(values["version"], "version")
    manifest_version = (
        _positive_int(values["manifest_version"], "manifest_version")
        if "manifest_version" in values
        else 1
    )
    if manifest_version not in {1, 2}:
        raise SkillError(
            "invalid_skill_frontmatter", "unsupported skill manifest_version"
        )

    tools = _names(values.get("required_tools"), "required_tools", _identifier)
    for tool in tools:
        if tool not in KNOWN_TOOL_NAMES:
            raise SkillError("invalid_skill_frontmatter", f"unknown required tool: {tool}")
    binaries = _names(values.get("required_binaries"), "required_binaries", _binary)
    env = _names(values.get("required_env"), "required_env", _env_name)
    platforms = _names(values.get("supported_platforms"), "supported_platforms", _platform)

    return SkillManifest(
        manifest_version=manifest_version,
        name=name,
        description=description,
        version=version,
        license=values.get("license"),
        homepage=_homepage(values.get("homepage")),
        required_tools=tools,
        required_binaries=binaries,
        required_env=env,
        supported_platforms=platforms,
        model_invocable=_boolean(values.get("model_invocable"), default=True),
        user_invocable=_boolean(values.get("user_invocable"), default=True),
        content_hash=hashlib.sha256(document.encode("utf-8")).hexdigest(),
    )


def _frontmatter(document: str) -> dict[str, str]:
    """解析仅含单行 scalar 的 frontmatter；不接受嵌套或重复键。"""
    lines = document.splitlines()
    if not lines or lines[0] != "---":
        raise SkillError("invalid_skill_frontmatter", "skill must start with frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise SkillError(
            "invalid_skill_frontmatter", "skill frontmatter is incomplete"
        ) from error
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            raise SkillError("invalid_skill_frontmatter", "skill field must be a scalar")
        key, value = (part.strip() for part in line.split(":", 1))
        if not key or not value or key in values:
            raise SkillError("invalid_skill_frontmatter", "skill field is invalid")
        values[key] = value
    return values


def _names(
    raw: str | None, field: str, validate
) -> tuple[str, ...]:
    """把逗号分隔的清单解析为去重、排序、逐项校验过的名称元组。"""
    if raw is None:
        return ()
    items = [piece.strip() for piece in raw.split(",")]
    if any(not piece for piece in items):
        raise SkillError("invalid_skill_frontmatter", f"{field} has an empty entry")
    if len(items) > _MAX_LIST_ITEMS:
        raise SkillError("invalid_skill_frontmatter", f"{field} declares too many entries")
    for piece in items:
        validate(piece, field)
    return tuple(sorted(set(items)))


def _identifier(value: str, field: str) -> None:
    """校验 Tool 名形状。"""
    if re.fullmatch(r"[a-z][a-z0-9_]*", value) is None:
        raise SkillError("invalid_skill_frontmatter", f"{field} has an invalid name")


def _binary(value: str, field: str) -> None:
    """校验可执行文件名；不接受路径，安装期再解析真实位置。"""
    if "/" in value or _BINARY_NAME.fullmatch(value) is None:
        raise SkillError("invalid_skill_frontmatter", f"{field} must be a bare binary name")


def _env_name(value: str, field: str) -> None:
    """只接受环境变量名；出现 ``=`` 说明有人想把 Secret 值写进来。"""
    if "=" in value:
        raise SkillError(
            "invalid_skill_frontmatter",
            f"{field} must declare environment variable names, not values",
        )
    if _ENV_NAME.fullmatch(value) is None:
        raise SkillError(
            "invalid_skill_frontmatter", f"{field} has an invalid environment name"
        )


def _platform(value: str, field: str) -> None:
    """平台只能取封闭集合。"""
    if value not in _SUPPORTED_PLATFORMS:
        raise SkillError("invalid_skill_frontmatter", f"{field} has an unknown platform")


def _homepage(value: str | None) -> str | None:
    """只接受 https 主页；不接受 http、file 或模型可构造的其他 scheme。"""
    if value is None:
        return None
    if not value.startswith("https://") or len(value) > 300:
        raise SkillError("invalid_skill_frontmatter", "skill homepage must be https")
    return value


def _boolean(value: str | None, *, default: bool) -> bool:
    """把明确的 true/false 解析为布尔；其他一律拒绝。"""
    if value is None:
        return default
    if value not in {"true", "false"}:
        raise SkillError("invalid_skill_frontmatter", "skill flag must be a boolean")
    return value == "true"


def _positive_int(value: str, field: str) -> int:
    """解析严格正整数，拒绝前导零与非数字。"""
    try:
        parsed = int(value)
    except ValueError as error:
        raise SkillError(
            "invalid_skill_version", f"skill {field} must be an integer"
        ) from error
    if parsed <= 0 or str(parsed) != value:
        raise SkillError("invalid_skill_version", f"skill {field} must be positive")
    return parsed
