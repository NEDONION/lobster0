"""验证 installer manifest 与不可变请求模型的 fail-closed 契约。"""

import dataclasses
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from miniclaw.install import models as install_models
from miniclaw.install.models import (
    InstallError,
    InstallEvent,
    InstallPlan,
    InstallRequest,
    NodePolicy,
    NodeRange,
    PlatformKey,
    ReleaseManifest,
)


class InstallModelsTest(unittest.TestCase):
    """覆盖 manifest 信任边界以及计划输出的脱敏边界。"""

    fixture = Path("tests/install/manifest_v1.json")
    schema = Path("release/manifest.schema.json")

    def setUp(self) -> None:
        """为每个用例载入一份可独立修改的 fixture。"""
        self.document = json.loads(self.fixture.read_text(encoding="utf-8"))

    def manifest_bytes(self, mutation: dict[str, object] | None = None) -> bytes:
        """返回应用顶层修改后的紧凑 manifest 字节。"""
        document = dict(self.document)
        if mutation:
            document.update(mutation)
        return json.dumps(document, separators=(",", ":")).encode()

    def artifact_mutation(self, **changes: object) -> bytes:
        """返回只修改首个 artifact 的 manifest。"""
        artifact = dict(self.document["artifacts"][0])
        artifact.update(changes)
        self.document["artifacts"][0] = artifact
        return self.manifest_bytes()

    def release_bytes(self, version: str, artifacts: list[dict[str, object]]) -> bytes:
        """返回指定版本和 artifact 的完整 manifest JSON。"""
        document = dict(self.document)
        document["version"] = version
        document["artifacts"] = artifacts
        return json.dumps(document, separators=(",", ":")).encode()

    def release_url(self, version: str, filename: str) -> str:
        """返回 fixture 使用的 immutable MiniClaw Release asset URL。"""
        return f"https://github.com/NEDONION/miniclaw/releases/download/v{version}/{filename}"

    def request(self, **changes: object) -> InstallRequest:
        """构造一个不含 Secret 值的有效安装请求。"""
        values: dict[str, object] = {
            "action": "install",
            "version": "0.7.0",
            "channel": "stable",
            "prefix": Path("/opt/miniclaw"),
            "state_home": Path("/var/lib/miniclaw"),
            "system_prefix": False,
            "onboard": True,
            "config_file": Path("/private/import-config.toml"),
            "secrets_file": Path("/private/import-secrets.env"),
            "service": True,
            "allow_system_packages": False,
            "dry_run": False,
            "json_output": False,
            "verbose": False,
            "purge_data": False,
            "confirm_data_loss": False,
        }
        values.update(changes)
        return InstallRequest(**values)  # type: ignore[arg-type]

    def plan(self, request: InstallRequest | None = None, **changes: object) -> InstallPlan:
        """构造一个只引用 fixture artifact 的有效安装计划。"""
        selected_request = self.request() if request is None else request
        manifest = ReleaseManifest.from_bytes(self.fixture.read_bytes())
        values: dict[str, object] = {
            "request": selected_request,
            "manifest": manifest,
            "platform": PlatformKey("linux", "x86_64"),
            "distro_id": "ubuntu",
            "distro_version": "24.04",
            "service_manager": "systemd-user",
            "program_prefix": Path("/opt/miniclaw"),
            "state_home": selected_request.state_home,
            "artifact_filenames": ("miniclaw-tui-0.7.0-linux-x86_64.tar.gz",),
            "system_argvs": (("apt-get", "install", "-y", "libsqlite3-0"),),
            "install_service": True,
            "run_onboarding": True,
        }
        values.update(changes)
        return InstallPlan(**values)  # type: ignore[arg-type]

    def test_manifest_accepts_exact_v1_and_selects_one_platform_artifact(self) -> None:
        """有效 v1 fixture 应解析成不可变值并精确选择目标 TUI。"""
        manifest = ReleaseManifest.from_bytes(self.fixture.read_bytes())

        self.assertEqual(manifest.version, "0.7.0")
        self.assertEqual(manifest.database_schema, 5)
        selected = manifest.require_artifact("tui", PlatformKey("linux", "x86_64"))
        self.assertEqual(selected.component_version, "0.7.0")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.version = "0.8.0"  # type: ignore[misc]

    def test_manifest_rejects_unknown_missing_and_duplicate_json_keys(self) -> None:
        """所有对象必须 exact-key，JSON 文本中的重复键也不得被覆盖。"""
        for data in (
            self.manifest_bytes({"mystery": True}),
            self.manifest_bytes({"product": "other"}),
            self.manifest_bytes({"version": "v0.7"}),
        ):
            with self.subTest(data=data[:100]), self.assertRaisesRegex(
                InstallError, "manifest_invalid"
            ):
                ReleaseManifest.from_bytes(data)

        del self.document["features"]
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(self.manifest_bytes())

        duplicate = self.fixture.read_text(encoding="utf-8").replace(
            '"product": "miniclaw",',
            '"product": "miniclaw", "product": "miniclaw",',
            1,
        )
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(duplicate.encode())

    def test_manifest_normalizes_malicious_json_types_and_depth(self) -> None:
        """错误容器类型和极深 JSON 都必须归一化为稳定错误。"""
        documents = []
        for field in ("kind", "source_repository"):
            artifact = dict(self.document["artifacts"][0])
            artifact[field] = {"unhashable": True}
            documents.append({**self.document, "artifacts": [artifact]})
        artifact = dict(self.document["artifacts"][0])
        artifact["platform"] = {"os": {"unhashable": True}, "arch": "x86_64"}
        documents.extend(
            (
                {**self.document, "artifacts": [artifact]},
                {**self.document, "features": [{"unhashable": True}]},
                {**self.document, "product": ["miniclaw"]},
            )
        )
        for document in documents:
            with self.subTest(document=document), self.assertRaisesRegex(
                InstallError, "manifest_invalid"
            ):
                ReleaseManifest.from_bytes(json.dumps(document).encode())

        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(("[" * 2_000 + "]" * 2_000).encode())

    def test_manifest_rejects_bad_commit_hash_size_and_integer_bool(self) -> None:
        """commit/hash/size/schema 整数必须规范且 bool 不能冒充 int。"""
        cases = (
            self.manifest_bytes({"git_commit": "a" * 39}),
            self.artifact_mutation(sha256="A" * 64),
            self.artifact_mutation(sha256=""),
            self.artifact_mutation(size=0),
            self.artifact_mutation(size=1_073_741_825),
            self.manifest_bytes({"database_schema": True}),
            self.manifest_bytes({"schema_version": True}),
        )
        for data in cases:
            with self.subTest(data=data[:100]), self.assertRaisesRegex(
                InstallError, "manifest_invalid"
            ):
                ReleaseManifest.from_bytes(data)

    def test_manifest_rejects_untrusted_urls_and_repositories(self) -> None:
        """artifact URL 必须是无附加信息的 allowlisted HTTPS 来源。"""
        cases = (
            "http://github.com/NEDONION/miniclaw/releases/download/v0.7.0/a.whl",
            "https://user@github.com/NEDONION/miniclaw/releases/download/v0.7.0/a.whl",
            "https://github.com/NEDONION/miniclaw/releases/download/v0.7.0/a.whl?x=1",
            "https://github.com/NEDONION/miniclaw/releases/download/v0.7.0/a.whl#x",
            "https://github.com/OTHER/miniclaw/releases/download/v0.7.0/a.whl",
            "https://github.com/NEDONION/other/releases/download/v0.7.0/a.whl",
        )
        for url in cases:
            with self.subTest(url=url), self.assertRaisesRegex(
                InstallError, "manifest_invalid"
            ):
                ReleaseManifest.from_bytes(self.artifact_mutation(url=url))

        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(
                self.artifact_mutation(source_repository="https://github.com/OTHER/miniclaw")
            )

    def test_url_mutation_corpus_is_rejected_by_python_and_json_schema(self) -> None:
        """同一恶意 path corpus 必须同时被 parser 和 schema URL pattern 拒绝。"""
        valid = self.document["artifacts"][0]["url"]
        filename = self.document["artifacts"][0]["filename"]
        invalid_urls = (
            valid.replace("/v0.7.0/", "/../"),
            valid.replace("/v0.7.0/", "/%2e%2e/"),
            valid.replace(filename, "%2e%2e"),
            valid.replace(filename, f"bad name-{filename}"),
            valid.replace("https://github.com", "HTTPS://GITHUB.com"),
        )
        schema = json.loads(self.schema.read_text(encoding="utf-8"))
        pattern = schema["$defs"]["artifact"]["properties"]["url"]["pattern"]

        self.assertIsNotNone(re.fullmatch(pattern, valid))
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaisesRegex(InstallError, "manifest_invalid"):
                    ReleaseManifest.from_bytes(self.artifact_mutation(url=url))
                self.assertIsNone(re.fullmatch(pattern, url))

    def test_artifact_kind_matrix_and_url_filename_binding_are_closed(self) -> None:
        """kind 必须绑定 Release host、source、platform、media、upstream 与 URL basename。"""
        wheel = dict(self.document["artifacts"][0])
        node = dict(self.document["artifacts"][1])
        tui = dict(self.document["artifacts"][2])
        mutations = (
            {**wheel, "source_repository": "https://github.com/nodejs/node"},
            {**wheel, "platform": {"os": "linux", "arch": "x86_64"}},
            {**wheel, "media_type": "application/gzip"},
            {**wheel, "upstream_sha256": "f" * 64},
            {
                **wheel,
                "url": wheel["url"].replace(wheel["filename"], "different.whl"),
            },
            {
                **node,
                "filename": wheel["filename"],
                "url": node["url"].replace(node["filename"], wheel["filename"]),
            },
            {**node, "source_repository": "https://github.com/NEDONION/miniclaw"},
            {**node, "platform": {"os": "any", "arch": "any"}},
            {**node, "media_type": "application/zip"},
            {**node, "upstream_sha256": None},
            {**tui, "source_repository": "https://github.com/earendil-works/pi-tui"},
            {**tui, "upstream_sha256": "f" * 64},
        )
        for artifact in mutations:
            with self.subTest(artifact=artifact), self.assertRaisesRegex(
                InstallError, "manifest_invalid"
            ):
                ReleaseManifest.from_bytes(
                    self.manifest_bytes({"artifacts": [artifact]})
                )

    def test_all_nine_stable_artifact_kinds_remain_valid(self) -> None:
        """Task 15 九种 stable artifact kind 都应满足 closed-world matrix。"""
        wheel, node, tui = (dict(item) for item in self.document["artifacts"])

        def universal(
            kind: str,
            filename: str,
            media_type: str,
        ) -> dict[str, object]:
            """从有效 wheel 派生一个 MiniClaw universal artifact。"""
            return {
                **wheel,
                "kind": kind,
                "filename": filename,
                "url": self.release_url("0.7.0", filename),
                "media_type": media_type,
            }

        artifacts = [
            wheel,
            universal("sdist", "miniclaw_agent-0.7.0.tar.gz", "application/gzip"),
            universal("requirements", "requirements-all.lock", "text/plain"),
            node,
            tui,
            universal("sandbox-image", "miniclaw-sandbox-image-digest.txt", "text/plain"),
            universal("runtime-image", "miniclaw-runtime-image-digest.txt", "text/plain"),
            universal("installer", "miniclaw-installer.pyz", "application/zip"),
            universal("sbom", "miniclaw-0.7.0.cdx.json", "application/vnd.cyclonedx+json"),
        ]

        manifest = ReleaseManifest.from_bytes(self.release_bytes("0.7.0", artifacts))

        self.assertEqual({artifact.kind for artifact in manifest.artifacts}, {
            "wheel", "sdist", "requirements", "node", "tui", "sandbox-image",
            "runtime-image", "installer", "sbom",
        })

    def test_prerelease_python_filenames_match_pep440_in_parser_and_schema(self) -> None:
        """rc SemVer 的 wheel/sdist 文件名必须使用受限 PEP 440 normalization。"""
        version = "0.8.0-rc.1"
        wheel = dict(self.document["artifacts"][0])
        schema = json.loads(self.schema.read_text(encoding="utf-8"))
        rules = schema["$defs"]["artifact"]["allOf"]

        def schema_patterns(kind: str) -> tuple[str, str]:
            """读取指定 kind 的 schema filename 与 URL patterns。"""
            for rule in rules:
                condition = rule["if"]["properties"]["kind"]
                if condition.get("const") == kind:
                    properties = rule["then"]["properties"]
                    return properties["filename"]["pattern"], properties["url"]["pattern"]
            self.fail(f"missing schema rule for {kind}")

        correct = (
            ("wheel", "miniclaw_agent-0.8.0rc1-py3-none-any.whl", "application/zip"),
            ("sdist", "miniclaw_agent-0.8.0rc1.tar.gz", "application/gzip"),
        )
        wrong = (
            ("wheel", "miniclaw_agent-0.8.0-rc.1-py3-none-any.whl", "application/zip"),
            ("sdist", "miniclaw_agent-0.8.0-rc.1.tar.gz", "application/gzip"),
        )
        for kind, filename, media_type in correct:
            artifact = {
                **wheel,
                "kind": kind,
                "filename": filename,
                "url": self.release_url(version, filename),
                "component_version": version,
                "media_type": media_type,
            }
            with self.subTest(kind=kind, valid=True):
                filename_pattern, url_pattern = schema_patterns(kind)
                self.assertIsNotNone(re.fullmatch(filename_pattern, filename))
                self.assertIsNotNone(re.search(url_pattern, artifact["url"]))
                ReleaseManifest.from_bytes(self.release_bytes(version, [artifact]))
        for kind, filename, media_type in wrong:
            artifact = {
                **wheel,
                "kind": kind,
                "filename": filename,
                "url": self.release_url(version, filename),
                "component_version": version,
                "media_type": media_type,
            }
            with self.subTest(kind=kind, valid=False):
                filename_pattern, url_pattern = schema_patterns(kind)
                self.assertIsNone(re.fullmatch(filename_pattern, filename))
                self.assertIsNone(re.search(url_pattern, artifact["url"]))
                with self.assertRaisesRegex(InstallError, "manifest_invalid"):
                    ReleaseManifest.from_bytes(self.release_bytes(version, [artifact]))

    def test_manifest_rejects_unknown_features_and_unsupported_platforms(self) -> None:
        """feature 与 Release platform 必须来自封闭集合且覆盖完整 Tier 1。"""
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(self.manifest_bytes({"features": ["browser"]}))

        platforms = list(self.document["supported_platforms"])
        platforms[0] = {"os": "windows", "arch": "x86_64"}
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(self.manifest_bytes({"supported_platforms": platforms}))

        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(
                self.artifact_mutation(platform={"os": "linux", "arch": "riscv64"})
            )

    def test_manifest_rejects_invalid_node_policy_and_database_order(self) -> None:
        """Node 只接受既定 22/24 范围，DB readable schema 不得超前。"""
        node_cases = (
            {
                "default": "25.0.0",
                "accepted": self.document["node"]["accepted"],
            },
            {
                "default": "24.18.0",
                "accepted": [
                    {"minimum": "20.0.0", "maximum_exclusive": "21.0.0"},
                    {"minimum": "24.15.0", "maximum_exclusive": "25.0.0"},
                ],
            },
            {
                "default": "24.18.0",
                "accepted": [
                    {"minimum": "23.0.0", "maximum_exclusive": "22.0.0"},
                    {"minimum": "24.15.0", "maximum_exclusive": "25.0.0"},
                ],
            },
        )
        for node in node_cases:
            with self.subTest(node=node), self.assertRaisesRegex(
                InstallError, "manifest_invalid"
            ):
                ReleaseManifest.from_bytes(self.manifest_bytes({"node": node}))

        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(
                self.manifest_bytes({"minimum_readable_schema": 6})
            )

    def test_manifest_enforces_byte_and_artifact_budgets(self) -> None:
        """manifest 超过 1 MiB 或 artifact 超过 128 条必须在构造前拒绝。"""
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(b" " * 1_048_577)

        artifacts = [dict(self.document["artifacts"][0]) for _ in range(129)]
        for index, artifact in enumerate(artifacts):
            artifact["filename"] = f"artifact-{index}.whl"
            artifact["kind"] = "wheel" if index % 2 == 0 else "sdist"
            artifact["component_version"] = f"0.7.{index}"
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(self.manifest_bytes({"artifacts": artifacts}))

    def test_manifest_rejects_duplicate_artifacts_and_ambiguous_selection(self) -> None:
        """filename、artifact identity 和兼容平台选择都必须唯一。"""
        first = dict(self.document["artifacts"][0])
        second = dict(first)
        second["sha256"] = "d" * 64
        for field, value in (
            ("filename", "other.whl"),
            ("component_version", "0.7.1"),
        ):
            duplicate = dict(second)
            duplicate[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                InstallError, "manifest_invalid"
            ):
                ReleaseManifest.from_bytes(
                    self.manifest_bytes({"artifacts": [first, duplicate]})
                )

        universal_tui = dict(self.document["artifacts"][2])
        universal_tui["filename"] = "miniclaw-tui-0.7.0-any-any.tar.gz"
        universal_tui["platform"] = {"os": "any", "arch": "any"}
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            ReleaseManifest.from_bytes(
                self.manifest_bytes(
                    {"artifacts": [*self.document["artifacts"], universal_tui]}
                )
            )
        manifest = ReleaseManifest.from_bytes(self.fixture.read_bytes())
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            manifest.require_artifact("installer", PlatformKey("linux", "x86_64"))

    def test_request_models_reject_invalid_types_paths_and_versions(self) -> None:
        """请求的 enum、semver、绝对路径和 bool 字段必须严格。"""
        self.assertEqual(self.request().version, "0.7.0")
        for changes in (
            {"action": "repair"},
            {"action": []},
            {"channel": "nightly"},
            {"version": "v0.7.0"},
            {"version": "0.8.0-rc.1", "channel": "stable"},
            {"prefix": Path("relative")},
            {"prefix": Path("/opt/miniclaw\nSECRET_SENTINEL")},
            {"state_home": Path("relative")},
            {"config_file": Path("relative.toml")},
            {"secrets_file": Path("relative.env")},
            {"dry_run": 1},
            {"service": 1},
            {"prefix": Path("/opt/miniclaw"), "system_prefix": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(InstallError):
                self.request(**changes)

    def test_request_bool_fields_use_static_values_and_exact_errors(self) -> None:
        """所有 bool 字段必须绕过同名动态 lookup 并返回精确字段错误。"""
        fields = (
            "system_prefix",
            "onboard",
            "service",
            "allow_system_packages",
            "dry_run",
            "json_output",
            "verbose",
            "purge_data",
            "confirm_data_loss",
        )

        with mock.patch.object(install_models, "getattr", return_value=False, create=True):
            for field in fields:
                with self.subTest(field=field), self.assertRaises(InstallError) as caught:
                    self.request(**{field: 0})
                self.assertEqual(caught.exception.code, "request_invalid")
                self.assertEqual(caught.exception.detail, field)

    def test_plan_summary_excludes_config_secrets_and_state_home(self) -> None:
        """计划摘要仅展示安全字段，不得泄漏输入配置和 Secret 路径。"""
        request = self.request(
            config_file=Path("/private/CONFIG_SENTINEL.toml"),
            secrets_file=Path("/private/SECRET_SENTINEL.env"),
            state_home=Path("/private/STATE_SENTINEL"),
        )
        plan = self.plan(request)

        summary = plan.safe_summary()
        self.assertEqual(
            summary,
            "version=0.7.0 platform=linux/x86_64 service=True onboarding=True",
        )
        self.assertNotIn("/opt/miniclaw", summary)
        self.assertNotIn("CONFIG_SENTINEL", summary)
        self.assertNotIn("SECRET_SENTINEL", summary)
        self.assertNotIn("STATE_SENTINEL", summary)
        with self.assertRaisesRegex(InstallError, "plan_invalid"):
            dataclasses.replace(plan, platform="linux")  # type: ignore[arg-type]
        with self.assertRaisesRegex(InstallError, "plan_invalid"):
            dataclasses.replace(plan, program_prefix=Path("/opt/miniclaw\nSECRET_SENTINEL"))
        with self.assertRaisesRegex(InstallError, "plan_invalid"):
            dataclasses.replace(
                plan,
                artifact_filenames=({"unhashable": True},),  # type: ignore[arg-type]
            )

    def test_plan_rejects_request_escalation_and_control_character_argv(self) -> None:
        """plan 不得反转显式 false，也不得携带任何 argv 控制字符。"""
        plan = self.plan()

        for request in (
            dataclasses.replace(plan.request, service=False),
            dataclasses.replace(plan.request, onboard=False),
        ):
            with self.subTest(request=request), self.assertRaisesRegex(
                InstallError, "plan_invalid"
            ):
                dataclasses.replace(plan, request=request)
        for argument in ("line\nbreak", "tab\targument", "carriage\rreturn"):
            with self.subTest(argument=argument), self.assertRaisesRegex(
                InstallError, "plan_invalid"
            ):
                dataclasses.replace(plan, system_argvs=(("command", argument),))

    def test_event_and_error_details_are_bounded_and_redacted(self) -> None:
        """稳定错误与事件 detail 不得保留凭据、URL、私有路径或无限文本。"""
        unsafe_values = (
            '{"manifest":"raw","git_commit":"' + "a" * 40 + '"}',
            "a" * 40,
            "top_secret",
            "Authorization: Bearer TOP_SECRET",
            "token=TOP_SECRET https://user:pass@example.com/x /Users/alice/private",
            "x" * 900,
            "version=TOPSECRET platform=linux/x86_64 service=True onboarding=True",
            "version=1.2.3-TOPSECRET platform=linux/x86_64 service=True onboarding=True",
            "version=1.2.3-Bearer platform=linux/x86_64 service=True onboarding=True",
            f"version=1.2.3-{'a' * 40} platform=linux/x86_64 service=True onboarding=True",
            f"version=1.2.3-rc.{'1' * 40} platform=linux/x86_64 service=True onboarding=True",
            f"version={'1' * 40}.2.3 platform=linux/x86_64 service=True onboarding=True",
        )
        for unsafe in unsafe_values:
            error = InstallError("manifest_invalid", unsafe)
            event = InstallEvent("install.failed", "error", error.code, unsafe)
            with self.subTest(unsafe=unsafe):
                self.assertEqual(error.detail, "redacted")
                self.assertEqual(event.detail, "redacted")
                self.assertLessEqual(len(str(error)), 500)
                self.assertNotIn(unsafe, str(error))
                self.assertNotIn("TOP_SECRET", str(error))
                self.assertNotIn("TOPSECRET", str(error))
                self.assertNotIn("Bearer", str(error))
                self.assertNotIn('"manifest"', str(error))
                self.assertNotIn("a" * 40, str(error))
        self.assertEqual(InstallError("manifest_invalid", "artifacts.url").detail, "artifacts.url")
        safe_summary = self.plan().safe_summary()
        self.assertEqual(
            InstallEvent("install.preflight", "ok", None, safe_summary).detail,
            "redacted",
        )
        safe_prerelease = (
            "version=1.2.3-rc.1 platform=linux/x86_64 service=False onboarding=False"
        )
        self.assertEqual(
            InstallEvent("install.preflight", "ok", None, safe_prerelease).detail,
            "redacted",
        )
        with self.assertRaisesRegex(InstallError, "event_invalid"):
            InstallEvent("install.failed", [], None, "detail")  # type: ignore[arg-type]
        fallback = InstallError([], "detail")  # type: ignore[arg-type]
        self.assertEqual(fallback.code, "installer_error")

    def test_parser_error_keeps_code_and_field_without_raw_value(self) -> None:
        """parser 错误只输出稳定 code 与字段名，不回显恶意字段值。"""
        raw_url = "https://user:TOPSECRET@github.com/NEDONION/miniclaw/releases/raw"

        with self.assertRaises(InstallError) as caught:
            ReleaseManifest.from_bytes(self.artifact_mutation(url=raw_url))

        error = caught.exception
        self.assertEqual(error.code, "manifest_invalid")
        self.assertEqual(error.detail, "artifacts.url")
        self.assertNotIn(raw_url, str(error))
        self.assertNotIn("TOPSECRET", str(error))

    def test_node_tuple_segments_reject_bool_in_direct_constructors(self) -> None:
        """Node 三段版本每一段必须是 exact int，bool 不得利用 int 相等语义。"""
        manifest = ReleaseManifest.from_bytes(self.fixture.read_bytes())

        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            NodeRange((22, True, 3), (23, 0, 0))
        with self.assertRaisesRegex(InstallError, "manifest_invalid"):
            NodePolicy((24, 18, False), manifest.node.accepted)

    def test_json_schema_mirrors_closed_world_python_contract(self) -> None:
        """schema 应可由 stdlib 读取并镜像主要 closed-world 常量。"""
        schema = json.loads(self.schema.read_text(encoding="utf-8"))

        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(schema["properties"]["database_schema"]["const"], 5)
        self.assertEqual(schema["properties"]["artifacts"]["maxItems"], 128)
        self.assertEqual(schema["properties"]["node"]["properties"]["default"]["const"], "24.18.0")
        self.assertEqual(
            schema["properties"]["supported_platforms"]["minItems"],
            schema["properties"]["supported_platforms"]["maxItems"],
        )
        self.assertTrue(
            all(
                definition.get("additionalProperties") is False
                for definition in schema["$defs"].values()
            )
        )


if __name__ == "__main__":
    unittest.main()
