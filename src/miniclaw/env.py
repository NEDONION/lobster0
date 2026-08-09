"""以非执行方式加载 MiniClaw 本地 ``.env`` 凭据文件。"""

import os
import re
import stat
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from miniclaw.paths import StatePaths

_KEY_PATTERN = re.compile(r"[A-Z_][A-Z0-9_]*\Z")


class DotEnvError(ValueError):
    """表示本地 ``.env`` 文件格式、权限或读取状态不安全。"""


def resolve_dotenv_path(
    paths: StatePaths,
    environ: Mapping[str, str],
    *,
    cwd: Path | None = None,
) -> Path:
    """解析显式安装态 Secret 文件，否则保持 cwd/.env 开发语义。

    Args:
        paths: 当前 MiniClaw 状态路径；用于保持所有运行时入口的统一调用契约。
        environ: 用于读取 ``MINICLAW_ENV_FILE`` 的环境变量。
        cwd: 开发态 ``.env`` 所在目录；默认使用当前工作目录。

    Returns:
        需要按 owner-only 规则加载的 dotenv 文件路径。

    Raises:
        DotEnvError: 显式安装态路径不是绝对路径。
    """
    del paths
    selected = environ.get("MINICLAW_ENV_FILE", "").strip()
    if not selected:
        return (Path.cwd() if cwd is None else cwd) / ".env"
    candidate = Path(selected).expanduser()
    if not candidate.is_absolute():
        raise DotEnvError("MINICLAW_ENV_FILE must be absolute path")
    return candidate.resolve(strict=False)


def load_dotenv(
    path: Path,
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """加载尚未存在于进程环境中的本地变量。

    Args:
        path: 要读取的固定 ``.env`` 文件路径。
        environ: 接收变量的映射；省略时使用当前进程环境。

    Returns:
        按文件顺序返回实际写入环境的变量名；文件不存在时返回空元组。

    Raises:
        DotEnvError: 文件不私密、无法读取、编码错误或任一行格式无效。
    """
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise DotEnvError(f"cannot inspect local environment file {path}") from error
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
        raise DotEnvError(f"local environment file must be a private regular file: {path}")

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise DotEnvError(f"cannot read local environment file {path}") from error

    parsed = tuple(
        item
        for line_number, line in enumerate(lines, start=1)
        if (item := _parse_line(path, line_number, line)) is not None
    )
    target = os.environ if environ is None else environ
    loaded = tuple(key for key, _ in parsed if key not in target)
    target.update((key, value) for key, value in parsed if key not in target)
    return loaded


def _parse_line(path: Path, line_number: int, line: str) -> tuple[str, str] | None:
    """解析一行受限 dotenv 语法且不在异常中回显原文。

    Args:
        path: 用于安全定位错误的文件路径。
        line_number: 从一开始的当前行号。
        line: 尚未执行或展开的原始文本。

    Returns:
        键值对；空行或整行注释返回 ``None``。

    Raises:
        DotEnvError: 行包含 NUL、缺少赋值、键名非法或引号不配对。
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "\0" in line or "=" not in stripped:
        raise DotEnvError(f"invalid local environment entry at {path}:{line_number}")

    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    value = raw_value.strip()
    if _KEY_PATTERN.fullmatch(key) is None:
        raise DotEnvError(f"invalid local environment entry at {path}:{line_number}")
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or not value.endswith(quote):
            raise DotEnvError(f"invalid local environment entry at {path}:{line_number}")
        value = value[1:-1]
    return key, value
