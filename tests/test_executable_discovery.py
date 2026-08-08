"""Personal Profile 的可信本机 CLI 发现测试。"""

import os
import tempfile
import unittest
from pathlib import Path

from miniclaw.policy.executables import discover_executables


class ExecutableDiscoveryTest(unittest.TestCase):
    """验证发现器只组合确定性目录，不执行用户 Shell 配置。"""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()

    def _executable(self, relative: str) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    def test_personal_discovers_nvm_uv_pnpm_and_local_bins_in_stable_order(self) -> None:
        """常见用户级安装器目录必须按稳定顺序加入最小 PATH。"""
        lark_cli = self._executable(
            ".config/nvm/versions/node/v20.19.0/bin/lark-cli"
        )
        self._executable(".nvm/versions/node/v22.19.0/bin/node")
        self._executable(".local/share/uv/tools/demo/bin/demo")
        self._executable(".local/share/pnpm/pnpm")
        self._executable(".local/bin/local-tool")
        explicit = self.root / "explicit-bin"
        explicit.mkdir()

        environment = discover_executables(
            "personal",
            home=self.home,
            explicit_roots=(explicit,),
            discover_user=True,
            platform_name="darwin",
        )

        roots = environment.search_roots
        self.assertEqual(environment.home, self.home)
        self.assertIn(explicit, roots)
        self.assertIn(lark_cli.parent, roots)
        self.assertLess(roots.index(explicit), roots.index(lark_cli.parent))
        self.assertLess(
            roots.index(lark_cli.parent),
            roots.index(self.home / ".nvm/versions/node/v22.19.0/bin"),
        )
        self.assertIn(self.home / ".local/share/uv/tools/demo/bin", roots)
        self.assertIn(self.home / ".local/share/pnpm", roots)
        self.assertIn(self.home / ".local/bin", roots)
        self.assertEqual(environment.path_value, os.pathsep.join(map(str, roots)))
        self.assertEqual(len(roots), len(set(roots)))

    def test_workspace_profile_never_discovers_home(self) -> None:
        """旧 Workspace Profile 即使收到 Home 也不得静默扩大 PATH。"""
        self._executable(".config/nvm/versions/node/v20.19.0/bin/lark-cli")

        environment = discover_executables(
            "workspace",
            home=self.home,
            explicit_roots=(),
            discover_user=False,
            platform_name="darwin",
        )

        self.assertIsNone(environment.home)
        self.assertFalse(any(root.is_relative_to(self.home) for root in environment.search_roots))

    def test_invalid_or_symlink_explicit_roots_fail_closed(self) -> None:
        """相对、缺失、文件和 symlink 显式 Root 都不能进入可执行 PATH。"""
        real = self.root / "real-bin"
        real.mkdir()
        alias = self.root / "alias-bin"
        alias.symlink_to(real, target_is_directory=True)
        file_root = self.root / "file"
        file_root.write_text("x", encoding="utf-8")
        candidates = (
            Path("relative-bin"),
            self.root / "missing-bin",
            file_root,
            alias,
        )

        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                discover_executables(
                    "personal",
                    home=self.home,
                    explicit_roots=(candidate,),
                    discover_user=False,
                    platform_name="darwin",
                )

    def test_discovery_ignores_shell_startup_files_and_symlink_user_roots(self) -> None:
        """发现过程不读取或执行 zshrc，用户发现目录也不能借 symlink 注入。"""
        marker = self.root / "sourced"
        (self.home / ".zshrc").write_text(
            f"touch {marker}\n",
            encoding="utf-8",
        )
        external = self.root / "external-bin"
        external.mkdir()
        cargo = self.home / ".cargo"
        cargo.mkdir()
        (cargo / "bin").symlink_to(external, target_is_directory=True)

        environment = discover_executables(
            "personal",
            home=self.home,
            explicit_roots=(),
            discover_user=True,
            platform_name="darwin",
        )

        self.assertFalse(marker.exists())
        self.assertNotIn(external, environment.search_roots)


if __name__ == "__main__":
    unittest.main()
