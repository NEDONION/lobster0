"""执行封闭、脱敏的 Memory Autopilot versioned fixtures。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.buffer import MemoryBufferRepository
from miniclaw.memory.extractor import ExtractedCandidate
from miniclaw.memory.flush import FlushCoordinator, FlushSourceMessage, MemoryCapture
from miniclaw.memory.maintenance import MemoryMaintenance
from miniclaw.memory.markdown_store import MarkdownUnitDocument, MemoryMarkdownStore
from miniclaw.memory.migration import LegacyMemoryImporter
from miniclaw.memory.models import DisclosureContext, SourceRef
from miniclaw.memory.pipeline import MemoryPipelineHandler
from miniclaw.memory.reconcile import MemoryReconciler
from miniclaw.memory.repository import (
    MemoryCandidateRepository,
    MemoryFlushRun,
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryRunRepository,
    MemoryUnitRepository,
)
from miniclaw.memory.retrieval import MemoryRetrieval, SearchRequest
from miniclaw.memory.review import MemoryReviewService
from miniclaw.memory.service import ExplicitMemoryRequest, MemoryService, RememberResult
from miniclaw.memory.store import MemoryError, MemoryStore
from miniclaw.memory.validator import MemoryCandidateValidator
from miniclaw.paths import build_state_paths
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database

_NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class MemoryFixtureResult:
    """保存不含正文、路径或外部标识的 Memory evidence keys。"""

    evidence: tuple[str, ...]


class _Environment:
    """组装一个临时 Owner Memory Space 的生产级持久化边界。"""

    def __init__(self, root: Path) -> None:
        """初始化真实 migration、Repository、Markdown 与治理服务。"""
        self.paths = build_state_paths(root.resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        self.manifests = MemoryManifestRepository(self.database)
        self.markdown = MemoryMarkdownStore(self.paths, self.manifests)
        self.units = MemoryUnitRepository(self.database)
        self.reviews = MemoryReviewRepository(self.database)
        self.legacy = MemoryStore(self.paths)
        self.service = MemoryService(
            self.markdown,
            self.units,
            self.reviews,
            self.legacy,
        )
        self.governance = MemoryReviewService(
            self.database,
            self.markdown,
            self.units,
            self.reviews,
            self.legacy,
        )
        self.sessions = SessionRepository(self.database)
        self.turns = TurnRepository(self.database)
        self.local = DisclosureContext(
            self.owner.id,
            self.owner.id,
            "cli",
            "local",
            True,
        )

    def source(self, event_id: str, text: str, *, channel: str = "cli") -> SourceRef:
        """创建一条真实 Owner User Message 并返回受约束 SourceRef。"""
        session = (
            self.sessions.get_or_create_cli(self.owner.id, f"eval-{channel}")
            if channel == "cli"
            else self.sessions.get_or_create(
                self.owner.id,
                channel,
                "eval-account",
                f"eval-{channel}",
            )
        )
        turn = self.turns.create_with_user_message(
            session.id,
            event_id,
            "eval-model",
            text,
        )
        with self.database.connect_read_only() as connection:
            message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?",
                    (turn.id,),
                ).fetchone()[0]
            )
        return SourceRef(message_id, session.id, channel)

    def remember(self, event_id: str, fact: str) -> RememberResult:
        """提交一条本地 Owner 明确 remember 请求。"""
        latest = f"请记住：{fact}"
        return self.service.remember_explicit(
            ExplicitMemoryRequest(
                self.local,
                self.source(event_id, latest),
                latest,
                fact,
                _NOW,
            )
        )

    def complete_and_capture(self, event_id: str, text: str) -> None:
        """完成一个真实 Turn，并只写 durable capture receipt。"""
        session = self.sessions.get_or_create_cli(self.owner.id, "eval-pipeline")
        turn = self.turns.create_with_user_message(
            session.id,
            event_id,
            "eval-model",
            text,
        )
        self.turns.mark_running(turn.id)
        self.turns.complete_with_assistant_message(
            turn.id,
            session.id,
            "已收到",
            input_tokens=1,
            output_tokens=1,
            provider_request_id=event_id,
            iterations=1,
            finish_reason="stop",
        )
        MemoryCapture(MemoryBufferRepository(self.database)).capture_completed(
            owner_id=self.owner.id,
            session_id=session.id,
            turn_id=turn.id,
            disclosure=self.local,
        )

    def create_unit(
        self,
        unit_id: str,
        text: str,
        *,
        key: str,
        status: str = "active",
        confidence: float = 1.0,
    ) -> None:
        """按 Markdown-first 顺序创建带真实来源的 eval Unit。"""
        source = self.source(f"source-{unit_id}", text)
        document = MarkdownUnitDocument(
            unit_id,
            self.owner.id,
            key,
            text,
            "preference",
            "private",
            status,
            confidence,
            "low",
            _NOW,
            None,
            (source,),
        )
        write = self.markdown.append(document)
        self.units.create(
            unit_id=unit_id,
            owner_id=self.owner.id,
            key=key,
            text=text,
            kind="preference",
            scope="private",
            status=status,
            confidence=confidence,
            sensitivity="low",
            valid_from=_NOW,
            valid_until=None,
            sources=(source,),
            markdown_hash=write.block_hash,
            now=_NOW,
        )


class _PreferenceExtractor:
    """从当前批次首条 User Message 返回固定低风险偏好。"""

    async def extract(
        self,
        messages: tuple[FlushSourceMessage, ...],
    ) -> tuple[ExtractedCandidate, ...]:
        """返回一条带真实 source id 的确定性候选。"""
        source_id = next(message.id for message in messages if message.role == "user")
        return (
            ExtractedCandidate(
                "用户偏好使用中文回复",
                "preference",
                0.95,
                "low",
                (source_id,),
            ),
        )


class _FailingHandler:
    """模拟 Markdown commit 前 Provider/Extractor 失败。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.markdown_calls = 0

    async def write_markdown(
        self,
        run: MemoryFlushRun,
        messages: tuple[FlushSourceMessage, ...],
    ) -> None:
        """抛出不携带对话正文的合成失败。"""
        del run, messages
        self.markdown_calls += 1
        raise RuntimeError("synthetic provider unavailable")

    async def project(self, run: MemoryFlushRun) -> None:
        """该 fixture 不应进入 Projection。"""
        del run
        raise AssertionError("projection should not run")


class _CheckpointHandler:
    """记录 Markdown/Projection 调用并可让首次 Projection 失败。"""

    def __init__(self, *, fail_projection_once: bool = False) -> None:
        """保存一次性 Projection 故障开关。"""
        self.fail_projection_once = fail_projection_once
        self.markdown_calls = 0
        self.projection_calls = 0

    async def write_markdown(
        self,
        run: MemoryFlushRun,
        messages: tuple[FlushSourceMessage, ...],
    ) -> None:
        """模拟一次幂等 Markdown commit。"""
        del run, messages
        self.markdown_calls += 1

    async def project(self, run: MemoryFlushRun) -> None:
        """按配置让首次 Projection 失败。"""
        del run
        self.projection_calls += 1
        if self.fail_projection_once and self.projection_calls == 1:
            raise RuntimeError("synthetic projection failure")


async def run_memory_fixture(fixture: str, root: Path) -> MemoryFixtureResult:
    """运行一个白名单 Memory fixture 并返回脱敏 evidence。

    Args:
        fixture: 由场景 loader 校验过的固定 fixture 名称。
        root: 本次 case 独占的临时根目录。

    Returns:
        仅包含稳定 evidence key 的结果。

    Raises:
        ValueError: fixture 不在封闭映射中。
        AssertionError: 生产不变量不成立。
    """
    runners = {
        "cross_channel_disclosure": _cross_channel_disclosure,
        "explicit_restart_forget": _explicit_restart_forget,
        "secret_and_source_rejection": _secret_and_source_rejection,
        "short_term_repeat_promotion": _short_term_repeat_promotion,
        "sensitive_and_behavior_review": _sensitive_and_behavior_review,
        "conflict_correction_supersede": _conflict_correction_supersede,
        "provider_failure_retry": _provider_failure_retry,
        "checkpoint_and_lease_recovery": _checkpoint_and_lease_recovery,
        "direct_edit_and_legacy_migration": _direct_edit_and_legacy_migration,
        "chinese_recall_integrity": _chinese_recall_integrity,
    }
    runner = runners.get(fixture)
    if runner is None:
        raise ValueError("unsupported memory eval fixture")
    return await runner(root)


async def _cross_channel_disclosure(root: Path) -> MemoryFixtureResult:
    """验证四入口共用 Owner Space，并对群聊/非 Owner fail closed。"""
    env = _Environment(root)
    remembered = env.remember("cross-channel-source", "用户偏好使用中文回复")
    retrieval = MemoryRetrieval(env.database)
    contexts = (
        env.local,
        DisclosureContext(env.owner.id, env.owner.id, "feishu", "direct", True),
        DisclosureContext(env.owner.id, env.owner.id, "telegram", "direct", True),
        DisclosureContext(env.owner.id, env.owner.id, "discord", "direct", True),
    )
    hits = tuple(
        retrieval.search(SearchRequest(context, "中文回复", 5), now=_NOW).items
        for context in contexts
    )
    assert all(items and items[0].unit.id == remembered.unit_id for items in hits)
    group = DisclosureContext(env.owner.id, env.owner.id, "discord", "group", True)
    other = DisclosureContext(env.owner.id, env.owner.id + 1, "feishu", "direct", True)
    assert not retrieval.search(SearchRequest(group, "中文", 5), now=_NOW).items
    assert not retrieval.search(SearchRequest(other, "中文", 5), now=_NOW).items
    return MemoryFixtureResult(("owner_space_shared", "group_denied", "non_owner_denied"))


async def _explicit_restart_forget(root: Path) -> MemoryFixtureResult:
    """验证明确记忆、重建召回和 preview-bound Forget 跨重启保持一致。"""
    env = _Environment(root)
    remembered = env.remember("restart-source", "用户偏好简洁回答")
    assert remembered.status == "active"
    initialize_state(env.paths)
    restarted_database = Database(env.paths.database)
    restarted = MemoryRetrieval(restarted_database)
    private_contexts = (
        env.local,
        DisclosureContext(env.owner.id, env.owner.id, "feishu", "direct", True),
        DisclosureContext(env.owner.id, env.owner.id, "telegram", "direct", True),
        DisclosureContext(env.owner.id, env.owner.id, "discord", "direct", True),
    )
    assert all(
        restarted.search(SearchRequest(context, "简洁回答", 5), now=_NOW).items
        for context in private_contexts
    )
    preview = env.governance.preview_forget(
        env.local,
        remembered.unit_id,
        now=_NOW + timedelta(minutes=1),
    )
    env.governance.decide(
        env.local,
        preview.review_id,
        preview.preview_hash,
        approve=True,
        now=_NOW + timedelta(minutes=2),
    )
    assert env.units.get(env.owner.id, remembered.unit_id).status == "archived"
    initialize_state(env.paths)
    rebuilt = MemoryRetrieval(Database(env.paths.database))
    assert all(
        not rebuilt.search(SearchRequest(context, "简洁回答", 5), now=_NOW).items
        for context in private_contexts
    )
    return MemoryFixtureResult(
        ("explicit_persisted", "restart_recalled", "forget_archived", "rebuild_absent")
    )


async def _secret_and_source_rejection(root: Path) -> MemoryFixtureResult:
    """验证 Secret 和 fabricated source 在候选持久化前被拒绝。"""
    env = _Environment(root)
    secret = "synthetic-password-value-123456"
    try:
        env.remember("secret-source", f"password: {secret}")
    except MemoryError as error:
        assert secret not in str(error)
    else:
        raise AssertionError("secret memory must be rejected")
    source = env.source("valid-source", "用户偏好中文")
    messages = (FlushSourceMessage(source.message_id, source.session_id, "cli", "user", "x"),)
    validation = MemoryCandidateValidator().validate(
        ExtractedCandidate("用户偏好英文回复", "preference", 0.9, "low", (999_999,)),
        messages,
    )
    assert validation.decision == "rejected" and validation.text is None
    with env.database.connect_read_only() as connection:
        counts = tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("memory_candidates", "memory_units")
        )
    assert counts == (0, 0)
    return MemoryFixtureResult(
        ("secret_rejected", "fabricated_source_rejected", "zero_rejected_persistence")
    )


async def _short_term_repeat_promotion(root: Path) -> MemoryFixtureResult:
    """验证首见 short-term、独立重复晋升和单 Unit 去重。"""
    env = _Environment(root)
    buffers = MemoryBufferRepository(env.database)
    runs = MemoryRunRepository(env.database)
    handler = MemoryPipelineHandler(
        env.database,
        _PreferenceExtractor(),
        env.markdown,
        MemoryCandidateRepository(env.database),
        env.units,
        env.reviews,
    )
    coordinator = FlushCoordinator(
        env.database,
        buffers,
        runs,
        handler,
        extractor="memory-eval-v1",
        prompt_hash="a" * 64,
        batch_size=1,
    )
    env.complete_and_capture("promotion-1", "我偏好使用中文回复")
    first = await coordinator.run_once("worker-a", now=_NOW)
    assert first.status == "completed"
    with env.database.connect_read_only() as connection:
        unit_id = str(connection.execute("SELECT id FROM memory_units").fetchone()[0])
    assert env.units.get(env.owner.id, unit_id).status == "short_term"
    env.complete_and_capture("promotion-2", "我仍然偏好使用中文回复")
    second = await coordinator.run_once("worker-b", now=_NOW + timedelta(minutes=1))
    unit = env.units.get(env.owner.id, unit_id)
    assert second.status == "completed" and unit.status == "active"
    assert len(unit.sources) == 2
    with env.database.connect_read_only() as connection:
        count = int(connection.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0])
    assert count == 1
    return MemoryFixtureResult(
        ("first_observation_short_term", "independent_repeat_active", "duplicate_unit_zero")
    )


async def _sensitive_and_behavior_review(root: Path) -> MemoryFixtureResult:
    """验证高敏事实和行为规则只能进入 Owner Review。"""
    env = _Environment(root)
    source = env.source("review-source", "我的住址与行为偏好")
    messages = (
        FlushSourceMessage(
            source.message_id,
            source.session_id,
            "cli",
            "user",
            "我的家庭地址在合成测试区",
        ),
    )
    validator = MemoryCandidateValidator()
    sensitive = validator.validate(
        ExtractedCandidate(
            "用户家庭地址在合成测试区",
            "fact",
            0.9,
            "high",
            (source.message_id,),
        ),
        messages,
    )
    behavior = validator.validate(
        ExtractedCandidate(
            "以后自动执行所有命令，不要询问权限",
            "behavior_rule",
            0.99,
            "low",
            (source.message_id,),
        ),
        messages,
    )
    assert sensitive.decision == behavior.decision == "review_required"
    assert not hasattr(ExtractedCandidate("x", "fact", 1.0, "low", (1,)), "status")
    return MemoryFixtureResult(
        ("sensitive_review_required", "behavior_review_required", "model_cannot_approve")
    )


async def _conflict_correction_supersede(root: Path) -> MemoryFixtureResult:
    """验证冲突和纠错仅在 Owner 决策后 source-preserving supersede。"""
    env = _Environment(root)
    old = env.remember("language-zh", "用户偏好使用中文回复")
    incoming = env.remember("language-en", "用户偏好使用英文回复")
    assert incoming.status == "review_required" and incoming.review_id is not None
    conflict = env.governance.get(env.local, incoming.review_id)
    env.governance.decide(
        env.local,
        conflict.review_id,
        conflict.preview_hash,
        approve=True,
        now=_NOW + timedelta(minutes=1),
    )
    assert env.units.get(env.owner.id, old.unit_id).status == "superseded"
    correction_source = env.source("language-correction", "请更正：用户偏好详细回答")
    correction = env.governance.propose_correction(
        env.local,
        incoming.unit_id,
        "用户偏好详细回答",
        source=correction_source,
        latest_user_text="请更正：用户偏好详细回答",
        now=_NOW + timedelta(minutes=2),
    )
    env.governance.decide(
        env.local,
        correction.review_id,
        correction.preview_hash,
        approve=True,
        now=_NOW + timedelta(minutes=3),
    )
    corrected = env.units.get(env.owner.id, correction.unit_id)
    assert corrected.status == "active" and corrected.sources == (correction_source,)
    return MemoryFixtureResult(
        ("conflict_review_required", "old_unit_superseded", "correction_source_preserved")
    )


async def _provider_failure_retry(root: Path) -> MemoryFixtureResult:
    """验证 Provider/Markdown 前失败只生成安全 retry 并保留 source range。"""
    env = _Environment(root)
    env.complete_and_capture("provider-failure", "普通但可提取的事实")
    buffers = MemoryBufferRepository(env.database)
    runs = MemoryRunRepository(env.database)
    failing = _FailingHandler()
    coordinator = FlushCoordinator(
        env.database,
        buffers,
        runs,
        failing,
        extractor="memory-eval-failing-v1",
        prompt_hash="b" * 64,
        batch_size=1,
    )
    outcome = await coordinator.run_once("worker-fail", now=_NOW)
    assert outcome.status == "retry" and outcome.error_code == "memory_flush_failed"
    assert outcome.run_id is not None
    run = runs.get(outcome.run_id)
    assert run.status == "retry" and run.first_message_id <= run.last_message_id
    assert buffers.pending_count(env.owner.id) == 0
    with env.database.connect_read_only() as connection:
        assigned = int(
            connection.execute(
                "SELECT COUNT(*) FROM memory_buffers WHERE status = 'assigned'"
            ).fetchone()[0]
        )
    assert assigned == 1 and not env.markdown.path_for_owner(env.owner.id).exists()
    recovered_handler = _CheckpointHandler()
    restarted = FlushCoordinator(
        env.database,
        buffers,
        runs,
        recovered_handler,
        extractor="memory-eval-failing-v1",
        prompt_hash="b" * 64,
        batch_size=1,
    )
    recovered = await restarted.run_once("worker-retry", now=_NOW + timedelta(seconds=3))
    assert recovered.status == "completed"
    assert recovered_handler.markdown_calls == recovered_handler.projection_calls == 1
    return MemoryFixtureResult(
        ("provider_failure_sanitized", "source_range_retryable", "markdown_not_duplicated")
    )


async def _checkpoint_and_lease_recovery(root: Path) -> MemoryFixtureResult:
    """验证 Projection checkpoint、buffer 事务结算和 stale lease 回收。"""
    env = _Environment(root)
    env.complete_and_capture("checkpoint", "普通事实")
    buffers = MemoryBufferRepository(env.database)
    runs = MemoryRunRepository(env.database)
    first_handler = _CheckpointHandler(fail_projection_once=True)
    first = FlushCoordinator(
        env.database,
        buffers,
        runs,
        first_handler,
        extractor="memory-eval-checkpoint-v1",
        prompt_hash="c" * 64,
        batch_size=1,
    )
    failed = await first.run_once("worker-a", now=_NOW)
    assert failed.status == "projection_pending"
    replacement = _CheckpointHandler()
    restarted = FlushCoordinator(
        env.database,
        buffers,
        runs,
        replacement,
        extractor="memory-eval-checkpoint-v1",
        prompt_hash="c" * 64,
        batch_size=1,
    )
    recovered = await restarted.run_once("worker-b", now=_NOW + timedelta(seconds=1))
    assert recovered.status == "completed"
    assert first_handler.markdown_calls == 1 and replacement.markdown_calls == 0
    with env.database.connect_read_only() as connection:
        flushed = int(
            connection.execute(
                "SELECT COUNT(*) FROM memory_buffers WHERE status = 'flushed'"
            ).fetchone()[0]
        )
    assert flushed == 1
    stale_source = env.source("stale-lease", "另一个普通事实")
    stale = runs.enqueue(
        owner_id=env.owner.id,
        first_message_id=stale_source.message_id,
        last_message_id=stale_source.message_id,
        extractor="memory-eval-stale-v1",
        prompt_hash="d" * 64,
        now=_NOW - timedelta(minutes=5),
    )
    runs.claim_next("old-worker", now=_NOW - timedelta(minutes=5), lease_seconds=60)
    maintenance = MemoryMaintenance(env.database, env.markdown, env.units, env.reviews)
    result = maintenance.run_due(env.owner.id, now=_NOW)
    assert result.reclaimed_leases == 1 and runs.get(stale.id).status == "retry"
    return MemoryFixtureResult(
        ("projection_resumed", "stale_lease_reclaimed", "buffer_completed_once")
    )


async def _direct_edit_and_legacy_migration(root: Path) -> MemoryFixtureResult:
    """验证 direct edit fail-closed 对账和 legacy 原件 hash 幂等。"""
    reconcile_env = _Environment(root / "reconcile")
    remembered = reconcile_env.remember("manual-edit", "用户偏好简洁回答")
    reconciler = MemoryReconciler(
        reconcile_env.database,
        reconcile_env.markdown,
        reconcile_env.manifests,
    )
    path = reconcile_env.markdown.path_for_owner(reconcile_env.owner.id)
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("用户偏好简洁回答", "用户偏好详细回答"), encoding="utf-8")
    edited = reconciler.scan(reconcile_env.owner.id)
    assert edited.updated == (remembered.unit_id,)
    malformed = path.read_text(encoding="utf-8").replace(
        f"<!-- miniclaw:end {remembered.unit_id} -->",
        "<!-- broken -->",
    )
    path.write_text(malformed, encoding="utf-8")
    invalid = reconciler.scan(reconcile_env.owner.id)
    assert invalid.errors and path.read_text(encoding="utf-8") == malformed
    assert reconcile_env.units.get(reconcile_env.owner.id, remembered.unit_id).text == (
        "用户偏好详细回答"
    )

    legacy_env = _Environment(root / "legacy")
    legacy_text = "# Long-term Memory\n\n- 用户项目使用 Python 3.12\n"
    legacy_env.paths.memory_file.write_text(legacy_text, encoding="utf-8")
    importer = LegacyMemoryImporter(
        legacy_env.paths,
        legacy_env.database,
        legacy_env.markdown,
        legacy_env.units,
        legacy_env.legacy,
    )
    first = importer.import_all(legacy_env.owner.id, now=_NOW)
    second = importer.import_all(legacy_env.owner.id, now=_NOW + timedelta(minutes=1))
    assert first.unit_ids == second.unit_ids and len(first.unit_ids) == 1
    assert legacy_env.paths.memory_file.read_text(encoding="utf-8") == legacy_text
    return MemoryFixtureResult(
        (
            "manual_edit_reconciled",
            "invalid_edit_fail_closed",
            "legacy_hash_idempotent",
            "legacy_source_untouched",
        )
    )


async def _chinese_recall_integrity(root: Path) -> MemoryFixtureResult:
    """验证中文 Recall@5 上限、完整来源链和唯一 Unit IDs。"""
    env = _Environment(root)
    texts = (
        "用户偏好使用中文回复",
        "用户偏好中文技术说明",
        "用户喜欢中文代码注释",
        "用户项目文档使用中文",
        "用户希望中文错误提示",
        "用户偶尔需要中文摘要",
    )
    for index, text in enumerate(texts):
        env.create_unit(
            f"recall-{index}",
            text,
            key=f"preference.chinese.{index}",
            confidence=1.0 - index / 100,
        )
    result = MemoryRetrieval(env.database).search(
        SearchRequest(env.local, "中文回复 技术 文档 摘要", 5),
        now=_NOW,
    )
    assert len(result.items) == 5
    assert any(item.unit.text == "用户偏好使用中文回复" for item in result.items)
    assert all(item.unit.sources for item in result.items)
    identifiers = tuple(item.unit.id for item in result.items)
    assert len(identifiers) == len(set(identifiers))
    return MemoryFixtureResult(
        ("recall_top5_bounded", "chinese_unit_recalled", "all_units_sourced", "unit_ids_unique")
    )
