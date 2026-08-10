"""Browser Worker 与系统 Chromium 的无副作用本地发现。"""

import os
import shutil
from pathlib import Path


def browser_worker_root() -> Path:
    """返回 source checkout 中 Browser Worker 的固定根目录。"""
    return Path(__file__).resolve().parents[3] / "browser-worker"


def find_chromium() -> Path | None:
    """只从 PATH 和常见系统位置发现 Chromium，不启动应用。"""
    for command in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if found := shutil.which(command):
            return Path(found).resolve()
    for candidate in (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None
