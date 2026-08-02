"""SERP parsing: SKU extraction, ad/organic separation and tile normalisation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ozon_anti_bot_engine.scrapers.search_scraper import (
    extract_serp_items,
    is_advertisement,
    normalise_serp_item,
    sku_from_link,
)

ORGANIC_TILE: dict[str, Any] = {
    "sku": "123456",
    "link": "/product/krossovki-acme-123456/",
    "mainState": [
        {"id": "name", "atom": {"textAtom": {"text": "Кроссовки ACME Runner"}}},
        {"id": "atom-price", "priceV2": {"price": [{"price": "3 499 ₽"}]}},
        {"id": "rating", "atom": {"textAtom": {"text": "4,8 • 1 254 отзыва"}}},
    ],
}

FLAGGED_AD_TILE: dict[str, Any] = {
    "sku": "999001",
    "link": "/product/ad-item-999001/",
    "isAdv": True,
    "mainState": [{"id": "name", "atom": {"textAtom": {"text": "Реклама товара"}}}],
}

TRACKED_AD_TILE: dict[str, Any] = {
    "sku": "999002",
    "link": "/product/promo-999002/",
    "trackingInfo": {"type": "advertising"},
    "mainState": [{"id": "name", "atom": {"textAtom": {"text": "Промо"}}}],
}

LABELLED_AD_TILE: dict[str, Any] = {
    "sku": "999003",
    "link": "/product/labelled-999003/",
    "templateState": [{"id": "badge", "atom": {"textAtom": {"text": "Реклама"}}}],
}


# =============================================================================
#  SKU extraction
# =============================================================================
@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("/product/krossovki-acme-123456/", "123456"),
        ("/product/123456/", "123456"),
        ("https://www.ozon.ru/product/some-slug-987654/?asb=1", "987654"),
        ("/category/shoes/", None),
        ("", None),
    ],
)
def test_sku_from_link(link: str, expected: str | None) -> None:
    assert sku_from_link(link) == expected


# =============================================================================
#  Ad detection (spec §4.3.1)
# =============================================================================
def test_organic_tile_is_not_flagged() -> None:
    assert is_advertisement(ORGANIC_TILE) == (False, None)


@pytest.mark.parametrize(
    "tile", [FLAGGED_AD_TILE, TRACKED_AD_TILE, LABELLED_AD_TILE],
    ids=["boolean-flag", "tracking-info", "rendered-label"],
)
def test_paid_placements_are_detected_across_all_three_markings(tile: dict) -> None:
    is_ad, kind = is_advertisement(tile)
    assert is_ad
    assert kind in ("template", "search_promo")


# =============================================================================
#  Tile normalisation
# =============================================================================
def test_tile_fields_are_extracted() -> None:
    entry = normalise_serp_item(ORGANIC_TILE, position=4, organic_position=3, page=1)
    assert entry is not None
    assert entry.sku == "123456"
    assert entry.title == "Кроссовки ACME Runner"
    assert entry.price == 3499.0
    assert entry.rating == 4.8
    assert entry.review_count == 1254
    assert not entry.is_ad
    assert (entry.position, entry.organic_position) == (4, 3)


def test_sku_falls_back_to_the_link_when_absent() -> None:
    tile = {k: v for k, v in ORGANIC_TILE.items() if k != "sku"}
    entry = normalise_serp_item(tile, 1, 1, 1)
    assert entry is not None and entry.sku == "123456"


def test_tile_without_any_sku_is_dropped() -> None:
    assert normalise_serp_item({"link": "/category/x/"}, 1, 1, 1) is None


def test_ad_entries_carry_no_organic_position() -> None:
    entry = normalise_serp_item(FLAGGED_AD_TILE, position=1, organic_position=None, page=1)
    assert entry is not None
    assert entry.is_ad
    assert entry.organic_position is None


# =============================================================================
#  Payload extraction
# =============================================================================
def test_items_are_extracted_from_the_search_widget() -> None:
    payload = {
        "widgetStates": {
            "searchResultsV2-1-default-1": json.dumps(
                {"items": [ORGANIC_TILE, FLAGGED_AD_TILE]}
            )
        }
    }
    items = extract_serp_items(payload)
    assert [i["sku"] for i in items] == ["123456", "999001"]


def test_unrelated_widgets_are_ignored() -> None:
    payload = {"widgetStates": {"webPrice-1": json.dumps({"price": "1 ₽"})}}
    assert extract_serp_items(payload) == []


def test_empty_payload_yields_nothing() -> None:
    assert extract_serp_items({}) == []


# =============================================================================
#  Position accounting
# =============================================================================
def test_organic_rank_ignores_ad_slots() -> None:
    """A card at absolute slot 4 behind three ads is organic #1, not #4."""
    tiles = [FLAGGED_AD_TILE, TRACKED_AD_TILE, LABELLED_AD_TILE, ORGANIC_TILE]

    absolute = organic = 0
    entries = []
    for tile in tiles:
        absolute += 1
        is_ad, _ = is_advertisement(tile)
        if not is_ad:
            organic += 1
        entries.append(normalise_serp_item(tile, absolute, None if is_ad else organic, 1))

    target = entries[-1]
    assert target is not None
    assert target.position == 4
    assert target.organic_position == 1
