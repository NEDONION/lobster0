"""非 root 部署镜像与 Phase 6 Sandbox 镜像的离线 Dockerfile 契约测试。"""

import re
import tempfile
import unittest
from pathlib import Path

from lobster0 import __version__
from lobster0.sandbox.base import ExecutionPlan
from lobster0.sandbox.docker import DockerSandbox

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO_ROOT / "deploy" / "Dockerfile"
_SANDBOX_DOCKERFILE = _REPO_ROOT / "deploy" / "sandbox.Dockerfile"
_ENTRYPOINT = _REPO_ROOT / "deploy" / "entrypoint.sh"

_DIGEST_PINNED_FROM = re.compile(
    r"^FROM [a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}(?: AS [a-z0-9][a-z0-9-]*)?$"
)
_SECRET_NAME = re.compile(
    r"(?i)(secret|token|api[_-]?key|apikey|password|passwd|credential|private[_-]?key)"
)
_RESOLVER_INSTALL = re.compile(
    r"(?i)\b(apt|apt-get|aptitude|apk|yum|dnf|rpm|pip|pip3|easy_install|uv|poetry"
    r"|pdm|pipenv|conda|npm|pnpm|yarn|corepack|gem|cargo)\b[^\n]*"
    r"\b(install|add|sync|update|upgrade|download|fetch)\b"
)
_PROVIDER_KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "LOBSTER0_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "FEISHU_APP_SECRET",
)
_REMOVED_RESOLVER_PATHS = (
    "ensurepip",
    "site-packages/pip",
    "/usr/local/bin/pip",
    "/usr/bin/apt",
    "/var/lib/apt",
    "/var/lib/dpkg",
)


def _stages(text: str) -> tuple[tuple[str, str], ...]:
    """把 Dockerfile 文本切成 ``(stage_name, stage_body)`` 序列。"""
    stages: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("FROM "):
            name = stripped.split(" AS ")[1].strip() if " AS " in stripped else ""
            stages.append((name, [line]))
        elif stages:
            stages[-1][1].append(line)
    return tuple((name, "\n".join(body)) for name, body in stages)


def _directives(text: str, keyword: str) -> tuple[str, ...]:
    """返回某个 Dockerfile 指令的全部规范化单行文本。"""
    joined = re.sub(r"\\\n\s*", " ", text)
    return tuple(
        re.sub(r"\s+", " ", line).strip()
        for line in joined.splitlines()
        if line.strip().startswith(f"{keyword} ")
    )


def _env_assignments(text: str) -> tuple[str, ...]:
    """把所有 ENV 指令展开成 ``NAME=VALUE`` 赋值序列。"""
    assignments: list[str] = []
    for line in _directives(text, "ENV"):
        assignments.extend(
            token for token in line[len("ENV ") :].split(" ") if "=" in token
        )
    return tuple(assignments)


class DeployImageFilesTest(unittest.TestCase):
    """部署镜像的三个文件必须存在且可读。"""

    def test_task16_deployment_files_exist(self) -> None:
        """Runtime、Sandbox Dockerfile 与稳定 entrypoint 必须同时交付。"""
        for path in (_DOCKERFILE, _SANDBOX_DOCKERFILE, _ENTRYPOINT):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"missing {path}")
                self.assertFalse(path.is_symlink())


class RuntimeImageContractTest(unittest.TestCase):
    """验证 GHCR Runtime 镜像是 digest-pinned、非 root 且不含 resolver。"""

    def setUp(self) -> None:
        """读取 Dockerfile 文本并切分 stage。"""
        self.dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
        self.stages = _stages(self.dockerfile)
        self.assertTrue(self.stages, "Dockerfile declares no stage")
        self.final_stage = self.stages[-1][1]

    def test_runtime_image_is_digest_pinned_and_ends_non_root(self) -> None:
        """每个 FROM 固定 sha256 digest，镜像以 65532 非 root 结束。"""
        from_lines = _directives(self.dockerfile, "FROM")
        self.assertGreaterEqual(len(from_lines), 2, "image must be multi-stage")
        for line in from_lines:
            with self.subTest(line=line):
                self.assertRegex(line, _DIGEST_PINNED_FROM)
        self.assertIn("USER 65532:65532", self.dockerfile)
        self.assertNotIn("curl |", self.dockerfile)
        self.assertNotIn("latest", self.dockerfile)
        user_lines = _directives(self.final_stage, "USER")
        self.assertTrue(user_lines)
        self.assertEqual(user_lines[-1], "USER 65532:65532")
        self.assertNotIn("USER root", self.final_stage.split("USER 65532:65532")[-1])

    def test_runtime_image_declares_no_secret_argument_or_environment(self) -> None:
        """不得声明任何 Secret ARG/ENV，也不得内置默认 Provider key。"""
        for keyword in ("ARG", "ENV"):
            for line in _directives(self.dockerfile, keyword):
                with self.subTest(line=line):
                    self.assertIsNone(
                        _SECRET_NAME.search(line.replace("SHA256", "")),
                        f"secret-bearing {keyword}: {line}",
                    )
        for name in _PROVIDER_KEY_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(name, self.dockerfile)

    def test_runtime_image_copies_exact_wheel_and_hash_locked_requirements(self) -> None:
        """builder 只复制 exact wheel 与 lock，并强制 hash 校验安装。"""
        wheel = f"lobster0_agent-{__version__}-py3-none-any.whl"
        copies = _directives(self.dockerfile, "COPY")
        self.assertIn("COPY requirements-all.lock /build/requirements-all.lock", copies)
        self.assertIn(f"COPY dist/{wheel} /build/{wheel}", copies)
        self.assertEqual(_directives(self.dockerfile, "ADD"), ())
        for forbidden in ("COPY . ", "COPY ./ ", "COPY src "):
            self.assertNotIn(forbidden, self.dockerfile)
        builder = self.stages[0][1]
        self.assertIn("--require-hashes", builder)
        self.assertIn("--no-deps", builder)
        self.assertIn("--no-index", builder)
        self.assertIn("/opt/lobster0/venv", builder)

    def test_runtime_final_stage_carries_no_package_resolver(self) -> None:
        """final layer 既不调用 resolver，也必须显式移除 pip/apt 事实。"""
        self.assertIsNone(
            _RESOLVER_INSTALL.search(self.final_stage),
            "final stage invokes a package resolver",
        )
        for removed in _REMOVED_RESOLVER_PATHS:
            with self.subTest(removed=removed):
                self.assertIn(removed, self.final_stage)
        self.assertIn("command -v", self.final_stage)
        copy_from_builder = [
            line for line in _directives(self.final_stage, "COPY") if "--from=builder" in line
        ]
        self.assertTrue(copy_from_builder)
        for line in copy_from_builder:
            with self.subTest(line=line):
                self.assertTrue(line.endswith("/opt/lobster0 /opt/lobster0"))

    def test_runtime_image_uses_owner_only_state_and_managed_runtime_paths(self) -> None:
        """`/data` 是 owner-only 状态根，Node/TUI 走受管路径。"""
        assignments = _env_assignments(self.final_stage)
        self.assertIn("LOBSTER0_HOME=/data", assignments)
        self.assertIn("HOME=/data", assignments)
        self.assertIn("LOBSTER0_NODE=/opt/lobster0/current/node/bin/node", assignments)
        self.assertIn("LOBSTER0_TUI_ENTRY=/opt/lobster0/current/tui/dist/main.js", assignments)
        self.assertIn("chmod 700 /data", self.final_stage)
        self.assertIn("chown 65532:65532 /data", self.final_stage)
        self.assertIn('VOLUME ["/data"]', self.dockerfile)
        self.assertIn("nonroot:x:65532:65532:", self.final_stage)

    def test_runtime_image_exposes_no_docker_socket_or_port(self) -> None:
        """镜像不得挂载或引用 Docker socket，也不预开放端口。"""
        self.assertNotIn("docker.sock", self.dockerfile)
        self.assertNotIn("/var/run/docker", self.dockerfile)
        self.assertNotIn("DOCKER_HOST", self.dockerfile)
        self.assertEqual(_directives(self.dockerfile, "EXPOSE"), ())
        volumes = _directives(self.dockerfile, "VOLUME")
        self.assertEqual(volumes, ('VOLUME ["/data"]',))

    def test_runtime_image_smokes_cli_version_and_channel_imports_at_build(self) -> None:
        """构建期必须验证 CLI 版本与全部 Channel import。"""
        self.assertIn("lobster0 --version", self.dockerfile)
        for module in (
            "lobster0.cli",
            "lobster0.channels.feishu",
            "lobster0.channels.telegram",
            "lobster0.channels.discord",
        ):
            with self.subTest(module=module):
                self.assertIn(module, self.final_stage)

    def test_runtime_entrypoint_is_stable_exec_form_argv(self) -> None:
        """ENTRYPOINT 是 exec form，且 entrypoint.sh 不重解释 argv。"""
        entrypoints = _directives(self.dockerfile, "ENTRYPOINT")
        self.assertEqual(entrypoints, ('ENTRYPOINT ["/usr/local/bin/lobster0-entrypoint"]',))
        commands = _directives(self.dockerfile, "CMD")
        for line in commands:
            with self.subTest(line=line):
                self.assertRegex(line, r'^CMD \["')
        script = _ENTRYPOINT.read_text(encoding="utf-8")
        self.assertTrue(script.startswith("#!/bin/sh\n"))
        self.assertIn("set -eu", script)
        self.assertIn('exec "$LOBSTER0_BIN" "$@"', script)
        for forbidden in ("eval ", "$*", "sh -c", "curl", "wget", "sudo"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)


class SandboxImageContractTest(unittest.TestCase):
    """验证 Phase 6 command 镜像满足 DockerSandbox 的 containment 期望。"""

    def setUp(self) -> None:
        """读取 sandbox Dockerfile 并构造一份 canonical Docker plan。"""
        self.dockerfile = _SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
        self.stages = _stages(self.dockerfile)
        self.assertTrue(self.stages)
        self.final_stage = self.stages[-1][1]
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        workspace = Path(self.temporary_directory.name).resolve()
        self.image = "ghcr.io/nedonion/lobster0-sandbox@sha256:" + "b" * 64
        self.argv = DockerSandbox(image=self.image).build_argv(
            ExecutionPlan(
                argv=("python", "job.py"),
                cwd=workspace,
                environment_names=("LANG",),
                read_roots=(),
                write_roots=(workspace,),
                timeout_seconds=30,
                memory_mib=256,
                cpu_seconds=15,
                pids_limit=128,
                network_mode="none",
                backend="docker",
            )
        )

    def flag_value(self, flag: str) -> str:
        """返回 hardened Docker argv 中某个 flag 的紧邻取值。"""
        index = self.argv.index(flag)
        return self.argv[index + 1]

    def test_sandbox_image_is_digest_pinned_and_ends_non_root(self) -> None:
        """Sandbox 镜像同样 digest-pinned 并以 UID 65532 结束。"""
        from_lines = _directives(self.dockerfile, "FROM")
        self.assertTrue(from_lines)
        for line in from_lines:
            with self.subTest(line=line):
                self.assertRegex(line, _DIGEST_PINNED_FROM)
        self.assertNotIn("curl |", self.dockerfile)
        self.assertNotIn("latest", self.dockerfile)
        user_lines = _directives(self.final_stage, "USER")
        self.assertEqual(user_lines[-1], f"USER {self.flag_value('--user')}")

    def test_sandbox_image_matches_phase6_hardened_docker_flags(self) -> None:
        """镜像必须与 build_argv 固定的 user/workdir/tmpfs/network 一致。"""
        self.assertEqual(self.flag_value("--user"), "65532:65532")
        self.assertIn("--read-only", self.argv)
        workdir = self.flag_value("--workdir")
        self.assertEqual(_directives(self.dockerfile, "WORKDIR")[-1], f"WORKDIR {workdir}")
        tmpfs_target = self.flag_value("--tmpfs").split(":")[0]
        self.assertIn(f"chmod 1777 {tmpfs_target}", self.dockerfile)
        self.assertEqual(self.flag_value("--network"), "none")
        self.assertEqual(_directives(self.dockerfile, "EXPOSE"), ())
        self.assertIn(f"HOME={tmpfs_target}", _env_assignments(self.dockerfile))

    def test_sandbox_image_never_reinterprets_model_supplied_argv(self) -> None:
        """ENTRYPOINT 必须清空，CMD 只能是 exec form。"""
        self.assertEqual(_directives(self.dockerfile, "ENTRYPOINT"), ("ENTRYPOINT []",))
        for line in _directives(self.dockerfile, "CMD"):
            with self.subTest(line=line):
                self.assertRegex(line, r'^CMD \["')
        self.assertNotIn("SHELL ", self.dockerfile)
        self.assertEqual(self.argv[-len(("python", "job.py")) - 1], self.image)

    def test_sandbox_final_layer_has_no_package_manager_or_cache(self) -> None:
        """final layer 不安装任何包，并显式清除 resolver 与缓存。"""
        self.assertIsNone(
            _RESOLVER_INSTALL.search(self.final_stage),
            "sandbox final stage invokes a package resolver",
        )
        for removed in _REMOVED_RESOLVER_PATHS:
            with self.subTest(removed=removed):
                self.assertIn(removed, self.final_stage)
        self.assertIn("/var/cache", self.final_stage)
        self.assertIn("command -v", self.final_stage)

    def test_sandbox_image_declares_no_secret_or_docker_socket(self) -> None:
        """Sandbox 镜像不得携带 Secret 或任何 Docker socket 通路。"""
        for keyword in ("ARG", "ENV"):
            for line in _directives(self.dockerfile, keyword):
                with self.subTest(line=line):
                    self.assertIsNone(_SECRET_NAME.search(line))
        for name in _PROVIDER_KEY_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(name, self.dockerfile)
        self.assertNotIn("docker.sock", self.dockerfile)
        self.assertEqual(_directives(self.dockerfile, "VOLUME"), ())

    def test_published_sandbox_reference_is_accepted_by_docker_sandbox(self) -> None:
        """GHCR sandbox 引用必须是 DockerSandbox 接受的 digest-pinned 形式。"""
        DockerSandbox(image=self.image)
        with self.assertRaises(ValueError):
            DockerSandbox(image="ghcr.io/nedonion/lobster0-sandbox:0.7.0")


if __name__ == "__main__":
    unittest.main()
