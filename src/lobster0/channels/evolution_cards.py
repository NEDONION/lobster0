"""Controlled Evolution 的飞书 Proposal 摘要卡。

按文档第 2.1 与 13.2 节，IM 上只展示"够 Owner 判断要不要去本机细看"的摘要：目标、变更范围、
评测结论、风险与 candidate hash。**刻意不做**两件事：

* 不在 IM 内展示完整 diff 或候选正文——IM 会话不是 owner-only 边界，历史会被同步、转发、
  被其他客户端缓存；候选正文只在本机 CLI 通过显式 ``--show-diff`` 查看。
* 不提供"一键应用"按钮——apply 必须消费一条绑定精确哈希的 Core Approval，那条路径只在
  本机 CLI 上。卡片给的是"去哪儿看、用哪条命令"，不是执行入口。
"""

from dataclasses import dataclass
from typing import Any

_MAX_SUMMARY_CHARS = 200
_HASH_PREVIEW_CHARS = 12


@dataclass(frozen=True, slots=True, repr=False)
class ProposalSummary:
    """一条 Proposal 在 IM 上可安全展示的封闭字段集合。"""

    proposal_id: int
    target_type: str
    target_name: str
    status: str
    rationale: str
    candidate_hash: str
    eval_passed: bool | None
    eval_total_cases: int
    eval_passed_cases: int
    eval_safety_failures: int

    def __post_init__(self) -> None:
        """拒绝非法编号、空目标与超长摘要，避免把整段正文推到 IM。"""
        if type(self.proposal_id) is not int or self.proposal_id <= 0:
            raise ValueError("proposal_id must be a positive integer")
        if not self.target_type or not self.target_name:
            raise ValueError("proposal target must not be empty")
        if len(self.candidate_hash) != 64:
            raise ValueError("candidate_hash must be a full sha256 digest")
        if len(self.rationale) > _MAX_SUMMARY_CHARS:
            raise ValueError("rationale exceeds the IM summary budget")

    def __repr__(self) -> str:
        """只显示编号与目标，不显示 rationale。"""
        return (
            "ProposalSummary("
            f"proposal_id={self.proposal_id}, target={self.target_type}:{self.target_name})"
        )


def proposal_summary_card(summary: ProposalSummary) -> dict[str, Any]:
    """构造只含摘要字段的飞书 Proposal 卡片。

    Returns:
        不含候选正文、不含任何执行按钮的卡片 payload。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _header_template(summary),
            "title": {
                "tag": "plain_text",
                "content": f"Lobster0 改进提案 #{summary.proposal_id}",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**目标**：`{summary.target_type}:{summary.target_name}`\n"
                    f"**状态**：{summary.status}\n"
                    f"**改动理由**：{summary.rationale or '（未填写）'}\n"
                    f"**评测**：{_eval_line(summary)}\n"
                    f"**候选指纹**：`{summary.candidate_hash[:_HASH_PREVIEW_CHARS]}…`"
                ),
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            "完整改动内容不在这里展示。请在本机执行 "
                            f"lobster0 evolve show {summary.proposal_id} 查看，"
                            "应用需要本机审批。"
                        ),
                    }
                ],
            },
        ],
    }


def _header_template(summary: ProposalSummary) -> str:
    """用配色区分"还没过评测"与"已通过待审批"，但不暗示可以直接应用。"""
    if summary.eval_passed is None:
        return "grey"
    return "green" if summary.eval_passed else "red"


def _eval_line(summary: ProposalSummary) -> str:
    """渲染评测结论；未评测时明确说"未评测"，不留空白让人误以为通过。"""
    if summary.eval_passed is None:
        return "未评测"
    verdict = "通过" if summary.eval_passed else "未通过"
    return (
        f"{verdict}（{summary.eval_passed_cases}/{summary.eval_total_cases} 用例，"
        f"安全失败 {summary.eval_safety_failures}）"
    )
