from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from high_value_lottery_monitor.models import LotteryCase
from high_value_lottery_monitor.monitor import JsonlAuditLog, run_monitor
from high_value_lottery_monitor.providers.ricoh import SCHEDULE_URL, FetchDiagnostic
from high_value_lottery_monitor.state import EMPTY_STATE

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 8, 24, 9, 0, tzinfo=JST)


def make_case(*, form_url: str | None = None, expired: bool = False) -> LotteryCase:
    if expired:
        starts_at = datetime(2026, 7, 1, 12, 0, tzinfo=JST)
        ends_at = datetime(2026, 7, 3, 12, 0, tzinfo=JST)
    else:
        starts_at = datetime(2026, 8, 24, 12, 0, tzinfo=JST)
        ends_at = datetime(2026, 8, 26, 12, 0, tzinfo=JST)
    return LotteryCase(
        provider="ricoh_online_store",
        model_name="RICOH GR IV",
        product_url="https://example.test/products/gr-iv",
        source_url=SCHEDULE_URL,
        form_url=form_url,
        period_text="受付期間",
        starts_at=starts_at,
        ends_at=ends_at,
        price_yen=211_800,
    )


class FakeProvider:
    def __init__(self, case: LotteryCase):
        self.case = case

    def fetch_cases(self):
        return [self.case], [FetchDiagnostic(SCHEDULE_URL, True, "test")]


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.case_calls: list[tuple[LotteryCase, bool]] = []
        self.health_calls: list[tuple] = []

    def send_case(self, case, *, opened, detected_at):
        self.case_calls.append((case, opened))
        return f"message-{len(self.case_calls)}"

    def send_health_alert(self, source, failures, detail):
        self.health_calls.append(("alert", source, failures, detail))

    def send_recovery(self, source):
        self.health_calls.append(("recovery", source))


class FakeCalendar:
    def __init__(self):
        self.calls: list[LotteryCase] = []

    def upsert_case(self, case):
        self.calls.append(case)
        return f"event-{case.case_id[:16]}"


def run_once(
    tmp_path: Path,
    *,
    mode: str,
    state: dict,
    case: LotteryCase,
    notifier: FakeNotifier,
    calendar: FakeCalendar,
):
    return run_monitor(
        mode=mode,
        state=state,
        state_path=tmp_path / "state.json",
        provider=FakeProvider(case),
        notifier=notifier,
        calendar=calendar,
        audit=JsonlAuditLog(tmp_path / "logs" / "audit.jsonl"),
        now=NOW,
    )


def test_auto_first_run_baselines_without_notification(tmp_path: Path) -> None:
    state = deepcopy(EMPTY_STATE)
    notifier = FakeNotifier()
    calendar = FakeCalendar()

    summary = run_once(
        tmp_path,
        mode="auto",
        state=state,
        case=make_case(),
        notifier=notifier,
        calendar=calendar,
    )

    assert summary.mode == "baseline(auto)"
    assert state["armed"] is True
    assert notifier.case_calls == []
    assert calendar.calls == []


def test_form_appearance_after_baseline_notifies_once(tmp_path: Path) -> None:
    state = deepcopy(EMPTY_STATE)
    notifier = FakeNotifier()
    calendar = FakeCalendar()

    run_once(
        tmp_path,
        mode="baseline",
        state=state,
        case=make_case(),
        notifier=notifier,
        calendar=calendar,
    )
    opened_case = make_case(form_url="https://forms.gle/new-form")
    first = run_once(
        tmp_path,
        mode="run",
        state=state,
        case=opened_case,
        notifier=notifier,
        calendar=calendar,
    )
    second = run_once(
        tmp_path,
        mode="run",
        state=state,
        case=opened_case,
        notifier=notifier,
        calendar=calendar,
    )

    assert first.form_notifications == 1
    assert second.form_notifications == 0
    assert [opened for _, opened in notifier.case_calls] == [True]
    # 初回作成後、変化のない3回目ではCalendar APIを再度呼ばない。
    assert len(calendar.calls) == 1


def test_schedule_then_form_updates_calendar_and_notifies_each_stage(
    tmp_path: Path,
) -> None:
    state = deepcopy(EMPTY_STATE)
    state["armed"] = True
    notifier = FakeNotifier()
    calendar = FakeCalendar()

    scheduled = run_once(
        tmp_path,
        mode="run",
        state=state,
        case=make_case(),
        notifier=notifier,
        calendar=calendar,
    )
    opened = run_once(
        tmp_path,
        mode="run",
        state=state,
        case=make_case(form_url="https://forms.gle/new-form"),
        notifier=notifier,
        calendar=calendar,
    )

    assert scheduled.schedule_notifications == 1
    assert opened.form_notifications == 1
    assert [is_opened for _, is_opened in notifier.case_calls] == [False, True]
    assert len(calendar.calls) == 2


def test_expired_case_never_notifies(tmp_path: Path) -> None:
    state = deepcopy(EMPTY_STATE)
    state["armed"] = True
    notifier = FakeNotifier()
    calendar = FakeCalendar()

    summary = run_once(
        tmp_path,
        mode="run",
        state=state,
        case=make_case(form_url="https://forms.gle/old", expired=True),
        notifier=notifier,
        calendar=calendar,
    )

    assert summary.form_notifications == 0
    assert summary.schedule_notifications == 0
    assert notifier.case_calls == []
    assert calendar.calls == []

