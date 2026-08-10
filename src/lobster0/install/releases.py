"""解析 fixed、stable 与 bounded GitHub dev Release 来源。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, Never

from lobster0.install.artifacts import _Opener, _read_bounded_url
from lobster0.install.models import InstallError, _parse_semver

_API_BYTES = 1_048_576
_API_ROWS = 20
_API_URL = "https://api.github.com/repos/NEDONION/lobster0/releases?per_page=20"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_VERSION_CHARS = 128
_MAX_NUMERIC_IDENTIFIER_DIGITS = 18


@dataclass(frozen=True, slots=True)
class ReleaseSource:
    """保存一个已收窄的 manifest 来源和可选嵌入 hash。

    Args:
        channel: stable 或显式 prerelease dev 通道。
        requested_version: 规范 SemVer；stable latest 时为 None。
        manifest_url: exact Lobster0 GitHub Release manifest URL。
        expected_sha256: bootstrap 可选提供的 lowercase SHA-256。

    Raises:
        InstallError: 通道、版本、URL 或 hash 关系无效。
    """

    channel: Literal["stable", "dev"]
    requested_version: str | None
    manifest_url: str
    expected_sha256: str | None

    def __post_init__(self) -> None:
        """把 channel/version 与 exact Release URL 绑定。"""
        if type(self.channel) is not str or self.channel not in {"stable", "dev"}:
            _invalid()
        if self.requested_version is None:
            if self.channel != "stable":
                _invalid()
            expected_url = (
                "https://github.com/NEDONION/lobster0/releases/latest/download/"
                "release-manifest.json"
            )
        else:
            parsed = _parse_release_semver(self.requested_version)
            prerelease = parsed[3]
            if (self.channel == "stable" and prerelease is not None) or (
                self.channel == "dev" and prerelease is None
            ):
                _invalid()
            expected_url = (
                "https://github.com/NEDONION/lobster0/releases/download/"
                f"v{self.requested_version}/release-manifest.json"
            )
        if type(self.manifest_url) is not str or self.manifest_url != expected_url:
            _invalid()
        if self.expected_sha256 is not None and (
            type(self.expected_sha256) is not str
            or _HASH.fullmatch(self.expected_sha256) is None
        ):
            _invalid()


def resolve_release_source(
    channel: Literal["stable", "dev"],
    version: str | None,
    opener: _Opener | None = None,
) -> ReleaseSource:
    """把 fixed/stable/dev 请求收窄为一个 exact manifest source。

    Args:
        channel: stable 或 dev。
        version: 显式规范 SemVer；None 选择 stable latest 或最新 dev prerelease。
        opener: dev API discovery 使用的离线可替换 opener。

    Returns:
        已绑定通道、版本和 exact GitHub URL 的 ReleaseSource。

    Raises:
        InstallError: 请求无效、API 失败/越界或没有唯一可信 Release。
    """
    if type(channel) is not str or channel not in {"stable", "dev"}:
        _invalid()
    if version is not None:
        if type(version) is not str:
            _invalid()
        return ReleaseSource(
            channel,
            version,
            (
                "https://github.com/NEDONION/lobster0/releases/download/"
                f"v{version}/release-manifest.json"
            ),
            None,
        )
    if channel == "stable":
        return ReleaseSource(
            "stable",
            None,
            "https://github.com/NEDONION/lobster0/releases/latest/download/release-manifest.json",
            None,
        )
    data = _read_bounded_url(
        _API_URL,
        _API_BYTES,
        opener,
        code="manifest_invalid",
        allow_api_query=True,
    )
    releases = _parse_api(data)
    candidates = tuple(_parse_candidate(row) for row in releases)
    versions = [candidate[0] for candidate in candidates if candidate is not None]
    if len(versions) != len(set(versions)):
        _invalid()
    selected = max(
        (candidate for candidate in candidates if candidate is not None),
        key=_sort_key,
        default=None,
    )
    if selected is None:
        _invalid()
    version_text, manifest_url = selected
    return ReleaseSource("dev", version_text, manifest_url, None)


def _parse_api(data: bytes) -> list[object]:
    """解析无 duplicate keys 且最多 20 行的 GitHub Releases JSON。"""
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: _invalid(),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        raise InstallError("manifest_invalid", "manifest") from error
    if type(document) is not list or len(document) > _API_ROWS:
        _invalid()
    return document


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝 JSON object duplicate keys，避免安全字段被覆盖。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _parse_candidate(row: object) -> tuple[str, str] | None:
    """验证一行 GitHub Release，并返回可选 dev prerelease。"""
    if type(row) is not dict:
        _invalid()
    required = {"tag_name", "draft", "prerelease", "html_url", "assets"}
    if not required.issubset(row):
        _invalid()
    tag = row["tag_name"]
    draft = row["draft"]
    prerelease_flag = row["prerelease"]
    if type(tag) is not str or type(draft) is not bool or type(prerelease_flag) is not bool:
        _invalid()
    if not tag.startswith("v"):
        _invalid()
    version = tag[1:]
    parsed = _parse_release_semver(version)
    if parsed[4] is not None or (parsed[3] is None) != (not prerelease_flag):
        _invalid()
    if draft or not prerelease_flag:
        return None
    expected_html = f"https://github.com/NEDONION/lobster0/releases/tag/{tag}"
    if type(row["html_url"]) is not str or row["html_url"] != expected_html:
        _invalid()
    assets = row["assets"]
    if type(assets) is not list or len(assets) > 256:
        _invalid()
    manifests = []
    for asset in assets:
        if type(asset) is not dict or "name" not in asset or "browser_download_url" not in asset:
            _invalid()
        if asset["name"] == "release-manifest.json":
            manifests.append(asset)
    if len(manifests) != 1:
        _invalid()
    url = manifests[0]["browser_download_url"]
    expected_url = (
        "https://github.com/NEDONION/lobster0/releases/download/"
        f"{tag}/release-manifest.json"
    )
    if type(url) is not str or url != expected_url:
        _invalid()
    return version, url


def _sort_key(
    candidate: tuple[str, str],
) -> tuple[int, int, int, tuple[tuple[int, int | str], ...]]:
    """返回遵循 SemVer prerelease numeric/alphanumeric 规则的排序 key。"""
    parsed = _parse_release_semver(candidate[0])
    prerelease = parsed[3]
    if prerelease is None:
        _invalid()
    identifiers = tuple(
        (0, int(item)) if item.isdecimal() else (1, item)
        for item in prerelease.split(".")
    )
    return parsed[0], parsed[1], parsed[2], identifiers


def _invalid() -> Never:
    """抛出统一且不含远端 body 的 manifest 错误。"""
    raise InstallError("manifest_invalid", "manifest")


def _parse_release_semver(
    value: object,
) -> tuple[int, int, int, str | None, str | None]:
    """在 int 转换前限制 Release SemVer 总长与 numeric identifier 长度。"""
    if type(value) is not str or not 1 <= len(value) <= _MAX_VERSION_CHARS:
        _invalid()
    semantic = value.split("+", 1)[0]
    core, separator, prerelease = semantic.partition("-")
    identifiers = core.split(".")
    if separator:
        identifiers.extend(prerelease.split("."))
    if any(
        identifier.isdecimal() and len(identifier) > _MAX_NUMERIC_IDENTIFIER_DIGITS
        for identifier in identifiers
    ):
        _invalid()
    try:
        return _parse_semver(value, "version")
    except (InstallError, ValueError, OverflowError) as error:
        raise InstallError("manifest_invalid", "manifest") from error
