"""模型可见 Browser Tool 的参数、调用和审批回归。"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from miniclaw.artifacts.store import ArtifactStore
from miniclaw.bootstrap import initialize_state
from miniclaw.browser.models import BrowserAction, BrowserProtocolError
from miniclaw.config import load_config
from miniclaw.paths import build_state_paths
from miniclaw.policy.engine import PolicyEngine
from miniclaw.providers.base import ToolCall
from miniclaw.runtime import create_runtime
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ApprovalRepository, ToolRunRepository
from miniclaw.tools.base import ToolContext, ToolValidationError
from miniclaw.tools.browser import browser_tools
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.registry import ToolRegistry


class _Client:
    """记录 BrowserAction 并返回固定结果的异步 fake。"""

    def __init__(self) -> None:
        """初始化请求记录和可选稳定失败。"""
        self.actions: list[BrowserAction] = []
        self.error: BrowserProtocolError | None = None
        self.result: dict[str, object] | None = None

    async def request(self, action: BrowserAction) -> dict[str, object]:
        """记录动作；按配置抛错或返回确定性结果。"""
        self.actions.append(action)
        if self.error is not None:
            raise self.error
        return self.result or {"action": action.kind, "ok": True}


class BrowserToolsTest(unittest.IsolatedAsyncioTestCase):
    """验证八个 Browser Tool 只通过现有 Executor 进入 Worker。"""

    def setUp(self) -> None:
        """创建真实 SQLite 外键、Fake Client 和 ToolExecutor。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = build_state_paths(Path(self.temporary.name).resolve())
        owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        session = SessionRepository(self.database).get_or_create_cli(owner.id, "browser-test")
        turn = TurnRepository(self.database).create_with_user_message(
            session.id,
            "browser-event",
            "test-model",
            "browse",
        )
        TurnRepository(self.database).mark_running(turn.id)
        self.context = ToolContext(
            owner.id,
            session.id,
            turn.id,
            self.paths.home,
            self.paths.workspace,
            (),
        )
        self.client = _Client()
        self.tools = browser_tools(self.client, max_snapshot_chars=12_000)
        self.by_name = {tool.definition.name: tool for tool in self.tools}
        self.approvals = ApprovalRepository(self.database)
        self.executor = ToolExecutor(
            ToolRegistry(self.tools),
            PolicyEngine(
                network_resolver=lambda hostname, port: ("93.184.216.34",)
            ),
            ToolRunRepository(self.database),
            approvals=self.approvals,
        )

    def test_contract_exposes_exact_tools_and_rejects_unknown_arguments(self) -> None:
        """模型只能看到八个固定动作和 each action 的封闭参数对象。"""
        self.assertEqual(
            tuple(sorted(self.by_name)),
            (
                "browser_click",
                "browser_close",
                "browser_open",
                "browser_press",
                "browser_screenshot",
                "browser_scroll",
                "browser_snapshot",
                "browser_type",
            ),
        )
        with self.assertRaises(ToolValidationError):
            self.by_name["browser_open"].validate(
                {"url": "https://example.com", "javascript": "alert(1)"}
            )
        with self.assertRaises(ToolValidationError):
            self.by_name["browser_click"].validate(
                {
                    "origin": "https://example.com",
                    "generation": "g1",
                    "ref": "document.querySelector('button')",
                    "role": "button",
                }
            )

    async def test_safe_actions_use_context_session_and_bounded_snapshot(self) -> None:
        """Session ID 来自可信 Context，snapshot 字符预算不能由模型放大。"""
        opened = await self.executor.execute(
            self.context,
            ToolCall("open-1", "browser_open", {"url": "https://EXAMPLE.com/a"}),
        )
        snapshot = await self.executor.execute(
            self.context,
            ToolCall("snapshot-1", "browser_snapshot", {"cursor": 4}),
        )

        self.assertTrue(opened.succeeded)
        self.assertTrue(snapshot.succeeded)
        self.assertEqual(
            self.client.actions,
            [
                BrowserAction("u1:s1", "open", {"url": "https://example.com/a"}),
                BrowserAction(
                    "u1:s1",
                    "snapshot",
                    {"cursor": 4, "max_chars": 12_000},
                ),
            ],
        )

    async def test_click_requires_bound_approval_with_redacted_summary(self) -> None:
        """点击在批准前不能执行，审批摘要不展示 ref、generation 或页面 query。"""
        outcome = await self.executor.execute(
            self.context,
            ToolCall(
                "click-1",
                "browser_click",
                {
                    "origin": "https://example.com",
                    "generation": "private-generation",
                    "ref": "@e9",
                    "role": "button",
                },
            ),
        )

        self.assertIsNotNone(outcome.approval_id)
        self.assertEqual(self.client.actions, [])
        assert outcome.approval_id is not None
        summary = self.approvals.presentation(
            self.context.user_id,
            outcome.approval_id,
        ).approval.summary
        self.assertEqual(summary, "browser_click https://example.com:443 · button")
        self.assertNotIn("@e9", summary)
        self.assertNotIn("private-generation", summary)

    async def test_sensitive_type_is_denied_before_worker_and_error_is_stable(self) -> None:
        """敏感输入不建 Approval；Worker 内部错误也只能返回稳定 code。"""
        denied = await self.executor.execute(
            self.context,
            ToolCall(
                "type-secret",
                "browser_type",
                {
                    "origin": "https://example.com",
                    "generation": "g1",
                    "ref": "@e2",
                    "role": "textbox",
                    "input_kind": "password",
                    "text": "do-not-type",
                },
            ),
        )
        self.assertEqual(
            json.loads(denied.model_text)["error"]["code"],
            "browser_sensitive_input",
        )
        self.assertEqual(self.client.actions, [])

        self.client.error = BrowserProtocolError("browser_stale_ref", "private DOM")
        stale = await self.executor.execute(
            self.context,
            ToolCall("snapshot-stale", "browser_snapshot", {}),
        )
        self.assertEqual(
            json.loads(stale.model_text)["error"],
            {
                "code": "browser_stale_ref",
                "message": "browser action failed",
                "retryable": False,
            },
        )
        self.assertNotIn("private DOM", stale.model_text)

    async def test_worker_artifact_is_imported_without_exposing_staging_path(self) -> None:
        """Screenshot staging path 必须被 Store 消费，模型只能看到 Artifact metadata。"""
        staged = self.paths.downloads / "worker-shot.png"
        staged.write_bytes(_png(3, 2))
        staged.chmod(0o600)
        self.client.result = {
            "action": "screenshot",
            "artifact": {
                "staging_path": str(staged),
                "declared_media_type": "image/png",
                "source": "browser_screenshot",
                "width": 3,
                "height": 2,
            },
        }
        store = ArtifactStore(
            self.database,
            owner_id=self.context.user_id,
            root=self.paths.artifacts,
            staging_root=self.paths.downloads,
            max_bytes=1024,
        )
        screenshot = next(
            tool
            for tool in browser_tools(
                self.client,
                max_snapshot_chars=12_000,
                artifact_store=store,
            )
            if tool.definition.name == "browser_screenshot"
        )

        result = await screenshot.execute(self.context, {"full_page": False})

        model_text = result.to_model_text("browser_screenshot")
        self.assertTrue(result.ok)
        self.assertFalse(staged.exists())
        self.assertIn("artifact_id", model_text)
        self.assertNotIn("staging_path", model_text)
        self.assertNotIn(str(self.paths.downloads), model_text)

    async def test_runtime_exposes_browser_tools_only_when_browser_is_enabled(self) -> None:
        """Browser 开关应原子控制八个 Schema 和共享 Client 生命周期。"""
        base = load_config(self.paths, {}, {})
        disabled = create_runtime(base, self.paths, "test-key")
        enabled = create_runtime(
            replace(base, browser=replace(base.browser, enabled=True)),
            self.paths,
            "test-key",
        )
        try:
            self.assertFalse(
                any(
                    definition.name.startswith("browser_")
                    for definition in disabled.tool_definitions
                )
            )
            self.assertEqual(
                sum(
                    definition.name.startswith("browser_")
                    for definition in enabled.tool_definitions
                ),
                8,
            )
            self.assertIsNone(disabled.browser_client)
            self.assertIsNotNone(enabled.browser_client)
            assert enabled.browser_client is not None
            command = enabled.browser_client._command
            self.assertIn(f"--profile-root={self.paths.browser}", command)
            self.assertTrue(any(part.startswith("--executable-path=") for part in command))
            self.assertIn("--max-tabs=8", command)
            self.assertIn("--inactivity-timeout-ms=120000", command)
            self.assertIn("--headed=true", command)
            self.assertIn("--max-snapshot-chars=20000", command)
            self.assertIn(f"--staging-root={self.paths.downloads}", command)
            self.assertIn("--max-artifact-bytes=20971520", command)
        finally:
            await disabled.aclose()
            await enabled.aclose()

    async def test_enabled_runtime_removes_expired_browser_artifacts_on_startup(self) -> None:
        """Runtime 启用 Browser 时应清理已过 TTL 的私有文件，不让下载永久堆积。"""
        store = ArtifactStore(
            self.database,
            owner_id=self.context.user_id,
            root=self.paths.artifacts,
            staging_root=self.paths.downloads,
            max_bytes=1024,
        )
        staged = self.paths.downloads / "expired.png"
        staged.write_bytes(_png(1, 1))
        staged.chmod(0o600)
        artifact = store.put(
            staged,
            declared_media_type="image/png",
            source="browser_screenshot",
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE artifacts SET expires_at = ? WHERE artifact_id = ?",
                ("2000-01-01T00:00:00+00:00", artifact.artifact_id),
            )
        base = load_config(self.paths, {}, {})

        runtime = create_runtime(
            replace(base, browser=replace(base.browser, enabled=True)),
            self.paths,
            "test-key",
        )
        try:
            self.assertFalse(artifact.path.exists())
        finally:
            await runtime.aclose()


def _png(width: int, height: int) -> bytes:
    """返回只用于 MIME/IHDR 校验的最小 PNG 字节。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


if __name__ == "__main__":
    unittest.main()
