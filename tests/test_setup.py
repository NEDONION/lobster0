"""Lobster0 fresh setup 的安全配置与交互测试。"""

import contextlib
import io
import os
import select
import signal
import stat
import sys
import tempfile
import time
import unittest
import warnings
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from unittest import mock

from lobster0 import setup as setup_module
from lobster0.config import load_config
from lobster0.env import load_dotenv
from lobster0.paths import StatePaths, build_state_paths
from lobster0.setup import (
    SetupAnswers,
    SetupError,
    run_interactive_setup,
    validate_secret_value,
    write_fresh_setup,
)

PINNED_IMAGE = "ghcr.io/nedonion/lobster0-sandbox@sha256:" + "a" * 64
_PTY_SECRETS = {
    "zero": ("sentinel-zero-model",),
    "all": (
        "sentinel-all-model",
        "sentinel-all-app-id",
        "sentinel-all-app-secret",
        "sentinel-all-telegram",
        "sentinel-all-discord",
    ),
}


def _run_setup_child(home: str, name: str) -> None:
    """在 fresh interpreter 的 controlling TTY 上运行一个 setup 测试场景。"""
    signal.alarm(10)
    values = iter(_PTY_SECRETS[name])

    def hidden_input(prompt: str, stream: io.TextIOBase | None = None) -> str:
        assert stream is not None and os.isatty(stream.fileno())
        stream.write(prompt)
        stream.flush()
        return next(values)

    with mock.patch("lobster0.setup.getpass.getpass", side_effect=hidden_input):
        run_interactive_setup(build_state_paths(Path(home)), sandbox_image=PINNED_IMAGE)


class _FakeTty:
    """提供独立读写缓冲的最小双工 TTY fake。"""

    def __init__(self, responses: list[str]) -> None:
        """保存依次返回的非 Secret 回答。"""
        self._responses = iter(responses)
        self.output = io.StringIO()

    def __enter__(self) -> "_FakeTty":
        """把 fake 作为 context manager 返回。"""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """保持测试缓冲可读，不吞掉异常。"""
        del exc_type, exc_value, traceback

    def readline(self) -> str:
        """返回下一条预置回答。"""
        return next(self._responses)

    def write(self, value: str) -> int:
        """记录非 Secret 提示文本。"""
        return self.output.write(value)

    def flush(self) -> None:
        """匹配真实 TTY 的 flush 接口。"""


class SetupTest(unittest.TestCase):
    """验证 setup 只写 fresh、私密且可加载的本地状态。"""

    def setUp(self) -> None:
        """为每个测试准备尚不存在的状态根。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.paths = build_state_paths(self.root / "state")

    def _run_setup_in_controlling_tty(
        self,
        paths: StatePaths,
        name: str,
        responses: bytes,
    ) -> tuple[int, str]:
        """在 forkpty 子进程中运行真实 setup 并返回退出码与终端输出。"""
        command = (
            sys.executable,
            "-c",
            "import sys; from tests.test_setup import _run_setup_child; "
            "_run_setup_child(sys.argv[1], sys.argv[2])",
            str(paths.home),
            name,
        )
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"This process .* forkpty\(\) may lead to deadlocks",
                category=DeprecationWarning,
            )
            process, master = os.forkpty()
        if process == 0:
            try:
                os.execv(command[0], command)
            except OSError:
                os._exit(127)

        output = bytearray()
        try:
            os.write(master, responses)
            os.set_blocking(master, False)
            deadline = time.monotonic() + 15
            status: int | None = None
            while status is None:
                readable, _, _ = select.select((master,), (), (), 0.1)
                if readable:
                    try:
                        payload = os.read(master, 4096)
                    except OSError:
                        payload = b""
                    output.extend(payload)
                finished, child_status = os.waitpid(process, os.WNOHANG)
                if finished:
                    status = child_status
                elif time.monotonic() >= deadline:
                    os.kill(process, signal.SIGKILL)
                    _, status = os.waitpid(process, 0)
            while True:
                try:
                    payload = os.read(master, 4096)
                except OSError:
                    break
                if not payload:
                    break
                output.extend(payload)
        finally:
            os.close(master)
        assert status is not None
        return os.waitstatus_to_exitcode(status), output.decode("utf-8", errors="replace")

    def test_fresh_setup_writes_private_config_and_secrets_without_echo(self) -> None:
        """启用飞书时应写固定 env 名、私密文件且配置不含 Secret。"""
        answers = SetupAnswers(
            enable_feishu=True,
            feishu_owner_open_id="ou_owner",
            enable_telegram=False,
            telegram_owner_user_id=None,
            enable_discord=False,
            discord_owner_user_id=None,
        )
        secrets = {
            "LOBSTER0_MODEL_API_KEY": "sentinel-model-key",
            "LOBSTER0_FEISHU_APP_ID": "cli_app",
            "LOBSTER0_FEISHU_APP_SECRET": "sentinel-app-secret",
        }

        with mock.patch("lobster0.setup.os.fsync", wraps=os.fsync) as fsync:
            result = write_fresh_setup(
                self.paths,
                answers,
                secrets,
                sandbox_image=PINNED_IMAGE,
            )

        self.assertEqual(stat.S_IMODE(self.paths.home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.paths.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.secrets_file.stat().st_mode), 0o600)
        self.assertEqual(fsync.call_count, 2)
        config = load_config(self.paths, {})
        self.assertTrue(config.channels.feishu.enabled)
        self.assertFalse(config.channels.telegram.enabled)
        self.assertFalse(config.channels.discord.enabled)
        self.assertEqual(config.sandbox.image, PINNED_IMAGE)
        config_text = self.paths.config.read_text(encoding="utf-8")
        self.assertNotIn("sentinel", config_text)
        self.assertIn('app_id_env = "LOBSTER0_FEISHU_APP_ID"', config_text)
        self.assertIn('app_secret_env = "LOBSTER0_FEISHU_APP_SECRET"', config_text)
        self.assertEqual(
            self.paths.secrets_file.read_text(encoding="utf-8"),
            "LOBSTER0_MODEL_API_KEY=sentinel-model-key\n"
            "LOBSTER0_FEISHU_APP_ID=cli_app\n"
            "LOBSTER0_FEISHU_APP_SECRET=sentinel-app-secret\n",
        )
        self.assertGreater(result.owner.id, 0)

    def test_setup_refuses_existing_files_and_unsafe_secret_text(self) -> None:
        """已有 config/Secret 与 dotenv 不安全值都必须在合并前拒绝。"""
        self.paths.home.mkdir(mode=0o700)
        self.paths.config.write_text("owned", encoding="utf-8")
        with self.assertRaisesRegex(SetupError, "already exists"):
            write_fresh_setup(
                self.paths,
                SetupAnswers.defaults(),
                {"LOBSTER0_MODEL_API_KEY": "x"},
                sandbox_image=PINNED_IMAGE,
            )

        self.paths.config.unlink()
        self.paths.secrets_file.write_text("owned", encoding="utf-8")
        with self.assertRaisesRegex(SetupError, "already exists"):
            write_fresh_setup(
                self.paths,
                SetupAnswers.defaults(),
                {"LOBSTER0_MODEL_API_KEY": "x"},
                sandbox_image=PINNED_IMAGE,
            )
        self.assertFalse(self.paths.config.exists())

        for value in ("", " leading", "trailing ", "'quoted", '"quoted', "a\rb", "a\nb", "a\0b"):
            with self.subTest(value=repr(value)), self.assertRaisesRegex(
                SetupError, "unsafe secret"
            ):
                validate_secret_value(value)
        self.assertEqual(validate_secret_value("safe=#value"), "safe=#value")

    def test_setup_secrets_cannot_add_a_second_dotenv_entry(self) -> None:
        """所有 splitlines 分隔符都应在写入前拒绝，安全值只 round-trip 一个 key。"""
        separators = ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
        for separator in separators:
            with self.subTest(separator=ascii(separator)):
                paths = build_state_paths(self.root / f"state-{ord(separator)}")
                with self.assertRaisesRegex(SetupError, "unsafe secret"):
                    write_fresh_setup(
                        paths,
                        SetupAnswers.defaults(),
                        {
                            "LOBSTER0_MODEL_API_KEY": (
                                f"safe{separator}INJECTED_ENV=owned"
                            )
                        },
                        PINNED_IMAGE,
                    )
                self.assertFalse(paths.secrets_file.exists())

        safe_paths = build_state_paths(self.root / "safe-state")
        write_fresh_setup(
            safe_paths,
            SetupAnswers.defaults(),
            {"LOBSTER0_MODEL_API_KEY": "safe=value"},
            PINNED_IMAGE,
        )
        environment: dict[str, str] = {}
        loaded = load_dotenv(safe_paths.secrets_file, environment)
        self.assertEqual(loaded, ("LOBSTER0_MODEL_API_KEY",))
        self.assertEqual(environment, {"LOBSTER0_MODEL_API_KEY": "safe=value"})

    def test_setup_rejects_unsafe_home_and_invalid_answers_before_writing(self) -> None:
        """state home 的 symlink/宽权限与无效 Owner ID 都应 fail closed。"""
        target = self.root / "target"
        target.mkdir(mode=0o700)
        self.paths.home.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(SetupError, "symbolic link"):
            write_fresh_setup(
                self.paths,
                SetupAnswers.defaults(),
                {"LOBSTER0_MODEL_API_KEY": "x"},
                sandbox_image=PINNED_IMAGE,
            )
        self.paths.home.unlink()
        self.paths.home.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(SetupError, "directory"):
            write_fresh_setup(
                self.paths,
                SetupAnswers.defaults(),
                {"LOBSTER0_MODEL_API_KEY": "x"},
                sandbox_image=PINNED_IMAGE,
            )
        self.paths.home.unlink()
        self.paths.home.mkdir(mode=0o755)
        with self.assertRaisesRegex(SetupError, "0700"):
            write_fresh_setup(
                self.paths,
                SetupAnswers.defaults(),
                {"LOBSTER0_MODEL_API_KEY": "x"},
                sandbox_image=PINNED_IMAGE,
            )

        self.paths.home.chmod(0o700)
        invalid_answers = (
            SetupAnswers(True, "invalid", False, None, False, None),
            SetupAnswers(False, None, True, True, False, None),
            SetupAnswers(False, None, False, None, True, 0),
        )
        for answers in invalid_answers:
            with self.subTest(answers=answers), self.assertRaisesRegex(
                SetupError, "Owner"
            ):
                write_fresh_setup(
                    self.paths,
                    answers,
                    {"LOBSTER0_MODEL_API_KEY": "x"},
                    sandbox_image=PINNED_IMAGE,
                )
        self.assertFalse(self.paths.config.exists())
        self.assertFalse(self.paths.secrets_file.exists())

    def test_setup_writes_telegram_and_discord_owner_allowlists(self) -> None:
        """Telegram/Discord 应使用固定 Token env 名与同一 Owner allowlist。"""
        answers = SetupAnswers(False, None, True, 123, True, 456)
        secrets = {
            "LOBSTER0_MODEL_API_KEY": "model",
            "LOBSTER0_TELEGRAM_BOT_TOKEN": "telegram",
            "LOBSTER0_DISCORD_BOT_TOKEN": "discord",
        }

        write_fresh_setup(self.paths, answers, secrets, PINNED_IMAGE)

        config = load_config(self.paths, {})
        self.assertEqual(config.channels.telegram.owner_user_id, 123)
        self.assertEqual(config.channels.telegram.allowed_user_ids, (123,))
        self.assertEqual(config.channels.discord.owner_user_id, 456)
        self.assertEqual(config.channels.discord.allowed_user_ids, (456,))
        self.assertIn(
            'bot_token_env = "LOBSTER0_TELEGRAM_BOT_TOKEN"',
            self.paths.config.read_text(encoding="utf-8"),
        )
        self.assertIn(
            'bot_token_env = "LOBSTER0_DISCORD_BOT_TOKEN"',
            self.paths.config.read_text(encoding="utf-8"),
        )

    def test_interactive_enabled_channels_hide_every_credential(self) -> None:
        """启用三个 Channel 时所有凭据都必须通过 getpass 且不回显。"""
        tty = _FakeTty(["y\n", "ou_owner\n", "y\n", "123\n", "y\n", "456\n"])
        sentinels = ["model", "app-id", "app-secret", "telegram", "discord"]

        with (
            mock.patch("lobster0.setup._open_tty", return_value=tty),
            mock.patch("lobster0.setup.getpass.getpass", side_effect=sentinels) as hidden,
        ):
            run_interactive_setup(self.paths, sandbox_image=PINNED_IMAGE)

        self.assertEqual(hidden.call_count, 5)
        self.assertEqual(
            [call.args[0] for call in hidden.call_args_list],
            [
                "Model API key: ",
                "Feishu App ID: ",
                "Feishu App Secret: ",
                "Telegram Bot token: ",
                "Discord Bot token: ",
            ],
        )
        visible = tty.output.getvalue()
        self.assertTrue(all(sentinel not in visible for sentinel in sentinels))

    def test_setup_requires_exact_fixed_secret_names_for_enabled_channels(self) -> None:
        """Secret 文件只接受模型与已启用 Channel 的固定变量名。"""
        answers = SetupAnswers(True, "ou_owner", False, None, False, None)
        cases = (
            {"LOBSTER0_MODEL_API_KEY": "x"},
            {
                "LOBSTER0_MODEL_API_KEY": "x",
                "LOBSTER0_FEISHU_APP_ID": "id",
                "LOBSTER0_FEISHU_APP_SECRET": "secret",
                "UNEXPECTED_TOKEN": "secret",
            },
        )
        for secrets in cases:
            with self.subTest(names=tuple(secrets)), self.assertRaisesRegex(
                SetupError, "required Secret names"
            ):
                write_fresh_setup(
                    self.paths,
                    answers,
                    secrets,
                    sandbox_image=PINNED_IMAGE,
                )
        self.assertFalse(self.paths.config.exists())

    def test_interactive_setup_reads_tty_and_uses_getpass_with_zero_channels(self) -> None:
        """交互 setup 应允许零 Channel，并只用 getpass 读取模型 Secret。"""
        tty = _FakeTty(["n\n", "n\n", "n\n"])
        sentinel = "interactive-sentinel"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch("lobster0.setup._open_tty", return_value=tty) as open_tty,
            mock.patch("lobster0.setup.getpass.getpass", return_value=sentinel) as hidden,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = run_interactive_setup(self.paths, sandbox_image=PINNED_IMAGE)

        open_tty.assert_called_once_with()
        hidden.assert_called_once_with("Model API key: ", stream=tty)
        config = load_config(self.paths, {})
        self.assertFalse(config.channels.feishu.enabled)
        self.assertFalse(config.channels.telegram.enabled)
        self.assertFalse(config.channels.discord.enabled)
        self.assertEqual(
            self.paths.secrets_file.read_text(encoding="utf-8"),
            f"LOBSTER0_MODEL_API_KEY={sentinel}\n",
        )
        self.assertGreater(result.owner.id, 0)
        visible = tty.output.getvalue() + stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(sentinel, visible)

    def test_interactive_setup_uses_real_duplex_controlling_tty(self) -> None:
        """零与全 Channel setup 都应通过 non-seekable controlling TTY 安全完成。"""
        cases = (
            (
                "zero",
                b"n\nn\nn\n",
                (False, False, False),
            ),
            (
                "all",
                b"y\nou_owner\ny\n123\ny\n456\n",
                (True, True, True),
            ),
        )
        for name, responses, enabled in cases:
            with self.subTest(name=name):
                paths = build_state_paths(self.root / f"pty-{name}")
                secrets = _PTY_SECRETS[name]

                returncode, visible = self._run_setup_in_controlling_tty(
                    paths, name, responses
                )

                self.assertEqual(returncode, 0, visible)
                self.assertIn("Enable Feishu?", visible)
                self.assertIn("Model API key:", visible)
                self.assertTrue(all(secret not in visible for secret in secrets))
                config = load_config(paths, {})
                self.assertEqual(
                    (
                        config.channels.feishu.enabled,
                        config.channels.telegram.enabled,
                        config.channels.discord.enabled,
                    ),
                    enabled,
                )
                self.assertEqual(stat.S_IMODE(paths.home.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(paths.config.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(paths.secrets_file.stat().st_mode), 0o600)
                config_text = paths.config.read_text(encoding="utf-8")
                self.assertTrue(all(secret not in config_text for secret in secrets))

    def test_open_tty_delegates_duplex_io_and_context_to_owned_pty(self) -> None:
        """single owner 应保留真实 PTY 的双工、fileno、isatty 与上下文语义。"""
        master, slave = os.openpty()

        def close_if_open(descriptor: int) -> None:
            try:
                os.close(descriptor)
            except OSError:
                pass

        self.addCleanup(close_if_open, master)
        self.addCleanup(close_if_open, slave)

        with mock.patch("lobster0.setup.os.open", return_value=slave):
            with setup_module._open_tty() as tty:
                self.assertTrue(tty.isatty())
                self.assertTrue(os.isatty(tty.fileno()))
                self.assertTrue(tty.readable())
                self.assertTrue(tty.writable())
                self.assertFalse(tty.seekable())
                self.assertEqual(tty.encoding, "utf-8")
                tty.write("prompt> ")
                tty.flush()
                readable, _, _ = select.select((master,), (), (), 1)
                self.assertEqual(readable, [master])
                self.assertEqual(os.read(master, len(b"prompt> ")), b"prompt> ")
                os.write(master, b"answer\n")
                self.assertEqual(tty.readline(), "answer\n")
                reader_descriptor = tty.fileno()

        self.assertTrue(tty.closed)
        with self.assertRaises(ValueError):
            tty.fileno()
        with self.assertRaises(OSError):
            os.fstat(reader_descriptor)

    def test_open_tty_rejects_non_character_device_without_dynamic_cause(self) -> None:
        """普通 fd 不得冒充终端，错误不得泄漏路径或动态底层原因。"""
        reader, writer = os.pipe()
        self.addCleanup(os.close, writer)

        with (
            mock.patch("lobster0.setup.os.open", return_value=reader),
            self.assertRaises(SetupError) as raised,
        ):
            setup_module._open_tty()

        self.assertEqual(str(raised.exception), "interactive terminal is unavailable")
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn("/dev/tty", str(raised.exception))
        with self.assertRaises(OSError):
            os.fstat(reader)

    def test_open_tty_closes_every_descriptor_when_duplex_wrap_fails(self) -> None:
        """双工 wrapper 中途失败时 base/read/write fd 都必须关闭。"""
        real_dup = os.dup
        descriptor = os.open(os.devnull, os.O_RDWR)
        descriptors = [descriptor]

        def close_if_open(value: int) -> None:
            try:
                os.close(value)
            except OSError:
                pass

        self.addCleanup(close_if_open, descriptor)

        def duplicate(value: int) -> int:
            duplicated = real_dup(value)
            descriptors.append(duplicated)
            self.addCleanup(close_if_open, duplicated)
            return duplicated

        expected_flags = (
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        with (
            mock.patch("lobster0.setup.os.open", return_value=descriptor) as opened,
            mock.patch("lobster0.setup.os.dup", side_effect=duplicate),
            mock.patch(
                "io.BufferedRWPair",
                side_effect=OSError("dynamic wrapper detail"),
            ),
            self.assertRaises(SetupError) as raised,
        ):
            setup_module._open_tty()

        opened.assert_called_once_with("/dev/tty", expected_flags)
        self.assertEqual(str(raised.exception), "interactive terminal is unavailable")
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertEqual(len(descriptors), 3)
        for value in descriptors:
            with self.subTest(descriptor=value), self.assertRaises(OSError):
                os.fstat(value)

    def test_open_tty_does_not_retry_a_reused_descriptor_after_close_error(self) -> None:
        """close 已释放 fd 后即使报错也不得再次关闭复用该编号的外部 fd。"""
        real_open = os.open
        real_close = os.close
        descriptor = real_open(os.devnull, os.O_RDWR)
        foreign: int | None = None

        def close_if_open(value: int) -> None:
            try:
                real_close(value)
            except OSError:
                pass

        self.addCleanup(close_if_open, descriptor)

        def close_with_reuse(value: int) -> None:
            nonlocal foreign
            if value == descriptor and foreign is None:
                real_close(value)
                foreign = real_open(os.devnull, os.O_RDONLY)
                self.addCleanup(close_if_open, foreign)
                raise OSError("close reported failure after releasing fd")
            real_close(value)

        with (
            mock.patch("lobster0.setup.os.open", return_value=descriptor),
            mock.patch("lobster0.setup.os.close", side_effect=close_with_reuse),
            self.assertRaisesRegex(SetupError, "interactive terminal is unavailable"),
        ):
            setup_module._open_tty()

        self.assertEqual(foreign, descriptor)
        assert foreign is not None
        try:
            metadata = os.fstat(foreign)
        except OSError:
            self.fail("foreign descriptor was closed by cleanup retry")
        self.assertTrue(stat.S_ISCHR(metadata.st_mode))

    def test_open_tty_async_wrapper_boundaries_keep_one_descriptor_owner(self) -> None:
        """wrapper 构造前后异步中断时 raw wrapper 不得与外层重复拥有 OS fd。"""
        real_open = os.open
        real_close = os.close
        real_dup = os.dup
        real_fileio = io.FileIO
        real_pair = io.BufferedRWPair
        real_text = io.TextIOWrapper

        def close_if_open(value: int) -> None:
            try:
                real_close(value)
            except OSError:
                pass

        for boundary in ("fileio", "pair", "text"):
            for phase in ("before", "after"):
                with self.subTest(boundary=boundary, phase=phase):
                    self._assert_async_tty_boundary_is_single_owner(
                        boundary,
                        phase,
                        real_open=real_open,
                        real_close=real_close,
                        real_dup=real_dup,
                        real_fileio=real_fileio,
                        real_pair=real_pair,
                        real_text=real_text,
                    )

    def _assert_async_tty_boundary_is_single_owner(
        self,
        boundary: str,
        phase: str,
        *,
        real_open: Callable[[str, int], int],
        real_close: Callable[[int], None],
        real_dup: Callable[[int], int],
        real_fileio: Callable[..., io.FileIO],
        real_pair: Callable[..., io.BufferedRWPair],
        real_text: Callable[..., io.TextIOWrapper],
    ) -> None:
        """在一个 wrapper 构造边界注入中断并断言 fd 只有外层 owner。"""
        descriptor = real_open(os.devnull, os.O_RDWR)
        descriptors = [descriptor]
        closefd_values: list[bool] = []
        text_calls = [0]

        def close_if_open(value: int) -> None:
            try:
                real_close(value)
            except OSError:
                pass

        self.addCleanup(close_if_open, descriptor)

        def duplicate(value: int) -> int:
            duplicated = real_dup(value)
            descriptors.append(duplicated)
            self.addCleanup(close_if_open, duplicated)
            return duplicated

        def interrupt_fileio(*args: object, **kwargs: object) -> io.FileIO:
            if phase == "before":
                raise KeyboardInterrupt
            raw = real_fileio(*args, **kwargs)
            closefd_values.append(raw.closefd)
            raise KeyboardInterrupt

        def interrupt_pair(
            reader: io.FileIO,
            writer: io.FileIO,
            *args: object,
            **kwargs: object,
        ) -> io.BufferedRWPair:
            closefd_values.extend((reader.closefd, writer.closefd))
            if phase == "before":
                raise KeyboardInterrupt
            pair = real_pair(reader, writer, *args, **kwargs)
            del pair
            raise KeyboardInterrupt

        def interrupt_text(*args: object, **kwargs: object) -> io.TextIOWrapper:
            if phase == "before":
                raise KeyboardInterrupt
            text = real_text(*args, **kwargs)
            text_calls[0] += 1
            del text
            raise KeyboardInterrupt

        stream: io.TextIOBase | None = None
        interrupted = False
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("lobster0.setup.os.open", return_value=descriptor))
            stack.enter_context(mock.patch("lobster0.setup.os.dup", side_effect=duplicate))
            if boundary == "fileio":
                stack.enter_context(mock.patch("io.FileIO", side_effect=interrupt_fileio))
            elif boundary == "pair":
                stack.enter_context(mock.patch("io.BufferedRWPair", side_effect=interrupt_pair))
            else:
                stack.enter_context(mock.patch("io.TextIOWrapper", side_effect=interrupt_text))
            try:
                stream = setup_module._open_tty()
            except KeyboardInterrupt:
                interrupted = True
        if stream is not None:
            stream.close()

        self.assertTrue(interrupted)
        if boundary == "fileio":
            self.assertEqual(closefd_values, [] if phase == "before" else [False])
        elif boundary == "pair":
            self.assertEqual(closefd_values, [False, False])
        else:
            self.assertEqual(text_calls, [0] if phase == "before" else [1])
        for value in descriptors:
            with self.subTest(descriptor=value):
                with self.assertRaises(OSError):
                    os.fstat(value)


if __name__ == "__main__":
    unittest.main()
