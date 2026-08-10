"""MiniClaw service CLI 的固定命令与脱敏错误测试。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from miniclaw.cli import (
    _launchd_service,
    _service_install_preflight,
    _service_repository_commit,
    build_parser,
    main,
)
from miniclaw.doctor import CheckResult, CheckStatus
from miniclaw.gateway import GatewayConfigError
from miniclaw.gateway_service import ServiceError, ServiceSpec, ServiceStatus
from miniclaw.paths import build_state_paths


class _Service:
    """记录 CLI 选择的唯一生命周期动作。"""

    def __init__(self) -> None:
        """初始化空动作记录。"""
        self.actions: list[str] = []

    def install(self) -> None:
        """记录 install。"""
        self.actions.append("install")

    def status(self) -> ServiceStatus:
        """记录 status 并返回稳定健康结果。"""
        self.actions.append("status")
        return ServiceStatus(installed=True, loaded=True, running=True)

    def restart(self) -> None:
        """记录 restart。"""
        self.actions.append("restart")

    def uninstall(self) -> None:
        """记录 uninstall。"""
        self.actions.append("uninstall")


def _run(arguments: list[str]) -> tuple[int, str, str]:
    """调用真实 CLI 并收集标准输出与错误。"""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class ServiceCliTest(unittest.TestCase):
    """验证 service 子命令没有可注入 label/path/argv。"""

    def test_parser_exposes_only_four_fixed_service_actions(self) -> None:
        """service CLI 不得接受任意 plist、label 或 launchctl 参数。"""
        parser = build_parser()
        service = next(
            action.choices["service"]
            for action in parser._actions
            if hasattr(action, "choices") and isinstance(action.choices, dict)
        )
        options = {
            option
            for action in service._actions
            for option in action.option_strings
        }

        self.assertEqual(options, {"-h", "--help", "--home"})
        choices = next(
            action.choices
            for action in service._actions
            if hasattr(action, "choices") and isinstance(action.choices, dict)
        )
        self.assertEqual(set(choices), {"install", "status", "restart", "uninstall"})

    def test_each_action_dispatches_once_and_status_is_structured(self) -> None:
        """CLI 只调用选定动作，status 不转发 launchctl 原始输出。"""
        with tempfile.TemporaryDirectory() as directory:
            for action in ("install", "status", "restart", "uninstall"):
                service = _Service()
                with (
                    self.subTest(action=action),
                    mock.patch("miniclaw.cli._launchd_service", return_value=service),
                    mock.patch("miniclaw.cli._service_install_preflight"),
                ):
                    result = _run(["service", "--home", directory, action])

                self.assertEqual(result[0], 0)
                self.assertEqual(service.actions, [action])
                if action == "status":
                    self.assertEqual(
                        result[1],
                        "service installed=true loaded=true running=true\n",
                    )

    def test_service_error_returns_stable_code_without_private_detail(self) -> None:
        """底层 manager/path 详情不能进入 CLI 错误输出。"""
        private = str(Path.home() / "private-service-path")
        service = _Service()
        service.restart = mock.Mock(side_effect=ServiceError("service_manager_failed", private))
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("miniclaw.cli._launchd_service", return_value=service),
        ):
            code, output, error = _run(
                ["service", "--home", directory, "restart"]
            )

        self.assertEqual(code, 5)
        self.assertEqual(output, "")
        self.assertEqual(error, "error: service_manager_failed\n")
        self.assertNotIn(private, error)

    def test_service_uses_console_launcher_beside_current_venv_python(self) -> None:
        """venv Python 可为 symlink，但 service 必须选择同一 env 的 miniclaw launcher。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime_bin = root / "runtime" / "bin"
            runtime_bin.mkdir(parents=True)
            python = runtime_bin / "python"
            python.symlink_to(Path("/usr/bin/python3"))
            launcher = runtime_bin / "miniclaw"
            launcher.write_text("#!/usr/bin/python3\n", encoding="utf-8")
            launcher.chmod(0o700)
            paths = build_state_paths(root / "state")
            spec = ServiceSpec(
                label="io.miniclaw.gateway",
                path=root / "agent.plist",
                receipt_path=root / "receipt.json",
                content=b"plist",
                sha256="9ceec13202afbf12ee3abb994c669c711749c18e194326734db6123e94947e04",
            )
            sentinel = object()

            with (
                mock.patch("miniclaw.cli.sys.executable", str(python)),
                mock.patch(
                    "miniclaw.cli.render_launchd_service",
                    return_value=spec,
                ) as render,
                mock.patch(
                    "miniclaw.cli._service_repository_commit",
                    return_value="a" * 40,
                ),
                mock.patch("miniclaw.cli.LaunchdService", return_value=sentinel),
            ):
                service = _launchd_service(paths)

        self.assertIs(service, sentinel)
        self.assertEqual(render.call_args.kwargs["launcher"], launcher)

    def test_install_preflight_ignores_unrelated_tui_and_browser_failures(self) -> None:
        """Gateway service 不应被未使用的 TUI/Browser readiness 阻塞。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            checks = (
                CheckResult("workspace", CheckStatus.PASS, "ready"),
                CheckResult("pi_tui", CheckStatus.FAIL, "not built"),
                CheckResult("browser", CheckStatus.FAIL, "disabled"),
            )
            with (
                mock.patch("miniclaw.cli.load_dotenv"),
                mock.patch("miniclaw.cli.load_config", return_value=SimpleNamespace()),
                mock.patch(
                    "miniclaw.cli.collect_enabled_channels",
                    return_value=("feishu",),
                ),
                mock.patch("miniclaw.cli.run_local_checks", return_value=checks),
            ):
                _service_install_preflight(paths)

    def test_install_preflight_rejects_core_failure_and_second_channel(self) -> None:
        """Workspace 等必需项失败或启用第二平台时必须在写 plist 前拒绝。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            with (
                mock.patch("miniclaw.cli.load_dotenv"),
                mock.patch("miniclaw.cli.load_config", return_value=SimpleNamespace()),
                mock.patch(
                    "miniclaw.cli.collect_enabled_channels",
                    return_value=("feishu", "discord"),
                ),
            ):
                with self.assertRaises(GatewayConfigError):
                    _service_install_preflight(paths)
            with (
                mock.patch("miniclaw.cli.load_dotenv"),
                mock.patch("miniclaw.cli.load_config", return_value=SimpleNamespace()),
                mock.patch(
                    "miniclaw.cli.collect_enabled_channels",
                    return_value=("feishu",),
                ),
                mock.patch(
                    "miniclaw.cli.run_local_checks",
                    return_value=(
                        CheckResult("workspace", CheckStatus.FAIL, "private detail"),
                    ),
                ),
            ):
                with self.assertRaisesRegex(ServiceError, "service_preflight_failed"):
                    _service_install_preflight(paths)

    def test_repository_commit_requires_clean_exact_head(self) -> None:
        """dirty status 或无效 HEAD 不能进入 Gateway provenance。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            clean = (
                mock.Mock(returncode=0, stdout="a" * 40 + "\n"),
                mock.Mock(returncode=0, stdout=""),
            )
            with mock.patch("miniclaw.cli.subprocess.run", side_effect=clean):
                self.assertEqual(_service_repository_commit(root), "a" * 40)
            dirty = (
                mock.Mock(returncode=0, stdout="a" * 40 + "\n"),
                mock.Mock(returncode=0, stdout=" M private-file\n"),
            )
            with (
                mock.patch("miniclaw.cli.subprocess.run", side_effect=dirty),
                self.assertRaisesRegex(ServiceError, "service_repository_dirty"),
            ):
                _service_repository_commit(root)


if __name__ == "__main__":
    unittest.main()
