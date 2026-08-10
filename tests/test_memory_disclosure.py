"""Memory Owner 身份与披露策略的失败关闭测试。"""

import unittest

from lobster0.memory.models import DisclosureContext, MemoryScope, MemoryStatus, SourceRef
from lobster0.memory.policy import MemoryDisclosurePolicy, MemoryPolicyError


class MemoryDisclosurePolicyTest(unittest.TestCase):
    """验证私人记忆只对已验证 Owner 的本地或私聊请求开放。"""

    def setUp(self) -> None:
        """创建无外部依赖的披露策略。"""
        self.policy = MemoryDisclosurePolicy()

    def decide(
        self,
        *,
        requester_user_id: int | None = 1,
        channel: str = "cli",
        conversation_kind: str = "local",
        identity_verified: bool = True,
    ):
        """构造固定 Owner 的请求并返回披露决策。"""
        return self.policy.decide(
            DisclosureContext(
                owner_id=1,
                requester_user_id=requester_user_id,
                channel=channel,
                conversation_kind=conversation_kind,
                identity_verified=identity_verified,
            )
        )

    def test_verified_owner_private_contexts_receive_private_memory(self) -> None:
        """删除 local/direct 任一允许分支都应让本测试失败。"""
        local = self.decide()
        direct = self.decide(channel="feishu", conversation_kind="direct")

        self.assertEqual(local.private_access, "full")
        self.assertEqual(local.capture_scope, "private")
        self.assertEqual(local.reason_code, "verified_owner_local")
        self.assertEqual(direct.private_access, "full")
        self.assertEqual(direct.capture_scope, "private")
        self.assertEqual(direct.reason_code, "verified_owner_direct")

    def test_group_non_owner_and_unknown_contexts_fail_closed(self) -> None:
        """任何把群聊或未验证请求扩大为私人访问的变更都应失败。"""
        group = self.decide(channel="discord", conversation_kind="group")
        non_owner = self.decide(
            requester_user_id=2,
            channel="telegram",
            conversation_kind="direct",
            identity_verified=False,
        )
        unknown = self.decide(
            requester_user_id=None,
            channel="feishu",
            conversation_kind="unknown",
            identity_verified=False,
        )

        self.assertEqual(group.private_access, "deny")
        self.assertEqual(group.capture_scope, "public")
        self.assertEqual(group.reason_code, "verified_owner_group")
        self.assertEqual(non_owner.private_access, "deny")
        self.assertEqual(non_owner.capture_scope, "none")
        self.assertEqual(non_owner.reason_code, "identity_unverified")
        self.assertEqual(unknown.private_access, "deny")
        self.assertEqual(unknown.capture_scope, "none")
        self.assertEqual(unknown.reason_code, "identity_unverified")

    def test_verified_identity_cannot_select_another_owner(self) -> None:
        """把已验证 requester 绑定到其他 owner 必须被视为伪造。"""
        with self.assertRaisesRegex(MemoryPolicyError, "owner mismatch"):
            self.decide(requester_user_id=2)

    def test_invalid_or_impersonated_context_is_rejected(self) -> None:
        """非法 ID、渠道组合和本地冒充不能退化为普通 deny。"""
        invalid_contexts = (
            DisclosureContext(0, 1, "cli", "local", True),
            DisclosureContext(1, 0, "cli", "local", True),
            DisclosureContext(1, 1, "web", "direct", True),
            DisclosureContext(1, 1, "cli", "direct", True),
            DisclosureContext(1, 1, "feishu", "local", True),
            DisclosureContext(1, 1, "cli", "local", False),
        )

        for context in invalid_contexts:
            with self.subTest(context=context), self.assertRaises(MemoryPolicyError):
                self.policy.decide(context)


class MemoryModelContractTest(unittest.TestCase):
    """验证首批 Memory 公共模型使用封闭枚举和稳定来源。"""

    def test_scope_status_and_source_ref_are_typed_and_immutable(self) -> None:
        """把公开状态改为自由字符串或可变来源会破坏后续状态机。"""
        source = SourceRef(message_id=7, session_id=3, channel="feishu")

        self.assertEqual(MemoryScope.PRIVATE, "private")
        self.assertEqual(MemoryStatus.REVIEW_REQUIRED, "review_required")
        self.assertEqual(source.message_id, 7)
        with self.assertRaises((AttributeError, TypeError)):
            source.message_id = 8  # type: ignore[misc]

    def test_source_ref_rejects_invalid_ids_and_channels(self) -> None:
        """来源引用不能接受 bool、非正 ID 或未知渠道。"""
        invalid = (
            {"message_id": True, "session_id": 1, "channel": "cli"},
            {"message_id": 1, "session_id": 0, "channel": "cli"},
            {"message_id": 1, "session_id": 1, "channel": "web"},
        )

        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                SourceRef(**arguments)


if __name__ == "__main__":
    unittest.main()
