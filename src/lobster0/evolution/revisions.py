"""不可变 artifact 与 active pointer 的原子切换、回滚与崩溃恢复。

采用文档第 11 节的"不可变 artifact + SQLite active pointer"模型，而不是覆盖 active 文件：
候选正文在 propose 阶段就已经按内容哈希落盘（见 ``proposals.validate_prompt_candidate``），
apply 只做三件事——重新校验 Approval 与 artifact 哈希、CAS 切换指针、写审计。因此不存在
"文件写了一半"的中间态：任何时刻 artifact 要么完整存在要么不存在，pointer 要么指向旧版本
要么指向新版本。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from lobster0.evolution.models import (
    ActiveRevision,
    EvolutionAction,
    ProposalTargetType,
    ProposalVersion,
)
from lobster0.evolution.proposals import PROMPT_BLOCKS
from lobster0.evolution.repository import (
    ActiveRevisionRepository,
    EvolutionError,
    ProposalRepository,
)

_BINDING_VERSION = 1


@dataclass(frozen=True, slots=True)
class ArtifactCheck:
    """描述一次 active artifact 完整性校验的封闭结果。"""

    valid: bool
    reason: str | None


def approval_binding_hash(
    *,
    action: EvolutionAction,
    proposal_id: int,
    proposal_version_ordinal: int,
    target_type: ProposalTargetType,
    target_name: str,
    base_hash: str,
    candidate_hash: str,
    eval_receipt_hash: str | None,
) -> str:
    """按文档第 10 节绑定对象计算审批哈希。

    任何一项（含 eval receipt）变化都会产生不同绑定，使旧 Approval 无法用于新候选。
    """
    payload = {
        "binding_version": _BINDING_VERSION,
        "action": action.value,
        "proposal_id": proposal_id,
        "proposal_version": proposal_version_ordinal,
        "target": f"{target_type.value}:{target_name}",
        "base_hash": base_hash,
        "candidate_hash": candidate_hash,
        "eval_receipt_hash": eval_receipt_hash,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def prompt_artifact_path(prompt_versions_root: Path, candidate_ref: str) -> Path:
    """把 ProposalVersion 的受控引用解析为 owner-only artifact 路径。

    Raises:
        EvolutionError: 引用为绝对路径或包含 ``..``，可能逃出 version store。
    """
    if candidate_ref.startswith("/") or ".." in Path(candidate_ref).parts:
        raise EvolutionError("artifact_ref_unsafe", "candidate reference escapes the store")
    return prompt_versions_root / candidate_ref


def verify_prompt_artifact(
    prompt_versions_root: Path, version: ProposalVersion
) -> ArtifactCheck:
    """校验 active artifact 仍然存在且内容哈希与 candidate_hash 一致。

    Returns:
        ``valid=False`` 时附带稳定 reason；调用方必须 fail closed，绝不加载损坏内容。
    """
    try:
        path = prompt_artifact_path(prompt_versions_root, version.candidate_ref)
    except EvolutionError as error:
        return ArtifactCheck(False, error.code)
    if path.is_symlink() or not path.is_file():
        return ArtifactCheck(False, "artifact_missing")
    try:
        payload = path.read_bytes()
    except OSError:
        return ArtifactCheck(False, "artifact_unreadable")
    if hashlib.sha256(payload).hexdigest() != version.candidate_hash:
        return ArtifactCheck(False, "artifact_hash_mismatch")
    return ArtifactCheck(True, None)


def skill_artifact_path(skill_versions_root: Path, candidate_ref: str) -> Path:
    """把 Skill ProposalVersion 的受控引用解析为 owner-only artifact 路径。"""
    return prompt_artifact_path(skill_versions_root, candidate_ref)


def verify_skill_artifact(
    skill_versions_root: Path, version: ProposalVersion
) -> ArtifactCheck:
    """校验 Skill artifact 仍存在且内容哈希与 candidate_hash 一致。

    Skill 的 ``candidate_hash`` 是 ``SkillLoader`` 对整个 ``SKILL.md`` 字节算出的摘要，
    与 Prompt 的算法一致，因此可以直接复用同一套校验。
    """
    return verify_prompt_artifact(skill_versions_root, version)


def active_skill_document(
    proposals: ProposalRepository,
    active: ActiveRevisionRepository,
    skill_versions_root: Path,
    *,
    owner_id: int,
    skill_name: str,
) -> str | None:
    """返回一个 Skill 当前生效版本的完整 ``SKILL.md`` 正文；没有或不可信时返回 ``None``。

    与 Prompt 一样 fail safe：指针缺失、version 读不到、artifact 损坏或哈希不匹配都返回
    ``None``，由调用方回退到磁盘上的常规 Skill，绝不加载可疑内容。
    """
    pointer = active.get(owner_id, ProposalTargetType.SKILL, skill_name)
    if pointer is None:
        return None
    try:
        version = proposals.get_version(owner_id, pointer.proposal_version_id)
    except EvolutionError:
        return None
    if not verify_skill_artifact(skill_versions_root, version).valid:
        return None
    try:
        return skill_artifact_path(skill_versions_root, version.candidate_ref).read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError):
        return None


def active_prompt_text(
    proposals: ProposalRepository,
    active: ActiveRevisionRepository,
    prompt_versions_root: Path,
    *,
    owner_id: int,
    block_id: str,
) -> str:
    """返回一个 Prompt block 当前生效的正文；异常时回退到内置 base。

    Runtime 每个 Turn 开始时读取一次；同一个 Turn 内不热切换。任何一步不可信——指针指向
    的 version 读不到、artifact 缺失或哈希不匹配——都回退到 Core 内置 base text，绝不加载
    可疑内容，也绝不因为 Evolution 状态异常而让整个 Turn 失败。

    Raises:
        EvolutionError: ``block_id`` 不在 Core 允许的固定 registry 中。
    """
    try:
        base_text = PROMPT_BLOCKS[block_id]
    except KeyError:
        raise EvolutionError(
            "unknown_prompt_block", f"unknown prompt block: {block_id}"
        ) from None
    pointer = active.get(owner_id, ProposalTargetType.PROMPT, block_id)
    if pointer is None:
        return base_text
    try:
        version = proposals.get_version(owner_id, pointer.proposal_version_id)
    except EvolutionError:
        return base_text
    check = verify_prompt_artifact(prompt_versions_root, version)
    if not check.valid:
        return base_text
    path = prompt_artifact_path(prompt_versions_root, version.candidate_ref)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return base_text


def recover_active_prompt_revision(
    proposals: ProposalRepository,
    active: ActiveRevisionRepository,
    prompt_versions_root: Path,
    *,
    owner_id: int,
    block_id: str,
) -> ArtifactCheck:
    """启动时校验 active artifact；损坏时 CAS 回退到 previous revision。

    覆盖文档第 11 节"DB commit 后、artifact 损坏"这一崩溃窗口：pointer 已经指向候选，但
    候选文件被删除或改写。此时既不能加载损坏内容，也不能保留一个指向坏内容的指针。

    Returns:
        校验结果；``valid=False`` 表示已经尝试过回退（没有 previous 时保持原状并报告）。
    """
    pointer = active.get(owner_id, ProposalTargetType.PROMPT, block_id)
    if pointer is None:
        return ArtifactCheck(True, None)
    try:
        version = proposals.get_version(owner_id, pointer.proposal_version_id)
    except EvolutionError:
        return ArtifactCheck(False, "active_version_missing")
    check = verify_prompt_artifact(prompt_versions_root, version)
    if check.valid:
        return check
    if pointer.previous_version_id is None:
        return check
    try:
        active.rollback(
            owner_id,
            ProposalTargetType.PROMPT,
            block_id,
            expected_current_version_id=pointer.proposal_version_id,
        )
    except EvolutionError:
        return check
    return check


def stale_orphan_artifacts(
    prompt_versions_root: Path, *, referenced_hashes: frozenset[str]
) -> tuple[Path, ...]:
    """列出 version store 中没有任何 ProposalVersion 引用的孤儿 artifact。

    覆盖文档第 11 节"stage 完成、DB commit 前崩溃"这一窗口：artifact 已经落盘但没有任何
    行引用它。这里只报告不删除——删除属于 retention 策略，需要 Owner 显式发起。
    """
    if not prompt_versions_root.is_dir():
        return ()
    orphans = [
        path
        for path in sorted(prompt_versions_root.rglob("*.md"))
        if path.is_file() and not path.is_symlink() and path.stem not in referenced_hashes
    ]
    return tuple(orphans)


def _canonical_json(value: object) -> str:
    """返回键排序、无多余空白的 canonical JSON，保证同内容同哈希。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ApplyReceipt:
    """描述一次成功的 active pointer 切换。"""

    proposal_id: int
    proposal_version_id: int
    target_type: ProposalTargetType
    target_name: str
    previous_version_id: int | None
    revision: ActiveRevision
