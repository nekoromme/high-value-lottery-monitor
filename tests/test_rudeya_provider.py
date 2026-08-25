import pytest

from high_value_lottery_monitor.providers.rudeya import (
    MODEL_JANS,
    RudeyaPriceError,
    parse_rudeya_search_page,
)


def test_exact_jan_price_is_parsed() -> None:
    html = """
    <html><body>
      <h3>新品 RICOH(リコー) GR IV</h3>
      <p>JAN: 4549212311291</p>
      <p>保証書店舗印あり-20000円</p>
      <p>買取価格 230,500円</p>
      <div>完品 買取 ¥230,500</div>
    </body></html>
    """

    assert parse_rudeya_search_page(
        html,
        expected_jan=MODEL_JANS["RICOH GR IV"],
        model_name="RICOH GR IV",
    ) == 230_500


def test_missing_expected_jan_fails_closed() -> None:
    html = "<p>JAN: 4549212311871</p><p>買取価格 267,000円</p>"

    with pytest.raises(RudeyaPriceError, match="JAN"):
        parse_rudeya_search_page(
            html,
            expected_jan=MODEL_JANS["RICOH GR IV"],
            model_name="RICOH GR IV",
        )


def test_multiple_base_prices_fail_closed() -> None:
    html = """
    <p>JAN: 4549212311291</p>
    <p>買取価格 230,500円</p>
    <p>買取価格 210,500円</p>
    """

    with pytest.raises(RudeyaPriceError, match="複数"):
        parse_rudeya_search_page(
            html,
            expected_jan=MODEL_JANS["RICOH GR IV"],
            model_name="RICOH GR IV",
        )
