"""Workspace 读取边界测试。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.policy.workspace import WorkspaceAccessError, WorkspaceGuard
from miniclaw.tools.base import ToolContext


class WorkspaceGuardTest(unittest.TestCase):
    """验证模型路径只能解析到配置允许的读取根。"""

    def setUp(self) -> None:
        """创建互相隔离的 Workspace、只读根和外部目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name).resolve()
        self.workspace = root / "workspace"
        self.read_only = root / "shared"
        self.outside = root / "outside"
        for directory in (self.workspace, self.read_only, self.outside):
            directory.mkdir()
        self.context = ToolContext(
            user_id=1,
            session_id=1,
            turn_id=1,
            state_home=root / "state",
            workspace=self.workspace,
            read_only_roots=(self.read_only,),
        )
        self.guard = WorkspaceGuard()

    def test_resolve_read_allows_workspace_and_read_only_root(self) -> None:
        """Workspace 相对路径和只读根绝对路径必须被允许。"""
        workspace_file = self.workspace / "notes.txt"
        shared_file = self.read_only / "guide.md"
        workspace_file.write_text("notes", encoding="utf-8")
        shared_file.write_text("guide", encoding="utf-8")

        self.assertEqual(self.guard.resolve_read(self.context, "notes.txt"), workspace_file)
        self.assertEqual(
            self.guard.resolve_read(self.context, str(shared_file)),
            shared_file,
        )

    def test_resolve_read_rejects_parent_and_absolute_escape(self) -> None:
        """相对父目录和外部绝对路径都必须返回稳定逃逸错误码。"""
        for candidate in ("../outside.txt", str(self.outside / "outside.txt")):
            with self.subTest(candidate=candidate), self.assertRaises(
                WorkspaceAccessError
            ) as caught:
                self.guard.resolve_read(self.context, candidate)
            self.assertEqual(caught.exception.code, "workspace_escape")

    def test_symlink_cannot_escape_workspace(self) -> None:
        """Workspace 内的 symlink 不得把读取目标带到允许根之外。"""
        (self.workspace / "jump").symlink_to(self.outside, target_is_directory=True)

        with self.assertRaises(WorkspaceAccessError) as caught:
            self.guard.resolve_read(self.context, "jump/outside.txt")

        self.assertEqual(caught.exception.code, "workspace_escape")

    def test_sensitive_names_are_denied_even_inside_workspace(self) -> None:
        """常见凭据目录、私钥和凭据文件名必须大小写无关地拒绝。"""
        candidates = (
            ".env",
            ".env.local",
            ".SSH/id_ed25519",
            ".aws/credentials",
            ".gnupg/private-keys-v1.d/key",
            ".kube/config",
            ".config/gcloud/application_default_credentials.json",
            "id_rsa",
            "credentials.json",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                WorkspaceAccessError
            ) as caught:
                self.guard.resolve_read(self.context, candidate)
            self.assertEqual(caught.exception.code, "sensitive_path")

    def test_symlink_to_sensitive_file_is_denied(self) -> None:
        """无害逻辑名解析到敏感文件时也必须拒绝。"""
        sensitive = self.workspace / ".env"
        sensitive.write_text("SECRET=value", encoding="utf-8")
        (self.workspace / "alias.txt").symlink_to(sensitive)

        with self.assertRaises(WorkspaceAccessError) as caught:
            self.guard.resolve_read(self.context, "alias.txt")

        self.assertEqual(caught.exception.code, "sensitive_path")

    def test_state_system_and_container_paths_are_sensitive(self) -> None:
        """MiniClaw 状态、系统权限文件和容器 socket 必须使用敏感错误码拒绝。"""
        candidates = (
            self.context.state_home / "config.toml",
            self.context.state_home / "miniclaw.db",
            self.context.state_home / "logs" / "miniclaw.log",
            Path("/etc/shadow"),
            Path("/etc/sudoers"),
            Path("/var/run/docker.sock"),
            Path("/run/containerd/containerd.sock"),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(
                WorkspaceAccessError
            ) as caught:
                self.guard.resolve_read(self.context, str(candidate))
            self.assertEqual(caught.exception.code, "sensitive_path")

    def test_display_returns_only_paths_relative_to_the_allowed_root(self) -> None:
        """模型可见路径必须相对允许根，根目录本身显示为点号。"""
        workspace_file = self.workspace / "folder" / "notes.txt"
        shared_file = self.read_only / "guide.md"

        self.assertEqual(self.guard.display(self.context, workspace_file), "folder/notes.txt")
        self.assertEqual(
            self.guard.display(self.context, shared_file, root=self.read_only),
            "guide.md",
        )
        self.assertEqual(self.guard.display(self.context, self.workspace), ".")


if __name__ == "__main__":
    unittest.main()
