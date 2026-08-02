"""Acceptance checklist §8: URL construction rules.

Two rules carry real money:

* no ``/`` immediately before ``?`` on review pagination (307 -> page 1),
* the full ``?areaId=`` navigation URL for every one of the 8 macro-regions.
"""

from __future__ import annotations

import pytest

from ozon_anti_bot_engine.config import REGIONS, resolve_region
from ozon_anti_bot_engine.core.requester import (
    OzonUrls,
    has_slash_before_query,
    normalise_path,
)


# =============================================================================
#  The no-trailing-slash rule (spec §4.1)
# =============================================================================
@pytest.mark.parametrize("page", [1, 2, 5, 37, 500])
@pytest.mark.parametrize("sort", ["score_desc", "score_asc", "published_at_desc"])
def test_review_urls_never_have_a_slash_before_the_query(sort: str, page: int) -> None:
    path = OzonUrls.reviews("1234567", sort=sort, page=page)
    assert not has_slash_before_query(path), f"307-triggering path built: {path}"
    assert "/reviews?" in path


def test_review_url_shape_matches_the_spec_example() -> None:
    assert OzonUrls.reviews("1234567", sort="score_asc", page=2) == (
        "/product/1234567/reviews?sort=score_asc&page=2"
    )


def test_page_one_omits_the_page_parameter() -> None:
    # Ozon treats a missing `page` as page 1; sending `page=1` is a needless
    # deviation from what the real client sends.
    assert OzonUrls.reviews("1234567", sort="score_desc", page=1) == (
        "/product/1234567/reviews?sort=score_desc"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/product/1/reviews/?page=2", "/product/1/reviews?page=2"),
        ("/product/1/reviews?page=2", "/product/1/reviews?page=2"),
        ("/product/1/reviews//?page=2", "/product/1/reviews?page=2"),
        ("/product/1/reviews/", "/product/1/reviews/"),  # no query: leave alone
        ("/search/?text=nike", "/search?text=nike"),
        ("/?areaId=2", "/?areaId=2"),                     # bare root must survive
    ],
)
def test_normalise_path(raw: str, expected: str) -> None:
    assert normalise_path(raw) == expected


def test_composer_wrapper_normalises_before_encoding() -> None:
    url = OzonUrls.composer("/product/1/reviews/?page=2")
    # The slash must be gone *before* percent-encoding, not merely encoded.
    assert "reviews%3Fpage%3D2" in url
    assert "reviews%2F%3F" not in url


# =============================================================================
#  Region navigation (spec §4.2)
# =============================================================================
def test_all_eight_macro_regions_are_configured() -> None:
    assert len(REGIONS) == 8
    assert [r.area_id for r in REGIONS] == [2, 3, 14, 16, 58, 67, 79, 2495]


@pytest.mark.parametrize("region", REGIONS, ids=lambda r: r.key)
def test_product_url_carries_the_area_id(region) -> None:
    url = OzonUrls.product_url("1234567", region.area_id)
    assert url == f"https://www.ozon.ru/product/1234567?areaId={region.area_id}"
    assert not has_slash_before_query(url)


def test_regions_resolve_by_key_area_id_and_city() -> None:
    assert resolve_region("moscow").area_id == 2
    assert resolve_region(2495).key == "arkhangelsk"
    assert resolve_region("58").city == "Екатеринбург"
    assert resolve_region("Казань").key == "kazan"
    with pytest.raises(KeyError):
        resolve_region("atlantis")


def test_search_url_is_region_pinned() -> None:
    path = OzonUrls.search("кроссовки", page=3, area_id=67)
    assert path.startswith("/search?")
    assert "areaId=67" in path
    assert "page=3" in path
    assert not has_slash_before_query(path)
