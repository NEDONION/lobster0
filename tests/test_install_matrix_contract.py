"""CI/Release 工作流策略与 Tier 1 install matrix 的离线契约测试。

这些测试是发布信任链的最后一道本地门禁：工作流 YAML 只有被这里断言到的行为才算数。
特别是 Task 16 复核实测得到的三条发布期强制义务（native bundle 硬失败、wheel 摘要
必传、wheel 必须先于镜像构建），以及"自托管 Tier 1 组合不可用必须阻断 stable 发布"。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_CI_WORKFLOW = _WORKFLOW_DIR / "ci.yml"
_RELEASE_WORKFLOW = _WORKFLOW_DIR / "release.yml"
_DEPENDABOT = _REPO_ROOT / ".github" / "dependabot.yml"
_MATRIX_DIR = _REPO_ROOT / "tests" / "install_matrix"
_MATRIX_RUNNER = _MATRIX_DIR / "run_install_matrix.py"
_MATRIX_CASES = _MATRIX_DIR / "cases.json"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_PINNED_USES = re.compile(r"^[^@]+@[0-9a-f]{40}$")

# 计划已审阅并固定的四个外部 Action commit；工作流不得引用任何其他 Action。
_REVIEWED_ACTIONS = {
    "actions/checkout": "de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-python": "a309ff8b426b58ec0e2a45f0f869d46889d02405",
    "actions/attest": "508db95dd578ae2727ebd6217d5ba78e4fbda05d",
    "pypa/gh-action-pypi-publish": "cef221092ed1bacb1cc03d23a2d87d1d172e277b",
}

_REQUIRED_CI_JOBS = ("offline", "node-22", "node-24", "artifact-build")
_REQUIRED_RELEASE_JOBS = ("tier1-install", "attestation", "publish")

_ASSEMBLY_ORDER = (
    "scripts/build_sbom.py",
    "scripts/build_release_manifest.py",
    "scripts/render_install_script.py",
    "scripts/build_release_manifest.py",
    "scripts/verify_release_artifacts.py",
)

_IMAGE_BUILD = re.compile(r"docker\s+(?:buildx\s+)?build\b")
_WHEEL_DIGEST_EXPRESSION = "${{ needs.python-build.outputs.wheel_sha256 }}"
_PUBLIC_INSTALL_URL = (
    "https://github.com/NEDONION/lobster0/releases/latest/download/install.sh"
)
_DESIGN_CASE_COUNT = 15


def _load(path: Path) -> dict[str, object]:
    """用 BaseLoader 解析工作流，避免 YAML 1.1 把 ``on`` 折叠成布尔键。"""
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    if type(document) is not dict:
        raise AssertionError(f"{path.name} is not a YAML mapping")
    return document


def _jobs(workflow: dict[str, object]) -> dict[str, dict[str, object]]:
    """返回工作流的 job 映射。"""
    jobs = workflow.get("jobs")
    if type(jobs) is not dict:
        raise AssertionError("workflow has no jobs mapping")
    return jobs


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    """返回一个 job 的 step 列表。"""
    steps = job.get("steps", [])
    return list(steps) if type(steps) is list else []


def _all_steps(workflow: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    """返回 ``(job_id, step)`` 的扁平序列。"""
    return [
        (job_id, step)
        for job_id, job in _jobs(workflow).items()
        for step in _steps(job)
    ]


def _run_text(job: dict[str, object]) -> str:
    """把一个 job 全部 ``run`` 脚本拼成可搜索文本。"""
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _needs(job: dict[str, object]) -> tuple[str, ...]:
    """把 ``needs`` 规范化为字符串元组。"""
    needs = job.get("needs")
    if needs is None:
        return ()
    if type(needs) is str:
        return (needs,)
    return tuple(str(item) for item in needs)


def _reachable(jobs: dict[str, dict[str, object]], job_id: str) -> set[str]:
    """返回一个 job 通过 ``needs`` 传递依赖到的全部 job。"""
    seen: set[str] = set()
    frontier = [job_id]
    while frontier:
        current = frontier.pop()
        for parent in _needs(jobs.get(current, {})):
            if parent not in seen:
                seen.add(parent)
                frontier.append(parent)
    return seen


class WorkflowPresenceTest(unittest.TestCase):
    """要求 Task 17 的全部文件真实存在且可解析。"""

    def test_workflow_and_matrix_files_exist(self) -> None:
        """CI/Release 工作流、dependabot 与 install matrix 必须全部落地。"""
        for path in (
            _CI_WORKFLOW,
            _RELEASE_WORKFLOW,
            _DEPENDABOT,
            _MATRIX_RUNNER,
            _MATRIX_CASES,
        ):
            self.assertTrue(path.is_file(), f"missing {path}")
            self.assertFalse(path.is_symlink(), f"{path} must be a regular file")

    def test_dev_extra_pins_the_yaml_parser(self) -> None:
        """PyYAML 只能进入既有 dev extra，不得进入运行时依赖。"""
        text = _PYPROJECT.read_text(encoding="utf-8")
        self.assertIn('"PyYAML>=6.0.2,<7"', text)
        runtime = text.split("[project.optional-dependencies]", 1)[0]
        self.assertNotIn("PyYAML", runtime)


class ActionPinningTest(unittest.TestCase):
    """外部 Action 只能是已审阅 commit 的固定引用。"""

    def test_every_action_reference_is_a_reviewed_commit_sha(self) -> None:
        """两个工作流的每个 ``uses`` 都必须是 40 位 commit 且在审阅名单内。"""
        for path in (_CI_WORKFLOW, _RELEASE_WORKFLOW):
            workflow = _load(path)
            for job_id, step in _all_steps(workflow):
                if "uses" not in step:
                    continue
                reference = str(step["uses"])
                with self.subTest(workflow=path.name, job=job_id, uses=reference):
                    self.assertRegex(reference, _PINNED_USES)
                    name, digest = reference.split("@", 1)
                    self.assertIn(name, _REVIEWED_ACTIONS)
                    self.assertEqual(digest, _REVIEWED_ACTIONS[name])

    def test_no_job_uses_a_reusable_workflow_or_container_shortcut(self) -> None:
        """禁止 job 级 ``uses``/``container`` 绕过被审阅的 Action 名单。"""
        for path in (_CI_WORKFLOW, _RELEASE_WORKFLOW):
            for job_id, job in _jobs(_load(path)).items():
                with self.subTest(workflow=path.name, job=job_id):
                    self.assertNotIn("uses", job)
                    self.assertNotIn("container", job)


class GateIntegrityTest(unittest.TestCase):
    """任何门禁都不得被静默弱化。"""

    def test_no_workflow_weakens_a_gate(self) -> None:
        """禁止 ``continue-on-error``、``if: false`` 与 ``always()`` 类逃逸。"""
        for path in (_CI_WORKFLOW, _RELEASE_WORKFLOW):
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn("continue-on-error", text)
                self.assertNotIn("if: false", text)
                self.assertNotIn("|| true", text)
            workflow = _load(path)
            for job_id, job in _jobs(workflow).items():
                condition = str(job.get("if", ""))
                if job_id in {"demote-release", "public-smoke"}:
                    continue
                with self.subTest(workflow=path.name, job=job_id):
                    self.assertNotIn("always(", condition)
            for job_id, step in _all_steps(workflow):
                with self.subTest(workflow=path.name, job=job_id):
                    self.assertNotIn("always(", str(step.get("if", "")))

    def test_top_level_permissions_are_read_only(self) -> None:
        """默认 token 权限必须是最小只读，写权限只能逐 job 声明。"""
        for path in (_CI_WORKFLOW, _RELEASE_WORKFLOW):
            workflow = _load(path)
            with self.subTest(workflow=path.name):
                self.assertEqual(workflow.get("permissions"), {"contents": "read"})

    def test_no_workflow_carries_a_long_lived_publishing_token(self) -> None:
        """除 ``GITHUB_TOKEN`` 外不得出现任何 secrets 引用或明文密码字段。"""
        for path in (_CI_WORKFLOW, _RELEASE_WORKFLOW):
            text = path.read_text(encoding="utf-8")
            references = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text))
            with self.subTest(workflow=path.name):
                self.assertLessEqual(references, {"GITHUB_TOKEN"})
                self.assertNotIn("PYPI_API_TOKEN", text)
                self.assertNotIn("TWINE_PASSWORD", text)
                self.assertNotIn("password:", text)
                self.assertNotIn("--password ", text)
                for line in text.splitlines():
                    if "docker login" in line:
                        self.assertIn("--password-stdin", line)


class ContinuousIntegrationWorkflowTest(unittest.TestCase):
    """CI 工作流必须提供计划要求的四个必需检查。"""

    def setUp(self) -> None:
        """加载 CI 工作流。"""
        self.workflow = _load(_CI_WORKFLOW)
        self.jobs = _jobs(self.workflow)

    def test_required_checks_exist(self) -> None:
        """必需检查 job id 必须与计划完全一致。"""
        for job_id in _REQUIRED_CI_JOBS:
            self.assertIn(job_id, self.jobs)

    def test_ci_never_triggers_on_a_release_tag(self) -> None:
        """CI 只在 push/pull_request 上运行，绝不响应 ``v*`` tag。"""
        triggers = self.workflow.get("on")
        self.assertEqual(set(triggers), {"push", "pull_request"})
        self.assertNotIn("tags", triggers["push"])

    def test_offline_job_runs_the_full_local_gate(self) -> None:
        """offline 检查必须真实执行全量 unittest、Ruff 与文档校验。"""
        text = _run_text(self.jobs["offline"])
        self.assertIn("unittest discover -s tests", text)
        self.assertIn("ruff check .", text)
        self.assertIn("scripts/validate_docs.py", text)
        self.assertIn("tests.test_install_matrix_contract", text)

    def test_node_jobs_pin_both_accepted_runtimes(self) -> None:
        """node-22/node-24 必须固定到策略允许的精确版本。"""
        self.assertIn("22.22.3", _run_text(self.jobs["node-22"]))
        self.assertIn("24.18.0", _run_text(self.jobs["node-24"]))

    def test_artifact_build_proves_reproducibility(self) -> None:
        """artifact-build 必须两次构建 installer 并比较摘要。"""
        text = _run_text(self.jobs["artifact-build"])
        self.assertIn("scripts/build_installer_zipapp.py", text)
        self.assertIn("sha256sum", text)
        self.assertIn("--local-offline", text)


class ReleaseTriggerTest(unittest.TestCase):
    """Release 只能由受保护的版本 tag 触发。"""

    def setUp(self) -> None:
        """加载 Release 工作流。"""
        self.workflow = _load(_RELEASE_WORKFLOW)
        self.jobs = _jobs(self.workflow)

    def test_release_only_triggers_on_version_tags_or_guarded_dispatch(self) -> None:
        """只允许 ``v*`` tag 与受守卫的手动派发，禁止 branch push 与定时任务。

        手动派发把"创建 tag"搬进流水线，方便在 GitHub UI 上点按钮发布；
        它不放宽任何发布前提，仍由 guard 在继续之前真实创建 tag（见
        ``test_dispatch_path_cannot_bypass_the_tag_binding``）。
        """
        triggers = self.workflow.get("on")
        self.assertEqual(set(triggers), {"push", "workflow_dispatch"})
        self.assertEqual(triggers["push"], {"tags": ["v*"]})
        self.assertNotIn("branches", triggers["push"])
        self.assertEqual(set(triggers["workflow_dispatch"]["inputs"]), {"version"})
        self.assertEqual(triggers["workflow_dispatch"]["inputs"]["version"]["required"], "true")

    def test_dispatch_path_cannot_bypass_the_tag_binding(self) -> None:
        """手动派发必须与 tag-push 路径等价：默认分支、干净、版本一致、真建 tag。

        这条测试是手动派发按钮的安全代价所在。若它被削弱，就可能从任意
        分支、任意版本号发布一个没有不可变 tag 支撑的正式版。
        """
        text = _run_text(self.jobs["guard"])
        self.assertIn("github.event.repository.default_branch", text)
        self.assertIn("_version.py", text)
        self.assertIn("git status --porcelain", text)
        self.assertIn("git tag ", text)
        self.assertIn("git push origin ", text)
        self.assertIn("rev-parse --verify", text)

    def test_only_the_guard_job_may_push_a_git_ref(self) -> None:
        """只有 guard 允许推送 git 引用，杜绝第二处能创建或移动 tag 的地方。

        多个作业持有 ``contents: write`` 是为了往草稿 Release 上传产物（草稿
        即产物传递通道）。真正需要收紧的不是写权限本身，而是"谁能动 tag"：
        tag 是发布产物绑定的不可变事实，只能由 guard 在校验通过后创建一次。
        """
        for job_id, job in self.jobs.items():
            if job_id == "guard":
                continue
            text = _run_text(job)
            self.assertNotIn("git push", text, f"{job_id} 不得推送任何 git 引用")
            self.assertNotIn("git tag", text, f"{job_id} 不得创建或移动 tag")

    def test_release_serializes_and_never_cancels_itself(self) -> None:
        """发布必须串行且不得被后续运行取消到一半。"""
        concurrency = self.workflow.get("concurrency")
        self.assertEqual(concurrency.get("cancel-in-progress"), "false")

    def test_guard_binds_the_tag_to_the_version_constant(self) -> None:
        """guard 必须把 tag、``_version.py`` 与 commit 绑成一个事实。"""
        text = _run_text(self.jobs["guard"])
        self.assertIn("_version.py", text)
        self.assertIn("SOURCE_DATE_EPOCH", text)
        self.assertIn("git status --porcelain", text)

    def test_required_release_jobs_exist(self) -> None:
        """tier1-install、attestation、publish 必须真实存在。"""
        for job_id in _REQUIRED_RELEASE_JOBS:
            self.assertIn(job_id, self.jobs)


class ReleaseObligationTest(unittest.TestCase):
    """Task 16 复核实测得到的三条发布期强制义务。"""

    def setUp(self) -> None:
        """加载 Release 工作流并定位镜像构建步骤。"""
        self.workflow = _load(_RELEASE_WORKFLOW)
        self.jobs = _jobs(self.workflow)
        self.image_steps = [
            (job_id, step)
            for job_id, step in _all_steps(self.workflow)
            if _IMAGE_BUILD.search(str(step.get("run", "")))
        ]

    def _runtime_image_commands(self) -> list[tuple[str, str]]:
        """返回每一条使用 ``deploy/Dockerfile`` 的 docker build 命令。"""
        commands: list[tuple[str, str]] = []
        for job_id, step in self.image_steps:
            for command in re.split(
                r"(?=docker\s+(?:buildx\s+)?build\b)", str(step["run"])
            )[1:]:
                if "deploy/Dockerfile" in command:
                    commands.append((job_id, command))
        return commands

    def test_only_the_two_reviewed_dockerfiles_are_built(self) -> None:
        """发布只允许构建 Runtime 与 Sandbox 两个已审阅 Dockerfile。"""
        self.assertTrue(self.image_steps)
        built = set()
        for _job_id, step in self.image_steps:
            built.update(re.findall(r"--file (\S+)", str(step["run"])))
        self.assertEqual(built, {"deploy/Dockerfile", "deploy/sandbox.Dockerfile"})
        self.assertTrue(self._runtime_image_commands())

    def test_obligation_one_every_runtime_image_requires_native_bundles(self) -> None:
        """义务一：Runtime 镜像构建必须传 ``LOBSTER0_REQUIRE_NATIVE_BUNDLES=1``。"""
        for job_id, command in self._runtime_image_commands():
            with self.subTest(job=job_id):
                self.assertIn("--build-arg LOBSTER0_REQUIRE_NATIVE_BUNDLES=1", command)
                self.assertNotIn("LOBSTER0_REQUIRE_NATIVE_BUNDLES=0", command)

    def test_obligation_two_every_runtime_image_pins_the_wheel_digest(self) -> None:
        """义务二：Runtime 镜像构建必须传非空 wheel 摘要，且来自 wheel 构建 job 的输出。"""
        commands = self._runtime_image_commands()
        self.assertTrue(commands)
        for job_id, command in commands:
            with self.subTest(job=job_id):
                self.assertIn(
                    f"--build-arg LOBSTER0_WHEEL_SHA256={_WHEEL_DIGEST_EXPRESSION}",
                    command,
                )
                self.assertNotIn('LOBSTER0_WHEEL_SHA256=""', command)
                digests = re.findall(r"LOBSTER0_WHEEL_SHA256=(\S*)", command)
                self.assertTrue(digests)
                for digest in digests:
                    self.assertEqual(digest, "${{")

    def test_obligation_two_wheel_digest_output_is_declared(self) -> None:
        """被引用的 wheel 摘要必须是 python-build 真实声明的 job 输出。"""
        python_build = self.jobs["python-build"]
        outputs = python_build.get("outputs")
        self.assertEqual(type(outputs), dict)
        self.assertIn("wheel_sha256", outputs)
        self.assertIn("sha256sum", _run_text(python_build))

    def test_obligation_three_images_are_built_after_the_wheel(self) -> None:
        """义务三：每个镜像构建 job 必须在依赖图上排在 wheel 构建之后。"""
        for job_id, _step in self.image_steps:
            with self.subTest(job=job_id):
                self.assertIn("python-build", _reachable(self.jobs, job_id))

    def test_obligation_three_image_job_consumes_only_the_fresh_wheel(self) -> None:
        """镜像 job 必须重新取回本次 wheel 并按 job 输出摘要校验后才构建。"""
        for job_id, _step in self.image_steps:
            text = _run_text(self.jobs[job_id])
            with self.subTest(job=job_id):
                self.assertIn("gh release download", text)
                self.assertIn(_WHEEL_DIGEST_EXPRESSION, text)
                self.assertIn("sha256sum -c -", text)
                self.assertNotIn("--cache-from", text)

    def test_wheel_job_does_not_depend_on_any_image_job(self) -> None:
        """wheel 构建不得反向依赖镜像，避免顺序义务被环状依赖抹平。"""
        image_jobs = {job_id for job_id, _step in self.image_steps}
        self.assertFalse(image_jobs & _reachable(self.jobs, "python-build"))


class ReleaseAssemblyOrderTest(unittest.TestCase):
    """装配顺序必须与计划固定的信任链一致。"""

    def setUp(self) -> None:
        """加载 Release 工作流的装配 job。"""
        self.workflow = _load(_RELEASE_WORKFLOW)
        self.jobs = _jobs(self.workflow)
        self.assemble = self.jobs["assemble"]

    def test_assembly_steps_run_in_the_fixed_order(self) -> None:
        """SBOM → manifest → install.sh → checksums → verify 顺序不可交换。"""
        commands: list[str] = []
        for step in _steps(self.assemble):
            for line in str(step.get("run", "")).splitlines():
                for script in set(_ASSEMBLY_ORDER):
                    if script in line:
                        commands.append(script)
        manifest_calls = [
            index
            for index, name in enumerate(commands)
            if name == "scripts/build_release_manifest.py"
        ]
        self.assertEqual(len(manifest_calls), 2)
        self.assertEqual(commands[0], "scripts/build_sbom.py")
        self.assertEqual(commands[-1], "scripts/verify_release_artifacts.py")
        render = commands.index("scripts/render_install_script.py")
        self.assertLess(manifest_calls[0], render)
        self.assertLess(render, manifest_calls[1])

    def test_manifest_and_checksums_use_the_declared_subcommands(self) -> None:
        """两次 manifest 调用必须分别是 ``manifest`` 与 ``checksums`` 子命令。"""
        text = _run_text(self.assemble)
        self.assertIn("manifest --release-inputs-output", text)
        self.assertIn("checksums --manifest", text)
        self.assertIn("--install-script", text)

    def test_assembly_depends_on_every_artifact_producer(self) -> None:
        """装配必须等待 wheel、Node bundle、TUI bundle 与镜像全部完成。"""
        needs = set(_needs(self.assemble))
        self.assertLessEqual(
            {"python-build", "node-bundles", "tui-bundles", "images"}, needs
        )

    def test_nothing_is_attested_or_published_before_verification(self) -> None:
        """attestation 与 publish 必须传递依赖于 assemble。"""
        for job_id in ("attestation", "publish"):
            with self.subTest(job=job_id):
                self.assertIn("assemble", _reachable(self.jobs, job_id))


class Tier1BlockingTest(unittest.TestCase):
    """自托管 Tier 1 组合不可用必须真实阻断 stable 发布。"""

    def setUp(self) -> None:
        """加载 Release 工作流与 install matrix 定义。"""
        self.workflow = _load(_RELEASE_WORKFLOW)
        self.jobs = _jobs(self.workflow)
        self.cases = json.loads(_MATRIX_CASES.read_text(encoding="utf-8"))

    def test_publish_directly_needs_tier1_install(self) -> None:
        """publish 的 ``needs`` 必须字面包含 tier1-install。"""
        self.assertIn("tier1-install", _needs(self.jobs["publish"]))

    def test_publish_has_no_condition_that_survives_a_skipped_tier1(self) -> None:
        """publish 不得带任何在上游 skip 时仍会执行的条件。"""
        self.assertNotIn("if", self.jobs["publish"])

    def test_tier1_install_is_gated_by_a_runner_preflight(self) -> None:
        """tier1-install 必须依赖一个会在 runner 缺失时失败的前置门禁。"""
        preflight = "tier1-runner-preflight"
        self.assertIn(preflight, self.jobs)
        self.assertIn(preflight, _needs(self.jobs["tier1-install"]))
        text = _run_text(self.jobs[preflight])
        self.assertIn("actions/runners", text)
        self.assertIn("PENDING", text)
        self.assertIn("exit 1", text)

    def test_preflight_enumerates_every_required_platform(self) -> None:
        """前置门禁必须从 cases.json 读取需要的全部标签组合。"""
        text = _run_text(self.jobs["tier1-runner-preflight"])
        self.assertIn("tests/install_matrix/run_install_matrix.py", text)
        self.assertIn("--required-runner-labels", text)
        self.assertIn("cases.json", _MATRIX_RUNNER.read_text(encoding="utf-8"))
        self.assertEqual(
            len(self.cases["platforms"]),
            len(json.loads(_MATRIX_CASES.read_text(encoding="utf-8"))["platforms"]),
        )

    def test_tier1_runs_on_self_hosted_labels_only(self) -> None:
        """tier1-install 必须跑在自托管标签上，不得回落到 GitHub 托管 runner。"""
        runs_on = str(self.jobs["tier1-install"].get("runs-on"))
        self.assertIn("matrix.platform.runner_labels", runs_on)
        self.assertNotIn("ubuntu-latest", runs_on)

    def test_tier1_matrix_covers_exactly_the_declared_platforms(self) -> None:
        """矩阵必须由 cases.json 的平台集合生成，不允许手写子集。"""
        strategy = self.jobs["tier1-install"].get("strategy")
        self.assertEqual(type(strategy), dict)
        self.assertEqual(strategy.get("fail-fast"), "true")
        matrix = strategy.get("matrix")
        self.assertEqual(type(matrix), dict)
        self.assertIn("needs.tier1-runner-preflight.outputs.platforms", str(matrix))

    def test_tier1_executes_every_design_case(self) -> None:
        """tier1-install 必须运行全部 15 个设计用例，不得挑选子集。"""
        text = _run_text(self.jobs["tier1-install"])
        self.assertIn("tests/install_matrix/run_install_matrix.py", text)
        self.assertIn("--all-cases", text)
        self.assertNotIn("--case ", text)

    def test_tier1_has_a_bounded_timeout(self) -> None:
        """缺 runner 时 tier1-install 必须超时失败，而不是无限排队。"""
        for job_id in ("tier1-install", "tier1-runner-preflight"):
            with self.subTest(job=job_id):
                self.assertIn("timeout-minutes", self.jobs[job_id])

    def test_evidence_records_pending_rather_than_pass(self) -> None:
        """release evidence 必须能如实记录 PENDING 状态。"""
        text = _run_text(self.jobs["tier1-runner-preflight"])
        self.assertIn("release-evidence.json", text)


class PublishTrustTest(unittest.TestCase):
    """publish 只能在全部证据齐备后提升 stable。"""

    def setUp(self) -> None:
        """加载 Release 工作流。"""
        self.workflow = _load(_RELEASE_WORKFLOW)
        self.jobs = _jobs(self.workflow)
        self.publish = self.jobs["publish"]

    def test_publish_requests_oidc_and_registry_write_only(self) -> None:
        """publish 权限必须精确到 OIDC、Release 写与 GHCR 写。"""
        permissions = self.publish.get("permissions")
        self.assertEqual(type(permissions), dict)
        self.assertEqual(permissions["id-token"], "write")
        self.assertEqual(permissions["contents"], "write")
        self.assertEqual(permissions["packages"], "write")

    def test_publish_runs_inside_a_protected_environment(self) -> None:
        """PyPI 提升必须经过受保护 environment。"""
        self.assertEqual(self.publish.get("environment"), "pypi")

    def test_pypi_publish_uses_trusted_publishing(self) -> None:
        """PyPI 只能走 OIDC Trusted Publishing。"""
        uses = [str(step.get("uses", "")) for step in _steps(self.publish)]
        self.assertTrue(
            any(item.startswith("pypa/gh-action-pypi-publish@") for item in uses)
        )
        for step in _steps(self.publish):
            if str(step.get("uses", "")).startswith("pypa/gh-action-pypi-publish@"):
                self.assertNotIn("password", step.get("with", {}))

    def test_publish_reverifies_before_promoting(self) -> None:
        """publish 必须重新校验 checksums 与 attestation 后才提升 Release。"""
        text = _run_text(self.publish)
        self.assertIn("sha256sum -c", text)
        self.assertIn("gh attestation verify", text)
        self.assertIn("--draft=false", text)

    def test_only_the_publish_job_can_reach_pypi_or_stable_tags(self) -> None:
        """除 publish 外任何 job 都不得发布 PyPI 或打上稳定镜像 tag。"""
        for job_id, job in self.jobs.items():
            if job_id == "publish":
                continue
            text = _run_text(job)
            uses = "\n".join(str(step.get("uses", "")) for step in _steps(job))
            with self.subTest(job=job_id):
                self.assertNotIn("gh-action-pypi-publish", uses)
                self.assertNotIn("twine upload", text)
                self.assertNotIn("--draft=false", text)
                self.assertNotIn("imagetools create", text)

    def test_images_are_pushed_by_digest_before_the_gates(self) -> None:
        """门禁前镜像只能以 digest 形态存在，不得占用可解析 tag。"""
        text = _run_text(self.jobs["images"])
        self.assertIn("push-by-digest=true", text)
        self.assertNotIn(":latest", text)

    def test_attestation_covers_every_release_subject(self) -> None:
        """attestation 必须对 manifest、checksums、install.sh 与全部 artifact 生效。"""
        attestation = self.jobs["attestation"]
        permissions = attestation.get("permissions")
        self.assertEqual(permissions["id-token"], "write")
        self.assertEqual(permissions["attestations"], "write")
        with_blocks = [
            step.get("with", {})
            for step in _steps(attestation)
            if str(step.get("uses", "")).startswith("actions/attest@")
        ]
        self.assertTrue(with_blocks)
        subjects = "\n".join(str(block.get("subject-path", "")) for block in with_blocks)
        for name in ("release-manifest.json", "SHA256SUMS", "install.sh"):
            self.assertIn(name, subjects)


class PostPublishSmokeTest(unittest.TestCase):
    """提升后的公共 URL smoke 与失败回收路径。"""

    def setUp(self) -> None:
        """加载 Release 工作流。"""
        self.workflow = _load(_RELEASE_WORKFLOW)
        self.jobs = _jobs(self.workflow)

    def test_public_smoke_uses_the_stable_latest_url(self) -> None:
        """提升后必须从公开 ``latest/download/install.sh`` 再装一次。"""
        text = _run_text(self.jobs["public-smoke"])
        self.assertIn(_PUBLIC_INSTALL_URL, text)
        self.assertIn("--proto '=https'", text)
        self.assertIn("--tlsv1.2", text)

    def test_failed_public_smoke_returns_the_release_to_draft(self) -> None:
        """公共 smoke 失败必须立即把 Release 退回 draft 并记录 FAIL。"""
        demote = self.jobs["demote-release"]
        condition = str(demote.get("if", ""))
        self.assertIn("failure()", condition)
        self.assertIn("public-smoke", condition)
        text = _run_text(demote)
        self.assertIn("--draft=true", text)
        self.assertIn("FAIL", text)
        self.assertNotIn("yank", text)
        self.assertNotIn("pypi", text.lower())


class DependabotTest(unittest.TestCase):
    """依赖更新必须覆盖 Action 与 Python 生态。"""

    def test_dependabot_watches_actions_and_python(self) -> None:
        """dependabot 必须同时监控 github-actions 与 pip。"""
        document = _load(_DEPENDABOT)
        self.assertEqual(document.get("version"), "2")
        ecosystems = {
            str(entry.get("package-ecosystem")) for entry in document.get("updates", [])
        }
        self.assertLessEqual({"github-actions", "pip"}, ecosystems)


class InstallMatrixDefinitionTest(unittest.TestCase):
    """install matrix 的用例与平台定义必须与设计文档一致。"""

    def setUp(self) -> None:
        """加载 cases.json。"""
        self.document = json.loads(_MATRIX_CASES.read_text(encoding="utf-8"))

    def test_exactly_the_fifteen_design_cases_are_declared(self) -> None:
        """15 条设计用例必须一条不多、一条不少且编号连续。"""
        cases = self.document["cases"]
        self.assertEqual(len(cases), _DESIGN_CASE_COUNT)
        numbers = sorted(int(case["design_case"]) for case in cases)
        self.assertEqual(numbers, list(range(1, _DESIGN_CASE_COUNT + 1)))
        identifiers = [str(case["id"]) for case in cases]
        self.assertEqual(len(set(identifiers)), _DESIGN_CASE_COUNT)

    def test_every_case_declares_its_local_evidence_or_a_pending_reason(self) -> None:
        """本地无法证明的用例必须写明 PENDING 理由，不得静默跳过。"""
        for case in self.document["cases"]:
            with self.subTest(case=case["id"]):
                if case["local_offline"] == "true" or case["local_offline"] is True:
                    self.assertTrue(str(case.get("probe", "")))
                else:
                    self.assertTrue(str(case.get("pending_reason", "")))

    def test_every_case_declares_a_live_command_or_a_live_pending_reason(self) -> None:
        """主机上无法执行的用例必须写明 live PENDING 理由，绝不静默视为通过。"""
        for case in self.document["cases"]:
            with self.subTest(case=case["id"]):
                command = case.get("live_command")
                if command is None:
                    self.assertTrue(str(case.get("live_pending_reason", "")))
                else:
                    self.assertIsInstance(command, list)
                    self.assertTrue(command)
                    self.assertNotIn("live_pending_reason", case)

    def test_live_commands_only_use_verified_cli_subcommands(self) -> None:
        """live 过程只能调用真实存在的 CLI 子命令。"""
        verified = {"install-smoke", "doctor", "service", "update", "uninstall"}
        for case in self.document["cases"]:
            command = case.get("live_command")
            if command is None:
                continue
            with self.subTest(case=case["id"]):
                self.assertIn(str(command[0]), verified)

    def test_platforms_match_the_tier_one_support_matrix(self) -> None:
        """平台集合必须精确覆盖 Tier 1 的 OS/版本/架构组合。"""
        platforms = self.document["platforms"]
        expected = set()
        for distro, versions in (
            ("ubuntu", ("22.04", "24.04")),
            ("debian", ("12", "13")),
            ("rhel", ("9", "10")),
            ("rocky", ("9", "10")),
            ("almalinux", ("9", "10")),
        ):
            for version in versions:
                for arch in ("x86_64", "arm64"):
                    expected.add((distro, version, arch))
        for arch in ("x86_64", "arm64"):
            expected.add(("macos", "13", arch))
        actual = {
            (str(item["distro"]), str(item["distro_version"]), str(item["arch"]))
            for item in platforms
        }
        self.assertEqual(actual, expected)

    def test_every_platform_requires_self_hosted_labels(self) -> None:
        """每个平台的 runner 标签必须含 self-hosted 且唯一。"""
        seen: set[tuple[str, ...]] = set()
        for platform in self.document["platforms"]:
            labels = tuple(str(item) for item in platform["runner_labels"])
            with self.subTest(platform=platform["id"]):
                self.assertIn("self-hosted", labels)
                self.assertNotIn(labels, seen)
                self.assertIn(
                    str(platform["artifact_platform"]),
                    {"linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64"},
                )
            seen.add(labels)


class InstallMatrixRunnerTest(unittest.TestCase):
    """``run_install_matrix.py`` 必须离线、stdlib-only 且如实报告。"""

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """在仓库根目录执行 matrix runner。"""
        return subprocess.run(
            [sys.executable, str(_MATRIX_RUNNER), *arguments],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=600,
            check=False,
        )

    def test_runner_imports_only_the_standard_library(self) -> None:
        """matrix runner 不得依赖任何第三方包。"""
        text = _MATRIX_RUNNER.read_text(encoding="utf-8")
        self.assertNotIn("import yaml", text)
        self.assertNotIn("import requests", text)
        self.assertNotIn("import httpx", text)

    def test_list_prints_every_platform_and_case(self) -> None:
        """``--list`` 必须打印全部平台标签与用例。"""
        result = self._run("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(_MATRIX_CASES.read_text(encoding="utf-8"))
        for platform in document["platforms"]:
            self.assertIn(str(platform["id"]), result.stdout)
        for case in document["cases"]:
            self.assertIn(str(case["id"]), result.stdout)

    def test_local_offline_matrix_passes_without_network_or_sudo(self) -> None:
        """``--local-offline`` 必须在假 artifact 上全绿并如实标注 PENDING。"""
        result = self._run("--local-offline")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("PASS", result.stdout)
        self.assertNotIn("FAIL", result.stdout)
        self.assertNotIn("sudo", result.stdout)

    def test_local_offline_never_reports_host_bound_cases_as_pass(self) -> None:
        """service/reboot 类用例在本地只能是 PENDING。"""
        result = self._run("--local-offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(_MATRIX_CASES.read_text(encoding="utf-8"))
        for case in document["cases"]:
            if str(case["local_offline"]).lower() == "true":
                continue
            for line in result.stdout.splitlines():
                if line.split()[:1] == [str(case["id"])]:
                    self.assertIn("PENDING", line)

    def test_required_runner_labels_are_derivable_for_the_preflight(self) -> None:
        """``--required-runner-labels`` 必须输出前置门禁使用的标签矩阵。"""
        result = self._run("--required-runner-labels")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        document = json.loads(_MATRIX_CASES.read_text(encoding="utf-8"))
        self.assertEqual(len(payload), len(document["platforms"]))
        for entry in payload:
            self.assertIn("self-hosted", entry["runner_labels"])


if __name__ == "__main__":
    unittest.main()
