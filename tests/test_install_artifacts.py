"""验证 installer 下载和 tar 解包在恶意输入下 fail closed。"""

from __future__ import annotations

import hashlib
import io
import stat
import tempfile
import unittest
from http.client import HTTPMessage
from pathlib import Path
from unittest import mock
from urllib.request import Request

from miniclaw.install import artifacts as artifact_transport
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
        headers: object | None = None,
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
                "https://github.com/NEDONION/mini-claw/releases/download/v0.7.0/"
                "miniclaw-tui-0.7.0-linux-x86_64.tar.gz"
            ),
            sha256=sha256 or hashlib.sha256(body).hexdigest(),
            size=len(body) if size is None else size,
            media_type="application/gzip",
            platform=PlatformKey("linux", "x86_64"),
            component_version="0.7.0",
            source_repository="https://github.com/NEDONION/mini-claw",
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

    def test_download_removes_final_when_parent_fsync_fails_after_replace(self) -> None:
        """replace 后 durability failure 不得把失败下载留在 final target。"""
        body = b"trusted"
        target = self.root / "fsync-failed.tar.gz"
        with mock.patch.object(
            artifact_transport,
            "_fsync_directory",
            side_effect=OSError("SECRET_PARENT_FSYNC"),
        ):
            with self.assertRaises(InstallError) as caught:
                download_artifact(
                    self.artifact(body),
                    target,
                    opener=FakeOpener(FakeResponse(body)),
                )

        self.assertEqual(caught.exception.code, "artifact_download_failed")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name(f"{target.name}.part").exists())

    def test_download_quarantines_failed_final_when_unlink_fails_and_retry_succeeds(self) -> None:
        """cleanup unlink 失败也必须先移走 final，且 private residue 不能阻塞重试。"""
        body = b"trusted"
        target = self.root / "fsync-retry.tar.gz"
        real_fsync_directory = artifact_transport._fsync_directory
        attempts = 0

        def fail_first_parent_fsync(directory: Path) -> None:
            """仅让第一次 final parent fsync 失败。"""
            nonlocal attempts
            if directory == target.parent:
                attempts += 1
                if attempts == 1:
                    raise OSError("SECRET_PARENT_FSYNC")
            real_fsync_directory(directory)

        with (
            mock.patch.object(
                artifact_transport,
                "_fsync_directory",
                side_effect=fail_first_parent_fsync,
            ),
            mock.patch.object(Path, "unlink", side_effect=OSError("SECRET_CLEANUP")),
        ):
            with self.assertRaises(InstallError) as caught:
                download_artifact(
                    self.artifact(body),
                    target,
                    opener=FakeOpener(FakeResponse(body)),
                )
            self.assertEqual(caught.exception.code, "artifact_download_failed")
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name(f"{target.name}.part").exists())

            result = download_artifact(
                self.artifact(body),
                target,
                opener=FakeOpener(FakeResponse(body)),
            )

        self.assertEqual(result.read_bytes(), body)
        residues = tuple(self.root.glob(f".{target.name}.cleanup-*"))
        self.assertLessEqual(len(residues), 1)
        self.assertLessEqual(sum(path.stat().st_size for path in residues), len(body))

    def test_download_fsyncs_parent_after_quarantine_and_private_cleanup(self) -> None:
        """commit fsync 失败后须持久化 quarantine rename 与后续 cleanup。"""
        body = b"trusted"
        target = self.root / "durable-rollback.tar.gz"
        real_fsync_directory = artifact_transport._fsync_directory
        parent_states: list[tuple[bool, int]] = []

        def fail_commit_fsync_once(directory: Path) -> None:
            """记录 parent namespace，并只让第一次 commit fsync 失败。"""
            if directory == target.parent:
                residues = tuple(target.parent.glob(f".{target.name}.cleanup-*"))
                parent_states.append((target.exists(), len(residues)))
                if len(parent_states) == 1:
                    raise OSError("SECRET_COMMIT_FSYNC")
            real_fsync_directory(directory)

        with mock.patch.object(
            artifact_transport,
            "_fsync_directory",
            side_effect=fail_commit_fsync_once,
        ):
            with self.assertRaises(InstallError) as caught:
                download_artifact(
                    self.artifact(body),
                    target,
                    opener=FakeOpener(FakeResponse(body)),
                )

        self.assertEqual(caught.exception.code, "artifact_download_failed")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(parent_states, [(True, 0), (False, 1), (False, 0)])
        self.assertFalse(target.exists())

    def test_download_rollback_fsync_failure_keeps_original_stable_error(self) -> None:
        """补偿 fsync 再失败也不得泄漏原因或替换原稳定 InstallError。"""
        body = b"trusted"
        target = self.root / "rollback-fsync-failed.tar.gz"
        real_fsync_directory = artifact_transport._fsync_directory
        parent_fsyncs = 0

        def fail_commit_and_rollback_fsync(directory: Path) -> None:
            """让 commit 和首次 rollback parent fsync 均瞬时失败。"""
            nonlocal parent_fsyncs
            if directory == target.parent:
                parent_fsyncs += 1
                if parent_fsyncs <= 2:
                    raise OSError(f"SECRET_PARENT_FSYNC_{parent_fsyncs}")
            real_fsync_directory(directory)

        with mock.patch.object(
            artifact_transport,
            "_fsync_directory",
            side_effect=fail_commit_and_rollback_fsync,
        ):
            with self.assertRaises(InstallError) as caught:
                download_artifact(
                    self.artifact(body),
                    target,
                    opener=FakeOpener(FakeResponse(body)),
                )

        self.assertEqual(caught.exception.code, "artifact_download_failed")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertGreaterEqual(parent_fsyncs, 2)
        self.assertFalse(target.exists())

    def test_download_rejects_duplicate_case_insensitive_security_headers(self) -> None:
        """真实 HTTPMessage 中重复 CL/Location/Encoding 不能靠首值绕过校验。"""
        body = b"trusted"
        asset_url = "https://release-assets.githubusercontent.com/asset/trusted"
        cases: list[tuple[str, FakeOpener]] = []

        lengths = HTTPMessage()
        lengths["Content-Length"] = "7"
        lengths["content-length"] = "7"
        cases.append(("length", FakeOpener(FakeResponse(body, headers=lengths))))

        encodings = HTTPMessage()
        encodings["Content-Encoding"] = "identity"
        encodings["content-encoding"] = "identity"
        cases.append(("encoding", FakeOpener(FakeResponse(body, headers=encodings))))

        locations = HTTPMessage()
        locations["Location"] = asset_url
        locations["location"] = "https://evil.example/second"
        cases.append(
            (
                "location",
                FakeOpener(
                    FakeResponse(b"", status=302, headers=locations),
                    FakeResponse(body),
                ),
            )
        )

        for name, opener in cases:
            with self.subTest(name=name):
                target = self.root / f"duplicate-{name}.tar.gz"
                with self.assertRaisesRegex(InstallError, "artifact_download_failed"):
                    download_artifact(self.artifact(body), target, opener=opener)
                self.assertFalse(target.exists())

    def test_tar_restores_existing_empty_destination_on_replace_or_fsync_failure(self) -> None:
        """archive commit 失败必须恢复原 empty 0700 destination，不能使其消失或非空。"""
        archive = make_archive(self.root / "commit-failure.tar.gz", "valid")
        real_replace = artifact_transport.os.replace
        real_fsync_directory = artifact_transport._fsync_directory

        replace_destination = self.root / "replace-failure"
        replace_destination.mkdir(mode=0o700)

        def fail_work_replace(source: str | Path, target: str | Path) -> None:
            """只让 extraction work 到 final 的 replace 失败，允许 rollback。"""
            name = Path(source).name
            if name.startswith(f".{replace_destination.name}.extract-") and not name.endswith(
                ".previous"
            ):
                raise OSError("SECRET_REPLACE_FAILURE")
            real_replace(source, target)

        with mock.patch.object(artifact_transport.os, "replace", side_effect=fail_work_replace):
            with self.assertRaisesRegex(InstallError, "manifest_invalid"):
                extract_tar_gz(archive, replace_destination, ExtractionLimits(32, 4096))
        self.assertTrue(replace_destination.is_dir())
        self.assertEqual(list(replace_destination.iterdir()), [])
        self.assertEqual(stat.S_IMODE(replace_destination.stat().st_mode), 0o700)

        fsync_destination = self.root / "fsync-failure"
        fsync_destination.mkdir(mode=0o700)

        def fail_final_parent(directory: Path) -> None:
            """只在 final 已落位后模拟 parent fsync 失败。"""
            if directory == fsync_destination.parent and (fsync_destination / "README").exists():
                raise OSError("SECRET_FSYNC_FAILURE")
            real_fsync_directory(directory)

        with mock.patch.object(
            artifact_transport,
            "_fsync_directory",
            side_effect=fail_final_parent,
        ):
            with self.assertRaisesRegex(InstallError, "manifest_invalid"):
                extract_tar_gz(archive, fsync_destination, ExtractionLimits(32, 4096))
        self.assertTrue(fsync_destination.is_dir())
        self.assertEqual(list(fsync_destination.iterdir()), [])
        self.assertEqual(stat.S_IMODE(fsync_destination.stat().st_mode), 0o700)

    def test_tar_rollback_is_atomic_when_private_cleanup_rmtree_fails(self) -> None:
        """commit fsync 失败须先隐藏 new tree、恢复 previous，再 best-effort 清 private residue。"""
        archive = make_archive(self.root / "atomic-rollback.tar.gz", "valid")
        destination = self.root / "atomic-rollback"
        destination.mkdir(mode=0o700)
        limits = ExtractionLimits(32, 4096)
        real_fsync_directory = artifact_transport._fsync_directory
        previous_seen = False

        def fail_commit_fsync(directory: Path) -> None:
            """记录 previous 仍在，再让第一次 final parent fsync 失败。"""
            nonlocal previous_seen
            if directory == destination.parent and (destination / "README").exists():
                previous_seen = any(
                    path.name.startswith(f".{destination.name}.extract-")
                    and path.name.endswith(".previous")
                    for path in destination.parent.iterdir()
                )
                raise OSError("SECRET_COMMIT_FSYNC")
            real_fsync_directory(directory)

        with (
            mock.patch.object(
                artifact_transport,
                "_fsync_directory",
                side_effect=fail_commit_fsync,
            ),
            mock.patch.object(
                artifact_transport.shutil,
                "rmtree",
                side_effect=OSError("SECRET_CLEANUP"),
            ),
        ):
            with self.assertRaises(InstallError) as caught:
                extract_tar_gz(archive, destination, limits)

        self.assertEqual(caught.exception.code, "manifest_invalid")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertTrue(previous_seen)
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        residues = tuple(destination.parent.glob(f".{destination.name}.extract-*"))
        self.assertLessEqual(len(residues), 1)
        residue_bytes = sum(
            path.stat().st_size
            for residue in residues
            for path in residue.rglob("*")
            if path.is_file()
        )
        self.assertLessEqual(residue_bytes, limits.max_bytes)

    def test_tar_fsyncs_parent_after_rollback_and_private_cleanup(self) -> None:
        """原空目录与原不存在两路 rollback 都须持久化补偿和 cleanup。"""
        archive = make_archive(self.root / "durable-tar-rollback.tar.gz", "valid")
        real_fsync_directory = artifact_transport._fsync_directory

        for existed in (True, False):
            destination = self.root / f"durable-tar-rollback-{existed}"
            if existed:
                destination.mkdir(mode=0o700)
            parent_states: list[tuple[bool, bool, int, int]] = []

            def fail_commit_fsync_once(
                directory: Path,
                watched: Path = destination,
                states: list[tuple[bool, bool, int, int]] = parent_states,
            ) -> None:
                """记录 final、previous 与 work，并只拒绝第一次 commit fsync。"""
                if directory == watched.parent:
                    works = tuple(
                        path
                        for path in watched.parent.glob(
                            f".{watched.name}.extract-*"
                        )
                        if not path.name.endswith(".previous")
                    )
                    previous = tuple(
                        watched.parent.glob(f".{watched.name}.extract-*.previous")
                    )
                    states.append(
                        (
                            watched.exists(),
                            (watched / "README").exists(),
                            len(works),
                            len(previous),
                        )
                    )
                    if len(states) == 1:
                        raise OSError("SECRET_COMMIT_FSYNC")
                real_fsync_directory(directory)

            with self.subTest(existed=existed), mock.patch.object(
                artifact_transport,
                "_fsync_directory",
                side_effect=fail_commit_fsync_once,
            ):
                with self.assertRaises(InstallError) as caught:
                    extract_tar_gz(archive, destination, ExtractionLimits(32, 4096))

            self.assertEqual(caught.exception.code, "manifest_invalid")
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertEqual(
                parent_states,
                [
                    (True, True, 0, 1 if existed else 0),
                    (existed, False, 1, 0),
                    (existed, False, 0, 0),
                ],
            )
            if existed:
                self.assertTrue(destination.is_dir())
                self.assertEqual(list(destination.iterdir()), [])
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            else:
                self.assertFalse(destination.exists())

    def test_tar_rollback_fsync_failure_keeps_original_stable_error(self) -> None:
        """tar 补偿 fsync 再失败仍返回原稳定错误并保留 rollback namespace。"""
        archive = make_archive(self.root / "tar-rollback-fsync-failed.tar.gz", "valid")
        destination = self.root / "tar-rollback-fsync-failed"
        destination.mkdir(mode=0o700)
        real_fsync_directory = artifact_transport._fsync_directory
        parent_fsyncs = 0

        def fail_commit_and_rollback_fsync(directory: Path) -> None:
            """让 commit 和首次 rollback parent fsync 均瞬时失败。"""
            nonlocal parent_fsyncs
            if directory == destination.parent:
                parent_fsyncs += 1
                if parent_fsyncs <= 2:
                    raise OSError(f"SECRET_PARENT_FSYNC_{parent_fsyncs}")
            real_fsync_directory(directory)

        with mock.patch.object(
            artifact_transport,
            "_fsync_directory",
            side_effect=fail_commit_and_rollback_fsync,
        ):
            with self.assertRaises(InstallError) as caught:
                extract_tar_gz(archive, destination, ExtractionLimits(32, 4096))

        self.assertEqual(caught.exception.code, "manifest_invalid")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertGreaterEqual(parent_fsyncs, 2)
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)

    def test_tar_durable_commit_survives_previous_cleanup_and_second_fsync_failures(self) -> None:
        """第一次 parent fsync 后 cleanup 失败只能留下 private residue，不能回退 final。"""
        archive = make_archive(self.root / "durable-cleanup.tar.gz", "valid")

        rmdir_destination = self.root / "rmdir-cleanup"
        rmdir_destination.mkdir(mode=0o700)
        real_rmdir = Path.rmdir

        def fail_previous_rmdir(path: Path) -> None:
            """只拒绝删除 durable commit 的 empty previous。"""
            if path.name.endswith(".previous"):
                raise OSError("SECRET_PREVIOUS_CLEANUP")
            real_rmdir(path)

        with mock.patch.object(
            Path,
            "rmdir",
            autospec=True,
            side_effect=fail_previous_rmdir,
        ):
            extracted = extract_tar_gz(
                archive,
                rmdir_destination,
                ExtractionLimits(32, 4096),
            )
        self.assertTrue(extracted)
        self.assertEqual((rmdir_destination / "README").read_bytes(), b"trusted\n")

        fsync_destination = self.root / "second-fsync-cleanup"
        fsync_destination.mkdir(mode=0o700)
        real_fsync_directory = artifact_transport._fsync_directory
        final_parent_fsyncs = 0

        def fail_second_final_parent_fsync(directory: Path) -> None:
            """第一次 durability fsync 成功，只让 cleanup 后第二次 fsync 失败。"""
            nonlocal final_parent_fsyncs
            if directory == fsync_destination.parent and (fsync_destination / "README").exists():
                final_parent_fsyncs += 1
                if final_parent_fsyncs == 2:
                    raise OSError("SECRET_SECOND_FSYNC")
            real_fsync_directory(directory)

        with mock.patch.object(
            artifact_transport,
            "_fsync_directory",
            side_effect=fail_second_final_parent_fsync,
        ):
            extracted = extract_tar_gz(
                archive,
                fsync_destination,
                ExtractionLimits(32, 4096),
            )
        self.assertTrue(extracted)
        self.assertEqual(final_parent_fsyncs, 2)
        self.assertEqual((fsync_destination / "README").read_bytes(), b"trusted\n")

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
                                "https://github.com/NEDONION/mini-claw/releases/download/"
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

    def test_tar_rejects_bounded_pax_gnu_longname_and_sparse_pseudo_metadata(self) -> None:
        """raw tar budget/type gate 必须在 tarfile 吞掉 pseudo headers 前拒绝它们。"""
        for kind in ("pax", "gnu_longname", "gnu_sparse"):
            with self.subTest(kind=kind):
                archive = make_archive(self.root / f"pseudo-{kind}.tar.gz", kind)
                destination = self.root / f"pseudo-output-{kind}"
                destination.mkdir(mode=0o700)
                with self.assertRaisesRegex(InstallError, "manifest_invalid"):
                    extract_tar_gz(archive, destination, ExtractionLimits(2, 32))
                self.assertEqual(list(destination.iterdir()), [])

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
