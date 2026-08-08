"""CLI 与 TUI 共用的唯一 Agent 运行期装配。"""

from dataclasses import dataclass, field
from pathlib import Path

from miniclaw.agent.compaction import ContextCompactor
from miniclaw.agent.context import ContextBuilder
from miniclaw.agent.runner import AgentRunner
from miniclaw.agent.turn import TurnService
from miniclaw.channels.manager import ChannelManager
from miniclaw.channels.observability import ChannelObserver
from miniclaw.config import AppConfig
from miniclaw.memory.store import MemoryStore
from miniclaw.paths import StatePaths
from miniclaw.policy.command import normalize_command
from miniclaw.policy.engine import PolicyEngine
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
    PolicyRuleRepository,
    ToolRunRepository,
)
from miniclaw.tools.base import ToolDefinition
from miniclaw.tools.command import RunCommandTool
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from miniclaw.tools.memory import ProposeMemoryTool, ReadMemoryTool
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
    service: TurnService
    tool_definitions: tuple[ToolDefinition, ...]
    provider: OpenAICompatibleProvider = field(repr=False)

    async def aclose(self) -> None:
        """关闭唯一 Provider 客户端。"""
        await self.provider.aclose()


def create_runtime(config: AppConfig, paths: StatePaths, api_key: str) -> AgentRuntime:
    """按已校验配置装配当前十个内置 Tool 和唯一 TurnService。"""
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
    configured_command_rules = tuple(
        normalize_command(rule.program, rule.args, config.workspace.path)
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
        ),
        ReadMemoryTool(memory),
        ProposeMemoryTool(memory),
    )
    tools = tuple(
        tool for tool in available_tools if tool.definition.name in config.tools.enabled
    )
    executor = ToolExecutor(
        ToolRegistry(tools),
        PolicyEngine(
            security=config.tools.security,
            ask=config.tools.ask,
            command_rules=command_rules,
            network_rules=network_rules,
        ),
        runs,
        result_max_chars=config.agent.tool_result_max_chars,
        approvals=approvals,
        policy_rules=rules,
        approval_ttl_seconds=config.tools.approval_ttl_seconds,
    )
    service = TurnService(
        model=config.agent.model,
        sessions=SessionRepository(database),
        messages=messages,
        turns=TurnRepository(database),
        context=ContextBuilder(
            paths,
            memory,
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
        state_home=paths.home,
        workspace=config.workspace,
    )
    return AgentRuntime(
        owner_id=owner.id,
        model=config.agent.model,
        workspace=config.workspace.path,
        ui_language=config.ui.language,
        context_budget_tokens=config.agent.context_budget_tokens,
        service=service,
        tool_definitions=tuple(
            tool.definition for tool in sorted(tools, key=lambda tool: tool.definition.name)
        ),
        provider=provider,
    )


def create_channel_manager(
    config: AppConfig,
    paths: StatePaths,
    runtime: AgentRuntime,
    *,
    observer: ChannelObserver | None = None,
) -> ChannelManager:
    """为飞书 Gateway 装配复用唯一 TurnService 的 durable ChannelManager。"""
    database = Database(paths.database)
    feishu = config.channels.feishu
    return ChannelManager(
        owner_id=runtime.owner_id,
        service=runtime.service,
        sessions=SessionRepository(database),
        messages=MessageRepository(database),
        turns=TurnRepository(database),
        identities=ChannelIdentityRepository(database),
        inbound=InboundEventRepository(database),
        deliveries=DeliveryRepository(database),
        channel="feishu",
        account_id=feishu.account_id,
        queue_size=feishu.queue_size,
        worker_count=feishu.worker_count,
        message_max_chars=feishu.message_max_chars,
        observer=observer,
    )
