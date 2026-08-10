#!/usr/bin/env python3
"""显式运行 Docker/Seatbelt 的真实 containment smoke。"""

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import cast

from miniclaw.evals.production_evidence import (
    ProductionEvidenceError,
    build_seatbelt_evidence_report,
    clean_repository_commit,
    prepare_private_directory,
    utc_timestamp,
    write_private_json,
)
from miniclaw.policy.command import SAFE_EXECUTABLE_PATH
from miniclaw.policy.executables import discover_executables
from miniclaw.sandbox.base import ExecutionPlan, ExecutionReceipt, SandboxBackendName
from miniclaw.sandbox.docker import DockerSandbox, discover_rootless_client_transport
from miniclaw.sandbox.executables import capture_executable_chain
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
    parser.add_argument(
        "--output-dir",
        help="owner-only directory for commit-bound Seatbelt evidence",
    )
    parser.add_argument(
        "--probe",
        choices=("python", "node-chain"),
        default="python",
        help="Seatbelt executable chain to verify (default: python)",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    """执行 workspace write、外部 Secret deny 与 network deny 探针。"""
    if not args.confirm_live:
        print("refusing live sandbox execution without --confirm-live", file=sys.stderr)
        return 2
    if args.backend == "docker" and args.engine is None:
        print("--engine is required for Docker", file=sys.stderr)
        return 2
    if args.backend == "docker" and args.probe != "python":
        print("--probe node-chain is only supported by Seatbelt", file=sys.stderr)
        return 2
    output_value = getattr(args, "output_dir", None)
    output_dir: Path | None = None
    commit: str | None = None
    started_at = utc_timestamp()
    if output_value is not None:
        if args.backend != "seatbelt":
            print("--output-dir is only supported by Seatbelt", file=sys.stderr)
            return 2
        try:
            output_dir = Path(output_value).expanduser()
            if not output_dir.is_absolute():
                output_dir = (Path.cwd() / output_dir).resolve(strict=False)
            commit = clean_repository_commit(Path.cwd())
        except (OSError, ProductionEvidenceError):
            print("production evidence preflight failed", file=sys.stderr)
            return 2
    with tempfile.TemporaryDirectory(prefix="miniclaw-sandbox-smoke-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        if args.backend == "docker":
            workspace.chmod(0o703)
        secret = root / "outside-secret"
        secret.write_text("MINICLAW_SMOKE_SECRET", encoding="utf-8")
        probe = workspace / "probe.py"
        result = workspace / "result.txt"
        if args.backend == "docker":
            probe.write_text(_probe_source(), encoding="utf-8")
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
            try:
                probe_executable, executable_path = _seatbelt_probe(
                    args.probe,
                    workspace,
                )
            except (OSError, RuntimeError, ValueError):
                print("seatbelt probe is unavailable", file=sys.stderr)
                return 3
            environment = {"LANG": "C.UTF-8", "PATH": executable_path}
            backend = SeatbeltSandbox(
                executable=executable,
                environment_resolver=environment.get,
            )
            argv = (
                probe_executable,
                *((str(probe),) if args.probe == "python" else ()),
                str(secret),
                str(result),
            )
            read_roots = (
                (_seatbelt_python_runtime_root(probe_executable),)
                if args.probe == "python"
                else ()
            )
            executables = capture_executable_chain(
                Path(probe_executable),
                executable_path=executable_path,
            )
            backend_name = "seatbelt"
        plan = ExecutionPlan(
            argv=argv,
            cwd=workspace,
            environment_names=(
                ("LANG", "PATH") if args.backend == "seatbelt" else ("LANG",)
            ),
            read_roots=read_roots if args.backend == "seatbelt" else (),
            write_roots=(workspace,),
            timeout_seconds=20,
            memory_mib=256,
            cpu_seconds=10,
            pids_limit=64,
            network_mode="none",
            backend=cast(SandboxBackendName, args.backend),
            executables=executables if args.backend == "seatbelt" else (),
            schema_version=2 if args.backend == "seatbelt" else 1,
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
            _stable_status(
                backend_name,
                safe,
                args.probe if args.backend == "seatbelt" else None,
            )
        )
        if output_dir is not None and commit is not None:
            try:
                finished_at = utc_timestamp()
                report = build_seatbelt_evidence_report(
                    commit=commit,
                    probe=args.probe,
                    started_at=started_at,
                    finished_at=finished_at,
                    contained=safe,
                    secret_matches=0,
                )
                prepare_private_directory(output_dir)
                suffix = finished_at.replace("-", "").replace(":", "").replace(".", "")
                target = output_dir / f"{args.probe}-{suffix}.json"
                write_private_json(target, report)
            except ProductionEvidenceError:
                print("production evidence write failed", file=sys.stderr)
                return 1
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


def _stable_status(engine: str, contained: bool, probe: str | None = None) -> str:
    """返回不含本机路径、UID 或进程细节的稳定 live 结果。"""
    detail = f" probe={probe}" if probe is not None else ""
    return f"engine={engine}{detail} containment={'PASS' if contained else 'FAIL'}"


def _seatbelt_probe(probe: str, workspace: Path) -> tuple[str, str]:
    """准备选定的 Seatbelt probe 并返回 exact executable 与最小 PATH。

    Args:
        probe: ``python`` 或 ``node-chain``。
        workspace: live smoke 的临时可写目录。

    Returns:
        probe executable path 与解析 shebang 使用的最小 PATH。

    Raises:
        ValueError: probe 未知或所需 runtime 无法确定。
        OSError: Node fixture 无法安全写入。
    """
    if probe == "python":
        executable = _seatbelt_probe_executable()
        source = workspace / "probe.py"
        source.write_text(_probe_source(), encoding="utf-8")
        return executable, SAFE_EXECUTABLE_PATH
    if probe == "node-chain":
        return _seatbelt_node_probe(workspace)
    raise ValueError("unsupported Seatbelt probe")


def _seatbelt_node_probe(workspace: Path) -> tuple[str, str]:
    """创建 env-node fixture，并使用生产发现器冻结 Node PATH。

    Args:
        workspace: live smoke 的临时可写目录。

    Returns:
        可执行 JavaScript fixture path 与只含可信目录的 PATH。

    Raises:
        ValueError: Owner Home、Node 或发现目录不安全或不可用。
        OSError: fixture 无法写入或设置执行位。
    """
    environment = discover_executables(
        "personal",
        home=Path.home().resolve(strict=True),
        explicit_roots=(),
        discover_user=True,
        platform_name=sys.platform,
    )
    found = shutil.which("node", path=environment.path_value)
    if found is None:
        raise ValueError("Node runtime is unavailable")
    Path(found).resolve(strict=True)
    probe = workspace / "probe.js"
    probe.write_text("#!/usr/bin/env node\n" + _node_probe_source(), encoding="utf-8")
    probe.chmod(0o700)
    return str(probe), environment.path_value


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


def _node_probe_source() -> str:
    """返回不依赖 npm package 的 Node 文件与 network containment probe。"""
    return (
        "const fs = require('fs');\n"
        "const net = require('net');\n"
        "const [secret, result] = process.argv.slice(2);\n"
        "fs.writeFileSync(result, 'workspace-write-ok', 'utf8');\n"
        "try { fs.readFileSync(secret, 'utf8'); console.log('outside-secret-readable'); }\n"
        "catch (_) { console.log('outside-secret-denied'); }\n"
        "let finished = false;\n"
        "const finish = (message) => {\n"
        "  if (finished) return; finished = true; console.log(message); process.exit(0);\n"
        "};\n"
        "try {\n"
        "  const socket = net.createConnection({host: '1.1.1.1', port: 53});\n"
        "  socket.once('connect', () => { socket.destroy(); finish('network-open'); });\n"
        "  socket.once('error', () => finish('network-denied'));\n"
        "  setTimeout(() => { socket.destroy(); finish('network-denied'); }, 1000);\n"
        "} catch (_) { finish('network-denied'); }\n"
    )


def main() -> int:
    """运行 async smoke 并返回适合 CI 的状态码。"""
    return asyncio.run(_run(_arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
