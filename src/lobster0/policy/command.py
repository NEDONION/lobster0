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
_FORBIDDEN_REMEDIES: tuple[tuple[frozenset[str], str], ...] = (
    (
        frozenset({"bash", "sh", "zsh", "dash", "ksh", "csh", "fish", "cmd", "powershell", "pwsh"}),
        "call the target program directly with exact argv; there is no shell to interpret pipes, "
        "redirection or globs",
    ),
    (
        frozenset({"env", "xargs", "sudo", "su", "doas"}),
        "call the target program directly instead of wrapping it",
    ),
    (
        frozenset({"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "telnet", "ftp"}),
        "use the http_get tool for HTTPS reads; outbound transfers are not available",
    ),
    (
        frozenset({"rm", "rmdir", "unlink", "shred", "mv", "cp", "truncate", "dd", "tee"}),
        "use write_file or edit_file to change workspace files",
    ),
    (
        frozenset(
            {
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
            }
        ),
        "installing packages is not available; work with what the environment already provides",
    ),
    (
        frozenset({"docker", "podman", "nerdctl", "systemctl", "service", "launchctl"}),
        "managing containers or system services is not available",
    ),
)
_INLINE_SWITCHES: tuple[tuple[tuple[str, ...], frozenset[str], bool], ...] = (
    (("python", "pypy"), frozenset({"-c", "-m"}), True),
    (("node", "deno", "bun"), frozenset({"-e", "--eval", "-p", "--print"}), False),
    (("ruby", "perl"), frozenset({"-e"}), False),
    (("php",), frozenset({"-r"}), False),
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
    *,
    executable_path: str = SAFE_EXECUTABLE_PATH,
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
        found = shutil.which(program, path=executable_path)
        if found is None:
            raise CommandPolicyError("command_not_found", "program was not found")
        try:
            resolved_program = str(Path(found).resolve(strict=True))
        except (OSError, RuntimeError):
            raise CommandPolicyError("command_not_found", "program was not found") from None

    name = Path(resolved_program).name.casefold()
    if name in _FORBIDDEN_PROGRAMS or name.startswith("pip3."):
        raise CommandPolicyError(
            "command_forbidden",
            f"'{name}' is permanently blocked and cannot be approved; {_forbidden_remedy(name)}",
        )
    switch = _inline_evaluation_switch(name, args)
    if switch is not None:
        raise CommandPolicyError(
            "command_forbidden",
            f"'{name} {switch}' runs inline code and is permanently blocked; drop {switch} and "
            "execute a script file instead, writing it with write_file first if needed",
        )
    if name == "git" and (subcommand := _forbidden_git_subcommand(args)) is not None:
        raise CommandPolicyError(
            "command_forbidden",
            f"'git {subcommand}' is permanently blocked and cannot be approved; read-only git "
            "commands such as status, diff and log are available",
        )
    return NormalizedCommand(resolved_program, args)


def _forbidden_remedy(name: str) -> str:
    """为硬禁止程序返回一句可执行的替代做法。"""
    for names, remedy in _FORBIDDEN_REMEDIES:
        if name in names:
            return remedy
    return "there is no approved way to run it"


def _has_control(value: str) -> bool:
    """拒绝 NUL、换行及其他控制字符，避免日志和 argv 歧义。"""
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _inline_evaluation_switch(name: str, args: tuple[str, ...]) -> str | None:
    """返回命中的解释器内联执行开关，便于把具体原因回给模型。"""
    for names, switches, by_prefix in _INLINE_SWITCHES:
        if not (name.startswith(names) if by_prefix else name in names):
            continue
        for argument in args:
            if argument in switches:
                return argument
    return None


def _forbidden_git_subcommand(args: tuple[str, ...]) -> str | None:
    """返回命中的被禁 Git 子命令，用于给出可读的拒绝原因。"""
    for argument in args:
        if argument in {"push", "clean", "config", "credential"}:
            return argument
    return "reset --hard" if "reset" in args and "--hard" in args else None
