"""在回环默认与显式 token 前提下启动 Web 控制台的 Node server。

与 `tui_launcher` 同构，但刻意不提供任何 fallback：Web 控制台没有降级形态，
条件不满足就报错退出，而不是静默地少一层保护继续运行。
"""

import ipaddress
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from lobster0.paths import StatePaths

DEFAULT_WEB_PORT = 4180
# token 只在非回环绑定时才需要；32 字符是「猜不出来」的下限，短于此等于没有保护。
MINIMUM_TOKEN_LENGTH = 32
_SUPPORTED_NODE_MESSAGE = "Node.js 22.22.3～<23 或 24.15.0～<25"
_NODE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_NODE_INJECTION_VARIABLES = ("NODE_OPTIONS", "NODE_PATH")
# 只接受无歧义的回环写法。主机名要靠 DNS 才知道绑到哪，非点分整型写法在不同
# inet_aton 实现下含义不同，两者都不能用来判断「这个绑定是否对网络可达」。
_LOOPBACK_NAMES = frozenset({"localhost"})

HostClass = Literal["loopback", "public"]


class WebLaunchError(RuntimeError):
    """表示 Web 控制台无法在满足既定保护的前提下启动。"""


@dataclass(frozen=True, slots=True)
class WebBindPlan:
    """决定「谁能访问这台 Agent」的唯一数据结构。"""

    host: str
    port: int
    token: str | None

    @property
    def public(self) -> bool:
        """非回环绑定为 True；此时 token 必然已存在。"""
        return classify_host(self.host) == "public"


@dataclass(frozen=True, slots=True)
class WebConsoleInspection:
    """保存 Node、构建入口和一条可操作问题。"""

    node: Path | None
    node_version: tuple[int, int, int] | None
    entry: Path | None
    problem: str | None

    @property
    def ready(self) -> bool:
        """只有 Node 与入口均满足要求时返回 True。"""
        return self.node is not None and self.entry is not None and self.problem is None


def is_supported_node_version(version: tuple[int, int, int]) -> bool:
    """判断 Node 三段版本是否落入已验证的 22/24 LTS 区间。

    Args:
        version: Node 的 major、minor、patch 三段非负整数。

    Returns:
        仅当版本属于 22.22.3～22.x 或 24.15.0～24.x 时返回 True。
    """
    if (
        type(version) is not tuple
        or len(version) != 3
        or any(type(part) is not int or part < 0 for part in version)
    ):
        return False
    return (22, 22, 3) <= version < (23, 0, 0) or (24, 15, 0) <= version < (25, 0, 0)


def classify_host(host: str) -> HostClass:
    """把绑定地址判定为回环或对网络可达。

    Args:
        host: 调用方给出的绑定地址；只接受 `localhost` 或 IP 字面量。

    Returns:
        `"loopback"` 表示非本机流量到不了，`"public"` 表示需要 token 保护。

    Raises:
        WebLaunchError: 地址不是无歧义的字面量，无法判断其可达性。
    """
    if type(host) is not str or host != host.strip() or not host:
        raise WebLaunchError("Web 控制台的 --host 必须是 localhost 或 IP 字面量")
    if host in _LOOPBACK_NAMES:
        return "loopback"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise WebLaunchError(
            f"Web 控制台无法判断 {host!r} 绑到哪里；--host 只接受 localhost 或 IP 字面量"
        ) from None
    return "loopback" if address.is_loopback else "public"


def resolve_bind_plan(
    host: str | None,
    port: int | None,
    environ: Mapping[str, str],
) -> WebBindPlan:
    """解析绑定计划，并在缺少保护时拒绝对网络可达的绑定。

    Args:
        host: 显式绑定地址；None 表示使用回环默认值。
        port: 显式端口；None 表示使用默认端口。
        environ: 读取 `LOBSTER0_WEB_TOKEN` 的环境；不会隐式合并进程环境。

    Returns:
        已校验的绑定计划。

    Raises:
        WebLaunchError: 端口越界，或非回环绑定缺少足够长的 token。
    """
    resolved_host = "127.0.0.1" if host is None else host
    resolved_port = DEFAULT_WEB_PORT if port is None else port
    if (
        type(resolved_port) is not int
        or resolved_port < 1024
        or resolved_port > 65_535
    ):
        raise WebLaunchError("Web 控制台端口必须落在 1024～65535")

    host_class = classify_host(resolved_host)
    token = environ.get("LOBSTER0_WEB_TOKEN", "").strip()
    if host_class == "loopback":
        # 回环绑定不需要凭据；即使环境里有 token 也不改变语义，因此不带上它。
        return WebBindPlan(resolved_host, resolved_port, None)
    if len(token) < MINIMUM_TOKEN_LENGTH:
        raise WebLaunchError(
            f"绑定 {resolved_host} 会让这台 Agent 对网络可达，"
            f"必须先在环境变量 LOBSTER0_WEB_TOKEN 里提供至少 "
            f"{MINIMUM_TOKEN_LENGTH} 个字符的共享密钥；"
            "不提供则拒绝启动，不会回退到回环"
        )
    return WebBindPlan(resolved_host, resolved_port, token)


def inspect_web_console(environ: Mapping[str, str] | None = None) -> WebConsoleInspection:
    """只读检查 Web 控制台所需的 Node 版本和构建产物。

    Args:
        environ: 可覆盖 `LOBSTER0_NODE`、`LOBSTER0_WEB_ENTRY` 和 PATH 的环境。

    Returns:
        包含已解析路径、版本和第一条可操作问题的检查结果。
    """
    source = os.environ if environ is None else environ
    node_environment = _sanitized_node_environment(source)
    configured_node = source.get("LOBSTER0_NODE", "").strip()
    resolved_node = configured_node or shutil.which("node", path=source.get("PATH"))
    if not resolved_node:
        return WebConsoleInspection(
            None,
            None,
            None,
            f"没有找到 Node.js；Web 控制台需要 {_SUPPORTED_NODE_MESSAGE}",
        )
    node = Path(resolved_node).expanduser().resolve(strict=False)
    version = _read_node_version(node, node_environment)
    if version is None:
        return WebConsoleInspection(node, None, None, "无法读取 Node.js 版本")
    if not is_supported_node_version(version):
        current = ".".join(str(part) for part in version)
        return WebConsoleInspection(
            node,
            version,
            None,
            f"Web 控制台需要 {_SUPPORTED_NODE_MESSAGE}；当前为 {current}",
        )

    configured_entry = source.get("LOBSTER0_WEB_ENTRY", "").strip()
    entry = (
        Path(configured_entry).expanduser().resolve(strict=False)
        if configured_entry
        else Path(__file__).resolve().parents[2] / "desktop/out/web/server/index.js"
    )
    if not entry.is_file():
        return WebConsoleInspection(
            node,
            version,
            None,
            f"Web 控制台尚未构建：{entry}；请运行 pnpm --dir desktop run build:web",
        )
    return WebConsoleInspection(node, version, entry, None)


def run_web_console(
    paths: StatePaths,
    *,
    host: str | None = None,
    port: int | None = None,
    environ: Mapping[str, str] | None = None,
    stderr: TextIO | None = None,
) -> int:
    """启动 Web 控制台 Node server 并阻塞直到它退出。

    Args:
        paths: 当前 Lobster0 状态路径。
        host: 显式绑定地址；None 表示回环默认值。
        port: 显式端口；None 表示默认端口。
        environ: Node 路径、构建入口与 token 的来源；默认当前环境。
        stderr: 输出非回环警告的流；默认进程 stderr。

    Returns:
        Node server 的退出码。

    Raises:
        WebLaunchError: 状态未初始化、绑定缺少保护、Node 不满足要求或进程无法启动。
    """
    source = os.environ if environ is None else environ
    error_stream = sys.stderr if stderr is None else stderr
    if not paths.config.is_file():
        raise WebLaunchError("Web 控制台需要已初始化的状态；请先运行 lobster0 init")

    # 绑定计划先于任何进程创建：缺 token 时一个 Node 进程都不该起来。
    plan = resolve_bind_plan(host, port, source)
    inspection = inspect_web_console(source)
    if not inspection.ready:
        assert inspection.problem is not None
        raise WebLaunchError(inspection.problem)
    assert inspection.node is not None and inspection.entry is not None

    if plan.public:
        # token 只用于判断「有没有保护」，值本身绝不进日志。
        print(
            f"warning: Web 控制台正在绑定 {plan.host}:{plan.port}，"
            "这台 Agent 现在对网络可达；它可以执行命令、读写文件并驱动浏览器。"
            "推荐改用 SSH 端口转发而不是直接暴露。",
            file=error_stream,
        )

    child_env = _sanitized_node_environment(source)
    child_env.update(
        {
            "LOBSTER0_HOME": str(paths.home),
            "LOBSTER0_NODE": str(inspection.node),
            "LOBSTER0_PYTHON": sys.executable,
            "LOBSTER0_WEB_ENTRY": str(inspection.entry),
            "LOBSTER0_WEB_HOST": plan.host,
            "LOBSTER0_WEB_PORT": str(plan.port),
        }
    )
    if plan.token is None:
        child_env.pop("LOBSTER0_WEB_TOKEN", None)
    else:
        child_env["LOBSTER0_WEB_TOKEN"] = plan.token
    try:
        completed = subprocess.run(
            [str(inspection.node), str(inspection.entry)],
            env=child_env,
            check=False,
            shell=False,
        )
    except OSError:
        raise WebLaunchError("Web 控制台进程无法启动") from None
    return int(completed.returncode)


def _sanitized_node_environment(source: Mapping[str, str]) -> dict[str, str]:
    """复制调用方环境并移除可向 Node 注入代码或全局模块的变量。

    Args:
        source: 调用方明确提供的环境；不会隐式合并进程环境。

    Returns:
        不含 Node 注入变量的独立环境字典。
    """
    environment = dict(source)
    for name in _NODE_INJECTION_VARIABLES:
        environment.pop(name, None)
    return environment


def _read_node_version(
    node: Path,
    environment: Mapping[str, str],
) -> tuple[int, int, int] | None:
    """在两秒预算内用调用方封闭环境读取 Node 的三段语义版本。

    Args:
        node: 要探测的显式 Node executable。
        environment: 已由调用方清洗且不会隐式扩展的环境。

    Returns:
        有效的三段版本；启动、超时、退出或格式异常时返回 None。
    """
    try:
        completed = subprocess.run(
            [str(node), "--version"],
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    matched = _NODE_VERSION.fullmatch(completed.stdout.strip())
    if matched is None:
        return None
    return tuple(int(part) for part in matched.groups())
