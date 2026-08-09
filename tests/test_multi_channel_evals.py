"""Phase 5 Telegram/Discord versioned Channel regression gate。"""

import re
import unittest
from pathlib import Path

from miniclaw.evals.cases import load_cases
from miniclaw.evals.channel import run_channel_suite

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = PROJECT_ROOT / "evals" / "scenarios"

TELEGRAM_IDS = {
    "TELEGRAM-DM-001",
    "TELEGRAM-GROUP-001",
    "TELEGRAM-GROUP-002",
    "TELEGRAM-REPLY-001",
    "TELEGRAM-DEDUPE-001",
    "TELEGRAM-TOOL-001",
    "TELEGRAM-APPROVAL-001",
    "TELEGRAM-DELIVERY-001",
    "TELEGRAM-RESTART-001",
    "TELEGRAM-ISOLATION-001",
}
DISCORD_IDS = {
    "DISCORD-DM-001",
    "DISCORD-GUILD-001",
    "DISCORD-GUILD-002",
    "DISCORD-THREAD-001",
    "DISCORD-DEDUPE-001",
    "DISCORD-TOOL-001",
    "DISCORD-APPROVAL-001",
    "DISCORD-DELIVERY-001",
    "DISCORD-RESTART-001",
    "DISCORD-ISOLATION-001",
    "DISCORD-UX-001",
}


class MultiChannelEvalTest(unittest.IsolatedAsyncioTestCase):
    """保证两个新平台经过真实 Adapter/SQLite/Core boundary，而非字符串 mock。"""

    def _new_cases(self):
        return tuple(
            case
            for case in load_cases(SCENARIO_ROOT)
            if case.id.startswith(("TELEGRAM-", "DISCORD-"))
            and case.status == "active"
            and "channel" in case.layers
        )

    async def test_repository_has_exact_versioned_platform_matrices(self) -> None:
        """Telegram 十条、Discord 十一条均进入 channel/live gate 并提供稳定证据。"""
        cases = self._new_cases()

        telegram_ids = {case.id for case in cases if case.id.startswith("TELEGRAM-")}
        discord_ids = {case.id for case in cases if case.id.startswith("DISCORD-")}
        self.assertEqual(telegram_ids, TELEGRAM_IDS)
        self.assertEqual(discord_ids, DISCORD_IDS)
        self.assertEqual(len(cases), 21)
        self.assertTrue(all(case.layers == ("channel", "live") for case in cases))
        self.assertTrue(all(case.channel_fixture for case in cases))
        self.assertTrue(all(case.expected.channel_evidence for case in cases))

    async def test_all_new_channel_cases_pass_real_local_vertical_slices(self) -> None:
        """21 条场景必须全部通过，失败只能返回稳定短码。"""
        suite = await run_channel_suite(self._new_cases())

        self.assertEqual(suite.total, 21)
        self.assertEqual(suite.passed, 21, suite.cases)
        self.assertEqual(suite.failed, 0)
        self.assertTrue(all(result.failures == () for result in suite.cases))

    async def test_evidence_never_contains_external_ids_paths_or_secrets(self) -> None:
        """版本化 evidence 只允许稳定 snake_case 短码。"""
        suite = await run_channel_suite(self._new_cases())

        for result in suite.cases:
            for item in result.evidence:
                self.assertRegex(item, re.compile(r"^[a-z][a-z0-9_]{1,63}$"))
                self.assertNotIn("/", item)
                self.assertNotIn("token", item)


if __name__ == "__main__":
    unittest.main()
