from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from high_value_lottery_monitor.workflow_recovery import decide_recovery


UTC = timezone.utc
NOW = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)


def stamp(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def run(
    *,
    minutes_ago: int,
    status: str = "completed",
    conclusion: str | None = "success",
    run_id: int = 1,
) -> dict[str, object]:
    return {
        "id": run_id,
        "created_at": stamp(minutes_ago),
        "updated_at": stamp(minutes_ago),
        "status": status,
        "conclusion": conclusion,
    }


class RecoveryDecisionTests(unittest.TestCase):
    def decide(self, runs: list[dict[str, object]]):
        return decide_recovery(
            runs,
            now=NOW,
            max_success_age_minutes=450,
            active_grace_minutes=30,
        )

    def test_recent_success_needs_no_recovery(self) -> None:
        decision = self.decide([run(minutes_ago=449)])
        self.assertFalse(decision.should_dispatch)
        self.assertIn("449分", decision.reason)

    def test_old_success_is_restarted(self) -> None:
        decision = self.decide([run(minutes_ago=451)])
        self.assertTrue(decision.should_dispatch)
        self.assertIn("451分", decision.reason)

    def test_fresh_active_run_prevents_duplicate_dispatch(self) -> None:
        decision = self.decide(
            [
                run(
                    minutes_ago=5,
                    status="in_progress",
                    conclusion=None,
                    run_id=2,
                ),
                run(minutes_ago=500),
            ]
        )
        self.assertFalse(decision.should_dispatch)
        self.assertIn("起動済み", decision.reason)

    def test_stale_active_run_does_not_block_recovery(self) -> None:
        decision = self.decide(
            [
                run(
                    minutes_ago=31,
                    status="in_progress",
                    conclusion=None,
                    run_id=2,
                ),
                run(minutes_ago=500),
            ]
        )
        self.assertTrue(decision.should_dispatch)

    def test_no_success_is_restarted(self) -> None:
        decision = self.decide([run(minutes_ago=10, conclusion="failure")])
        self.assertTrue(decision.should_dispatch)
        self.assertIn("正常終了の履歴がない", decision.reason)


if __name__ == "__main__":
    unittest.main()
