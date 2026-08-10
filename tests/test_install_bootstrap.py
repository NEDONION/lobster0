"""覆盖 pinned POSIX one-line bootstrap 模板与 renderer 的离线测试。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.render_install_script import (
    BootstrapRenderError,
    ReleaseInputs,
    render_install_script,
)
from scripts.render_install_script import (
    main as render_main,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _REPO_ROOT / "release" / "install.sh.tmpl"
_FAKE_BIN_SOURCE = _REPO_ROOT / "tests" / "install" / "fake_bootstrap_bin"
_GATE_FIXTURE = _REPO_ROOT / "tests" / "install" / "bootstrap-release.json"
_UV_ARCHIVE_NAMES = {
    "linux-x86_64": "uv-x86_64-unknown-linux-gnu.tar.gz",
    "linux-arm64": "uv-aarch64-unknown-linux-gnu.tar.gz",
    "macos-x86_64": "uv-x86_64-apple-darwin.tar.gz",
    "macos-arm64": "uv-aarch64-apple-darwin.tar.gz",
}
_PASSTHROUGH_COMMANDS = ("mktemp", "mkdir", "chmod", "rm")
_PROFILE_NAMES = (".profile", ".bashrc", ".bash_profile", ".zshrc")


class InstallBootstrapTest(unittest.TestCase):
    """覆盖 renderer 输出与离线 fake 环境下的完整 bootstrap 行为。"""

    def setUp(self) -> None:
        """构造一次性 fixture：fake Release 字节、runtime pins 与 hermetic PATH。"""
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

        self.manifest_bytes = b'{"schema_version":1,"version":"0.7.0"}\n'
        self.installer_bytes = b"stub installer pyz payload\n"
        self.uv_archive_bytes = b"fake uv release archive payload\n"

        self.release_inputs = ReleaseInputs(
            release_tag="v0.7.0",
            manifest_filename="release-manifest.json",
            manifest_sha256=hashlib.sha256(self.manifest_bytes).hexdigest(),
            manifest_size=len(self.manifest_bytes),
            installer_filename="lobster0-installer.pyz",
            installer_sha256=hashlib.sha256(self.installer_bytes).hexdigest(),
            installer_size=len(self.installer_bytes),
        )

        uv_sha256 = hashlib.sha256(self.uv_archive_bytes).hexdigest()
        self.runtime_versions_path = self.root / "runtime-versions.json"
        self.runtime_versions_path.write_text(
            json.dumps(
                {
                    "uv": {
                        "version": "0.12.0",
                        "archives": {
                            key: {
                                "url": (
                                    "https://github.com/astral-sh/uv/releases/download/"
                                    f"0.12.0/{name}"
                                ),
                                "sha256": uv_sha256,
                            }
                            for key, name in _UV_ARCHIVE_NAMES.items()
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        self.http_root = self.root / "http"
        self.http_root.mkdir()
        for name in _UV_ARCHIVE_NAMES.values():
            (self.http_root / name).write_bytes(self.uv_archive_bytes)
        (self.http_root / self.release_inputs.manifest_filename).write_bytes(
            self.manifest_bytes
        )
        (self.http_root / self.release_inputs.installer_filename).write_bytes(
            self.installer_bytes
        )

        self.bin_dir = self.root / "bin"
        self._build_hermetic_bin()

        self.home = self.root / "home"
        self.home.mkdir()

        self.tmp_root = self.root / "tmp"
        self.tmp_root.mkdir()

        self.call_log = self.root / "calls.log"

        self.script_path = self.root / "install.sh"
        self.rendered_text = self._render()
        self.script_path.write_text(self.rendered_text, encoding="utf-8")
        self.script_path.chmod(0o755)

    # -- fixture helpers -------------------------------------------------

    def _render(self, release_inputs: ReleaseInputs | None = None) -> str:
        """渲染当前 fixture 的 release inputs，返回完整脚本文本。"""
        return render_install_script(
            release_inputs or self.release_inputs,
            runtime_versions=self.runtime_versions_path,
            template=_TEMPLATE,
        )

    def _build_hermetic_bin(
        self,
        *,
        curl: bool = True,
        tar: bool = True,
        sha256sum: bool = True,
        shasum: bool = True,
        uname: bool = True,
        wc: bool = True,
    ) -> None:
        """构造一个只含所需真实工具与 fake 的封闭 PATH 目录。"""
        self.bin_dir.mkdir(exist_ok=True)
        python3_link = self.bin_dir / "python3"
        if python3_link.exists() or python3_link.is_symlink():
            python3_link.unlink()
        python3_link.symlink_to(sys.executable)
        for name in _PASSTHROUGH_COMMANDS:
            resolved = shutil.which(name)
            self.assertIsNotNone(resolved, f"missing real {name} on this host")
            link = self.bin_dir / name
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(resolved)
        wc_link = self.bin_dir / "wc"
        if wc_link.exists() or wc_link.is_symlink():
            wc_link.unlink()
        if wc:
            resolved = shutil.which("wc")
            self.assertIsNotNone(resolved, "missing real wc on this host")
            wc_link.symlink_to(resolved)
        wanted = {
            "curl": curl,
            "tar": tar,
            "uname": uname,
            "sha256sum": sha256sum,
            "shasum": shasum,
        }
        for name, include in wanted.items():
            destination = self.bin_dir / name
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            if include:
                shutil.copyfile(_FAKE_BIN_SOURCE / name, destination)
                destination.chmod(0o755)

    def _env(self, **overrides: str) -> dict[str, str]:
        """构造只含 bootstrap 所需变量的严格离线子进程环境。"""
        base = {
            "PATH": str(self.bin_dir),
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp_root),
            "FAKE_HTTP_ROOT": str(self.http_root),
            "FAKE_CALL_LOG": str(self.call_log),
        }
        base.update(overrides)
        return base

    def _run(
        self,
        *args: str,
        env_overrides: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess[bytes]:
        """在 ``/bin/sh`` 下以 hermetic 环境执行已渲染脚本。"""
        if self.call_log.exists():
            self.call_log.unlink()
        env = self._env(**(env_overrides or {}))
        return subprocess.run(
            ["/bin/sh", str(self.script_path), *args],
            env=env,
            capture_output=True,
            timeout=timeout,
        )

    def _calls(self, command: str) -> list[dict[str, object]]:
        """读取 fake call log 中匹配 command 名的调用记录。"""
        if not self.call_log.exists():
            return []
        records = []
        for line in self.call_log.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            record = json.loads(line)
            if record.get("cmd") == command:
                records.append(record)
        return records

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        """记录一个目录树下每个 regular file 的完整字节内容。"""
        snapshot: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                snapshot[str(path.relative_to(root))] = path.read_bytes()
        return snapshot

    # -- renderer tests ---------------------------------------------------

    def test_rendered_bootstrap_has_no_floating_or_unresolved_value(self) -> None:
        """渲染结果必须是完全替换、无浮动版本引用的固定脚本。"""
        script = self.rendered_text
        self.assertNotIn("latest", script)
        self.assertNotIn("{{", script)
        self.assertNotIn("}}", script)
        self.assertIn("UV_VERSION='0.12.0'", script)
        self.assertIn("RELEASE_TAG='v0.7.0'", script)
        self.assertIn(self.release_inputs.manifest_sha256, script)
        self.assertIn(self.release_inputs.installer_sha256, script)

    def test_renderer_rejects_invalid_release_inputs(self) -> None:
        """release inputs 的类型或格式错误必须在渲染前被拒绝。"""
        with self.assertRaises(BootstrapRenderError):
            ReleaseInputs(
                release_tag="latest",
                manifest_filename="release-manifest.json",
                manifest_sha256=self.release_inputs.manifest_sha256,
                manifest_size=1,
                installer_filename="lobster0-installer.pyz",
                installer_sha256=self.release_inputs.installer_sha256,
                installer_size=1,
            )

    def test_renderer_rejects_missing_uv_platform_pin(self) -> None:
        """runtime pins 缺少任一 Tier 1 平台必须被拒绝。"""
        document = json.loads(self.runtime_versions_path.read_text(encoding="utf-8"))
        del document["uv"]["archives"]["macos-arm64"]
        broken = self.root / "broken-runtime-versions.json"
        broken.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(BootstrapRenderError):
            render_install_script(
                self.release_inputs,
                runtime_versions=broken,
                template=_TEMPLATE,
            )

    def test_cli_gate_renders_syntactically_valid_script_with_default_pins(self) -> None:
        """Step4 gate 命令必须用生产 runtime-versions.json 渲染出合法 shell。"""
        output = self.root / "gate-install.sh"
        exit_code = render_main(
            ["--fixture", str(_GATE_FIXTURE), "--output", str(output)]
        )
        self.assertEqual(exit_code, 0)
        text = output.read_text(encoding="utf-8")
        self.assertNotIn("{{", text)
        self.assertNotIn("latest", text)
        for shell in ("sh", "bash"):
            completed = subprocess.run(
                [shell, "-n", str(output)],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    # -- shell syntax ------------------------------------------------------

    def test_syntax_is_valid_posix_and_bash(self) -> None:
        """渲染结果必须同时通过 ``sh -n`` 和 ``bash -n``。"""
        for shell in ("sh", "bash"):
            completed = subprocess.run(
                [shell, "-n", str(self.script_path)],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    # -- platform detection --------------------------------------------

    def test_unsupported_os_rejected_before_any_download(self) -> None:
        """未知 OS 必须在任何下载前 fail closed。"""
        completed = self._run(env_overrides={"FAKE_UNAME_S": "SunOS"})
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("curl"), [])
        self.assertFalse(any(self.tmp_root.iterdir()))

    def test_unsupported_arch_rejected_before_any_download(self) -> None:
        """未知架构必须在任何下载前 fail closed。"""
        completed = self._run(
            env_overrides={"FAKE_UNAME_S": "Linux", "FAKE_UNAME_M": "sparc64"}
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("curl"), [])
        self.assertFalse(any(self.tmp_root.iterdir()))

    # -- missing base commands ------------------------------------------

    def test_missing_curl_fails_closed(self) -> None:
        """缺少 curl 必须在创建任何私有目录前失败。"""
        self._build_hermetic_bin(curl=False)
        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(any(self.tmp_root.iterdir()))

    def test_missing_tar_fails_closed(self) -> None:
        """缺少 tar 必须在创建任何私有目录前失败。"""
        self._build_hermetic_bin(tar=False)
        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("curl"), [])
        self.assertFalse(any(self.tmp_root.iterdir()))

    def test_missing_both_hashers_fails_closed(self) -> None:
        """同时缺少 sha256sum 和 shasum 必须失败。"""
        self._build_hermetic_bin(sha256sum=False, shasum=False)
        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("curl"), [])
        self.assertFalse(any(self.tmp_root.iterdir()))

    def test_missing_wc_fails_closed(self) -> None:
        """缺少 wc 必须在创建任何私有目录前失败，而不是在中途报 could-not-size。"""
        self._build_hermetic_bin(wc=False)
        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("curl"), [])
        self.assertFalse(any(self.tmp_root.iterdir()))

    # -- checksum command fallback ---------------------------------------

    def test_checksum_prefers_sha256sum_when_both_present(self) -> None:
        """两种 hasher 都存在时优先使用 sha256sum。"""
        self._build_hermetic_bin(sha256sum=True, shasum=True)
        record = self.root / "record.json"
        completed = self._run(env_overrides={"FAKE_PYZ_RECORD": str(record)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(self._calls("sha256sum"))
        self.assertEqual(self._calls("shasum"), [])

    def test_checksum_falls_back_to_shasum_when_sha256sum_absent(self) -> None:
        """sha256sum 缺失时必须回退到 ``shasum -a 256``。"""
        self._build_hermetic_bin(sha256sum=False, shasum=True)
        record = self.root / "record.json"
        completed = self._run(env_overrides={"FAKE_PYZ_RECORD": str(record)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._calls("sha256sum"), [])
        self.assertTrue(self._calls("shasum"))

    # -- hash/size verification -------------------------------------------

    def test_hash_failure_stops_before_python_or_installer(self) -> None:
        """manifest 被篡改必须在触碰 uv/Python/installer 前失败。"""
        (self.http_root / self.release_inputs.manifest_filename).write_bytes(b"tampered")
        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("uv"), [])
        self.assertFalse((self.home / ".lobster0").exists())

    def test_installer_size_mismatch_is_rejected(self) -> None:
        """installer 字节数与 manifest 声明不一致时必须失败。"""
        (self.http_root / self.release_inputs.installer_filename).write_bytes(
            self.installer_bytes + b"extra"
        )
        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse((self.home / ".lobster0").exists())

    def test_manifest_size_mismatch_is_rejected(self) -> None:
        """manifest 字节数与渲染进脚本的 pinned size 不一致时必须失败。"""
        (self.http_root / self.release_inputs.manifest_filename).write_bytes(
            self.manifest_bytes + b"extra"
        )
        completed = self._run()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("uv"), [])
        self.assertFalse((self.home / ".lobster0").exists())

    def test_oversized_manifest_download_is_rejected_before_full_write(self) -> None:
        """伪造的超大响应必须被 curl ``--max-filesize`` 挡在完整落盘之前。

        没有这个上界，一个恶意/被攻破的端点可以在任何 size/hash 检查生效前
        把无限字节流进私有工作目录；这里断言 curl 确实带上了
        ``--max-filesize``，且从未到达 uv/installer 阶段。
        """
        oversized = self.manifest_bytes + b"x" * (self.release_inputs.manifest_size * 4)
        (self.http_root / self.release_inputs.manifest_filename).write_bytes(oversized)
        record = self.root / "record.json"
        completed = self._run(env_overrides={"FAKE_PYZ_RECORD": str(record)})

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("uv"), [])
        self.assertFalse(record.exists())
        self.assertFalse((self.home / ".lobster0").exists())
        self.assertFalse(any(self.tmp_root.iterdir()))

        manifest_calls = [
            call
            for call in self._calls("curl")
            if call["args"] and call["args"][-1].endswith(self.release_inputs.manifest_filename)
        ]
        self.assertTrue(manifest_calls)
        self.assertIn("--max-filesize", manifest_calls[0]["args"])
        max_filesize_index = manifest_calls[0]["args"].index("--max-filesize")
        self.assertEqual(
            manifest_calls[0]["args"][max_filesize_index + 1],
            str(self.release_inputs.manifest_size),
        )

    def test_interrupted_curl_download_aborts(self) -> None:
        """被截断的传输必须让 curl 以非零退出并中止 bootstrap。"""
        completed = self._run(
            env_overrides={"FAKE_CURL_TRUNCATE_MATCH": "release-manifest.json"}
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("uv"), [])

    def test_network_failure_aborts_before_extraction(self) -> None:
        """uv archive 下载失败必须在解压/校验前中止。"""
        completed = self._run(
            env_overrides={"FAKE_CURL_FAIL_MATCH": "uv-"}
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._calls("uv"), [])

    # -- private working tree ---------------------------------------------

    def test_private_bootstrap_tree_mode_is_owner_only(self) -> None:
        """私有 bootstrap 目录必须以 0700 创建。"""
        record = self.root / "record.json"
        completed = self._run(env_overrides={"FAKE_PYZ_RECORD": str(record)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(payload["work_dir_mode"], 0o700)

    # -- flag passthrough and exit code -----------------------------------

    def test_flag_passthrough_to_installer(self) -> None:
        """公开 flag 必须原样透传给 installer，internal flags 附加在前。"""
        record = self.root / "record.json"
        public_args = [
            "--version",
            "0.8.0-beta.1",
            "--channel",
            "dev",
            "--prefix",
            "/custom/prefix",
            "--no-onboard",
            "--json",
            "--verbose",
        ]
        completed = self._run(
            *public_args, env_overrides={"FAKE_PYZ_RECORD": str(record)}
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(record.read_text(encoding="utf-8"))
        argv = payload["argv"]

        self.assertEqual(argv[-len(public_args) :], public_args)
        self.assertIn("--manifest-file", argv)
        manifest_index = argv.index("--manifest-file")
        self.assertTrue(argv[manifest_index + 1].endswith(self.release_inputs.manifest_filename))
        self.assertIn("--manifest-sha256", argv)
        sha_index = argv.index("--manifest-sha256")
        self.assertEqual(argv[sha_index + 1], self.release_inputs.manifest_sha256)
        self.assertIn("--managed-uv", argv)
        self.assertIn("--managed-python-root", argv)
        self.assertIn("--managed-python-executable", argv)
        root_index = argv.index("--managed-python-root")
        executable_index = argv.index("--managed-python-executable")
        self.assertEqual(
            argv[executable_index + 1],
            f"{argv[root_index + 1]}/bin/python3.12",
        )

    def test_installer_exit_code_propagates(self) -> None:
        """installer 的退出码必须原样成为 bootstrap 的退出码。"""
        completed = self._run(env_overrides={"FAKE_PYZ_EXIT_CODE": "42"})
        self.assertEqual(completed.returncode, 42)

    def test_successful_run_never_invokes_uv_from_path(self) -> None:
        """成功路径只使用从 archive 解压出的 uv；PATH 上不存在任何 uv 命令。"""
        self.assertIsNone(shutil.which("uv", path=str(self.bin_dir)))
        record = self.root / "record.json"
        completed = self._run(env_overrides={"FAKE_PYZ_RECORD": str(record)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        uv_calls = self._calls("uv")
        self.assertEqual(
            [call["args"] for call in uv_calls],
            [
                ["python", "install", "3.12"],
                ["python", "find", "--managed-python", "3.12"],
            ],
        )

    # -- cleanup and state isolation ---------------------------------------

    def test_signal_cleanup_removes_private_tree(self) -> None:
        """收到 TERM 信号时必须清理私有 bootstrap 目录并以 143 (128+SIGTERM) 退出。

        信号发送给整个进程组（而不仅是 shell 本身），因为 POSIX shell 在
        阻塞等待前台子进程（这里是睡眠中的 fake curl）期间会推迟自身 trap
        的执行，直到该子进程先退出；这与真实终端 Ctrl-C 的语义一致。退出码
        显式断言为 143，而不是所有信号共用同一个码，这样包装脚本或 CI 才能
        区分到底是哪个信号中止了 bootstrap。
        """
        env = self._env(**{"FAKE_CURL_SLEEP": "5"})
        if self.call_log.exists():
            self.call_log.unlink()
        process = subprocess.Popen(
            ["/bin/sh", str(self.script_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            created = False
            while time.monotonic() < deadline:
                if any(self.tmp_root.iterdir()):
                    created = True
                    break
                time.sleep(0.05)
            self.assertTrue(created, "bootstrap never created its private tree")
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5.0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5.0)
        self.assertEqual(process.returncode, 143)
        self.assertFalse(any(self.tmp_root.iterdir()))

    def test_no_shell_profile_or_secret_state_is_touched(self) -> None:
        """bootstrap 绝不能读写 shell profile 或既有的 Secret 状态。"""
        for name in _PROFILE_NAMES:
            (self.home / name).write_bytes(f"sentinel:{name}\n".encode())
        lobster0_home = self.home / ".lobster0"
        lobster0_home.mkdir()
        secrets_file = lobster0_home / "secrets.env"
        secrets_file.write_bytes(b"LOBSTER0_SECRET_TOKEN=do-not-touch\n")
        before = self._snapshot(self.home)

        record = self.root / "record.json"
        completed = self._run(env_overrides={"FAKE_PYZ_RECORD": str(record)})
        self.assertEqual(completed.returncode, 0, completed.stderr)

        after = self._snapshot(self.home)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
