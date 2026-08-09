"""运行只向 pi-tui 暴露 NDJSON 的内部 Python Bridge 进程。"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from miniclaw.config import ConfigError, load_config
from miniclaw.env import DotEnvError, load_dotenv, resolve_dotenv_path
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
    """创建仅供 Node 前端启动的内部 Bridge 参数解析器。"""
    parser = argparse.ArgumentParser(prog="python -m miniclaw.bridge")
    parser.add_argument("--home", help="absolute MiniClaw state directory")
    return parser


async def _run(home: str | None) -> int:
    """装配唯一 Runtime，运行 Bridge，并在退出时关闭 Provider。"""
    paths = build_state_paths(resolve_home(home))
    load_dotenv(resolve_dotenv_path(paths, os.environ))
    config = load_config(paths)
    api_key = os.environ.get(config.provider.api_key_env, "").strip()
    if not api_key:
        raise ConfigError(f"{config.provider.api_key_env} is not configured")
    runtime = create_runtime(config, paths, api_key)
    try:
        return await BridgeServer(runtime, StdioLineReader(), StdioFrameWriter()).run()
    finally:
        await runtime.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """运行内部 Bridge，并把启动错误限制在 stderr 和稳定退出码。"""
    arguments = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(arguments.home))
    except (ConfigError, DotEnvError, PathConfigurationError, OSError):
        print("error: MiniClaw Bridge startup failed", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
