"""Gateway 自重启的托管检测、控制器语义与退出码测试。"""

import asyncio
import unittest

from lobster0.channels.restart import (
    RESTART_EXIT_CODE,
    GatewayRestartController,
    detect_supervision,
)


class DetectSupervisionTest(unittest.TestCase):
    """只在有明确证据时才认定进程受托管，其余一律视为前台运行。"""

    def test_systemd_and_launchd_evidence_is_recognized(self) -> None:
        """systemd 的 INVOCATION_ID 与 launchd 的精确 Label 都算受托管。"""
        self.assertEqual(
            detect_supervision({"INVOCATION_ID": "b1946ac92492d2347c6235b4d2611184"}),
            "systemd-user",
        )
        self.assertEqual(
            detect_supervision({"XPC_SERVICE_NAME": "io.lobster0.gateway"}),
            "launchd",
        )

    def test_foreground_and_foreign_launchd_jobs_are_not_supervised(self) -> None:
        """终端前台运行、空值和别的 launchd 作业都不能被误判成受托管。"""
        for environ in (
            {},
            {"INVOCATION_ID": ""},
            {"XPC_SERVICE_NAME": "0"},
            {"XPC_SERVICE_NAME": "com.apple.Terminal"},
            {"TERM": "xterm-256color", "SHELL": "/bin/zsh"},
        ):
            with self.subTest(environ=environ):
                self.assertIsNone(detect_supervision(environ))

    def test_explicit_declaration_wins_in_both_directions(self) -> None:
        """LOBSTER0_SUPERVISED 既能声明外部守护，也能强制关闭自动检测。"""
        self.assertEqual(detect_supervision({"LOBSTER0_SUPERVISED": "1"}), "declared")
        self.assertIsNone(
            detect_supervision(
                {"LOBSTER0_SUPERVISED": "0", "INVOCATION_ID": "abc"},
            )
        )


class GatewayRestartControllerTest(unittest.IsolatedAsyncioTestCase):
    """受托管才允许退出；没有守护时宁可什么都不做。"""

    async def test_unsupervised_request_never_exits_and_says_so(self) -> None:
        """前台运行时退出就等于把机器人关掉，必须拒绝并给出手动指引。"""
        shutdown = asyncio.Event()
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        controller = GatewayRestartController(
            shutdown_event=shutdown,
            supervision=None,
            sleep=sleep,
        )

        notice = controller.request()
        await asyncio.sleep(0)

        self.assertFalse(shutdown.is_set())
        self.assertFalse(controller.restart_requested)
        self.assertEqual(controller.exit_code, 0)
        self.assertEqual(slept, [])
        self.assertIn("没有", notice)
        self.assertIn("lobster0 service install", notice)
        self.assertIn("Ctrl-C", notice)

    async def test_supervised_request_acknowledges_then_exits_non_zero(self) -> None:
        """受托管时先确认、再在宽限期后触发关停，并以非零码退出让 supervisor 拉起。"""
        shutdown = asyncio.Event()
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        controller = GatewayRestartController(
            shutdown_event=shutdown,
            supervision="systemd-user",
            grace_seconds=3.0,
            sleep=sleep,
        )

        notice = controller.request()

        self.assertFalse(shutdown.is_set())
        self.assertIn("systemd", notice)
        self.assertIn("重启", notice)
        await controller.wait_for_shutdown_request()
        self.assertEqual(slept, [3.0])
        self.assertTrue(shutdown.is_set())
        self.assertTrue(controller.restart_requested)
        self.assertEqual(controller.exit_code, RESTART_EXIT_CODE)
        self.assertNotEqual(RESTART_EXIT_CODE, 0)

    async def test_close_cancels_a_still_pending_shutdown_task(self) -> None:
        """SIGTERM 与 /restart 撞在一起时，宽限任务必须被收走而不是变成悬挂 Task。"""
        shutdown = asyncio.Event()
        started = asyncio.Event()

        async def sleep(seconds: float) -> None:
            del seconds
            started.set()
            await asyncio.Event().wait()

        controller = GatewayRestartController(
            shutdown_event=shutdown,
            supervision="declared",
            sleep=sleep,
        )

        controller.request()
        await started.wait()
        await controller.close()

        self.assertFalse(shutdown.is_set())
        self.assertTrue(controller.restart_requested)

    async def test_repeated_request_arms_the_shutdown_only_once(self) -> None:
        """重复发 /restart 不能叠加多个关停任务。"""
        shutdown = asyncio.Event()
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        controller = GatewayRestartController(
            shutdown_event=shutdown,
            supervision="launchd",
            sleep=sleep,
        )

        controller.request()
        second = controller.request()
        await controller.wait_for_shutdown_request()

        self.assertEqual(len(slept), 1)
        self.assertIn("已经", second)


if __name__ == "__main__":
    unittest.main()
