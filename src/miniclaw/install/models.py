"""定义安装器使用的 strict immutable Release 与请求模型。"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never
from urllib.parse import urlsplit

_MAX_MANIFEST_BYTES = 1_048_576
_MAX_ARTIFACT_BYTES = 1_073_741_824
_MAX_ARTIFACTS = 128
_DATABASE_SCHEMA_VERSION = 5
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FILENAME = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,127}$")
_LICENSE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,127}$")
_STABLE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[-_]?key|token|secret|password)\s*[:=]\s*\S+"
)
_URL_TEXT = re.compile(r"(?i)https?://\S+")
_PRIVATE_PATH = re.compile(r"(?<![A-Za-z0-9])(?:/[^\s]+|[A-Za-z]:\\[^\s]+)")

_ARTIFACT_KINDS = {
    "wheel",
    "sdist",
    "requirements",
    "node",
    "tui",
    "sandbox-image",
    "runtime-image",
    "installer",
    "sbom",
}
_FEATURES = {
    "agent",
    "tools",
    "memory",
    "skills",
    "evolution",
    "tui",
    "feishu",
    "telegram",
    "discord",
    "gateway",
    "automation",
    "heartbeat",
    "sandbox",
    "checkpoint",
    "rollback",
}
_SUPPORTED_PLATFORMS = {
    ("linux", "x86_64"),
    ("linux", "arm64"),
    ("macos", "x86_64"),
    ("macos", "arm64"),
}
_SOURCE_REPOSITORIES = {
    "https://github.com/NEDONION/miniclaw",
    "https://github.com/astral-sh/uv",
    "https://github.com/nodejs/node",
    "https://github.com/earendil-works/pi-tui",
}
_GITHUB_DOWNLOAD_REPOSITORIES = {
    ("NEDONION", "miniclaw"),
    ("astral-sh", "uv"),
}
_EXPECTED_NODE_RANGES = (
    ((22, 22, 3), (23, 0, 0)),
    ((24, 15, 0), (25, 0, 0)),
)

ArtifactKind = Literal[
    "wheel",
    "sdist",
    "requirements",
    "node",
    "tui",
    "sandbox-image",
    "runtime-image",
    "installer",
    "sbom",
]


class InstallError(RuntimeError):
    """表示带稳定代码且 detail 已脱敏和截断的安装失败。

    Args:
        code: 供调用方分支处理的稳定错误代码。
        detail: 不可信诊断文本；构造时会移除常见凭据、URL 和私人路径。
    """

    def __init__(self, code: str, detail: str) -> None:
        """保存稳定代码和不超过 500 字符的安全详情。"""
        self.code = (
            code
            if type(code) is str and _STABLE_NAME.fullmatch(code)
            else "installer_error"
        )
        self.detail = _safe_detail(detail)
        super().__init__(f"{self.code}: {self.detail}")


@dataclass(frozen=True, slots=True)
class PlatformKey:
    """标识一个 Release artifact 目标。

    Args:
        os: Release 使用的规范操作系统名或通用标记。
        arch: Release 使用的规范架构名或通用标记。

    Raises:
        InstallError: OS/架构不受支持或只使用了一半通用标记。
    """

    os: Literal["linux", "macos", "any"]
    arch: Literal["x86_64", "arm64", "any"]

    def __post_init__(self) -> None:
        """拒绝 unsupported 或含糊的平台组合。"""
        if type(self.os) is not str or type(self.arch) is not str:
            _invalid("platform")
        if self.os not in {"linux", "macos", "any"} or self.arch not in {
            "x86_64",
            "arm64",
            "any",
        }:
            _invalid("platform")
        if (self.os == "any") != (self.arch == "any"):
            _invalid("platform")


@dataclass(frozen=True, slots=True)
class NodeRange:
    """限制一个受支持 Node major 的最小和排他上界。

    Args:
        minimum: 可接受的最小三段版本。
        maximum_exclusive: 不再接受的排他三段上界。

    Raises:
        InstallError: 版本元组不规范或范围为空。
    """

    minimum: tuple[int, int, int]
    maximum_exclusive: tuple[int, int, int]

    def __post_init__(self) -> None:
        """校验三段整数与严格递增边界。"""
        for value in (self.minimum, self.maximum_exclusive):
            if (
                type(value) is not tuple
                or len(value) != 3
                or any(type(part) is not int or part < 0 for part in value)
            ):
                _invalid("node.accepted")
        if self.minimum >= self.maximum_exclusive:
            _invalid("node.accepted")


@dataclass(frozen=True, slots=True)
class NodePolicy:
    """保存默认 Node 与允许复用的两个 LTS 范围。

    Args:
        default: Release 默认安装的 Node 三段版本。
        accepted: 严格按 major 排列的两个允许范围。

    Raises:
        InstallError: 默认值或允许范围偏离当前发布策略。
    """

    default: tuple[int, int, int]
    accepted: tuple[NodeRange, ...]

    def __post_init__(self) -> None:
        """固定 v0.7 的 Node 24 默认值和 22/24 LTS 范围。"""
        if (
            self.default != (24, 18, 0)
            or type(self.accepted) is not tuple
            or any(type(item) is not NodeRange for item in self.accepted)
        ):
            _invalid("node")
        ranges = tuple((item.minimum, item.maximum_exclusive) for item in self.accepted)
        if ranges != _EXPECTED_NODE_RANGES:
            _invalid("node.accepted")


@dataclass(frozen=True, slots=True)
class Artifact:
    """描述一个有 size/hash/source/license 约束的 Release 文件。

    Args:
        kind: Release 文件的封闭组件类别。
        filename: 不含目录的安全文件名。
        url: 无凭据、query 或 fragment 的 allowlisted HTTPS URL。
        sha256: 文件的 lowercase SHA-256。
        size: 期望文件字节数。
        media_type: 规范小写媒体类型。
        platform: 文件适用的平台。
        component_version: 组件的规范 SemVer。
        source_repository: allowlisted 上游 GitHub 仓库。
        license_ref: SPDX ID 或稳定 LicenseRef。
        upstream_sha256: 有上游二进制时的 lowercase SHA-256。

    Raises:
        InstallError: 任一字段违反 manifest 信任边界。
    """

    kind: ArtifactKind
    filename: str
    url: str
    sha256: str
    size: int
    media_type: str
    platform: PlatformKey
    component_version: str
    source_repository: str
    license_ref: str
    upstream_sha256: str | None

    def __post_init__(self) -> None:
        """在对象进入 ReleaseManifest 前再次校验所有字段。"""
        if type(self.kind) is not str or self.kind not in _ARTIFACT_KINDS:
            _invalid("artifacts.kind")
        if type(self.filename) is not str or _FILENAME.fullmatch(self.filename) is None:
            _invalid("artifacts.filename")
        _validate_artifact_url(self.url)
        _require_hash(self.sha256, "artifacts.sha256")
        if type(self.size) is not int or not 1 <= self.size <= _MAX_ARTIFACT_BYTES:
            _invalid("artifacts.size")
        if type(self.media_type) is not str or _MEDIA_TYPE.fullmatch(self.media_type) is None:
            _invalid("artifacts.media_type")
        if type(self.platform) is not PlatformKey:
            _invalid("artifacts.platform")
        _parse_semver(self.component_version, "artifacts.component_version")
        if (
            type(self.source_repository) is not str
            or self.source_repository not in _SOURCE_REPOSITORIES
        ):
            _invalid("artifacts.source_repository")
        if type(self.license_ref) is not str or _LICENSE.fullmatch(self.license_ref) is None:
            _invalid("artifacts.license_ref")
        if self.upstream_sha256 is not None:
            _require_hash(self.upstream_sha256, "artifacts.upstream_sha256")


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """保存一个已完成 strict schema 校验的同源 Release。

    Args:
        schema_version: 当前只接受版本 1。
        product: 固定产品标识 miniclaw。
        version: Release 的规范 SemVer。
        git_commit: 绑定 Release 的 lowercase 40-hex commit。
        python: 固定 Python 3.12 系列。
        node: 固定 Node LTS 策略。
        artifacts: 最多 128 个唯一 Release 文件。
        supported_platforms: 四个 Tier 1 OS/arch 组合。
        features: 当前实现能力的封闭集合。
        database_schema: 当前 Runtime 写入的 schema 版本。
        minimum_readable_schema: 当前 Runtime 可读取的最早 schema 版本。

    Raises:
        InstallError: 字段、关系或唯一性约束无效。
    """

    schema_version: Literal[1]
    product: Literal["miniclaw"]
    version: str
    git_commit: str
    python: Literal["3.12"]
    node: NodePolicy
    artifacts: tuple[Artifact, ...]
    supported_platforms: tuple[PlatformKey, ...]
    features: tuple[str, ...]
    database_schema: int
    minimum_readable_schema: int

    def __post_init__(self) -> None:
        """闭合 Release 级版本、平台、数据库和唯一性关系。"""
        if type(self.schema_version) is not int or self.schema_version != 1:
            _invalid("schema_version")
        if self.product != "miniclaw" or self.python != "3.12":
            _invalid("product")
        _parse_semver(self.version, "version")
        if type(self.git_commit) is not str or _COMMIT.fullmatch(self.git_commit) is None:
            _invalid("git_commit")
        if type(self.node) is not NodePolicy:
            _invalid("node")
        if (
            type(self.artifacts) is not tuple
            or not 1 <= len(self.artifacts) <= _MAX_ARTIFACTS
            or any(type(item) is not Artifact for item in self.artifacts)
        ):
            _invalid("artifacts")
        filenames = [item.filename for item in self.artifacts]
        identities = [
            (item.kind, item.platform, item.component_version, item.media_type)
            for item in self.artifacts
        ]
        if len(filenames) != len(set(filenames)) or len(identities) != len(set(identities)):
            _invalid("artifacts")
        self._validate_release_urls()
        if type(self.supported_platforms) is not tuple or any(
            type(item) is not PlatformKey for item in self.supported_platforms
        ):
            _invalid("supported_platforms")
        platforms = [(item.os, item.arch) for item in self.supported_platforms]
        if len(platforms) != 4 or set(platforms) != _SUPPORTED_PLATFORMS:
            _invalid("supported_platforms")
        if (
            type(self.features) is not tuple
            or any(type(item) is not str or item not in _FEATURES for item in self.features)
        ):
            _invalid("features")
        if len(self.features) != len(set(self.features)):
            _invalid("features")
        if (
            type(self.database_schema) is not int
            or self.database_schema != _DATABASE_SCHEMA_VERSION
        ):
            _invalid("database_schema")
        if (
            type(self.minimum_readable_schema) is not int
            or not 1 <= self.minimum_readable_schema <= self.database_schema
        ):
            _invalid("minimum_readable_schema")

    @classmethod
    def from_bytes(cls, data: bytes) -> "ReleaseManifest":
        """从不超过 1 MiB 的 UTF-8 JSON 构造 strict manifest。

        Args:
            data: 未信任的 manifest 原始字节。

        Returns:
            已完成全部嵌套与关系校验的不可变 manifest。

        Raises:
            InstallError: 字节预算、JSON、exact key 或模型约束无效。
        """
        if type(data) is not bytes or not data or len(data) > _MAX_MANIFEST_BYTES:
            _invalid("manifest")
        try:
            document = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=lambda _value: _raise_json_value(),
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            RecursionError,
        ) as error:
            raise InstallError("manifest_invalid", "manifest") from error
        if type(document) is not dict:
            _invalid("manifest")
        _exact_keys(
            document,
            {
                "schema_version",
                "product",
                "version",
                "git_commit",
                "python",
                "node",
                "artifacts",
                "supported_platforms",
                "features",
                "database_schema",
                "minimum_readable_schema",
            },
            "manifest",
        )
        return cls(
            schema_version=document["schema_version"],
            product=document["product"],
            version=document["version"],
            git_commit=document["git_commit"],
            python=document["python"],
            node=_parse_node(document["node"]),
            artifacts=_parse_artifacts(document["artifacts"]),
            supported_platforms=_parse_platforms(
                document["supported_platforms"], "supported_platforms"
            ),
            features=_parse_features(document["features"]),
            database_schema=document["database_schema"],
            minimum_readable_schema=document["minimum_readable_schema"],
        )

    def require_artifact(self, kind: str, platform: PlatformKey) -> Artifact:
        """返回唯一兼容目标平台的指定组件。

        Args:
            kind: 需要选择的 artifact kind。
            platform: 具体 Release OS/arch。

        Returns:
            唯一同时匹配 kind 与具体/通用平台的 artifact。

        Raises:
            InstallError: kind/platform 无效、缺失或匹配出多条记录。
        """
        if (
            type(kind) is not str
            or kind not in _ARTIFACT_KINDS
            or type(platform) is not PlatformKey
            or (platform.os, platform.arch) not in _SUPPORTED_PLATFORMS
        ):
            _invalid("artifact_selection")
        matches = tuple(
            artifact
            for artifact in self.artifacts
            if artifact.kind == kind
            and artifact.platform.os in {platform.os, "any"}
            and artifact.platform.arch in {platform.arch, "any"}
        )
        if len(matches) != 1:
            _invalid("artifact_selection")
        return matches[0]

    def _validate_release_urls(self) -> None:
        """把 MiniClaw GitHub asset tag 绑定到 manifest 自身版本。"""
        expected = f"/NEDONION/miniclaw/releases/download/v{self.version}/"
        for artifact in self.artifacts:
            parts = urlsplit(artifact.url)
            if parts.hostname == "github.com" and parts.path.startswith(
                "/NEDONION/miniclaw/"
            ):
                if not parts.path.startswith(expected):
                    _invalid("artifacts.url")


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """保存 install/update/uninstall 的全部非 Secret 请求参数。

    Raises:
        InstallError: enum、SemVer、路径、bool 或互斥 prefix 参数无效。
    """

    action: Literal["install", "update", "uninstall"]
    version: str | None
    channel: Literal["stable", "dev"]
    prefix: Path | None
    state_home: Path
    system_prefix: bool
    onboard: bool
    config_file: Path | None
    secrets_file: Path | None
    service: bool | None
    allow_system_packages: bool
    dry_run: bool
    json_output: bool
    verbose: bool
    purge_data: bool
    confirm_data_loss: bool

    def __post_init__(self) -> None:
        """验证请求只包含规范、绝对且非 Secret 的控制参数。"""
        if type(self.action) is not str or self.action not in {"install", "update", "uninstall"}:
            raise InstallError("request_invalid", "action")
        if type(self.channel) is not str or self.channel not in {"stable", "dev"}:
            raise InstallError("request_invalid", "channel")
        if self.version is not None:
            parsed = _parse_semver(self.version, "version", code="request_invalid")
            if self.channel == "stable" and parsed[3] is not None:
                raise InstallError("request_invalid", "version")
        for name, path in (
            ("prefix", self.prefix),
            ("state_home", self.state_home),
            ("config_file", self.config_file),
            ("secrets_file", self.secrets_file),
        ):
            if path is None and name != "state_home":
                continue
            if not _absolute_path_is_safe(path):
                raise InstallError("request_invalid", name)
        for name in (
            "system_prefix",
            "onboard",
            "allow_system_packages",
            "dry_run",
            "json_output",
            "verbose",
            "purge_data",
            "confirm_data_loss",
        ):
            if type(getattr(self, name)) is not bool:
                raise InstallError("request_invalid", name)
        if self.service is not None and type(self.service) is not bool:
            raise InstallError("request_invalid", "service")
        if self.system_prefix and self.prefix is not None:
            raise InstallError("request_invalid", "prefix")
        if self.purge_data and self.action != "uninstall":
            raise InstallError("request_invalid", "purge_data")
        if self.confirm_data_loss and not self.purge_data:
            raise InstallError("request_invalid", "confirm_data_loss")


@dataclass(frozen=True, slots=True)
class InstallEvent:
    """保存一个可安全输出的人类或 NDJSON 安装事件。

    Raises:
        InstallError: name、status 或 code 不是稳定标识符。
    """

    name: str
    status: Literal["start", "ok", "warn", "error"]
    code: str | None
    detail: str

    def __post_init__(self) -> None:
        """验证稳定字段并原位替换为有界脱敏 detail。"""
        if type(self.name) is not str or _STABLE_NAME.fullmatch(self.name) is None:
            raise InstallError("event_invalid", "name")
        if type(self.status) is not str or self.status not in {"start", "ok", "warn", "error"}:
            raise InstallError("event_invalid", "status")
        if self.code is not None and (
            type(self.code) is not str or _STABLE_NAME.fullmatch(self.code) is None
        ):
            raise InstallError("event_invalid", "code")
        if type(self.detail) is not str:
            raise InstallError("event_invalid", "detail")
        object.__setattr__(self, "detail", _safe_detail(self.detail))


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """保存确认前可完整展示且不含 Secret 内容的安装计划。

    Raises:
        InstallError: 路径、平台、artifact、命令或服务关系无效。
    """

    request: InstallRequest
    manifest: ReleaseManifest
    platform: PlatformKey
    distro_id: str
    distro_version: str
    service_manager: Literal["systemd-user", "launchd"]
    program_prefix: Path
    state_home: Path
    artifact_filenames: tuple[str, ...]
    system_argvs: tuple[tuple[str, ...], ...]
    install_service: bool
    run_onboarding: bool

    def __post_init__(self) -> None:
        """校验计划只引用已验证 manifest 与 bounded exact argv。"""
        if type(self.request) is not InstallRequest or type(self.manifest) is not ReleaseManifest:
            raise InstallError("plan_invalid", "model")
        if type(self.platform) is not PlatformKey:
            raise InstallError("plan_invalid", "platform")
        if (self.platform.os, self.platform.arch) not in _SUPPORTED_PLATFORMS:
            raise InstallError("plan_invalid", "platform")
        if self.platform not in self.manifest.supported_platforms:
            raise InstallError("plan_invalid", "platform")
        for name, value in (("distro_id", self.distro_id), ("distro_version", self.distro_version)):
            if (
                type(value) is not str
                or not 1 <= len(value) <= 64
                or any(character.isspace() or ord(character) < 32 for character in value)
            ):
                raise InstallError("plan_invalid", name)
        expected_manager = "systemd-user" if self.platform.os == "linux" else "launchd"
        if self.service_manager != expected_manager:
            raise InstallError("plan_invalid", "service_manager")
        for name, path in (
            ("program_prefix", self.program_prefix),
            ("state_home", self.state_home),
        ):
            if not _absolute_path_is_safe(path):
                raise InstallError("plan_invalid", name)
        if self.state_home != self.request.state_home:
            raise InstallError("plan_invalid", "state_home")
        manifest_filenames = {artifact.filename for artifact in self.manifest.artifacts}
        if (
            type(self.artifact_filenames) is not tuple
            or any(
                type(filename) is not str or filename not in manifest_filenames
                for filename in self.artifact_filenames
            )
        ):
            raise InstallError("plan_invalid", "artifact_filenames")
        if len(self.artifact_filenames) != len(set(self.artifact_filenames)):
            raise InstallError("plan_invalid", "artifact_filenames")
        _validate_argvs(self.system_argvs)
        if type(self.install_service) is not bool or type(self.run_onboarding) is not bool:
            raise InstallError("plan_invalid", "flags")

    def safe_summary(self) -> str:
        """返回不含 config、Secret 和 state home 的单行计划摘要。

        Returns:
            仅含版本、平台、程序 prefix 与两个公开动作开关的摘要。
        """
        return (
            f"version={self.manifest.version} platform={self.platform.os}/{self.platform.arch} "
            f"prefix={self.program_prefix} service={self.install_service} "
            f"onboarding={self.run_onboarding}"
        )


def _safe_detail(detail: str) -> str:
    """移除常见凭据、URL、绝对路径和换行后截断诊断文本。

    Args:
        detail: 可能来自不可信边界的诊断文本。

    Returns:
        单行且不超过 500 字符的保守脱敏文本。
    """
    if type(detail) is not str:
        return "redacted"
    value = " ".join(detail.splitlines())
    value = _SECRET_ASSIGNMENT.sub("[credential]", value)
    value = _URL_TEXT.sub("[url]", value)
    value = _PRIVATE_PATH.sub("[path]", value)
    return value[:500]


def _absolute_path_is_safe(value: object) -> bool:
    """判断路径是否为 bounded、无控制字符的绝对 Path。

    Args:
        value: 待检查模型字段。

    Returns:
        仅安全绝对 Path 返回 True。
    """
    if not isinstance(value, Path) or not value.is_absolute():
        return False
    rendered = str(value)
    return len(rendered) <= 4096 and all(character.isprintable() for character in rendered)


def _invalid(field: str) -> Never:
    """用稳定字段名抛出统一 manifest 错误。

    Args:
        field: 不含输入值的稳定字段名。

    Raises:
        InstallError: 始终抛出 manifest_invalid。
    """
    raise InstallError("manifest_invalid", field)


def _raise_json_value() -> Never:
    """拒绝 JSON 标准之外的 NaN 和 Infinity 常量。

    Raises:
        ValueError: 始终抛出并由 manifest parser 归一化。
    """
    raise ValueError("non-finite JSON value")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造不允许 duplicate key 的 JSON 对象。

    Args:
        pairs: json decoder 按原顺序提供的键值对。

    Returns:
        未发现重复键的普通字典。

    Raises:
        ValueError: 同一对象包含重复键。
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _exact_keys(value: object, keys: set[str], field: str) -> dict[str, object]:
    """要求对象键集合与 schema 完全相同。

    Args:
        value: 待检查 JSON 值。
        keys: 唯一允许且必须出现的键集合。
        field: 错误使用的稳定字段名。

    Returns:
        通过 exact-key 检查的字典。

    Raises:
        InstallError: 值不是对象或键集合不相等。
    """
    if type(value) is not dict or set(value) != keys:
        _invalid(field)
    return value


def _parse_semver(
    value: object, field: str, *, code: str = "manifest_invalid"
) -> tuple[int, int, int, str | None, str | None]:
    """使用唯一 anchored regex 解析规范 SemVer。

    Args:
        value: 待解析值。
        field: 失败时的稳定字段名。
        code: 调用边界对应的稳定错误代码。

    Returns:
        major、minor、patch、prerelease 与 build 五元组。

    Raises:
        InstallError: 值不是规范 SemVer。
    """
    match = _SEMVER.fullmatch(value) if type(value) is str else None
    if match is None:
        raise InstallError(code, field)
    return int(match[1]), int(match[2]), int(match[3]), match[4], match[5]


def _parse_core_version(value: object, field: str) -> tuple[int, int, int]:
    """解析不含 prerelease/build 的 Node 三段版本。

    Args:
        value: 待解析 JSON 值。
        field: 失败时的稳定字段名。

    Returns:
        三个非负整数组成的版本元组。

    Raises:
        InstallError: 值不是纯三段 SemVer。
    """
    major, minor, patch, prerelease, build = _parse_semver(value, field)
    if prerelease is not None or build is not None:
        _invalid(field)
    return major, minor, patch


def _require_hash(value: object, field: str) -> str:
    """要求一个 lowercase 64-hex SHA-256。

    Args:
        value: 待检查值。
        field: 失败时的稳定字段名。

    Returns:
        已校验的 hash 文本。

    Raises:
        InstallError: hash 缺失或非规范。
    """
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _invalid(field)
    return value


def _parse_platform(value: object, field: str) -> PlatformKey:
    """从 exact-key JSON 对象解析平台。

    Args:
        value: 待解析对象。
        field: 失败时的稳定字段名。

    Returns:
        已校验 PlatformKey。
    """
    document = _exact_keys(value, {"os", "arch"}, field)
    return PlatformKey(document["os"], document["arch"])


def _parse_platforms(value: object, field: str) -> tuple[PlatformKey, ...]:
    """解析 bounded 平台数组。

    Args:
        value: 待解析 JSON 数组。
        field: 失败时的稳定字段名。

    Returns:
        保持原顺序的不可变平台元组。

    Raises:
        InstallError: 值不是最多四项数组。
    """
    if type(value) is not list or len(value) > 4:
        _invalid(field)
    return tuple(_parse_platform(item, field) for item in value)


def _parse_node(value: object) -> NodePolicy:
    """解析 exact Node default 与 accepted ranges。

    Args:
        value: 待解析 Node JSON 对象。

    Returns:
        已校验的不可变 NodePolicy。

    Raises:
        InstallError: 对象键、默认值或范围无效。
    """
    document = _exact_keys(value, {"default", "accepted"}, "node")
    accepted = document["accepted"]
    if type(accepted) is not list or len(accepted) != 2:
        _invalid("node.accepted")
    ranges: list[NodeRange] = []
    for item in accepted:
        row = _exact_keys(item, {"minimum", "maximum_exclusive"}, "node.accepted")
        ranges.append(
            NodeRange(
                _parse_core_version(row["minimum"], "node.accepted.minimum"),
                _parse_core_version(
                    row["maximum_exclusive"], "node.accepted.maximum_exclusive"
                ),
            )
        )
    return NodePolicy(_parse_core_version(document["default"], "node.default"), tuple(ranges))


def _parse_artifact(value: object) -> Artifact:
    """从 exact-key JSON 对象解析一个 artifact。

    Args:
        value: 待解析 artifact 对象。

    Returns:
        已完成字段校验的 Artifact。

    Raises:
        InstallError: 对象键或任一字段无效。
    """
    document = _exact_keys(
        value,
        {
            "kind",
            "filename",
            "url",
            "sha256",
            "size",
            "media_type",
            "platform",
            "component_version",
            "source_repository",
            "license_ref",
            "upstream_sha256",
        },
        "artifacts",
    )
    return Artifact(
        kind=document["kind"],
        filename=document["filename"],
        url=document["url"],
        sha256=document["sha256"],
        size=document["size"],
        media_type=document["media_type"],
        platform=_parse_platform(document["platform"], "artifacts.platform"),
        component_version=document["component_version"],
        source_repository=document["source_repository"],
        license_ref=document["license_ref"],
        upstream_sha256=document["upstream_sha256"],
    )


def _parse_artifacts(value: object) -> tuple[Artifact, ...]:
    """解析最多 128 个 artifact。

    Args:
        value: 待解析 JSON 数组。

    Returns:
        保持原顺序的不可变 Artifact 元组。

    Raises:
        InstallError: 值不是 bounded 非空数组。
    """
    if type(value) is not list or not 1 <= len(value) <= _MAX_ARTIFACTS:
        _invalid("artifacts")
    return tuple(_parse_artifact(item) for item in value)


def _parse_features(value: object) -> tuple[str, ...]:
    """解析 bounded feature 名称数组。

    Args:
        value: 待解析 JSON 数组。

    Returns:
        保持原顺序的不可变字符串元组。

    Raises:
        InstallError: 值不是最多 64 项数组。
    """
    if type(value) is not list or len(value) > 64:
        _invalid("features")
    return tuple(value)


def _validate_artifact_url(value: object) -> None:
    """限制 artifact URL 为固定官方 HTTPS 下载路径。

    Args:
        value: 待验证 URL。

    Raises:
        InstallError: URL 含凭据、端口、query、fragment 或不在 allowlist。
    """
    if (
        type(value) is not str
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        _invalid("artifacts.url")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise InstallError("manifest_invalid", "artifacts.url") from error
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.query
        or parts.fragment
        or not parts.path
        or "%" in parts.path
    ):
        _invalid("artifacts.url")
    host = parts.hostname
    if host == "github.com":
        segments = parts.path.split("/")
        if (
            len(segments) != 7
            or (segments[1], segments[2]) not in _GITHUB_DOWNLOAD_REPOSITORIES
            or segments[3:5] != ["releases", "download"]
            or segments[5] in {"", ".", ".."}
            or segments[6] in {"", ".", ".."}
        ):
            _invalid("artifacts.url")
    elif host == "files.pythonhosted.org":
        if not parts.path.startswith("/packages/"):
            _invalid("artifacts.url")
    elif host == "nodejs.org":
        if not parts.path.startswith("/dist/"):
            _invalid("artifacts.url")
    elif host == "astral.sh":
        if not parts.path.startswith("/uv/"):
            _invalid("artifacts.url")
    else:
        _invalid("artifacts.url")


def _validate_argvs(value: object) -> None:
    """限制系统动作是 bounded 非空 exact argv 元组。

    Args:
        value: InstallPlan 的系统命令集合。

    Raises:
        InstallError: 命令不是 tuple、超预算或含空/control 参数。
    """
    if type(value) is not tuple or len(value) > 128:
        raise InstallError("plan_invalid", "system_argvs")
    for argv in value:
        if type(argv) is not tuple or not 1 <= len(argv) <= 64:
            raise InstallError("plan_invalid", "system_argvs")
        if any(
            type(argument) is not str
            or not argument
            or len(argument) > 4096
            or "\x00" in argument
            for argument in argv
        ):
            raise InstallError("plan_invalid", "system_argvs")
