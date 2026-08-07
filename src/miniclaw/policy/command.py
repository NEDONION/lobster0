"""不经过 Shell 的 exact-argv 命令规范化与硬禁止规则。"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

SAFE_EXECUTABLE_PATH = os.pathsep.join(
    ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/usr/local/bin")
)
_FORBIDDEN_PROGRAMS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "dash",
        "ksh",
        "csh",
        "fish",
        "cmd",
        "powershell",
        "pwsh",
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "curl",
        "wget",
        "nc",
        "ncat",
        "telnet",
        "ftp",
        "sudo",
        "su",
        "doas",
        "env",
        "xargs",
        "docker",
        "podman",
        "nerdctl",
        "pip",
        "pip3",
        "npm",
        "yarn",
        "pnpm",
        "brew",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "pacman",
        "gem",
        "systemctl",
        "service",
        "launchctl",
        "mv",
        "cp",
        "truncate",
        "dd",
        "tee",
    }
)


class CommandPolicyError(ValueError):
    """表示命令无法安全规范化或命中不可审批的硬禁止。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class NormalizedCommand:
    """保存已解析 executable 和不经拼接的完整 argv。"""

    resolved_program: str
    args: tuple[str, ...]


def command_rule_is_persistable(command: NormalizedCommand) -> bool:
    """只允许不携带 inline AppleScript 的精确命令成为持久规则。"""
    name = Path(command.resolved_program).name.casefold()
    return not (name == "osascript" and "-e" in command.args)


def normalize_command(
    program: str,
    args: tuple[str, ...],
    workspace: Path,
) -> NormalizedCommand:
    """解析 executable，保留参数边界，并拒绝 Shell/删除/远程/提权动作。"""
    if not isinstance(program, str) or not program or _has_control(program):
        raise CommandPolicyError("invalid_command", "program is invalid")
    if not isinstance(args, tuple) or any(
        not isinstance(argument, str) or _has_control(argument) for argument in args
    ):
        raise CommandPolicyError("invalid_command", "command arguments are invalid")

    supplied = Path(program)
    if supplied.is_absolute() or len(supplied.parts) > 1:
        candidate = supplied if supplied.is_absolute() else workspace / supplied
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise CommandPolicyError("command_not_found", "program was not found") from None
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise CommandPolicyError("command_not_found", "program is not executable")
        resolved_program = str(resolved)
    else:
        found = shutil.which(program, path=SAFE_EXECUTABLE_PATH)
        if found is None:
            raise CommandPolicyError("command_not_found", "program was not found")
        try:
            resolved_program = str(Path(found).resolve(strict=True))
        except (OSError, RuntimeError):
            raise CommandPolicyError("command_not_found", "program was not found") from None

    name = Path(resolved_program).name.casefold()
    if name in _FORBIDDEN_PROGRAMS or name.startswith("pip3."):
        raise CommandPolicyError("command_forbidden", "program is not allowed")
    if _is_inline_evaluation(name, args):
        raise CommandPolicyError("command_forbidden", "inline code execution is not allowed")
    if name == "git" and _is_forbidden_git(args):
        raise CommandPolicyError("command_forbidden", "git operation is not allowed")
    return NormalizedCommand(resolved_program, args)


def _has_control(value: str) -> bool:
    """拒绝 NUL、换行及其他控制字符，避免日志和 argv 歧义。"""
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _is_inline_evaluation(name: str, args: tuple[str, ...]) -> bool:
    """拒绝常见解释器的字符串或模块执行开关。"""
    if name.startswith(("python", "pypy")):
        return any(argument in {"-c", "-m"} for argument in args)
    if name in {"node", "deno", "bun"}:
        return any(argument in {"-e", "--eval", "-p", "--print"} for argument in args)
    if name in {"ruby", "perl"}:
        return "-e" in args
    return name == "php" and "-r" in args


def _is_forbidden_git(args: tuple[str, ...]) -> bool:
    """拒绝上传和直接破坏工作树的 Git 子命令。"""
    return (
        any(argument in {"push", "clean", "config", "credential"} for argument in args)
        or "reset" in args
        and "--hard" in args
    )
