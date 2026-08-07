"""MiniClaw 本地状态目录的解析与派生。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class PathConfigurationError(ValueError):
    """表示用户提供的状态路径不满足安全约束。"""


@dataclass(frozen=True, slots=True)
class StatePaths:
    """保存一个 MiniClaw 实例使用的全部本地状态路径。"""

    home: Path
    config: Path
    database: Path
    soul: Path
    user: Path
    memory_file: Path
    memory_dir: Path
    prompts: Path
    prompt_versions: Path
    skills: Path
    skill_versions: Path
    evals: Path
    eval_baseline: Path
    eval_failures: Path
    workspace: Path
    logs: Path
    run: Path

    @property
    def directories(self) -> tuple[Path, ...]:
        """返回初始化时需要创建的目录，父目录排在子目录之前。"""
        return (
            self.home,
            self.memory_dir,
            self.prompts,
            self.prompt_versions,
            self.skills,
            self.skill_versions,
            self.evals,
            self.eval_baseline,
            self.eval_failures,
            self.workspace,
            self.logs,
            self.run,
        )


def resolve_home(
    value: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """按显式值、环境变量和默认值的顺序解析绝对状态根目录。

    Args:
        value: CLI 或调用方显式传入的状态根目录。
        environ: 用于读取 ``MINICLAW_HOME`` 的环境；默认使用当前进程环境。

    Returns:
        展开用户目录并规范化后的绝对路径。

    Raises:
        PathConfigurationError: 选择出的路径为空或不是绝对路径。
    """
    source = os.environ if environ is None else environ
    selected: str | Path = (
        value
        if value is not None
        else source.get("MINICLAW_HOME") or Path.home() / ".miniclaw"
    )
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        raise PathConfigurationError("MiniClaw home must be an absolute path")
    return candidate.resolve(strict=False)


def build_state_paths(home: Path) -> StatePaths:
    """从一个绝对状态根目录派生所有固定文件和目录。

    Args:
        home: 已选择的 MiniClaw 状态根目录。

    Returns:
        不访问文件系统的不可变路径集合。

    Raises:
        PathConfigurationError: ``home`` 不是绝对路径。
    """
    resolved_home = resolve_home(home, {})
    memory_dir = resolved_home / "memory"
    prompts = resolved_home / "prompts"
    skills = resolved_home / "skills"
    evals = resolved_home / "evals"
    return StatePaths(
        home=resolved_home,
        config=resolved_home / "config.toml",
        database=resolved_home / "miniclaw.db",
        soul=resolved_home / "SOUL.md",
        user=resolved_home / "USER.md",
        memory_file=resolved_home / "MEMORY.md",
        memory_dir=memory_dir,
        prompts=prompts,
        prompt_versions=prompts / "versions",
        skills=skills,
        skill_versions=skills / "versions",
        evals=evals,
        eval_baseline=evals / "baseline",
        eval_failures=evals / "failures",
        workspace=resolved_home / "workspace",
        logs=resolved_home / "logs",
        run=resolved_home / "run",
    )
