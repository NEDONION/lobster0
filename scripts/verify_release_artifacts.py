#!/usr/bin/env python3
"""独立复核候选 Release 的 manifest schema、artifact hash、checksums 与 SBOM。"""

import argparse
import hashlib
import json
import re
from pathlib import Path

_SHA256_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]{0,199})$")
_MANIFEST_FILENAME = "release-manifest.json"
_INSTALL_SCRIPT_FILENAME = "install.sh"
_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 1_073_741_824


class ReleaseVerifyError(RuntimeError):
    """表示候选 Release 未通过独立复核。"""


def verify_release(
    *,
    manifest: Path,
    schema: Path,
    artifact_directory: Path,
    checksums: Path,
    install_script: Path,
) -> tuple[str, ...]:
    """完全从磁盘重新加载并复核一个候选 Release 的全部事实。

    Args:
        manifest: 待复核的 `release-manifest.json`。
        schema: 权威的 `release/manifest.schema.json`。
        artifact_directory: 只包含 manifest 所列 artifact 的目录。
        checksums: 覆盖 manifest、bootstrap 与全部 artifact 的 `SHA256SUMS`。
        install_script: 已渲染的 bootstrap 信任根文件。

    Returns:
        按固定顺序描述已复核事实的报告行。

    Raises:
        ReleaseVerifyError: schema、hash、size、覆盖范围或 SBOM 主体不一致。
    """
    manifest_bytes = _read_bytes(manifest, "manifest")
    document = _load_json(manifest_bytes, "manifest")
    schema_document = _load_json(_read_bytes(schema, "schema"), "schema")
    if type(document) is not dict or type(schema_document) is not dict:
        raise ReleaseVerifyError("manifest schema violation at $")
    _validate(document, schema_document, schema_document, "$")
    directory = Path(artifact_directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ReleaseVerifyError("artifact directory must be a regular directory")
    artifacts = document["artifacts"]
    version = document["version"]
    listed = [str(entry["filename"]) for entry in artifacts]
    on_disk = sorted(path.name for path in directory.iterdir())
    if sorted(listed) != on_disk:
        raise ReleaseVerifyError("artifact directory does not match the manifest")
    digests: dict[str, str] = {}
    for entry in artifacts:
        filename = str(entry["filename"])
        path = directory / filename
        if not path.is_file() or path.is_symlink():
            raise ReleaseVerifyError(f"artifact is not a regular file: {filename}")
        size = path.stat().st_size
        digest = _sha256(path)
        digests[filename] = digest
        if digest != entry["sha256"]:
            raise ReleaseVerifyError(f"artifact hash mismatch: {filename}")
        if size != entry["size"] or not 1 <= size <= _MAX_ARTIFACT_BYTES:
            raise ReleaseVerifyError(f"artifact size mismatch: {filename}")
        if not str(entry["url"]).endswith(f"/v{version}/{filename}"):
            raise ReleaseVerifyError(f"artifact url mismatch: {filename}")
    _verify_sbom(directory, digests, version)
    covered = _verify_checksums(
        checksums, manifest_bytes, install_script, directory, digests
    )
    return (
        f"manifest {version} verified",
        f"artifacts {len(artifacts)} verified",
        f"checksums {covered} verified",
    )


def _verify_sbom(directory: Path, digests: dict[str, str], version: str) -> None:
    """要求两份 SBOM 的主体与 wheel hash 与 manifest 完全一致。"""
    wheel = f"lobster0_agent-{version}-py3-none-any.whl"
    if wheel not in digests:
        raise ReleaseVerifyError("manifest is missing the universal wheel")
    cyclonedx = _load_json(
        _read_bytes(directory / f"lobster0-{version}-sbom.cyclonedx.json", "sbom"), "sbom"
    )
    spdx = _load_json(
        _read_bytes(directory / f"lobster0-{version}-sbom.spdx.json", "sbom"), "sbom"
    )
    if type(cyclonedx) is not dict or type(spdx) is not dict:
        raise ReleaseVerifyError("sbom subject mismatch")
    metadata = cyclonedx.get("metadata")
    component = metadata.get("component") if type(metadata) is dict else None
    if (
        cyclonedx.get("specVersion") != "1.5"
        or type(component) is not dict
        or component.get("version") != version
        or spdx.get("spdxVersion") != "SPDX-2.3"
        or spdx.get("name") != f"lobster0-{version}"
    ):
        raise ReleaseVerifyError("sbom subject mismatch")
    if _lookup(cyclonedx.get("components"), wheel, "hashes", "content") != digests[wheel]:
        raise ReleaseVerifyError("sbom subject mismatch")
    if _lookup(spdx.get("packages"), wheel, "checksums", "checksumValue") != digests[wheel]:
        raise ReleaseVerifyError("sbom subject mismatch")


def _verify_checksums(
    checksums: Path,
    manifest_bytes: bytes,
    install_script: Path,
    directory: Path,
    digests: dict[str, str],
) -> int:
    """重新计算信任根 hash 并要求 checksums 精确覆盖且排序。"""
    text = _read_bytes(checksums, "checksums").decode("utf-8", errors="strict")
    lines = text.splitlines()
    if not text.endswith("\n") or not lines:
        raise ReleaseVerifyError("checksums file is not canonical")
    recorded: dict[str, str] = {}
    names: list[str] = []
    for line in lines:
        matched = _SHA256_LINE.fullmatch(line)
        if matched is None:
            raise ReleaseVerifyError("checksums file is not canonical")
        if matched.group(2) in recorded:
            raise ReleaseVerifyError("checksums file has a duplicate entry")
        recorded[matched.group(2)] = matched.group(1)
        names.append(matched.group(2))
    if names != sorted(names, key=lambda name: name.encode("utf-8")):
        raise ReleaseVerifyError("checksums file is not sorted")
    expected = dict(digests)
    expected[_MANIFEST_FILENAME] = hashlib.sha256(manifest_bytes).hexdigest()
    script = Path(install_script)
    if script.name != _INSTALL_SCRIPT_FILENAME or not script.is_file() or script.is_symlink():
        raise ReleaseVerifyError("install script is missing")
    expected[_INSTALL_SCRIPT_FILENAME] = _sha256(script)
    if recorded != expected:
        raise ReleaseVerifyError("checksums do not match the release files")
    if not directory.is_dir():
        raise ReleaseVerifyError("artifact directory must be a regular directory")
    return len(recorded)


def _lookup(entries: object, name: str, container: str, key: str) -> str:
    """在 SBOM 列表里取出唯一匹配记录的第一个 hash 值。"""
    if type(entries) is not list:
        raise ReleaseVerifyError("sbom subject mismatch")
    matches = [item for item in entries if type(item) is dict and item.get("name") == name]
    if len(matches) != 1:
        raise ReleaseVerifyError("sbom subject mismatch")
    values = matches[0].get(container)
    if type(values) is not list or not values or type(values[0]) is not dict:
        raise ReleaseVerifyError("sbom subject mismatch")
    value = values[0].get(key)
    if type(value) is not str:
        raise ReleaseVerifyError("sbom subject mismatch")
    return value


def _validate(value: object, schema: object, root: dict[str, object], location: str) -> None:
    """按 manifest schema 使用的 JSON Schema 子集递归校验一个文档。"""
    if type(schema) is not dict:
        raise ReleaseVerifyError(f"manifest schema violation at {location}")
    reference = schema.get("$ref")
    if type(reference) is str:
        _validate(value, _resolve(reference, root, location), root, location)
        return
    for keyword, expected in schema.items():
        _check(keyword, expected, value, schema, root, location)


def _check(
    keyword: str,
    expected: object,
    value: object,
    schema: dict[str, object],
    root: dict[str, object],
    location: str,
) -> None:
    """校验单个 JSON Schema 关键字对当前实例的约束。"""
    if keyword in {"$schema", "$id", "title", "description", "$defs"}:
        return
    if keyword == "type" and not _is_type(value, expected):
        _fail(location)
    elif keyword == "const" and value != expected:
        _fail(location)
    elif keyword == "enum" and (type(expected) is not list or value not in expected):
        _fail(location)
    elif keyword == "pattern":
        if type(value) is not str or re.compile(str(expected)).search(value) is None:
            _fail(location)
    elif keyword == "required" and type(value) is dict:
        if any(name not in value for name in expected):  # type: ignore[union-attr]
            _fail(location)
    elif keyword == "additionalProperties" and expected is False and type(value) is dict:
        allowed = set(schema.get("properties", {}))  # type: ignore[arg-type]
        if set(value) - allowed:
            _fail(location)
    elif keyword == "properties" and type(value) is dict:
        for name, subschema in expected.items():  # type: ignore[union-attr]
            if name in value:
                _validate(value[name], subschema, root, f"{location}.{name}")
    elif keyword == "items" and type(value) is list:
        for index, item in enumerate(value):
            _validate(item, expected, root, f"{location}[{index}]")
    elif keyword == "minItems" and type(value) is list and len(value) < int(str(expected)):
        _fail(location)
    elif keyword == "maxItems" and type(value) is list and len(value) > int(str(expected)):
        _fail(location)
    elif keyword == "uniqueItems" and expected is True and type(value) is list:
        encoded = [json.dumps(item, sort_keys=True) for item in value]
        if len(encoded) != len(set(encoded)):
            _fail(location)
    elif keyword == "minimum" and type(value) is int and value < int(str(expected)):
        _fail(location)
    elif keyword == "maximum" and type(value) is int and value > int(str(expected)):
        _fail(location)
    elif keyword == "allOf":
        for index, subschema in enumerate(expected):  # type: ignore[union-attr]
            _validate(value, subschema, root, f"{location}/allOf[{index}]")
    elif keyword == "anyOf":
        if not any(_matches(value, subschema, root) for subschema in expected):  # type: ignore[union-attr]
            _fail(location)
    elif keyword == "oneOf":
        matched = sum(
            1 for subschema in expected if _matches(value, subschema, root)  # type: ignore[union-attr]
        )
        if matched != 1:
            _fail(location)
    elif keyword == "not" and _matches(value, expected, root):
        _fail(location)
    elif keyword == "contains" and type(value) is list:
        if not any(_matches(item, expected, root) for item in value):
            _fail(location)
    elif keyword == "if":
        branch = "then" if _matches(value, expected, root) else "else"
        if branch in schema:
            _validate(value, schema[branch], root, f"{location}/{branch}")
    elif keyword in {"then", "else"} and "if" not in schema:
        _fail(location)


def _matches(value: object, schema: object, root: dict[str, object]) -> bool:
    """在不抛出的前提下判断实例是否满足一个子 schema。"""
    try:
        _validate(value, schema, root, "$")
    except ReleaseVerifyError:
        return False
    return True


def _resolve(reference: str, root: dict[str, object], location: str) -> object:
    """解析 manifest schema 内部使用的本地 `#/` 引用。"""
    if not reference.startswith("#/"):
        _fail(location)
    node: object = root
    for part in reference[2:].split("/"):
        if type(node) is not dict or part not in node:
            _fail(location)
        node = node[part]  # type: ignore[index]
    return node


def _is_type(value: object, expected: object) -> bool:
    """按 JSON Schema 语义判断实例类型，整数与布尔严格区分。"""
    names = expected if type(expected) is list else [expected]
    for name in names:
        if name == "object" and type(value) is dict:
            return True
        if name == "array" and type(value) is list:
            return True
        if name == "string" and type(value) is str:
            return True
        if name == "integer" and type(value) is int:
            return True
        if name == "number" and type(value) in {int, float}:
            return True
        if name == "boolean" and type(value) is bool:
            return True
        if name == "null" and value is None:
            return True
    return False


def _fail(location: str) -> None:
    """以统一消息报告一次 schema 违规。"""
    raise ReleaseVerifyError(f"manifest schema violation at {location}")


def _read_bytes(path: Path, label: str) -> bytes:
    """读取一个有字节上限的常规文件。"""
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ReleaseVerifyError(f"{label} must be a regular file")
    data = candidate.read_bytes()
    if not data or len(data) > _MAX_DOCUMENT_BYTES:
        raise ReleaseVerifyError(f"{label} size is out of range")
    return data


def _load_json(data: bytes, label: str) -> object:
    """把字节解析为 UTF-8 JSON 文档。"""
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseVerifyError(f"{label} is not valid JSON") from error


def _sha256(path: Path) -> str:
    """流式计算一个 regular file 的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    """解析 CLI 参数并独立复核一个候选 Release。"""
    parser = argparse.ArgumentParser(description="Verify a candidate Lobster0 release")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=Path("release/manifest.schema.json"))
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--checksums", type=Path, required=True)
    parser.add_argument("--install-script", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        report = verify_release(
            manifest=arguments.manifest,
            schema=arguments.schema,
            artifact_directory=arguments.artifact_dir,
            checksums=arguments.checksums,
            install_script=arguments.install_script,
        )
    except ReleaseVerifyError as error:
        parser.error(str(error))
        return 2
    for line in report:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
