"""macOS sandbox-exec compatible Seatbelt backend。"""

import os
import sys
import tempfile
from pathlib import Path

from miniclaw.sandbox.base import (
    ExecutionPlan,
    ExecutionReceipt,
    SandboxAvailability,
    SandboxPlanError,
    SandboxUnavailableError,
)
from miniclaw.sandbox.docker import _rebind_receipt
from miniclaw.sandbox.host import EnvironmentResolver, HostSandbox


class SeatbeltSandbox:
    """生成 deny-default profile，并通过 exact sandbox-exec 路径执行。"""

    def __init__(
        self,
        *,
        executable: str = "/usr/bin/sandbox-exec",
        platform: str | None = None,
        environment_resolver: EnvironmentResolver | None = None,
    ) -> None:
        """绑定平台、可信 executable 和受管环境 resolver。"""
        path = Path(executable)
        if not path.is_absolute():
            raise ValueError("seatbelt executable must be absolute")
        self._executable = path
        self._platform = platform or sys.platform
        self._environment_resolver = environment_resolver or _default_environment

    def availability(self) -> SandboxAvailability:
        """仅当运行于 macOS 且 exact sandbox-exec 可执行时报告可用。"""
        available = (
            self._platform == "darwin"
            and self._executable.is_file()
            and os.access(self._executable, os.X_OK)
        )
        return SandboxAvailability(
            available,
            "seatbelt is ready" if available else "seatbelt is unavailable",
        )

    def build_profile(self, plan: ExecutionPlan) -> str:
        """只用 canonical literal paths 生成 deny-default Seatbelt profile。"""
        if plan.backend != "seatbelt":
            raise SandboxPlanError("sandbox_backend_mismatch")
        if plan.network_mode != "none":
            raise ValueError("sandbox_network_unsupported")
        lines = [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(deny network*)",
            "(allow process-fork)",
            f'(allow process-exec (literal "{_escape(plan.argv[0])}"))',
            '(allow file-read-metadata (literal "/"))',
            '(allow file-read* (subpath "/usr/lib"))',
            '(allow file-read* (subpath "/System/Library"))',
        ]
        for root in plan.read_roots:
            escaped = _escape(str(root))
            lines.append(
                f'(allow file-read-metadata file-test-existence '
                f'(path-ancestors "{escaped}"))'
            )
            lines.append(f'(allow file-read* (subpath "{escaped}"))')
        for root in plan.write_roots:
            escaped = _escape(str(root))
            lines.append(
                f'(allow file-read-metadata file-test-existence '
                f'(path-ancestors "{escaped}"))'
            )
            lines.append(f'(allow file-read* (subpath "{escaped}"))')
            lines.append(f'(allow file-write* (subpath "{escaped}"))')
        return "\n".join(lines) + "\n"

    async def execute(self, plan: ExecutionPlan) -> ExecutionReceipt:
        """用 owner-only 临时 profile 执行，并在所有结果下删除 profile。"""
        if not self.availability().available:
            raise SandboxUnavailableError()
        profile = self.build_profile(plan)
        descriptor, profile_name = tempfile.mkstemp(prefix="miniclaw-seatbelt-", suffix=".sb")
        profile_path = Path(profile_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(profile)
            wrapper = ExecutionPlan(
                argv=(str(self._executable), "-f", str(profile_path), "--", *plan.argv),
                cwd=plan.cwd,
                environment_names=plan.environment_names,
                read_roots=plan.read_roots,
                write_roots=plan.write_roots,
                timeout_seconds=plan.timeout_seconds,
                memory_mib=plan.memory_mib,
                cpu_seconds=plan.cpu_seconds,
                pids_limit=plan.pids_limit,
                network_mode="none",
                backend="host",
            )
            receipt = await HostSandbox(self._environment_resolver).execute(wrapper)
            return _rebind_receipt(receipt, plan, "seatbelt")
        finally:
            profile_path.unlink(missing_ok=True)


def _escape(value: str) -> str:
    """把 canonical path 安全编码为 Seatbelt 双引号 literal。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _default_environment(name: str) -> str | None:
    """只解析 sandbox-exec 子进程所需的非 Secret 最小环境。"""
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"}.get(name)
