#!/usr/bin/env python3
"""把 setuptools 产出的 sdist 重写为 byte reproducible 的 gzip tar。

setuptools 的 sdist 不接受 ``SOURCE_DATE_EPOCH``：它按 PAX 格式为每个成员
写入构建时刻的高精度 mtime，因此同一棵源码树连续构建两次也必然得到不同的
摘要。wheel 不受影响——它本来就按 ``SOURCE_DATE_EPOCH`` 归一。

本脚本把 sdist 原地重写成与 Node/TUI bundle 同一套确定性规则（USTAR、成员
排序、mtime 与 uid/gid 归零），使发布出去的 sdist 与 CI 证明可复现的 sdist
是同一构造，而不是只在门禁里做一次性的比较。
"""

import argparse
import os
import tarfile
import tempfile
from pathlib import Path

if __package__:
    from scripts.build_node_bundle import NodeBundleError, write_deterministic_tar
else:
    from build_node_bundle import NodeBundleError, write_deterministic_tar

_MAX_MEMBERS = 20_000
_MAX_TOTAL_BYTES = 512 * 1024 * 1024


class SdistNormalizeError(Exception):
    """sdist 不是一个可安全归一化的 regular tree 时抛出。"""


def _extract(sdist: Path, destination: Path) -> str:
    """把 sdist 安全解包到 destination，并返回其唯一顶层目录名。

    Args:
        sdist: 待解包的 `.tar.gz`，只允许 regular file 与 directory。
        destination: 已存在的空目录。

    Returns:
        sdist 的唯一顶层目录名，例如 ``lobster0_agent-0.7.0``。

    Raises:
        SdistNormalizeError: 含 link/special 成员、逃逸路径、顶层目录不唯一，
            或超出成员数与总字节预算。
    """
    roots: set[str] = set()
    total = 0
    try:
        with tarfile.open(sdist, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_MEMBERS:
                raise SdistNormalizeError("sdist has too many members")
            for member in members:
                if not (member.isreg() or member.isdir()):
                    raise SdistNormalizeError("sdist contains a non-regular member")
                name = Path(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise SdistNormalizeError("sdist contains an escaping member path")
                if not name.parts:
                    raise SdistNormalizeError("sdist contains an empty member path")
                roots.add(name.parts[0])
                total += member.size
                if total > _MAX_TOTAL_BYTES:
                    raise SdistNormalizeError("sdist exceeds the extraction budget")
            if len(roots) != 1:
                raise SdistNormalizeError("sdist must have exactly one top-level directory")
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError, ValueError) as error:
        raise SdistNormalizeError("could not read the sdist") from error
    return roots.pop()


def normalize_sdist(sdist: Path) -> Path:
    """原地把一个 sdist 重写为确定性 archive。

    Args:
        sdist: 已存在的 `.tar.gz` 路径；成功后被同名的确定性 archive 替换。

    Returns:
        被重写的 sdist 路径。

    Raises:
        SdistNormalizeError: sdist 不可读、含不安全成员，或重写失败。
    """
    source = Path(sdist)
    if not source.is_file() or source.is_symlink():
        raise SdistNormalizeError("sdist must be a regular file")
    with tempfile.TemporaryDirectory(dir=source.parent) as scratch:
        workspace = Path(scratch)
        unpacked = workspace / "tree"
        unpacked.mkdir()
        _extract(source, unpacked)
        rebuilt = workspace / "rebuilt.tar.gz"
        try:
            write_deterministic_tar(unpacked, rebuilt)
        except NodeBundleError as error:
            raise SdistNormalizeError("could not rewrite the sdist") from error
        os.replace(rebuilt, source)
    return source


def main() -> int:
    """把命令行给出的每个 sdist 原地归一化。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdist", nargs="+", type=Path, help="待归一化的 sdist 路径")
    arguments = parser.parse_args()
    for path in arguments.sdist:
        normalize_sdist(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
