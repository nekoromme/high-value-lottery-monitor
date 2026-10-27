"""2026/10/27の実際の3機種で、投稿数・リンク・既存履歴を検証する。"""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from high_value_lottery_monitor.models import LotteryCase
from high_value_lottery_monitor.monitor import JsonlAuditLog, run_monitor
from high_value_lottery_monitor.services.discord import DiscordNotifier
from high_value_lottery_monitor.state import EMPTY_STATE
from test_monitor import FakeCalendar

NOW = datetime.fromisoformat("2026-10-27T21:15:42+09:00")


@pytest.fixture
def cases():
    rows = json.loads(
        (Path(__file__).parent / "fixtures/ricoh_20261027_cases.json").read_text()
    )
    for row in rows:
        for key in ("starts_at", "ends_at"):
            row[key] = datetime.fromisoformat(row[key])
    return [LotteryCase(**row) for row in rows]


class Provider:
    def __init__(self, cases):
        self.cases = cases

    def fetch_cases(self):
        return self.cases, []


class CaptureNotifier(DiscordNotifier):
    """実際の通知生成を使い、外部送信だけを止める。"""

    def __init__(self):
        super().__init__("https://example.test/not-a-webhook")
        self.payloads = []
        self.fail = False

    def _post(self, payload):
        if self.fail:
            raise RuntimeError("simulated delivery failure")
        self.payloads.append(payload)
        return f"message-{len(self.payloads)}"


def run(tmp_path, state, notifier, cases, *, mode="run"):
    return run_monitor(
        mode=mode, state=state, state_path=tmp_path / "state.json",
        provider=Provider(cases), notifier=notifier, calendar=FakeCalendar(),
        audit=JsonlAuditLog(tmp_path / "audit.jsonl"), now=NOW,
    )


def armed_state():
    state = deepcopy(EMPTY_STATE)
    state["armed"] = True
    return state


def test_actual_three_models_are_one_compact_post_and_never_repeat(tmp_path, cases):
    state, notifier = armed_state(), CaptureNotifier()
    summary = run(tmp_path, state, notifier, cases)
    assert summary.form_notifications == 3  # 集計は従来どおり機種数
    assert len(notifier.payloads) == 1
    embeds = notifier.payloads[0]["embeds"]
    assert len(embeds) == 1
    assert "3機種" in embeds[0]["title"]
    fields = embeds[0]["fields"]
    assert len(fields) == 5  # 共通期間＋3機種＋検出日時
    assert fields[0]["value"] == "2026/10/28 12:00 ～ 2026/10/30 12:00"
    for case in cases:
        field = next(f for f in fields if f["name"] == case.model_name)
        assert case.form_url in field["value"]
        assert case.product_url in field["value"]
        assert f"{case.price_yen:,}円" in field["value"]
        assert "\n" in field["value"]
        assert state["cases"][case.case_id]["discord_message_id"] == "message-1"
    assert run(tmp_path, state, notifier, cases).form_notifications == 0
    assert len(notifier.payloads) == 1


def test_legacy_notified_model_is_not_included_and_next_round_is_new(tmp_path, cases):
    state, notifier = armed_state(), CaptureNotifier()
    legacy = cases[0].as_state_record(NOW)
    legacy.update(form_notified=True, schedule_notified=True, discord_message_id="old")
    state["cases"][cases[0].case_id] = legacy
    assert run(tmp_path, state, notifier, cases).form_notifications == 2
    assert cases[0].form_url not in json.dumps(notifier.payloads)
    next_round = [
        replace(c, starts_at=c.starts_at + timedelta(days=7),
                ends_at=c.ends_at + timedelta(days=7)) for c in cases
    ]
    assert run(tmp_path, state, notifier, next_round).form_notifications == 3
    assert len(notifier.payloads) == 2


def test_schedule_and_later_forms_each_group_once(tmp_path, cases):
    state, notifier = armed_state(), CaptureNotifier()
    scheduled = [replace(c, form_url=None) for c in cases]
    assert run(tmp_path, state, notifier, scheduled).schedule_notifications == 3
    assert len(notifier.payloads) == 1
    assert run(tmp_path, state, notifier, cases[:2]).form_notifications == 2
    assert run(tmp_path, state, notifier, cases).form_notifications == 1
    assert len(notifier.payloads) == 3


def test_failed_group_retries_without_marking_sent(tmp_path, cases):
    state, notifier = armed_state(), CaptureNotifier()
    notifier.fail = True
    summary = run(tmp_path, state, notifier, cases)
    assert summary.errors and summary.form_notifications == 0
    assert all(not state["cases"][c.case_id]["form_notified"] for c in cases)
    notifier.fail = False
    assert run(tmp_path, state, notifier, cases).form_notifications == 3
    assert len(notifier.payloads) == 1


@pytest.mark.parametrize("difference", ["period", "provider", "unknown", "stage"])
def test_different_rounds_sources_unknown_dates_or_stages_stay_separate(
    tmp_path, cases, difference
):
    first, second = cases[:2]
    if difference == "period":
        second = replace(second, ends_at=second.ends_at + timedelta(days=1))
    elif difference == "provider":
        second = replace(second, provider="other_store")
    elif difference == "unknown":
        first = replace(first, starts_at=None)
        second = replace(second, starts_at=None)
    else:
        second = replace(second, form_url=None)
    notifier = CaptureNotifier()
    run(tmp_path, armed_state(), notifier, [first, second])
    assert len(notifier.payloads) == 2


@pytest.mark.parametrize("mode", ["baseline", "dry-run", "disabled"])
def test_no_notifications_when_suppressed(tmp_path, cases, mode):
    state, notifier = armed_state(), CaptureNotifier()
    if mode == "disabled":
        notifier.webhook_url = None
        # 未設定時は本物の_postに戻して外部送信なし・既知化なしを確認する。
        notifier._post = DiscordNotifier._post.__get__(notifier)
        mode = "run"
    run(tmp_path, state, notifier, cases, mode=mode)
    assert notifier.payloads == []
    if mode == "run":
        assert all(not state["cases"][c.case_id]["form_notified"] for c in cases)
