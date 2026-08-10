"""MiniClaw fresh-install 的交互配置与 owner-only Secret 写入。"""

import getpass
import io
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TextIO

from miniclaw.bootstrap import InitResult, initialize_state, render_default_config
from miniclaw.paths import StatePaths

_FEISHU_OWNER = re.compile(r"ou_[A-Za-z0-9_-]{1,128}\Z")
_MODEL_SECRET = "MINICLAW_MODEL_API_KEY"
_FEISHU_SECRETS = ("MINICLAW_FEISHU_APP_ID", "MINICLAW_FEISHU_APP_SECRET")
_TELEGRAM_SECRET = "MINICLAW_TELEGRAM_BOT_TOKEN"
_DISCORD_SECRET = "MINICLAW_DISCORD_BOT_TOKEN"


class SetupError(ValueError):
    """表示 fresh setup 输入或目标状态不满足安全约束。"""


class _DuplexTty(io.TextIOBase):
    """独占终端 fd，并把文本读写委托给不拥有 fd 的双工 wrapper。"""

    def __init__(self) -> None:
        """创建尚未打开终端、可安全清理的空 owner。"""
        super().__init__()
        self._descriptors: list[int] = []
        self._reader_descriptor: int | None = None
        self._stream: io.TextIOWrapper | None = None
        self._closed = False

    def initialize(self) -> None:
        """打开、校验并包装 controlling TTY；失败时由调用方关闭 owner。"""
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = self._own_open(flags)
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            raise OSError("not a character device")
        reader_descriptor = self._own_dup(descriptor)
        writer_descriptor = self._own_dup(descriptor)
        self._reader_descriptor = reader_descriptor
        self._close_owned(descriptor)

        reader = io.FileIO(reader_descriptor, "rb", closefd=False)
        writer = io.FileIO(writer_descriptor, "wb", closefd=False)
        buffer = io.BufferedRWPair(reader, writer)
        stream = io.TextIOWrapper(
            buffer,
            encoding="utf-8",
            line_buffering=True,
            write_through=True,
        )
        self._stream = stream

    def _own_open(self, flags: int) -> int:
        """打开终端 fd 并立即登记为当前 owner 的资源。"""
        descriptor = os.open("/dev/tty", flags)
        self._descriptors.append(descriptor)
        return descriptor

    def _own_dup(self, descriptor: int) -> int:
        """复制 fd 并立即登记为当前 owner 的资源。"""
        duplicated = os.dup(descriptor)
        self._descriptors.append(duplicated)
        return duplicated

    def _close_owned(self, descriptor: int) -> None:
        """先撤销 ownership 再关闭一个 fd，禁止错误后的编号重试。"""
        self._descriptors.remove(descriptor)
        os.close(descriptor)

    def _require_stream(self) -> io.TextIOWrapper:
        """返回已初始化且未关闭的文本流，否则抛出标准 closed 错误。"""
        if self._closed or self._stream is None:
            raise ValueError("I/O operation on closed file")
        return self._stream

    @property
    def closed(self) -> bool:
        """返回 owner 是否已经完成幂等关闭。"""
        return self._closed

    @property
    def encoding(self) -> str:
        """返回固定 UTF-8 文本编码。"""
        return "utf-8"

    def readable(self) -> bool:
        """说明当前流支持读取。"""
        return not self._closed

    def writable(self) -> bool:
        """说明当前流支持写入。"""
        return not self._closed

    def seekable(self) -> bool:
        """说明 controlling TTY 不支持 seek。"""
        return False

    def readline(self, size: int = -1, /) -> str:
        """从委托文本流读取一行，可选限制字符数。"""
        return self._require_stream().readline(size)

    def write(self, value: str, /) -> int:
        """向委托文本流写入文本并返回字符数。"""
        return self._require_stream().write(value)

    def flush(self) -> None:
        """刷新委托文本流的待写数据。"""
        self._require_stream().flush()

    def fileno(self) -> int:
        """返回仍由当前 owner 独占的读取终端 fd。"""
        if self._closed or self._reader_descriptor is None:
            raise ValueError("I/O operation on closed file")
        return self._reader_descriptor

    def isatty(self) -> bool:
        """返回读取 fd 当前是否仍连接字符终端。"""
        return os.isatty(self.fileno())

    def __enter__(self) -> "_DuplexTty":
        """进入文本流上下文并返回当前 owner。"""
        self._require_stream()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出上下文时幂等关闭 wrapper 与全部 owned fd。"""
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        """先断开公开状态，再关闭 wrapper，最后各关闭 owned fd 一次。"""
        if self._closed:
            return
        self._closed = True
        stream = self._stream
        descriptors = tuple(self._descriptors)
        self._stream = None
        self._reader_descriptor = None
        self._descriptors.clear()
        try:
            if stream is not None:
                stream.close()
        finally:
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


@dataclass(frozen=True, slots=True)
class SetupAnswers:
    """保存 fresh setup 的非 Secret 选择。"""

    enable_feishu: bool
    feishu_owner_open_id: str | None
    enable_telegram: bool
    telegram_owner_user_id: int | None
    enable_discord: bool
    discord_owner_user_id: int | None

    @classmethod
    def defaults(cls) -> "SetupAnswers":
        """返回不启用任何 Channel 的 fresh setup 默认选择。

        Returns:
            三个平台均关闭且没有 Owner ID 的不可变答案。
        """
        return cls(False, None, False, None, False, None)


def validate_secret_value(value: str) -> str:
    """拒绝受限 dotenv 无法无损安全表达的 Secret。

    Args:
        value: 尚未写入磁盘的 Secret 文本。

    Returns:
        通过校验的原始值，不做规范化或引号变换。

    Raises:
        SetupError: 值为空、带边缘空白、引号前缀、NUL 或任一 splitlines 分隔符。
    """
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value[0] in "'\""
        or "\0" in value
        or value.splitlines() != [value]
    ):
        raise SetupError("unsafe secret value")
    return value


def write_fresh_setup(
    paths: StatePaths,
    answers: SetupAnswers,
    secrets: Mapping[str, str],
    sandbox_image: str | None,
) -> InitResult:
    """写入 fresh 配置和 Secret 后复用幂等状态初始化。

    Args:
        paths: fresh MiniClaw 状态路径。
        answers: 不包含 Secret 的 Channel 与 Owner 选择。
        secrets: 只含模型及已启用 Channel 固定环境变量名的 Secret。
        sandbox_image: 安装器校验过的 digest-pinned image；开发态可省略。

    Returns:
        完成数据库、模板和 Owner 初始化的结果。

    Raises:
        SetupError: Home、答案、Secret 或 fresh-only 条件不安全。
        ConfigError: Sandbox image 不满足安装态 digest 约束。
        BootstrapError: 后续状态模板目标不安全。
        OSError: owner-only 文件无法持久写入。
    """
    _ensure_fresh_home(paths.home)
    _validate_answers(answers)
    required_names = _required_secret_names(answers)
    if set(secrets) != set(required_names):
        raise SetupError("setup requires the exact required Secret names")
    validated_secrets = tuple(
        (name, validate_secret_value(secrets[name])) for name in required_names
    )
    config_text = render_default_config(paths, sandbox_image=sandbox_image) + _render_channels(
        answers
    )
    for path in (paths.config, paths.secrets_file):
        if path.is_symlink() or path.exists():
            raise SetupError(f"setup target already exists: {path}")

    _write_private_file(paths.config, config_text)
    try:
        _write_private_file(
            paths.secrets_file,
            "".join(f"{name}={value}\n" for name, value in validated_secrets),
        )
    except BaseException:
        paths.config.unlink(missing_ok=True)
        raise
    return initialize_state(paths, sandbox_image=sandbox_image)


def run_interactive_setup(
    paths: StatePaths,
    *,
    sandbox_image: str | None = None,
) -> InitResult:
    """从 `/dev/tty` 和 getpass 收集 fresh setup 输入并安全写入。

    Args:
        paths: fresh MiniClaw 状态路径。
        sandbox_image: 安装器校验过的 digest-pinned image；开发态可省略。

    Returns:
        完成持久化的状态初始化结果。

    Raises:
        SetupError: TTY 不可用、回答无效或 fresh setup 安全校验失败。
        BootstrapError: 后续状态模板目标不安全。
        ConfigError: Sandbox image 不满足安装态 digest 约束。
        OSError: owner-only 文件无法持久写入。
    """
    try:
        with _open_tty() as tty:
            enable_feishu = _ask_yes_no(tty, "Enable Feishu? [y/N]: ")
            feishu_owner = (
                _ask_text(tty, "Feishu Owner open_id: ") if enable_feishu else None
            )
            enable_telegram = _ask_yes_no(tty, "Enable Telegram? [y/N]: ")
            telegram_owner = (
                _ask_owner_user_id(tty, "Telegram Owner user ID: ")
                if enable_telegram
                else None
            )
            enable_discord = _ask_yes_no(tty, "Enable Discord? [y/N]: ")
            discord_owner = (
                _ask_owner_user_id(tty, "Discord Owner user ID: ")
                if enable_discord
                else None
            )
            answers = SetupAnswers(
                enable_feishu,
                feishu_owner,
                enable_telegram,
                telegram_owner,
                enable_discord,
                discord_owner,
            )
            secrets = {_MODEL_SECRET: getpass.getpass("Model API key: ", stream=tty)}
            if enable_feishu:
                secrets[_FEISHU_SECRETS[0]] = getpass.getpass(
                    "Feishu App ID: ", stream=tty
                )
                secrets[_FEISHU_SECRETS[1]] = getpass.getpass(
                    "Feishu App Secret: ", stream=tty
                )
            if enable_telegram:
                secrets[_TELEGRAM_SECRET] = getpass.getpass(
                    "Telegram Bot token: ", stream=tty
                )
            if enable_discord:
                secrets[_DISCORD_SECRET] = getpass.getpass(
                    "Discord Bot token: ", stream=tty
                )
    except (EOFError, StopIteration) as error:
        raise SetupError("interactive setup input ended unexpectedly") from error
    return write_fresh_setup(paths, answers, secrets, sandbox_image)


def _ensure_fresh_home(path: Path) -> None:
    """创建或校验当前进程拥有的非 symlink 0700 state home。

    Args:
        path: 需要承载 fresh setup 的状态根。

    Raises:
        SetupError: 路径是 symlink、非目录、非当前 owner 或权限不是 0700。
        OSError: 目录无法创建或检查。
    """
    if path.is_symlink():
        raise SetupError("state home must not be a symbolic link")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError as error:
        raise SetupError("state home must be a directory") from error
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise SetupError("state home must be a directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise SetupError("state home must be owner-owned with mode 0700")


def _validate_answers(answers: SetupAnswers) -> None:
    """校验 Channel 开关与平台 Owner ID 的组合。

    Args:
        answers: 调用方提供的 typed setup answers。

    Raises:
        SetupError: 开关不是 bool、启用项缺 ID、禁用项携带 ID 或 ID 越界。
    """
    flags = (answers.enable_feishu, answers.enable_telegram, answers.enable_discord)
    if any(type(enabled) is not bool for enabled in flags):
        raise SetupError("Channel selections must be boolean")
    if answers.enable_feishu:
        if (
            not isinstance(answers.feishu_owner_open_id, str)
            or _FEISHU_OWNER.fullmatch(answers.feishu_owner_open_id) is None
        ):
            raise SetupError("Feishu Owner open_id is invalid")
    elif answers.feishu_owner_open_id is not None:
        raise SetupError("Feishu Owner open_id must be omitted when disabled")
    _validate_integer_owner(
        "Telegram",
        answers.enable_telegram,
        answers.telegram_owner_user_id,
        2**63 - 1,
    )
    _validate_integer_owner(
        "Discord",
        answers.enable_discord,
        answers.discord_owner_user_id,
        2**64 - 1,
    )


def _validate_integer_owner(
    channel: str,
    enabled: bool,
    owner_id: int | None,
    maximum: int,
) -> None:
    """校验整数型 Channel Owner ID。

    Args:
        channel: 用于稳定错误定位的平台名。
        enabled: 当前平台是否启用。
        owner_id: 平台 Owner 的整数 ID。
        maximum: 平台协议允许的最大值。

    Raises:
        SetupError: 启用项 ID 不是正整数或禁用项仍携带 ID。
    """
    if enabled:
        if type(owner_id) is not int or not 1 <= owner_id <= maximum:
            raise SetupError(f"{channel} Owner user ID is invalid")
    elif owner_id is not None:
        raise SetupError(f"{channel} Owner user ID must be omitted when disabled")


def _required_secret_names(answers: SetupAnswers) -> tuple[str, ...]:
    """按稳定顺序返回当前选择需要的固定 Secret 环境变量名。

    Args:
        answers: 已完成安全校验的 setup answers。

    Returns:
        模型 Secret 加已启用 Channel Secret 的精确名称元组。
    """
    names = [_MODEL_SECRET]
    if answers.enable_feishu:
        names.extend(_FEISHU_SECRETS)
    if answers.enable_telegram:
        names.append(_TELEGRAM_SECRET)
    if answers.enable_discord:
        names.append(_DISCORD_SECRET)
    return tuple(names)


def _render_channels(answers: SetupAnswers) -> str:
    """只渲染明确启用的 Channel TOML tables。

    Args:
        answers: 已完成安全校验的 setup answers。

    Returns:
        使用固定 env 名与 Owner allowlist 的 TOML 后缀。
    """
    sections: list[str] = []
    if answers.enable_feishu:
        owner = json.dumps(answers.feishu_owner_open_id)
        sections.append(
            "\n[channels.feishu]\n"
            "enabled = true\n"
            'app_id_env = "MINICLAW_FEISHU_APP_ID"\n'
            'app_secret_env = "MINICLAW_FEISHU_APP_SECRET"\n'
            f"owner_open_id = {owner}\n"
            f"allowed_open_ids = [{owner}]\n"
        )
    if answers.enable_telegram:
        owner = answers.telegram_owner_user_id
        sections.append(
            "\n[channels.telegram]\n"
            "enabled = true\n"
            'bot_token_env = "MINICLAW_TELEGRAM_BOT_TOKEN"\n'
            f"owner_user_id = {owner}\n"
            f"allowed_user_ids = [{owner}]\n"
        )
    if answers.enable_discord:
        owner = answers.discord_owner_user_id
        sections.append(
            "\n[channels.discord]\n"
            "enabled = true\n"
            'bot_token_env = "MINICLAW_DISCORD_BOT_TOKEN"\n'
            f"owner_user_id = {owner}\n"
            f"allowed_user_ids = [{owner}]\n"
        )
    return "".join(sections)


def _write_private_file(path: Path, content: str) -> None:
    """以 O_EXCL、0600 和 fsync 持久写入一个 fresh UTF-8 文件。

    Args:
        path: 必须尚不存在的目标文件。
        content: 完整 UTF-8 文件内容。

    Raises:
        SetupError: 路径已存在，禁止覆盖或跟随 symlink。
        OSError: 文件创建、写入、chmod 或 fsync 失败。
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise SetupError(f"setup target already exists: {path}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _open_tty() -> TextIO:
    """打开控制终端用于读取非 Secret setup 回答。

    Returns:
        可同时读写、逐行刷新的 `/dev/tty` 文本流。

    Raises:
        SetupError: 当前进程没有可用控制终端。
    """
    stream = _DuplexTty()
    try:
        stream.initialize()
        return stream
    except BaseException as error:
        try:
            stream.close()
        except BaseException:
            pass
        if not isinstance(error, Exception):
            raise
        raise SetupError("interactive terminal is unavailable") from None


def _ask_yes_no(tty: TextIO, prompt: str) -> bool:
    """从控制终端读取一个严格 yes/no 回答。

    Args:
        tty: 已打开的控制终端。
        prompt: 不含 Secret 的提示文本。

    Returns:
        `y/yes` 为 True，空值与 `n/no` 为 False。

    Raises:
        SetupError: 输入不是受支持的 yes/no 表达。
    """
    value = _ask_text(tty, prompt).casefold()
    if value in ("", "n", "no"):
        return False
    if value in ("y", "yes"):
        return True
    raise SetupError("answer must be yes or no")


def _ask_text(tty: TextIO, prompt: str) -> str:
    """从控制终端读取并修剪一个非 Secret 文本回答。

    Args:
        tty: 已打开的控制终端。
        prompt: 不含 Secret 的提示文本。

    Returns:
        去除首尾空白的单行回答。
    """
    tty.write(prompt)
    tty.flush()
    return tty.readline().strip()


def _ask_owner_user_id(tty: TextIO, prompt: str) -> int:
    """从控制终端读取整数型 Owner user ID。

    Args:
        tty: 已打开的控制终端。
        prompt: 不含 Secret 的提示文本。

    Returns:
        尚待平台范围校验的整数。

    Raises:
        SetupError: 回答无法按十进制整数解析。
    """
    try:
        return int(_ask_text(tty, prompt))
    except ValueError as error:
        raise SetupError("Owner user ID must be an integer") from error
