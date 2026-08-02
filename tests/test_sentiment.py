"""Acceptance checklist §8: negative-review clustering and the LLM payload."""

from __future__ import annotations

import pytest

from ozon_anti_bot_engine.models import NegativeCategory, Review, SellerAnswer
from ozon_anti_bot_engine.services.sentiment_ai import (
    SentimentAnalyzer,
    classify,
    normalise,
    score_text,
)


def _review(uuid: str, score: int, comment: str, **kwargs) -> Review:
    return Review(uuid=uuid, sku="1234567", score=score, comment=comment, **kwargs)


# =============================================================================
#  Classification
# =============================================================================
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Товар пришёл сломанным, экран треснул", NegativeCategory.DEFECT_OR_DAMAGE),
        ("Заводской брак, не работает совсем", NegativeCategory.DEFECT_OR_DAMAGE),
        ("Упаковка была мятая и вскрытая", NegativeCategory.PACKAGING),
        ("Коробка помята, без упаковки приехало", NegativeCategory.PACKAGING),
        (
            "Размер не соответствует описанию, цвет другой",
            NegativeCategory.SIZE_OR_DESCRIPTION_MISMATCH,
        ),
        ("Сильно маломерит, не подошло", NegativeCategory.SIZE_OR_DESCRIPTION_MISMATCH),
        (
            "Доставка задержалась на неделю, курьер не привез вовремя",
            NegativeCategory.OZON_LOGISTICS,
        ),
        ("Потеряли заказ на ПВЗ", NegativeCategory.OZON_LOGISTICS),
    ],
)
def test_negative_reasons_are_clustered_into_the_four_spec_labels(
    text: str, expected: NegativeCategory
) -> None:
    category, score, matched = classify(text)
    assert category is expected
    assert score >= 1.5
    assert matched


@pytest.mark.parametrize("text", ["Всё отлично", "", "Спасибо продавцу", "123 456"])
def test_unclassifiable_text_falls_through_to_other(text: str) -> None:
    category, _score, _matched = classify(text)
    assert category is NegativeCategory.OTHER


def test_yo_is_normalised_so_the_lexicon_matches_both_spellings() -> None:
    assert normalise("Плёнка порвана") == "пленка порвана"
    with_yo, _, _ = classify("Плёнка порвана, упаковка вскрыта")
    without_yo, _, _ = classify("Пленка порвана, упаковка вскрыта")
    assert with_yo is without_yo is NegativeCategory.PACKAGING


def test_scoring_returns_evidence_for_every_matching_category() -> None:
    scores = score_text("Упаковка помята и товар сломан")
    assert NegativeCategory.PACKAGING in scores
    assert NegativeCategory.DEFECT_OR_DAMAGE in scores
    _score, terms = scores[NegativeCategory.PACKAGING]
    assert "упаковк" in terms


def test_the_strongest_signal_wins_a_multi_category_review() -> None:
    # Packaging vocabulary is dense here; the defect mention is incidental.
    category, _, _ = classify(
        "Упаковка вскрыта, коробка помята, плёнка порвана. Царапина на корпусе."
    )
    assert category is NegativeCategory.PACKAGING


# =============================================================================
#  Clustering service
# =============================================================================
def test_cluster_counts_shares_and_samples() -> None:
    reviews = [
        _review("a", 1, "Брак, экран треснул"),
        _review("b", 2, "Пришёл сломанным"),
        _review("c", 3, "Упаковка вскрыта и помята"),
        _review("d", 5, "Всё супер, рекомендую"),  # positive: excluded
    ]
    result = SentimentAnalyzer().cluster(reviews, sku="1234567")

    assert result.analysed == 3  # the 5★ review is not analysed
    by_category = {b.category: b for b in result.buckets}
    assert by_category[NegativeCategory.DEFECT_OR_DAMAGE].count == 2
    assert by_category[NegativeCategory.PACKAGING].count == 1
    assert sum(b.share for b in result.buckets) == pytest.approx(1.0)
    assert set(by_category[NegativeCategory.DEFECT_OR_DAMAGE].sample_uuids) == {"a", "b"}


def test_bare_ratings_are_skipped_because_there_is_nothing_to_cluster() -> None:
    result = SentimentAnalyzer().cluster(
        [_review("a", 1, ""), _review("b", 2, "Брак")], sku="1"
    )
    assert result.analysed == 1


def test_negative_only_can_be_disabled() -> None:
    reviews = [_review("a", 5, "Доставка задержалась, но товар хороший")]
    assert SentimentAnalyzer().cluster(reviews, negative_only=True).analysed == 0
    assert SentimentAnalyzer().cluster(reviews, negative_only=False).analysed == 1


def test_empty_input_produces_an_empty_result_not_an_error() -> None:
    result = SentimentAnalyzer().cluster([], sku="1")
    assert result.analysed == 0
    assert result.buckets == []
    assert result.llm_payload is None


# =============================================================================
#  LLM payload
# =============================================================================
def test_llm_payload_carries_categories_playbook_and_schema() -> None:
    reviews = [_review("a", 1, "Брак, не работает", author="Иван Петров")]
    result = SentimentAnalyzer().cluster(reviews, sku="1234567", product_title="Наушники")

    payload = result.llm_payload
    assert payload is not None
    assert payload["language"] == "ru"
    assert payload["product"]["sku"] == "1234567"
    assert payload["product"]["title"] == "Наушники"
    assert set(payload["category_playbook"]) == {c.value for c in NegativeCategory}
    assert payload["response_format"]["schema"]["replies"]

    entry = payload["reviews"][0]
    assert entry["uuid"] == "a"
    assert entry["category"] == NegativeCategory.DEFECT_OR_DAMAGE.value
    # Only the first name travels: replies address the buyer, they don't need an identity.
    assert entry["buyer_first_name"] == "Иван"


def test_already_answered_reviews_are_excluded_from_the_draft_request() -> None:
    reviews = [
        _review("a", 1, "Брак"),
        _review(
            "b", 1, "Тоже брак", seller_answers=[SellerAnswer(text="Уже ответили")]
        ),
    ]
    payload = SentimentAnalyzer().cluster(reviews, sku="1").llm_payload
    assert payload is not None
    assert [r["uuid"] for r in payload["reviews"]] == ["a"]


def test_anonymous_reviewers_contribute_no_name() -> None:
    reviews = [_review("a", 1, "Брак", author="Аноним", is_anonymous=True)]
    payload = SentimentAnalyzer().cluster(reviews, sku="1").llm_payload
    assert payload is not None
    assert payload["reviews"][0]["buyer_first_name"] is None


def test_payload_can_be_switched_off() -> None:
    result = SentimentAnalyzer().cluster(
        [_review("a", 1, "Брак")], sku="1", build_llm_payload=False
    )
    assert result.llm_payload is None


def test_summary_line_is_human_readable() -> None:
    result = SentimentAnalyzer().cluster([_review("a", 1, "Брак, треснул")], sku="1")
    summary = SentimentAnalyzer().summarise(result)
    assert "defect_or_damage" in summary
    assert "1" in summary
