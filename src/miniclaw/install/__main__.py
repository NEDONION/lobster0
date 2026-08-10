"""提供 stdlib-only installer zipapp 的参数与退出码入口。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Never

from miniclaw.install.models import InstallError, InstallEvent, InstallRequest
from miniclaw.install.orchestrator import (
    BootstrapInputs,
    Installer,
    InstallResult,
    _invoking_account,
    emit_event,
)

_ACTIONS = {"install", "update", "uninstall"}
_INTERNAL_FLAGS = (
    "--manifest-file",
    "--manifest-sha256",
    "--managed-uv",
    "--managed-python-root",
    "--managed-python-executable",
)


class _CliError(ValueError):
    """表示不应触发 argparse 进程退出的安全请求错误。"""


class _Parser(argparse.ArgumentParser):
    """把 parser error 转换为稳定 exit 2。"""

    def error(self, message: str) -> Never:
        """拒绝输出含用户路径的 argparse 原始错误。"""
        del message
        raise _CliError("invalid installer arguments")


def build_parser() -> argparse.ArgumentParser:
    """创建 public flags 与 bootstrap internal handoff 参数解析器。"""
    parser = _Parser(
        prog="miniclaw-installer.pyz",
        description="MiniClaw trusted one-line installer",
    )
    parser.add_argument("action", nargs="?", choices=sorted(_ACTIONS), default="install")
    parser.add_argument("--version")
    parser.add_argument("--channel", choices=("stable", "dev"), default="stable")
    prefix = parser.add_mutually_exclusive_group()
    prefix.add_argument("--prefix")
    prefix.add_argument("--system-prefix", action="store_true")
    parser.add_argument("--home")
    onboarding = parser.add_mutually_exclusive_group()
    onboarding.add_argument("--onboard", dest="onboard", action="store_true")
    onboarding.add_argument("--no-onboard", dest="onboard", action="store_false")
    parser.set_defaults(onboard=True)
    parser.add_argument("--config")
    parser.add_argument("--secrets-file")
    service = parser.add_mutually_exclusive_group()
    service.add_argument("--install-service", dest="service", action="store_true")
    service.add_argument("--no-install-service", dest="service", action="store_false")
    parser.set_defaults(service=None)
    parser.add_argument("--allow-system-packages", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", dest="json_output", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--purge-data", action="store_true")
    parser.add_argument("--confirm-data-loss", action="store_true")
    for flag in _INTERNAL_FLAGS:
        parser.add_argument(flag, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin_isatty: bool | None = None,
    installer_factory: Callable[..., Installer] = Installer,
) -> int:
    """解析请求、运行 Installer 并映射稳定退出码 0/2/3。

    Args:
        argv: 省略时读取 ``sys.argv[1:]``。
        stdin_isatty: 测试可注入的 stdin TTY 事实。
        installer_factory: 测试可替换 Installer 构造器。

    Returns:
        成功为 0、请求/确认错误为 2、安装事务错误为 3。
    """
    raw = tuple(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw
    if raw in {("-h",), ("--help",)}:
        build_parser().print_help()
        return 0
    try:
        if any(raw.count(flag) != 1 for flag in _INTERNAL_FLAGS):
            raise _CliError("internal installer arguments must occur exactly once")
        parsed = build_parser().parse_args(raw)
        tty = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
        request = _request(parsed, tty)
        bootstrap = BootstrapInputs(
            manifest_file=Path(parsed.manifest_file),
            manifest_sha256=parsed.manifest_sha256,
            managed_uv=Path(parsed.managed_uv),
            managed_python_root=Path(parsed.managed_python_root),
            managed_python_executable=Path(parsed.managed_python_executable),
            public_argv=_public_argv(raw, parsed.action),
        )
        result = installer_factory(bootstrap).run(request)
    except (_CliError, argparse.ArgumentError, InstallError) as error:
        if isinstance(error, InstallError) and error.code != "request_invalid":
            event = InstallEvent("install.failed", "error", error.code, error.detail)
            emit_event(event, json_output=json_requested)
            return 3
        event = InstallEvent("install.failed", "error", "request_invalid", "manifest")
        emit_event(event, json_output=json_requested)
        return 2
    except (OSError, ValueError):
        event = InstallEvent("install.failed", "error", "installer_error", "manifest")
        emit_event(event, json_output=json_requested)
        return 3
    _emit_result(result, json_output=request.json_output)
    return 0


def _request(arguments: argparse.Namespace, stdin_isatty: bool) -> InstallRequest:
    """从 parser namespace 构造 strict InstallRequest 并闭合非 TTY 确认。"""
    if type(stdin_isatty) is not bool:
        raise _CliError("invalid tty state")
    if arguments.version is not None:
        prerelease = "-" in arguments.version.split("+", 1)[0]
        if prerelease != (arguments.channel == "dev"):
            raise _CliError("version and channel do not match")
    if (arguments.config is None) != (arguments.secrets_file is None):
        raise _CliError("config and Secret import must be paired")
    if arguments.onboard and arguments.config is not None:
        raise _CliError("imports require no-onboard")
    if not arguments.onboard and not stdin_isatty and arguments.config is None:
        raise _CliError("non-interactive no-onboard requires imports")
    if arguments.purge_data and not arguments.confirm_data_loss and not stdin_isatty:
        raise _CliError("non-interactive purge requires confirmation")
    if arguments.home:
        state_home = Path(arguments.home)
    elif arguments.system_prefix:
        state_home = Path(_invoking_account().pw_dir) / ".miniclaw"
    else:
        state_home = Path.home() / ".miniclaw"
    return InstallRequest(
        action=arguments.action,
        version=arguments.version,
        channel=arguments.channel,
        prefix=Path(arguments.prefix) if arguments.prefix else None,
        state_home=state_home,
        system_prefix=arguments.system_prefix,
        onboard=arguments.onboard,
        config_file=Path(arguments.config) if arguments.config else None,
        secrets_file=Path(arguments.secrets_file) if arguments.secrets_file else None,
        service=arguments.service,
        allow_system_packages=arguments.allow_system_packages,
        dry_run=arguments.dry_run,
        json_output=arguments.json_output,
        verbose=arguments.verbose,
        purge_data=arguments.purge_data,
        confirm_data_loss=arguments.confirm_data_loss,
    )


def _public_argv(argv: tuple[str, ...], action: str) -> tuple[str, ...]:
    """删除 internal flag/value 与可选 action，保留 public argv 原顺序。"""
    if action not in _ACTIONS:
        raise _CliError("invalid action")
    public: list[str] = []
    action_removed = False
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in _INTERNAL_FLAGS:
            index += 2
            continue
        if not action_removed and value == action:
            action_removed = True
            index += 1
            continue
        public.append(value)
        index += 1
    return tuple(public)


def _emit_result(result: InstallResult, *, json_output: bool) -> None:
    """按事务顺序输出安全 events，最后输出 strict structured target。"""
    if type(result) is not InstallResult:
        raise InstallError("installer_error", "manifest")
    for event in result.events:
        emit_event(event, json_output=json_output)
    platform = (
        None
        if result.platform is None
        else {"arch": result.platform.arch, "os": result.platform.os}
    )
    if json_output:
        document = {
            "action": result.action,
            "changed": result.changed,
            "event": "install.result",
            "platform": platform,
            "version": result.version,
        }
        print(
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )
        return
    platform_text = (
        "none" if result.platform is None else f"{result.platform.os}/{result.platform.arch}"
    )
    version = "none" if result.version is None else result.version
    changed = "true" if result.changed else "false"
    print(
        f"[RESULT] action={result.action} version={version} "
        f"platform={platform_text} changed={changed}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
