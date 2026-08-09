"""生成 Task 6 安全解包测试使用的确定性 tar.gz。"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path


def _add_file(archive: tarfile.TarFile, name: str, body: bytes, mode: int = 0o644) -> None:
    """向 archive 加入一个指定名称、内容和 mode 的普通文件。"""
    member = tarfile.TarInfo(name)
    member.size = len(body)
    member.mode = mode
    archive.addfile(member, io.BytesIO(body))


def make_archive(path: Path, kind: str) -> Path:
    """创建一种有效或恶意 archive fixture 并返回路径。"""
    options: dict[str, object] = {}
    if kind == "pax":
        options["pax_headers"] = {"comment": "x" * 5_000_000}
    if kind in {"gnu_longname", "gnu_sparse"}:
        options["format"] = tarfile.GNU_FORMAT
    with tarfile.open(path, "w:gz", **options) as archive:
        if kind == "valid":
            directory = tarfile.TarInfo("bin/")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o777
            archive.addfile(directory)
            _add_file(archive, "bin/run", b"#!/bin/sh\n", 0o777)
            _add_file(archive, "README", b"trusted\n", 0o666)
        elif kind == "absolute":
            _add_file(archive, "/escape", b"x")
        elif kind == "dotdot":
            _add_file(archive, "safe/../escape", b"x")
        elif kind == "dot":
            _add_file(archive, "./escape", b"x")
        elif kind in {"symlink", "hardlink"}:
            member = tarfile.TarInfo(kind)
            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            member.linkname = "target"
            archive.addfile(member)
        elif kind == "device":
            member = tarfile.TarInfo("device")
            member.type = tarfile.CHRTYPE
            archive.addfile(member)
        elif kind == "fifo":
            member = tarfile.TarInfo("fifo")
            member.type = tarfile.FIFOTYPE
            archive.addfile(member)
        elif kind == "duplicate":
            _add_file(archive, "same", b"first")
            _add_file(archive, "same", b"second")
        elif kind == "case_collision":
            _add_file(archive, "Bin/Run", b"first")
            _add_file(archive, "bin/run", b"second")
        elif kind == "case_parent_collision":
            _add_file(archive, "Bin/one", b"first")
            _add_file(archive, "bin/two", b"second")
        elif kind == "unicode_collision":
            _add_file(archive, "café", b"first")
            _add_file(archive, "café", b"second")
        elif kind == "too_many":
            for index in range(33):
                _add_file(archive, f"files/{index}", b"")
        elif kind == "too_large":
            _add_file(archive, "large", b"x" * 4097)
        elif kind == "pax":
            _add_file(archive, "payload", b"x")
        elif kind == "gnu_longname":
            _add_file(archive, "a" * 200, b"x")
        elif kind == "gnu_sparse":
            member = tarfile.TarInfo("sparse")
            member.type = tarfile.GNUTYPE_SPARSE
            archive.addfile(member)
        else:
            raise ValueError(f"unknown archive fixture: {kind}")
    return path
