#!/usr/bin/env python3
"""从已校验官方 archive 生成最小、可复现的 managed Node bundle。"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

_PLATFORM_SUFFIXES = {
    "linux-x86_64": "linux-x64",
    "linux-arm64": "linux-arm64",
    "macos-x86_64": "darwin-x64",
    "macos-arm64": "darwin-arm64",
}
_VERSION = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NodeBundleError(RuntimeError):
    """表示 Node bundle 输入、验证或写入失败。"""


def build_node_bundle(
    runtime_versions: Path,
    upstream_archive: Path,
    platform: str,
    output_directory: Path,
) -> Path:
    """校验官方 Node archive 并生成 deterministic symlink-free tar.gz。

    Args:
        runtime_versions: 包含 Node version、URL 与 upstream SHA-256 的 pins JSON。
        upstream_archive: 已下载的官方 Node `.tar.gz`。
        platform: `linux|macos` 与 `x86_64|arm64` 组成的 Tier 1 key。
        output_directory: 新 artifact 的父目录。

    Returns:
        已生成的 `miniclaw-node-<version>-<platform>.tar.gz`。

    Raises:
        NodeBundleError: pin、archive、Node probe 或输出不满足严格契约。
    """
    version, url, expected_hash = _load_node_pin(runtime_versions, platform)
    archive = Path(upstream_archive)
    if not archive.is_file() or archive.is_symlink():
        raise NodeBundleError("upstream archive must be a regular file")
    actual_hash = _sha256(archive)
    if actual_hash != expected_hash:
        raise NodeBundleError("archive hash mismatch")
    expected_name = f"node-v{version}-{_PLATFORM_SUFFIXES[platform]}.tar.gz"
    if archive.name != expected_name or PurePosixPath(urlsplit(url).path).name != expected_name:
        raise NodeBundleError("archive filename mismatch")

    with tempfile.TemporaryDirectory(prefix="miniclaw-node-bundle-") as temporary:
        staging = Path(temporary)
        node_root = staging / "node"
        (node_root / "bin").mkdir(parents=True)
        _copy_official_members(archive, version, platform, node_root)
        executable = node_root / "bin/node"
        executable.chmod(0o755)
        if not _reports_exact_version(executable, version):
            raise NodeBundleError("node version mismatch")
        component = {
            "name": "node",
            "platform": platform,
            "upstream_sha256": actual_hash,
            "upstream_url": url,
            "version": version,
        }
        (node_root / "release-component.json").write_bytes(_canonical_json(component))
        destination = Path(output_directory) / f"miniclaw-node-{version}-{platform}.tar.gz"
        return write_deterministic_tar(staging, destination)


def write_deterministic_tar(source_root: Path, destination: Path) -> Path:
    """把一个仅含 regular file/directory 的 tree 写成 deterministic gzip tar。

    Args:
        source_root: archive member 的共同父目录，不写入自身名称。
        destination: 必须尚不存在的 `.tar.gz` 输出路径。

    Returns:
        已完成且 metadata 归零的输出路径。

    Raises:
        NodeBundleError: source 含 link/special entry 或 destination 已存在。
    """
    root = Path(source_root)
    output = Path(destination)
    if not root.is_dir() or root.is_symlink():
        raise NodeBundleError("bundle source must be a regular directory")
    if output.exists() or output.is_symlink():
        raise NodeBundleError("bundle output already exists")
    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for entry in entries:
        metadata = entry.lstat()
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise NodeBundleError("bundle source contains a non-regular entry")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        raise NodeBundleError("temporary bundle output already exists")
    try:
        with temporary.open("xb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
            ) as zipped:
                with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    for entry in entries:
                        relative = entry.relative_to(root).as_posix()
                        metadata = entry.lstat()
                        member = tarfile.TarInfo(relative)
                        member.uid = member.gid = member.mtime = 0
                        member.uname = member.gname = ""
                        if stat.S_ISDIR(metadata.st_mode):
                            member.type = tarfile.DIRTYPE
                            member.mode = 0o755
                            archive.addfile(member)
                        else:
                            member.mode = 0o755 if metadata.st_mode & 0o111 else 0o644
                            member.size = metadata.st_size
                            with entry.open("rb") as source:
                                archive.addfile(member, source)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, output)
    except (OSError, tarfile.TarError, ValueError) as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise NodeBundleError("could not write deterministic bundle") from error
    return output


def _load_node_pin(runtime_versions: Path, platform: str) -> tuple[str, str, str]:
    """读取并严格验证一个目标平台的 Node runtime pin。"""
    if platform not in _PLATFORM_SUFFIXES:
        raise NodeBundleError("unsupported platform")
    try:
        document = json.loads(Path(runtime_versions).read_text(encoding="utf-8"))
        node = document["node"]
        version = node["version"]
        selected = node["archives"][platform]
        url = selected["url"]
        digest = selected["sha256"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise NodeBundleError("invalid runtime pins") from error
    if (
        type(version) is not str
        or _VERSION.fullmatch(version) is None
        or type(url) is not str
        or not url.startswith("https://nodejs.org/dist/")
        or type(digest) is not str
        or _SHA256.fullmatch(digest) is None
    ):
        raise NodeBundleError("invalid runtime pins")
    return version, url, digest


def _sha256(path: Path) -> str:
    """流式计算 regular file 的 SHA-256。"""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise NodeBundleError("could not read upstream archive") from error
    return digest.hexdigest()


def _copy_official_members(
    archive_path: Path,
    version: str,
    platform: str,
    destination: Path,
) -> None:
    """只从官方 archive 复制 regular `bin/node` 与 `LICENSE`。"""
    root = f"node-v{version}-{_PLATFORM_SUFFIXES[platform]}"
    wanted = {
        f"{root}/LICENSE": (destination / "LICENSE", 2 * 1024 * 1024),
        f"{root}/bin/node": (destination / "bin/node", 256 * 1024 * 1024),
    }
    found: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                selected = wanted.get(member.name)
                if selected is None:
                    continue
                if member.name in found or not member.isreg():
                    raise NodeBundleError("official Node members must be unique regular files")
                target, maximum = selected
                if member.size < 1 or member.size > maximum:
                    raise NodeBundleError("official Node member size is invalid")
                source = archive.extractfile(member)
                if source is None:
                    raise NodeBundleError("official Node member could not be read")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                found.add(member.name)
    except NodeBundleError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise NodeBundleError("invalid official Node archive") from error
    if found != set(wanted):
        raise NodeBundleError("official Node archive is missing required regular files")


def _reports_exact_version(executable: Path, version: str) -> bool:
    """在隔离环境中要求 managed Node 输出精确 patch version。"""
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            env={},
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (
        completed.returncode == 0
        and completed.stdout == f"v{version}\n".encode()
        and not completed.stderr
    )


def _canonical_json(document: object) -> bytes:
    """编码带结尾换行的 canonical UTF-8 JSON。"""
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{encoded}\n".encode()


def main() -> int:
    """解析 CLI 参数并构建一个平台 Node bundle。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-versions", type=Path, default=Path("release/runtime-versions.json")
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--platform", choices=tuple(_PLATFORM_SUFFIXES), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        output = build_node_bundle(
            arguments.runtime_versions,
            arguments.archive,
            arguments.platform,
            arguments.output_dir,
        )
    except NodeBundleError as error:
        parser.error(str(error))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
