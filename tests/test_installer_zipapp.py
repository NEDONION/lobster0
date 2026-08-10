"""验证 stdlib-only installer zipapp 构建边界。"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path("scripts/build_installer_zipapp.py")


def _load_builder() -> object:
    """从仓库脚本路径加载 builder 供离线单元测试调用。"""
    spec = importlib.util.spec_from_file_location("build_installer_zipapp", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerZipappTests(unittest.TestCase):
    """覆盖 import allowlist、archive 内容、可复现性和隔离 help。"""

    def test_ast_boundary_rejects_non_stdlib_import(self) -> None:
        """任何第三方 absolute import 都必须在构建前失败。"""
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "install"
            source.mkdir()
            (source / "__init__.py").write_text("import httpx\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "httpx"):
                builder.validate_imports(source)

    def test_ast_boundary_accepts_stdlib_and_relative_install_imports(self) -> None:
        """stdlib 与包内 relative import 是唯一允许的依赖。"""
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "install"
            source.mkdir()
            (source / "__init__.py").write_text(
                "import json\nimport os\nimport sys\nprint(sys.argv[0])\n"
                "print(getattr(os, 'O_NOFOLLOW', 0))\n"
                "from . import models\nfrom miniclaw.install import models\n",
                encoding="utf-8",
            )
            (source / "models.py").write_text("from pathlib import Path\n", encoding="utf-8")
            builder.validate_imports(source)

    def test_ast_boundary_rejects_relative_import_that_escapes_install_package(self) -> None:
        """两级 relative import 不得逃出 bundled miniclaw.install package。"""
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "install"
            source.mkdir()
            (source / "__init__.py").write_text(
                "from ..agent import AgentRuntime\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "relative"):
                builder.validate_imports(source)

    def test_ast_boundary_rejects_dynamic_import_and_code_execution_aliases(self) -> None:
        """动态 import 与 builtin code execution 的简单 alias 绕过都必须拒绝。"""
        cases = (
            ("__import__('httpx')\n", "__import__"),
            ("import importlib as loader\nloader.import_module('httpx')\n", "import_module"),
            (
                "from importlib import import_module as load\nload('httpx')\n",
                "import_module",
            ),
            ("runner = eval\nrunner('40 + 2')\n", "eval"),
            ("import builtins as b\ngetattr(b, 'exec')('pass')\n", "exec"),
            ("import builtins as b\nalias = b\nalias.eval('40 + 2')\n", "eval"),
            (
                "from builtins import compile as build\nbuild('pass', 'x', 'exec')\n",
                "compile",
            ),
        )
        builder = _load_builder()
        for payload, detail in cases:
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "install"
                source.mkdir()
                (source / "__init__.py").write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, detail):
                    builder.validate_imports(source)

    def test_ast_boundary_rejects_dynamic_module_and_global_namespace_access(self) -> None:
        """importlib/builtins alias 与 global namespace 均必须 fail closed。"""
        cases = (
            ("import importlib as loader\nloader\n", "importlib"),
            ("import builtins as b\nb\n", "builtins"),
            ("globals()['__builtins__']\n", "__builtins__"),
            ("globals()['__built' + 'ins__']\n", "globals"),
            (
                "import importlib\n"
                "getattr(importlib, 'import_' + 'module')('httpx')\n",
                "importlib",
            ),
        )
        builder = _load_builder()
        for payload, detail in cases:
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "install"
                source.mkdir()
                (source / "__init__.py").write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, detail):
                    builder.validate_imports(source)

    def test_ast_boundary_rejects_dynamic_namespace_primitives_and_literal_join(self) -> None:
        """namespace 原语、alias/getattr 与 literal join 组合均必须 fail closed。"""
        cases = (
            ("import sys\nsys.modules\n", "modules"),
            ("lookup = globals\nlookup()\n", "globals"),
            ("lookup = vars\nlookup()\n", "vars"),
            ("lookup = locals\nlookup()\n", "locals"),
            ("import sys as platform\nplatform.__dict__\n", "__dict__"),
            ("import sys as platform\ngetattr(platform, '__dict__')\n", "__dict__"),
            ("import sys as platform\ngetattr(platform, 'modules')\n", "modules"),
            (
                "import sys\n"
                "loader = sys.modules[''.join(('import', 'lib'))]\n"
                "getattr(loader, ''.join(('import_', 'module')))('httpx')\n",
                "modules",
            ),
            (
                "import sys\n"
                "namespace = sys.modules[''.join(('built', 'ins'))]\n"
                "getattr(namespace, ''.join(('__im', 'port__')))('httpx')\n",
                "modules",
            ),
        )
        builder = _load_builder()
        for payload, detail in cases:
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "install"
                source.mkdir()
                (source / "__init__.py").write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, detail):
                    builder.validate_imports(source)

    def test_ast_boundary_rejects_computed_and_escaped_getattr(self) -> None:
        """computed attribute name 与 escaped builtin getattr 均必须 fail closed。"""
        cases = (
            "import sys\ngetattr(sys, 'mod' + 'ules')\n",
            "import sys\ngetattr(sys, ''.join(('mod', 'ules')))\n",
            "import sys\nlookup = getattr\nlookup(sys, 'argv')\n",
            "def consume(value):\n    return value\nconsume(getattr)\n",
            "import sys\ndef use(getattr):\n    return getattr(sys, 'argv')\n",
            "import sys\ndef getattr(value, name):\n    return None\ngetattr(sys, 'argv')\n",
            "import sys\nfrom operator import attrgetter as getattr\ngetattr(sys, 'argv')\n",
        )
        builder = _load_builder()
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "install"
                source.mkdir()
                (source / "__init__.py").write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "getattr"):
                    builder.validate_imports(source)

    def test_ast_boundary_rejects_dunder_escape_and_hidden_getattr_bindings(self) -> None:
        """通用 dunder 与非 Name binding 不得绕过 closed-world reflection boundary。"""
        cases = (
            (
                "import os\n"
                "os.__builtins__['__im' + 'port__']('httpx')\n",
                "__builtins__",
            ),
            (
                "def marker():\n    pass\n"
                "marker.__globals__['__built' + 'ins__']['__im' + 'port__']('httpx')\n",
                "__globals__",
            ),
            (
                "import os\ntry:\n    raise RuntimeError\n"
                "except RuntimeError as getattr:\n"
                "    getattr(os, 'O_NOFOLLOW', 0)\n",
                "getattr",
            ),
            (
                "import os\nmatch object():\n    case getattr:\n"
                "        getattr(os, 'O_NOFOLLOW', 0)\n",
                "getattr",
            ),
        )
        builder = _load_builder()
        for payload, detail in cases:
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "install"
                source.mkdir()
                (source / "__init__.py").write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, detail):
                    builder.validate_imports(source)

    def test_ast_boundary_rejects_unaudited_constant_getattr_name(self) -> None:
        """exact string 仍须来自真实 install package 审计形成的字段 allowlist。"""
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "install"
            source.mkdir()
            (source / "__init__.py").write_text(
                "import sys\ngetattr(sys, 'argv')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "getattr"):
                builder.validate_imports(source)

    def test_ast_boundary_accepts_only_audited_dunder_attributes(self) -> None:
        """真实 install package 所需的四个 bounded dunder Attribute 必须继续可构建。"""
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "install"
            source.mkdir()
            (source / "__init__.py").write_text(
                "class Example:\n    pass\n"
                "instance = object.__new__(Example)\n"
                "object.__setattr__(instance, 'value', 1)\n"
                "instance.__post_init__()\n"
                "super().__init__()\n",
                encoding="utf-8",
            )
            builder.validate_imports(source)

    def test_archive_contains_only_install_package_with_fixed_timestamps(self) -> None:
        """pyz 不得携带主包其他模块、cache 或本机时间。"""
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "installer.pyz"
            builder.build_zipapp(Path("src/miniclaw/install"), output)
            with zipfile.ZipFile(output) as archive:
                infos = archive.infolist()
                names = {info.filename for info in infos}
            self.assertIn("miniclaw/install/orchestrator.py", names)
            self.assertIn("__main__.py", names)
            self.assertFalse(any("__pycache__" in name for name in names))
            self.assertEqual(
                {
                    name
                    for name in names
                    if name.startswith("miniclaw/") and not name.startswith("miniclaw/install/")
                },
                {"miniclaw/__init__.py"},
            )
            self.assertEqual({info.date_time for info in infos}, {(1980, 1, 1, 0, 0, 0)})

    def test_build_is_byte_reproducible(self) -> None:
        """相同 source 两次构建必须得到完全相同的 SHA-256。"""
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.pyz"
            second = Path(temporary) / "second.pyz"
            builder.build_zipapp(Path("src/miniclaw/install"), first)
            builder.build_zipapp(Path("src/miniclaw/install"), second)
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).digest(),
                hashlib.sha256(second.read_bytes()).digest(),
            )

    def test_isolated_python_help_works_from_empty_directory(self) -> None:
        """无 site packages、无 checkout cwd 时 pyz 仍能展示 CLI help。"""
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "installer.pyz"
            empty = root / "empty"
            empty.mkdir()
            builder.build_zipapp(Path("src/miniclaw/install"), output)
            completed = subprocess.run(
                ("python3", "-I", str(output), "--help"),
                cwd=empty,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
            self.assertIn(b"MiniClaw", completed.stdout)


if __name__ == "__main__":
    unittest.main()
