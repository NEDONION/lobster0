"""Markdown SKILL.md 严格解析、惰性选择与安全边界测试。"""

import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from lobster0.bootstrap import initialize_state
from lobster0.paths import build_state_paths
from lobster0.skills.loader import SkillError, SkillLoader


class SkillLoaderTest(unittest.TestCase):
    """验证 Loader 只加载命中正文、最多三个且拒绝路径逃逸。"""

    def setUp(self) -> None:
        """创建初始化状态并移除内置模板，保证每个测试显式控制 Skill。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialize_state(self.paths)
        for name in ("summarize", "feishu-lark-cli"):
            example = self.paths.skills / name / "SKILL.md"
            if example.exists():
                example.unlink()
                example.parent.rmdir()
        self.loader = SkillLoader(self.paths.skills)

    def make_skill(
        self,
        name: str,
        description: str,
        body: str = "Follow this workflow.",
        *,
        version: int = 1,
    ) -> Path:
        """创建符合正式目录格式的测试 Skill 并返回文件路径。"""
        directory = self.paths.skills / name
        directory.mkdir()
        path = directory / "SKILL.md"
        path.write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"version: {version}\n"
            "---\n\n"
            f"# Instructions\n\n{body}\n",
            encoding="utf-8",
        )
        return path

    def test_selects_at_most_three_matching_skills_in_stable_order(self) -> None:
        """同分候选按名称稳定选择前三，并记录版本和 64 位内容哈希。"""
        for name in ("delta", "alpha", "charlie", "bravo"):
            self.make_skill(name, "summarize reports and decisions", body=f"Use {name}.")

        selected = self.loader.select("Please summarize this report")

        self.assertEqual([skill.name for skill in selected], ["alpha", "bravo", "charlie"])
        self.assertTrue(all(skill.version == 1 for skill in selected))
        self.assertTrue(all(len(skill.content_hash) == 64 for skill in selected))
        self.assertIn("Use alpha.", selected[0].content)

    def test_unmatched_body_is_not_read_until_query_activates_it(self) -> None:
        """metadata 扫描不能读取未命中正文；命中坏 UTF-8 时才稳定失败。"""
        self.make_skill("summary", "summarize reports")
        broken = self.paths.skills / "binary/SKILL.md"
        broken.parent.mkdir()
        broken.write_bytes(
            b"---\nname: binary\ndescription: inspect binary artifact\nversion: 1\n---\n\n"
            b"invalid:\xff"
        )

        selected = self.loader.select("summarize a report")

        self.assertEqual([skill.name for skill in selected], ["summary"])
        with self.assertRaises(SkillError) as caught:
            self.loader.select("inspect binary artifact")
        self.assertEqual(caught.exception.code, "invalid_skill_text")

    def test_chinese_query_matches_description_without_loading_unrelated_skill(self) -> None:
        """中文 query 应按有效字词命中说明，不能要求 description 使用英文。"""
        self.make_skill("summarize", "总结长文本并提取决定和行动项", "先结论，后行动项。")
        self.make_skill("weather", "查询天气和温度", "只处理天气。")

        selected = self.loader.select("请帮我总结这份很长的会议文档")

        self.assertEqual([skill.name for skill in selected], ["summarize"])
        self.assertIn("先结论", selected[0].content)

    def test_rejects_symlink_size_name_and_frontmatter_violations(self) -> None:
        """Skill 根逃逸、超限、目录名不符和未知字段都必须 fail closed。"""
        cases: list[tuple[str, Callable[[], None]]] = []

        def symlink_case() -> None:
            outside = self.paths.home / "outside-skill.md"
            outside.write_text(
                "---\nname: escaped\ndescription: escaped task\nversion: 1\n---\n",
                encoding="utf-8",
            )
            directory = self.paths.skills / "escaped"
            directory.mkdir()
            (directory / "SKILL.md").symlink_to(outside)

        def size_case() -> None:
            path = self.make_skill("huge", "huge task")
            path.write_bytes(b"x" * (64 * 1024 + 1))

        def name_case() -> None:
            path = self.make_skill("folder-name", "name mismatch")
            path.write_text(
                "---\nname: other-name\ndescription: name mismatch\nversion: 1\n---\n",
                encoding="utf-8",
            )

        def field_case() -> None:
            path = self.make_skill("unknown", "unknown field")
            path.write_text(
                "---\nname: unknown\ndescription: unknown field\nversion: 1\ncode: run.py\n---\n",
                encoding="utf-8",
            )

        cases.extend(
            (
                ("symlink", symlink_case),
                ("size", size_case),
                ("name", name_case),
                ("field", field_case),
            )
        )
        for label, arrange in cases:
            with self.subTest(label=label):
                for child in tuple(self.paths.skills.iterdir()):
                    if child.name == "versions":
                        continue
                    if child.is_dir() and not child.is_symlink():
                        for file in child.iterdir():
                            file.unlink()
                        child.rmdir()
                    else:
                        child.unlink()
                arrange()
                with self.assertRaises(SkillError):
                    self.loader.catalog()


if __name__ == "__main__":
    unittest.main()
