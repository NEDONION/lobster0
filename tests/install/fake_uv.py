#!/usr/bin/env python3
"""供 Runtime subprocess 集成测试使用的离线 uv fake。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_FAKE_PYTHON = b"""#!/usr/bin/env python3
import json
import sys

if sys.argv[1:] == [\"-I\", \"-m\", \"miniclaw\", \"--version\"]:
    print(\"miniclaw 0.7.0\")
elif sys.argv[1:] == [\"-I\", \"-m\", \"miniclaw\", \"install-smoke\", \"--json\"]:
    print(json.dumps({\"status\": \"ok\", \"version\": \"0.7.0\"}, separators=(\",\", \":\")))
else:
    raise SystemExit(2)
"""


def main(argv: list[str] | None = None) -> int:
    """模拟 `uv venv` 和两条 `uv pip install`，不访问网络。"""
    values = sys.argv[1:] if argv is None else argv
    if len(values) == 4 and values[:3] == ["venv", "--python", "3.12"]:
        python = Path(values[3]) / "bin" / "python"
        python.parent.mkdir(parents=True)
        descriptor = os.open(python, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            os.write(descriptor, _FAKE_PYTHON)
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return 0
    if len(values) >= 5 and values[:2] == ["pip", "install"] and "--python" in values:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
