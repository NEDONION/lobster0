"""不经过 Shell、通过 immutable ExecutionPlan 执行的命令 Tool。"""

import os
from pathlib import Path
from typing import cast

from miniclaw.policy.command import (
    SAFE_EXECUTABLE_PATH,
    CommandPolicyError,
    normalize_command,
)
from miniclaw.providers.base import JsonValue
from miniclaw.sandbox.base import (
    ExecutionPlan,
    ExecutionReceipt,
    SandboxBackendName,
    SandboxPlanError,
)
from miniclaw.sandbox.docker import DockerSandbox, discover_rootless_client_transport
from miniclaw.sandbox.host import HostSandbox
from miniclaw.sandbox.seatbelt import SeatbeltSandbox
from miniclaw.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)

_MAX_ARGS = 64
_MAX_ARGV_BYTES = 32 * 1024


class RunCommandTool:
    """在固定 Workspace 中执行一个解析后的程序和原样参数数组。"""

    definition = ToolDefinition(
        name="run_command",
        description=(
            "Run a single executable directly with exact arguments in the workspace. "
            "Never use a shell, pipeline, redirection, or inline code. Call this tool to "
            "request approval when needed instead of claiming it is unavailable. On macOS, "
            "if the exact installed app name is uncertain, call system_info applications "
            'first; then launch it with open -a: program "open", args ["-a", "Exact Name"]. '
            "For Feishu/Lark documents, messages, calendars, tasks, or other cloud services, "
            "follow the active feishu-lark-cli Skill and call program lark-cli with exact args. "
            "Do not claim that Feishu APIs are unavailable and do not search the local "
            "workspace for cloud data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "program": {"type": "string"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _MAX_ARGS,
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["program", "args"],
            "additionalProperties": False,
        },
        risk=ToolRisk.HIGH,
    )

    def __init__(
        self,
        *,
        timeout_seconds: int = 30,
        max_timeout_seconds: int = 120,
        executable_path: str = SAFE_EXECUTABLE_PATH,
        owner_home: Path | None = None,
        automation_backend: str = "host",
        container_engine: str = "docker-rootless",
        sandbox_image: str = "miniclaw-sandbox:phase6",
        sandbox_memory_mib: int = 512,
        sandbox_cpu_seconds: int = 60,
        sandbox_pids_limit: int = 128,
    ) -> None:
        if (
            type(timeout_seconds) is not int
            or type(max_timeout_seconds) is not int
            or timeout_seconds <= 0
            or max_timeout_seconds <= 0
            or max_timeout_seconds > 120
            or timeout_seconds > max_timeout_seconds
        ):
            raise ValueError("command timeouts must satisfy 0 < default <= maximum <= 120")
        if not isinstance(executable_path, str) or not executable_path:
            raise ValueError("executable_path must be a non-empty string")
        if owner_home is not None and not owner_home.is_absolute():
            raise ValueError("owner_home must be absolute")
        if automation_backend not in {"host", "docker", "seatbelt"}:
            raise ValueError("automation_backend is invalid")
        if container_engine not in {"docker-rootless", "podman-rootless"}:
            raise ValueError("container_engine is invalid")
        for value, name, maximum in (
            (sandbox_memory_mib, "sandbox_memory_mib", 32_768),
            (sandbox_cpu_seconds, "sandbox_cpu_seconds", 3600),
            (sandbox_pids_limit, "sandbox_pids_limit", 4096),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} is invalid")
        self._timeout_seconds = timeout_seconds
        self._max_timeout_seconds = max_timeout_seconds
        self._executable_path = executable_path
        self._owner_home = owner_home
        self._automation_backend = cast(SandboxBackendName, automation_backend)
        self._container_engine = container_engine
        self._sandbox_image = sandbox_image
        self._sandbox_memory_mib = sandbox_memory_mib
        self._sandbox_cpu_seconds = sandbox_cpu_seconds
        self._sandbox_pids_limit = sandbox_pids_limit

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验 program、字符串 argv 和不可放大的 timeout。"""
        if set(arguments) - {"program", "args", "timeout_seconds"}:
            raise ToolValidationError(
                "run_command only accepts program, args, and timeout_seconds"
            )
        if "program" not in arguments or "args" not in arguments:
            raise ToolValidationError("run_command requires program and args")
        program = arguments["program"]
        args = arguments["args"]
        timeout = arguments.get("timeout_seconds", self._timeout_seconds)
        if not isinstance(program, str) or not program:
            raise ToolValidationError("program must be a non-empty string")
        if (
            not isinstance(args, list)
            or len(args) > _MAX_ARGS
            or any(not isinstance(argument, str) for argument in args)
        ):
            raise ToolValidationError("args must be a list of at most 64 strings")
        argv = [program, *args]
        argv_bytes = sum(
            len(value.encode("utf-8", errors="surrogatepass")) for value in argv
        )
        if argv_bytes > _MAX_ARGV_BYTES:
            raise ToolValidationError("command argv must not exceed 32 KiB")
        if type(timeout) is not int or not 1 <= timeout <= self._max_timeout_seconds:
            raise ToolValidationError(
                f"timeout_seconds must be between 1 and {self._max_timeout_seconds}"
            )
        return {"program": program, "args": args, "timeout_seconds": timeout}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """兼容直接调用：构造一次 Plan 后交给 Host backend 执行。"""
        try:
            plan = self.build_execution_plan(context, arguments)
            result, _ = await self.execute_plan(context, plan)
        except SandboxPlanError as error:
            return ToolResult.failure(error.code, error.code)
        return result

    def build_execution_plan(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ExecutionPlan:
        """把 Policy 规范化的 exact argv 固化为 Host ExecutionPlan。"""
        program = arguments["program"]
        args = arguments["args"]
        timeout = arguments["timeout_seconds"]
        assert isinstance(program, str)
        assert isinstance(args, list) and all(isinstance(argument, str) for argument in args)
        assert type(timeout) is int
        try:
            normalized = normalize_command(
                program,
                tuple(args),
                context.workspace,
                executable_path=self._executable_path,
            )
        except CommandPolicyError as error:
            raise SandboxPlanError(error.code, str(error)) from None
        environment = _safe_environment(self._executable_path, self._owner_home)
        automation = context.source == "automation"
        if automation:
            environment.pop("HOME", None)
        backend = self._automation_backend if automation else "host"
        planned_program = (
            Path(normalized.resolved_program).name
            if backend == "docker"
            else normalized.resolved_program
        )
        return ExecutionPlan(
            argv=(planned_program, *normalized.args),
            cwd=context.workspace,
            environment_names=tuple(environment),
            read_roots=(context.workspace,) if automation else (),
            write_roots=() if automation else (context.workspace,),
            timeout_seconds=timeout,
            memory_mib=self._sandbox_memory_mib if automation else 512,
            cpu_seconds=self._sandbox_cpu_seconds if automation else timeout,
            pids_limit=self._sandbox_pids_limit if automation else 64,
            network_mode="none",
            backend=backend,
        )

    async def execute_plan(
        self,
        context: ToolContext,
        plan: ExecutionPlan,
    ) -> tuple[ToolResult, ExecutionReceipt]:
        """只执行传入 plan，不从 arguments 重新生成或替换批准内容。"""
        del context
        environment = _safe_environment(self._executable_path, self._owner_home)
        if plan.backend == "host":
            backend = HostSandbox(environment.get)
        elif plan.backend == "docker":
            transport = discover_rootless_client_transport(
                self._container_engine,
                self._executable_path,
                self._owner_home,
            )
            backend = DockerSandbox(
                image=self._sandbox_image,
                container_engine=self._container_engine,
                docker_executable=str(transport.executable),
                client_transport=transport,
                environment_resolver=environment.get,
            )
        else:
            backend = SeatbeltSandbox(environment_resolver=environment.get)
        receipt = await backend.execute(plan)
        if receipt.timed_out:
            return (
                ToolResult.failure("tool_timeout", "command exceeded its timeout"),
                receipt,
            )
        data: dict[str, JsonValue] = {
            "program": Path(plan.argv[0]).name,
            "args": list(plan.argv[1:]),
            "cwd": str(plan.cwd),
            "exit_code": receipt.exit_code,
            "stdout": receipt.stdout,
            "stderr": receipt.stderr,
            "stdout_truncated": receipt.stdout_truncated,
            "stderr_truncated": receipt.stderr_truncated,
            "duration_ms": receipt.duration_ms,
        }
        return ToolResult.success(data), receipt


def _safe_environment(
    executable_path: str = SAFE_EXECUTABLE_PATH,
    owner_home: Path | None = None,
) -> dict[str, str]:
    """构造不含 API Key、代理或用户环境的最小子进程环境。"""
    environment = {
        "PATH": executable_path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
    }
    if owner_home is not None:
        environment["HOME"] = str(owner_home)
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR", "PATHEXT"):
            if name in os.environ:
                environment[name] = os.environ[name]
    return environment
