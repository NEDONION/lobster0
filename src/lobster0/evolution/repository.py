"""Feedback、Proposal、Eval 与 ActiveRevision 的事务化 Repository。"""

import sqlite3
from datetime import UTC, datetime

from lobster0.evolution.models import (
    ActiveRevision,
    EvalCaseResult,
    EvalCaseStatus,
    EvalRun,
    EvalRunStatus,
    Feedback,
    FeedbackRating,
    FeedbackStatus,
    Proposal,
    ProposalStatus,
    ProposalTargetType,
    ProposalVersion,
)
from lobster0.storage.database import Database

_ALLOWED_PROPOSAL_TRANSITIONS: frozenset[tuple[ProposalStatus, ProposalStatus]] = frozenset({
    (ProposalStatus.DRAFT, ProposalStatus.EVALUATING),
    (ProposalStatus.EVALUATING, ProposalStatus.DRAFT),
    (ProposalStatus.EVALUATING, ProposalStatus.REJECTED),
    (ProposalStatus.EVALUATING, ProposalStatus.APPROVED),
    (ProposalStatus.EVALUATING, ProposalStatus.FAILED),
    (ProposalStatus.APPROVED, ProposalStatus.APPLIED),
    (ProposalStatus.APPROVED, ProposalStatus.REJECTED),
    (ProposalStatus.APPLIED, ProposalStatus.ROLLED_BACK),
    (ProposalStatus.APPLIED, ProposalStatus.FAILED),
    (ProposalStatus.FAILED, ProposalStatus.EVALUATING),
})


class EvolutionError(RuntimeError):
    """表示可安全展示给 CLI/IM 的 Evolution 状态冲突。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码，丢弃可能包含内部细节的原始异常。"""
        super().__init__(message)
        self.code = code


class FeedbackRepository:
    """记录、列出与遗忘 Owner 对 assistant message 的评价。"""

    def __init__(self, database: Database, *, clock: type[datetime] = datetime) -> None:
        """绑定已迁移到 v7 的数据库与可替换的时钟。"""
        self._database = database
        self._clock = clock

    def record(
        self,
        *,
        owner_id: int,
        message_id: int,
        rating: FeedbackRating,
        redacted_reason: str | None,
        context_hash: str,
    ) -> Feedback:
        """插入一条脱敏后的反馈；同一 Owner 对同一 message 只能有一条。

        Raises:
            EvolutionError: Owner 已对该 message 记录过反馈。
        """
        now = self._clock.now(UTC)
        with self._database.connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO feedback (
                        owner_id, message_id, rating, redacted_reason,
                        context_hash, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (
                        owner_id,
                        message_id,
                        rating.value,
                        redacted_reason,
                        context_hash,
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise EvolutionError(
                    "feedback_already_recorded",
                    "owner already recorded feedback for this message",
                ) from error
            row = connection.execute(
                "SELECT * FROM feedback WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _feedback_from_row(row)

    def get(self, owner_id: int, feedback_id: int) -> Feedback:
        """读取一条反馈，区分不存在与 Owner 不匹配。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM feedback WHERE id = ?", (feedback_id,)
            ).fetchone()
        if row is None:
            raise EvolutionError("feedback_not_found", "feedback was not found")
        if row["owner_id"] != owner_id:
            raise EvolutionError("not_owner", "feedback belongs to a different owner")
        return _feedback_from_row(row)

    def list(
        self, owner_id: int, *, rating: FeedbackRating | None = None
    ) -> tuple[Feedback, ...]:
        """按 ID 顺序返回当前 Owner 的反馈，可选按 rating 过滤。"""
        query = "SELECT * FROM feedback WHERE owner_id = ?"
        parameters: tuple[object, ...] = (owner_id,)
        if rating is not None:
            query += " AND rating = ?"
            parameters += (rating.value,)
        query += " ORDER BY id"
        with self._database.connect_read_only() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_feedback_from_row(row) for row in rows)

    def forget(self, owner_id: int, feedback_id: int) -> Feedback:
        """清除反馈 reason 材料并标记为已遗忘；保留不可逆 hash。"""
        now = self._clock.now(UTC)
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM feedback WHERE id = ?", (feedback_id,)
            ).fetchone()
            if row is None:
                raise EvolutionError("feedback_not_found", "feedback was not found")
            if row["owner_id"] != owner_id:
                raise EvolutionError("not_owner", "feedback belongs to a different owner")
            if row["status"] == FeedbackStatus.FORGOTTEN.value:
                return _feedback_from_row(row)
            connection.execute(
                """
                UPDATE feedback
                SET status = 'forgotten', redacted_reason = NULL, forgotten_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (now.isoformat(), feedback_id, owner_id),
            )
            updated = connection.execute(
                "SELECT * FROM feedback WHERE id = ?", (feedback_id,)
            ).fetchone()
        return _feedback_from_row(updated)


class ProposalRepository:
    """创建 Proposal、追加 append-only ProposalVersion 并驱动状态机。"""

    def __init__(self, database: Database, *, clock: type[datetime] = datetime) -> None:
        """绑定已迁移到 v7 的数据库与可替换的时钟。"""
        self._database = database
        self._clock = clock

    def create_draft(
        self,
        *,
        owner_id: int,
        feedback_id: int,
        target_type: ProposalTargetType,
        target_name: str,
        base_hash: str,
        candidate_hash: str,
        manifest_json: str,
        candidate_ref: str,
        rationale: str,
    ) -> tuple[Proposal, ProposalVersion]:
        """原子创建一个 draft Proposal 及其首个 immutable version。"""
        now = self._clock.now(UTC)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal_cursor = connection.execute(
                """
                INSERT INTO proposals (
                    owner_id, feedback_id, target_type, target_name,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    owner_id,
                    feedback_id,
                    target_type.value,
                    target_name,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            proposal_id = int(proposal_cursor.lastrowid)
            version = _insert_version(
                connection,
                proposal_id=proposal_id,
                ordinal=1,
                base_hash=base_hash,
                candidate_hash=candidate_hash,
                manifest_json=manifest_json,
                candidate_ref=candidate_ref,
                rationale=rationale,
                now=now,
            )
            connection.execute(
                "UPDATE proposals SET current_version_id = ? WHERE id = ?",
                (version.id, proposal_id),
            )
            proposal_row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return _proposal_from_row(proposal_row), version

    def add_version(
        self,
        owner_id: int,
        proposal_id: int,
        *,
        base_hash: str,
        candidate_hash: str,
        manifest_json: str,
        candidate_ref: str,
        rationale: str,
    ) -> ProposalVersion:
        """在既有 evaluating Proposal 上追加下一个不可变候选版本，并把状态切回 draft。

        Raises:
            EvolutionError: Proposal 不存在、Owner 不匹配，或当前不是 evaluating 状态。
        """
        now = self._clock.now(UTC)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal_row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            _require_owned_proposal(proposal_row, owner_id)
            proposal = _proposal_from_row(proposal_row)
            if proposal.status is not ProposalStatus.EVALUATING:
                raise EvolutionError(
                    "proposal_not_evaluating",
                    "proposal must be evaluating to add a revised candidate version",
                )
            ordinal_row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM proposal_versions "
                "WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            version = _insert_version(
                connection,
                proposal_id=proposal_id,
                ordinal=int(ordinal_row[0]),
                base_hash=base_hash,
                candidate_hash=candidate_hash,
                manifest_json=manifest_json,
                candidate_ref=candidate_ref,
                rationale=rationale,
                now=now,
            )
            connection.execute(
                """
                UPDATE proposals
                SET current_version_id = ?, status = 'draft', updated_at = ?
                WHERE id = ?
                """,
                (version.id, now.isoformat(), proposal_id),
            )
        return version

    def get(self, owner_id: int, proposal_id: int) -> Proposal:
        """读取一个 Proposal，区分不存在与 Owner 不匹配。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        _require_owned_proposal(row, owner_id)
        return _proposal_from_row(row)

    def list(
        self, owner_id: int, *, status: ProposalStatus | None = None
    ) -> tuple[Proposal, ...]:
        """按 ID 顺序返回当前 Owner 的 Proposal，可选按状态过滤。"""
        query = "SELECT * FROM proposals WHERE owner_id = ?"
        parameters: tuple[object, ...] = (owner_id,)
        if status is not None:
            query += " AND status = ?"
            parameters += (status.value,)
        query += " ORDER BY id"
        with self._database.connect_read_only() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_proposal_from_row(row) for row in rows)

    def get_version(self, owner_id: int, version_id: int) -> ProposalVersion:
        """按 Owner 归属读取一个 immutable ProposalVersion。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT pv.*, p.owner_id AS proposal_owner_id
                FROM proposal_versions pv
                JOIN proposals p ON p.id = pv.proposal_id
                WHERE pv.id = ?
                """,
                (version_id,),
            ).fetchone()
        if row is None:
            raise EvolutionError("proposal_version_not_found", "proposal version not found")
        if row["proposal_owner_id"] != owner_id:
            raise EvolutionError("not_owner", "proposal belongs to a different owner")
        return _version_from_row(row)

    def transition(
        self,
        owner_id: int,
        proposal_id: int,
        *,
        expected_status: ProposalStatus,
        new_status: ProposalStatus,
    ) -> Proposal:
        """按文档状态机原子推进 Proposal 状态，拒绝任何未列出的跳转。"""
        self._require_transition(expected_status, new_status)
        now = self._clock.now(UTC)
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE proposals SET status = ?, updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = ?
                """,
                (new_status.value, now.isoformat(), proposal_id, owner_id, expected_status.value),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
                _require_owned_proposal(row, owner_id)
                raise EvolutionError(
                    "proposal_status_changed",
                    "proposal status no longer matches the expected transition source",
                )
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return _proposal_from_row(row)

    def _require_transition(
        self, current: ProposalStatus, target: ProposalStatus
    ) -> None:
        """拒绝文档第 7 节状态机未显式允许的跳转，例如 draft -> applied。"""
        if (current, target) not in _ALLOWED_PROPOSAL_TRANSITIONS:
            raise EvolutionError(
                "proposal_transition_denied",
                f"proposal cannot move from {current.value} to {target.value}",
            )


class EvalRepository:
    """记录 EvalRun 与逐 case 结果；不可原地修改已完成的判定。"""

    def __init__(self, database: Database, *, clock: type[datetime] = datetime) -> None:
        """绑定已迁移到 v7 的数据库与可替换的时钟。"""
        self._database = database
        self._clock = clock

    def start_run(self, *, proposal_version_id: int, suite_manifest_hash: str) -> EvalRun:
        """为一个 immutable ProposalVersion 新开一次 running EvalRun。"""
        now = self._clock.now(UTC)
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO eval_runs (
                    proposal_version_id, suite_manifest_hash, status, created_at
                ) VALUES (?, ?, 'running', ?)
                """,
                (proposal_version_id, suite_manifest_hash, now.isoformat()),
            )
            row = connection.execute(
                "SELECT * FROM eval_runs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _eval_run_from_row(row)

    def record_case_result(
        self,
        eval_run_id: int,
        *,
        case_id: str,
        suite_version: str,
        status: EvalCaseStatus,
        latency_ms: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        result_hash: str,
    ) -> EvalCaseResult:
        """追加一条不可变 case 判定；同一 run 内 case_id 只能出现一次。

        Raises:
            EvolutionError: 该 case 在这次 EvalRun 中已经记录过结果。
        """
        with self._database.connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO eval_case_results (
                        eval_run_id, case_id, suite_version, status,
                        latency_ms, input_tokens, output_tokens, result_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eval_run_id,
                        case_id,
                        suite_version,
                        status.value,
                        latency_ms,
                        input_tokens,
                        output_tokens,
                        result_hash,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise EvolutionError(
                    "eval_case_result_duplicate",
                    "case already has a recorded result for this eval run",
                ) from error
            row = connection.execute(
                "SELECT * FROM eval_case_results WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _eval_case_result_from_row(row)

    def complete_run(
        self,
        eval_run_id: int,
        *,
        status: EvalRunStatus,
        receipt_hash: str | None,
        total_cases: int,
        passed_cases: int,
        safety_failures: int,
        duration_ms: int | None,
    ) -> EvalRun:
        """把一次 running EvalRun 原子结算为最终状态，禁止重复结算。

        Raises:
            EvolutionError: 该 EvalRun 已经不是 running 状态。
        """
        now = self._clock.now(UTC)
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE eval_runs
                SET status = ?, receipt_hash = ?, total_cases = ?, passed_cases = ?,
                    safety_failures = ?, duration_ms = ?, completed_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status.value,
                    receipt_hash,
                    total_cases,
                    passed_cases,
                    safety_failures,
                    duration_ms,
                    now.isoformat(),
                    eval_run_id,
                ),
            )
            if updated.rowcount != 1:
                raise EvolutionError(
                    "eval_run_not_running", "eval run is not running or does not exist"
                )
            row = connection.execute(
                "SELECT * FROM eval_runs WHERE id = ?", (eval_run_id,)
            ).fetchone()
        return _eval_run_from_row(row)

    def get_run(self, eval_run_id: int) -> EvalRun:
        """读取一次 EvalRun 的当前聚合结果。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM eval_runs WHERE id = ?", (eval_run_id,)
            ).fetchone()
        if row is None:
            raise EvolutionError("eval_run_not_found", "eval run was not found")
        return _eval_run_from_row(row)

    def list_case_results(self, eval_run_id: int) -> tuple[EvalCaseResult, ...]:
        """按插入顺序返回一次 EvalRun 下全部 case 判定。"""
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT * FROM eval_case_results WHERE eval_run_id = ? ORDER BY id",
                (eval_run_id,),
            ).fetchall()
        return tuple(_eval_case_result_from_row(row) for row in rows)


class ActiveRevisionRepository:
    """用 SQLite compare-and-swap 原子切换与回滚 active revision 指针。"""

    def __init__(self, database: Database, *, clock: type[datetime] = datetime) -> None:
        """绑定已迁移到 v7 的数据库与可替换的时钟。"""
        self._database = database
        self._clock = clock

    def get(
        self, owner_id: int, target_type: ProposalTargetType, target_name: str
    ) -> ActiveRevision | None:
        """读取一个 target 当前的 active revision；从未切换过时返回 None。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT * FROM active_revision
                WHERE owner_id = ? AND target_type = ? AND target_name = ?
                """,
                (owner_id, target_type.value, target_name),
            ).fetchone()
        return None if row is None else _active_revision_from_row(row)

    def activate(
        self,
        owner_id: int,
        target_type: ProposalTargetType,
        target_name: str,
        *,
        proposal_version_id: int,
        expected_current_version_id: int | None,
    ) -> ActiveRevision:
        """把 active pointer 从期望的当前版本原子切换到新版本。

        Args:
            expected_current_version_id: 调用方评测时看到的 active version；
                首次激活该 target 时传 ``None``。

        Raises:
            EvolutionError: active base 在评测后已经变化（CAS 失败）。
        """
        now = self._clock.now(UTC)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM active_revision
                WHERE owner_id = ? AND target_type = ? AND target_name = ?
                """,
                (owner_id, target_type.value, target_name),
            ).fetchone()
            current_version_id = None if row is None else row["proposal_version_id"]
            if current_version_id != expected_current_version_id:
                raise EvolutionError(
                    "active_base_changed",
                    "active revision no longer matches the proposal's base version",
                )
            if row is None:
                connection.execute(
                    """
                    INSERT INTO active_revision (
                        owner_id, target_type, target_name,
                        proposal_version_id, previous_version_id, activated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        owner_id,
                        target_type.value,
                        target_name,
                        proposal_version_id,
                        now.isoformat(),
                    ),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE active_revision
                    SET proposal_version_id = ?, previous_version_id = ?, activated_at = ?
                    WHERE owner_id = ? AND target_type = ? AND target_name = ?
                      AND proposal_version_id = ?
                    """,
                    (
                        proposal_version_id,
                        current_version_id,
                        now.isoformat(),
                        owner_id,
                        target_type.value,
                        target_name,
                        current_version_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise EvolutionError(
                        "active_base_changed",
                        "active revision no longer matches the proposal's base version",
                    )
            result_row = connection.execute(
                """
                SELECT * FROM active_revision
                WHERE owner_id = ? AND target_type = ? AND target_name = ?
                """,
                (owner_id, target_type.value, target_name),
            ).fetchone()
        return _active_revision_from_row(result_row)

    def rollback(
        self,
        owner_id: int,
        target_type: ProposalTargetType,
        target_name: str,
        *,
        expected_current_version_id: int,
    ) -> ActiveRevision:
        """把 active pointer 原子切回记录中的 previous immutable revision。

        Raises:
            EvolutionError: 当前指针已不是期望版本，或没有可回滚的 previous revision。
        """
        now = self._clock.now(UTC)
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM active_revision
                WHERE owner_id = ? AND target_type = ? AND target_name = ?
                """,
                (owner_id, target_type.value, target_name),
            ).fetchone()
            if row is None or row["proposal_version_id"] != expected_current_version_id:
                raise EvolutionError(
                    "active_base_changed",
                    "active revision no longer matches the proposal's current version",
                )
            if row["previous_version_id"] is None:
                raise EvolutionError(
                    "no_previous_revision",
                    "active revision has no previous version to roll back to",
                )
            connection.execute(
                """
                UPDATE active_revision
                SET proposal_version_id = ?, previous_version_id = NULL, activated_at = ?
                WHERE owner_id = ? AND target_type = ? AND target_name = ?
                  AND proposal_version_id = ?
                """,
                (
                    row["previous_version_id"],
                    now.isoformat(),
                    owner_id,
                    target_type.value,
                    target_name,
                    expected_current_version_id,
                ),
            )
            result_row = connection.execute(
                """
                SELECT * FROM active_revision
                WHERE owner_id = ? AND target_type = ? AND target_name = ?
                """,
                (owner_id, target_type.value, target_name),
            ).fetchone()
        return _active_revision_from_row(result_row)


def _insert_version(
    connection: sqlite3.Connection,
    *,
    proposal_id: int,
    ordinal: int,
    base_hash: str,
    candidate_hash: str,
    manifest_json: str,
    candidate_ref: str,
    rationale: str,
    now: datetime,
) -> ProposalVersion:
    """插入一条 append-only ProposalVersion 并返回其持久化后的值。

    Raises:
        EvolutionError: candidate_hash 已经被其他 version 使用过。
    """
    try:
        cursor = connection.execute(
            """
            INSERT INTO proposal_versions (
                proposal_id, ordinal, base_hash, candidate_hash,
                manifest_json, candidate_ref, rationale, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                ordinal,
                base_hash,
                candidate_hash,
                manifest_json,
                candidate_ref,
                rationale,
                now.isoformat(),
            ),
        )
    except sqlite3.IntegrityError as error:
        raise EvolutionError(
            "candidate_hash_duplicate", "this exact candidate has already been proposed"
        ) from error
    row = connection.execute(
        "SELECT * FROM proposal_versions WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return _version_from_row(row)


def _require_owned_proposal(row: sqlite3.Row | None, owner_id: int) -> None:
    """把缺失行与 Owner 不匹配区分成两种稳定错误。"""
    if row is None:
        raise EvolutionError("proposal_not_found", "proposal was not found")
    if row["owner_id"] != owner_id:
        raise EvolutionError("not_owner", "proposal belongs to a different owner")


def _feedback_from_row(row: sqlite3.Row) -> Feedback:
    """把一行 feedback 反序列化为不可变模型。"""
    return Feedback(
        id=row["id"],
        owner_id=row["owner_id"],
        message_id=row["message_id"],
        rating=FeedbackRating(row["rating"]),
        redacted_reason=row["redacted_reason"],
        context_hash=row["context_hash"],
        status=FeedbackStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        forgotten_at=(
            None if row["forgotten_at"] is None else datetime.fromisoformat(row["forgotten_at"])
        ),
    )


def _proposal_from_row(row: sqlite3.Row) -> Proposal:
    """把一行 proposals 反序列化为不可变模型。"""
    return Proposal(
        id=row["id"],
        owner_id=row["owner_id"],
        feedback_id=row["feedback_id"],
        target_type=ProposalTargetType(row["target_type"]),
        target_name=row["target_name"],
        status=ProposalStatus(row["status"]),
        current_version_id=row["current_version_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _version_from_row(row: sqlite3.Row) -> ProposalVersion:
    """把一行 proposal_versions 反序列化为不可变模型。"""
    return ProposalVersion(
        id=row["id"],
        proposal_id=row["proposal_id"],
        ordinal=row["ordinal"],
        base_hash=row["base_hash"],
        candidate_hash=row["candidate_hash"],
        manifest_json=row["manifest_json"],
        candidate_ref=row["candidate_ref"],
        rationale=row["rationale"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _eval_run_from_row(row: sqlite3.Row) -> EvalRun:
    """把一行 eval_runs 反序列化为不可变模型。"""
    return EvalRun(
        id=row["id"],
        proposal_version_id=row["proposal_version_id"],
        suite_manifest_hash=row["suite_manifest_hash"],
        status=EvalRunStatus(row["status"]),
        receipt_hash=row["receipt_hash"],
        total_cases=row["total_cases"],
        passed_cases=row["passed_cases"],
        safety_failures=row["safety_failures"],
        duration_ms=row["duration_ms"],
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=(
            None if row["completed_at"] is None else datetime.fromisoformat(row["completed_at"])
        ),
    )


def _eval_case_result_from_row(row: sqlite3.Row) -> EvalCaseResult:
    """把一行 eval_case_results 反序列化为不可变模型。"""
    return EvalCaseResult(
        id=row["id"],
        eval_run_id=row["eval_run_id"],
        case_id=row["case_id"],
        suite_version=row["suite_version"],
        status=EvalCaseStatus(row["status"]),
        latency_ms=row["latency_ms"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        result_hash=row["result_hash"],
    )


def _active_revision_from_row(row: sqlite3.Row) -> ActiveRevision:
    """把一行 active_revision 反序列化为不可变模型。"""
    return ActiveRevision(
        owner_id=row["owner_id"],
        target_type=ProposalTargetType(row["target_type"]),
        target_name=row["target_name"],
        proposal_version_id=row["proposal_version_id"],
        previous_version_id=row["previous_version_id"],
        activated_at=datetime.fromisoformat(row["activated_at"]),
    )
