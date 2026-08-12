"""Gateway 自重启：只在确实受托管时才退出，否则宁可什么都不做。

`/restart` 能救的只有"进程还活着、还在收消息、但状态卡住"的 Gateway。进程真的死了，
这条命令根本到不了这里，那是 supervisor 的职责。

关键事实来自 `src/lobster0/install/service.py`：systemd unit 是 `Restart=on-failure`，
launchd plist 是 `KeepAlive = {SuccessfulExit: false}`。两者都**只在非零退出时拉起**，
所以"优雅退出让 supervisor 拉起"必须以非零码退出；干净地 exit 0 会让服务永远停在那里。
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

type Supervision = Literal["systemd-user", "launchd", "declared"]

# EX_TEMPFAIL。必须非零，否则 systemd 的 Restart=on-failure 和 launchd 的
# KeepAlive.SuccessfulExit=false 都会认为服务正常结束，不再拉起。
RESTART_EXIT_CODE = 75

# 与 install/service.py 的 _LAUNCHD_LABEL 一致；精确相等才认，避免把终端里的
# 其他 launchd 作业误判成本进程的守护。
_LAUNCHD_LABEL = "io.lobster0.gateway"
_DEFAULT_GRACE_SECONDS = 3.0
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

_SUPERVISOR_LABELS: Mapping[str, str] = {
    "systemd-user": "systemd user service",
    "launchd": "launchd LaunchAgent",
    "declared": "外部进程守护（LOBSTER0_SUPERVISED）",
}


def detect_supervision(environ: Mapping[str, str]) -> Supervision | None:
    """判断当前进程是否由某个 supervisor 启动。

    只认可以肯定的证据；拿不准一律当作前台运行。误判成"受托管"会让 `/restart`
    把一个没人拉起的机器人直接关掉，代价远高于误判成"没托管"。

    Args:
        environ: 当前进程环境变量映射。

    Returns:
        受托管时返回具体形态，前台运行返回 ``None``。
    """
    declared = environ.get("LOBSTER0_SUPERVISED", "").strip().lower()
    if declared in _FALSE_VALUES:
        return None
    if declared in _TRUE_VALUES:
        return "declared"
    # launchd 为每个作业注入 XPC_SERVICE_NAME=<Label>；终端里通常是 "0" 或缺失。
    if environ.get("XPC_SERVICE_NAME", "").strip() == _LAUNCHD_LABEL:
        return "launchd"
    # systemd 为每次 unit 调用注入 INVOCATION_ID；交互式 shell 不会继承它。
    if environ.get("INVOCATION_ID", "").strip():
        return "systemd-user"
    return None


class GatewayRestartController:
    """把 Owner 的 `/restart` 转成一次有宽限期、非零退出的自愿关停。"""

    def __init__(
        self,
        *,
        shutdown_event: asyncio.Event,
        supervision: Supervision | None,
        grace_seconds: float = _DEFAULT_GRACE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """绑定关停信号、托管形态与可注入的宽限期实现。

        Args:
            shutdown_event: Gateway 用于触发有序关停的同一个事件。
            supervision: ``detect_supervision`` 的结论；``None`` 表示前台运行。
            grace_seconds: 先把确认写进 Outbox、再触发关停之间的宽限秒数。
            sleep: 可注入的等待实现，测试用它避免真的 sleep。

        Raises:
            ValueError: 宽限期不是非负有限数。
        """
        if type(grace_seconds) not in {int, float} or grace_seconds < 0:
            raise ValueError("grace_seconds must be a non-negative number")
        self._shutdown_event = shutdown_event
        self._supervision = supervision
        self._grace_seconds = float(grace_seconds)
        self._sleep = sleep
        self._requested = False
        self._task: asyncio.Task[None] | None = None

    @property
    def supervision(self) -> Supervision | None:
        """返回当前托管形态；``None`` 表示没有任何守护。"""
        return self._supervision

    @property
    def restart_requested(self) -> bool:
        """返回本进程是否已经因为 `/restart` 进入自愿关停。"""
        return self._requested

    @property
    def exit_code(self) -> int:
        """返回进程应使用的退出码：自愿重启为非零，其余为 0。"""
        return RESTART_EXIT_CODE if self._requested else 0

    def request(self) -> str:
        """处理一次 `/restart`，返回要发回聊天的说明文本。

        受托管时先返回确认、再由后台任务在宽限期后触发关停；没有守护时**不做任何
        关停动作**——前台进程退出等于把机器人关掉，比不动更糟。

        Returns:
            可直接投递到 IM 的中文说明，不含任何 Secret 或平台标识。
        """
        if self._supervision is None:
            return (
                "已收到 /restart，但当前进程没有任何守护（看起来是在终端前台运行）。"
                "主动退出等于直接把我关掉，比不动更糟，所以这次不执行。\n"
                "请在启动它的终端里 Ctrl-C 后重新运行 `lobster0 gateway`；"
                "或者用 `lobster0 service install` 交给 systemd/launchd 托管，"
                "之后 /restart 才会生效。\n"
                "另外说明：/restart 只能救「进程还活着但卡住」的情况；"
                "进程真的死了，这条消息根本到不了我这里。"
            )
        if self._requested:
            return "已经在重启流程里了，正在等待当前任务收尾，请稍候。"
        self._requested = True
        self._task = asyncio.get_running_loop().create_task(
            self._shutdown_after_grace(),
            name="gateway-restart",
        )
        label = _SUPERVISOR_LABELS[self._supervision]
        return (
            f"已收到 /restart。当前进程由 {label} 托管，我会在 "
            f"{self._grace_seconds:.0f} 秒后主动退出（退出码 {RESTART_EXIT_CODE}），"
            "由它重新拉起。\n"
            "已经开始的 Turn 不会被自动重放，还没发出的回复留在 Outbox，"
            "重启后会继续发送。\n"
            "另外说明：/restart 只能救「进程还活着但卡住」的情况；"
            "进程真的死了，这条消息根本到不了我这里，那是 supervisor 的职责。"
        )

    async def close(self) -> None:
        """收走仍在宽限期里的关停任务。

        SIGTERM 和 `/restart` 撞在一起时，Gateway 会先因为信号退出；这时宽限任务还挂
        在事件循环上，不收走就会变成一个被销毁的 pending Task。
        """
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def wait_for_shutdown_request(self) -> None:
        """等待已排程的关停任务结束；没有排程时立即返回。"""
        if self._task is not None:
            await self._task

    async def _shutdown_after_grace(self) -> None:
        """先给确认留出投递时间，再触发 Gateway 的正常有序关停。"""
        await self._sleep(self._grace_seconds)
        self._shutdown_event.set()
