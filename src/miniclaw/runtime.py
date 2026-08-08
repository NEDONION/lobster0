"""CLI 与 TUI 共用的唯一 Agent 运行期装配。"""

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from miniclaw.agent.compaction import ContextCompactor
from miniclaw.agent.context import ContextBuilder
from miniclaw.agent.runner import AgentRunner
from miniclaw.agent.turn import TurnService
from miniclaw.channels.base import ChannelLimits
from miniclaw.channels.manager import ChannelManager
from miniclaw.channels.observability import ChannelObserver
from miniclaw.config import AppConfig, resolve_permission_roots
from miniclaw.memory.buffer import MemoryBufferRepository
from miniclaw.memory.console import MemoryConsole
from miniclaw.memory.flush import MemoryCapture
from miniclaw.memory.markdown_store import MemoryMarkdownStore
from miniclaw.memory.repository import (
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryUnitRepository,
)
from miniclaw.memory.retrieval import MemoryRetrieval
from miniclaw.memory.service import MemoryService
from miniclaw.memory.store import MemoryStore
from miniclaw.memory.worker import MemoryFlushScheduler
from miniclaw.paths import StatePaths
from miniclaw.policy.command import normalize_command
from miniclaw.policy.engine import PolicyEngine
from miniclaw.policy.executables import discover_executables
from miniclaw.policy.modes import PermissionState
from miniclaw.policy.network import normalize_network_rule
from miniclaw.providers.openai_compatible import OpenAICompatibleProvider
from miniclaw.storage.channels import (
    ChannelIdentityRepository,
    DeliveryRepository,
    InboundEventRepository,
)
from miniclaw.storage.conversations import MessageRepository, SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import OwnerRepository
from miniclaw.storage.tooling import (
    ApprovalRepository,
    PermissionModeAuditRepository,
    PolicyRuleRepository,
    ToolRunRepository,
)
from miniclaw.tools.base import ToolDefinition
from miniclaw.tools.command import RunCommandTool
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from miniclaw.tools.memory import ProposeMemoryTool, ReadMemoryTool
from miniclaw.tools.memory_v2 import (
    MemoryFlushTool,
    MemoryGetTool,
    MemoryListTool,
    MemoryRememberTool,
    MemorySearchTool,
)
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.search import GlobTool, GrepTool
from miniclaw.tools.system import SystemInfoTool
from miniclaw.tools.web import HttpGetTool


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
    tool_definitions: tuple[ToolDefinition, ...]
    provider: OpenAICompatibleProvider = field(repr=False)

    async def aclose(self) -> None:
        """关闭唯一 Provider 客户端。"""
        await self.provider.aclose()


def create_runtime(config: AppConfig, paths: StatePaths, api_key: str) -> AgentRuntime:
    """按已校验配置装配内置 Tool、Memory Service 和唯一 TurnService。"""
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
    provider = OpenAICompatibleProvider(
        config.provider.base_url,
        api_key,
        config.provider.timeout_seconds,
    )
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
    memory = MemoryStore(paths)
    memory_retrieval = MemoryRetrieval(database)
    memory_scheduler = MemoryFlushScheduler()
    memory_service = MemoryService(
        MemoryMarkdownStore(paths, MemoryManifestRepository(database)),
        MemoryUnitRepository(database),
        MemoryReviewRepository(database),
        memory,
    )
    available_tools = (
        SystemInfoTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        HttpGetTool(
            timeout_seconds=config.tools.http_get.timeout_seconds,
            max_response_bytes=config.tools.http_get.max_response_bytes,
            allow_rules=network_rules,
        ),
        RunCommandTool(
            timeout_seconds=config.tools.run_command.timeout_seconds,
            max_timeout_seconds=config.tools.run_command.max_timeout_seconds,
            executable_path=executable_environment.path_value,
            owner_home=permission_roots.owner_home,
        ),
        ReadMemoryTool(memory),
        ProposeMemoryTool(memory),
        MemoryRememberTool(memory_service, messages),
        MemorySearchTool(memory_retrieval),
        MemoryGetTool(memory_retrieval),
        MemoryListTool(memory_retrieval),
        MemoryFlushTool(memory_scheduler.schedule),
    )
    tools = tuple(
        tool for tool in available_tools if tool.definition.name in config.tools.enabled
    )
    executor = ToolExecutor(
        ToolRegistry(tools),
        PolicyEngine(
            security=config.tools.security,
            ask=config.tools.ask,
            permission_state=permission_state,
            command_rules=command_rules,
            network_rules=network_rules,
            executable_path=executable_environment.path_value,
        ),
        runs,
        result_max_chars=config.agent.tool_result_max_chars,
        approvals=approvals,
        policy_rules=rules,
        approval_ttl_seconds=config.tools.approval_ttl_seconds,
    )
    service = TurnService(
        owner_id=owner.id,
        model=config.agent.model,
        sessions=SessionRepository(database),
        messages=messages,
        turns=TurnRepository(database),
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
        ),
        approvals=approvals,
        compactor=ContextCompactor(
            messages,
            provider,
            model=config.agent.model,
            context_budget_tokens=config.agent.context_budget_tokens,
        ),
        memory_capture=MemoryCapture(MemoryBufferRepository(database)),
        state_home=paths.home,
        workspace=effective_workspace,
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
            memory_scheduler.schedule,
        ),
        tool_definitions=tuple(
            tool.definition for tool in sorted(tools, key=lambda tool: tool.definition.name)
        ),
        provider=provider,
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
