#!/usr/bin/env python3
"""人工驱动的 Feishu live smoke 记录器；本脚本从不主动发消息。"""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.evals.cases import load_cases
from miniclaw.paths import build_state_paths, resolve_home
from miniclaw.storage.database import Database


def build_parser() -> argparse.ArgumentParser:
    """创建必须显式确认的 live smoke 参数。"""
    parser = argparse.ArgumentParser(
        description=(
            "Record human-driven Feishu acceptance without sending messages or printing IDs."
        )
    )
    parser.add_argument("--home", help="absolute MiniClaw state directory")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="confirm that a human will interact with the configured test bot",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd() / "evals" / "scenarios",
        help="versioned eval scenario directory",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / ".local" / "eval-results" / "feishu",
        help="ignored local evidence directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """逐条提示人工执行 live case，并只保存脱敏状态和计数。"""
    arguments = build_parser().parse_args(argv)
    if not arguments.confirm_live:
        print("error: --confirm-live is required; no network action was taken", file=sys.stderr)
        return 2
    try:
        paths = build_state_paths(resolve_home(arguments.home))
        cases = tuple(
            case
            for case in load_cases(arguments.root)
            if case.status == "active" and "channel" in case.layers and "live" in case.layers
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    checks = run_local_checks(paths)
    failed_checks = [item.name for item in checks if item.status is CheckStatus.FAIL]
    if failed_checks:
        print(
            "error: doctor preflight failed: " + ", ".join(sorted(failed_checks)),
            file=sys.stderr,
        )
        return 2

    print("MiniClaw Feishu live smoke")
    print("1. In another terminal run: uv run miniclaw gateway")
    print("2. Use only your configured test DM/group; this script sends nothing.")
    print("3. After each action, enter p=pass, f=fail, s=skip.")
    results: list[dict[str, str]] = []
    for case in cases:
        print(f"\n{case.id}: {case.title}\nQuery/action: {case.query}")
        status = _read_status(case.id)
        results.append({"id": case.id, "status": status})

    evidence = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "commit": _commit(),
        "doctor": {
            "pass": sum(item.status is CheckStatus.PASS for item in checks),
            "warn": sum(item.status is CheckStatus.WARN for item in checks),
            "fail": 0,
        },
        "database": _database_counts(paths.database),
        "cases": results,
        "summary": {
            "pass": sum(item["status"] == "pass" for item in results),
            "fail": sum(item["status"] == "fail" for item in results),
            "skip": sum(item["status"] == "skip" for item in results),
        },
    }
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    target = arguments.output_dir / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    target.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"\nSaved redacted evidence: {target}")
    return 1 if evidence["summary"]["fail"] or evidence["summary"]["skip"] else 0


def _read_status(case_id: str) -> str:
    """只接受三个单字符人工结论。"""
    while True:
        try:
            value = input(f"{case_id} [p/f/s]: ").strip().lower()
        except EOFError:
            return "skip"
        if value in {"p", "f", "s"}:
            return {"p": "pass", "f": "fail", "s": "skip"}[value]


def _commit() -> str:
    """读取当前 commit；失败时只保存 unknown。"""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else "unknown"


def _database_counts(path: Path) -> dict[str, dict[str, int]]:
    """只记录状态计数，不读取消息正文、用户 ID 或 Chat ID。"""
    try:
        with Database(path).connect_read_only() as connection:
            return {
                "inbox": {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT status, COUNT(*) FROM processed_events GROUP BY status"
                    )
                },
                "delivery": {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT status, COUNT(*) FROM deliveries GROUP BY status"
                    )
                },
                "approval": {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT status, COUNT(*) FROM approvals GROUP BY status"
                    )
                },
            }
    except Exception:  # noqa: BLE001 - live evidence uses one stable unavailable state
        return {"unavailable": {"count": 1}}


if __name__ == "__main__":
    raise SystemExit(main())
