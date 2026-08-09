#!/usr/bin/env python3
"""供 Runtime subprocess 集成测试使用的离线 uv fake。"""

from __future__ import annotations

import sys
from pathlib import Path

_MINICLAW_MAIN = b"""from __future__ import annotations

import json
import sys

if sys.argv[1:] == [\"--version\"]:
    print(\"miniclaw 0.7.0\")
elif sys.argv[1:] == [\"install-smoke\", \"--json\"]:
    print(json.dumps({\"status\": \"ok\", \"version\": \"0.7.0\"}, separators=(\",\", \":\")))
else:
    raise SystemExit(2)
"""


def main(argv: list[str] | None = None) -> int:
    """模拟 `uv venv` 和两条 `uv pip install`，不访问网络。"""
    values = sys.argv[1:] if argv is None else argv
    if (
        len(values) == 6
        and values[:3] == ["venv", "--relocatable", "--python"]
        and values[4] == "--no-python-downloads"
    ):
        internal = Path(values[3])
        venv = Path(values[5])
        python = venv / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.symlink_to(internal)
        (python.parent / "python3").symlink_to("python")
        (python.parent / "python3.12").symlink_to("python")
        config = venv / "pyvenv.cfg"
        config.write_text(
            f"home = {internal.parent}\nversion_info = 3.12.11\nrelocatable = true\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        return 0
    if len(values) >= 5 and values[:2] == ["pip", "install"] and "--python" in values:
        if "--no-deps" in values:
            python = Path(values[values.index("--python") + 1])
            package = python.parents[1] / "lib" / "python3.12" / "site-packages" / "miniclaw"
            package.mkdir(parents=True)
            (package / "__init__.py").write_bytes(b"")
            (package / "__main__.py").write_bytes(_MINICLAW_MAIN)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
