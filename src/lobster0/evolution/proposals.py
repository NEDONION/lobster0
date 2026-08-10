"""Prompt、Skill、Memory 三类受限 Candidate 的确定性生成与校验。

Task 3 只负责候选内容本身的结构校验、hard-deny 拒绝与哈希/落盘；Runtime 何时真正读取
active revision（Prompt 的 bilingual 系统前言如何被覆盖）是 Task 5 的范围，这里刻意不碰
``agent/context.py`` 里正在使用的安全前言文本。
"""

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lobster0.memory.models import DisclosureContext
from lobster0.memory.review import MemoryReviewService
from lobster0.memory.store import MemoryError
from lobster0.skills.loader import ActivatedSkill, SkillError, SkillLoader

_MAX_PROMPT_CHARS = 4_000
_DIFF_MARKERS = re.compile(r"(?m)^(?:--- |\+\+\+ |@@ |diff --git |Index: )")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_TOOL_POLICY_KEYWORDS = re.compile(
    r"(?i)\b(tool[_ -]?polic(?:y|ies)|policy[_ -]?rule|allow[_ -]?list|"
    r"grant\s+approval|bypass\s+approval|disable\s+sandbox)\b"
)

PROMPT_BLOCKS: dict[str, str] = {
    "agent-behavior": (
        "Follow the owner's identity instructions, preserve user privacy, and answer "
        "clearly using available tools when they are needed."
    ),
}


class CandidateError(RuntimeError):
    """表示候选内容、目标数量或格式违反 Phase 7 的固定契约。"""

    def __init__(self, code: str, message: str) -> None:
        """保存机器错误码和不包含候选正文的安全消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CandidateMaterial:
    """保存一个已校验候选的哈希与受控引用，供写入 ProposalVersion。"""

    base_hash: str
    candidate_hash: str
    manifest_json: str
    candidate_ref: str


def validate_prompt_candidate(
    versions_root: Path, block_id: str, candidate_text: str
) -> CandidateMaterial:
    """校验 Prompt candidate 并原子写入 owner-only version store。

    Args:
        versions_root: ``StatePaths.prompt_versions``。
        block_id: Core 允许的固定 block ID；未知 ID 直接拒绝。
        candidate_text: Provider 返回的完整候选 Markdown 正文，不能是 diff/patch。

    Returns:
        绑定 base/candidate hash 与落盘引用的候选材料。

    Raises:
        CandidateError: block 未知、内容为空/超限/含控制字符/像 diff/触碰 Tool 权限语义。
    """
    try:
        base_text = PROMPT_BLOCKS[block_id]
    except KeyError:
        raise CandidateError(
            "unknown_prompt_block", f"unknown prompt block: {block_id}"
        ) from None
    if not isinstance(candidate_text, str) or not candidate_text.strip():
        raise CandidateError("empty_candidate", "prompt candidate must not be empty")
    if len(candidate_text) > _MAX_PROMPT_CHARS:
        raise CandidateError(
            "candidate_too_large", "prompt candidate exceeds the character limit"
        )
    _reject_diff_like(candidate_text)
    _reject_control_characters(candidate_text)
    if _TOOL_POLICY_KEYWORDS.search(candidate_text) is not None:
        raise CandidateError(
            "tool_policy_language_denied",
            "prompt candidate must not attempt to define Tool permissions",
        )
    base_hash = hashlib.sha256(base_text.encode("utf-8")).hexdigest()
    candidate_hash = hashlib.sha256(candidate_text.encode("utf-8")).hexdigest()
    relative = f"{block_id}/{candidate_hash}.md"
    _write_candidate_file(versions_root / relative, candidate_text.encode("utf-8"))
    manifest = _canonical_json(
        {"kind": "prompt", "block_id": block_id, "chars": len(candidate_text)}
    )
    return CandidateMaterial(base_hash, candidate_hash, manifest, relative)


def validate_skill_candidate(
    staging_directory: Path, *, existing: ActivatedSkill | None = None
) -> CandidateMaterial:
    """复用 SkillLoader 校验 staging 目录，只允许恰好一个 Skill。

    Args:
        staging_directory: 只包含这一个候选 Skill 子目录的隔离 staging 根。
        existing: 同名 Skill 当前的 active 内容；新建 Skill 时为 ``None``。

    Returns:
        绑定 base/candidate hash 与 staging 目录名的候选材料。

    Raises:
        CandidateError: staging 目录不安全、为空、包含一个以上 Skill，或加载失败。
    """
    try:
        loader = SkillLoader(staging_directory)
        catalog = loader.catalog()
    except SkillError as error:
        raise CandidateError(error.code, str(error)) from error
    if len(catalog) != 1:
        raise CandidateError(
            "single_skill_required", "a skill proposal must stage exactly one skill"
        )
    metadata = catalog[0]
    try:
        activated = loader.select(f"{metadata.name} {metadata.description}")
    except SkillError as error:
        raise CandidateError(error.code, str(error)) from error
    matching = next((item for item in activated if item.name == metadata.name), None)
    if matching is None:
        raise CandidateError("skill_load_failed", "staged skill could not be loaded")
    if existing is not None and existing.name != matching.name:
        raise CandidateError(
            "skill_name_mismatch", "candidate skill name does not match the target"
        )
    base_hash = (
        hashlib.sha256(b"").hexdigest()
        if existing is None
        else hashlib.sha256(existing.content.encode("utf-8")).hexdigest()
    )
    manifest = _canonical_json(
        {"kind": "skill", "name": matching.name, "version": matching.version}
    )
    return CandidateMaterial(
        base_hash=base_hash,
        candidate_hash=matching.content_hash,
        manifest_json=manifest,
        candidate_ref=f"skill-staging/{metadata.path.parent.name}",
    )


def build_memory_forget_candidate(
    memory_reviews: MemoryReviewService,
    *,
    owner_id: int,
    unit_id: str,
    now: datetime,
) -> CandidateMaterial:
    """通过既有 MemoryReviewService.preview_forget 生成 forget candidate。

    完整候选内容仍然只存在于既有 Memory Review/Unit 表中；这里只保存不含正文的引用，
    不复制 Memory 正文进 Evolution 的 manifest。

    Args:
        memory_reviews: 已绑定 Owner Disclosure 规则的既有 Memory Review 服务。
        owner_id: 发起遗忘的 Owner。
        unit_id: 目标 Memory Unit 的稳定 ID。
        now: 用于绑定 preview hash 的当前时间。

    Returns:
        绑定 base/candidate hash 与 Review 引用的候选材料。

    Raises:
        CandidateError: Unit 不存在或不处于可遗忘状态（由 MemoryError 转译）。
    """
    disclosure = DisclosureContext(
        owner_id=owner_id,
        requester_user_id=owner_id,
        channel="cli",
        conversation_kind="local",
        identity_verified=True,
    )
    try:
        preview = memory_reviews.preview_forget(disclosure, unit_id, now=now)
    except MemoryError as error:
        raise CandidateError(error.code, str(error)) from error
    base_hash = hashlib.sha256(
        f"{preview.unit_id}:{preview.current_status}".encode()
    ).hexdigest()
    manifest = _canonical_json(
        {
            "kind": "memory",
            "review_type": preview.review_type,
            "unit_id": preview.unit_id,
            "review_id": preview.review_id,
        }
    )
    return CandidateMaterial(
        base_hash=base_hash,
        candidate_hash=preview.preview_hash,
        manifest_json=manifest,
        candidate_ref=f"memory-review:{preview.review_id}",
    )


def _reject_diff_like(text: str) -> None:
    """拒绝形似 unified diff/patch 的候选；Proposal 必须是完整正文。"""
    if _DIFF_MARKERS.search(text) is not None:
        raise CandidateError(
            "diff_patch_denied", "candidate must be full content, not a diff or patch"
        )


def _reject_control_characters(text: str) -> None:
    """拒绝除换行/Tab 外会破坏终端或 Markdown 渲染的控制字符。"""
    if _CONTROL_CHARACTERS.search(text) is not None:
        raise CandidateError(
            "control_characters_denied", "candidate must not contain control characters"
        )


def _write_candidate_file(path: Path, content: bytes) -> None:
    """以 owner-only 临时文件、fsync 和 atomic replace 写入候选正文。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_json(value: dict[str, object]) -> str:
    """返回键排序、无多余空白的 canonical JSON，保证同内容同哈希。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
