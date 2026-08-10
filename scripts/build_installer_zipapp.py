"""校验 stdlib import boundary 并构建 deterministic installer zipapp。"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import symtable
import sys
import tempfile
import zipapp
import zipfile
from pathlib import Path

_TIMESTAMP = 315_532_800
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_SHEBANG = b"#!/usr/bin/env python3\n"
_DYNAMIC_BUILTINS = {"__import__", "compile", "eval", "exec"}
_DYNAMIC_MODULES = {"builtins", "importlib"}
_DYNAMIC_NAMES = _DYNAMIC_BUILTINS | _DYNAMIC_MODULES | {
    "__builtins__",
    "__dict__",
    "globals",
    "import_module",
    "locals",
    "modules",
    "vars",
}
_DYNAMIC_ATTRIBUTES = (_DYNAMIC_BUILTINS - {"compile"}) | {
    "__dict__",
    "import_module",
    "modules",
}
_SAFE_GETATTR_NAMES = {
    "O_CLOEXEC",
    "O_DIRECTORY",
    "O_NOFOLLOW",
    "_evidence",
    "getcode",
    "getheader",
    "headers",
    "items",
    "kevent",
    "kqueue",
    "lstat",
    "require_backend",
    "run",
    "status",
    "waitid",
}
_SAFE_DUNDER_ATTRIBUTES = {
    "__init__",  # InstallError delegates construction to RuntimeError.
    "__new__",  # Frozen layout/service records require explicit allocation.
    "__post_init__",  # Sealed records re-run their bounded validation.
    "__setattr__",  # Frozen dataclass fields are initialized explicitly.
}


def validate_imports(source: Path) -> None:
    """拒绝 install package 中的第三方或跨 MiniClaw package import。

    Args:
        source: `src/miniclaw/install` 或同结构测试目录。

    Raises:
        ValueError: source 不安全、Python 无法解析或 import 超出 boundary。
    """
    if not source.is_dir() or source.is_symlink():
        raise ValueError("installer source must be a regular directory")
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    files = tuple(sorted(source.glob("*.py")))
    if not files or any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError("installer source contains unsafe Python files")
    for path in files:
        source_text = path.read_text(encoding="utf-8")
        tree = ast.parse(source_text, filename=str(path))
        _require_safe_syntax(tree, path, source_text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _require_import(alias.name, allowed, path)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    continue
                if node.level > 1:
                    raise ValueError(f"unsafe relative import in {path.name}")
                if node.module is None:
                    raise ValueError(f"unsafe import in {path.name}")
                _require_import(node.module, allowed, path)


def _require_safe_syntax(tree: ast.AST, path: Path, source_text: str) -> None:
    """拒绝绕过 static import allowlist 的动态加载与 code execution。

    Args:
        tree: 一个 install module 的完整 Python AST。
        path: 仅用于安全错误定位的源文件路径。
        source_text: 与 tree 对应的完整源码，供 stdlib symtable 分析 binding。

    Raises:
        ValueError: 发现 dynamic import、eval/exec/compile 或简单 alias 绕过。
    """
    scopes = [symtable.symtable(source_text, str(path), "exec")]
    while scopes:
        scope = scopes.pop()
        for symbol in scope.get_symbols():
            if symbol.get_name() == "getattr" and (
                symbol.is_parameter()
                or symbol.is_assigned()
                or symbol.is_imported()
                or symbol.is_local()
            ):
                raise ValueError(f"unsafe dynamic getattr in {path.name}")
        scopes.extend(scope.get_children())
    nodes = tuple(ast.walk(tree))
    direct_getattr: set[int] = set()
    unsafe_getattr = False
    for node in nodes:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
        ):
            if (
                len(node.args) not in {2, 3}
                or node.keywords
                or not isinstance(node.args[1], ast.Constant)
                or type(node.args[1].value) is not str
                or node.args[1].value not in _SAFE_GETATTR_NAMES
            ):
                unsafe_getattr = True
            else:
                direct_getattr.add(id(node.func))
    for node in nodes:
        if (
            isinstance(node, ast.Name)
            and node.id == "getattr"
            and id(node) not in direct_getattr
        ):
            unsafe_getattr = True
        if isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMES:
            raise ValueError(f"unsafe dynamic {node.id} in {path.name}")
        if isinstance(node, ast.Attribute):
            if (
                node.attr.startswith("__")
                and node.attr.endswith("__")
                and node.attr not in _SAFE_DUNDER_ATTRIBUTES
            ):
                raise ValueError(f"unsafe dynamic {node.attr} in {path.name}")
            if node.attr in _DYNAMIC_ATTRIBUTES:
                raise ValueError(f"unsafe dynamic {node.attr} in {path.name}")
        if isinstance(node, ast.Constant) and node.value in _DYNAMIC_NAMES:
            raise ValueError(f"unsafe dynamic {node.value} in {path.name}")
        if isinstance(node, ast.ImportFrom):
            blocked = next(
                (alias.name for alias in node.names if alias.name in _DYNAMIC_NAMES),
                None,
            )
            if blocked is not None:
                raise ValueError(f"unsafe dynamic {blocked} in {path.name}")
    for node in nodes:
        modules: tuple[str, ...] = ()
        if isinstance(node, ast.Import):
            modules = tuple(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules = (node.module,)
        blocked = next(
            (
                name.split(".", 1)[0]
                for name in modules
                if name.split(".", 1)[0] in _DYNAMIC_MODULES
            ),
            None,
        )
        if blocked is not None:
            raise ValueError(f"unsafe dynamic {blocked} in {path.name}")
    if unsafe_getattr:
        raise ValueError(f"unsafe dynamic getattr in {path.name}")


def build_zipapp(source: Path, output: Path) -> Path:
    """只复制 install package 并生成 byte-reproducible stdlib-only pyz。

    Args:
        source: 已校验的 `src/miniclaw/install`。
        output: 尚未存在或可原子替换的 `.pyz` 路径。

    Returns:
        已 chmod 0755 的输出路径。

    Raises:
        ValueError: import boundary 或输出路径无效。
        OSError: 临时复制、zip 或原子替换失败。
    """
    validate_imports(source)
    if not output.is_absolute():
        output = Path.cwd() / output
    output = Path(os.path.normpath(str(output)))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="miniclaw-installer-") as temporary:
        root = Path(temporary)
        application = root / "app"
        package = application / "miniclaw" / "install"
        package.mkdir(parents=True)
        namespace_marker = package.parent / "__init__.py"
        namespace_marker.write_bytes(b"")
        os.chmod(namespace_marker, 0o644)
        os.utime(namespace_marker, (_TIMESTAMP, _TIMESTAMP))
        for path in sorted(source.glob("*.py")):
            destination = package / path.name
            shutil.copyfile(path, destination)
            os.chmod(destination, 0o644)
            os.utime(destination, (_TIMESTAMP, _TIMESTAMP))
        for directory in (package, package.parent, application):
            os.chmod(directory, 0o755)
            os.utime(directory, (_TIMESTAMP, _TIMESTAMP))
        raw = root / "raw.pyz"
        zipapp.create_archive(
            application,
            target=raw,
            interpreter="/usr/bin/env python3",
            main="miniclaw.install.__main__:main",
            compressed=True,
        )
        canonical = root / "canonical.pyz"
        _canonicalize(raw, canonical)
        temporary_output = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        if temporary_output.exists() or temporary_output.is_symlink():
            raise ValueError("installer temporary output already exists")
        shutil.copyfile(canonical, temporary_output)
        os.chmod(temporary_output, 0o755)
        os.replace(temporary_output, output)
    return output


def main(argv: list[str] | None = None) -> int:
    """解析 builder CLI 并构建仓库 install package。"""
    parser = argparse.ArgumentParser(description="Build the MiniClaw installer zipapp")
    parser.add_argument("--source", type=Path, default=Path("src/miniclaw/install"))
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    build_zipapp(arguments.source, arguments.output)
    return 0


def _require_import(name: str, allowed: set[str], path: Path) -> None:
    """允许 stdlib root 或 exact miniclaw.install package。"""
    root = name.split(".", 1)[0]
    if root in allowed or name == "miniclaw.install" or name.startswith("miniclaw.install."):
        return
    raise ValueError(f"unsafe import {name} in {path.name}")


def _canonicalize(source: Path, destination: Path) -> None:
    """重写 zip entries 的顺序、权限和时间，同时保留固定 shebang。"""
    with zipfile.ZipFile(source) as archive:
        entries = tuple(
            (info.filename, archive.read(info))
            for info in sorted(archive.infolist(), key=lambda value: value.filename)
            if not info.is_dir()
        )
    with destination.open("wb") as output:
        output.write(_SHEBANG)
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, payload in entries:
                info = zipfile.ZipInfo(name, _ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


if __name__ == "__main__":
    raise SystemExit(main())
