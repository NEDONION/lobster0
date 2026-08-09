"""运行只向 pi-tui 暴露 NDJSON 的内部 Python Bridge 进程。"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from miniclaw.config import ConfigError, load_config
from miniclaw.env import DotEnvError, load_dotenv
from miniclaw.paths import PathConfigurationError, build_state_paths, resolve_home
from miniclaw.runtime import create_runtime

from .protocol import MAX_FRAME_BYTES
from .server import BridgeServer


class StdioLineReader:
    """在线程中执行受大小限制的 stdin 行读取。"""

    async def readline(self) -> bytes:
        """读取最多比协议上限多一个字节，用于可靠识别超限帧。"""
        return await asyncio.to_thread(sys.stdin.buffer.readline, MAX_FRAME_BYTES + 1)


class StdioFrameWriter:
    """把完整协议帧刷新到 stdout，且不在 stdout 写日志。"""

    async def write(self, data: bytes) -> None:
        """在线程中写入并立即刷新一条完整帧。"""
        await asyncio.to_thread(self._write_sync, data)

    @staticmethod
    def _write_sync(data: bytes) -> None:
        """同步完成一条 stdout 帧的写入与刷新。"""
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def build_parser() -> argparse.ArgumentParser:
    """创建仅供 Node 前端启动的内部 Bridge 参数解析器。

    Returns:
        支持状态目录与可选 Workspace 覆盖的参数解析器。
    """
    parser = argparse.ArgumentParser(prog="python -m miniclaw.bridge")
    parser.add_argument("--home", help="absolute MiniClaw state directory")
    parser.add_argument("--workspace", type=Path, help="absolute task workspace")
    return parser


async def _run(home: str | None, workspace: Path | None) -> int:
    """装配唯一 Runtime，应用 Workspace 覆盖并运行 Bridge。

    Args:
        home: 可选的 MiniClaw 状态目录。
        workspace: 仅对当前 Bridge 进程生效的绝对 Workspace。

    Returns:
        Bridge 进程退出码。

    Raises:
        ConfigError: 配置或 Workspace 覆盖无效。
        DotEnvError: 当前目录的环境文件无法解析。
        PathConfigurationError: 状态目录配置无效。
        OSError: Runtime 或标准流初始化失败。
    """
    paths = build_state_paths(resolve_home(home))
    load_dotenv(Path.cwd() / ".env")
    overrides = {} if workspace is None else {"workspace": workspace}
    config = load_config(paths, overrides=overrides)
    api_key = os.environ.get(config.provider.api_key_env, "").strip()
    if not api_key:
        raise ConfigError(f"{config.provider.api_key_env} is not configured")
    runtime = create_runtime(config, paths, api_key)
    try:
        return await BridgeServer(runtime, StdioLineReader(), StdioFrameWriter()).run()
    finally:
        await runtime.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """运行内部 Bridge，并把启动错误限制在 stderr 和稳定退出码。

    Args:
        argv: 可选命令行参数；默认读取当前进程参数。

    Returns:
        成功时返回 Bridge 退出码，配置失败返回 2，中断返回 130。
    """
    arguments = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(arguments.home, arguments.workspace))
    except (ConfigError, DotEnvError, PathConfigurationError, OSError):
        print("error: MiniClaw Bridge startup failed", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
