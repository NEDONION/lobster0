#!/usr/bin/env python3
"""从已构建 Release artifact 生成 canonical CycloneDX 1.5 与 SPDX 2.3 SBOM。"""

import argparse
import hashlib
import json
import re
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_NAME = re.compile(r"^[@A-Za-z0-9][A-Za-z0-9._@/+-]{0,127}$")
_PACKAGE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_LICENSE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+ ()-]{0,127}$")
_SPDX_ID = re.compile(r"[^A-Za-z0-9.-]")
_IMAGE_DIGEST = re.compile(r"^(ghcr\.io/nedonion/lobster0(?:-sandbox)?)@sha256:([0-9a-f]{64})$")
_REPOSITORY = "https://github.com/NEDONION/lobster0"
_TOOL = "lobster0-build-sbom"
_PROJECT_LICENSE = "MIT"
_MEMBER_LIMIT = 4 * 1024 * 1024
_DOCUMENT_LIMIT = 8 * 1024 * 1024
_MAX_COMPONENTS = 4096


class SbomBuildError(RuntimeError):
    """表示 SBOM 输入、依赖清单或输出不满足严格契约。"""


@dataclass(frozen=True, slots=True)
class SbomInputs:
    """描述生成一份 Release SBOM 所需的全部本地事实。

    Args:
        version: Release 的三段版本。
        git_commit: 绑定 Release 的 lowercase 40-hex commit。
        source_date_epoch: tag commit 时间，用作唯一的文档时间戳来源。
        python_components: `uv export --format cyclonedx1.5` 的输出 JSON。
        requirements_lock: 与 export 精确同源的 hash-locked requirements。
        artifacts: 需要记录 SHA-256 的已构建 Release 文件。
        image_digests: GHCR runtime/sandbox digest 文本文件。

    Raises:
        SbomBuildError: 任一字段类型、格式或范围不可信。
    """

    version: str
    git_commit: str
    source_date_epoch: int
    python_components: Path
    requirements_lock: Path
    artifacts: tuple[Path, ...]
    image_digests: tuple[Path, ...]

    def __post_init__(self) -> None:
        """执行不读取文件内容的严格结构校验。"""
        if (
            type(self.version) is not str
            or _SEMVER.fullmatch(self.version) is None
            or type(self.git_commit) is not str
            or _COMMIT.fullmatch(self.git_commit) is None
            or type(self.source_date_epoch) is not int
            or isinstance(self.source_date_epoch, bool)
            or not 0 < self.source_date_epoch < 4_102_444_800
            or type(self.artifacts) is not tuple
            or not self.artifacts
            or type(self.image_digests) is not tuple
            or not self.image_digests
        ):
            raise SbomBuildError("invalid sbom inputs")
        paths = (
            self.python_components,
            self.requirements_lock,
            *self.artifacts,
            *self.image_digests,
        )
        for path in paths:
            if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
                raise SbomBuildError("invalid sbom inputs")


@dataclass(frozen=True, slots=True)
class SbomDocuments:
    """保存同一 Release 的两种 canonical SBOM 序列化。

    Args:
        cyclonedx: CycloneDX 1.5 JSON 字节。
        spdx: SPDX 2.3 JSON 字节。
    """

    cyclonedx: bytes
    spdx: bytes


@dataclass(frozen=True, slots=True)
class _Entry:
    """描述一个进入两种 SBOM 的规范化组件记录。"""

    category: str
    name: str
    version: str
    license_id: str
    purl: str | None
    sha256: str | None


def build_sbom(inputs: SbomInputs) -> SbomDocuments:
    """把 locked Python、production Node、artifact 与镜像事实汇成两份 SBOM。

    Args:
        inputs: 已通过结构校验的本地 SBOM 输入。

    Returns:
        字节可复现的 CycloneDX 1.5 与 SPDX 2.3 文档。

    Raises:
        SbomBuildError: 依赖集合不一致、清单不可解析或组件重复。
    """
    if type(inputs) is not SbomInputs:
        raise SbomBuildError("invalid sbom inputs")
    entries: list[_Entry] = []
    entries.extend(_python_entries(inputs.python_components, inputs.requirements_lock))
    entries.extend(_node_entries(inputs.artifacts))
    entries.extend(_node_runtime_entries(inputs.artifacts))
    entries.extend(_file_entries(inputs.artifacts, inputs.version))
    entries.extend(_image_entries(inputs.image_digests, inputs.version))
    ordered = tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.category,
                entry.name,
                entry.version,
                entry.purl or "",
            ),
        )
    )
    if not 0 < len(ordered) <= _MAX_COMPONENTS:
        raise SbomBuildError("sbom component budget exceeded")
    references = [_reference(entry) for entry in ordered]
    if len(references) != len(set(references)):
        raise SbomBuildError("duplicate sbom component")
    timestamp = datetime.fromtimestamp(inputs.source_date_epoch, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return SbomDocuments(
        cyclonedx=_canonical_json(_cyclonedx(inputs, ordered, references, timestamp)),
        spdx=_canonical_json(_spdx(inputs, ordered, references, timestamp)),
    )


def locked_packages(path: Path) -> dict[str, str]:
    """从 hash-locked requirements 读取精确的包名到版本映射。

    Args:
        path: `requirements-all.lock` 的路径。

    Returns:
        规范化小写包名到精确版本的映射。

    Raises:
        SbomBuildError: 文件不可读、条目非固定版本或出现重复包。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SbomBuildError("could not read locked requirements") from error
    packages: dict[str, str] = {}
    for line in text.replace("\\\n", " ").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "--")):
            continue
        requirement = stripped.split(" ", 1)[0]
        if requirement.count("==") != 1:
            raise SbomBuildError("locked requirements entry is not pinned")
        name, version = requirement.split("==", 1)
        normalized = _normalize(name)
        if (
            _PACKAGE_NAME.fullmatch(normalized) is None
            or _PACKAGE_VERSION.fullmatch(version) is None
        ):
            raise SbomBuildError("locked requirements entry is not pinned")
        if normalized in packages:
            raise SbomBuildError("duplicate locked requirements entry")
        packages[normalized] = version
    if not packages:
        raise SbomBuildError("locked requirements are empty")
    return packages


def _python_entries(components: Path, lock: Path) -> tuple[_Entry, ...]:
    """要求 uv export 与 lock 的包集合精确一致并生成 PyPI 组件。"""
    locked = locked_packages(lock)
    document = _read_json(components, "python components")
    raw = document.get("components") if type(document) is dict else None
    if type(raw) is not list or not raw:
        raise SbomBuildError("invalid python components")
    entries: dict[str, _Entry] = {}
    for item in raw:
        if type(item) is not dict:
            raise SbomBuildError("invalid python components")
        name = _normalize(str(item.get("name", "")))
        version = str(item.get("version", ""))
        if (
            _PACKAGE_NAME.fullmatch(name) is None
            or _PACKAGE_VERSION.fullmatch(version) is None
        ):
            raise SbomBuildError("invalid python components")
        if name in entries:
            raise SbomBuildError("duplicate python component")
        entries[name] = _Entry(
            category="library",
            name=name,
            version=version,
            license_id=_license_of(item.get("licenses")),
            purl=f"pkg:pypi/{name}@{version}",
            sha256=None,
        )
    if {name: entry.version for name, entry in entries.items()} != locked:
        raise SbomBuildError("python package set mismatch")
    return tuple(entries[name] for name in sorted(entries))


def _node_entries(artifacts: tuple[Path, ...]) -> tuple[_Entry, ...]:
    """从四个 TUI bundle 的同一 production license 清单生成 npm 组件。"""
    bundles = tuple(path for path in artifacts if path.name.startswith("lobster0-tui-"))
    if not bundles:
        raise SbomBuildError("missing tui bundle for the license inventory")
    inventories = {
        _read_tar_member(path, "tui/licenses.json") for path in sorted(bundles)
    }
    if len(inventories) != 1:
        raise SbomBuildError("tui license inventory mismatch")
    try:
        document = json.loads(inventories.pop().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SbomBuildError("invalid license inventory") from error
    if type(document) is not dict or not document:
        raise SbomBuildError("invalid license inventory")
    entries: dict[str, _Entry] = {}
    for license_id in sorted(document):
        group = document[license_id]
        if type(group) is not list or _LICENSE_ID.fullmatch(str(license_id)) is None:
            raise SbomBuildError("invalid license inventory")
        for item in group:
            if type(item) is not dict:
                raise SbomBuildError("invalid license inventory")
            name = str(item.get("name", ""))
            versions = item.get("versions", [item.get("version", "")])
            if type(versions) is not list or not versions:
                raise SbomBuildError("invalid license inventory")
            for version in versions:
                entry = _npm_entry(name, str(version), str(license_id))
                if entry.name in entries:
                    raise SbomBuildError("duplicate node package")
                entries[entry.name] = entry
    if not entries:
        raise SbomBuildError("invalid license inventory")
    return tuple(entries[name] for name in sorted(entries))


def _npm_entry(name: str, version: str, license_id: str) -> _Entry:
    """构造一个已校验的 npm production 组件记录。"""
    if (
        _PACKAGE_NAME.fullmatch(name) is None
        or _PACKAGE_VERSION.fullmatch(version) is None
    ):
        raise SbomBuildError("invalid license inventory")
    return _Entry(
        category="library",
        name=name,
        version=version,
        license_id=license_id,
        purl=f"pkg:npm/{name}@{version}",
        sha256=None,
    )


def _node_runtime_entries(artifacts: tuple[Path, ...]) -> tuple[_Entry, ...]:
    """从每个 Node bundle 的 release-component.json 生成 runtime 组件。"""
    bundles = tuple(path for path in artifacts if path.name.startswith("lobster0-node-"))
    if not bundles:
        raise SbomBuildError("missing node bundle for the runtime component")
    entries: list[_Entry] = []
    versions: set[str] = set()
    for path in sorted(bundles):
        try:
            component = json.loads(
                _read_tar_member(path, "node/release-component.json").decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SbomBuildError("invalid node component") from error
        if type(component) is not dict:
            raise SbomBuildError("invalid node component")
        version = str(component.get("version", ""))
        platform = str(component.get("platform", ""))
        upstream = str(component.get("upstream_sha256", ""))
        if (
            _SEMVER.fullmatch(version) is None
            or _PACKAGE_VERSION.fullmatch(platform) is None
            or _SHA256.fullmatch(upstream) is None
        ):
            raise SbomBuildError("invalid node component")
        versions.add(version)
        entries.append(
            _Entry(
                category="library",
                name=f"node ({platform})",
                version=version,
                license_id=_PROJECT_LICENSE,
                purl=f"pkg:generic/node@{version}?platform={platform}",
                sha256=upstream,
            )
        )
    if len(versions) != 1:
        raise SbomBuildError("node runtime version mismatch")
    return tuple(entries)


def _file_entries(artifacts: tuple[Path, ...], version: str) -> tuple[_Entry, ...]:
    """为每个已构建 Release 文件生成带 SHA-256 的 file 组件。"""
    entries: dict[str, _Entry] = {}
    for path in sorted(artifacts):
        if path.name in entries:
            raise SbomBuildError("duplicate release artifact filename")
        entries[path.name] = _Entry(
            category="file",
            name=path.name,
            version=version,
            license_id=_PROJECT_LICENSE,
            purl=None,
            sha256=_sha256(path),
        )
    return tuple(entries[name] for name in sorted(entries))


def _image_entries(image_digests: tuple[Path, ...], version: str) -> tuple[_Entry, ...]:
    """把每个 GHCR digest 文件解析成 immutable container 组件。"""
    entries: list[_Entry] = []
    for path in sorted(image_digests):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise SbomBuildError("could not read image digest") from error
        matched = _IMAGE_DIGEST.fullmatch(text.strip())
        if matched is None or text.strip().count("\n") != 0:
            raise SbomBuildError("invalid image digest")
        repository, digest = matched.group(1), matched.group(2)
        entries.append(
            _Entry(
                category="container",
                name=repository,
                version=version,
                license_id=_PROJECT_LICENSE,
                purl=f"pkg:oci/{repository.rsplit('/', 1)[-1]}@sha256:{digest}",
                sha256=digest,
            )
        )
    return tuple(entries)


def _cyclonedx(
    inputs: SbomInputs,
    entries: tuple[_Entry, ...],
    references: list[str],
    timestamp: str,
) -> dict[str, object]:
    """渲染 CycloneDX 1.5 文档，含 metadata、组件与依赖关系。"""
    root = f"pkg:github/NEDONION/lobster0@v{inputs.version}"
    components = []
    for entry, reference in zip(entries, references, strict=True):
        component: dict[str, object] = {
            "bom-ref": reference,
            "type": entry.category,
            "name": entry.name,
            "version": entry.version,
            "licenses": [{"license": {"name": entry.license_id}}],
        }
        if entry.purl is not None:
            component["purl"] = entry.purl
        if entry.sha256 is not None:
            component["hashes"] = [{"alg": "SHA-256", "content": entry.sha256}]
        components.append(component)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": [{"name": _TOOL, "version": inputs.version}],
            "component": {
                "bom-ref": root,
                "type": "application",
                "name": "lobster0",
                "version": inputs.version,
                "purl": root,
                "licenses": [{"license": {"name": _PROJECT_LICENSE}}],
                "externalReferences": [{"type": "vcs", "url": _REPOSITORY}],
            },
            "properties": [{"name": "lobster0:git_commit", "value": inputs.git_commit}],
        },
        "components": components,
        "dependencies": [
            {"ref": root, "dependsOn": sorted(references)},
            *({"ref": reference, "dependsOn": []} for reference in sorted(references)),
        ],
    }


def _spdx(
    inputs: SbomInputs,
    entries: tuple[_Entry, ...],
    references: list[str],
    timestamp: str,
) -> dict[str, object]:
    """渲染 SPDX 2.3 文档，含 DESCRIBES 与 CONTAINS 关系。"""
    root_purl = f"pkg:github/NEDONION/lobster0@v{inputs.version}"
    root_id = f"SPDXRef-lobster0-{inputs.version}".replace(".", "-")
    packages: list[dict[str, object]] = [
        {
            "SPDXID": root_id,
            "name": "lobster0",
            "versionInfo": inputs.version,
            "downloadLocation": f"{_REPOSITORY}/releases/tag/v{inputs.version}",
            "filesAnalyzed": False,
            "licenseConcluded": _PROJECT_LICENSE,
            "licenseDeclared": _PROJECT_LICENSE,
            "copyrightText": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": root_purl,
                }
            ],
        }
    ]
    identifiers = {root_id}
    for entry, reference in zip(entries, references, strict=True):
        identifier = f"SPDXRef-{_SPDX_ID.sub('-', reference)}"
        if identifier in identifiers:
            raise SbomBuildError("duplicate spdx identifier")
        identifiers.add(identifier)
        package: dict[str, object] = {
            "SPDXID": identifier,
            "name": entry.name,
            "versionInfo": entry.version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": entry.license_id,
            "licenseDeclared": entry.license_id,
            "copyrightText": "NOASSERTION",
        }
        if entry.sha256 is not None:
            package["checksums"] = [
                {"algorithm": "SHA256", "checksumValue": entry.sha256}
            ]
        if entry.purl is not None:
            package["externalRefs"] = [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": entry.purl,
                }
            ]
        packages.append(package)
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root_id,
        }
    ]
    relationships.extend(
        {
            "spdxElementId": root_id,
            "relationshipType": "CONTAINS",
            "relatedSpdxElement": str(package["SPDXID"]),
        }
        for package in packages[1:]
    )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"lobster0-{inputs.version}",
        "documentNamespace": (
            f"{_REPOSITORY}/releases/download/v{inputs.version}/"
            f"lobster0-{inputs.version}-sbom.spdx.json"
        ),
        "creationInfo": {
            "created": timestamp,
            "creators": [f"Tool: {_TOOL}", "Organization: Lobster0 contributors"],
            "comment": f"git_commit={inputs.git_commit}",
        },
        "packages": packages,
        "relationships": relationships,
    }


def _reference(entry: _Entry) -> str:
    """返回一个组件在两种 SBOM 中共用的稳定引用。"""
    return entry.purl if entry.purl is not None else f"file/{entry.name}"


def _license_of(value: object) -> str:
    """从 CycloneDX licenses 结构提取单一稳定 license 标识。"""
    if type(value) is not list or not value:
        return "NOASSERTION"
    first = value[0]
    if type(first) is not dict:
        return "NOASSERTION"
    expression = first.get("expression")
    if type(expression) is str and _LICENSE_ID.fullmatch(expression) is not None:
        return expression
    license_object = first.get("license")
    if type(license_object) is dict:
        for key in ("id", "name"):
            candidate = license_object.get(key)
            if type(candidate) is str and _LICENSE_ID.fullmatch(candidate) is not None:
                return candidate
    return "NOASSERTION"


def _normalize(name: str) -> str:
    """按 PyPA 规则把分发名规范化为可比较形式。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_json(path: Path, label: str) -> object:
    """读取一个有字节上限的 UTF-8 JSON 文档。"""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise SbomBuildError(f"could not read {label}") from error
    if not data or len(data) > _DOCUMENT_LIMIT:
        raise SbomBuildError(f"invalid {label}")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SbomBuildError(f"invalid {label}") from error


def _read_tar_member(path: Path, name: str) -> bytes:
    """从 tar.gz bundle 中读取一个有上限的 regular member。"""
    try:
        with tarfile.open(path, "r:gz") as archive:
            member = archive.getmember(name)
            if not member.isreg() or not 0 < member.size <= _MEMBER_LIMIT:
                raise SbomBuildError("invalid bundle member")
            source = archive.extractfile(member)
            if source is None:
                raise SbomBuildError("invalid bundle member")
            with source:
                return source.read(_MEMBER_LIMIT + 1)
    except SbomBuildError:
        raise
    except (OSError, EOFError, KeyError, tarfile.TarError) as error:
        raise SbomBuildError("invalid bundle member") from error


def _sha256(path: Path) -> str:
    """流式计算一个 regular file 的 SHA-256。"""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise SbomBuildError("could not read release artifact") from error
    return digest.hexdigest()


def _canonical_json(document: object) -> bytes:
    """编码带结尾换行、键序稳定的 canonical UTF-8 JSON。"""
    encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{encoded}\n".encode()


def main(argv: list[str] | None = None) -> int:
    """解析 CLI 参数并写出两种格式的 Release SBOM。"""
    parser = argparse.ArgumentParser(description="Build the Lobster0 release SBOM documents")
    parser.add_argument("--version", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--python-components", type=Path, required=True)
    parser.add_argument("--requirements-lock", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, action="append", required=True)
    parser.add_argument("--image-digest", type=Path, action="append", required=True)
    parser.add_argument("--cyclonedx-output", type=Path, required=True)
    parser.add_argument("--spdx-output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        documents = build_sbom(
            SbomInputs(
                version=arguments.version,
                git_commit=arguments.git_commit,
                source_date_epoch=arguments.source_date_epoch,
                python_components=arguments.python_components,
                requirements_lock=arguments.requirements_lock,
                artifacts=tuple(sorted(arguments.artifact)),
                image_digests=tuple(sorted(arguments.image_digest)),
            )
        )
    except SbomBuildError as error:
        parser.error(str(error))
        return 2
    for output, payload in (
        (arguments.cyclonedx_output, documents.cyclonedx),
        (arguments.spdx_output, documents.spdx),
    ):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
