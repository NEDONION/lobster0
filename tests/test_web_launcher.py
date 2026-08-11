"""Web 控制台 launcher 的绑定安全、Node 门槛与进程参数测试。"""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lobster0.paths import build_state_paths
from lobster0.web_launcher import (
    DEFAULT_WEB_PORT,
    WebLaunchError,
    classify_host,
    inspect_web_console,
    resolve_bind_plan,
    run_web_console,
)

_TOKEN = "t" * 32


class HostClassificationTest(unittest.TestCase):
    """只有明确的回环字面量才算回环；其余一律按可达网络对待。"""

    def test_accepts_the_three_loopback_literals(self) -> None:
        """默认与手写回环地址都必须归类为 loopback。"""
        for host in ("127.0.0.1", "::1", "localhost", "127.0.0.2"):
            with self.subTest(host=host):
                self.assertEqual(classify_host(host), "loopback")

    def test_treats_wildcard_and_routable_addresses_as_public(self) -> None:
        """通配与真实网卡地址都必须归类为 public，从而强制要求 token。"""
        for host in ("0.0.0.0", "::", "192.168.1.5", "10.0.0.1", "203.0.113.7"):
            with self.subTest(host=host):
                self.assertEqual(classify_host(host), "public")

    def test_refuses_anything_that_is_not_an_ip_literal(self) -> None:
        """拒绝主机名与非点分写法，避免 DNS 或 inet_aton 的歧义解析。"""
        # `2130706433` 与 `0x7f000001` 在部分 inet_aton 实现里就是 127.0.0.1，
        # 但在别的实现里不是；`example.com` 更是要靠 DNS 才能知道绑到哪。
        # 与其猜，不如只接受无歧义的字面量。
        for host in ("2130706433", "0x7f000001", "0", "127.1", "example.com", "", " "):
            with self.subTest(host=host):
                with self.assertRaises(WebLaunchError):
                    classify_host(host)


class BindPlanTest(unittest.TestCase):
    """绑定计划是唯一决定「谁能访问」的地方。"""

    def test_defaults_to_loopback_without_any_token(self) -> None:
        """默认绑回环，且不要求任何凭据。"""
        plan = resolve_bind_plan(None, None, {})

        self.assertEqual(plan.host, "127.0.0.1")
        self.assertEqual(plan.port, DEFAULT_WEB_PORT)
        self.assertIsNone(plan.token)
        self.assertFalse(plan.public)

    def test_refuses_a_public_bind_without_a_token(self) -> None:
        """非回环绑定缺 token 时必须拒绝启动，不得回退回环。"""
        for host in ("0.0.0.0", "::", "192.168.1.5"):
            with self.subTest(host=host):
                with self.assertRaises(WebLaunchError):
                    resolve_bind_plan(host, None, {})

    def test_refuses_a_public_bind_with_a_short_token(self) -> None:
        """短 token 等于没有保护，按缺失处理。"""
        with self.assertRaises(WebLaunchError):
            resolve_bind_plan("0.0.0.0", None, {"LOBSTER0_WEB_TOKEN": "t" * 31})

    def test_allows_a_public_bind_with_an_environment_token(self) -> None:
        """显式 host 加足够长的环境 token 才允许对网络可达。"""
        plan = resolve_bind_plan("0.0.0.0", 8080, {"LOBSTER0_WEB_TOKEN": _TOKEN})

        self.assertEqual(plan.host, "0.0.0.0")
        self.assertEqual(plan.port, 8080)
        self.assertEqual(plan.token, _TOKEN)
        self.assertTrue(plan.public)

    def test_ignores_a_token_when_bound_to_loopback(self) -> None:
        """回环绑定不需要 token；提供了也不改变回环语义。"""
        plan = resolve_bind_plan("127.0.0.1", None, {"LOBSTER0_WEB_TOKEN": _TOKEN})

        self.assertFalse(plan.public)

    def test_rejects_ports_outside_the_unprivileged_range(self) -> None:
        """拒绝特权端口与越界端口，避免需要 root 或必然失败的绑定。"""
        for port in (0, 80, 1023, 65_536, -1):
            with self.subTest(port=port):
                with self.assertRaises(WebLaunchError):
                    resolve_bind_plan(None, port, {})


class WebConsoleInspectionTest(unittest.TestCase):
    """启动前只做只读检查，并给出一条可操作问题。"""

    def setUp(self) -> None:
        """准备一个不接触真实用户状态的临时构建产物。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.entry = self.root / "out/web/server/index.js"
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text("// test entry\n", encoding="utf-8")

    @mock.patch("lobster0.web_launcher.subprocess.run")
    def test_reports_an_unsupported_node_version(self, run) -> None:
        """低于已验证 LTS 区间的 Node 必须被指名拒绝。"""
        run.return_value = mock.Mock(returncode=0, stdout="v22.19.0\n")

        inspection = inspect_web_console(
            {
                "LOBSTER0_NODE": "/opt/node/bin/node",
                "LOBSTER0_WEB_ENTRY": str(self.entry),
            }
        )

        self.assertFalse(inspection.ready)
        assert inspection.problem is not None
        self.assertIn("22.22.3", inspection.problem)

    @mock.patch("lobster0.web_launcher.subprocess.run")
    def test_reports_a_missing_build(self, run) -> None:
        """未构建时给出确切路径与构建命令，而不是让 Node 报模块找不到。"""
        run.return_value = mock.Mock(returncode=0, stdout="v24.15.0\n")

        inspection = inspect_web_console(
            {
                "LOBSTER0_NODE": "/opt/node/bin/node",
                "LOBSTER0_WEB_ENTRY": str(self.root / "missing.js"),
            }
        )

        self.assertFalse(inspection.ready)
        assert inspection.problem is not None
        self.assertIn("build:web", inspection.problem)

    @mock.patch("lobster0.web_launcher.subprocess.run")
    def test_probes_node_with_a_sanitized_environment(self, run) -> None:
        """探测 Node 版本只能用调用方封闭环境，且剔除注入变量。"""
        run.return_value = mock.Mock(returncode=0, stdout="v24.15.0\n")

        with mock.patch.dict(
            os.environ,
            {
                "NODE_OPTIONS": "--require=/tmp/inject.js",
                "NODE_PATH": "/tmp/global",
                "LEAK_FROM_REAL_ENV": "must-not-propagate",
            },
        ):
            inspection = inspect_web_console(
                {
                    "LOBSTER0_NODE": "/opt/node/bin/node",
                    "LOBSTER0_WEB_ENTRY": str(self.entry),
                    "NODE_OPTIONS": "--require=/tmp/from-caller.js",
                }
            )

        self.assertTrue(inspection.ready)
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("NODE_OPTIONS", environment)
        self.assertNotIn("NODE_PATH", environment)
        self.assertNotIn("LEAK_FROM_REAL_ENV", environment)


class RunWebConsoleTest(unittest.TestCase):
    """真正 spawn 时的 argv、环境与失败语义。"""

    def setUp(self) -> None:
        """创建已初始化状态与临时构建产物。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.paths = build_state_paths(self.root / "state")
        self.paths.config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.config.write_text("[agent]\n", encoding="utf-8")
        self.entry = self.root / "out/web/server/index.js"
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text("// test entry\n", encoding="utf-8")
        self.environ = {
            "LOBSTER0_NODE": "/opt/node/bin/node",
            "LOBSTER0_WEB_ENTRY": str(self.entry),
        }

    def _patch_node_probe(self) -> None:
        """让版本探测返回一个受支持的 Node 版本。"""
        patcher = mock.patch(
            "lobster0.web_launcher._read_node_version",
            return_value=(24, 15, 0),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @mock.patch("lobster0.web_launcher.subprocess.run")
    def test_passes_the_bind_plan_through_the_environment(self, run) -> None:
        """host/port 通过环境传给 Node，argv 只有 node 与入口两项。"""
        self._patch_node_probe()
        run.return_value = mock.Mock(returncode=0)

        exit_code = run_web_console(self.paths, environ=self.environ)

        self.assertEqual(exit_code, 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/opt/node/bin/node", str(self.entry)])
        self.assertFalse(run.call_args.kwargs["shell"])
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["LOBSTER0_WEB_HOST"], "127.0.0.1")
        self.assertEqual(environment["LOBSTER0_WEB_PORT"], str(DEFAULT_WEB_PORT))
        self.assertEqual(environment["LOBSTER0_HOME"], str(self.paths.home))
        self.assertEqual(environment["LOBSTER0_PYTHON"], mock.ANY)

    @mock.patch("lobster0.web_launcher.subprocess.run")
    def test_never_puts_the_token_in_argv(self, run) -> None:
        """token 只能走环境；出现在 argv 里会被同机其他用户从 ps 读到。"""
        self._patch_node_probe()
        run.return_value = mock.Mock(returncode=0)
        environ = {**self.environ, "LOBSTER0_WEB_TOKEN": _TOKEN}

        run_web_console(self.paths, host="0.0.0.0", environ=environ, stderr=io.StringIO())

        argv = run.call_args.args[0]
        self.assertNotIn(_TOKEN, " ".join(argv))
        self.assertEqual(run.call_args.kwargs["env"]["LOBSTER0_WEB_TOKEN"], _TOKEN)

    @mock.patch("lobster0.web_launcher.subprocess.run")
    def test_warns_loudly_before_a_public_bind(self, run) -> None:
        """对网络可达时必须打印警告，且警告里不含 token。"""
        self._patch_node_probe()
        run.return_value = mock.Mock(returncode=0)
        environ = {**self.environ, "LOBSTER0_WEB_TOKEN": _TOKEN}
        stderr = io.StringIO()

        run_web_console(self.paths, host="0.0.0.0", environ=environ, stderr=stderr)

        warning = stderr.getvalue()
        self.assertIn("warning", warning)
        self.assertIn("0.0.0.0", warning)
        self.assertNotIn(_TOKEN, warning)

    @mock.patch("lobster0.web_launcher.subprocess.run")
    def test_does_not_warn_on_the_loopback_default(self, run) -> None:
        """回环默认不该产生噪音警告。"""
        self._patch_node_probe()
        run.return_value = mock.Mock(returncode=0)
        stderr = io.StringIO()

        run_web_console(self.paths, environ=self.environ, stderr=stderr)

        self.assertEqual(stderr.getvalue(), "")

    def test_refuses_a_public_bind_without_a_token_before_spawning(self) -> None:
        """缺 token 时必须在 spawn 之前失败，一个 Node 进程都不该起来。"""
        self._patch_node_probe()
        with mock.patch("lobster0.web_launcher.subprocess.run") as run:
            with self.assertRaises(WebLaunchError):
                run_web_console(self.paths, host="0.0.0.0", environ=self.environ)
            run.assert_not_called()

    def test_refuses_to_run_before_state_is_initialized(self) -> None:
        """没有 config 就没有可用的 Bridge，直接报错而不是让浏览器看到空壳。"""
        self._patch_node_probe()
        self.paths.config.unlink()

        with self.assertRaises(WebLaunchError):
            run_web_console(self.paths, environ=self.environ)

    @mock.patch("lobster0.web_launcher.subprocess.run", side_effect=OSError("boom"))
    def test_raises_instead_of_falling_back_when_node_cannot_start(self, run) -> None:
        """Web 控制台没有降级形态；起不来就报错，不静默做别的事。"""
        self._patch_node_probe()

        with self.assertRaises(WebLaunchError):
            run_web_console(self.paths, environ=self.environ)

    @mock.patch("lobster0.web_launcher.subprocess.run")
    def test_propagates_the_node_exit_code(self, run) -> None:
        """Node 的退出码要原样透出，便于服务管理器判断。"""
        self._patch_node_probe()
        run.return_value = mock.Mock(returncode=7)

        self.assertEqual(run_web_console(self.paths, environ=self.environ), 7)


if __name__ == "__main__":
    unittest.main()
