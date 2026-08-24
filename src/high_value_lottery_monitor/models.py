"""監視結果を受け渡すためのデータ型。

HTML解析、状態保存、Discord通知を直接つなぐと、サイトが増えた際に巨大な一枚岩になる。
そこで、各サイトの解析結果をいったん ``LotteryCase`` に揃える。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class LotteryCase:
    """公式ページから取得した抽選販売1件分。"""

    provider: str
    model_name: str
    product_url: str
    source_url: str
    form_url: str | None
    period_text: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    price_yen: int | None

    @property
    def case_id(self) -> str:
        """同一抽選を安定して識別するIDを作る。

        フォームURLは受付開始時に初めて現れることがあるのでIDには入れない。
        こうすると「日程だけ発表」から「応募フォーム公開」への変化を同一案件として扱える。
        """

        raw = "|".join(
            [
                self.provider,
                self.model_name,
                self.product_url,
                self.starts_at.isoformat() if self.starts_at else self.period_text or "unknown",
                self.ends_at.isoformat() if self.ends_at else "unknown",
            ]
        )
        return sha256(raw.encode("utf-8")).hexdigest()

    def as_state_record(self, now: datetime) -> dict:
        """JSONへ保存できる辞書に変換する。"""

        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "product_url": self.product_url,
            "source_url": self.source_url,
            "form_url": self.form_url,
            "period_text": self.period_text,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "price_yen": self.price_yen,
            "first_seen_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "schedule_notified": False,
            "form_notified": False,
            "calendar_event_id": None,
        }

