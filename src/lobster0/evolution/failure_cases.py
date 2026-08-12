"""把一条 ``/bad`` 反馈整理成可复核的 versioned failure case 草稿。

## 为什么产出的是 ``planned`` + ``live`` 草稿而不是可直接入门禁的 active offline case

离线 Runner 使用 ``ScriptedProvider``：模型回复来自 case 文件本身。如果把回复也写进去，
那么"候选是否修好了这个问题"根本没有被检验——答案是预设的。真正验证行为改进必须调用
真实 Provider，因此这类 case 天然属于 ``live`` 层。

同时，"本该怎么做"（应当调用哪个 Tool、正确回答长什么样）无法从一条失败对话里机械推导，
那是 Owner 的判断。因此本模块只做能确定性完成的部分：

* 从反馈绑定的 assistant message 回溯同一 Turn 的真实用户提问；
* 复用既有 redaction 抹掉 Secret、邮箱与本机路径；
* 把失败回答里的显著片段沉淀为 ``answer_excludes``（"不要再这样答"）；
* 记录该 Turn 实际调用过的 Tool 名称，供 Owner 对照"本该调用什么"；
* 用反馈的 ``context_hash`` 绑定来源，使草稿可追溯。

``status`` 固定为 ``planned``：草稿不会进入任何 active 门禁，必须由 Owner 补齐期望、
确认无隐私残留后才提升为 active。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from lobster0.evolution.models import Feedback
from lobster0.evolution.redaction import redact_feedback_context

_MAX_EXCLUDE_CHARS = 60
_MIN_EXCLUDE_CHARS = 8
_SENTENCE_SPLIT = re.compile(r"[。！？\n.!?]+")


class FailureCaseError(RuntimeError):
    """表示无法从这条反馈整理出草稿；不包含对话正文。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码与安全消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FailureCaseDraft:
    """一份等待 Owner 补齐期望的 failure case 草稿。"""

    case_id: str
    document: dict[str, object]

    def to_jsonl(self) -> str:
        """渲染为一行 versioned JSONL，键排序保证同输入同输出。"""
        return json.dumps(self.document, ensure_ascii=False, sort_keys=True) + "\n"


def build_failure_case(
    feedback: Feedback,
    *,
    user_query: str,
    failing_answer: str,
    tool_names: tuple[str, ...],
) -> FailureCaseDraft:
    """把一条 bad 反馈整理成 versioned failure case 草稿。

    Args:
        feedback: 目标反馈；必须是 ``bad`` 且尚未被遗忘。
        user_query: 同一 Turn 里 Owner 的原始提问（未脱敏，本函数负责脱敏）。
        failing_answer: 被差评的 assistant 回答（未脱敏）。
        tool_names: 该 Turn 实际调用过的 Tool 名称，按调用顺序去重。

    Returns:
        可直接写入 JSONL 的草稿；``status`` 固定为 ``planned``。

    Raises:
        FailureCaseError: 反馈不是 bad、已被遗忘，或提问脱敏后为空。
    """
    if feedback.rating.value != "bad":
        raise FailureCaseError(
            "feedback_not_bad", "only a bad rating describes a failure to fix"
        )
    if feedback.status.value == "forgotten":
        raise FailureCaseError(
            "feedback_forgotten", "forgotten feedback must not be revived into a case"
        )
    query = redact_feedback_context(user_query).strip()
    if not query:
        raise FailureCaseError("empty_query", "redacted user query is empty")

    excludes = _distinctive_fragments(redact_feedback_context(failing_answer))
    case_id = f"EVO-FAILURE-{feedback.id:03d}"
    document: dict[str, object] = {
        "schema_version": 1,
        "id": case_id,
        "title": f"反馈 #{feedback.id} 的失败复现（草稿，待 Owner 补齐期望）",
        # planned：草稿绝不进入任何 active 门禁。
        "status": "planned",
        # live：验证行为改进必须调用真实 Provider；脚本化回复无法证明候选有效。
        "layers": ["live"],
        "capability": "controlled_evolution",
        "query": query,
        "expected": {"answer_excludes": excludes},
        "introduced_by": f"owner-bad-feedback-{feedback.id}",
        "tags": ["evolution", "failure", "draft", "owner-review-required"],
    }
    # Owner 的差评理由、实际调用过的 Tool、来源哈希都必须落在 case schema 的字段白名单
    # 之内，否则草稿虽然写得出来，Owner 提升为 active 时才会被 loader 拒绝。这里统一挂进
    # tags 与 title，既可被 loader 接受，也一眼能看到需要补什么。
    tags = list(document["tags"])
    if tool_names:
        tags.extend(f"observed-tool:{name}" for name in tool_names)
    tags.append(f"source-context:{feedback.context_hash[:16]}")
    document["tags"] = tags
    if feedback.redacted_reason:
        document["title"] = (
            f"反馈 #{feedback.id}：{feedback.redacted_reason[:40]}（草稿，待 Owner 补齐期望）"
        )
    return FailureCaseDraft(case_id=case_id, document=document)


def write_failure_case(draft: FailureCaseDraft, path: Path) -> None:
    """以 owner-only 权限写出草稿；已存在同名文件时拒绝覆盖。

    Raises:
        FailureCaseError: 目标已存在或不可写。
    """
    if path.exists() or path.is_symlink():
        raise FailureCaseError(
            "draft_exists", "a draft already exists at the requested path"
        )
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(draft.to_jsonl(), encoding="utf-8")
        path.chmod(0o600)
    except OSError as error:
        raise FailureCaseError("draft_unwritable", "draft could not be written") from error


def _distinctive_fragments(answer: str) -> list[str]:
    """从失败回答里挑出可用作"不要再这样答"的短片段。

    只取有界、去重、长度适中的句子片段：太短会误伤正常回答，太长几乎不可能再次精确命中。
    """
    fragments: list[str] = []
    for raw in _SENTENCE_SPLIT.split(answer):
        piece = raw.strip()
        if not _MIN_EXCLUDE_CHARS <= len(piece) <= _MAX_EXCLUDE_CHARS:
            continue
        if piece in fragments:
            continue
        fragments.append(piece)
        if len(fragments) == 3:
            break
    return fragments
