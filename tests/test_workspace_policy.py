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

    def test_resolution_failures_become_redacted_workspace_errors(self) -> None:
        """NUL 与 symlink loop 必须变成不含绝对路径的稳定 Guard 错误。"""
        loop = self.workspace / "loop"
        loop.symlink_to(loop)
        candidates = ("invalid" + chr(0) + "path", "loop/file.txt")
        for candidate in candidates:
            with self.subTest(candidate=repr(candidate)):
                with self.assertRaises(WorkspaceAccessError) as caught:
                    self.guard.resolve_read(self.context, candidate)
                self.assertEqual(caught.exception.code, "workspace_escape")
                self.assertNotIn(str(self.workspace.parent), str(caught.exception))

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

    def test_additional_credentials_and_keystores_are_denied_in_all_read_roots(self) -> None:
        """补充凭据名和常见密钥库后缀在所有读取根内都必须拒绝。"""
        candidates = (
            ".netrc",
            ".npmrc",
            ".git-credentials",
            ".pypirc",
            ".docker/config.json",
            "token.json",
            "secrets.json",
            "secrets.yaml",
            "client.pem",
            "private.key",
            "identity.p12",
            "identity.pfx",
            "truststore.jks",
            "release.keystore",
        )
        for candidate in candidates:
            for raw_path in (candidate, str(self.read_only / candidate)):
                with self.subTest(raw_path=raw_path):
                    with self.assertRaises(WorkspaceAccessError) as caught:
                        self.guard.resolve_read(self.context, raw_path)
                    self.assertEqual(caught.exception.code, "sensitive_path")

    def test_symlinks_to_additional_credentials_are_denied(self) -> None:
        """无害别名解析到新增凭据路径时也必须稳定拒绝。"""
        targets = (
            self.workspace / ".git-credentials",
            self.workspace / ".pypirc",
            self.workspace / ".docker" / "config.json",
        )
        for index, target in enumerate(targets):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("credential", encoding="utf-8")
            alias = self.workspace / f"credential-{index}.txt"
            alias.symlink_to(target)
            with self.subTest(target=target.name):
                with self.assertRaises(WorkspaceAccessError) as caught:
                    self.guard.resolve_read(self.context, alias.name)
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
            self.context.state_home / "miniclaw.db-wal",
            self.context.state_home / "miniclaw.db-shm",
            self.context.state_home / "miniclaw.db-journal",
            self.context.state_home / "logs" / "miniclaw.log",
            Path("/etc/shadow"),
            Path("/etc/gshadow"),
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

    def test_state_database_sidecar_aliases_are_denied_without_overmatching(self) -> None:
        """数据库 sidecar 的解析别名必须拒绝，类似前缀的普通文档仍可读。"""
        self.context.state_home.mkdir()
        allowed_context = ToolContext(
            user_id=self.context.user_id,
            session_id=self.context.session_id,
            turn_id=self.context.turn_id,
            state_home=self.context.state_home,
            workspace=self.context.workspace,
            read_only_roots=(self.context.state_home,),
        )
        for suffix in ("-wal", "-shm", "-journal"):
            target = self.context.state_home / f"miniclaw.db{suffix}"
            target.write_text("sqlite state", encoding="utf-8")
            alias = self.workspace / f"sidecar{suffix}.txt"
            alias.symlink_to(target)
            with self.subTest(suffix=suffix):
                with self.assertRaises(WorkspaceAccessError) as caught:
                    self.guard.resolve_read(allowed_context, alias.name)
                self.assertEqual(caught.exception.code, "sensitive_path")

        notes = self.context.state_home / "miniclaw.db-notes.md"
        notes.write_text("safe notes", encoding="utf-8")
        self.assertEqual(self.guard.resolve_read(allowed_context, str(notes)), notes)

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
