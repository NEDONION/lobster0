"""在 Automation 写库前拒绝 Secret、递归控制与不可信投递目标。"""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from lobster0.automation.models import DeliveryTarget
from lobster0.config import ChannelConfig, DiscordConfig, FeishuConfig, TelegramConfig
from lobster0.memory.models import ConversationKind
from lobster0.skills.loader import SkillError, SkillLoader

type DeliveryOriginChannel = Literal["feishu", "telegram", "discord", "cli"]

_MAX_PROMPT_BYTES = 64 * 1024
_MAX_SKILLS = 3
_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_PRIVATE_KEY = re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.IGNORECASE)
_BEARER_SECRET = re.compile(
    r"(?:authorization\s*:\s*)?bearer\s+[a-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)
_ASSIGNED_SECRET = re.compile(
    r"\b(?:api[_-]?key|secret|password|token)\b\s*[:=]\s*[\"']?[^\s\"']{6,}",
    re.IGNORECASE,
)
_DELIVERY_FIELDS = frozenset({"route", "channel", "account_id", "conversation_id"})
_RECURSIVE_MARKERS = (
    "ignorepolicy",
    "bypasspolicy",
    "callmanagetask",
    "createmanagetask",
    "createanothercron",
    "modifysystemprompt",
    "editsystemprompt",
    "edittaskledger",
    "modifytaskledger",
    "忽略安全策略",
    "绕过安全策略",
    "修改系统提示词",
    "创建另一个定时任务",
    "再次创建定时任务",
    "修改任务账本",
)


class AutomationGuardError(ValueError):
    """表示可安全返回且不包含 Prompt、Secret 或平台 ID 的 Guard 错误。"""

    def __init__(self, code: str) -> None:
        """保存稳定错误码，并把异常文本限制为同一个码。"""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class GuardedTaskInput:
    """保存已经过确定性扫描的 NFC Prompt 与 Skill 名称。"""

    prompt: str
    skill_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeliveryOrigin:
    """保存由 Core 构造、模型不能伪造的当前会话投递事实。"""

    channel: DeliveryOriginChannel
    account_id: str
    external_conversation_id: str
    conversation_kind: ConversationKind
    identity_verified: bool

    def __post_init__(self) -> None:
        """拒绝未知入口、空平台 ID 与 bool 伪造。"""
        if self.channel not in {"feishu", "telegram", "discord", "cli"}:
            raise ValueError("delivery origin channel is invalid")
        if not _non_empty(self.account_id) or not _non_empty(
            self.external_conversation_id
        ):
            raise ValueError("delivery origin identifiers must be non-empty")
        if self.conversation_kind not in {"local", "direct", "group", "unknown"}:
            raise ValueError("delivery origin conversation kind is invalid")
        if type(self.identity_verified) is not bool:
            raise ValueError("delivery origin identity flag must be bool")


class AutomationPromptGuard:
    """扫描 Task Prompt，并只允许当前 Skill catalog 中的名称。"""

    def __init__(self, skills: SkillLoader) -> None:
        """绑定只读取 metadata 的现有 SkillLoader。"""
        self._skills = skills

    def validate(
        self,
        prompt: str,
        skill_names: tuple[str, ...],
    ) -> GuardedTaskInput:
        """规范化并验证 Prompt 与 Skill 引用，失败时不回显输入。

        参数：
            prompt: 将作为后台任务输入持久化的正文。
            skill_names: 用户请求启用的固定 Skill 名称。

        返回：
            可以安全写入 Task Ledger 的规范化输入。

        异常：
            AutomationGuardError: 正文或 Skill 违反硬边界。
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise AutomationGuardError("task_prompt_invalid")
        normalized = unicodedata.normalize("NFC", prompt).strip()
        if len(normalized.encode("utf-8")) > _MAX_PROMPT_BYTES:
            raise AutomationGuardError("task_prompt_too_large")
        if any(_unsafe_control(character) for character in normalized):
            raise AutomationGuardError("task_prompt_control_character")
        if _contains_secret(normalized):
            raise AutomationGuardError("task_prompt_secret")
        if _contains_recursive_control(normalized):
            raise AutomationGuardError("recursive_automation_denied")
        validated_skills = self._validate_skills(skill_names)
        return GuardedTaskInput(prompt=normalized, skill_names=validated_skills)

    def validate_skills(self, skill_names: tuple[str, ...]) -> tuple[str, ...]:
        """只验证显式 Skill 名称，供不修改 Prompt 的 Task update 使用。"""
        return self._validate_skills(skill_names)

    def _validate_skills(self, skill_names: tuple[str, ...]) -> tuple[str, ...]:
        """把名称限制为最多三个、无重复且存在于 metadata catalog。"""
        if not isinstance(skill_names, tuple) or len(skill_names) > _MAX_SKILLS:
            raise AutomationGuardError("task_skill_count")
        invalid_name = any(
            not isinstance(name, str) or _SKILL_NAME.fullmatch(name) is None
            for name in skill_names
        )
        if invalid_name:
            raise AutomationGuardError("task_skill_name")
        if len(set(skill_names)) != len(skill_names):
            raise AutomationGuardError("task_skill_duplicate")
        try:
            available = {metadata.name for metadata in self._skills.catalog()}
        except SkillError as exc:
            raise AutomationGuardError("task_skill_catalog") from exc
        if any(name not in available for name in skill_names):
            raise AutomationGuardError("task_skill_unknown")
        return skill_names


def resolve_delivery_target(
    requested: Mapping[str, object],
    origin: DeliveryOrigin | None,
    config: ChannelConfig,
) -> DeliveryTarget:
    """把不可信投递请求解析成创建时冻结的 allowlisted ``DeliveryTarget``。

    参数：
        requested: Tool/CLI 提交的严格 route mapping。
        origin: Core 注入的当前可信会话事实；CLI 可为空。
        config: 已通过配置校验的三平台配置。

    返回：
        不再依赖模型文本的平台目标；CLI origin 默认静默。

    异常：
        AutomationGuardError: 字段、来源、平台或 allowlist 不满足要求。
    """
    if not isinstance(requested, Mapping) or set(requested) - _DELIVERY_FIELDS:
        raise AutomationGuardError("delivery_fields")
    route = requested.get("route")
    if not isinstance(route, str) or route not in {"origin", "owner", "explicit", "none"}:
        raise AutomationGuardError("delivery_route")
    if route == "none":
        if set(requested) != {"route"}:
            raise AutomationGuardError("delivery_fields")
        return DeliveryTarget(route="none", channel="none")
    if route == "origin":
        if set(requested) != {"route"}:
            raise AutomationGuardError("delivery_fields")
        return _resolve_origin(origin, config)

    channel = requested.get("channel")
    if not isinstance(channel, str) or channel not in {"feishu", "telegram", "discord"}:
        raise AutomationGuardError("delivery_channel")
    channel_config = _enabled_channel_config(channel, config)
    if route == "owner":
        if set(requested) != {"route", "channel"}:
            raise AutomationGuardError("delivery_fields")
        return _resolve_owner(channel, channel_config, origin)

    if set(requested) != {"route", "channel", "account_id", "conversation_id"}:
        raise AutomationGuardError("delivery_fields")
    account_id = requested.get("account_id")
    conversation_id = requested.get("conversation_id")
    if not _non_empty(account_id) or not _non_empty(conversation_id):
        raise AutomationGuardError("delivery_target_invalid")
    if account_id != channel_config.account_id:
        raise AutomationGuardError("delivery_target_denied")
    if not _explicit_allowed(channel, conversation_id, channel_config):
        raise AutomationGuardError("delivery_target_denied")
    return DeliveryTarget(
        route="explicit",
        channel=channel,
        account_id=account_id,
        conversation_id=conversation_id,
    )


def _unsafe_control(character: str) -> bool:
    """拒绝 C0/C1 与不可见格式字符，但保留换行和 Tab。"""
    if character in {"\n", "\t"}:
        return False
    return unicodedata.category(character) in {"Cc", "Cf"}


def _contains_secret(prompt: str) -> bool:
    """识别 private key、Bearer 值和常见 secret 赋值。"""
    return any(pattern.search(prompt) is not None for pattern in (
        _PRIVATE_KEY,
        _BEARER_SECRET,
        _ASSIGNED_SECRET,
    ))


def _contains_recursive_control(prompt: str) -> bool:
    """忽略大小写与空白/标点后识别递归控制面指令。"""
    compact = "".join(character for character in prompt.casefold() if character.isalnum())
    return any(marker in compact for marker in _RECURSIVE_MARKERS)


def _resolve_origin(
    origin: DeliveryOrigin | None,
    config: ChannelConfig,
) -> DeliveryTarget:
    """只接受 verified direct IM origin；本地 CLI 显式降级为静默。"""
    if origin is not None and origin.channel == "cli":
        return DeliveryTarget(route="none", channel="none")
    if (
        origin is None
        or not origin.identity_verified
        or origin.conversation_kind != "direct"
    ):
        raise AutomationGuardError("delivery_origin_untrusted")
    channel_config = _enabled_channel_config(origin.channel, config)
    if origin.account_id != channel_config.account_id:
        raise AutomationGuardError("delivery_origin_untrusted")
    return DeliveryTarget(
        route="origin",
        channel=origin.channel,
        account_id=origin.account_id,
        conversation_id=origin.external_conversation_id,
    )


def _resolve_owner(
    channel: str,
    channel_config: FeishuConfig | TelegramConfig | DiscordConfig,
    origin: DeliveryOrigin | None,
) -> DeliveryTarget:
    """解析配置中可确定的 Owner 私聊，否则使用同平台可信 origin。"""
    if channel == "telegram" and isinstance(channel_config, TelegramConfig):
        if channel_config.owner_user_id <= 0:
            raise AutomationGuardError("delivery_target_unavailable")
        return DeliveryTarget(
            route="owner",
            channel="telegram",
            account_id=channel_config.account_id,
            conversation_id=f"chat:{channel_config.owner_user_id}",
        )
    if (
        origin is not None
        and origin.channel == channel
        and origin.identity_verified
        and origin.conversation_kind == "direct"
        and origin.account_id == channel_config.account_id
    ):
        return DeliveryTarget(
            route="owner",
            channel=channel,
            account_id=origin.account_id,
            conversation_id=origin.external_conversation_id,
        )
    raise AutomationGuardError("delivery_target_unavailable")


def _enabled_channel_config(
    channel: str,
    config: ChannelConfig,
) -> FeishuConfig | TelegramConfig | DiscordConfig:
    """返回已启用的平台配置，否则拒绝主动投递。"""
    selected: FeishuConfig | TelegramConfig | DiscordConfig
    if channel == "feishu":
        selected = config.feishu
    elif channel == "telegram":
        selected = config.telegram
    elif channel == "discord":
        selected = config.discord
    else:
        raise AutomationGuardError("delivery_channel")
    if not selected.enabled:
        raise AutomationGuardError("delivery_channel_disabled")
    return selected


def _explicit_allowed(
    channel: str,
    conversation_id: str,
    config: FeishuConfig | TelegramConfig | DiscordConfig,
) -> bool:
    """按平台内部 conversation key 语法校验静态 allowlist。"""
    if channel == "feishu" and isinstance(config, FeishuConfig):
        return conversation_id in config.allowed_chat_ids
    if channel == "telegram" and isinstance(config, TelegramConfig):
        match = re.fullmatch(r"chat:(-?[1-9][0-9]*)(?::topic:[1-9][0-9]*)?", conversation_id)
        if match is None:
            return False
        chat_id = int(match.group(1))
        return chat_id == config.owner_user_id or chat_id in config.allowed_chat_ids
    if channel == "discord" and isinstance(config, DiscordConfig):
        direct = re.fullmatch(r"channel:([1-9][0-9]*)", conversation_id)
        if direct is not None:
            return int(direct.group(1)) in config.allowed_channel_ids
        guild = re.fullmatch(
            r"guild:([1-9][0-9]*):channel:([1-9][0-9]*)(?::thread:[1-9][0-9]*)?",
            conversation_id,
        )
        return guild is not None and (
            int(guild.group(1)) in config.allowed_guild_ids
            and int(guild.group(2)) in config.allowed_channel_ids
        )
    return False


def _non_empty(value: object) -> bool:
    """判断值是否为不含 NUL 的非空字符串。"""
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value
