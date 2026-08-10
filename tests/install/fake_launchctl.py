"""提供 LaunchAgent lifecycle 的离线 exact runner。"""

from lobster0.install.runtime import CommandResult


class FakeLaunchctlRunner:
    """记录 exact argv/env/timeout，并按顺序返回离线结果。"""

    def __init__(
        self,
        outcomes: tuple[object, ...] = (),
        *,
        active_target: str | None = None,
        enforce_manager_state: bool = False,
        side_effecting_calls: frozenset[int] = frozenset(),
    ) -> None:
        """保存结果，并可让指定失败调用仍产生真实 manager side effect。"""
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []
        self.outcomes = list(outcomes)
        self.active_target = active_target
        self.enforce_manager_state = enforce_manager_state
        self.side_effecting_calls = side_effecting_calls

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        """记录一次调用，并返回下一项或零退出结果。"""
        call_index = len(self.calls)
        self.calls.append((argv, dict(env), timeout))
        explicit = bool(self.outcomes)
        outcome = self.outcomes.pop(0) if explicit else 0
        if (
            self.enforce_manager_state
            and argv[0] == "/bin/launchctl"
            and (not explicit or outcome == 0 or call_index in self.side_effecting_calls)
        ):
            manager_outcome = self._manager_outcome(argv)
            if not explicit or outcome == 0 and manager_outcome != 0:
                outcome = manager_outcome
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
            self.active_target = f"{argv[2]}/io.lobster0.gateway"
            return 0
        if action == "print":
            return 0 if len(argv) == 3 and argv[2] == self.active_target else 3
        return 0
