"""Python Core 与 Browser Worker 的 NDJSON 协议测试。"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from miniclaw.browser.client import BrowserClient
from miniclaw.browser.models import BrowserAction, BrowserProtocolError


class BrowserProtocolTest(unittest.IsolatedAsyncioTestCase):
    """验证握手、关联响应、错误版本和取消清理。"""

    def setUp(self) -> None:
        """为每个 fake Worker 创建独立脚本目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def _worker(self, source: str) -> tuple[str, ...]:
        """写入一个最小 Worker 脚本并返回精确 argv。"""
        path = self.root / "worker.py"
        path.write_text(source, encoding="utf-8")
        return (sys.executable, str(path))

    async def test_request_returns_only_its_correlated_result(self) -> None:
        """Client 必须等待 ready，并只接受同 ID 的标准响应。"""
        command = self._worker(
            "import json, sys\n"
            "print(json.dumps({'protocol':'miniclaw.browser.v1','type':'ready'}), flush=True)\n"
            "request = json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'protocol':'miniclaw.browser.v1','id':request['id'],"
            "'ok':True,'result':{'accepted':request['action']}}), flush=True)\n"
        )
        client = BrowserClient(command, timeout_seconds=1)
        self.addAsyncCleanup(client.close)

        await client.start()
        result = await client.request(BrowserAction("session-1", "close", {}))

        self.assertEqual(result, {"accepted": "close"})

    async def test_wrong_handshake_version_fails_closed(self) -> None:
        """Worker 声明未知协议版本时不能继续发送动作。"""
        command = self._worker(
            "import json\n"
            "print(json.dumps({'protocol':'miniclaw.browser.v2','type':'ready'}), flush=True)\n"
        )
        client = BrowserClient(command, timeout_seconds=1)

        with self.assertRaises(BrowserProtocolError) as raised:
            await client.start()

        self.assertEqual(raised.exception.code, "unsupported_version")
        self.assertFalse(client.running)

    async def test_handshake_timeout_is_stable_and_closes_worker(self) -> None:
        """握手超时必须返回稳定短码并清理尚未 ready 的 Worker。"""
        command = self._worker("import time\ntime.sleep(30)\n")
        client = BrowserClient(command, timeout_seconds=0.05)

        with self.assertRaises(BrowserProtocolError) as raised:
            await client.start()

        self.assertEqual(raised.exception.code, "browser_timeout")
        self.assertFalse(client.running)

    async def test_cancelled_request_terminates_worker(self) -> None:
        """取消等待中的请求必须关闭 Worker，不能留下孤儿进程。"""
        command = self._worker(
            "import json, sys, time\n"
            "print(json.dumps({'protocol':'miniclaw.browser.v1','type':'ready'}), flush=True)\n"
            "sys.stdin.readline()\n"
            "time.sleep(30)\n"
        )
        client = BrowserClient(command, timeout_seconds=10)
        await client.start()
        pending = asyncio.create_task(
            client.request(BrowserAction("session-1", "snapshot", {}))
        )
        await asyncio.sleep(0.05)

        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending

        self.assertFalse(client.running)

    async def test_worker_error_uses_stable_code_and_bounded_message(self) -> None:
        """Worker 失败只暴露稳定短码和受限公开消息。"""
        command = self._worker(
            "import json, sys\n"
            "print(json.dumps({'protocol':'miniclaw.browser.v1','type':'ready'}), flush=True)\n"
            "request = json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'protocol':'miniclaw.browser.v1','id':request['id'],"
            "'ok':False,'error':{'code':'browser_action_unavailable',"
            "'message':'Browser action is not available'}}), flush=True)\n"
        )
        client = BrowserClient(command, timeout_seconds=1)
        self.addAsyncCleanup(client.close)
        await client.start()

        with self.assertRaises(BrowserProtocolError) as raised:
            await client.request(BrowserAction("session-1", "open", {"url": "https://example.com"}))

        self.assertEqual(raised.exception.code, "browser_action_unavailable")
        self.assertLessEqual(len(str(raised.exception)), 256)

    async def test_mismatched_response_closes_desynchronized_worker(self) -> None:
        """响应 ID 错配后必须关闭协议流，不能继续复用未知状态进程。"""
        command = self._worker(
            "import json, sys, time\n"
            "print(json.dumps({'protocol':'miniclaw.browser.v1','type':'ready'}), flush=True)\n"
            "sys.stdin.readline()\n"
            "print(json.dumps({'protocol':'miniclaw.browser.v1','id':'wrong-id',"
            "'ok':True,'result':{}}), flush=True)\n"
            "time.sleep(30)\n"
        )
        client = BrowserClient(command, timeout_seconds=1)
        await client.start()

        with self.assertRaises(BrowserProtocolError) as raised:
            await client.request(BrowserAction("session-1", "snapshot", {}))

        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertFalse(client.running)


if __name__ == "__main__":
    unittest.main()
