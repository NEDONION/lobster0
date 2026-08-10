"""扫描 SKILL.md metadata，并只加载当前 Query 命中的正文。"""

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

_MAX_SKILL_BYTES = 64 * 1024
_MAX_FRONTMATTER_BYTES = 8 * 1024
_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_LATIN_WORD = re.compile(r"[a-z0-9][a-z0-9-]+")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_CJK_STOP = frozenset("的了和与是我你请帮把个这那一份很并或及")


class SkillError(RuntimeError):
    """表示 Skill 路径、格式、大小或文本违反稳定契约。"""

    def __init__(self, code: str, message: str) -> None:
        """保存机器错误码和不包含 Skill 正文的安全消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    """保存无需读取正文即可参与匹配的 Skill 信息。"""

    name: str
    description: str
    version: int
    path: Path


@dataclass(frozen=True, slots=True)
class ActivatedSkill:
    """保存当前 Turn 真正加载的 Skill 正文与可回放哈希。"""

    name: str
    description: str
    version: int
    content: str
    content_hash: str


class SkillLoader:
    """从固定根目录选择最多三个与当前 Query 匹配的 Skill。"""

    def __init__(self, root: Path) -> None:
        """绑定不允许通过 symlink 扩大的 Skill 根目录。"""
        self._root = root

    def catalog(self) -> tuple[SkillMetadata, ...]:
        """按名称稳定扫描直接子目录的严格 frontmatter。

        Returns:
            未读取正文的 Skill metadata。

        Raises:
            SkillError: 根、文件路径、大小或 frontmatter 不安全。
        """
        try:
            if self._root.is_symlink() or not self._root.is_dir():
                raise SkillError("unsafe_skill_path", "skills root is not a safe directory")
            resolved_root = self._root.resolve(strict=True)
            entries = sorted(self._root.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise SkillError("unsafe_skill_path", "skills root is not safely readable") from error

        catalog: list[SkillMetadata] = []
        for directory in entries:
            if directory.name == "versions":
                continue
            try:
                if directory.is_symlink() or not directory.is_dir():
                    continue
                if not directory.resolve(strict=True).is_relative_to(resolved_root):
                    raise SkillError("unsafe_skill_path", "skill directory escapes its root")
            except OSError as error:
                raise SkillError("unsafe_skill_path", "skill directory is not safe") from error
            path = directory / "SKILL.md"
            if not path.exists():
                continue
            self._validate_path(path, resolved_root)
            header = _read_header(path)
            values = _parse_frontmatter(header)
            catalog.append(_metadata(values, directory.name, path))
        return tuple(catalog)

    def select(self, query: str) -> tuple[ActivatedSkill, ...]:
        """按确定性关键词分数选择并加载最多三个正文。

        Args:
            query: 当前 Turn 最后一个用户消息。

        Returns:
            分数降序、名称升序的最多三个 ActivatedSkill。

        Raises:
            SkillError: 命中的正文在加载时不安全或不是 UTF-8。
        """
        query_tokens = _tokens(query)
        if not query_tokens:
            return ()
        ranked: list[tuple[int, str, SkillMetadata]] = []
        for metadata in self.catalog():
            score = len(query_tokens & _tokens(f"{metadata.name} {metadata.description}"))
            if score:
                ranked.append((-score, metadata.name, metadata))
        selected = sorted(ranked)[:3]
        return tuple(self._load(metadata) for _, _, metadata in selected)

    def _load(self, metadata: SkillMetadata) -> ActivatedSkill:
        """在命中后读取完整正文并复核 metadata 与内容哈希。"""
        resolved_root = self._root.resolve(strict=True)
        self._validate_path(metadata.path, resolved_root)
        payload = _read_complete(metadata.path)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SkillError("invalid_skill_text", "skill file is not valid UTF-8") from error
        values, body = _parse_document(text)
        current = _metadata(values, metadata.path.parent.name, metadata.path)
        if current != metadata:
            raise SkillError("skill_changed", "skill metadata changed while loading")
        return ActivatedSkill(
            name=metadata.name,
            description=metadata.description,
            version=metadata.version,
            content=body.strip(),
            content_hash=hashlib.sha256(payload).hexdigest(),
        )

    @staticmethod
    def _validate_path(path: Path, root: Path) -> None:
        """拒绝 symlink、根外目标、非普通文件与超过 64 KiB 的 Skill。"""
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise SkillError("unsafe_skill_path", "skill file is not safely readable") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or not resolved.is_relative_to(root)
        ):
            raise SkillError("unsafe_skill_path", "skill file is not a safe regular file")
        if metadata.st_size > _MAX_SKILL_BYTES:
            raise SkillError("skill_too_large", "skill file exceeds 64 KiB")


def _read_header(path: Path) -> bytes:
    """只读取 frontmatter 行，避免未命中 Skill 的正文进入内存。"""
    descriptor = _open_no_follow(path)
    header = bytearray()
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as skill_file:
            for index in range(256):
                line = skill_file.readline(_MAX_FRONTMATTER_BYTES + 1 - len(header))
                if not line or len(header) + len(line) > _MAX_FRONTMATTER_BYTES:
                    break
                header.extend(line)
                if index > 0 and line.rstrip(b"\r\n") == b"---":
                    return bytes(header)
    except OSError as error:
        raise SkillError("skill_read_failed", "skill metadata could not be read") from error
    finally:
        os.close(descriptor)
    raise SkillError("invalid_skill_frontmatter", "skill frontmatter is incomplete")


def _read_complete(path: Path) -> bytes:
    """读取一个已经命中的有限 Skill 正文。"""
    descriptor = _open_no_follow(path)
    try:
        payload = os.read(descriptor, _MAX_SKILL_BYTES + 1)
    except OSError as error:
        raise SkillError("skill_read_failed", "skill file could not be read") from error
    finally:
        os.close(descriptor)
    if len(payload) > _MAX_SKILL_BYTES:
        raise SkillError("skill_too_large", "skill file exceeds 64 KiB")
    return payload


def _open_no_follow(path: Path) -> int:
    """用单个文件描述符打开普通文件并拒绝最终 symlink。"""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise SkillError("unsafe_skill_path", "skill path is not a regular file")
        return descriptor
    except SkillError:
        raise
    except OSError as error:
        raise SkillError("unsafe_skill_path", "skill file is not safely readable") from error


def _parse_frontmatter(header: bytes) -> dict[str, str]:
    """严格解码仅含单行 scalar 的 frontmatter。"""
    try:
        text = header.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillError("invalid_skill_text", "skill metadata is not valid UTF-8") from error
    values, _ = _parse_document(text)
    return values


def _parse_document(text: str) -> tuple[dict[str, str], str]:
    """解析完整或仅 frontmatter 文本，并返回 scalar 字段和正文。"""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise SkillError("invalid_skill_frontmatter", "skill must start with frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise SkillError("invalid_skill_frontmatter", "skill frontmatter is incomplete") from error
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            raise SkillError("invalid_skill_frontmatter", "skill field must be a scalar")
        key, value = (part.strip() for part in line.split(":", 1))
        if not key or not value or key in values:
            raise SkillError("invalid_skill_frontmatter", "skill field is invalid")
        values[key] = value
    if set(values) != {"name", "description", "version"}:
        raise SkillError("invalid_skill_frontmatter", "skill fields are not supported")
    return values, "\n".join(lines[end + 1 :])


def _metadata(values: dict[str, str], directory_name: str, path: Path) -> SkillMetadata:
    """把 frontmatter scalar 转换为强类型且验证目录名。"""
    name = values["name"]
    description = values["description"]
    if not _SKILL_NAME.fullmatch(name) or name != directory_name:
        raise SkillError("invalid_skill_name", "skill name must match its directory")
    if len(description) > 500:
        raise SkillError("invalid_skill_frontmatter", "skill description is too long")
    try:
        version = int(values["version"])
    except ValueError as error:
        raise SkillError("invalid_skill_version", "skill version must be an integer") from error
    if version <= 0 or str(version) != values["version"]:
        raise SkillError("invalid_skill_version", "skill version must be a positive integer")
    return SkillMetadata(name, description, version, path)


def _tokens(value: str) -> frozenset[str]:
    """生成适用于英文词和中文字符的轻量确定性匹配 token。"""
    lowered = value.casefold()
    tokens = set(_LATIN_WORD.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        tokens.update(character for character in run if character not in _CJK_STOP)
    return frozenset(tokens)
