"""Skill manifest v2 的严格解析、权限声明与 legacy v1 兼容测试。"""

import unittest

from lobster0.skills.loader import SkillError
from lobster0.skills.manifest import KNOWN_TOOL_NAMES, parse_manifest

_LEGACY = """---
name: summarize
description: summarize long documents
version: 1
---

# Instructions

Summarize.
"""

_V2 = """---
name: lark-cli
description: query Feishu documents through the local lark-cli
version: 2
manifest_version: 2
license: MIT
homepage: https://example.com/lark-cli
required_tools: run_command, read_file
required_binaries: lark-cli
required_env: LARK_APP_ID, LARK_APP_SECRET
supported_platforms: darwin, linux
model_invocable: true
user_invocable: true
---

# Instructions

Use lark-cli.
"""


def _with(field: str, value: str) -> str:
    """在 v2 manifest 上替换或追加一个字段。"""
    lines = _V2.splitlines()
    end = lines.index("---", 1)
    body = [line for line in lines[1:end] if not line.startswith(f"{field}:")]
    body.append(f"{field}: {value}")
    return "\n".join(["---", *body, "---", *lines[end + 1 :]]) + "\n"


class ManifestV2Test(unittest.TestCase):
    """验证 v2 只声明名称，不承载任何 Secret 值。"""

    def test_manifest_declares_names_not_secret_values(self) -> None:
        """required_env 只能是变量名；repr 不得带出任何值。"""
        manifest = parse_manifest(_V2, directory_name="lark-cli")

        self.assertEqual(manifest.required_env, ("LARK_APP_ID", "LARK_APP_SECRET"))
        self.assertEqual(manifest.required_binaries, ("lark-cli",))
        self.assertEqual(manifest.required_tools, ("read_file", "run_command"))
        self.assertNotIn("LARK_APP_SECRET", repr(manifest))

    def test_env_values_are_rejected(self) -> None:
        """写成 NAME=value 形状必须拒绝，避免 Secret 落进 Skill 文件。"""
        with self.assertRaisesRegex(SkillError, "environment"):
            parse_manifest(
                _with("required_env", "LARK_APP_ID=secret-value"),
                directory_name="lark-cli",
            )

    def test_unknown_tool_is_rejected(self) -> None:
        """required_tools 只能引用 Core 真实存在的 Tool。"""
        with self.assertRaisesRegex(SkillError, "unknown required tool"):
            parse_manifest(_with("required_tools", "disable_policy"), directory_name="lark-cli")

    def test_allowlist_matches_the_real_tool_modules_exactly(self) -> None:
        """白名单必须与 Tool 模块实际声明的名字逐一对齐。

        手写副本会在新增或改名 Tool 后静默漂移：漏掉新 Tool 会让合法 Skill 装不上，
        留下已删除的名字则会放行一个根本不存在的能力声明。
        """
        import re
        from pathlib import Path as _Path

        declared: set[str] = set()
        for module in (_Path(__file__).resolve().parents[1] / "src/lobster0/tools").glob(
            "*.py"
        ):
            declared |= set(
                re.findall(r'name="([a-z_]+)"', module.read_text(encoding="utf-8"))
            )

        self.assertEqual(declared, set(KNOWN_TOOL_NAMES))
        self.assertNotIn("disable_policy", KNOWN_TOOL_NAMES)

    def test_unknown_security_relevant_field_is_rejected(self) -> None:
        """未知字段可能是新的权限维度，必须 fail closed 而不是忽略。"""
        with self.assertRaisesRegex(SkillError, "unsupported"):
            parse_manifest(_with("allow_network", "true"), directory_name="lark-cli")

    def test_invalid_platform_is_rejected(self) -> None:
        """平台只能取封闭集合。"""
        with self.assertRaisesRegex(SkillError, "platform"):
            parse_manifest(_with("supported_platforms", "solaris"), directory_name="lark-cli")

    def test_invocability_flags_must_be_boolean(self) -> None:
        """model_invocable / user_invocable 必须是明确布尔值。"""
        with self.assertRaisesRegex(SkillError, "boolean"):
            parse_manifest(_with("model_invocable", "maybe"), directory_name="lark-cli")

    def test_name_must_match_its_directory(self) -> None:
        """沿用 v1 的目录绑定，避免同名覆盖。"""
        with self.assertRaisesRegex(SkillError, "directory"):
            parse_manifest(_V2, directory_name="other-name")

    def test_content_hash_is_stable_and_content_sensitive(self) -> None:
        """同内容同哈希；正文变化必须改变哈希。"""
        first = parse_manifest(_V2, directory_name="lark-cli")
        second = parse_manifest(_V2, directory_name="lark-cli")
        changed = parse_manifest(_V2 + "\nextra\n", directory_name="lark-cli")

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.content_hash, changed.content_hash)


class LegacyV1CompatibilityTest(unittest.TestCase):
    """现有最小 Skill 必须继续可用，不能因为 v2 落地而失效。"""

    def test_legacy_skill_parses_as_manifest_version_one(self) -> None:
        """只有 name/description/version 的旧 Skill 仍然合法。"""
        manifest = parse_manifest(_LEGACY, directory_name="summarize")

        self.assertEqual(manifest.manifest_version, 1)
        self.assertEqual(manifest.name, "summarize")
        self.assertEqual(manifest.required_tools, ())
        self.assertEqual(manifest.required_env, ())

    def test_legacy_defaults_are_conservative(self) -> None:
        """未声明能力时默认最小：可被模型调用，但不预设任何权限。"""
        manifest = parse_manifest(_LEGACY, directory_name="summarize")

        self.assertTrue(manifest.model_invocable)
        self.assertEqual(manifest.required_binaries, ())
        self.assertEqual(manifest.supported_platforms, ())


if __name__ == "__main__":
    unittest.main()
