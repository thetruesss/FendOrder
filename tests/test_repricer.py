"""Acceptance checklist §8: repricing strategies and the unit-economics floor."""

from __future__ import annotations

from ozon_anti_bot_engine.models import (
    CompetitorOffer,
    RepriceRequest,
    RepricerStrategy,
)
from ozon_anti_bot_engine.services.repricer import Repricer

COMPETITORS = [
    CompetitorOffer(sku="111", price=100.0, seller="A"),
    CompetitorOffer(sku="222", price=120.0, seller="B"),
    CompetitorOffer(sku="333", price=150.0, seller="C"),
]


def _request(**overrides) -> RepriceRequest:
    payload = {
        "sku": "999",
        "current_price": 130.0,
        "min_allowed_price": 80.0,
        "competitors": COMPETITORS,
    }
    payload.update(overrides)
    return RepriceRequest(**payload)


# =============================================================================
#  Strategies
# =============================================================================
def test_undercut_lowest_sets_price_below_the_cheapest_rival() -> None:
    decision = Repricer().calculate(_request(strategy=RepricerStrategy.UNDERCUT_LOWEST))
    assert decision.market_min == 100.0
    assert decision.applied_price == 99.0
    assert decision.delta_rub == -31.0
    assert decision.should_update


def test_match_lowest_equalises_with_the_cheapest_rival() -> None:
    decision = Repricer().calculate(_request(strategy=RepricerStrategy.MATCH_LOWEST))
    assert decision.applied_price == 100.0


def test_match_average_uses_the_median_of_the_niche() -> None:
    decision = Repricer().calculate(_request(strategy=RepricerStrategy.MATCH_AVERAGE))
    assert decision.market_median == 120.0
    assert decision.applied_price == 120.0


def test_undercut_amount_is_configurable() -> None:
    decision = Repricer().calculate(_request(undercut_by=5.0))
    assert decision.applied_price == 95.0


# =============================================================================
#  The floor (spec §5.2.2)
# =============================================================================
def test_price_never_drops_below_the_unit_economics_floor() -> None:
    decision = Repricer().calculate(_request(min_allowed_price=105.0))
    assert decision.target_price == 99.0     # what the strategy wanted
    assert decision.applied_price == 105.0   # what the floor allows
    assert decision.floor_hit
    assert "floor" in decision.reason


def test_floor_holds_even_when_every_rival_is_below_cost() -> None:
    decision = Repricer().calculate(
        _request(
            competitors=[CompetitorOffer(sku="111", price=10.0)],
            min_allowed_price=95.0,
        )
    )
    assert decision.applied_price == 95.0
    assert decision.floor_hit


def test_ceiling_is_respected_when_configured() -> None:
    decision = Repricer().calculate(
        _request(
            strategy=RepricerStrategy.MATCH_AVERAGE,
            max_allowed_price=110.0,
            current_price=90.0,
        )
    )
    assert decision.applied_price == 110.0
    assert decision.ceiling_hit


# =============================================================================
#  Competitor eligibility
# =============================================================================
def test_out_of_stock_rivals_are_ignored_by_default() -> None:
    competitors = [
        CompetitorOffer(sku="111", price=50.0, in_stock=False),
        CompetitorOffer(sku="222", price=120.0, in_stock=True),
    ]
    decision = Repricer().calculate(_request(competitors=competitors))
    assert decision.competitors_considered == 1
    assert decision.market_min == 120.0


def test_out_of_stock_rivals_can_be_opted_in() -> None:
    competitors = [
        CompetitorOffer(sku="111", price=50.0, in_stock=False),
        CompetitorOffer(sku="222", price=120.0, in_stock=True),
    ]
    decision = Repricer().calculate(
        _request(competitors=competitors, include_out_of_stock=True)
    )
    assert decision.competitors_considered == 2
    assert decision.market_min == 50.0


def test_own_listing_is_excluded_from_the_competitor_set() -> None:
    decision = Repricer().calculate(
        _request(competitors=[CompetitorOffer(sku="999", price=10.0)])
    )
    assert decision.competitors_considered == 0
    assert not decision.should_update
    assert "no eligible competitor" in decision.reason


def test_no_competitors_holds_the_price() -> None:
    decision = Repricer().calculate(_request(competitors=[]))
    assert decision.applied_price == decision.current_price
    assert not decision.should_update
    assert decision.seller_api_payload is None


# =============================================================================
#  Rounding
# =============================================================================
def test_undercut_rounds_down_so_it_cannot_land_above_the_rival() -> None:
    decision = Repricer().calculate(_request(round_to=10.0))
    # 100 - 1 = 99 -> rounding *up* would give 100 and defeat the undercut.
    assert decision.applied_price == 90.0


def test_other_strategies_round_to_nearest() -> None:
    decision = Repricer().calculate(
        _request(strategy=RepricerStrategy.MATCH_AVERAGE, round_to=25.0)
    )
    assert decision.applied_price == 125.0


# =============================================================================
#  Seller API payload
# =============================================================================
def test_payload_targets_the_price_import_endpoint_with_string_prices() -> None:
    decision = Repricer().calculate(_request(offer_id="ACME-1", old_price=200.0))
    payload = decision.seller_api_payload
    assert payload is not None
    assert payload["endpoint"] == "/v1/product/import/prices"
    assert payload["method"] == "POST"

    item = payload["body"]["prices"][0]
    assert item["offer_id"] == "ACME-1"
    assert item["price"] == "99.00"          # strings, not numbers
    assert item["min_price"] == "80.00"      # floor forwarded to Ozon
    assert item["old_price"] == "200.00"
    assert item["currency_code"] == "RUB"
    assert item["auto_action_enabled"] == "DISABLED"


def test_product_id_is_used_when_no_offer_id_is_supplied() -> None:
    decision = Repricer().calculate(_request())
    item = decision.seller_api_payload["body"]["prices"][0]
    assert item["product_id"] == 999
    assert "offer_id" not in item


def test_stale_old_price_is_cleared_rather_than_rendering_a_negative_discount() -> None:
    decision = Repricer().calculate(_request(old_price=50.0))
    assert decision.seller_api_payload["body"]["prices"][0]["old_price"] == "0"


def test_no_payload_when_the_price_is_already_at_target() -> None:
    decision = Repricer().calculate(_request(current_price=99.0))
    assert decision.applied_price == 99.0
    assert not decision.should_update
    assert decision.seller_api_payload is None
    assert "already at target" in decision.reason


def test_batch_calculation_merges_into_a_single_api_call() -> None:
    repricer = Repricer()
    decisions = repricer.calculate_many(
        [_request(sku="111"), _request(sku="222"), _request(sku="333", current_price=99.0)]
    )
    merged = repricer.merge_payloads(decisions)

    # The third SKU is already at target and must not be pushed.
    assert merged["item_count"] == 2
    assert len(merged["body"]["prices"]) == 2
    assert merged["endpoint"] == "/v1/product/import/prices"
