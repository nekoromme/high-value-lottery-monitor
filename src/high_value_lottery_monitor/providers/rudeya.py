"""買取ルデヤの新品買取価格をJANコードで安全に取得する。

機種名検索では ``GR IV`` と ``GR IV HDF`` のような似た商品を
取り違えやすい。そこで、RICOH公式商品のJANコードを固定の照合キーにし、
検索結果にも同じJANコードが存在することを確認してから価格を採用する。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


RUDEYA_SEARCH_URL = "https://kaitori-rudeya.com/search/index/{jan}"

# RICOH公式オンラインストアで抽選対象となっている6機種。
# 価格照合は曖昧な商品名ではなく、このJANコードを使用する。
MODEL_JANS = {
    "RICOH GR IV": "4549212311291",
    "RICOH GR IV HDF": "4549212311871",
    "RICOH GR IV Monochrome": "4549212311994",
    "RICOH GR IIIx": "4549212303739",
    "RICOH GR IIIx Urban Edition": "4549212304507",
    "RICOH GR IIIx HDF": "4549212306297",
}


class RudeyaPriceError(RuntimeError):
    """価格を安全に確定できず、自動応募から除外すべき場合。"""


@dataclass(frozen=True, slots=True)
class RudeyaQuote:
    """買取ルデヤから取得した新品・完品の買取価格。"""

    model_name: str
    jan: str
    price_yen: int
    source_url: str


def _normalise_text(value: str) -> str:
    """全角数字・HTML空白・改行差を吸収する。"""

    value = unicodedata.normalize("NFKC", unescape(value))
    return re.sub(r"\s+", " ", value).strip()


def parse_rudeya_search_page(
    html: str, *, expected_jan: str, model_name: str
) -> int:
    """JAN検索結果から新品・完品の基準買取価格を読み取る。

    JANコードがページ内に無い、価格が複数候補になった、0円以下など、
    少しでも判定が怪しい場合は推測せず例外にする。誤って赤字商品へ
    応募するより、その機種だけ止めて人間に確認させるためである。
    """

    if not re.fullmatch(r"\d{13}", expected_jan):
        raise ValueError(f"JANコードの形式が不正です: {expected_jan!r}")

    text = _normalise_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    jan_position = text.find(expected_jan)
    if jan_position < 0:
        raise RudeyaPriceError(
            f"{model_name}: 買取ルデヤの検索結果にJAN {expected_jan} がありません"
        )

    # JANの直後にある商品情報だけを見る。サイドバーや人気商品の別価格を
    # 拾わないよう、次の商品見出しより十分短い範囲へ制限する。
    window = text[jan_position : jan_position + 1_500]
    price_matches = re.findall(
        r"買取価格\s*[¥￥]?\s*([0-9][0-9,]*)\s*円", window
    )
    if not price_matches:
        raise RudeyaPriceError(f"{model_name}: 買取価格を取得できません")

    # 同じ基準価格が商品カードと条件欄に繰り返される場合は許容する。
    # 異なる価格が並ぶ場合は、減額条件を誤採用する恐れがあるため停止する。
    prices = {int(value.replace(",", "")) for value in price_matches}
    if len(prices) != 1:
        raise RudeyaPriceError(
            f"{model_name}: 買取価格が複数あり安全に確定できません: {sorted(prices)}"
        )

    price_yen = prices.pop()
    if price_yen <= 0:
        raise RudeyaPriceError(f"{model_name}: 買取価格が0円以下です")
    return price_yen


class RudeyaClient:
    """買取ルデヤへ低頻度でJAN検索を行うHTTPクライアント。"""

    def __init__(self, user_agent_contact: str | None = None, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        contact = user_agent_contact or "not-configured"
        self.session.headers.update(
            {
                "User-Agent": (
                    "high-value-lottery-monitor/0.2 "
                    f"(+contact: {contact}; user-initiated price check)"
                ),
                "Accept-Language": "ja,en;q=0.5",
            }
        )
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def fetch_quote(self, model_name: str) -> RudeyaQuote:
        """機種名に対応するJAN検索結果を取得して検証する。"""

        jan = MODEL_JANS.get(model_name)
        if jan is None:
            raise RudeyaPriceError(
                f"{model_name}: JANコードが未登録なので自動判定できません"
            )

        url = RUDEYA_SEARCH_URL.format(jan=quote(jan, safe=""))
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        price_yen = parse_rudeya_search_page(
            response.text,
            expected_jan=jan,
            model_name=model_name,
        )
        return RudeyaQuote(
            model_name=model_name,
            jan=jan,
            price_yen=price_yen,
            source_url=url,
        )
