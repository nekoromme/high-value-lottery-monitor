from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from high_value_lottery_monitor.providers.ricoh import (
    RicohOnlineStoreProvider,
    parse_entry_period,
    parse_product_page,
)

FIXTURES = Path(__file__).parent / "fixtures"
JST = ZoneInfo("Asia/Tokyo")
PRODUCT_URL = (
    "https://ricohimagingstore.com/Form/Product/"
    "ProductDetail.aspx?cat=002010&pid=S0001551&shop=0"
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_entry_period_parses_omitted_end_year() -> None:
    period, starts_at, ends_at = parse_entry_period(
        "エントリー受付期間：2026年8月24日（月）正午～8月26日（水）正午まで"
    )

    assert period is not None
    assert starts_at == datetime(2026, 8, 24, 12, 0, tzinfo=JST)
    assert ends_at == datetime(2026, 8, 26, 12, 0, tzinfo=JST)


def test_open_product_page_extracts_form_period_and_price() -> None:
    case = parse_product_page(
        fixture("ricoh_gr_iv_open.html"),
        model_name="RICOH GR IV",
        product_url=PRODUCT_URL,
    )

    assert case is not None
    assert case.form_url == "https://forms.gle/AbCdEf012345"
    assert case.starts_at == datetime(2026, 8, 24, 12, 0, tzinfo=JST)
    assert case.ends_at == datetime(2026, 8, 26, 12, 0, tzinfo=JST)
    assert case.price_yen == 211_800


def test_scheduled_product_page_is_detected_before_form_opens() -> None:
    case = parse_product_page(
        fixture("ricoh_gr_iv_scheduled.html"),
        model_name="RICOH GR IV",
        product_url=PRODUCT_URL,
    )

    assert case is not None
    assert case.form_url is None
    assert case.period_text is not None


def test_non_lottery_product_is_ignored() -> None:
    case = parse_product_page(
        "<html><body><h1>RICOH GR IV</h1><p>販売中</p></body></html>",
        model_name="RICOH GR IV",
        product_url=PRODUCT_URL,
    )

    assert case is None


def test_schedule_page_discovers_product_links() -> None:
    products = RicohOnlineStoreProvider.discover_products(
        fixture("ricoh_schedule.html")
    )

    assert products["RICOH GR IV"] == PRODUCT_URL
    assert "RICOH GR IIIx" in products

