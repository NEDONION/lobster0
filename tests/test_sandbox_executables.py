"""Seatbelt executable chain 的冻结与执行前复核测试。"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from miniclaw.sandbox.base import SandboxPlanError
from miniclaw.sandbox.executables import (
    capture_executable_chain,
    verify_executable_chain,
)


class ExecutableChainTest(unittest.TestCase):
    """验证 direct/shebang/env chain 只绑定 exact regular executable。"""

    def setUp(self) -> None:
        """创建独立 executable root。"""
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def executable(self, name: str, content: bytes = b"native") -> Path:
        """创建一个仅供当前用户执行的普通 fixture。"""
        path = self.bin / name
        path.write_bytes(content)
        path.chmod(0o700)
        return path

    def test_direct_executable_is_one_hashed_ref(self) -> None:
        """直接 executable 必须绑定 resolved path 与完整内容 SHA-256。"""
        direct = self.executable("direct", b"direct-binary")

        chain = capture_executable_chain(direct, executable_path=str(self.bin))

        self.assertEqual(tuple(ref.path for ref in chain), (direct.resolve(),))
        self.assertEqual(chain[0].sha256, hashlib.sha256(b"direct-binary").hexdigest())

    def test_absolute_shebang_freezes_script_and_interpreter(self) -> None:
        """绝对 shebang 必须把脚本与 exact interpreter 都放入 chain。"""
        interpreter = self.executable("interpreter", b"native-interpreter")
        script = self.executable("script", f"#!{interpreter}\nbody\n".encode())

        chain = capture_executable_chain(script, executable_path=str(self.bin))

        self.assertEqual(tuple(ref.path for ref in chain), (script, interpreter))

    def test_env_shebang_freezes_script_env_and_exact_interpreter(self) -> None:
        """env shebang 的目标只能从显式 executable PATH 冻结。"""
        node = self.executable("node", b"native-node")
        script = self.executable("lark-cli", b"#!/usr/bin/env node\n")

        chain = capture_executable_chain(script, executable_path=str(self.bin))

        self.assertEqual(
            tuple(ref.path.name for ref in chain),
            ("lark-cli", "env", "node"),
        )
        self.assertEqual(chain[-1].path, node)

    def test_changed_executable_fails_verification_without_path_disclosure(self) -> None:
        """批准后的内容变化必须失败，异常不得回显真实 path。"""
        direct = self.executable("private-name", b"before")
        chain = capture_executable_chain(direct, executable_path=str(self.bin))
        direct.write_bytes(b"after")

        with self.assertRaises(SandboxPlanError) as raised:
            verify_executable_chain(chain)

        self.assertEqual(raised.exception.code, "execution_plan_executable_changed")
        self.assertNotIn("private-name", str(raised.exception))
        self.assertNotIn(str(self.root), str(raised.exception))

    def test_symlink_replacement_fails_verification(self) -> None:
        """ref 的最终文件被替换为 symlink 时 O_NOFOLLOW 必须拒绝。"""
        direct = self.executable("direct", b"same")
        replacement = self.executable("replacement", b"same")
        chain = capture_executable_chain(direct, executable_path=str(self.bin))
        direct.unlink()
        direct.symlink_to(replacement)

        with self.assertRaisesRegex(
            SandboxPlanError, "execution_plan_executable_changed"
        ):
            verify_executable_chain(chain)

    def test_unsupported_shebang_forms_fail_closed(self) -> None:
        """带参数、env -S、相对或不可解析 shebang 不能生成歧义 chain。"""
        cases = {
            "absolute-args": b"#!/bin/echo one\n",
            "env-s": b"#!/usr/bin/env -S node --trace\n",
            "relative": b"#!node\n",
            "shell": b"#!/bin/sh\n",
            "missing": b"#!/missing/interpreter\n",
            "missing-env": b"#!/usr/bin/env missing-node\n",
        }
        for name, content in cases.items():
            script = self.executable(name, content)
            with self.subTest(name=name), self.assertRaisesRegex(
                SandboxPlanError, "execution_plan_exec_chain_invalid"
            ):
                capture_executable_chain(script, executable_path=str(self.bin))

    def test_chain_longer_than_four_fails_closed(self) -> None:
        """递归 shebang chain 超过固定上限时不得静默截断。"""
        paths = [self.bin / f"tool-{index}" for index in range(5)]
        paths[-1].write_bytes(b"native")
        for index, path in enumerate(paths[:-1]):
            path.write_text(f"#!{paths[index + 1]}\n", encoding="utf-8")
        for path in paths:
            path.chmod(0o700)

        with self.assertRaisesRegex(
            SandboxPlanError, "execution_plan_exec_chain_invalid"
        ):
            capture_executable_chain(paths[0], executable_path=str(self.bin))

    def test_missing_non_regular_and_non_executable_inputs_fail_closed(self) -> None:
        """只接受存在、普通且带执行位的文件。"""
        non_executable = self.bin / "non-executable"
        non_executable.write_bytes(b"native")
        candidates = (self.bin / "missing", self.bin, non_executable)

        for candidate in candidates:
            with self.subTest(candidate=candidate.name), self.assertRaisesRegex(
                SandboxPlanError, "execution_plan_exec_chain_invalid"
            ):
                capture_executable_chain(candidate, executable_path=str(self.bin))


if __name__ == "__main__":
    unittest.main()
