"""Owner Memory 的身份校验和失败关闭披露策略。"""

from miniclaw.memory.models import DisclosureContext, DisclosureDecision

_CHANNELS = frozenset({"cli", "feishu", "telegram", "discord"})
_REMOTE_CHANNELS = frozenset({"feishu", "telegram", "discord"})
_CONVERSATION_KINDS = frozenset({"local", "direct", "group", "unknown"})


class MemoryPolicyError(ValueError):
    """表示可信运行期提供了自相矛盾或非法的 Memory 身份边界。"""


class MemoryDisclosurePolicy:
    """只向已验证 Owner 的本地或私聊请求披露私人记忆。"""

    def decide(self, context: DisclosureContext) -> DisclosureDecision:
        """校验身份边界并返回私人读取与采集决策。

        Args:
            context: 由 Core 根据本地入口或 Channel Identity 构造的上下文。

        Returns:
            不包含记忆正文的稳定 DisclosureDecision。

        Raises:
            MemoryPolicyError: ID、渠道、会话类型或已验证身份自相矛盾。
        """
        self._validate(context)
        if not context.identity_verified:
            return DisclosureDecision("deny", "none", "identity_unverified")
        if context.requester_user_id != context.owner_id:
            raise MemoryPolicyError("memory owner mismatch")
        if context.conversation_kind == "local":
            return DisclosureDecision("full", "private", "verified_owner_local")
        if context.conversation_kind == "direct":
            return DisclosureDecision("full", "private", "verified_owner_direct")
        if context.conversation_kind == "group":
            return DisclosureDecision("deny", "public", "verified_owner_group")
        return DisclosureDecision("deny", "none", "conversation_unknown")

    def _validate(self, context: DisclosureContext) -> None:
        """拒绝不能安全解释的 ID、渠道和会话类型组合。"""
        if type(context.owner_id) is not int or context.owner_id <= 0:
            raise MemoryPolicyError("memory owner id is invalid")
        if context.requester_user_id is not None and (
            type(context.requester_user_id) is not int or context.requester_user_id <= 0
        ):
            raise MemoryPolicyError("memory requester id is invalid")
        if type(context.identity_verified) is not bool:
            raise MemoryPolicyError("memory identity verification is invalid")
        if context.channel not in _CHANNELS:
            raise MemoryPolicyError("memory channel is unsupported")
        if context.conversation_kind not in _CONVERSATION_KINDS:
            raise MemoryPolicyError("memory conversation kind is unsupported")
        if context.conversation_kind == "local" and context.channel != "cli":
            raise MemoryPolicyError("local memory context requires cli")
        if context.channel == "cli" and context.conversation_kind != "local":
            raise MemoryPolicyError("cli memory context must be local")
        if context.conversation_kind in {"direct", "group", "unknown"} and (
            context.channel not in _REMOTE_CHANNELS
        ):
            raise MemoryPolicyError("remote memory context requires a channel")
        if context.conversation_kind == "local" and not context.identity_verified:
            raise MemoryPolicyError("unverified local memory context")
        if context.identity_verified and context.requester_user_id != context.owner_id:
            raise MemoryPolicyError("memory owner mismatch")
