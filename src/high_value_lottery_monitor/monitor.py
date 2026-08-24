"""監視全体の進行、重複防止、通知をまとめる。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from high_value_lottery_monitor.models import LotteryCase
from high_value_lottery_monitor.providers.ricoh import (
    SCHEDULE_URL,
    FetchDiagnostic,
    RicohOnlineStoreProvider,
)
from high_value_lottery_monitor.services.calendar import GoogleCalendarWriter
from high_value_lottery_monitor.services.discord import DiscordNotifier
from high_value_lottery_monitor.state import save_state

LOGGER = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
HEALTH_ALERT_THRESHOLD = 3


@dataclass(slots=True)
class RunSummary:
    """GitHub Actionsの画面で確認するための実行結果。"""

    mode: str
    detected_cases: int = 0
    new_cases: int = 0
    schedule_notifications: int = 0
    form_notifications: int = 0
    calendar_updates: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "detected_cases": self.detected_cases,
            "new_cases": self.new_cases,
            "schedule_notifications": self.schedule_notifications,
            "form_notifications": self.form_notifications,
            "calendar_updates": self.calendar_updates,
            "errors": self.errors,
        }


class JsonlAuditLog:
    """あとで原因を追えるよう、重要な判断を1行1JSONで記録する。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **details: object) -> None:
        record = {
            "timestamp": datetime.now(JST).isoformat(),
            "event": event,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _update_health(
    *,
    state: dict,
    diagnostic: FetchDiagnostic,
    notifier: DiscordNotifier,
    now: datetime,
    audit: JsonlAuditLog,
    allow_notifications: bool,
) -> None:
    health = state["source_health"].setdefault(
        diagnostic.url,
        {
            "consecutive_failures": 0,
            "alert_sent": False,
            "last_error": None,
            "last_success_at": None,
        },
    )

    previous_failures = int(health.get("consecutive_failures", 0))
    alert_sent = bool(health.get("alert_sent", False))
    if diagnostic.ok:
        if previous_failures >= HEALTH_ALERT_THRESHOLD and alert_sent and allow_notifications:
            notifier.send_recovery(diagnostic.url)
        health.update(
            {
                "consecutive_failures": 0,
                "alert_sent": False,
                "last_error": None,
                "last_success_at": now.isoformat(),
            }
        )
        audit.write("source_ok", url=diagnostic.url, detail=diagnostic.detail)
        return

    failures = previous_failures + 1
    health.update(
        {
            "consecutive_failures": failures,
            "last_error": diagnostic.detail,
            "last_failure_at": now.isoformat(),
        }
    )
    if (
        failures >= HEALTH_ALERT_THRESHOLD
        and not alert_sent
        and allow_notifications
        and notifier.enabled
    ):
        notifier.send_health_alert(diagnostic.url, failures, diagnostic.detail)
        health["alert_sent"] = True
    audit.write(
        "source_error",
        url=diagnostic.url,
        failures=failures,
        detail=diagnostic.detail,
    )


def _case_is_expired(case: LotteryCase, now: datetime) -> bool:
    return case.ends_at is not None and case.ends_at < now


def _merge_case_record(record: dict, case: LotteryCase, now: datetime) -> None:
    """通知済みフラグを残したまま、公式ページの最新値へ更新する。"""

    record.update(
        {
            "model_name": case.model_name,
            "product_url": case.product_url,
            "source_url": case.source_url,
            "form_url": case.form_url,
            "period_text": case.period_text,
            "starts_at": case.starts_at.isoformat() if case.starts_at else None,
            "ends_at": case.ends_at.isoformat() if case.ends_at else None,
            "price_yen": case.price_yen,
            "last_seen_at": now.isoformat(),
        }
    )


def run_monitor(
    *,
    mode: str,
    state: dict,
    state_path: Path,
    provider: RicohOnlineStoreProvider,
    notifier: DiscordNotifier,
    calendar: GoogleCalendarWriter,
    audit: JsonlAuditLog,
    now: datetime | None = None,
) -> RunSummary:
    """1回分の監視を実行する。

    mode:
      - ``auto``: 未初期化なら無通知baseline、初期化済みなら通常監視
      - ``baseline``: 現在見える案件を既知化して通知しない
      - ``run``: 通常監視。未初期化なら誤通知防止のため停止
      - ``dry-run``: 取得と解析だけ。通知・カレンダー・状態更新なし
    """

    if mode not in {"auto", "baseline", "run", "dry-run"}:
        raise ValueError(f"未対応のmodeです: {mode}")

    now = now or datetime.now(JST)
    summary = RunSummary(mode=mode)
    allow_side_effects = mode not in {"baseline", "dry-run"}

    try:
        cases, diagnostics = provider.fetch_cases()
    except Exception as exc:
        diagnostic = FetchDiagnostic(SCHEDULE_URL, False, repr(exc))
        if mode != "dry-run":
            _update_health(
                state=state,
                diagnostic=diagnostic,
                notifier=notifier,
                now=now,
                audit=audit,
                allow_notifications=allow_side_effects and bool(state.get("armed")),
            )
            save_state(state_path, state)
        audit.write("run_failed", error=repr(exc))
        raise

    summary.detected_cases = len(cases)
    audit.write(
        "fetch_complete",
        detected_cases=len(cases),
        models=[case.model_name for case in cases],
    )

    if mode == "dry-run":
        for diagnostic in diagnostics:
            audit.write(
                "dry_run_source",
                url=diagnostic.url,
                ok=diagnostic.ok,
                detail=diagnostic.detail,
            )
        for case in cases:
            audit.write(
                "dry_run_case",
                case_id=case.case_id,
                model_name=case.model_name,
                starts_at=case.starts_at.isoformat() if case.starts_at else None,
                ends_at=case.ends_at.isoformat() if case.ends_at else None,
                form_url=case.form_url,
                price_yen=case.price_yen,
            )
        return summary

    # 初回scheduled runでも通知爆撃しないよう、自動でbaselineに切り替える。
    effective_mode = mode
    if mode == "auto" and not state.get("armed"):
        effective_mode = "baseline"
        summary.mode = "baseline(auto)"
        allow_side_effects = False
    elif mode == "auto":
        effective_mode = "run"

    if effective_mode == "run" and not state.get("armed"):
        raise RuntimeError(
            "状態が未初期化です。先にbaselineまたはautoを1回実行してください"
        )

    for diagnostic in diagnostics:
        _update_health(
            state=state,
            diagnostic=diagnostic,
            notifier=notifier,
            now=now,
            audit=audit,
            allow_notifications=allow_side_effects,
        )

    if effective_mode == "baseline":
        for case in cases:
            record = state["cases"].setdefault(
                case.case_id, case.as_state_record(now)
            )
            _merge_case_record(record, case, now)
            # baseline時点で存在するものは既知扱い。後からフォームだけ出ても、
            # baseline時点でフォームが無かった場合は受付開始時に通知できるよう残す。
            record["schedule_notified"] = True
            record["form_notified"] = bool(case.form_url)
        state["armed"] = True
        state["baseline_at"] = now.isoformat()
        save_state(state_path, state)
        audit.write("baseline_complete", cases=len(cases))
        return summary

    for case in cases:
        record = state["cases"].get(case.case_id)
        is_new = record is None
        previous_form_url = record.get("form_url") if record else None
        if is_new:
            summary.new_cases += 1
            record = case.as_state_record(now)
            state["cases"][case.case_id] = record
        else:
            _merge_case_record(record, case, now)

        if _case_is_expired(case, now):
            # 過去案件を遅れて発見しても通知しない。再発した過去通知事故の防止策。
            record["schedule_notified"] = True
            record["form_notified"] = bool(case.form_url)
            audit.write("expired_case_suppressed", case_id=case.case_id)
            continue

        # 新規案件、未作成、フォームURL追加時だけ更新する。
        # 毎回upsertすると、監視のたびにCalendar APIを無駄打ちしてしまう。
        calendar_needs_update = (
            is_new
            or not record.get("calendar_event_id")
            or (bool(case.form_url) and case.form_url != previous_form_url)
        )
        if calendar_needs_update:
            # カレンダーは通知より先に更新。失敗してもDiscord通知は続行し、
            # event IDが残らないため次回また再試行できる。
            try:
                event_id = calendar.upsert_case(case)
                if event_id:
                    record["calendar_event_id"] = event_id
                    summary.calendar_updates += 1
            # 外部Calendar SDK由来の例外をここで隔離し、Discord通知は続ける。
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"Calendar: {case.model_name}: {exc!r}")
                audit.write("calendar_error", case_id=case.case_id, error=repr(exc))

        if case.form_url and not record.get("form_notified"):
            try:
                message_id = notifier.send_case(case, opened=True, detected_at=now)
                # Webhookが未設定なら通知済みにしない。設定後に現在の受付を拾える。
                if notifier.enabled:
                    record["form_notified"] = True
                    record["schedule_notified"] = True
                    record["discord_message_id"] = message_id
                    summary.form_notifications += 1
                audit.write("form_detected", case_id=case.case_id, notified=notifier.enabled)
            # Webhookの通信・JSON応答など通知経路の例外をまとめて次回再試行する。
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"Discord: {case.model_name}: {exc!r}")
                audit.write("discord_error", case_id=case.case_id, error=repr(exc))
        elif not record.get("schedule_notified"):
            try:
                message_id = notifier.send_case(case, opened=False, detected_at=now)
                if notifier.enabled:
                    record["schedule_notified"] = True
                    record["discord_message_id"] = message_id
                    summary.schedule_notifications += 1
                audit.write("schedule_detected", case_id=case.case_id, notified=notifier.enabled)
            # Webhookの通信・JSON応答など通知経路の例外をまとめて次回再試行する。
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"Discord: {case.model_name}: {exc!r}")
                audit.write("discord_error", case_id=case.case_id, error=repr(exc))

    state["last_success_at"] = now.isoformat()
    save_state(state_path, state)
    audit.write("run_complete", **summary.as_dict())
    return summary
