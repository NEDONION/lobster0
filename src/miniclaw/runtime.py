"""CLI 与 TUI 共用的唯一 Agent 运行期装配。"""

from dataclasses import dataclass, field
from pathlib import Path

from miniclaw.agent.context import ContextBuilder
from miniclaw.agent.runner import AgentRunner
from miniclaw.agent.turn import TurnService
from miniclaw.config import AppConfig
from miniclaw.paths import StatePaths
from miniclaw.policy.command import normalize_command
from miniclaw.policy.engine import PolicyEngine
from miniclaw.providers.openai_compatible import OpenAICompatibleProvider
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
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.search import GlobTool, GrepTool
from miniclaw.tools.system import SystemInfoTool


@dataclass(slots=True)
class AgentRuntime:
    """拥有一次 TUI 进程内的 Provider、Service 与可见 Tool。"""

    owner_id: int
    model: str
    workspace: Path
    service: TurnService
    tool_definitions: tuple[ToolDefinition, ...]
    provider: OpenAICompatibleProvider = field(repr=False)

    async def aclose(self) -> None:
        """关闭唯一 Provider 客户端。"""
        await self.provider.aclose()


def create_runtime(config: AppConfig, paths: StatePaths, api_key: str) -> AgentRuntime:
    """按已校验配置装配当前七个内置 Tool 和唯一 TurnService。"""
    database = Database(paths.database)
    apply_migrations(database)
    owner = OwnerRepository(database).get_or_create()
    provider = OpenAICompatibleProvider(
        config.provider.base_url,
        api_key,
        config.provider.timeout_seconds,
    )
    available_tools = (
        SystemInfoTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        RunCommandTool(
            timeout_seconds=config.tools.run_command.timeout_seconds,
            max_timeout_seconds=config.tools.run_command.max_timeout_seconds,
        ),
    )
    tools = tuple(
        tool for tool in available_tools if tool.definition.name in config.tools.enabled
    )
    approvals = ApprovalRepository(database)
    configured_command_rules = tuple(
        normalize_command(rule.program, rule.args, config.workspace.path)
        for rule in config.tools.run_command.allow_commands
    )
    command_rules = tuple(
        dict.fromkeys(
            (*configured_command_rules, *PolicyRuleRepository(database).command_rules(owner.id))
        )
    )
    executor = ToolExecutor(
        ToolRegistry(tools),
        PolicyEngine(
            security=config.tools.security,
            ask=config.tools.ask,
            command_rules=command_rules,
        ),
        ToolRunRepository(database),
        result_max_chars=config.agent.tool_result_max_chars,
        approvals=approvals,
        approval_ttl_seconds=config.tools.approval_ttl_seconds,
    )
    service = TurnService(
        model=config.agent.model,
        sessions=SessionRepository(database),
        messages=MessageRepository(database),
        turns=TurnRepository(database),
        context=ContextBuilder(paths),
        runner=AgentRunner(
            provider,
            executor,
            max_iterations=config.agent.max_tool_iterations,
        ),
        approvals=approvals,
        state_home=paths.home,
        workspace=config.workspace,
    )
    return AgentRuntime(
        owner_id=owner.id,
        model=config.agent.model,
        workspace=config.workspace.path,
        service=service,
        tool_definitions=tuple(
            tool.definition for tool in sorted(tools, key=lambda tool: tool.definition.name)
        ),
        provider=provider,
    )
