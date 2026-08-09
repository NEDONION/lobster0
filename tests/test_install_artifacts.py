"""验证 installer 下载和 tar 解包在恶意输入下 fail closed。"""

from __future__ import annotations

import hashlib
import io
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request

from miniclaw.install.artifacts import ExtractionLimits, download_artifact, extract_tar_gz
from miniclaw.install.models import Artifact, InstallError, PlatformKey
from tests.install.make_archives import make_archive


class FakeResponse(io.BytesIO):
    """模拟 urllib response，但不访问网络。"""

    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """保存响应体、状态码和大小写不敏感场景所需 headers。"""
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def getcode(self) -> int:
        """返回 HTTP status。"""
        return self.status


class InterruptedResponse(FakeResponse):
    """模拟 headers 成功后读取被中断的响应。"""

    def read(self, size: int = -1) -> bytes:
        """在首次 body read 时抛出 timeout。"""
        del size
        raise TimeoutError("SECRET_RESPONSE_BODY")


class CloseFailingInterruptedResponse(InterruptedResponse):
    """模拟 read 与 close 都失败的响应。"""

    def close(self) -> None:
        """模拟 socket close 失败，确保仍清理下载 part。"""
        raise OSError("SECRET_CLOSE_FAILURE")


class FakeOpener:
    """按顺序返回离线 HTTP response 或异常。"""

    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        """保存预设响应队列和收到的 URL。"""
        self.responses = list(responses)
        self.urls: list[str] = []

    def open(self, request: Request, timeout: float | None = None) -> FakeResponse:
        """记录请求并弹出下一个 response。"""
        del timeout
        self.urls.append(request.full_url)
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class InstallArtifactTests(unittest.TestCase):
    """覆盖 verified download 和 bounded extraction。"""

    def setUp(self) -> None:
        """为每个测试创建独立 owner staging。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        """清理测试 staging。"""
        self.temporary.cleanup()

    def artifact(
        self,
        body: bytes,
        *,
        size: int | None = None,
        sha256: str | None = None,
    ) -> Artifact:
        """构造一个由 strict model 校验的测试 Artifact。"""
        return Artifact(
            kind="tui",
            filename="miniclaw-tui-0.7.0-linux-x86_64.tar.gz",
            url=(
                "https://github.com/NEDONION/miniclaw/releases/download/v0.7.0/"
                "miniclaw-tui-0.7.0-linux-x86_64.tar.gz"
            ),
            sha256=sha256 or hashlib.sha256(body).hexdigest(),
            size=len(body) if size is None else size,
            media_type="application/gzip",
            platform=PlatformKey("linux", "x86_64"),
            component_version="0.7.0",
            source_repository="https://github.com/NEDONION/miniclaw",
            license_ref="MIT",
            upstream_sha256=None,
        )

    def test_download_checks_length_stream_size_hash_and_mode_before_replace(self) -> None:
        """删除任一 size/hash 检查都会让不完整 artifact 落到 final target。"""
        body = b"trusted"
        target = self.root / "artifact.tar.gz"
        opener = FakeOpener(
            FakeResponse(body, headers={"Content-Length": str(len(body))}),
        )

        result = download_artifact(self.artifact(body), target, opener=opener)

        self.assertEqual(result, target)
        self.assertEqual(result.read_bytes(), body)
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        self.assertFalse(target.with_name(f"{target.name}.part").exists())

    def test_download_rejects_length_short_stream_oversize_and_wrong_hash(self) -> None:
        """任一声明/实际 size 或 hash 不一致都不得生成 target。"""
        cases = (
            (
                self.artifact(b"trusted"),
                FakeResponse(b"trusted", headers={"Content-Length": "8"}),
            ),
            (
                self.artifact(b"trusted!"),
                FakeResponse(b"trusted", headers={"Content-Length": "8"}),
            ),
            (
                self.artifact(b"trusted"),
                FakeResponse(b"trusted!"),
            ),
            (
                self.artifact(b"trusted", sha256="f" * 64),
                FakeResponse(b"trusted", headers={"Content-Length": "7"}),
            ),
            (
                self.artifact(b"trusted"),
                FakeResponse(b"trusted", headers={"Content-Length": "not-an-int"}),
            ),
        )
        for index, (artifact, response) in enumerate(cases):
            target = self.root / f"bad-{index}.tar.gz"
            with self.subTest(index=index), self.assertRaises(InstallError) as caught:
                download_artifact(artifact, target, opener=FakeOpener(response))
            self.assertIn(
                caught.exception.code,
                {"artifact_download_failed", "artifact_hash_mismatch"},
            )
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name(f"{target.name}.part").exists())

    def test_download_timeout_and_existing_paths_fail_without_leak_or_overwrite(self) -> None:
        """中断和 O_EXCL 冲突不得泄漏 body、覆盖 target 或删除他人的 part。"""
        body = b"trusted"
        interrupted = self.root / "interrupted.tar.gz"
        with self.assertRaises(InstallError) as caught:
            download_artifact(
                self.artifact(body),
                interrupted,
                opener=FakeOpener(InterruptedResponse(body)),
            )
        self.assertEqual(caught.exception.code, "artifact_download_failed")
        self.assertNotIn("SECRET_RESPONSE_BODY", str(caught.exception))
        self.assertFalse(interrupted.exists())
        self.assertFalse(interrupted.with_name(f"{interrupted.name}.part").exists())

        close_failure = self.root / "close-failure.tar.gz"
        with self.assertRaises(InstallError) as caught:
            download_artifact(
                self.artifact(body),
                close_failure,
                opener=FakeOpener(CloseFailingInterruptedResponse(body)),
            )
        self.assertEqual(caught.exception.code, "artifact_download_failed")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertFalse(close_failure.exists())
        self.assertFalse(close_failure.with_name(f"{close_failure.name}.part").exists())

        existing = self.root / "existing.tar.gz"
        existing.write_bytes(b"keep-target")
        with self.assertRaisesRegex(InstallError, "artifact_download_failed"):
            download_artifact(self.artifact(body), existing, opener=FakeOpener())
        self.assertEqual(existing.read_bytes(), b"keep-target")

        blocked = self.root / "blocked.tar.gz"
        part = blocked.with_name(f"{blocked.name}.part")
        part.write_bytes(b"keep-part")
        with self.assertRaisesRegex(InstallError, "artifact_download_failed"):
            download_artifact(self.artifact(body), blocked, opener=FakeOpener())
        self.assertEqual(part.read_bytes(), b"keep-part")
        self.assertFalse(blocked.exists())

    def test_download_revalidates_every_redirect_and_only_allows_github_asset_hop(self) -> None:
        """绕过任一 redirect 校验都会允许凭据、query、fragment 或外部 host。"""
        body = b"trusted"
        asset_url = "https://release-assets.githubusercontent.com/github-production-release-asset/trusted"
        target = self.root / "redirected.tar.gz"
        opener = FakeOpener(
            FakeResponse(b"SECRET_REDIRECT_BODY", status=302, headers={"Location": asset_url}),
            FakeResponse(body, headers={"Content-Length": "7"}),
        )
        self.assertEqual(
            download_artifact(self.artifact(body), target, opener=opener).read_bytes(),
            body,
        )
        self.assertEqual(opener.urls[-1], asset_url)

        invalid_locations = (
            "https://evil.example/artifact",
            "https://user:pass@release-assets.githubusercontent.com/artifact",
            "https://release-assets.githubusercontent.com/artifact?token=SECRET_QUERY",
            "https://release-assets.githubusercontent.com/artifact#SECRET_FRAGMENT",
            "http://release-assets.githubusercontent.com/artifact",
        )
        for index, location in enumerate(invalid_locations):
            destination = self.root / f"redirect-{index}.tar.gz"
            with self.subTest(location=location), self.assertRaises(InstallError) as caught:
                download_artifact(
                    self.artifact(body),
                    destination,
                    opener=FakeOpener(
                        FakeResponse(
                            b"SECRET_REDIRECT_BODY",
                            status=302,
                            headers={"Location": location},
                        )
                    ),
                )
            self.assertEqual(caught.exception.code, "artifact_download_failed")
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertFalse(destination.exists())

        chained = self.root / "chained.tar.gz"
        with self.assertRaisesRegex(InstallError, "artifact_download_failed"):
            download_artifact(
                self.artifact(body),
                chained,
                opener=FakeOpener(
                    FakeResponse(
                        b"",
                        status=302,
                        headers={
                            "Location": (
                                "https://github.com/NEDONION/miniclaw/releases/download/"
                                "v0.7.0/next"
                            )
                        },
                    ),
                    FakeResponse(
                        b"",
                        status=302,
                        headers={"Location": "https://evil.example/second-hop"},
                    ),
                ),
            )
        self.assertFalse(chained.exists())

    def test_tar_extracts_regular_members_with_declared_executable_only(self) -> None:
        """tar mode 不得越权；只有调用方声明的 regular path 保留 executable。"""
        archive = make_archive(self.root / "valid.tar.gz", "valid")
        destination = self.root / "output"

        extracted = extract_tar_gz(
            archive,
            destination,
            ExtractionLimits(32, 4096),
            executable_paths=frozenset({"bin/run"}),
        )

        self.assertEqual(
            tuple(path.relative_to(destination).as_posix() for path in extracted),
            ("bin", "bin/run", "README"),
        )
        self.assertEqual((destination / "bin/run").read_bytes(), b"#!/bin/sh\n")
        self.assertEqual(stat.S_IMODE((destination / "bin").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((destination / "bin/run").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((destination / "README").stat().st_mode), 0o600)

    def test_tar_rejects_escaping_special_duplicate_collision_and_budget_members(self) -> None:
        """header 校验必须在写入前拒绝全部逃逸、special、碰撞与预算超限。"""
        kinds = (
            "absolute",
            "dotdot",
            "dot",
            "symlink",
            "hardlink",
            "device",
            "fifo",
            "duplicate",
            "case_collision",
            "case_parent_collision",
            "unicode_collision",
            "too_many",
            "too_large",
        )
        for kind in kinds:
            archive = make_archive(self.root / f"{kind}.tar.gz", kind)
            destination = self.root / f"output-{kind}"
            destination.mkdir()
            with self.subTest(kind=kind), self.assertRaisesRegex(
                InstallError,
                "manifest_invalid|artifact_hash_mismatch",
            ):
                extract_tar_gz(archive, destination, ExtractionLimits(32, 4096))
            self.assertEqual(list(destination.rglob("*")), [])

    def test_tar_failure_keeps_destination_empty_and_rejects_missing_executable(self) -> None:
        """失败不能留下半解包文件，manifest executable 集合也必须闭合。"""
        archive = make_archive(self.root / "valid-missing-exec.tar.gz", "valid")
        destination = self.root / "failed-output"
        destination.mkdir()
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            extract_tar_gz(
                archive,
                destination,
                ExtractionLimits(32, 4096),
                executable_paths=frozenset({"bin/missing"}),
            )
        self.assertEqual(list(destination.rglob("*")), [])

        occupied = self.root / "occupied-output"
        occupied.mkdir()
        keep = occupied / "keep"
        keep.write_bytes(b"user-data")
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            extract_tar_gz(archive, occupied, ExtractionLimits(32, 4096))
        self.assertEqual(keep.read_bytes(), b"user-data")

    def test_extraction_limits_reject_bool_zero_and_excessive_values(self) -> None:
        """bool 或无界预算不得绕过 dataclass 的整数限制。"""
        cases = (
            (True, 1),
            (1, False),
            (0, 1),
            (1, 0),
            (20_001, 1),
            (1, 1_073_741_825),
        )
        for entries, size in cases:
            with self.subTest(entries=entries, size=size), self.assertRaisesRegex(
                InstallError,
                "manifest_invalid",
            ):
                ExtractionLimits(entries, size)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
