"""RICOH公式オンラインストアの抽選販売を解析する。

監視の起点は抽選スケジュールページで、そこから各商品ページを辿る。
フォームURLは受付開始時刻になってから商品ページへ追加されるため、
一覧ページだけでなく商品ページも毎回確認する必要がある。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from high_value_lottery_monitor.models import LotteryCase

LOGGER = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

SCHEDULE_URL = "https://ricohimagingstore.com/Page/Feature/FeaturePage009.aspx"

# 一覧ページのHTML構造が一時的に崩れても、既知商品だけは確認し続けるための退避先。
# 新機種は一覧ページから自動発見されるので、毎回ここへ手作業で足す必要はない。
FALLBACK_PRODUCTS = {
    "RICOH GR IV": "https://ricohimagingstore.com/Form/Product/ProductDetail.aspx?cat=002010&pid=S0001551&shop=0",
    "RICOH GR IIIx": "https://ricohimagingstore.com/Form/Product/ProductDetail.aspx?pid=S0015284&shop=0",
    "RICOH GR IV HDF": "https://ricohimagingstore.com/Form/Product/ProductDetail.aspx?cat=002010&pid=S0001566&shop=0",
    "RICOH GR IIIx Urban Edition": "https://ricohimagingstore.com/Form/Product/ProductDetail.aspx?pid=S0001156&shop=0",
    "RICOH GR IV Monochrome": "https://ricohimagingstore.com/Form/Product/ProductDetail.aspx?cat=002010&pid=S0001580&shop=0",
    "RICOH GR IIIx HDF": "https://ricohimagingstore.com/Form/Product/ProductDetail.aspx?pid=S0001281&shop=0",
}


class RicohParseError(RuntimeError):
    """ページは取得できたが、必要な情報を読めなかった場合。"""


@dataclass(frozen=True, slots=True)
class FetchDiagnostic:
    """実行ログへ残す取得状況。"""

    url: str
    ok: bool
    detail: str


def _normalise_text(value: str) -> str:
    """全角数字や改行差を吸収し、正規表現で扱いやすくする。"""

    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


DATE_TOKEN = re.compile(
    r"(?:(?P<year>20\d{2})年)?"
    r"(?:(?P<month>\d{1,2})月)?"
    r"(?P<day>\d{1,2})日"
    r"(?:\([月火水木金土日祝]\))?\s*"
    r"(?P<time>正午|午前\d{1,2}時(?:\d{1,2}分)?|午後\d{1,2}時(?:\d{1,2}分)?|\d{1,2}時(?:\d{1,2}分)?)?"
)


def _parse_time(value: str | None) -> tuple[int, int]:
    if not value:
        # 日付しか取れないケースは日付開始として扱う。
        return 0, 0
    if value == "正午":
        return 12, 0

    match = re.search(r"(?:(午前|午後))?(\d{1,2})時(?:(\d{1,2})分)?", value)
    if not match:
        raise RicohParseError(f"時刻を解釈できません: {value}")

    meridiem, hour_text, minute_text = match.groups()
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if meridiem == "午後" and hour < 12:
        hour += 12
    elif meridiem == "午前" and hour == 12:
        hour = 0
    return hour, minute


def _parse_date_token(
    token: str,
    *,
    default_year: int | None = None,
    default_month: int | None = None,
) -> datetime:
    match = DATE_TOKEN.fullmatch(token.strip())
    if not match:
        raise RicohParseError(f"日付を解釈できません: {token}")

    year = int(match.group("year") or default_year or 0)
    month = int(match.group("month") or default_month or 0)
    if not year or not month:
        raise RicohParseError(f"年または月が不足しています: {token}")
    hour, minute = _parse_time(match.group("time"))
    return datetime(year, month, int(match.group("day")), hour, minute, tzinfo=JST)


def parse_entry_period(text: str) -> tuple[str | None, datetime | None, datetime | None]:
    """商品ページ本文から応募受付期間を取り出す。

    終了側は「8月26日」のように年が省略されるため、開始側の年を補完する。
    年末をまたぐ場合も、終了月が開始月より小さければ翌年に補正する。
    """

    normalised = _normalise_text(text)
    label_position = normalised.find("エントリー受付期間")
    if label_position < 0:
        return None, None, None

    # ラベル後の短い範囲だけを見る。ページ後半の当選日などを誤って拾わないため。
    window = normalised[label_position : label_position + 180]
    tokens = list(DATE_TOKEN.finditer(window))
    if len(tokens) < 2:
        return window, None, None

    start_token = tokens[0].group(0)
    end_token = tokens[1].group(0)
    start = _parse_date_token(start_token)
    end = _parse_date_token(
        end_token, default_year=start.year, default_month=start.month
    )
    if end < start:
        # 12月開始、1月終了など年またぎだけを補正する。
        end = end.replace(year=end.year + 1)

    separator_start = tokens[0].start()
    separator_end = tokens[1].end()
    period_text = window[separator_start:separator_end]
    return period_text, start, end


def _is_google_form(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname == "forms.gle" or hostname == "docs.google.com"


def _extract_form_url(soup: BeautifulSoup, raw_html: str, base_url: str) -> str | None:
    """応募フォームURLをアンカーと生HTMLの両方から探す。"""

    for anchor in soup.find_all("a", href=True):
        candidate = urljoin(base_url, unescape(anchor["href"]))
        if _is_google_form(candidate):
            return candidate

    # JavaScript内へURLが埋め込まれた場合の保険。
    for candidate in re.findall(
        r"https?://(?:forms\.gle|docs\.google\.com/forms)/[^\s\"'<>]+", raw_html
    ):
        candidate = unescape(candidate)
        if _is_google_form(candidate):
            return candidate
    return None


def parse_product_page(
    html: str, *, model_name: str, product_url: str, source_url: str = SCHEDULE_URL
) -> LotteryCase | None:
    """商品ページ1枚を解析する。

    抽選に関係しない通常商品ページだった場合は ``None`` を返す。
    """

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    normalised = _normalise_text(text)
    if "抽選販売" not in normalised and "抽選エントリー" not in normalised:
        return None

    period_text, starts_at, ends_at = parse_entry_period(text)
    form_url = _extract_form_url(soup, html, product_url)

    price_match = re.search(r"定価\s*[¥￥]\s*([0-9,]+)", normalised)
    if not price_match:
        price_match = re.search(r"販売価格\s*[¥￥]?\s*([0-9,]+)円?", normalised)
    price_yen = int(price_match.group(1).replace(",", "")) if price_match else None

    # 日程もフォームも無い古い説明だけのページは、現在の抽選として扱わない。
    if not period_text and not form_url:
        return None

    return LotteryCase(
        provider="ricoh_online_store",
        model_name=model_name,
        product_url=product_url,
        source_url=source_url,
        form_url=form_url,
        period_text=period_text,
        starts_at=starts_at,
        ends_at=ends_at,
        price_yen=price_yen,
    )


class RicohOnlineStoreProvider:
    """RICOH公式オンラインストアを取得するprovider。"""

    name = "ricoh_online_store"

    def __init__(self, user_agent_contact: str | None = None, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        contact = user_agent_contact or "not-configured"
        self.session.headers.update(
            {
                "User-Agent": (
                    "high-value-lottery-monitor/0.1 "
                    f"(+contact: {contact}; polite scheduled monitoring)"
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

    def _get(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        return response.text

    @staticmethod
    def discover_products(schedule_html: str) -> dict[str, str]:
        """スケジュールページからGR商品ページを自動発見する。"""

        soup = BeautifulSoup(schedule_html, "html.parser")
        products: dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = urljoin(SCHEDULE_URL, anchor["href"])
            label = _normalise_text(anchor.get_text(" ", strip=True))
            if (
                label.startswith("RICOH GR")
                and "/Form/Product/ProductDetail.aspx" in href
            ):
                products[label] = href

        # 自動発見できたURLを優先し、足りない既知機種だけを退避URLで補う。
        for model_name, product_url in FALLBACK_PRODUCTS.items():
            products.setdefault(model_name, product_url)
        return products

    def fetch_cases(self) -> tuple[list[LotteryCase], list[FetchDiagnostic]]:
        diagnostics: list[FetchDiagnostic] = []
        try:
            schedule_html = self._get(SCHEDULE_URL)
            diagnostics.append(FetchDiagnostic(SCHEDULE_URL, True, "取得成功"))
        except Exception as exc:
            diagnostics.append(FetchDiagnostic(SCHEDULE_URL, False, repr(exc)))
            raise RuntimeError(f"RICOHスケジュール取得失敗: {exc}") from exc

        products = self.discover_products(schedule_html)
        if not products:
            raise RicohParseError("RICOH商品ページを1件も発見できません")

        cases: list[LotteryCase] = []
        for model_name, product_url in sorted(products.items()):
            try:
                html = self._get(product_url)
                case = parse_product_page(
                    html,
                    model_name=model_name,
                    product_url=product_url,
                    source_url=SCHEDULE_URL,
                )
                detail = "現在の抽選なし" if case is None else "抽選情報を解析"
                diagnostics.append(FetchDiagnostic(product_url, True, detail))
                if case is not None:
                    cases.append(case)
            except Exception as exc:
                LOGGER.exception("商品ページの取得・解析に失敗: %s", product_url)
                diagnostics.append(FetchDiagnostic(product_url, False, repr(exc)))

        # 一部の商品失敗は他機種の通知を止めない。ただし全滅は異常として止める。
        if not cases and all(not item.ok for item in diagnostics[1:]):
            raise RuntimeError("RICOH商品ページがすべて取得失敗しました")
        return cases, diagnostics

