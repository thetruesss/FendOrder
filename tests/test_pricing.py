"""Acceptance checklist §8: regional price extraction and SPP arithmetic."""

from __future__ import annotations

import json

import pytest

from ozon_anti_bot_engine.config import resolve_region
from ozon_anti_bot_engine.core.requester import BrowserCapture
from ozon_anti_bot_engine.models import RegionalPrice
from ozon_anti_bot_engine.scrapers.price_scraper import (
    PriceScraper,
    _merge_states,
    extract_embedded_state,
    parse_price,
)


# =============================================================================
#  Price parsing
# =============================================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 299 ₽", 1299.0),
        ("1 299 ₽", 1299.0),          # non-breaking space
        ("1 299 ₽", 1299.0),     # narrow + thin space
        ("1 299,50 ₽", 1299.5),
        ("12 345 678 ₽", 12_345_678.0),
        (1299, 1299.0),
        (1299.5, 1299.5),
        ({"price": "999 ₽"}, 999.0),
        ("", None),
        ("—", None),
        (None, None),
        (True, None),
        ("0 ₽", None),
    ],
)
def test_parse_price(raw: object, expected: float | None) -> None:
    assert parse_price(raw) == expected


# =============================================================================
#  SPP arithmetic (spec §4.2)
# =============================================================================
def test_spp_is_the_delta_between_showcase_and_ozon_card_price() -> None:
    price = RegionalPrice(
        region_key="moscow",
        city="Москва",
        macro_region="Центр",
        area_id=2,
        base_price=2500.0,
        showcase_price=1299.0,
        ozon_card_price=1199.0,
    )
    assert price.spp_rub == 100.0
    assert price.spp_percent == pytest.approx(7.7, abs=0.01)
    assert price.seller_discount_percent == pytest.approx(48.04, abs=0.01)


def test_spp_is_none_when_a_component_is_missing() -> None:
    price = RegionalPrice(
        region_key="spb", city="СПб", macro_region="СЗ", area_id=3, showcase_price=1000.0
    )
    assert price.spp_rub is None
    assert price.spp_percent is None
    assert price.seller_discount_percent is None


def test_zero_spp_when_card_price_equals_showcase() -> None:
    price = RegionalPrice(
        region_key="kazan",
        city="Казань",
        macro_region="Поволжье",
        area_id=16,
        showcase_price=1000.0,
        ozon_card_price=1000.0,
    )
    assert price.spp_rub == 0.0
    assert price.spp_percent == 0.0


# =============================================================================
#  Widget extraction
# =============================================================================
def _capture(states: dict[str, str], html: str = "") -> BrowserCapture:
    return BrowserCapture(
        url="https://www.ozon.ru/product/1?areaId=2",
        final_url="https://www.ozon.ru/product/1?areaId=2",
        html=html,
        captured=[{"widgetStates": states}],
    )


PRICE_STATES = {
    "webPrice-3121879-default-1": json.dumps(
        {
            "price": "1 299 ₽",
            "originalPrice": "2 500 ₽",
            "cardPrice": "1 199 ₽",
            "isAvail": True,
        }
    ),
    "webProductHeading-3311601-default-1": json.dumps({"title": "Наушники ACME X1"}),
    "webCurrentSeller-1": json.dumps({"name": "ACME Store"}),
}


def test_regional_price_is_built_from_captured_widget_states() -> None:
    scraper = PriceScraper(requester=None)
    region = resolve_region("moscow")
    states = _merge_states(_capture(PRICE_STATES))

    price = scraper._build_regional_price(region, states, html="")

    assert price.area_id == 2
    assert price.base_price == 2500.0
    assert price.showcase_price == 1299.0
    assert price.ozon_card_price == 1199.0
    assert price.spp_rub == 100.0
    assert price.is_available


def test_missing_card_price_falls_back_to_showcase_so_spp_is_zero_not_null() -> None:
    scraper = PriceScraper(requester=None)
    states = _merge_states(
        _capture({"webPrice-1": json.dumps({"price": "500 ₽", "isAvail": True})})
    )
    price = scraper._build_regional_price(resolve_region("spb"), states, html="")
    assert price.showcase_price == 500.0
    assert price.ozon_card_price == 500.0
    assert price.spp_rub == 0.0


def test_card_with_no_price_is_reported_unavailable() -> None:
    scraper = PriceScraper(requester=None)
    states = _merge_states(_capture({"webPrice-1": json.dumps({"isAvail": True})}))
    price = scraper._build_regional_price(resolve_region("spb"), states, html="")
    assert not price.is_available
    assert price.showcase_price is None


def test_best_price_badge_is_detected_from_widget_key_and_from_html() -> None:
    scraper = PriceScraper(requester=None)
    region = resolve_region("moscow")

    by_widget = _merge_states(
        _capture({**PRICE_STATES, "webBestPrice-1": json.dumps({"shown": True})})
    )
    assert scraper._build_regional_price(region, by_widget, html="").best_price_badge

    plain = _merge_states(_capture(PRICE_STATES))
    assert scraper._build_regional_price(
        region, plain, html="<div>Лучшая цена</div>"
    ).best_price_badge

    assert not scraper._build_regional_price(region, plain, html="<div/>").best_price_badge


def test_title_and_seller_extraction() -> None:
    scraper = PriceScraper(requester=None)
    states = _merge_states(_capture(PRICE_STATES))
    assert scraper._extract_title(states) == "Наушники ACME X1"
    assert scraper._extract_seller(states) == "ACME Store"


# =============================================================================
#  HTML fallback
# =============================================================================
def test_embedded_state_is_recovered_from_pdp_html() -> None:
    payload = {"widgetStates": {"webPrice-1": json.dumps({"price": "777 ₽"})}}
    html = f"<html><body><script>window.__STATE__ = {json.dumps(payload)};</script></body></html>"

    recovered = extract_embedded_state(html)
    assert "widgetStates" in recovered
    assert json.loads(recovered["widgetStates"]["webPrice-1"])["price"] == "777 ₽"


def test_embedded_state_returns_empty_on_unparseable_html() -> None:
    assert extract_embedded_state("<html>no state here</html>") == {}
    assert extract_embedded_state('{"widgetStates": {broken') == {}


def test_html_fallback_is_used_when_no_xhr_was_captured() -> None:
    payload = {"widgetStates": {"webPrice-1": json.dumps({"price": "640 ₽"})}}
    capture = BrowserCapture(
        url="u", final_url="u", html=f"<script>{json.dumps(payload)}</script>", captured=[]
    )
    states = _merge_states(capture)
    price = PriceScraper(requester=None)._build_regional_price(
        resolve_region("moscow"), states, html=""
    )
    assert price.showcase_price == 640.0
