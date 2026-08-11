"""TurnService 对 Context、Runner 和 SQLite 的编排测试。"""

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from lobster0.agent.context import ContextBuilder
from lobster0.agent.events import RunEvent
from lobster0.agent.runner import AgentNoProgressError, AgentRunBudget, AgentRunner
from lobster0.agent.turn import TurnExecutionProfile, TurnService, _model_message
from lobster0.artifacts.store import ArtifactError, ArtifactStore
from lobster0.bootstrap import initialize_state
from lobster0.config import WorkspaceConfig, load_config
from lobster0.paths import build_state_paths
from lobster0.policy.approvals import ApprovalDecision, ApprovalError
from lobster0.policy.engine import PolicyEngine
from lobster0.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderAuthenticationError,
    ProviderServerError,
    StreamHandler,
    ToolCall,
)
from lobster0.runtime import create_runtime
from lobster0.storage.conversations import (
    ConversationDataError,
    MessageRepository,
    SessionRepository,
    StoredMessage,
    TurnRepository,
)
from lobster0.storage.database import Database
from lobster0.storage.tooling import ApprovalRepository, ToolRunRepository
from lobster0.tools.base import ToolContext, ToolDefinition, ToolResult, ToolRisk
from lobster0.tools.executor import ToolExecutor
from lobster0.tools.filesystem import WriteFileTool
from lobster0.tools.registry import ToolRegistry
from lobster0.tools.system import SystemInfoTool
from lobster0.tools.task_completion import CompleteTaskTool
from tests.fakes.fake_provider import FakeProvider


class _AutomationContinuationProbe:
    """记录 Automation Approval continuation 的 durable Run hook。"""

    def __init__(self) -> None:
        """创建空事件列表。"""
        self.events: list[tuple[str, int, int | None]] = []
        self.failures: list[tuple[str, bool, bool]] = []

    def begin(self, profile: TurnExecutionProfile, approval_id: int) -> None:
        """记录 side effect 前的 Run resume。"""
        self.events.append(("begin", profile.task_run_id or 0, approval_id))

    def settle(
        self,
        profile: TurnExecutionProfile,
        approval_id: int,
        result,
    ) -> None:
        """记录 continuation 的可观察终态。"""
        self.events.append(("settle", profile.task_run_id or 0, result.approval_id))

    def fail(
        self,
        profile: TurnExecutionProfile,
        approval_id: int,
        *,
        error_code: str,
        session_id: int,
        turn_id: int | None,
        interrupted: bool = False,
        timed_out: bool = False,
    ) -> None:
        """记录 continuation 异常结算，不保存错误正文。"""
        del session_id
        self.failures.append((error_code, interrupted, timed_out))
        self.events.append(("fail", profile.task_run_id or 0, turn_id or approval_id))


class _SlowSecondProvider(FakeProvider):
    """第一次返回 Approval Tool Call，第二次永久等待 cancellation。"""

    async def complete(
        self,
        request: ModelRequest,
        on_text: StreamHandler | None = None,
    ) -> ModelResponse:
        """第二次调用等待 TurnService 的 Automation deadline。"""
        if len(self.requests) == 1:
            self.requests.append(request)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return await super().complete(request, on_text)


class _ContextProbeTool:
    """记录模型不可伪造的 ToolContext，供信任传播测试断言。"""

    definition = ToolDefinition(
        name="context_probe",
        description="Record the current trusted runtime context.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self) -> None:
        """创建空的上下文记录。"""
        self.contexts: list[ToolContext] = []

    def validate(self, arguments):
        """只接受空参数。"""
        if arguments:
            raise ValueError("context_probe accepts no arguments")
        return arguments

    async def execute(self, context, arguments):
        """保存完整上下文并返回固定成功结果。"""
        del arguments
        self.contexts.append(context)
        return ToolResult.success({"captured": True})


def final_response(content: str = "world") -> ModelResponse:
    """创建 Turn 成功路径使用的固定最终响应。"""
    return ModelResponse(
        content=content,
        tool_calls=(),
        reasoning_content="internal",
        finish_reason="stop",
        input_tokens=9,
        output_tokens=3,
        provider_request_id="req_turn",
    )


class TurnServiceTest(unittest.IsolatedAsyncioTestCase):
    """验证一次用户输入最终形成可回放的终态 Turn。"""

    def setUp(self) -> None:
        """创建完整状态、Repository 和 ContextBuilder。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(self.paths)
        self.owner = initialized.owner
        self.database = Database(self.paths.database)
        self.sessions = SessionRepository(self.database)
        self.messages = MessageRepository(self.database)
        self.turns = TurnRepository(self.database)
        self.context = ContextBuilder(self.paths)

    def service(
        self,
        provider: FakeProvider,
        runner: AgentRunner | None = None,
        approvals: ApprovalRepository | None = None,
        artifacts: "ArtifactStore | None" = None,
    ) -> TurnService:
        """用真实 Repository/Context/Runner 和指定模型 Fake 构造服务。"""
        return TurnService(
            owner_id=self.owner.id,
            model="deepseek-v4-pro",
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            context=self.context,
            runner=runner or AgentRunner(provider),
            approvals=approvals,
            artifacts=artifacts,
            state_home=self.paths.home,
            workspace=WorkspaceConfig(path=self.paths.workspace),
        )

    def _artifact_store(self) -> ArtifactStore:
        """构造与 Runtime 同参数的 Store，供附件用例使用。"""
        return ArtifactStore(
            Database(self.paths.database),
            owner_id=self.owner.id,
            root=self.paths.artifacts,
            staging_root=self.paths.downloads,
            max_bytes=1024,
        )

    def _put_text(self, store: ArtifactStore, name: str, body: bytes) -> str:
        """在 Store 里放一个文本 Artifact，返回 id。"""
        staged = self.paths.downloads / name
        staged.write_bytes(body)
        staged.chmod(0o600)
        return store.put(
            staged, declared_media_type="text/plain", source="user_upload"
        ).artifact_id

    async def test_turn_without_attachments_stores_the_text_verbatim(self) -> None:
        """回归防线：不带附件时用户消息必须逐字节不变。

        附件功能引入的任何追加内容都不能泄漏到普通对话里。
        """
        provider = FakeProvider((final_response(),))

        result = await self.service(provider).handle(self.owner.id, "hello", "default")

        history = self.messages.list_recent(result.session_id)
        self.assertEqual(history[0].content, "hello")

    async def test_attachment_manifest_is_appended_by_core(self) -> None:
        """附件清单由 Core 从 Store 读出后追加，Desktop 只传 id。"""
        store = self._artifact_store()
        artifact_id = self._put_text(store, "note.txt", b"hello lobster0")
        provider = FakeProvider((final_response(),))

        result = await self.service(provider, artifacts=store).handle(
            self.owner.id,
            "看看这个",
            "default",
            attachments=((artifact_id, "季度报告.txt"),),
        )

        content = self.messages.list_recent(result.session_id)[0].content
        self.assertTrue(content.startswith("看看这个"))
        self.assertIn(artifact_id, content)
        # 文件名来自调用方，类型与大小来自 Store——后两者无法被谎报。
        self.assertIn("季度报告.txt", content)
        self.assertIn("text/plain", content)

    async def test_attachment_is_linked_to_the_session(self) -> None:
        """发送成功后 Artifact 必须与会话建立关联，右栏才能列出它。"""
        store = self._artifact_store()
        artifact_id = self._put_text(store, "note.txt", b"hello")
        provider = FakeProvider((final_response(),))

        result = await self.service(provider, artifacts=store).handle(
            self.owner.id, "看看", "default", attachments=((artifact_id, "note.txt"),)
        )

        links = store.list_for_session(result.session_id, limit=10)
        self.assertEqual([item.artifact_id for item in links], [artifact_id])
        self.assertEqual(links[0].origin, "user_upload")

    async def test_forged_attachment_id_aborts_the_turn(self) -> None:
        """伪造的 id 必须整体拒绝，且不留下半条 Turn。"""
        store = self._artifact_store()
        provider = FakeProvider((final_response(),))
        before = len(self.messages.list_recent(
            self.sessions.get_or_create_cli(self.owner.id, "default").id
        ))

        with self.assertRaises(ArtifactError):
            await self.service(provider, artifacts=store).handle(
                self.owner.id, "看看", "default", attachments=(("art_" + "e" * 64, "fake.txt"),)
            )

        session = self.sessions.get_or_create_cli(self.owner.id, "default")
        self.assertEqual(len(self.messages.list_recent(session.id)), before)

    async def test_a_forged_manifest_in_the_body_is_not_mistaken_for_real(self) -> None:
        """正文里手写的假清单不影响真实清单——真实的永远追加在最后。"""
        store = self._artifact_store()
        artifact_id = self._put_text(store, "real.txt", b"real")
        provider = FakeProvider((final_response(),))

        result = await self.service(provider, artifacts=store).handle(
            self.owner.id,
            "[附件]\n- art_0000 · fake.txt · text/plain · 1 B",
            "default",
            attachments=((artifact_id, "real.txt"),),
        )

        content = self.messages.list_recent(result.session_id)[0].content
        self.assertIn(artifact_id, content)
        # 真实清单在最后，伪造段落只是普通正文的一部分。
        self.assertLess(content.index("art_0000"), content.index(artifact_id))

    async def test_success_persists_user_assistant_usage_and_completed_turn(self) -> None:
        """成功 Turn 应保存两条消息、Token、最终文本和 completed 状态。"""
        provider = FakeProvider((final_response(),))

        result = await self.service(provider).handle(self.owner.id, "hello", "default")

        saved = self.turns.get(result.turn_id)
        history = self.messages.list_recent(result.session_id)
        self.assertEqual(result.content, "world")
        self.assertIsNotNone(result.message_id)
        self.assertIsNone(result.approval_id)
        self.assertEqual(saved.status, "completed")
        self.assertEqual((saved.input_tokens, saved.output_tokens), (9, 3))
        self.assertEqual(
            [(message.role, message.content) for message in history],
            [("user", "hello"), ("assistant", "world")],
        )
        self.assertEqual(provider.requests[0].messages[-1].content, "hello")
        self.assertEqual(history[-1].id, result.message_id)

    async def test_automation_turn_uses_fresh_session_filtered_tools_and_terminal_result(
        self,
    ) -> None:
        """后台 Turn 只暴露 profile Tool，并由 complete_task 产生结构化终态。"""
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_complete",
                            "complete_task",
                            {"notify": True, "text": "自动任务完成"},
                        ),
                    ),
                    reasoning_content="done",
                    finish_reason="tool_calls",
                    input_tokens=4,
                    output_tokens=2,
                    provider_request_id="req_automation",
                ),
            )
        )
        executor = ToolExecutor(
            ToolRegistry((SystemInfoTool(), CompleteTaskTool())),
            PolicyEngine(),
            ToolRunRepository(self.database),
        )
        service = self.service(provider, AgentRunner(provider, executor))

        result = await service.handle_automation(
            task_id=7,
            task_run_id=9,
            text="summarize status",
            profile=TurnExecutionProfile(
                source="automation",
                task_run_id=9,
                allowed_tool_names=frozenset({"complete_task"}),
                budget=AgentRunBudget(max_turns=2, max_tool_calls=1),
                automation_gate=lambda: True,
            ),
        )

        self.assertEqual(result.terminal_response.text, "自动任务完成")
        self.assertEqual(self.turns.get(result.turn_id).status, "completed")
        self.assertEqual(
            [schema["function"]["name"] for schema in provider.requests[0].tools],
            ["complete_task"],
        )
        self.assertEqual(provider.requests[0].runtime_snapshot["source"], "automation")
        session = self.sessions.get_or_create(
            self.owner.id,
            "automation",
            "local",
            "task:7:run:9",
        )
        self.assertEqual(result.session_id, session.id)

    async def test_automation_approval_resumes_original_profile_and_terminal_tool(
        self,
    ) -> None:
        """审批 continuation 必须复用 automation provenance、参数和 Tool allowlist。"""
        destination = self.paths.workspace / "approved-automation.txt"
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_write_automation",
                            "write_file",
                            {"path": "approved-automation.txt", "content": "bound"},
                        ),
                    ),
                    reasoning_content="write",
                    finish_reason="tool_calls",
                    input_tokens=4,
                    output_tokens=2,
                    provider_request_id="req_automation_wait",
                ),
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_complete_automation",
                            "complete_task",
                            {"notify": True, "text": "已写入"},
                        ),
                    ),
                    reasoning_content="complete",
                    finish_reason="tool_calls",
                    input_tokens=3,
                    output_tokens=2,
                    provider_request_id="req_automation_resume",
                ),
            )
        )
        approvals = ApprovalRepository(self.database)
        executor = ToolExecutor(
            ToolRegistry((SystemInfoTool(), WriteFileTool(), CompleteTaskTool())),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        continuation = _AutomationContinuationProbe()
        service = TurnService(
            owner_id=self.owner.id,
            model="deepseek-v4-pro",
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            context=self.context,
            runner=AgentRunner(provider, executor),
            approvals=approvals,
            automation_continuation=continuation,
            state_home=self.paths.home,
            workspace=WorkspaceConfig(path=self.paths.workspace),
            automation_gate=lambda: True,
        )
        profile = TurnExecutionProfile(
            source="automation",
            task_run_id=17,
            allowed_tool_names=frozenset({"write_file", "complete_task"}),
            budget=AgentRunBudget(max_turns=3, max_tool_calls=2),
            automation_gate=lambda: True,
        )

        waiting = await service.handle_automation(
            task_id=13,
            task_run_id=17,
            text="write the bound content",
            profile=profile,
        )
        result = await service.continue_approval(
            self.owner.id,
            waiting.approval_id,
            decision=ApprovalDecision.ONCE,
        )

        self.assertEqual(destination.read_text(encoding="utf-8"), "bound")
        self.assertEqual(result.terminal_response.text, "已写入")
        self.assertEqual(
            continuation.events,
            [("begin", 17, waiting.approval_id), ("settle", 17, None)],
        )
        self.assertEqual(
            [schema["function"]["name"] for schema in provider.requests[1].tools],
            ["complete_task", "write_file"],
        )

    async def test_automation_approval_continuation_enforces_original_wall_clock(self) -> None:
        """审批后 Tool+Provider 续跑仍受原始 Automation timeout 约束。"""
        provider = _SlowSecondProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_timeout_write",
                            "write_file",
                            {"path": "timeout.txt", "content": "bounded"},
                        ),
                    ),
                    reasoning_content="write",
                    finish_reason="tool_calls",
                    input_tokens=2,
                    output_tokens=1,
                    provider_request_id="req_timeout_wait",
                ),
            )
        )
        approvals = ApprovalRepository(self.database)
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(), CompleteTaskTool())),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        continuation = _AutomationContinuationProbe()
        service = TurnService(
            owner_id=self.owner.id,
            model="deepseek-v4-pro",
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            context=self.context,
            runner=AgentRunner(provider, executor),
            approvals=approvals,
            automation_continuation=continuation,
            state_home=self.paths.home,
            workspace=WorkspaceConfig(path=self.paths.workspace),
            automation_gate=lambda: True,
        )
        profile = TurnExecutionProfile(
            source="automation",
            task_run_id=29,
            allowed_tool_names=frozenset({"write_file", "complete_task"}),
            budget=AgentRunBudget(
                max_turns=3,
                max_tool_calls=2,
                timeout_seconds=1,
            ),
            automation_gate=lambda: True,
        )
        waiting = await service.handle_automation(
            task_id=23,
            task_run_id=29,
            text="perform the bounded action",
            profile=profile,
        )

        result = await service.continue_approval(
            self.owner.id,
            waiting.approval_id,
            decision=ApprovalDecision.ONCE,
        )

        self.assertEqual(result.error_code, "task_timeout")
        self.assertEqual(continuation.failures, [("task_timeout", False, True)])
        self.assertEqual(self.turns.get(result.turn_id).status, "failed")

    async def test_feishu_inbound_uses_stable_id_and_duplicate_reuses_result(self) -> None:
        """Channel 重投同一消息不能产生第二个 Turn、User Message 或 Provider 请求。"""
        provider = FakeProvider((final_response("飞书回复"),))
        service = self.service(provider)

        first = await service.handle_inbound(
            user_id=self.owner.id,
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_personal",
            inbound_event_id="om_stable",
            text="飞书问题",
        )
        duplicate = await service.handle_inbound(
            user_id=self.owner.id,
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_personal",
            inbound_event_id="om_stable",
            text="不得覆盖",
        )

        session = self.sessions.get_or_create(
            self.owner.id,
            "feishu",
            "default",
            "oc_personal",
        )
        history = self.messages.list_recent(session.id)
        self.assertEqual(first, duplicate)
        self.assertEqual(first.content, "飞书回复")
        self.assertIsNotNone(first.message_id)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(
            [(message.role, message.content) for message in history],
            [("user", "飞书问题"), ("assistant", "飞书回复")],
        )
        self.assertEqual(self.turns.get(first.turn_id).inbound_event_id, "om_stable")

    async def test_channel_inbound_id_is_nonempty_bounded_and_control_free(self) -> None:
        """不可信平台标识不能用控制字符或超长值污染 Turn 唯一键。"""
        provider = FakeProvider(())
        service = self.service(provider)

        for inbound_id in ("", "om_bad\x00id", "x" * 257):
            with self.subTest(inbound_id_length=len(inbound_id)):
                with self.assertRaisesRegex(ValueError, "inbound_event_id"):
                    await service.handle_inbound(
                        user_id=self.owner.id,
                        channel="feishu",
                        account_id="default",
                        external_conversation_id="oc_personal",
                        inbound_event_id=inbound_id,
                        text="hello",
                    )
        self.assertEqual(provider.requests, [])

    async def test_untrusted_channel_drops_personal_roots_but_cli_keeps_them(self) -> None:
        """非 Owner 私聊不能继承 Personal 全局读写根；本地 CLI 可以。"""
        read_root = self.paths.home / "read-root"
        write_root = self.paths.home / "write-root"
        read_root.mkdir()
        write_root.mkdir()
        probe = _ContextProbeTool()
        call = ToolCall("context-1", "context_probe", {})
        provider = FakeProvider(
            (
                ModelResponse("", (call,), "probe", "tool_calls", 1, 1, "req-1"),
                final_response("channel done"),
                ModelResponse(
                    "",
                    (ToolCall("context-2", "context_probe", {}),),
                    "probe",
                    "tool_calls",
                    1,
                    1,
                    "req-2",
                ),
                final_response("cli done"),
            )
        )
        executor = ToolExecutor(
            ToolRegistry((probe,)),
            PolicyEngine(),
            ToolRunRepository(self.database),
        )
        service = TurnService(
            owner_id=self.owner.id,
            model="deepseek-v4-pro",
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            context=self.context,
            runner=AgentRunner(provider, executor),
            state_home=self.paths.home,
            workspace=WorkspaceConfig(
                path=self.paths.workspace,
                read_only_roots=(read_root,),
                write_roots=(write_root,),
                owner_home=self.paths.home,
            ),
        )

        try:
            await service.handle_inbound(
                user_id=self.owner.id,
                channel="feishu",
                account_id="default",
                external_conversation_id="oc_friend",
                inbound_event_id="om_friend",
                text="inspect",
                trusted_owner=False,
                conversation_kind="direct",
                identity_verified=False,
            )
        except TypeError:
            self.fail("TurnService.handle_inbound must accept trusted_owner")
        await service.handle(self.owner.id, "inspect", "local-owner")

        self.assertEqual(len(probe.contexts), 2)
        self.assertFalse(probe.contexts[0].trusted_owner)
        self.assertEqual(probe.contexts[0].disclosure.conversation_kind, "direct")
        self.assertFalse(probe.contexts[0].disclosure.identity_verified)
        self.assertEqual(probe.contexts[0].read_only_roots, ())
        self.assertEqual(probe.contexts[0].write_roots, ())
        self.assertIsNone(probe.contexts[0].owner_home)
        self.assertTrue(probe.contexts[1].trusted_owner)
        self.assertEqual(probe.contexts[1].disclosure.conversation_kind, "local")
        self.assertTrue(probe.contexts[1].disclosure.identity_verified)
        self.assertEqual(probe.contexts[1].read_only_roots, (read_root,))
        self.assertEqual(probe.contexts[1].write_roots, (write_root,))
        self.assertEqual(probe.contexts[1].owner_home, self.paths.home)

    async def test_run_events_follow_persisted_turn_and_tool_states(self) -> None:
        """TUI 只能看到已落库的 Turn/Tool 状态，且顺序与真实执行一致。"""
        call = ToolCall("call_system", "system_info", {})
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(call,),
                    reasoning_content="inspect",
                    finish_reason="tool_calls",
                    input_tokens=2,
                    output_tokens=1,
                    provider_request_id="req_tool_event",
                ),
                final_response("done"),
            )
        )
        executor = ToolExecutor(
            ToolRegistry((SystemInfoTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
        )
        events: list[RunEvent] = []

        async def capture(event: RunEvent) -> None:
            events.append(event)
            if event.kind == "turn_started":
                self.assertEqual(self.turns.get(event.turn_id).status, "running")
            if event.kind == "tool_finished":
                with self.database.connect_read_only() as connection:
                    status = connection.execute(
                        "SELECT status FROM tool_runs WHERE tool_call_id = ?",
                        (event.data["call_id"],),
                    ).fetchone()[0]
                self.assertEqual(status, "succeeded")
            if event.kind == "turn_finished":
                self.assertEqual(self.turns.get(event.turn_id).status, "completed")

        with mock.patch(
            "lobster0.tools.system._collect_system_info",
            return_value={"unavailable_sections": []},
        ):
            await self.service(provider, AgentRunner(provider, executor)).handle(
                self.owner.id,
                "inspect",
                "events",
                on_event=capture,
            )

        self.assertEqual(
            [event.kind for event in events],
            [
                "turn_started",
                "model_usage",
                "model_reasoning",
                "tool_requested",
                "tool_started",
                "tool_finished",
                "model_text_delta",
                "model_usage",
                "model_reasoning",
                "turn_finished",
            ],
        )
        requested = next(event for event in events if event.kind == "tool_requested")
        self.assertEqual(requested.data["arguments"], {})
        finished = events[-1]
        self.assertEqual(finished.data["context_tokens"], 9)
        self.assertEqual(finished.data["input_tokens"], 11)
        self.assertEqual(finished.data["output_tokens"], 4)
        self.assertEqual(finished.data["iterations"], 2)
        self.assertEqual(finished.data["tool_calls"], 1)
        self.assertEqual(finished.data["provider_request_id"], "req_turn")
        self.assertIs(type(finished.data["duration_ms"]), int)

    async def test_approval_event_has_committed_normalized_arguments(self) -> None:
        """审批弹窗收到事件时，pending Approval 已存在且参数来自 Policy 归一化。"""
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_write",
                            "write_file",
                            {"path": "approved.txt", "content": "yes"},
                        ),
                    ),
                    reasoning_content="write",
                    finish_reason="tool_calls",
                    input_tokens=2,
                    output_tokens=1,
                    provider_request_id="req_approval_event",
                ),
            )
        )
        approvals = ApprovalRepository(self.database)
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        events: list[RunEvent] = []

        async def capture(event: RunEvent) -> None:
            events.append(event)
            if event.kind == "approval_required":
                approval_id = event.data["approval_id"]
                assert type(approval_id) is int
                self.assertEqual(approvals.get(self.owner.id, approval_id).status, "pending")

        result = await self.service(
            provider,
            AgentRunner(provider, executor),
            approvals,
        ).handle(self.owner.id, "write", "approval-events", on_event=capture)

        approval_event = next(
            event for event in events if event.kind == "approval_required"
        )
        arguments = approval_event.data["arguments"]
        assert isinstance(arguments, dict)
        self.assertEqual(arguments["content"], "yes")
        self.assertEqual(arguments["path"], str(self.paths.workspace / "approved.txt"))
        self.assertEqual(self.turns.get(result.turn_id).status, "waiting_approval")
        self.assertIsNone(result.message_id)
        self.assertEqual(result.approval_id, approval_event.data["approval_id"])
        self.assertNotIn("turn_finished", [event.kind for event in events])

    async def test_second_turn_receives_previous_history_in_chronological_order(self) -> None:
        """复用同一 CLI Session 时，新请求应包含上一轮和当前输入。"""
        provider = FakeProvider((final_response("first answer"), final_response("second answer")))
        service = self.service(provider)

        await service.handle(self.owner.id, "first question", "default")
        await service.handle(self.owner.id, "second question", "default")

        messages = provider.requests[1].messages
        self.assertEqual(
            [(message.role, message.content) for message in messages[-3:]],
            [
                ("user", "first question"),
                ("assistant", "first answer"),
                ("user", "second question"),
            ],
        )

    async def test_provider_failure_marks_turn_failed_with_stable_code(self) -> None:
        """认证失败应原样抛给 CLI，同时数据库保存安全错误分类。"""
        provider = FakeProvider((ProviderAuthenticationError("authentication failed"),))
        events: list[RunEvent] = []

        async def capture(event: RunEvent) -> None:
            events.append(event)

        with self.assertRaises(ProviderAuthenticationError):
            await self.service(provider).handle(
                self.owner.id,
                "hello",
                "default",
                on_event=capture,
            )

        session = self.sessions.get_or_create_cli(self.owner.id, "default")
        saved = self.turns.list_recent(session.id, limit=1)[0]
        self.assertEqual(saved.status, "failed")
        self.assertEqual(saved.error_code, "provider_authentication")
        self.assertEqual(saved.error_message, "authentication failed")
        self.assertEqual(events[-1].kind, "turn_failed")
        self.assertEqual(events[-1].data["error_code"], "provider_authentication")
        self.assertIs(type(events[-1].data["duration_ms"]), int)
        self.assertNotIn("authentication failed", str(events[-1].data))

    async def test_no_progress_failure_persists_stable_code_and_trace(self) -> None:
        """连续无进展必须保存专用错误码，并保留停止前的 Tool 轨迹。"""
        provider = FakeProvider(
            tuple(
                ModelResponse(
                    content="",
                    tool_calls=(ToolCall(f"call_repeat_{index}", "system_info", {}),),
                    reasoning_content="repeat",
                    finish_reason="tool_calls",
                    input_tokens=1,
                    output_tokens=1,
                    provider_request_id=f"req-repeat-{index}",
                )
                for index in range(4)
            )
        )
        executor = ToolExecutor(
            ToolRegistry((SystemInfoTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
        )
        runner = AgentRunner(
            provider,
            executor,
            max_iterations=8,
            hard_max_iterations=12,
            max_no_progress_iterations=3,
        )
        events: list[RunEvent] = []

        async def capture(event: RunEvent) -> None:
            events.append(event)

        with mock.patch(
            "lobster0.tools.system._collect_system_info",
            return_value={"cpu": {"model": "Test CPU"}, "unavailable_sections": []},
        ):
            with self.assertRaises(AgentNoProgressError):
                await self.service(provider, runner).handle(
                    self.owner.id,
                    "重复查看配置",
                    "no-progress",
                    on_event=capture,
                )

        session = self.sessions.get_or_create_cli(self.owner.id, "no-progress")
        saved = self.turns.list_recent(session.id, limit=1)[0]
        history = self.messages.list_recent(session.id)
        self.assertEqual(saved.status, "failed")
        self.assertEqual(saved.error_code, "loop_no_progress")
        self.assertEqual([message.role for message in history].count("tool"), 4)
        self.assertEqual(events[-1].kind, "turn_failed")
        self.assertEqual(events[-1].data["error_code"], "loop_no_progress")

    async def test_cancellation_marks_turn_cancelled_and_propagates(self) -> None:
        """取消必须持久化 cancelled，并继续抛出以便 CLI 返回 130。"""
        provider = FakeProvider((asyncio.CancelledError(),))
        events: list[RunEvent] = []

        async def capture(event: RunEvent) -> None:
            events.append(event)

        with self.assertRaises(asyncio.CancelledError):
            await self.service(provider).handle(
                self.owner.id,
                "hello",
                "default",
                on_event=capture,
            )

        session = self.sessions.get_or_create_cli(self.owner.id, "default")
        saved = self.turns.list_recent(session.id, limit=1)[0]
        self.assertEqual(saved.status, "cancelled")
        self.assertEqual(events[-1].kind, "turn_cancelled")
        self.assertIs(type(events[-1].data["duration_ms"]), int)

    async def test_tool_loop_persists_and_restores_complete_history(self) -> None:
        """真实 Tool 纵切必须保存轨迹，并在下一 Turn 恢复结构化调用。"""
        call = ToolCall("call_system", "system_info", {})
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(call,),
                    reasoning_content="need actual data",
                    finish_reason="tool_calls",
                    input_tokens=5,
                    output_tokens=2,
                    provider_request_id="req_tool",
                ),
                final_response("你的电脑是测试配置"),
                final_response("我记得刚才的配置"),
            )
        )
        executor = ToolExecutor(
            ToolRegistry((SystemInfoTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
        )
        service = self.service(provider, AgentRunner(provider, executor))

        with mock.patch(
            "lobster0.tools.system._collect_system_info",
            return_value={
                "cpu": {"model": "Test CPU", "logical_cores": 8},
                "unavailable_sections": [],
            },
        ):
            first = await service.handle(self.owner.id, "查看我的电脑配置", "tools")
            await service.handle(self.owner.id, "你还记得吗", "tools")

        saved_turn = self.turns.get(first.turn_id)
        history = self.messages.list_recent(first.session_id)
        self.assertEqual(saved_turn.status, "completed")
        self.assertEqual(
            [message.role for message in history],
            ["user", "assistant", "tool", "assistant", "user", "assistant"],
        )
        with self.database.connect_read_only() as connection:
            tool_run = connection.execute(
                "SELECT tool_name, status FROM tool_runs"
            ).fetchone()
        self.assertEqual(tuple(tool_run), ("system_info", "succeeded"))
        self.assertEqual(
            provider.requests[0].tools[0]["function"]["name"],
            "system_info",
        )
        self.assertEqual(provider.requests[1].messages[-1].role, "tool")
        restored = provider.requests[2].messages
        assistant_call = next(message for message in restored if message.tool_calls)
        self.assertEqual(assistant_call.tool_calls, (call,))

    async def test_write_request_persists_waiting_turn_without_fake_tool_result(self) -> None:
        """需审批写入只保存 Assistant 调用并暂停原 Turn。"""
        target = self.paths.workspace / "approved.txt"
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_write",
                            "write_file",
                            {"path": "approved.txt", "content": "yes"},
                        ),
                    ),
                    reasoning_content="need to write",
                    finish_reason="tool_calls",
                    input_tokens=5,
                    output_tokens=2,
                    provider_request_id="req_write",
                ),
            )
        )
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=ApprovalRepository(self.database),
        )

        result = await self.service(provider, AgentRunner(provider, executor)).handle(
            self.owner.id,
            "写入文件",
            "waiting",
        )

        saved = self.turns.get(result.turn_id)
        history = self.messages.list_recent(result.session_id)
        approval = ApprovalRepository(self.database).list(self.owner.id)[0]
        self.assertEqual(result.content, f"Approval {approval.id} required for write_file.")
        self.assertEqual(saved.status, "waiting_approval")
        self.assertEqual(saved.runtime_snapshot["approval_id"], approval.id)
        self.assertEqual([message.role for message in history], ["user", "assistant"])
        self.assertFalse(target.exists())
        self.assertEqual(len(provider.requests), 1)

    async def test_new_turn_omits_orphaned_approval_call_from_provider_history(self) -> None:
        """旧审批直接结算后，新请求只发送 protocol-safe Context。"""
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_orphan",
                            "write_file",
                            {"path": "old.txt", "content": "old"},
                        ),
                    ),
                    reasoning_content="need approval",
                    finish_reason="tool_calls",
                    input_tokens=1,
                    output_tokens=1,
                    provider_request_id="req-old",
                ),
                final_response("只处理当前请求"),
            )
        )
        approvals = ApprovalRepository(self.database)
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        service = self.service(provider, AgentRunner(provider, executor), approvals)
        waiting = await service.handle(self.owner.id, "执行旧动作", "orphan")
        approval = approvals.list(self.owner.id, status="pending")[0]
        approvals.deny(self.owner.id, approval.id)

        current = await service.handle(self.owner.id, "只处理当前请求", "orphan")

        self.assertEqual(current.content, "只处理当前请求")
        self.assertEqual(self.turns.get(waiting.turn_id).status, "waiting_approval")
        sent_history = provider.requests[1].messages
        self.assertEqual(
            [(message.role, message.content) for message in sent_history[-1:]],
            [("user", "只处理当前请求")],
        )
        self.assertFalse(any(message.tool_calls for message in sent_history))

    async def test_approve_after_restart_creates_child_executes_once_and_finishes(self) -> None:
        """批准后由 child Turn 执行绑定写入，重启后也不能重复消费。"""
        target = self.paths.workspace / "approved.txt"
        call = ToolCall(
            "call_write",
            "write_file",
            {"path": "approved.txt", "content": "approved"},
        )
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(call,),
                    reasoning_content="need write",
                    finish_reason="tool_calls",
                    input_tokens=5,
                    output_tokens=2,
                    provider_request_id="req_write",
                ),
                final_response("写入完成"),
            )
        )
        approvals = ApprovalRepository(self.database)
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        service = self.service(provider, AgentRunner(provider, executor), approvals)
        waiting = await service.handle(self.owner.id, "写入文件", "approve")
        approval = approvals.list(self.owner.id, status="pending")[0]
        self.messages.create_channel_notice(
            waiting.session_id,
            f"卡片不可用时发送：/approve {approval.id} once",
        )

        restarted_approvals = ApprovalRepository(self.database)
        restarted_executor = ToolExecutor(
            ToolRegistry((WriteFileTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=restarted_approvals,
        )
        restarted = self.service(
            provider,
            AgentRunner(provider, restarted_executor),
            restarted_approvals,
        )
        result = await restarted.continue_approval(
            self.owner.id,
            approval.id,
            decision=ApprovalDecision.ONCE,
        )

        child = self.turns.get(result.turn_id)
        history = self.messages.list_recent(result.session_id)
        self.assertEqual(result.content, "写入完成")
        self.assertEqual(target.read_text(encoding="utf-8"), "approved")
        self.assertEqual(child.parent_turn_id, waiting.turn_id)
        self.assertEqual(child.status, "completed")
        self.assertEqual(
            [message.role for message in history],
            ["user", "assistant", "assistant", "tool", "assistant"],
        )
        sent_history = provider.requests[1].messages
        call_index = next(
            index
            for index, message in enumerate(sent_history)
            if message.tool_calls == (call,)
        )
        self.assertEqual(sent_history[call_index + 1].tool_call_id, call.call_id)
        self.assertFalse(
            any(message.metadata.get("channel_notice") for message in sent_history)
        )
        with self.assertRaises(ApprovalError) as repeated:
            await restarted.continue_approval(
                self.owner.id,
                approval.id,
                decision=ApprovalDecision.ONCE,
            )
        self.assertEqual(repeated.exception.code, "already_decided")
        self.assertEqual(target.read_text(encoding="utf-8"), "approved")

    async def test_deny_creates_tool_error_and_never_writes(self) -> None:
        """拒绝必须让模型收到 approval_denied，并保持文件不存在。"""
        target = self.paths.workspace / "denied.txt"
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_denied",
                            "write_file",
                            {"path": "denied.txt", "content": "no"},
                        ),
                    ),
                    reasoning_content=None,
                    finish_reason="tool_calls",
                    input_tokens=2,
                    output_tokens=1,
                    provider_request_id="req_denied",
                ),
                final_response("已取消写入"),
            )
        )
        approvals = ApprovalRepository(self.database)
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        service = self.service(provider, AgentRunner(provider, executor), approvals)
        await service.handle(self.owner.id, "不要真的写", "deny")
        approval = approvals.list(self.owner.id, status="pending")[0]

        result = await service.continue_approval(
            self.owner.id,
            approval.id,
            decision=ApprovalDecision.DENY,
        )

        tool_payload = json.loads(provider.requests[1].messages[-1].content)
        self.assertEqual(result.content, "已取消写入")
        self.assertEqual(tool_payload["error"]["code"], "approval_denied")
        self.assertFalse(target.exists())
        self.assertEqual(approvals.get(self.owner.id, approval.id).status, "denied")
        with self.database.connect_read_only() as connection:
            run_status = connection.execute("SELECT status FROM tool_runs").fetchone()[0]
        self.assertEqual(run_status, "denied")

    async def test_changed_approval_arguments_never_write_or_create_child(self) -> None:
        """存储参数被改动后必须 fail closed，不能启动 continuation。"""
        target = self.paths.workspace / "tampered.txt"
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_tampered",
                            "write_file",
                            {"path": "tampered.txt", "content": "original"},
                        ),
                    ),
                    reasoning_content=None,
                    finish_reason="tool_calls",
                    input_tokens=2,
                    output_tokens=1,
                    provider_request_id="req-tampered",
                ),
            )
        )
        approvals = ApprovalRepository(self.database)
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        service = self.service(provider, AgentRunner(provider, executor), approvals)
        waiting = await service.handle(self.owner.id, "写入", "tampered")
        approval = approvals.list(self.owner.id, status="pending")[0]
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE tool_runs SET arguments_json = ? WHERE tool_call_id = ?",
                (
                    json.dumps(
                        {
                            "path": str(target),
                            "content": "changed",
                            "overwrite": False,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "call_tampered",
                ),
            )

        with self.assertRaises(ApprovalError) as changed:
            await service.continue_approval(
                self.owner.id,
                approval.id,
                decision=ApprovalDecision.ONCE,
            )

        self.assertEqual(changed.exception.code, "hash_mismatch")
        self.assertFalse(target.exists())
        self.assertEqual(
            [turn.id for turn in self.turns.list_recent(waiting.session_id)],
            [waiting.turn_id],
        )
        self.assertEqual(len(provider.requests), 1)

    async def test_disallowed_always_scope_never_writes_or_creates_child(self) -> None:
        """write_file 的 Always 必须在执行和 continuation 前失败关闭。"""
        target = self.paths.workspace / "never-always.txt"
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_always_write",
                            "write_file",
                            {"path": target.name, "content": "no"},
                        ),
                    ),
                    reasoning_content=None,
                    finish_reason="tool_calls",
                    input_tokens=2,
                    output_tokens=1,
                    provider_request_id="req-always-write",
                ),
            )
        )
        approvals = ApprovalRepository(self.database)
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        service = self.service(provider, AgentRunner(provider, executor), approvals)
        waiting = await service.handle(self.owner.id, "写入", "always-write")
        approval = approvals.list(self.owner.id, status="pending")[0]

        with self.assertRaises(ApprovalError) as rejected:
            await service.continue_approval(
                self.owner.id,
                approval.id,
                decision=ApprovalDecision.ALWAYS,
            )

        self.assertEqual(rejected.exception.code, "scope_forbidden")
        self.assertFalse(target.exists())
        self.assertEqual(approvals.get(self.owner.id, approval.id).status, "pending")
        self.assertEqual(
            [turn.id for turn in self.turns.list_recent(waiting.session_id)],
            [waiting.turn_id],
        )

    async def test_continuation_marks_later_same_batch_calls_not_executed(self) -> None:
        """首个调用待审批后，同批后续调用必须补齐失败结果且绝不执行。"""
        first = self.paths.workspace / "first.txt"
        later = self.paths.workspace / "later.txt"
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "call_first",
                            "write_file",
                            {"path": "first.txt", "content": "one"},
                        ),
                        ToolCall(
                            "call_later",
                            "write_file",
                            {"path": "later.txt", "content": "two"},
                        ),
                    ),
                    reasoning_content=None,
                    finish_reason="tool_calls",
                    input_tokens=3,
                    output_tokens=2,
                    provider_request_id="req-batch",
                ),
                final_response("只执行了已批准项"),
            )
        )
        approvals = ApprovalRepository(self.database)
        executor = ToolExecutor(
            ToolRegistry((WriteFileTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        service = self.service(provider, AgentRunner(provider, executor), approvals)
        await service.handle(self.owner.id, "写两个文件", "batch")
        approval = approvals.list(self.owner.id, status="pending")[0]

        await service.continue_approval(
            self.owner.id,
            approval.id,
            decision=ApprovalDecision.ONCE,
        )

        continuation_tools = [
            message for message in provider.requests[1].messages if message.role == "tool"
        ]
        self.assertEqual(
            [message.tool_call_id for message in continuation_tools[-2:]],
            ["call_first", "call_later"],
        )
        self.assertEqual(
            json.loads(continuation_tools[-1].content)["error"]["code"],
            "not_executed",
        )
        self.assertEqual(first.read_text(encoding="utf-8"), "one")
        self.assertFalse(later.exists())
        self.assertEqual(len(approvals.list(self.owner.id)), 1)

    async def test_runtime_executes_read_file_and_persists_full_trace(self) -> None:
        """共享 Runtime 必须暴露 read_file，并持久化完整的两轮 Tool 轨迹。"""
        (self.paths.workspace / "README.md").write_text(
            "Lobster0 workspace README\nSecond line\n",
            encoding="utf-8",
        )
        call = ToolCall("call_readme", "read_file", {"path": "README.md"})
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(call,),
                    reasoning_content="read the workspace readme",
                    finish_reason="tool_calls",
                    input_tokens=5,
                    output_tokens=2,
                    provider_request_id="req_readme",
                ),
                final_response("README says Lobster0 workspace README"),
            )
        )
        complete = provider.complete

        async def verify_tool_message_before_final_response(
            request: ModelRequest,
            on_text: StreamHandler | None = None,
        ) -> ModelResponse:
            """在第二轮返回总结前验证真实 Tool Message 已传回 Provider。"""
            if provider.requests:
                message = request.messages[-1]
                self.assertEqual((message.role, message.tool_call_id), ("tool", "call_readme"))
                self.assertEqual(json.loads(message.content)["ok"], True)
            return await complete(request, on_text)

        provider.complete = verify_tool_message_before_final_response  # type: ignore[method-assign]
        provider.aclose = mock.AsyncMock()  # type: ignore[attr-defined]

        with mock.patch(
            "lobster0.runtime.OpenAICompatibleProvider",
            return_value=provider,
        ):
            runtime = create_runtime(
                load_config(self.paths, {}, {}),
                self.paths,
                "offline-secret",
            )
        try:
            result = await runtime.service.handle(
                runtime.owner_id,
                "read the README",
                "read-file",
            )
        finally:
            await runtime.aclose()

        session = self.sessions.get_or_create_cli(self.owner.id, "read-file")
        turn = self.turns.list_recent(session.id, limit=1)[0]
        history = self.messages.list_recent(session.id)
        tool_message = provider.requests[1].messages[-1]
        with self.database.connect_read_only() as connection:
            tool_run = connection.execute(
                "SELECT tool_name, status, policy_action FROM tool_runs"
            ).fetchone()
            audit_events = connection.execute(
                "SELECT event_type FROM audit_events ORDER BY id"
            ).fetchall()

        self.assertEqual(result.content, "README says Lobster0 workspace README")
        self.assertEqual(
            [schema["function"]["name"] for schema in provider.requests[0].tools],
            [
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
            ],
        )
        self.assertEqual((tool_message.role, tool_message.tool_call_id), ("tool", "call_readme"))
        self.assertEqual(
            json.loads(tool_message.content),
            {
                "ok": True,
                "tool": "read_file",
                "data": {
                    "path": "README.md",
                    "content": "Lobster0 workspace README\nSecond line\n",
                    "offset": 1,
                    "lines": 2,
                    "truncated": False,
                },
            },
        )
        self.assertEqual(turn.status, "completed")
        self.assertEqual(
            [(message.role, message.content) for message in history],
            [
                ("user", "read the README"),
                ("assistant", ""),
                ("tool", tool_message.content),
                ("assistant", "README says Lobster0 workspace README"),
            ],
        )
        self.assertEqual(tuple(tool_run), ("read_file", "succeeded", "allow"))
        self.assertEqual(
            [event[0] for event in audit_events],
            ["tool.started", "tool.succeeded"],
        )

    def test_corrupt_tool_call_metadata_is_not_sent_to_provider(self) -> None:
        """缺字段的持久 Tool Call 必须在 Provider 边界前被拒绝。"""
        stored = StoredMessage(
            id=99,
            session_id=1,
            turn_id=1,
            role="assistant",
            content="",
            provider_message_id=None,
            tool_call_id=None,
            metadata={"tool_calls": [{"name": "system_info"}]},
            created_at=datetime.now(UTC),
        )

        with self.assertRaises(ConversationDataError):
            _model_message(stored)

    async def test_provider_failure_after_tool_keeps_executed_message_trace(self) -> None:
        """第二轮模型失败时，已经执行的 Tool Call/Result 不能从回放中消失。"""
        call = ToolCall("call_before_failure", "system_info", {})
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(call,),
                    reasoning_content="need actual data",
                    finish_reason="tool_calls",
                    input_tokens=5,
                    output_tokens=2,
                    provider_request_id="req_tool",
                ),
                ProviderServerError("model provider server error"),
            )
        )
        executor = ToolExecutor(
            ToolRegistry((SystemInfoTool(),)),
            PolicyEngine(),
            ToolRunRepository(self.database),
        )
        service = self.service(provider, AgentRunner(provider, executor))

        with (
            mock.patch(
                "lobster0.tools.system._collect_system_info",
                return_value={"cpu": {"model": "Test CPU"}, "unavailable_sections": []},
            ),
            self.assertRaises(ProviderServerError),
        ):
            await service.handle(self.owner.id, "查看配置", "failed-after-tool")

        session = self.sessions.get_or_create_cli(self.owner.id, "failed-after-tool")
        turn = self.turns.list_recent(session.id, limit=1)[0]
        history = self.messages.list_recent(session.id)
        self.assertEqual(turn.status, "failed")
        self.assertEqual(
            [message.role for message in history],
            ["user", "assistant", "tool"],
        )
        with self.database.connect_read_only() as connection:
            tool_status = connection.execute("SELECT status FROM tool_runs").fetchone()[0]
        self.assertEqual(tool_status, "succeeded")


if __name__ == "__main__":
    unittest.main()
