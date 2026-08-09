"""Desktop 一键启动脚本的离线行为测试。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import TracebackType

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_SOURCE = REPOSITORY_ROOT / "start-desktop.command"


class LauncherSandbox:
    """在临时伪仓库中执行真实 launcher，并记录外部命令边界。"""

    def __init__(self, *, initialized: bool, dependencies: bool) -> None:
        """保存初始化和依赖状态，实际目录在进入上下文时创建。

        Args:
            initialized: 是否创建已有配置与私密 Secret 文件。
            dependencies: 是否创建 Python 和 Node 依赖占位目录。
        """
        self._initialized = initialized
        self._dependencies = dependencies
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.state_home = self.root / "state"
        self.log = self.root / "commands.log"
        self.launcher = self.root / "start-desktop.command"

    def __enter__(self) -> LauncherSandbox:
        """创建伪仓库、状态和最小外部命令。

        Returns:
            已准备好的当前 sandbox。
        """
        for path in (
            self.root / "fake-bin",
            self.root / "elsewhere",
            self.root / "tui",
            self.root / "desktop",
            self.root / ".venv" / "bin",
            self.state_home,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if LAUNCHER_SOURCE.is_file():
            shutil.copy2(LAUNCHER_SOURCE, self.launcher)
        if self._dependencies:
            (self.root / "tui" / "node_modules").mkdir()
            (self.root / "desktop" / "node_modules").mkdir()
        if self._initialized:
            (self.state_home / "config.toml").write_text("[provider]\n", encoding="utf-8")
            secret = self.state_home / "secrets.env"
            secret.write_text("MINICLAW_MODEL_API_KEY=SECRET_SENTINEL\n", encoding="utf-8")
            secret.chmod(0o600)
        self._write_executable("uname", "print -r -- Darwin\n")
        self._write_executable("uv", "exit 0\n")
        self._write_executable("node", "exit 0\n")
        self._write_executable(
            "corepack",
            """
if [[ "$*" == "pnpm --dir desktop dev" ]]; then
  print -r -- \
    "corepack $* home=${MINICLAW_HOME:-} env=${MINICLAW_ENV_FILE:-}" \
    >> "$MINICLAW_TEST_LOG"
else
  print -r -- "corepack $*" >> "$MINICLAW_TEST_LOG"
fi
""",
        )
        self._write_executable(
            ".venv/bin/python",
            'print -r -- "${MINICLAW_HOME:-$HOME/.miniclaw}"\n',
            root_relative=True,
        )
        self._write_executable(
            ".venv/bin/miniclaw",
            'print -r -- "miniclaw $*" >> "$MINICLAW_TEST_LOG"\n',
            root_relative=True,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """释放临时伪仓库。

        Args:
            exc_type: 上下文异常类型。
            exc_value: 上下文异常实例。
            traceback: 上下文异常回溯。
        """
        del exc_type, exc_value, traceback
        self._temporary.cleanup()

    def run(self) -> subprocess.CompletedProcess[str]:
        """从无关工作目录运行 launcher。

        Returns:
            捕获文本输出的完成进程。
        """
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("MINICLAW_")
        }
        environment.update(
            {
                "HOME": str(self.root / "home"),
                "MINICLAW_HOME": str(self.state_home),
                "MINICLAW_TEST_LOG": str(self.log),
                "PATH": f"{self.root / 'fake-bin'}:/usr/bin:/bin",
            }
        )
        return subprocess.run(
            ("/bin/zsh", str(self.launcher)),
            cwd=self.root / "elsewhere",
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def log_lines(self) -> list[str]:
        """返回 fake external commands 的稳定调用顺序。

        Returns:
            不包含空行的调用日志。
        """
        if not self.log.is_file():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def _write_executable(
        self,
        relative: str,
        body: str,
        *,
        root_relative: bool = False,
    ) -> None:
        """写入一个只执行测试固定行为的 zsh executable。

        Args:
            relative: 相对 fake-bin 或伪仓库根的路径。
            body: executable 的 zsh 正文。
            root_relative: 为真时相对伪仓库根写入。
        """
        base = self.root if root_relative else self.root / "fake-bin"
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"#!/bin/zsh\nset -eu\n{body}", encoding="utf-8")
        target.chmod(0o755)


class DesktopLauncherTest(unittest.TestCase):
    """验证 Desktop 一键入口只执行受控且稳定的启动步骤。"""

    def test_existing_state_builds_shared_client_and_starts_desktop_once(self) -> None:
        """已有状态只幂等初始化、构建共享 Client 并启动一次 Desktop。"""
        with LauncherSandbox(initialized=True, dependencies=True) as sandbox:
            result = sandbox.run()

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                sandbox.log_lines(),
                [
                    f"miniclaw init --home {sandbox.state_home}",
                    "corepack pnpm --dir tui build",
                    (
                        "corepack pnpm --dir desktop dev "
                        f"home={sandbox.state_home} env={sandbox.state_home / 'secrets.env'}"
                    ),
                ],
            )


if __name__ == "__main__":
    unittest.main()
