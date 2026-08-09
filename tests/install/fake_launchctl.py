"""提供 LaunchAgent lifecycle 的离线 exact runner。"""

from miniclaw.install.runtime import CommandResult


class FakeLaunchctlRunner:
    """记录 exact argv/env/timeout，并按顺序返回离线结果。"""

    def __init__(self, outcomes: tuple[object, ...] = ()) -> None:
        """保存预设结果；空队列默认成功。"""
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []
        self.outcomes = list(outcomes)

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        """记录一次调用，并返回下一项或零退出结果。"""
        self.calls.append((argv, dict(env), timeout))
        outcome = self.outcomes.pop(0) if self.outcomes else 0
        if isinstance(outcome, BaseException):
            raise outcome
        if type(outcome) is not int:
            return outcome  # type: ignore[return-value]
        return CommandResult(outcome, b"service\n" if outcome == 0 else b"", b"")
