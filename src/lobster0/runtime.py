"""CLI、TUI 与 Gateway 共用的唯一 Agent/Automation 运行期装配。"""

import json
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from lobster0.agent.compaction import ContextCompactor
from lobster0.agent.context import ContextBuilder
from lobster0.agent.runner import AgentRunner
from lobster0.agent.turn import TurnService
from lobster0.artifacts.store import ArtifactStore
from lobster0.automation.continuation import TaskApprovalContinuation
from lobster0.automation.delivery import TaskDeliveryService
from lobster0.automation.guard import AutomationPromptGuard
from lobster0.automation.heartbeat import HeartbeatReconciler
from lobster0.automation.models import DeliveryTarget
from lobster0.automation.repository import (
    AutomationControlRepository,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from lobster0.automation.runner import TaskRunner
from lobster0.automation.scheduler import Scheduler
from lobster0.bridge.conversations import ConversationConsole
from lobster0.browser.client import BrowserClient
from lobster0.browser.discovery import browser_worker_root, find_chromium
from lobster0.channels.base import ChannelLimits
from lobster0.channels.manager import ChannelManager
from lobster0.channels.observability import ChannelObserver
from lobster0.checkpoints.store import CheckpointStore
from lobster0.config import AppConfig, resolve_permission_roots
from lobster0.media.switching import VisionSwitchingProvider
from lobster0.memory.buffer import MemoryBufferRepository
from lobster0.memory.console import MemoryConsole
from lobster0.memory.extractor import (
    MEMORY_EXTRACTOR_PROMPT_HASH,
    MEMORY_EXTRACTOR_VERSION,
    MemoryExtractor,
)
from lobster0.memory.flush import FlushCoordinator, MemoryCapture
from lobster0.memory.maintenance import MemoryMaintenance
from lobster0.memory.markdown_store import MemoryMarkdownStore
from lobster0.memory.migration import LegacyMemoryImporter
from lobster0.memory.pipeline import MemoryPipelineHandler
from lobster0.memory.reconcile import MemoryReconciler
from lobster0.memory.repository import (
    MemoryCandidateRepository,
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryRunRepository,
    MemoryUnitRepository,
)
from lobster0.memory.retrieval import MemoryRetrieval
from lobster0.memory.review import MemoryReviewService
from lobster0.memory.service import MemoryService
from lobster0.memory.store import MemoryStore
from lobster0.memory.worker import MemoryFlushScheduler, MemoryWorker
from lobster0.paths import StatePaths
from lobster0.policy.command import normalize_command
from lobster0.policy.engine import PolicyEngine
from lobster0.policy.executables import discover_executables
from lobster0.policy.modes import PermissionState
from lobster0.policy.network import normalize_network_rule
from lobster0.providers.openai_compatible import OpenAICompatibleProvider
from lobster0.skills.loader import SkillLoader
from lobster0.storage.channels import (
    ChannelIdentityRepository,
    DeliveryRepository,
    InboundEventRepository,
)
from lobster0.storage.conversations import MessageRepository, SessionRepository, TurnRepository
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations
from lobster0.storage.repositories import OwnerRepository
from lobster0.storage.tooling import (
    ApprovalRepository,
    PermissionModeAuditRepository,
    PolicyRuleRepository,
    ToolRunRepository,
)
from lobster0.tools.artifacts import ReadArtifactTool
from lobster0.tools.automation import ManageTaskTool
from lobster0.tools.base import ToolDefinition
from lobster0.tools.browser import browser_tools
from lobster0.tools.command import RunCommandTool
from lobster0.tools.executor import ToolExecutor
from lobster0.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from lobster0.tools.memory import ProposeMemoryTool, ReadMemoryTool
from lobster0.tools.memory_v2 import (
    MemoryCorrectTool,
    MemoryFlushTool,
    MemoryForgetTool,
    MemoryGetTool,
    MemoryListTool,
    MemoryRememberTool,
    MemoryReviewListTool,
    MemorySearchTool,
)
from lobster0.tools.registry import ToolRegistry
from lobster0.tools.search import GlobTool, GrepTool
from lobster0.tools.system import SystemInfoTool
from lobster0.tools.task_completion import CompleteTaskTool
from lobster0.tools.web import HttpGetTool


@dataclass(slots=True)
class AgentRuntime:
    """拥有一次 TUI 进程内的 Provider、Service 与可见 Tool。"""

    owner_id: int
    model: str
    workspace: Path
    ui_language: str
    context_budget_tokens: int
    permission_state: PermissionState
    service: TurnService
    memory_console: MemoryConsole
    conversation_console: ConversationConsole
    memory_worker: MemoryWorker = field(repr=False)
    memory_scheduler: MemoryFlushScheduler = field(repr=False)
    task_runner: TaskRunner = field(repr=False)
    scheduler: Scheduler = field(repr=False)
    heartbeat_reconciler: HeartbeatReconciler = field(repr=False)
    automation_control: AutomationControlRepository = field(repr=False)
    automation_enabled: bool
    database: Database = field(repr=False)
    tool_definitions: tuple[ToolDefinition, ...]
    provider: OpenAICompatibleProvider = field(repr=False)
    # Bridge 的 Provider 配置写操作要读当前配置、写回配置文件与密钥文件，
    # 这两项是它需要的唯一入口，故随 Runtime 一起下发。
    paths: StatePaths = field(repr=False)
    config: AppConfig = field(repr=False)
    artifact_store: ArtifactStore = field(repr=False)
    # 附件自己的上限，落在 artifact_store 的 max_bytes 之内。
    attachment_max_bytes: int
    browser_client: BrowserClient | None = field(default=None, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _background_started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def astart(self) -> None:
        """幂等恢复 Automation，再启动 Runner/Scheduler 与 Memory Worker。"""
        if self._started:
            return
        if self._closed:
            raise RuntimeError("runtime is already closed")
        await self.memory_worker.start()
        if self.automation_enabled:
            now = datetime.now(UTC)
            recovery = self.task_runner.recover_startup(now=now)
            heartbeat = self.heartbeat_reconciler.reconcile(now)
            control = self.automation_control.status()
            if control.halted:
                _record_automation_event(
                    self.database,
                    self.owner_id,
                    "automation.halted",
                    {
                        "revision": control.revision,
                        "requeued": recovery.requeued,
                        "interrupted": recovery.interrupted,
                    },
                )
            else:
                await self.task_runner.start()
                try:
                    await self.scheduler.start()
                except BaseException:
                    await self.task_runner.stop()
                    raise
                self._background_started = True
                _record_automation_event(
                    self.database,
                    self.owner_id,
                    "automation.started",
                    {
                        "heartbeat_enqueued": heartbeat.enqueued,
                        "interrupted": recovery.interrupted,
                        "requeued": recovery.requeued,
                    },
                )
        self._started = True

    async def astop_background(self) -> None:
        """先停止 Scheduler intake，再停止/取消 bounded TaskRunner workers。"""
        if not self._background_started:
            return
        await self.scheduler.stop()
        await self.task_runner.stop()
        self._background_started = False
        _record_automation_event(
            self.database,
            self.owner_id,
            "automation.stopped",
            {"reason": "runtime_shutdown"},
        )

    async def aclose(self) -> None:
        """停止后台 Worker，再关闭 Browser 子进程和唯一 Provider 客户端。"""
        if self._closed:
            return
        await self.astop_background()
        self.memory_scheduler.schedule()
        try:
            await self.memory_worker.flush_once(timeout=3.0)
        finally:
            try:
                await self.memory_worker.stop(timeout=3.0)
            finally:
                try:
                    if self.browser_client is not None:
                        await self.browser_client.close()
                finally:
                    await self.provider.aclose()
                    self._closed = True



def _with_vision_switching(provider, config: AppConfig):
    """在带图轮次上自动切到视觉后端；未配置时原样返回主 Provider。

    视觉后端来自 ``providers`` 列表里 id 为 ``vision`` 的那一条，模型名取自
    ``LOBSTER0_VISION_MODEL``。三者缺一就退化为"没有视觉能力"——此时带图轮次会
    在 Provider 边界明确报错，而不是把图悄悄丢掉发给看不见图的模型。

    刻意不新增配置字段：``config.py`` 正在被并行改动，从既有的多 Provider 列表加
    环境变量取值可以完整实现能力，且不与之冲突。
    """
    vision_config = next(
        (item for item in config.providers if item.id == "vision"), None
    )
    model = os.environ.get("LOBSTER0_VISION_MODEL", "").strip()
    if vision_config is None or not model:
        return VisionSwitchingProvider(provider, vision=None, vision_model=None)
    vision_key = os.environ.get(vision_config.api_key_env, "").strip()
    if not vision_key:
        return VisionSwitchingProvider(provider, vision=None, vision_model=None)
    backend = OpenAICompatibleProvider(
        vision_config.base_url,
        vision_key,
        vision_config.timeout_seconds,
    )
    return VisionSwitchingProvider(provider, vision=backend, vision_model=model)


def create_runtime(config: AppConfig, paths: StatePaths, api_key: str) -> AgentRuntime:
    """按已校验配置装配唯一 Agent、Automation 与查询 Runtime。

    Args:
        config: 已通过类型与安全校验的应用配置。
        paths: 当前 Lobster0 状态目录集合。
        api_key: 仅传给 Provider 的运行期密钥值。

    Returns:
        拥有 Provider、TurnService、Console 与后台组件生命周期的 Runtime。

    Raises:
        OSError: Workspace、数据库或本地存储初始化失败。
        ValueError: 已校验配置与运行环境仍存在不一致。
    """
    permission_roots = resolve_permission_roots(
        config.permissions,
        config.workspace.path,
        home=Path.home(),
        platform_name=sys.platform,
    )
    effective_workspace = replace(
        config.workspace,
        read_only_roots=tuple(
            dict.fromkeys(
                (*config.workspace.read_only_roots, *permission_roots.read_roots)
            )
        ),
        write_roots=permission_roots.write_roots,
        owner_home=permission_roots.owner_home,
    )
    executable_environment = discover_executables(
        config.permissions.profile,
        home=permission_roots.owner_home,
        explicit_roots=config.permissions.executable_roots,
        discover_user=config.permissions.discover_user_executables,
        platform_name=sys.platform,
    )
    database = Database(paths.database)
    apply_migrations(database)
    owner = OwnerRepository(database).get_or_create()
    runs = ToolRunRepository(database)
    runs.interrupt_stale_runs()
    turns = TurnRepository(database)
    turns.interrupt_stale()
    scheduled_tasks = ScheduledTaskRepository(database)
    task_runs = TaskRunRepository(database)
    automation_control = AutomationControlRepository(database)
    provider = OpenAICompatibleProvider(
        config.provider.base_url,
        api_key,
        config.provider.timeout_seconds,
    )
    provider = _with_vision_switching(provider, config)
    approvals = ApprovalRepository(database)
    rules = PolicyRuleRepository(database)
    messages = MessageRepository(database)
    permission_state = PermissionState(
        config.tools.mode,
        audit=PermissionModeAuditRepository(database).record,
    )
    configured_command_rules = tuple(
        normalize_command(
            rule.program,
            rule.args,
            config.workspace.path,
            executable_path=executable_environment.path_value,
        )
        for rule in config.tools.run_command.allow_commands
    )
    command_rules = tuple(
        dict.fromkeys((*configured_command_rules, *rules.command_rules(owner.id)))
    )
    configured_network_rules = tuple(
        normalize_network_rule(value) for value in config.tools.http_get.allow_hosts
    )
    network_rules = tuple(
        dict.fromkeys((*configured_network_rules, *rules.network_rules(owner.id)))
    )
    checkpoint_store = (
        CheckpointStore(
            database,
            owner_id=owner.id,
            workspace=effective_workspace.path,
            state_home=paths.home,
            max_entries=config.checkpoint.max_entries,
            max_total_bytes=config.checkpoint.max_total_bytes,
            max_file_bytes=config.checkpoint.max_file_bytes,
            max_count=config.checkpoint.max_count,
        )
        if config.checkpoint.enabled
        else None
    )
    memory = MemoryStore(paths)
    memory_retrieval = MemoryRetrieval(database)
    memory_scheduler = MemoryFlushScheduler()
    memory_buffers = MemoryBufferRepository(database)
    memory_runs = MemoryRunRepository(database)
    memory_candidates = MemoryCandidateRepository(database)
    memory_units = MemoryUnitRepository(database)
    memory_reviews = MemoryReviewRepository(database)
    memory_manifests = MemoryManifestRepository(database)
    memory_markdown = MemoryMarkdownStore(paths, memory_manifests)
    memory_reconciler = MemoryReconciler(
        database,
        memory_markdown,
        memory_manifests,
    )
    memory_importer = LegacyMemoryImporter(
        paths,
        database,
        memory_markdown,
        memory_units,
        memory,
    )
    memory_service = MemoryService(
        memory_markdown,
        memory_units,
        memory_reviews,
        memory,
    )
    memory_governance = MemoryReviewService(
        database,
        memory_markdown,
        memory_units,
        memory_reviews,
        memory,
    )
    memory_maintenance = MemoryMaintenance(
        database,
        memory_markdown,
        memory_units,
        memory_reviews,
        reconciler=memory_reconciler,
        importer=memory_importer,
    )
    memory_maintenance.run_due(owner.id)
    memory_handler = MemoryPipelineHandler(
        database,
        MemoryExtractor(provider, model=config.agent.model),
        memory_markdown,
        memory_candidates,
        memory_units,
        memory_reviews,
    )
    memory_coordinator = FlushCoordinator(
        database,
        memory_buffers,
        memory_runs,
        memory_handler,
        extractor=MEMORY_EXTRACTOR_VERSION,
        prompt_hash=MEMORY_EXTRACTOR_PROMPT_HASH,
        maintenance=lambda current: memory_maintenance.run_due(owner.id, now=current),
    )
    memory_worker = MemoryWorker(
        memory_coordinator,
        worker_id="runtime-memory-worker",
        interval=600,
    )
    memory_scheduler.bind(memory_worker.notify)
    # 无条件构造：附件与浏览器是两件无关的事，而 browser.enabled 默认为 False。
    # delete_expired 随之改为始终执行——过期 Artifact 本来就该回收，不该因为
    # 用户关了浏览器就一直留着。
    artifact_store = ArtifactStore(
        database,
        owner_id=owner.id,
        root=paths.artifacts,
        staging_root=paths.downloads,
        max_bytes=config.browser.download_max_bytes,
    )
    artifact_store.delete_expired()
    available_tools = (
        SystemInfoTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        ManageTaskTool(
            scheduled_tasks,
            task_runs,
            AutomationPromptGuard(SkillLoader(paths.skills)),
            config.channels,
            enabled=config.automation.enabled,
            misfire_grace_seconds=config.automation.misfire_grace_seconds,
        ),
        HttpGetTool(
            timeout_seconds=config.tools.http_get.timeout_seconds,
            max_response_bytes=config.tools.http_get.max_response_bytes,
            allow_rules=network_rules,
            trusted_cidrs=config.tools.http_get.trusted_cidrs,
        ),
        RunCommandTool(
            timeout_seconds=config.tools.run_command.timeout_seconds,
            max_timeout_seconds=config.tools.run_command.max_timeout_seconds,
            executable_path=executable_environment.path_value,
            owner_home=permission_roots.owner_home,
            automation_backend=config.sandbox.backend,
            container_engine=config.sandbox.container_engine,
            sandbox_image=config.sandbox.image,
            sandbox_memory_mib=config.sandbox.memory_mib,
            sandbox_cpu_seconds=config.sandbox.cpu_seconds,
            sandbox_pids_limit=config.sandbox.pids_limit,
        ),
        ReadMemoryTool(memory),
        ProposeMemoryTool(memory),
        MemoryRememberTool(memory_service, messages),
        MemorySearchTool(memory_retrieval),
        MemoryGetTool(memory_retrieval),
        MemoryListTool(memory_retrieval),
        MemoryFlushTool(memory_scheduler.schedule),
        MemoryForgetTool(memory_governance, messages),
        MemoryCorrectTool(memory_governance, messages),
        MemoryReviewListTool(memory_governance),
        ReadArtifactTool(artifact_store),
    )
    configured_tools = tuple(
        tool for tool in available_tools if tool.definition.name in config.tools.enabled
    )
    browser_client = (
        BrowserClient(
            (
                "node",
                str(browser_worker_root() / "dist" / "server.js"),
                f"--profile-root={paths.browser}",
                f"--executable-path={find_chromium() or 'chromium'}",
                f"--max-tabs={config.browser.max_tabs}",
                "--inactivity-timeout-ms="
                f"{config.browser.inactivity_timeout_seconds * 1000}",
                f"--headed={str(config.browser.headed).lower()}",
                f"--max-snapshot-chars={config.browser.max_snapshot_chars}",
                f"--staging-root={paths.downloads}",
                f"--max-artifact-bytes={config.browser.download_max_bytes}",
            )
        )
        if config.browser.enabled
        else None
    )
    browser_toolset = (
        browser_tools(
            browser_client,
            max_snapshot_chars=config.browser.max_snapshot_chars,
            artifact_store=artifact_store,
        )
        if browser_client is not None
        else ()
    )
    tools = (*configured_tools, *browser_toolset)
    execution_tools = (*tools, CompleteTaskTool())
    executor = ToolExecutor(
        ToolRegistry(execution_tools),
        PolicyEngine(
            security=config.tools.security,
            ask=config.tools.ask,
            permission_state=permission_state,
            command_rules=command_rules,
            network_rules=network_rules,
            executable_path=executable_environment.path_value,
            trusted_cidrs=config.tools.http_get.trusted_cidrs,
        ),
        runs,
        result_max_chars=config.agent.tool_result_max_chars,
        approvals=approvals,
        policy_rules=rules,
        approval_ttl_seconds=config.tools.approval_ttl_seconds,
        checkpoint_store=checkpoint_store,
    )
    task_delivery = TaskDeliveryService(
        DeliveryRepository(database),
        task_runs,
        approvals=approvals,
        channel_max_chars={
            "feishu": config.channels.feishu.message_max_chars,
            "telegram": config.channels.telegram.message_max_chars,
            "discord": config.channels.discord.message_max_chars,
        },
    )
    automation_audit = partial(_record_automation_event, database, owner.id)
    service = TurnService(
        owner_id=owner.id,
        model=config.agent.model,
        sessions=SessionRepository(database),
        messages=messages,
        turns=turns,
        context=ContextBuilder(
            paths,
            memory,
            retrieval=memory_retrieval,
            context_budget_tokens=config.agent.context_budget_tokens,
        ),
        runner=AgentRunner(
            provider,
            executor,
            max_iterations=config.agent.max_tool_iterations,
            hard_max_iterations=config.agent.max_tool_iterations_hard,
            max_no_progress_iterations=config.agent.max_no_progress_iterations,
            max_turn_seconds=config.agent.max_turn_seconds,
        ),
        approvals=approvals,
        artifacts=artifact_store,
        compactor=ContextCompactor(
            messages,
            provider,
            model=config.agent.model,
            context_budget_tokens=config.agent.context_budget_tokens,
        ),
        memory_capture=MemoryCapture(
            memory_buffers,
            wake=memory_scheduler.schedule,
            wake_threshold=5,
        ),
        automation_gate=lambda: not automation_control.status().halted,
        automation_continuation=TaskApprovalContinuation(
            task_runs,
            delivery=task_delivery,
            audit=automation_audit,
        ),
        state_home=paths.home,
        workspace=effective_workspace,
    )
    task_runner = TaskRunner(
        task_runs,
        automation_control,
        service,
        allowed_tool_names=frozenset(
            tool.definition.name for tool in execution_tools
        ),
        lease_seconds=config.automation.lease_seconds,
        max_concurrent_runs=config.automation.max_concurrent_runs,
        delivery=task_delivery,
        audit=automation_audit,
    )
    scheduler = Scheduler(
        scheduled_tasks,
        task_runs,
        automation_control,
        max_active_tasks=config.automation.max_active_tasks,
        misfire_grace_seconds=config.automation.misfire_grace_seconds,
    )
    heartbeat_reconciler = HeartbeatReconciler(
        config.heartbeat,
        owner_id=owner.id,
        tasks=scheduled_tasks,
        runs=task_runs,
        max_concurrent_runs=config.automation.max_concurrent_runs,
        delivery=DeliveryTarget("none", "none"),
    )
    return AgentRuntime(
        owner_id=owner.id,
        model=config.agent.model,
        workspace=config.workspace.path,
        ui_language=config.ui.language,
        context_budget_tokens=config.agent.context_budget_tokens,
        permission_state=permission_state,
        service=service,
        memory_console=MemoryConsole(
            database,
            owner.id,
            memory_retrieval,
            memory_governance,
            memory_reconciler,
            memory_scheduler.schedule,
        ),
        conversation_console=ConversationConsole(database),
        memory_worker=memory_worker,
        memory_scheduler=memory_scheduler,
        task_runner=task_runner,
        scheduler=scheduler,
        heartbeat_reconciler=heartbeat_reconciler,
        automation_control=automation_control,
        automation_enabled=config.automation.enabled,
        database=database,
        tool_definitions=tuple(
            tool.definition for tool in sorted(tools, key=lambda tool: tool.definition.name)
        ),
        provider=provider,
        paths=paths,
        config=config,
        artifact_store=artifact_store,
        attachment_max_bytes=config.attachments.max_bytes,
        browser_client=browser_client,
    )


def _record_automation_event(
    database: Database,
    owner_id: int,
    event_type: str,
    metadata: dict[str, int | str],
) -> None:
    """持久化只含 code/count/revision 的 Automation lifecycle audit。"""
    allowed = {
        "automation.started",
        "automation.halted",
        "automation.stopped",
        "task_run.claimed",
        "task_run.waiting_approval",
        "task_run.terminal",
    }
    if event_type not in allowed:
        raise ValueError("automation lifecycle event is invalid")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO audit_events (event_type, user_id, summary, metadata_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (
                event_type,
                owner_id,
                event_type,
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                datetime.now(UTC).isoformat(),
            ),
        )


def limits_for_channel(config: AppConfig, channel: str) -> ChannelLimits:
    """把三套强类型配置收窄为 Manager/Experience 共用预算。"""
    if channel == "feishu":
        selected = config.channels.feishu
        progress_update_interval = 0.5
    elif channel == "telegram":
        selected = config.channels.telegram
        progress_update_interval = selected.progress_update_interval
    elif channel == "discord":
        selected = config.channels.discord
        progress_update_interval = selected.progress_update_interval
    else:
        raise ValueError("unsupported channel")
    return ChannelLimits(
        channel=channel,
        account_id=selected.account_id,
        queue_size=selected.queue_size,
        worker_count=selected.worker_count,
        message_max_chars=selected.message_max_chars,
        progress_update_interval=progress_update_interval,
    )


def create_channel_manager(
    paths: StatePaths,
    runtime: AgentRuntime,
    limits: ChannelLimits,
    *,
    owner_external_user_id: str,
    observer: ChannelObserver | None = None,
) -> ChannelManager:
    """为一个 Channel 装配复用唯一 TurnService 的 durable Manager。"""
    database = Database(paths.database)
    return ChannelManager(
        owner_id=runtime.owner_id,
        owner_external_user_id=owner_external_user_id,
        permission_state=runtime.permission_state,
        service=runtime.service,
        sessions=SessionRepository(database),
        messages=MessageRepository(database),
        turns=TurnRepository(database),
        identities=ChannelIdentityRepository(database),
        inbound=InboundEventRepository(database),
        deliveries=DeliveryRepository(database),
        channel=limits.channel,
        account_id=limits.account_id,
        queue_size=limits.queue_size,
        worker_count=limits.worker_count,
        message_max_chars=limits.message_max_chars,
        observer=observer,
    )
