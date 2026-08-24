"""Discord Webhookへの通知。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from high_value_lottery_monitor.models import LotteryCase

JST = ZoneInfo("Asia/Tokyo")


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "公式ページから時刻を取得できず"
    return value.astimezone(JST).strftime("%Y/%m/%d %H:%M")


def _format_price(value: int | None) -> str:
    if value is None:
        return "公式ページで要確認"
    return f"{value:,}円"


class DiscordNotifier:
    """通知先Webhookを1か所にまとめる。

    Webhook未設定なら何もしないため、テスト時に誤通知しない。
    """

    def __init__(self, webhook_url: str | None, timeout_seconds: int = 20):
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def _post(self, payload: dict) -> str | None:
        if not self.webhook_url:
            return None
        response = requests.post(
            self.webhook_url,
            params={"wait": "true"},
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        if response.content:
            return str(response.json().get("id") or "") or None
        return None

    def send_case(self, case: LotteryCase, *, opened: bool, detected_at: datetime) -> str | None:
        """日程公開または応募フォーム公開を通知する。"""

        if opened:
            title = "📷 RICOH抽選の応募フォームが公開"
            colour = 0x2ECC71
            description = f"[{case.model_name} の応募フォームを開く]({case.form_url})"
        else:
            title = "🗓️ RICOH抽選の日程を検知"
            colour = 0xF1C40F
            description = "応募フォームは受付開始時刻に商品ページへ公開されます。"

        fields = [
            {"name": "機種", "value": case.model_name, "inline": False},
            {
                "name": "受付期間",
                "value": f"{_format_datetime(case.starts_at)} ～ {_format_datetime(case.ends_at)}",
                "inline": False,
            },
            {"name": "表示価格", "value": _format_price(case.price_yen), "inline": True},
            {
                "name": "検出日時",
                "value": detected_at.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S"),
                "inline": True,
            },
            {
                "name": "公式商品ページ",
                "value": f"[商品ページを開く]({case.product_url})",
                "inline": False,
            },
            {
                "name": "重複防止ID",
                "value": f"`{case.case_id[:12]}`",
                "inline": True,
            },
        ]
        return self._post(
            {
                "username": "高額抽選監視",
                "embeds": [
                    {
                        "title": title,
                        "description": description,
                        "color": colour,
                        "fields": fields,
                        "footer": {"text": "RICOH公式オンラインストア監視"},
                    }
                ],
            }
        )

    def send_health_alert(self, source: str, failures: int, detail: str) -> str | None:
        """連続失敗が規定回数に達したときだけ異常通知する。"""

        return self._post(
            {
                "username": "高額抽選監視",
                "embeds": [
                    {
                        "title": "⚠️ RICOH監視が連続失敗",
                        "description": "抽選情報ではなく監視システムの異常です。",
                        "color": 0xE74C3C,
                        "fields": [
                            {"name": "監視元", "value": source, "inline": False},
                            {"name": "連続失敗", "value": f"{failures}回", "inline": True},
                            {"name": "原因", "value": detail[:1000], "inline": False},
                        ],
                    }
                ],
            }
        )

    def send_recovery(self, source: str) -> str | None:
        return self._post(
            {
                "username": "高額抽選監視",
                "embeds": [
                    {
                        "title": "✅ RICOH監視が復旧",
                        "description": source,
                        "color": 0x3498DB,
                    }
                ],
            }
        )

