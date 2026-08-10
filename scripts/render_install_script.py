#!/usr/bin/env python3
"""从固定 Release 事实渲染 pinned POSIX one-line bootstrap 脚本。"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

_SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_TAG = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UV_URL_PREFIX = "https://github.com/astral-sh/uv/releases/download/"
_PLATFORM_KEYS = ("linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64")
_REPO_BASE_URL = "https://github.com/NEDONION/miniclaw/releases/download"
_DEFAULT_TEMPLATE = Path("release/install.sh.tmpl")
_DEFAULT_RUNTIME_VERSIONS = Path("release/runtime-versions.json")


class BootstrapRenderError(RuntimeError):
    """表示 bootstrap release 输入、pins 或渲染结果不满足严格契约。"""


@dataclass(frozen=True, slots=True)
class ReleaseInputs:
    """描述渲染一个具体 Release `install.sh` 所需的固定事实。

    Args:
        release_tag: 形如 ``v0.7.0`` 的精确 Git tag。
        manifest_filename: Release 中 manifest 资产的文件名。
        manifest_sha256: manifest 资产的精确 SHA-256。
        manifest_size: manifest 资产的精确字节数。
        installer_filename: Release 中 installer pyz 资产的文件名。
        installer_sha256: installer pyz 资产的精确 SHA-256。
        installer_size: installer pyz 资产的精确字节数。

    Raises:
        BootstrapRenderError: 任一字段类型、格式或范围不可信。
    """

    release_tag: str
    manifest_filename: str
    manifest_sha256: str
    manifest_size: int
    installer_filename: str
    installer_sha256: str
    installer_size: int

    def __post_init__(self) -> None:
        """执行不依赖文件系统的严格结构校验。"""
        if (
            type(self.release_tag) is not str
            or _TAG.fullmatch(self.release_tag) is None
            or type(self.manifest_filename) is not str
            or _FILENAME.fullmatch(self.manifest_filename) is None
            or type(self.installer_filename) is not str
            or _FILENAME.fullmatch(self.installer_filename) is None
            or self.manifest_filename == self.installer_filename
            or type(self.manifest_sha256) is not str
            or _SHA256.fullmatch(self.manifest_sha256) is None
            or type(self.installer_sha256) is not str
            or _SHA256.fullmatch(self.installer_sha256) is None
            or type(self.manifest_size) is not int
            or isinstance(self.manifest_size, bool)
            or not 0 < self.manifest_size <= 1_073_741_824
            or type(self.installer_size) is not int
            or isinstance(self.installer_size, bool)
            or not 0 < self.installer_size <= 1_073_741_824
        ):
            raise BootstrapRenderError("invalid release inputs")


def render_install_script(
    release_inputs: ReleaseInputs,
    *,
    runtime_versions: Path = _DEFAULT_RUNTIME_VERSIONS,
    template: Path = _DEFAULT_TEMPLATE,
) -> str:
    """把固定 Release 事实和 uv pins 替换进 bootstrap 模板。

    Args:
        release_inputs: 已校验的具体 Release 事实。
        runtime_versions: 包含 uv version 与四平台 URL/SHA-256 的 pins JSON。
        template: 含 ``{{TOKEN}}`` 占位符的 POSIX shell 模板。

    Returns:
        零残留占位符、零 ``latest`` 字面量的完整 shell 脚本文本。

    Raises:
        BootstrapRenderError: 输入类型、pins 或渲染结果不可信。
    """
    if type(release_inputs) is not ReleaseInputs:
        raise BootstrapRenderError("invalid release inputs")
    uv_pins = _load_uv_pins(Path(runtime_versions))
    try:
        text = Path(template).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise BootstrapRenderError("could not read bootstrap template") from error

    manifest_url = (
        f"{_REPO_BASE_URL}/{release_inputs.release_tag}/{release_inputs.manifest_filename}"
    )
    installer_url = (
        f"{_REPO_BASE_URL}/{release_inputs.release_tag}/{release_inputs.installer_filename}"
    )
    tokens = {
        "UV_VERSION": uv_pins["version"],
        "RELEASE_TAG": release_inputs.release_tag,
        "REPO_BASE_URL": _REPO_BASE_URL,
        "MANIFEST_FILENAME": release_inputs.manifest_filename,
        "MANIFEST_URL": manifest_url,
        "MANIFEST_SHA256": release_inputs.manifest_sha256,
        "MANIFEST_SIZE": str(release_inputs.manifest_size),
        "INSTALLER_FILENAME": release_inputs.installer_filename,
        "INSTALLER_URL": installer_url,
        "INSTALLER_SHA256": release_inputs.installer_sha256,
        "INSTALLER_SIZE": str(release_inputs.installer_size),
    }
    for key in _PLATFORM_KEYS:
        prefix = "UV_" + key.upper().replace("-", "_")
        tokens[f"{prefix}_URL"] = uv_pins["archives"][key]["url"]
        tokens[f"{prefix}_SHA256"] = uv_pins["archives"][key]["sha256"]

    for name, value in tokens.items():
        text = text.replace("{{" + name + "}}", value)
    if "{{" in text or "}}" in text:
        raise BootstrapRenderError("template has unresolved placeholders")
    if "latest" in text.lower():
        raise BootstrapRenderError("rendered script must not reference a floating version")
    return text


def _load_uv_pins(path: Path) -> dict[str, object]:
    """读取并严格验证四平台 uv pin，拒绝缺失或格式错误的条目。"""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        uv = document["uv"]
        version = uv["version"]
        archives = uv["archives"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise BootstrapRenderError("invalid runtime pins") from error
    if type(version) is not str or _SEMVER.fullmatch(version) is None:
        raise BootstrapRenderError("invalid runtime pins")
    if type(archives) is not dict or set(archives) != set(_PLATFORM_KEYS):
        raise BootstrapRenderError("invalid runtime pins")
    normalized: dict[str, dict[str, str]] = {}
    for key in _PLATFORM_KEYS:
        entry = archives[key]
        if type(entry) is not dict:
            raise BootstrapRenderError("invalid runtime pins")
        url = entry.get("url")
        digest = entry.get("sha256")
        if (
            type(url) is not str
            or not url.startswith(_UV_URL_PREFIX)
            or any(character.isspace() for character in url)
            or type(digest) is not str
            or _SHA256.fullmatch(digest) is None
        ):
            raise BootstrapRenderError("invalid runtime pins")
        normalized[key] = {"url": url, "sha256": digest}
    return {"version": version, "archives": normalized}


def _load_release_inputs(path: Path) -> ReleaseInputs:
    """从 fixture JSON 加载并构造严格校验的 ReleaseInputs。"""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapRenderError("invalid release fixture") from error
    if type(document) is not dict:
        raise BootstrapRenderError("invalid release fixture")
    try:
        return ReleaseInputs(
            release_tag=document["release_tag"],
            manifest_filename=document["manifest_filename"],
            manifest_sha256=document["manifest_sha256"],
            manifest_size=document["manifest_size"],
            installer_filename=document["installer_filename"],
            installer_sha256=document["installer_sha256"],
            installer_size=document["installer_size"],
        )
    except KeyError as error:
        raise BootstrapRenderError("invalid release fixture") from error


def main(argv: list[str] | None = None) -> int:
    """解析 CLI 参数并把渲染结果写到 output 路径。"""
    parser = argparse.ArgumentParser(description="Render the MiniClaw install.sh bootstrap")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--runtime-versions", type=Path, default=_DEFAULT_RUNTIME_VERSIONS)
    parser.add_argument("--template", type=Path, default=_DEFAULT_TEMPLATE)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        release_inputs = _load_release_inputs(arguments.fixture)
        text = render_install_script(
            release_inputs,
            runtime_versions=arguments.runtime_versions,
            template=arguments.template,
        )
    except BootstrapRenderError as error:
        parser.error(str(error))
        return 2
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(text, encoding="utf-8")
    arguments.output.chmod(0o755)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
