"""MiniClaw 命令行入口。"""

import argparse
from collections.abc import Sequence

from miniclaw import __version__


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器。

    Returns:
        配置好程序名、简介和版本参数的解析器。MVP 子命令会在对应能力实现时加入。
    """
    parser = argparse.ArgumentParser(
        prog="miniclaw",
        description="MiniClaw — a tiny self-hosted personal agent.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行参数并运行当前可用入口。

    Args:
        argv: 需要解析的参数；为 ``None`` 时由 ``argparse`` 读取进程参数。

    Returns:
        正常显示帮助后的进程退出码 0。版本参数由 ``argparse`` 直接结束进程。
    """
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
