#!/usr/bin/env python3
"""显式运行 Docker/Seatbelt 的真实 containment smoke。"""

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import cast

from miniclaw.policy.command import SAFE_EXECUTABLE_PATH
from miniclaw.sandbox.base import ExecutionPlan, ExecutionReceipt, SandboxBackendName
from miniclaw.sandbox.docker import DockerSandbox, discover_rootless_client_transport
from miniclaw.sandbox.seatbelt import SeatbeltSandbox


def _arguments() -> argparse.Namespace:
    """解析必须显式确认的 live 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("docker", "seatbelt"), required=True)
    parser.add_argument(
        "--engine", choices=("docker-rootless", "podman-rootless")
    )
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--image", help="Docker image pinned as name@sha256:digest")
    parser.add_argument("--executable", help="Exact backend executable path")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    """执行 workspace write、外部 Secret deny 与 network deny 探针。"""
    if not args.confirm_live:
        print("refusing live sandbox execution without --confirm-live", file=sys.stderr)
        return 2
    if args.backend == "docker" and args.engine is None:
        print("--engine is required for Docker", file=sys.stderr)
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
            try:
                backend, backend_name = _rootless_backend(args)
            except (OSError, RuntimeError, ValueError):
                print("rootless container engine is unavailable", file=sys.stderr)
                return 3
            argv = ("python", "/workspace/probe.py", str(secret), "/workspace/result.txt")
        else:
            executable = args.executable or "/usr/bin/sandbox-exec"
            backend = SeatbeltSandbox(executable=executable)
            probe_executable = _seatbelt_probe_executable()
            argv = (
                probe_executable,
                str(probe),
                str(secret),
                str(result),
            )
            read_roots = (_seatbelt_python_runtime_root(probe_executable),)
            backend_name = "seatbelt"
        plan = ExecutionPlan(
            argv=argv,
            cwd=workspace,
            environment_names=("LANG",),
            read_roots=read_roots if args.backend == "seatbelt" else (),
            write_roots=(workspace,),
            timeout_seconds=20,
            memory_mib=256,
            cpu_seconds=10,
            pids_limit=64,
            network_mode="none",
            backend=cast(SandboxBackendName, args.backend),
        )
        receipt: ExecutionReceipt = await backend.execute(plan)
        safe = (
            receipt.exit_code == 0
            and result.read_text(encoding="utf-8") == "workspace-write-ok"
            and "outside-secret-denied" in receipt.stdout
            and "network-denied" in receipt.stdout
            and "MINICLAW_SMOKE_SECRET" not in receipt.canonical_json
        )
        print(_stable_status(backend_name, safe))
        return 0 if safe else 1


def _rootless_backend(args: argparse.Namespace) -> tuple[DockerSandbox, str]:
    """用生产发现器创建显式 rootless Docker/Podman backend。

    Args:
        args: 已解析且包含 engine、image 与可选 exact executable 的参数。

    Returns:
        已绑定验证 transport 的 backend 与稳定 engine 名称。

    Raises:
        SandboxUnavailableError: rootless executable、runtime 或 socket 不安全。
        ValueError: engine、image 或 executable 参数无效。
    """
    engine = args.engine
    if engine not in {"docker-rootless", "podman-rootless"}:
        raise ValueError("rootless engine is required")
    explicit = Path(args.executable).resolve() if args.executable else None
    executable_path = str(explicit.parent) if explicit is not None else SAFE_EXECUTABLE_PATH
    which = (
        (lambda name, *, path: str(explicit))
        if explicit is not None
        else None
    )
    transport = discover_rootless_client_transport(
        engine,
        executable_path,
        Path.home().resolve(),
        which=which,
    )
    return (
        DockerSandbox(
            image=args.image,
            container_engine=engine,
            docker_executable=str(transport.executable),
            client_transport=transport,
        ),
        engine,
    )


def _stable_status(engine: str, contained: bool) -> str:
    """返回不含本机路径、UID 或进程细节的稳定 live 结果。"""
    return f"engine={engine} containment={'PASS' if contained else 'FAIL'}"


def _seatbelt_probe_executable(value: str | None = None) -> str:
    """冻结真实 Python executable，避免把 venv symlink 写入 Plan。"""
    return str(Path(value or sys.executable).resolve(strict=True))


def _seatbelt_python_runtime_root(executable: str) -> Path:
    """返回只覆盖当前 Python 版本的最小只读 runtime root。"""
    path = Path(executable)
    root = path.parent.parent.resolve(strict=True)
    if root == Path(root.anchor) or not (root / "lib").is_dir():
        raise ValueError("seatbelt Python runtime root is unavailable")
    return root


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
