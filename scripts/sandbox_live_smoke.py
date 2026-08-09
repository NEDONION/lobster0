#!/usr/bin/env python3
"""显式运行 Docker/Seatbelt 的真实 containment smoke。"""

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import cast

from miniclaw.sandbox.base import ExecutionPlan, ExecutionReceipt, SandboxBackendName
from miniclaw.sandbox.docker import DockerSandbox
from miniclaw.sandbox.seatbelt import SeatbeltSandbox


def _arguments() -> argparse.Namespace:
    """解析必须显式确认的 live 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("docker", "seatbelt"), required=True)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--image", help="Docker image pinned as name@sha256:digest")
    parser.add_argument("--executable", help="Exact backend executable path")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    """执行 workspace write、外部 Secret deny 与 network deny 探针。"""
    if not args.confirm_live:
        print("refusing live sandbox execution without --confirm-live", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="miniclaw-sandbox-smoke-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        secret = root / "outside-secret"
        secret.write_text("MINICLAW_SMOKE_SECRET", encoding="utf-8")
        probe = workspace / "probe.py"
        result = workspace / "result.txt"
        probe.write_text(_probe_source(), encoding="utf-8")
        if args.backend == "docker":
            if not args.image:
                print("--image with sha256 digest is required for Docker", file=sys.stderr)
                return 2
            executable = args.executable or shutil.which("docker")
            if executable is None:
                print("Docker executable is unavailable", file=sys.stderr)
                return 3
            backend = DockerSandbox(
                image=args.image,
                docker_executable=str(Path(executable).resolve()),
            )
            argv = ("python", "/workspace/probe.py", str(secret), "/workspace/result.txt")
            backend_name = "docker"
        else:
            executable = args.executable or "/usr/bin/sandbox-exec"
            backend = SeatbeltSandbox(executable=executable)
            argv = (sys.executable, str(probe), str(secret), str(result))
            backend_name = "seatbelt"
        plan = ExecutionPlan(
            argv=argv,
            cwd=workspace,
            environment_names=("LANG",),
            read_roots=(),
            write_roots=(workspace,),
            timeout_seconds=20,
            memory_mib=256,
            cpu_seconds=10,
            pids_limit=64,
            network_mode="none",
            backend=cast(SandboxBackendName, backend_name),
        )
        receipt: ExecutionReceipt = await backend.execute(plan)
        safe = (
            receipt.exit_code == 0
            and result.read_text(encoding="utf-8") == "workspace-write-ok"
            and "outside-secret-denied" in receipt.stdout
            and "network-denied" in receipt.stdout
            and "MINICLAW_SMOKE_SECRET" not in receipt.canonical_json
        )
        print(
            f"backend={backend_name} exit={receipt.exit_code} "
            f"timeout={receipt.timed_out} containment={'PASS' if safe else 'FAIL'}"
        )
        return 0 if safe else 1


def _probe_source() -> str:
    """返回不依赖第三方库的文件与 network containment probe。"""
    return (
        "import pathlib, socket, sys\n"
        "secret, result = map(pathlib.Path, sys.argv[1:])\n"
        "result.write_text('workspace-write-ok', encoding='utf-8')\n"
        "try:\n"
        "    secret.read_text(encoding='utf-8')\n"
        "except (OSError, PermissionError):\n"
        "    print('outside-secret-denied')\n"
        "else:\n"
        "    print('outside-secret-readable')\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=1).close()\n"
        "except OSError:\n"
        "    print('network-denied')\n"
        "else:\n"
        "    print('network-open')\n"
    )


def main() -> int:
    """运行 async smoke 并返回适合 CI 的状态码。"""
    return asyncio.run(_run(_arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
