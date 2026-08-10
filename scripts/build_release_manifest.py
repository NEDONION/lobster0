#!/usr/bin/env python3
"""从干净 tag commit 与已构建 artifact 生成封闭世界 Release manifest 与 checksums。"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from lobster0.storage.migrations import LATEST_SCHEMA_VERSION

_SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION_CONSTANT = re.compile(r'^__version__:\s*str\s*=\s*"([^"]+)"$', re.MULTILINE)
_IMAGE_DIGEST = re.compile(r"^(ghcr\.io/nedonion/lobster0(?:-sandbox)?)@sha256:[0-9a-f]{64}$")
_METADATA_FIELD = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]*(.*)$")
_DISTRIBUTION = "lobster0-agent"
_PRODUCT = "lobster0"
_PYTHON_SERIES = "3.12"
_MINIMUM_READABLE_SCHEMA = 1
_MANIFEST_FILENAME = "release-manifest.json"
_INSTALL_SCRIPT_FILENAME = "install.sh"
_CHECKSUMS_FILENAME = "SHA256SUMS"
_REPOSITORY = "https://github.com/NEDONION/lobster0"
_NODE_REPOSITORY = "https://github.com/nodejs/node"
_DOWNLOAD_BASE = f"{_REPOSITORY}/releases/download"
_PROJECT_LICENSE = "MIT"
_PLATFORMS = ("linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64")
_NODE_POLICY = {
    "default": "24.18.0",
    "accepted": [
        {"minimum": "22.22.3", "maximum_exclusive": "23.0.0"},
        {"minimum": "24.15.0", "maximum_exclusive": "25.0.0"},
    ],
}
_FEATURES = (
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
)
_MAX_ARTIFACTS = 128
_MAX_ARTIFACT_BYTES = 1_073_741_824
_MAX_ARCHIVE_ENTRIES = 65_536
_MEMBER_LIMIT = 4 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 30


class ReleaseBuildError(RuntimeError):
    """表示 Release 事实、artifact 集合或输出不满足严格契约。"""


@dataclass(frozen=True, slots=True)
class ReleaseInputs:
    """描述装配一个具体 Release 所需的全部已验证事实。

    Args:
        version: `_version.py` 给出的三段 Release 版本。
        release_tag: 与版本精确对应的 `v<version>` tag。
        git_commit: tag 指向的 lowercase 40-hex commit。
        source_date_epoch: tag commit 时间，作为唯一时间来源。
        node_version: `runtime-versions.json` 固定的默认 Node 版本。
        minimum_readable_schema: 当前 Runtime 可读取的最早数据库 schema。
        features: 已实现且在注册表中登记的能力名。
        artifacts: 需要进入 manifest 的全部已构建 Release 文件。
        install_script: 已渲染的 bootstrap 信任根文件。
        runtime_versions: 固定 uv/Node/pnpm 版本与上游 hash 的 pins JSON。

    Raises:
        ReleaseBuildError: 任一字段类型、格式、范围或相互关系不可信。
    """

    version: str
    release_tag: str
    git_commit: str
    source_date_epoch: int
    node_version: str
    minimum_readable_schema: int
    features: tuple[str, ...]
    artifacts: tuple[Path, ...]
    install_script: Path
    runtime_versions: Path

    def __post_init__(self) -> None:
        """在读取任何 artifact 字节前闭合 Release 身份关系。"""
        if type(self.version) is not str or _SEMVER.fullmatch(self.version) is None:
            raise ReleaseBuildError("invalid release version")
        if self.release_tag != f"v{self.version}":
            raise ReleaseBuildError("release tag must match the release version")
        if type(self.git_commit) is not str or _COMMIT.fullmatch(self.git_commit) is None:
            raise ReleaseBuildError("invalid git commit")
        if (
            type(self.source_date_epoch) is not int
            or isinstance(self.source_date_epoch, bool)
            or not 0 < self.source_date_epoch < 4_102_444_800
        ):
            raise ReleaseBuildError("invalid SOURCE_DATE_EPOCH")
        if type(self.node_version) is not str or _SEMVER.fullmatch(self.node_version) is None:
            raise ReleaseBuildError("invalid node version")
        if (
            type(self.minimum_readable_schema) is not int
            or isinstance(self.minimum_readable_schema, bool)
            or not 1 <= self.minimum_readable_schema <= LATEST_SCHEMA_VERSION
        ):
            raise ReleaseBuildError("invalid minimum readable schema")
        if (
            type(self.features) is not tuple
            or not self.features
            or any(item not in _FEATURES for item in self.features)
        ):
            raise ReleaseBuildError("invalid feature registry")
        if len(self.features) != len(set(self.features)):
            raise ReleaseBuildError("duplicate feature registry entry")
        if (
            type(self.artifacts) is not tuple
            or not 1 <= len(self.artifacts) <= _MAX_ARTIFACTS
            or any(not isinstance(item, Path) for item in self.artifacts)
        ):
            raise ReleaseBuildError("invalid artifact set")
        for path in (*self.artifacts, self.install_script, self.runtime_versions):
            if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
                raise ReleaseBuildError("release inputs must be regular files")


@dataclass(frozen=True, slots=True)
class ReleaseOutputs:
    """保存一个候选 Release 的两份 canonical 文本产物。

    Args:
        manifest: canonical `release-manifest.json` 字节。
        checksums: 排序后的 `SHA256SUMS` 字节。
    """

    manifest: bytes
    checksums: bytes


@dataclass(frozen=True, slots=True)
class _Expected:
    """描述封闭世界中一个 Release 文件的全部固定属性。"""

    kind: str
    os: str
    arch: str
    media_type: str
    component_version: str
    source_repository: str
    license_ref: str


@dataclass(frozen=True, slots=True)
class _Record:
    """保存一个已散列并已深度校验的 manifest artifact 记录。"""

    expected: _Expected
    filename: str
    sha256: str
    size: int
    upstream_sha256: str | None


def build_manifest(inputs: ReleaseInputs) -> bytes:
    """校验完整 Tier 1 组件集合并生成 canonical Release manifest。

    Args:
        inputs: 已通过身份校验的 Release 输入。

    Returns:
        键序稳定、以换行结尾的 canonical UTF-8 manifest 字节。

    Raises:
        ReleaseBuildError: artifact 集合不完整、多余、重复或内容与 Release 不符。
    """
    if type(inputs) is not ReleaseInputs:
        raise ReleaseBuildError("invalid release inputs")
    expected = _expected_artifacts(inputs.version, inputs.node_version)
    present = _require_complete_set(inputs, expected)
    records = _validate_artifacts(inputs, expected, present)
    ordered = sorted(
        records,
        key=lambda record: (
            record.expected.kind,
            record.expected.os,
            record.expected.arch,
            record.filename,
        ),
    )
    document = {
        "schema_version": 1,
        "product": _PRODUCT,
        "version": inputs.version,
        "git_commit": inputs.git_commit,
        "python": _PYTHON_SERIES,
        "node": _NODE_POLICY,
        "artifacts": [_artifact_document(record, inputs.release_tag) for record in ordered],
        "supported_platforms": [
            {"os": platform.split("-", 1)[0], "arch": platform.split("-", 1)[1]}
            for platform in _PLATFORMS
        ],
        "features": list(inputs.features),
        "database_schema": LATEST_SCHEMA_VERSION,
        "minimum_readable_schema": inputs.minimum_readable_schema,
    }
    return _canonical_json(document)


def build_checksums(manifest: bytes, inputs: ReleaseInputs) -> bytes:
    """对 manifest、bootstrap 与全部 Release artifact 生成排序 checksums。

    Args:
        manifest: 已完成的 canonical manifest 字节。
        inputs: 与 manifest 同源的 Release 输入。

    Returns:
        按文件名排序、每行 `<sha256>  <filename>` 的 `SHA256SUMS` 字节。

    Raises:
        ReleaseBuildError: manifest 字节、bootstrap 文件名或文件集合不可信。
    """
    if type(manifest) is not bytes or not manifest:
        raise ReleaseBuildError("invalid manifest bytes")
    if type(inputs) is not ReleaseInputs:
        raise ReleaseBuildError("invalid release inputs")
    if inputs.install_script.name != _INSTALL_SCRIPT_FILENAME:
        raise ReleaseBuildError("install script must be named install.sh")
    _require_complete_set(inputs, _expected_artifacts(inputs.version, inputs.node_version))
    entries = {
        _MANIFEST_FILENAME: hashlib.sha256(manifest).hexdigest(),
        _INSTALL_SCRIPT_FILENAME: _sha256(inputs.install_script),
    }
    for path in inputs.artifacts:
        if path.name in entries:
            raise ReleaseBuildError(f"duplicate artifact filename {path.name}")
        entries[path.name] = _sha256(path)
    if _CHECKSUMS_FILENAME in entries:
        raise ReleaseBuildError("SHA256SUMS must not be a release artifact")
    lines = [
        f"{entries[filename]}  {filename}\n"
        for filename in sorted(entries, key=lambda name: name.encode("utf-8"))
    ]
    return "".join(lines).encode()


def build_release_outputs(inputs: ReleaseInputs) -> ReleaseOutputs:
    """一次性生成同一候选 Release 的 manifest 与 checksums。

    Args:
        inputs: 已通过身份校验的 Release 输入。

    Returns:
        对相同输入逐字节可复现的两份文本产物。

    Raises:
        ReleaseBuildError: manifest 或 checksums 任一环节校验失败。
    """
    manifest = build_manifest(inputs)
    return ReleaseOutputs(manifest=manifest, checksums=build_checksums(manifest, inputs))


def build_release_inputs_document(manifest: bytes, inputs: ReleaseInputs) -> bytes:
    """生成 `install.sh` 渲染所需的固定 manifest/installer 事实。

    Args:
        manifest: 已完成的 canonical manifest 字节。
        inputs: 与 manifest 同源的 Release 输入。

    Returns:
        canonical `release-inputs.json` 字节。

    Raises:
        ReleaseBuildError: manifest 字节或 installer artifact 不可信。
    """
    if type(manifest) is not bytes or not manifest:
        raise ReleaseBuildError("invalid manifest bytes")
    if type(inputs) is not ReleaseInputs:
        raise ReleaseBuildError("invalid release inputs")
    installer = next(
        (path for path in inputs.artifacts if path.name == "lobster0-installer.pyz"), None
    )
    if installer is None:
        raise ReleaseBuildError("missing installer artifact")
    return _canonical_json(
        {
            "release_tag": inputs.release_tag,
            "manifest_filename": _MANIFEST_FILENAME,
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "manifest_size": len(manifest),
            "installer_filename": installer.name,
            "installer_sha256": _sha256(installer),
            "installer_size": installer.stat().st_size,
        }
    )


def load_features(path: Path) -> tuple[str, ...]:
    """读取 features 注册表并返回排序、唯一、封闭的能力名。

    Args:
        path: `release/features.json` 的路径。

    Returns:
        与注册表登记顺序一致的能力名元组。

    Raises:
        ReleaseBuildError: 注册表结构、能力名或 import 断言不可信。
    """
    document = _read_json(path, "feature registry")
    if type(document) is not dict or document.get("schema_version") != 1:
        raise ReleaseBuildError("invalid feature registry")
    entries = document.get("features")
    if type(entries) is not list or not entries:
        raise ReleaseBuildError("invalid feature registry")
    names: list[str] = []
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"feature", "module", "attribute"}:
            raise ReleaseBuildError("invalid feature registry")
        name = entry["feature"]
        module = entry["module"]
        attribute = entry["attribute"]
        if (
            type(name) is not str
            or name not in _FEATURES
            or type(module) is not str
            or not module.startswith("lobster0.")
            or type(attribute) is not str
            or not attribute.isidentifier()
        ):
            raise ReleaseBuildError("invalid feature registry")
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise ReleaseBuildError("duplicate feature registry entry")
    return tuple(names)


def collect_release_inputs(
    repository: Path,
    artifact_directory: Path,
    install_script: Path,
    environ: dict[str, str],
) -> ReleaseInputs:
    """从干净 tag commit 与本地 artifact 目录收集全部 Release 事实。

    Args:
        repository: 处于 tag commit 且工作区干净的 Git 仓库。
        artifact_directory: 只包含已构建 Release 文件的目录。
        install_script: 已渲染的 bootstrap 信任根文件。
        environ: 必须提供 `SOURCE_DATE_EPOCH` 的进程环境映射。

    Returns:
        与 tag、commit、版本和固定 pins 精确一致的 Release 输入。

    Raises:
        ReleaseBuildError: 工作区不干净、tag 不指向 HEAD、时间不一致或目录不合规。
    """
    repository_root = Path(repository)
    if _git(repository_root, "status", "--porcelain").strip():
        raise ReleaseBuildError("git worktree is not clean")
    version = _read_version(repository_root)
    tag = f"v{version}"
    head = _git(repository_root, "rev-parse", "HEAD").strip()
    tagged = _git(repository_root, "rev-parse", "--verify", f"{tag}^{{commit}}").strip()
    if _COMMIT.fullmatch(head) is None or tagged != head:
        raise ReleaseBuildError("release tag does not point at HEAD")
    epoch_text = _git(repository_root, "show", "-s", "--format=%ct", f"{tag}^{{commit}}").strip()
    if not epoch_text.isdigit():
        raise ReleaseBuildError("could not read the tag commit time")
    requested = environ.get("SOURCE_DATE_EPOCH") if type(environ) is dict else None
    if type(requested) is not str or not requested.isdigit() or requested != epoch_text:
        raise ReleaseBuildError("SOURCE_DATE_EPOCH must equal the tag commit time")
    directory = Path(artifact_directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ReleaseBuildError("artifact directory must be a regular directory")
    artifacts = tuple(sorted(directory.iterdir()))
    runtime_versions = repository_root / "release/runtime-versions.json"
    return ReleaseInputs(
        version=version,
        release_tag=tag,
        git_commit=head,
        source_date_epoch=int(epoch_text),
        node_version=_node_pins(runtime_versions)[0],
        minimum_readable_schema=_MINIMUM_READABLE_SCHEMA,
        features=load_features(repository_root / "release/features.json"),
        artifacts=artifacts,
        install_script=Path(install_script),
        runtime_versions=runtime_versions,
    )


def _expected_artifacts(version: str, node_version: str) -> dict[str, _Expected]:
    """枚举一个完整 Release 必须恰好包含的全部文件。"""
    universal = _Expected(
        kind="",
        os="any",
        arch="any",
        media_type="",
        component_version=version,
        source_repository=_REPOSITORY,
        license_ref=_PROJECT_LICENSE,
    )
    table = {
        f"lobster0_agent-{version}-py3-none-any.whl": replace(
            universal, kind="wheel", media_type="application/zip"
        ),
        f"lobster0_agent-{version}.tar.gz": replace(
            universal, kind="sdist", media_type="application/gzip"
        ),
        "requirements-all.lock": replace(
            universal, kind="requirements", media_type="text/plain"
        ),
        "lobster0-installer.pyz": replace(
            universal, kind="installer", media_type="application/zip"
        ),
        f"lobster0-{version}-sbom.cyclonedx.json": replace(
            universal, kind="sbom", media_type="application/vnd.cyclonedx+json"
        ),
        f"lobster0-{version}-sbom.spdx.json": replace(
            universal, kind="sbom", media_type="application/spdx+json"
        ),
        f"lobster0-runtime-image-{version}.txt": replace(
            universal, kind="runtime-image", media_type="text/plain"
        ),
        f"lobster0-sandbox-image-{version}.txt": replace(
            universal, kind="sandbox-image", media_type="text/plain"
        ),
    }
    for platform in _PLATFORMS:
        os_name, arch = platform.split("-", 1)
        table[f"lobster0-node-{node_version}-{platform}.tar.gz"] = _Expected(
            kind="node",
            os=os_name,
            arch=arch,
            media_type="application/gzip",
            component_version=node_version,
            source_repository=_NODE_REPOSITORY,
            license_ref=_PROJECT_LICENSE,
        )
        table[f"lobster0-tui-{version}-{platform}.tar.gz"] = _Expected(
            kind="tui",
            os=os_name,
            arch=arch,
            media_type="application/gzip",
            component_version=version,
            source_repository=_REPOSITORY,
            license_ref=_PROJECT_LICENSE,
        )
    return table


def _require_complete_set(
    inputs: ReleaseInputs, expected: dict[str, _Expected]
) -> dict[str, Path]:
    """要求输入文件集合与封闭世界期望精确相等。"""
    present = _index_artifacts(inputs.artifacts, expected)
    for filename in sorted(expected):
        if filename not in present:
            raise ReleaseBuildError(_missing_message(expected[filename]))
    return present


def _missing_message(expected: _Expected) -> str:
    """为一个缺失组件生成精确、稳定的失败消息。"""
    if expected.kind == "sbom":
        fmt = "cyclonedx" if "cyclonedx" in expected.media_type else "spdx"
        return f"missing sbom artifact for {fmt}"
    if expected.os != "any":
        return f"missing {expected.kind} artifact for {expected.os}-{expected.arch}"
    return f"missing {expected.kind} artifact"


def _index_artifacts(
    artifacts: tuple[Path, ...], expected: dict[str, _Expected]
) -> dict[str, Path]:
    """按文件名索引输入文件并拒绝未登记或重复的条目。"""
    present: dict[str, Path] = {}
    for path in artifacts:
        if path.name not in expected:
            raise ReleaseBuildError(f"unexpected artifact {path.name}")
        if path.name in present:
            raise ReleaseBuildError(f"duplicate artifact filename {path.name}")
        present[path.name] = path
    return present


def _validate_artifacts(
    inputs: ReleaseInputs,
    expected: dict[str, _Expected],
    present: dict[str, Path],
) -> tuple[_Record, ...]:
    """散列每个 artifact 并按 kind 执行深度内容校验。"""
    node_version, node_archives = _node_pins(inputs.runtime_versions)
    if node_version != inputs.node_version:
        raise ReleaseBuildError("node version does not match the pinned runtime")
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for filename in sorted(present):
        path = present[filename]
        size = path.stat().st_size
        if not 1 <= size <= _MAX_ARTIFACT_BYTES:
            raise ReleaseBuildError(f"artifact size is out of range for {filename}")
        digests[filename] = _sha256(path)
        sizes[filename] = size
    wheel_name = f"lobster0_agent-{inputs.version}-py3-none-any.whl"
    _validate_wheel(present[wheel_name], inputs.version)
    _validate_sdist(present[f"lobster0_agent-{inputs.version}.tar.gz"], inputs.version)
    _validate_requirements(present["requirements-all.lock"])
    _validate_installer(present["lobster0-installer.pyz"])
    records: list[_Record] = []
    for filename in sorted(present):
        entry = expected[filename]
        upstream: str | None = None
        if entry.kind == "node":
            platform = f"{entry.os}-{entry.arch}"
            _scan_archive(present[filename])
            upstream = _validate_node_bundle(
                present[filename], platform, node_version, node_archives
            )
        elif entry.kind == "tui":
            _scan_archive(present[filename])
            _validate_tui_bundle(present[filename])
        elif entry.kind == "sdist":
            _scan_archive(present[filename])
        elif entry.kind == "sbom":
            _validate_sbom(
                present[filename], entry.media_type, inputs.version, wheel_name,
                digests[wheel_name],
            )
        elif entry.kind in {"runtime-image", "sandbox-image"}:
            _validate_image_digest(present[filename], entry.kind)
        records.append(
            _Record(
                expected=entry,
                filename=filename,
                sha256=digests[filename],
                size=sizes[filename],
                upstream_sha256=upstream,
            )
        )
    return tuple(records)


def _artifact_document(record: _Record, release_tag: str) -> dict[str, object]:
    """把一条已校验记录渲染成 manifest artifact 对象。"""
    return {
        "kind": record.expected.kind,
        "filename": record.filename,
        "url": f"{_DOWNLOAD_BASE}/{release_tag}/{record.filename}",
        "sha256": record.sha256,
        "size": record.size,
        "media_type": record.expected.media_type,
        "platform": {"os": record.expected.os, "arch": record.expected.arch},
        "component_version": record.expected.component_version,
        "source_repository": record.expected.source_repository,
        "license_ref": record.expected.license_ref,
        "upstream_sha256": record.upstream_sha256,
    }


def _validate_wheel(path: Path, version: str) -> None:
    """要求 wheel 的分发名、版本与 console script 与 Release 精确一致。"""
    dist_info = f"lobster0_agent-{version}.dist-info"
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {
                f"{dist_info}/METADATA",
                f"{dist_info}/RECORD",
                f"{dist_info}/entry_points.txt",
            }
            if not required.issubset(names):
                raise ReleaseBuildError("wheel metadata mismatch")
            metadata = _metadata_fields(archive.read(f"{dist_info}/METADATA"))
            entry_points = archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")
    except ReleaseBuildError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as error:
        raise ReleaseBuildError("wheel metadata mismatch") from error
    if (
        _normalize(metadata.get("Name", "")) != _DISTRIBUTION
        or metadata.get("Version") != version
        or "lobster0 = lobster0.cli:main" not in entry_points
    ):
        raise ReleaseBuildError("wheel metadata mismatch")


def _validate_sdist(path: Path, version: str) -> None:
    """要求 sdist 的 PKG-INFO 与 Release 名称版本精确一致。"""
    metadata = _metadata_fields(
        _read_tar_member(path, f"lobster0_agent-{version}/PKG-INFO")
    )
    if _normalize(metadata.get("Name", "")) != _DISTRIBUTION or metadata.get("Version") != version:
        raise ReleaseBuildError("sdist metadata mismatch")


def _validate_requirements(path: Path) -> None:
    """要求 lock 中每条 requirement 都固定版本并带 SHA-256。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseBuildError("could not read the requirements lock") from error
    entries = 0
    for line in text.replace("\\\n", " ").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        requirement = stripped.split(" ", 1)[0]
        if requirement.count("==") != 1 or not requirement.split("==", 1)[1]:
            raise ReleaseBuildError("requirements entry without pinned version")
        if "--hash=sha256:" not in stripped:
            raise ReleaseBuildError("requirements entry without hash")
        entries += 1
    if entries == 0:
        raise ReleaseBuildError("requirements lock is empty")


def _validate_installer(path: Path) -> None:
    """要求 installer pyz 带固定 shebang、可执行入口且无损坏 entry。"""
    try:
        with path.open("rb") as source:
            header = source.read(23)
        if header != b"#!/usr/bin/env python3\n" or not zipfile.is_zipfile(path):
            raise ReleaseBuildError("installer pyz is invalid")
        with zipfile.ZipFile(path) as archive:
            if "__main__.py" not in set(archive.namelist()):
                raise ReleaseBuildError("installer pyz is missing its entry point")
            if archive.testzip() is not None:
                raise ReleaseBuildError("installer pyz has a corrupt entry")
    except ReleaseBuildError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise ReleaseBuildError("installer pyz is invalid") from error


def _validate_node_bundle(
    path: Path, platform: str, node_version: str, archives: dict[str, dict[str, str]]
) -> str:
    """要求 Node bundle 记录的上游 hash 与固定 pin 精确一致。"""
    component = _read_json_bytes(
        _read_tar_member(path, "node/release-component.json"), "node component"
    )
    if type(component) is not dict:
        raise ReleaseBuildError("invalid node component")
    if component.get("version") != node_version or component.get("platform") != platform:
        raise ReleaseBuildError(f"node component mismatch for {platform}")
    upstream = component.get("upstream_sha256")
    if type(upstream) is not str or _SHA256.fullmatch(upstream) is None:
        raise ReleaseBuildError("invalid node component")
    if upstream != archives[platform]["sha256"]:
        raise ReleaseBuildError(f"node upstream hash mismatch for {platform}")
    return upstream


def _validate_tui_bundle(path: Path) -> None:
    """要求 TUI bundle 携带 production license 清单与 package 描述。"""
    for member in ("tui/licenses.json", "tui/package.json"):
        if not _read_tar_member(path, member):
            raise ReleaseBuildError("tui bundle is missing a release member")


def _validate_sbom(
    path: Path, media_type: str, version: str, wheel_filename: str, wheel_sha256: str
) -> None:
    """要求两种 SBOM 的主体与 wheel hash 与本 Release 精确一致。"""
    document = _read_json(path, "sbom")
    if type(document) is not dict:
        raise ReleaseBuildError("sbom subject mismatch")
    if media_type == "application/vnd.cyclonedx+json":
        metadata = document.get("metadata")
        component = metadata.get("component") if type(metadata) is dict else None
        if (
            document.get("specVersion") != "1.5"
            or type(component) is not dict
            or component.get("name") != _PRODUCT
            or component.get("version") != version
        ):
            raise ReleaseBuildError("sbom subject mismatch")
        entries = document.get("components")
        found = _find_entry(entries, wheel_filename, "name")
        digest = _first_value(found, "hashes", "content")
    else:
        if document.get("spdxVersion") != "SPDX-2.3" or document.get("name") != (
            f"{_PRODUCT}-{version}"
        ):
            raise ReleaseBuildError("sbom subject mismatch")
        entries = document.get("packages")
        found = _find_entry(entries, wheel_filename, "name")
        digest = _first_value(found, "checksums", "checksumValue")
    if digest != wheel_sha256:
        raise ReleaseBuildError("sbom subject mismatch")


def _validate_image_digest(path: Path, kind: str) -> None:
    """要求 GHCR digest 文件是精确的单行 immutable 引用。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseBuildError("image digest missing") from error
    matched = _IMAGE_DIGEST.fullmatch(text.strip())
    if matched is None or text.strip().count("\n") != 0:
        raise ReleaseBuildError("image digest missing")
    expected = f"ghcr.io/nedonion/{_PRODUCT}" + ("-sandbox" if kind == "sandbox-image" else "")
    if matched.group(1) != expected:
        raise ReleaseBuildError("image digest missing")


def _scan_archive(path: Path) -> None:
    """拒绝含 link、special entry、逃逸路径或重复条目的 Release archive。"""
    seen: set[str] = set()
    try:
        with tarfile.open(path, "r:gz") as archive:
            for index, member in enumerate(archive):
                if index >= _MAX_ARCHIVE_ENTRIES:
                    raise ReleaseBuildError("archive contains too many entries")
                name = member.name
                pure = PurePosixPath(name)
                if (
                    not (member.isreg() or member.isdir())
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or name in seen
                ):
                    raise ReleaseBuildError("archive contains an unsafe entry")
                seen.add(name)
    except ReleaseBuildError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise ReleaseBuildError("archive contains an unsafe entry") from error
    if not seen:
        raise ReleaseBuildError("archive contains an unsafe entry")


def _find_entry(entries: object, value: str, key: str) -> dict[str, object]:
    """在 SBOM 列表中定位唯一匹配指定键值的对象。"""
    if type(entries) is not list:
        raise ReleaseBuildError("sbom subject mismatch")
    matches = [
        item for item in entries if type(item) is dict and item.get(key) == value
    ]
    if len(matches) != 1:
        raise ReleaseBuildError("sbom subject mismatch")
    return matches[0]


def _first_value(entry: dict[str, object], container: str, key: str) -> str:
    """读取 SBOM 记录中第一条 hash 结构的字符串值。"""
    values = entry.get(container)
    if type(values) is not list or not values or type(values[0]) is not dict:
        raise ReleaseBuildError("sbom subject mismatch")
    value = values[0].get(key)
    if type(value) is not str:
        raise ReleaseBuildError("sbom subject mismatch")
    return value


def _metadata_fields(payload: bytes) -> dict[str, str]:
    """解析 core metadata 头部并返回首次出现的字段值。"""
    fields: dict[str, str] = {}
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseBuildError("invalid distribution metadata") from error
    for line in text.splitlines():
        if not line.strip():
            break
        matched = _METADATA_FIELD.fullmatch(line)
        if matched is not None and matched.group(1) not in fields:
            fields[matched.group(1)] = matched.group(2).strip()
    return fields


def _node_pins(path: Path) -> tuple[str, dict[str, dict[str, str]]]:
    """读取并严格校验四平台 Node 版本与上游 hash。"""
    document = _read_json(path, "runtime pins")
    node = document.get("node") if type(document) is dict else None
    if type(node) is not dict:
        raise ReleaseBuildError("invalid runtime pins")
    version = node.get("version")
    archives = node.get("archives")
    if (
        type(version) is not str
        or _SEMVER.fullmatch(version) is None
        or type(archives) is not dict
        or set(archives) != set(_PLATFORMS)
    ):
        raise ReleaseBuildError("invalid runtime pins")
    normalized: dict[str, dict[str, str]] = {}
    for platform in _PLATFORMS:
        entry = archives[platform]
        digest = entry.get("sha256") if type(entry) is dict else None
        url = entry.get("url") if type(entry) is dict else None
        if (
            type(digest) is not str
            or _SHA256.fullmatch(digest) is None
            or type(url) is not str
            or not url.startswith("https://nodejs.org/dist/")
        ):
            raise ReleaseBuildError("invalid runtime pins")
        normalized[platform] = {"sha256": digest, "url": url}
    return version, normalized


def _read_version(repository: Path) -> str:
    """从版本单一来源常量读取 Release 版本，不导入任何模块。"""
    try:
        text = (repository / "src/lobster0/_version.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseBuildError("could not read the release version") from error
    matched = _VERSION_CONSTANT.search(text)
    if matched is None or _SEMVER.fullmatch(matched.group(1)) is None:
        raise ReleaseBuildError("could not read the release version")
    return matched.group(1)


def _git(repository: Path, *arguments: str) -> str:
    """以 exact argv 运行一个只读 git 子命令。"""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseBuildError("could not read git release facts") from error
    if completed.returncode != 0:
        raise ReleaseBuildError("could not read git release facts")
    return completed.stdout


def _read_json(path: Path, label: str) -> object:
    """读取一个有字节上限的 UTF-8 JSON 文档。"""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise ReleaseBuildError(f"could not read {label}") from error
    return _read_json_bytes(data, label)


def _read_json_bytes(data: bytes, label: str) -> object:
    """解析一个有字节上限的 UTF-8 JSON 字节串。"""
    if not data or len(data) > _MAX_ARTIFACT_BYTES:
        raise ReleaseBuildError(f"invalid {label}")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseBuildError(f"invalid {label}") from error


def _read_tar_member(path: Path, name: str) -> bytes:
    """从 tar.gz bundle 中读取一个有上限的 regular member。"""
    try:
        with tarfile.open(path, "r:gz") as archive:
            member = archive.getmember(name)
            if not member.isreg() or not 0 < member.size <= _MEMBER_LIMIT:
                raise ReleaseBuildError(f"invalid bundle member {name}")
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseBuildError(f"invalid bundle member {name}")
            with source:
                return source.read(_MEMBER_LIMIT + 1)
    except ReleaseBuildError:
        raise
    except (OSError, EOFError, KeyError, tarfile.TarError) as error:
        raise ReleaseBuildError(f"invalid bundle member {name}") from error


def _sha256(path: Path) -> str:
    """流式计算一个 regular file 的 SHA-256。"""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ReleaseBuildError("could not read a release artifact") from error
    return digest.hexdigest()


def _normalize(name: str) -> str:
    """按 PyPA 规则把分发名规范化为可比较形式。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _canonical_json(document: object) -> bytes:
    """编码带结尾换行、键序稳定的 canonical UTF-8 JSON。"""
    encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{encoded}\n".encode()


def main(argv: list[str] | None = None) -> int:
    """解析 CLI 子命令并写出 manifest 或 checksums。"""
    parser = argparse.ArgumentParser(description="Assemble the Lobster0 release manifest")
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--install-script", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)
    manifest_parser = subcommands.add_parser("manifest", help="write release-manifest.json")
    manifest_parser.add_argument("--release-inputs-output", type=Path, default=None)
    checksums_parser = subcommands.add_parser("checksums", help="write SHA256SUMS")
    checksums_parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        inputs = collect_release_inputs(
            arguments.repository,
            arguments.artifact_dir,
            arguments.install_script,
            dict(os.environ),
        )
        if arguments.command == "manifest":
            payload = build_manifest(inputs)
            if arguments.release_inputs_output is not None:
                arguments.release_inputs_output.write_bytes(
                    build_release_inputs_document(payload, inputs)
                )
        else:
            payload = build_checksums(arguments.manifest.read_bytes(), inputs)
    except ReleaseBuildError as error:
        parser.error(str(error))
        return 2
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
