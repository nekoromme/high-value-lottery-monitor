"""Google Calendarへ応募開始予定を登録する。"""

from __future__ import annotations

import base64
import json
import logging
from datetime import timedelta

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from high_value_lottery_monitor.models import LotteryCase

LOGGER = logging.getLogger(__name__)
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def _decode_service_account(raw_value: str) -> dict:
    """GitHub Secretが生JSONでもBase64でも読めるようにする。"""

    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        try:
            return json.loads(base64.b64decode(raw_value).decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSONをJSONとして解釈できません"
            ) from exc


class GoogleCalendarWriter:
    """カレンダー設定があるときだけ有効になる薄いラッパー。"""

    def __init__(self, service_account_json: str | None, calendar_id: str | None):
        self.calendar_id = calendar_id
        self.service = None
        if not service_account_json or not calendar_id:
            return

        info = _decode_service_account(service_account_json)
        credentials = Credentials.from_service_account_info(
            info, scopes=[CALENDAR_SCOPE]
        )
        self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    @property
    def enabled(self) -> bool:
        return self.service is not None and bool(self.calendar_id)

    @staticmethod
    def event_id(case: LotteryCase) -> str:
        # Googleのevent IDは英小文字a-vと数字を使える。SHA-256の16進文字は条件内。
        return f"ricoh{case.case_id[:32]}"

    def upsert_case(self, case: LotteryCase) -> str | None:
        """同じcase_idなら更新、無ければ作成する。

        日程公開時に作った予定へ、後から応募フォームURLが現れても追記できる。
        """

        if not self.enabled or case.starts_at is None:
            return None

        event_id = self.event_id(case)
        links = [f"商品ページ: {case.product_url}", f"日程ページ: {case.source_url}"]
        if case.form_url:
            links.insert(0, f"応募フォーム: {case.form_url}")

        event = {
            "id": event_id,
            "summary": f"【応募開始】{case.model_name} 抽選販売",
            "description": "\n".join(links),
            "start": {
                "dateTime": case.starts_at.isoformat(),
                "timeZone": "Asia/Tokyo",
            },
            "end": {
                "dateTime": (case.starts_at + timedelta(minutes=15)).isoformat(),
                "timeZone": "Asia/Tokyo",
            },
            "source": {"title": "RICOH公式", "url": case.product_url},
        }

        try:
            self.service.events().insert(
                calendarId=self.calendar_id, body=event, sendUpdates="none"
            ).execute()
        except HttpError as exc:
            if exc.resp.status != 409:
                raise
            # 既存IDならフォームURLなど最新内容に更新する。
            self.service.events().update(
                calendarId=self.calendar_id,
                eventId=event_id,
                body=event,
                sendUpdates="none",
            ).execute()
        LOGGER.info("Google Calendarを更新: %s", event_id)
        return event_id

