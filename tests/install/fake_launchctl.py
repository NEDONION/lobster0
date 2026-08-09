"""提供 LaunchAgent lifecycle 的离线 exact runner。"""

from miniclaw.install.runtime import CommandResult


class FakeLaunchctlRunner:
    """记录 exact argv/env/timeout，并按顺序返回离线结果。"""

    def __init__(
        self,
        outcomes: tuple[object, ...] = (),
        *,
        active_target: str | None = None,
        enforce_manager_state: bool = False,
    ) -> None:
        """保存预设结果，并可模拟 active label collision。"""
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []
        self.outcomes = list(outcomes)
        self.active_target = active_target
        self.enforce_manager_state = enforce_manager_state

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        """记录一次调用，并返回下一项或零退出结果。"""
        self.calls.append((argv, dict(env), timeout))
        outcome = self.outcomes.pop(0) if self.outcomes else self._manager_outcome(argv)
        if isinstance(outcome, BaseException):
            raise outcome
        if type(outcome) is not int:
            return outcome  # type: ignore[return-value]
        return CommandResult(outcome, b"service\n" if outcome == 0 else b"", b"")

    def _manager_outcome(self, argv: tuple[str, ...]) -> int:
        """离线模拟 launchctl target collision 与显式状态迁移。"""
        if not self.enforce_manager_state or argv[0] != "/bin/launchctl":
            return 0
        action = argv[1]
        if action == "bootout":
            if len(argv) != 3 or argv[2] != self.active_target:
                return 3
            self.active_target = None
            return 0
        if action == "bootstrap":
            if self.active_target is not None:
                return 36
            self.active_target = f"{argv[2]}/io.miniclaw.gateway"
            return 0
        if action == "print":
            return 0 if len(argv) == 3 and argv[2] == self.active_target else 3
        return 0
