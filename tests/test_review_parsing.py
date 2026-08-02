"""Acceptance checklist §8: review extraction — media, points, seller answers, splits."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ozon_anti_bot_engine.models import Review
from ozon_anti_bot_engine.scrapers.review_scraper import (
    ReviewScraper,
    extract_review_dicts,
    extract_total_reviews,
    normalise_review,
    upgrade_media_url,
)

RAW_REVIEW: dict[str, Any] = {
    "uuid": "9f1c-aaa",
    "score": 2,
    "content": {
        "positive": "Приехал быстро",
        "negative": "Экран треснул при вскрытии",
        "comment": "Прошу заменить товар",
    },
    "author": {"firstName": "Иван", "lastName": "П."},
    "publishedAt": "2026-01-15T10:00:00Z",
    "photos": [
        {"url": "https://cdn1.ozone.ru/s3/multimedia-a/wc50/6012.jpg", "width": 50},
        {"url": "https://cdn1.ozone.ru/s3/multimedia-b/wc250/6013.jpg"},
    ],
    "videos": [{"url": "https://video.ozone.ru/vod/wc100/clip.mp4"}],
    "comments": [
        {
            "text": "Извините за доставленные неудобства, оформим замену.",
            "author": {"title": "Продавец"},
            "publishedAt": "2026-01-16T09:30:00Z",
        }
    ],
    "pointsForReview": {"amount": 150},
    "usefulness": {"useful": 7, "unuseful": 1},
    "isAnonymous": False,
}

BARE_RATING: dict[str, Any] = {"uuid": "bare-1", "score": 5, "content": {}}


def make_payload(reviews: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    """Build a Composer response — note widget states are JSON *strings*."""
    states = {"webListReviews-3311601-default-1": json.dumps({"reviews": reviews})}
    if total is not None:
        states["webReviewProductScore-3311601-default-1"] = json.dumps(
            {"totalReviewsCount": total}
        )
    return {"widgetStates": states, "layout": []}


# =============================================================================
#  Extraction
# =============================================================================
def test_reviews_are_extracted_from_stringified_widget_states() -> None:
    payload = make_payload([RAW_REVIEW, BARE_RATING], total=1500)
    raw = extract_review_dicts(payload)
    assert [r["uuid"] for r in raw] == ["9f1c-aaa", "bare-1"]


def test_total_review_count_drives_adaptive_depth() -> None:
    assert extract_total_reviews(make_payload([RAW_REVIEW], total=12_345)) == 12_345
    assert extract_total_reviews(make_payload([RAW_REVIEW])) is None


def test_empty_payload_yields_no_reviews() -> None:
    assert extract_review_dicts({"widgetStates": {}}) == []
    assert extract_review_dicts({}) == []


# =============================================================================
#  Normalisation
# =============================================================================
def test_review_fields_are_split_into_dignity_shortcoming_comment() -> None:
    review = normalise_review(RAW_REVIEW, "1234567", "negative")
    assert review is not None
    assert review.dignity == "Приехал быстро"
    assert review.shortcoming == "Экран треснул при вскрытии"
    assert review.comment == "Прошу заменить товар"
    assert review.has_text
    assert review.is_negative
    assert review.author == "Иван П."
    assert review.discovered_via == "negative"


def test_media_is_extracted_at_original_quality() -> None:
    review = normalise_review(RAW_REVIEW, "1234567", "negative")
    assert review is not None
    photos = [m for m in review.media if m.kind == "photo"]
    videos = [m for m in review.media if m.kind == "video"]
    assert len(photos) == 2
    assert len(videos) == 1
    # Thumbnail size segments must be upgraded, not passed through.
    assert all("/wc1000/" in m.url for m in review.media)
    assert not any("/wc50/" in m.url or "/wc250/" in m.url for m in review.media)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://cdn.ozone.ru/s3/x/wc50/a.jpg", "https://cdn.ozone.ru/s3/x/wc1000/a.jpg"),
        ("https://cdn.ozone.ru/s3/x/a.jpg", "https://cdn.ozone.ru/s3/x/a.jpg"),
        ("", ""),
    ],
)
def test_upgrade_media_url(raw: str, expected: str) -> None:
    assert upgrade_media_url(raw) == expected


def test_seller_answers_are_captured() -> None:
    review = normalise_review(RAW_REVIEW, "1234567", "negative")
    assert review is not None
    assert len(review.seller_answers) == 1
    assert review.seller_answers[0].author == "Продавец"
    assert "замену" in review.seller_answers[0].text


def test_points_for_review_is_extracted_from_nested_and_flat_shapes() -> None:
    nested = normalise_review(RAW_REVIEW, "1", "p")
    assert nested is not None and nested.points_for_review == 150

    flat = normalise_review({**RAW_REVIEW, "pointsForReview": 90}, "1", "p")
    assert flat is not None and flat.points_for_review == 90

    absent = normalise_review({**RAW_REVIEW, "pointsForReview": None}, "1", "p")
    assert absent is not None and absent.points_for_review is None


def test_usefulness_counters_are_read() -> None:
    review = normalise_review(RAW_REVIEW, "1", "p")
    assert review is not None
    assert (review.useful_count, review.unuseful_count) == (7, 1)


@pytest.mark.parametrize(
    "broken",
    [
        {"score": 4, "content": {}},                       # no id
        {"uuid": "x", "content": {}},                      # no score
        {"uuid": "x", "score": 0, "content": {}},          # score out of range
        {"uuid": "x", "score": 9, "content": {}},          # score out of range
    ],
)
def test_malformed_reviews_are_dropped_not_raised(broken: dict[str, Any]) -> None:
    assert normalise_review(broken, "1", "p") is None


# =============================================================================
#  Splits (spec §4.1.3)
# =============================================================================
def _review(uuid: str, score: int, text: str = "") -> Review:
    return Review(uuid=uuid, sku="1", score=score, comment=text)


def test_written_reviews_are_separated_from_bare_ratings() -> None:
    reviews = [
        _review("a", 5, "Отличный товар"),
        _review("b", 5),
        _review("c", 1, "Ужас"),
    ]
    written, bare = ReviewScraper.split_by_text(reviews)
    assert [r.uuid for r in written] == ["a", "c"]
    assert [r.uuid for r in bare] == ["b"]


def test_positive_and_negative_split_at_three_stars() -> None:
    reviews = [_review(str(score), score) for score in (1, 2, 3, 4, 5)]
    positive, negative = ReviewScraper.split_by_sentiment(reviews)
    assert [r.score for r in positive] == [4, 5]
    assert [r.score for r in negative] == [1, 2, 3]


def test_bare_rating_has_no_text() -> None:
    review = normalise_review(BARE_RATING, "1", "p")
    assert review is not None
    assert not review.has_text
    assert review.text == ""
