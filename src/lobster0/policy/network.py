"""HTTPS URL、DNS 公网地址与精确 authority 规范化。"""

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

type Resolver = Callable[[str, int], tuple[str, ...]]

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ENCODED_CONTROL = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", re.IGNORECASE)


IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class NetworkPolicyError(ValueError):
    """表示 URL、authority 或 DNS 结果命中 SSRF 安全边界。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class NetworkRule:
    """保存一个 lower-case hostname 与精确端口。"""

    hostname: str
    port: int = 443


@dataclass(frozen=True, slots=True)
class NetworkTarget:
    """保存规范 URL、TLS hostname、端口与已验证公网 IP。"""

    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]
    request_target: str

    @property
    def rule(self) -> NetworkRule:
        """返回当前目标的精确 authority。"""
        return NetworkRule(self.hostname, self.port)


def validate_https_target(
    url: str,
    resolver: Resolver | None = None,
    *,
    allowed_ports: tuple[int, ...] = (443,),
    allow_cidrs: tuple[IpNetwork, ...] = (),
) -> NetworkTarget:
    """规范 HTTPS URL，并要求每个 DNS 地址都是明确公网地址。

    Args:
        allow_cidrs: 用户显式声明的额外可信网段。fake-IP 模式的代理（Surge、
            Clash 等）会把所有域名解析到 ``198.18.0.0/15`` 这类保留段，真实流量
            由代理转发；不声明就会让 http_get 在这类机器上完全不可用。
            **豁免不能打开真正危险的地址**：回环、链路本地（含 169.254.169.254
            云元数据）、RFC1918 内网、组播与保留段永远拒绝，哪怕调用方写了
            ``0.0.0.0/0``。
    """
    if (
        not isinstance(url, str)
        or not url
        or len(url) > 8192
        or any(character.isspace() or ord(character) < 32 for character in url)
        or "\\" in url
        or _ENCODED_CONTROL.search(url)
    ):
        raise NetworkPolicyError("invalid_url", "URL is invalid")
    if not allowed_ports or any(
        type(port) is not int or not 1 <= port <= 65535 for port in allowed_ports
    ):
        raise ValueError("allowed_ports must contain valid TCP ports")
    try:
        parsed = urlsplit(url)
        hostname_value = parsed.hostname
        port = parsed.port or 443
    except ValueError:
        raise NetworkPolicyError("invalid_url", "URL authority is invalid") from None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname_value is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise NetworkPolicyError(
            "https_required",
            "only HTTPS URLs without credentials or fragments are allowed",
        )
    hostname = _normalize_hostname(hostname_value)
    if port not in allowed_ports:
        raise NetworkPolicyError("port_forbidden", "URL port is not allowed")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            answers = (resolver or default_resolver)(hostname, port)
        except (OSError, UnicodeError):
            raise NetworkPolicyError("dns_failed", "hostname could not be resolved") from None
    else:
        answers = (str(literal),)
    if not answers:
        raise NetworkPolicyError("dns_failed", "hostname did not resolve to an address")

    validated: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer)
        except ValueError:
            raise NetworkPolicyError("dns_failed", "hostname returned an invalid address") from None
        mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
        if not _is_reachable_address(address, allow_cidrs) or (
            mapped is not None and not _is_reachable_address(mapped, allow_cidrs)
        ):
            raise NetworkPolicyError(
                "non_public_address",
                "hostname resolved to a non-public address",
            )
        validated.add(address)
    addresses = tuple(
        str(address) for address in sorted(validated, key=lambda item: (item.version, int(item)))
    )
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    authority = host_text if port == 443 else f"{host_text}:{port}"
    path = parsed.path or "/"
    canonical_url = urlunsplit(("https", authority, path, parsed.query, ""))
    request_target = path + (f"?{parsed.query}" if parsed.query else "")
    return NetworkTarget(canonical_url, hostname, port, addresses, request_target)


def normalize_network_rule(value: str) -> NetworkRule:
    """规范配置或持久化的 `hostname[:port]` 精确规则，不做 DNS。"""
    if (
        not isinstance(value, str)
        or not value
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(character in value for character in "/?#\\@")
    ):
        raise NetworkPolicyError("invalid_host_rule", "hostname rule is invalid")
    try:
        parsed = urlsplit(f"//{value}")
        hostname_value = parsed.hostname
        port = parsed.port or 443
    except ValueError:
        raise NetworkPolicyError("invalid_host_rule", "hostname rule is invalid") from None
    if hostname_value is None or parsed.username is not None or parsed.password is not None:
        raise NetworkPolicyError("invalid_host_rule", "hostname rule is invalid")
    return NetworkRule(_normalize_hostname(hostname_value), port)


def default_resolver(hostname: str, port: int) -> tuple[str, ...]:
    """通过 SOCK_STREAM getaddrinfo 返回 DNS 地址，不建立连接。"""
    rows = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return tuple(row[4][0] for row in rows)


def _normalize_hostname(value: str) -> str:
    """拒绝 IDN、尾点、纯数字歧义和非标准 label。"""
    try:
        hostname = value.encode("ascii").decode("ascii").casefold()
    except UnicodeError:
        raise NetworkPolicyError("invalid_hostname", "hostname is invalid") from None
    if not hostname or hostname.endswith(".") or len(hostname) > 253 or "%" in hostname:
        raise NetworkPolicyError("invalid_hostname", "hostname is invalid")
    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        pass
    if all(character.isdigit() or character == "." for character in hostname):
        raise NetworkPolicyError("invalid_hostname", "hostname encoding is ambiguous")
    labels = hostname.split(".")
    if any(_HOST_LABEL.fullmatch(label) is None for label in labels):
        raise NetworkPolicyError("invalid_hostname", "hostname is invalid")
    return hostname


# 无论用户怎么配置都不放行的地址。SSRF 真正要防的就是这些：回环、链路本地
# （169.254.169.254 是云元数据端点）、RFC1918 内网、组播与保留段。
_NEVER_REACHABLE = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_reachable_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allow_cidrs: tuple[IpNetwork, ...],
) -> bool:
    """判断地址是否可访问：公网直接放行，其余只看显式豁免且不触红线。"""
    if _is_public_address(address):
        return True
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or any(address in network for network in _NEVER_REACHABLE)
    ):
        return False
    return any(address in network for network in allow_cidrs)


def _is_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """显式排除所有 SSRF 特殊类别，不只依赖版本差异较大的 is_global。"""
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )
