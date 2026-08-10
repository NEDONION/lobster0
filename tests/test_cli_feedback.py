"""Lobster0 feedback CLI 子命令的行为测试。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from lobster0.evolution.models import FeedbackRating  # noqa: E402
from lobster0.evolution.repository import FeedbackRepository  # noqa: E402
from lobster0.paths import build_state_paths  # noqa: E402
from lobster0.storage.database import Database  # noqa: E402
from lobster0.storage.repositories import OwnerRepository  # noqa: E402
from tests.test_cli import run_cli  # noqa: E402


class CliFeedbackTest(unittest.TestCase):
    """验证 list/show/forget 只读写 Repository，不加载 Provider。"""

    def _seed_message(self, database: Database, owner_id: int) -> int:
        """插入一条最小 assistant message，返回其内部 ID。"""
        with database.connect() as connection:
            session = connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, 'cli', 'local', 'feedback-cli-test', 'active',
                          '2026-08-11T00:00:00+00:00', '2026-08-11T00:00:00+00:00')
                """,
                (owner_id,),
            ).lastrowid
            message = connection.execute(
                """
                INSERT INTO messages (session_id, role, content, created_at)
                VALUES (?, 'assistant', 'SECRET_SENTINEL reply', '2026-08-11T00:00:00+00:00')
                """,
                (session,),
            ).lastrowid
        return int(message)

    def test_feedback_commands_are_repository_only_and_redact_reason(self) -> None:
        """list/show/forget 不加载 Provider，show/list 都不输出完整正文。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            paths = build_state_paths(Path(directory).resolve())
            database = Database(paths.database)
            owner = OwnerRepository(database).get_or_create()
            message_id = self._seed_message(database, owner.id)
            created = FeedbackRepository(database).record(
                owner_id=owner.id,
                message_id=message_id,
                rating=FeedbackRating.BAD,
                redacted_reason="没有真正调用工具",
                context_hash="a" * 64,
            )

            with mock.patch(
                "lobster0.runtime.create_runtime",
                side_effect=AssertionError("Provider runtime must not load"),
            ):
                listed = run_cli(["feedback", "--home", directory, "list"])
                filtered = run_cli(
                    ["feedback", "--home", directory, "list", "--rating", "good"]
                )
                shown = run_cli(
                    ["feedback", "--home", directory, "show", str(created.id)]
                )

        self.assertEqual(listed[0], 0)
        self.assertIn(f"feedback={created.id}", listed[1])
        self.assertIn("rating=bad", listed[1])
        self.assertNotIn("SECRET_SENTINEL", listed[1])
        self.assertNotIn("没有真正调用工具", listed[1])

        self.assertEqual(filtered[0], 0)
        self.assertIn("No feedback.", filtered[1])

        self.assertEqual(shown[0], 0)
        self.assertIn(f"feedback={created.id}", shown[1])
        self.assertIn("没有真正调用工具", shown[1])
        self.assertNotIn("SECRET_SENTINEL", shown[1])

    def test_forget_clears_reason_and_show_not_found_returns_stable_exit_code(self) -> None:
        """forget 之后 show 不能再显示原因；不存在的 ID 必须映射为退出码 4。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            paths = build_state_paths(Path(directory).resolve())
            database = Database(paths.database)
            owner = OwnerRepository(database).get_or_create()
            message_id = self._seed_message(database, owner.id)
            created = FeedbackRepository(database).record(
                owner_id=owner.id,
                message_id=message_id,
                rating=FeedbackRating.BAD,
                redacted_reason="敏感原因",
                context_hash="b" * 64,
            )

            forgotten = run_cli(["feedback", "--home", directory, "forget", str(created.id)])
            shown_after = run_cli(
                ["feedback", "--home", directory, "show", str(created.id)]
            )
            missing = run_cli(["feedback", "--home", directory, "show", "999999"])

        self.assertEqual(forgotten[0], 0)
        self.assertIn("status=forgotten", forgotten[1])

        self.assertEqual(shown_after[0], 0)
        self.assertIn("reason=-", shown_after[1])
        self.assertNotIn("敏感原因", shown_after[1])

        self.assertEqual(missing[0], 4)
        self.assertIn("feedback_not_found", missing[2])

    def test_uninitialized_state_returns_config_error(self) -> None:
        """未初始化的 home 必须返回配置错误退出码 2，而不是尝试建库。"""
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli(["feedback", "--home", directory, "list"])

        self.assertEqual(result[0], 2)


if __name__ == "__main__":
    unittest.main()
