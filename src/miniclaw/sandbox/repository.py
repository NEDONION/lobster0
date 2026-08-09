"""ExecutionPlan 与 Receipt 的 immutable SQLite 存储。"""

import sqlite3
from datetime import UTC, datetime

from miniclaw.sandbox.base import ExecutionPlan, ExecutionReceipt, SandboxPlanError
from miniclaw.storage.database import Database


class ExecutionPlanRepository:
    """保存 ToolRun 唯一 plan，并以 compare-on-write 终结 receipt。"""

    def __init__(self, database: Database) -> None:
        """绑定已迁移至 schema v5 的数据库。"""
        self._database = database

    def create(self, tool_run_id: int, plan: ExecutionPlan) -> None:
        """幂等创建 plan；已存在不同 plan 时失败关闭。"""
        with self._database.connect() as connection:
            insert_execution_plan(connection, tool_run_id, plan)

    def get(self, tool_run_id: int) -> ExecutionPlan:
        """读取并重新 canonicalize/hash 校验 plan。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM execution_plans WHERE tool_run_id = ?",
                (tool_run_id,),
            ).fetchone()
        if row is None:
            raise SandboxPlanError("execution_plan_missing")
        plan = ExecutionPlan.from_canonical_json(row["plan_json"])
        if (
            plan.sha256 != row["plan_hash"]
            or plan.schema_version != row["schema_version"]
            or plan.backend != row["backend"]
        ):
            raise SandboxPlanError("execution_plan_mismatch")
        return plan

    def complete(self, tool_run_id: int, receipt: ExecutionReceipt) -> None:
        """只写一次绑定原 plan 的 receipt；相同重试幂等。"""
        plan = self.get(tool_run_id)
        if receipt.plan_hash != plan.sha256 or receipt.backend != plan.backend:
            raise SandboxPlanError("execution_plan_mismatch")
        now = datetime.now(UTC).isoformat()
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM execution_plans WHERE tool_run_id = ?",
                (tool_run_id,),
            ).fetchone()
            if row is None:
                raise SandboxPlanError("execution_plan_missing")
            if row["receipt_json"] is not None:
                if row["receipt_json"] != receipt.canonical_json:
                    raise SandboxPlanError("execution_receipt_conflict")
                return
            updated = connection.execute(
                "UPDATE execution_plans SET receipt_json = ?, completed_at = ? "
                "WHERE tool_run_id = ? AND receipt_json IS NULL",
                (receipt.canonical_json, now, tool_run_id),
            )
            if updated.rowcount != 1:
                raise SandboxPlanError("execution_receipt_conflict")

    def receipt(self, tool_run_id: int) -> ExecutionReceipt | None:
        """读取已终结 receipt；未执行时返回 None。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM execution_plans WHERE tool_run_id = ?",
                (tool_run_id,),
            ).fetchone()
        if row is None:
            raise SandboxPlanError("execution_plan_missing")
        if row["receipt_json"] is None:
            return None
        return ExecutionReceipt.from_canonical_json(row["receipt_json"])


def insert_execution_plan(
    connection: sqlite3.Connection,
    tool_run_id: int,
    plan: ExecutionPlan,
) -> None:
    """在调用方事务内创建 plan row，并拒绝冲突覆盖。"""
    row = connection.execute(
        "SELECT plan_json, plan_hash FROM execution_plans WHERE tool_run_id = ?",
        (tool_run_id,),
    ).fetchone()
    if row is not None:
        if row["plan_json"] != plan.canonical_json or row["plan_hash"] != plan.sha256:
            raise SandboxPlanError("execution_plan_mismatch")
        return
    connection.execute(
        "INSERT INTO execution_plans (tool_run_id, schema_version, plan_json, plan_hash, "
        "backend, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            tool_run_id,
            plan.schema_version,
            plan.canonical_json,
            plan.sha256,
            plan.backend,
            datetime.now(UTC).isoformat(),
        ),
    )
