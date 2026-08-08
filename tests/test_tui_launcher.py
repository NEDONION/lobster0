"""裸 miniclaw 在 pi-tui 与 Textual fallback 之间选择的测试。"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miniclaw.paths import build_state_paths
from miniclaw.tui_launcher import PiTuiInspection, TuiLaunchError, run_default_tui


class TuiLauncherTest(unittest.TestCase):
    """验证默认 pi-tui、显式 fallback 和安全进程参数。"""

    def setUp(self) -> None:
        """创建不会接触真实用户状态的路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())

    @mock.patch("miniclaw.tui_launcher.subprocess.run")
    @mock.patch("miniclaw.tui_launcher.inspect_pi_tui")
    def test_auto_launches_compatible_built_pi_tui_with_argv(
        self,
        inspect_pi_tui,
        run,
    ) -> None:
        """默认模式必须用当前 Python 和显式 argv 启动 Node TUI。"""
        entry = self.paths.home / "dist" / "main.js"
        inspect_pi_tui.return_value = PiTuiInspection(
            node=Path("/opt/node/bin/node"),
            node_version=(22, 19, 0),
            entry=entry,
            problem=None,
        )
        run.return_value = mock.Mock(returncode=0)
        stderr = io.StringIO()

        result = run_default_tui(self.paths, environ={}, stderr=stderr)

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        args = run.call_args.args[0]
        self.assertEqual(args, ["/opt/node/bin/node", str(entry)])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["MINICLAW_HOME"], str(self.paths.home))
        self.assertTrue(child_env["MINICLAW_PYTHON"].endswith("python"))

    @mock.patch("miniclaw.tui_launcher.run_tui", return_value=7)
    @mock.patch("miniclaw.tui_launcher.inspect_pi_tui")
    def test_explicit_textual_never_inspects_or_starts_node(
        self,
        inspect_pi_tui,
        run_tui,
    ) -> None:
        """显式 Textual fallback 必须完全跳过 Node 探测。"""
        result = run_default_tui(
            self.paths,
            environ={"MINICLAW_TUI": "textual"},
            stderr=io.StringIO(),
        )

        self.assertEqual(result, 7)
        inspect_pi_tui.assert_not_called()
        run_tui.assert_called_once_with(self.paths)

    @mock.patch("miniclaw.tui_launcher.run_tui", return_value=0)
    @mock.patch("miniclaw.tui_launcher.inspect_pi_tui")
    def test_auto_reports_problem_and_falls_back_without_crashing(
        self,
        inspect_pi_tui,
        run_tui,
    ) -> None:
        """Auto 缺少 Node 22 时应给出一次行动提示并保持现有 TUI 可用。"""
        inspect_pi_tui.return_value = PiTuiInspection(
            node=Path("/usr/local/bin/node"),
            node_version=(20, 19, 0),
            entry=None,
            problem="pi-tui 需要 Node.js >= 22.19.0；当前为 20.19.0",
        )
        stderr = io.StringIO()

        result = run_default_tui(self.paths, environ={}, stderr=stderr)

        self.assertEqual(result, 0)
        self.assertIn("回退 Textual", stderr.getvalue())
        self.assertIn("22.19.0", stderr.getvalue())
        run_tui.assert_called_once_with(self.paths)

    @mock.patch("miniclaw.tui_launcher.inspect_pi_tui")
    def test_explicit_pi_fails_when_runtime_is_not_ready(self, inspect_pi_tui) -> None:
        """显式 pi 模式不能静默换壳，便于部署脚本发现缺失依赖。"""
        inspect_pi_tui.return_value = PiTuiInspection(
            node=None,
            node_version=None,
            entry=None,
            problem="没有找到 Node.js",
        )

        with self.assertRaises(TuiLaunchError) as captured:
            run_default_tui(
                self.paths,
                environ={"MINICLAW_TUI": "pi"},
                stderr=io.StringIO(),
            )

        self.assertIn("没有找到 Node.js", str(captured.exception))

    def test_unknown_mode_fails_before_any_process_is_started(self) -> None:
        """未知 UI mode 必须视为配置错误而非猜测默认值。"""
        with self.assertRaises(TuiLaunchError):
            run_default_tui(
                self.paths,
                environ={"MINICLAW_TUI": "desktop"},
                stderr=io.StringIO(),
            )


if __name__ == "__main__":
    unittest.main()
