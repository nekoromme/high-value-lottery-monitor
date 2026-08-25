from datetime import datetime
from zoneinfo import ZoneInfo

from high_value_lottery_monitor.models import LotteryCase
from high_value_lottery_monitor.pc_prepare import decide_price, is_case_open
from high_value_lottery_monitor.providers.rudeya import RudeyaQuote


JST = ZoneInfo("Asia/Tokyo")


def make_case(price_yen: int | None = 211_800) -> LotteryCase:
    return LotteryCase(
        provider="ricoh_online_store",
        model_name="RICOH GR IV",
        product_url="https://example.test/product",
        source_url="https://example.test/schedule",
        form_url="https://forms.gle/example",
        period_text="受付期間",
        starts_at=datetime(2026, 8, 24, 12, 0, tzinfo=JST),
        ends_at=datetime(2026, 8, 26, 12, 0, tzinfo=JST),
        price_yen=price_yen,
    )


def make_quote(price_yen: int) -> RudeyaQuote:
    return RudeyaQuote(
        model_name="RICOH GR IV",
        jan="4549212311291",
        price_yen=price_yen,
        source_url="https://kaitori-rudeya.com/search/index/4549212311291",
    )


def test_purchase_price_higher_than_buyback_is_skipped() -> None:
    decision = decide_price(make_case(), make_quote(200_000))

    assert decision.should_fill is False
    assert decision.gross_difference_yen == -11_800


def test_equal_price_is_included_as_requested() -> None:
    decision = decide_price(make_case(), make_quote(211_800))

    assert decision.should_fill is True
    assert decision.gross_difference_yen == 0


def test_missing_buyback_price_fails_closed() -> None:
    decision = decide_price(make_case(), None, quote_error="取得失敗")

    assert decision.should_fill is False
    assert decision.buyback_price_yen is None


def test_purchase_price_override_is_used() -> None:
    decision = decide_price(
        make_case(price_yen=220_000),
        make_quote(210_000),
        purchase_price_override=200_000,
    )

    assert decision.should_fill is True
    assert decision.purchase_price_yen == 200_000


def test_only_current_form_is_open() -> None:
    case = make_case()

    assert is_case_open(case, datetime(2026, 8, 25, 12, 0, tzinfo=JST)) is True
    assert is_case_open(case, datetime(2026, 8, 27, 12, 0, tzinfo=JST)) is False
