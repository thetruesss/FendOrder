"""AI clustering of negative reviews and LLM auto-reply dataset prep (spec §5.1).

Clustering is lexical, not neural, and that is a deliberate choice: the four
target labels are defined by a small, stable Russian vocabulary, and a rule
engine is auditable, instant, free, and — unlike an embedding model — cannot
quietly reclassify a whole category after a model update. Sellers reading a
«brak vs. logistics» split need to trust the numbers.

Russian morphology is handled by matching on *roots* (``слома`` covers
``сломан``/``сломался``/``сломанный``) rather than by full stemming, which keeps
the rule set legible to whoever tunes it next.

The second half of the module builds the cleaned JSON payload handed to an LLM
(Claude / Gemini / OpenAI) to draft personalised replies. This module only
*prepares* the payload; dispatching it is the caller's decision.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..models import (
    ClusterBucket,
    ClusterHit,
    NegativeCategory,
    Review,
    SentimentClusterResult,
)

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ]+", re.UNICODE)


# =============================================================================
#  Lexicon
# =============================================================================
#  Each entry is (root, weight). Weights let an unambiguous root ("бракован")
#  outvote a generic one ("плохо") when a review hits several categories.
LEXICON: dict[NegativeCategory, tuple[tuple[str, float], ...]] = {
    NegativeCategory.DEFECT_OR_DAMAGE: (
        ("брак", 3.0),
        ("бракован", 3.0),
        ("дефект", 3.0),
        ("слома", 2.5),
        ("полома", 2.5),
        ("треснул", 2.5),
        ("трещин", 2.5),
        ("разбит", 2.5),
        ("скол", 2.0),
        ("царапин", 1.8),
        ("неисправ", 2.5),
        ("работа", 0.6),      # weak on its own; "не работает" is the real signal
        ("отвалил", 2.2),
        ("порван", 2.0),
        ("подделк", 2.5),
        ("некачествен", 2.0),
        ("вмятин", 2.0),
    ),
    NegativeCategory.PACKAGING: (
        ("упаковк", 3.0),
        ("упакова", 2.5),
        ("коробк", 2.2),
        ("помят", 2.5),
        ("мят", 2.0),
        ("вскрыт", 2.5),
        ("распечат", 2.0),
        ("пупырч", 1.5),
        ("пакет", 1.5),
        ("целлофан", 1.5),
        ("плёнк", 1.2),
        ("пленк", 1.2),
        ("грязн", 1.5),
    ),
    NegativeCategory.SIZE_OR_DESCRIPTION_MISMATCH: (
        ("размер", 2.5),
        ("маломер", 3.0),
        ("большемер", 3.0),
        ("не соответств", 3.0),
        ("несоответств", 3.0),
        ("не такой", 2.5),
        ("отличает", 2.2),
        ("описани", 2.0),
        ("цвет", 1.8),
        ("оттенок", 1.8),
        ("на фото", 2.0),
        ("картинк", 1.5),
        ("комплектац", 2.2),
        ("не подош", 2.5),
        ("мал", 1.2),
        ("велик", 1.2),
        ("объём", 1.2),
        ("объем", 1.2),
    ),
    NegativeCategory.OZON_LOGISTICS: (
        ("доставк", 2.5),
        ("достав", 1.8),
        ("задерж", 3.0),
        ("опозда", 2.8),
        ("пвз", 2.5),
        ("пункт выдач", 2.5),
        ("курьер", 2.2),
        ("потеря", 2.5),
        ("не привез", 3.0),
        ("не пришл", 2.5),
        ("долго ждал", 2.5),
        ("срок", 1.5),
        ("перенес", 2.0),
        ("отмени", 1.8),
        ("логистик", 2.5),
        ("склад", 1.2),
    ),
}

# Phrases that flip a weak root into a strong signal.
BIGRAM_BOOSTS: dict[NegativeCategory, tuple[tuple[str, float], ...]] = {
    NegativeCategory.DEFECT_OR_DAMAGE: (
        ("не работает", 3.0),
        ("не включается", 3.0),
        ("пришёл сломанным", 3.5),
        ("пришел сломанным", 3.5),
    ),
    NegativeCategory.PACKAGING: (
        ("без упаковки", 3.0),
        ("упаковка порвана", 3.5),
    ),
    NegativeCategory.SIZE_OR_DESCRIPTION_MISMATCH: (
        ("не соответствует описанию", 3.5),
        ("не тот размер", 3.0),
        ("другой цвет", 3.0),
    ),
    NegativeCategory.OZON_LOGISTICS: (
        ("долгая доставка", 3.0),
        ("потеряли заказ", 3.5),
        ("не доставили", 3.5),
    ),
}

# Below this the review is labelled OTHER rather than forced into a bucket.
MIN_CATEGORY_SCORE = 1.5


def normalise(text: str) -> str:
    return " ".join(_TOKEN_RE.findall(text.lower().replace("ё", "е")))


def _lexicon_root(root: str) -> str:
    return root.replace("ё", "е")


def score_text(text: str) -> dict[NegativeCategory, tuple[float, list[str]]]:
    """Score one review body against every category. Returns score + evidence."""
    haystack = normalise(text)
    scores: dict[NegativeCategory, tuple[float, list[str]]] = {}
    if not haystack:
        return scores

    for category, roots in LEXICON.items():
        total = 0.0
        matched: list[str] = []
        for root, weight in roots:
            if _lexicon_root(root) in haystack:
                total += weight
                matched.append(root)
        for phrase, weight in BIGRAM_BOOSTS.get(category, ()):
            if _lexicon_root(phrase) in haystack:
                total += weight
                matched.append(phrase)
        if total > 0:
            scores[category] = (round(total, 2), matched)
    return scores


def classify(text: str) -> tuple[NegativeCategory, float, list[str]]:
    """Assign the single best-fitting category to a review body."""
    scores = score_text(text)
    if not scores:
        return NegativeCategory.OTHER, 0.0, []
    category, (score, matched) = max(scores.items(), key=lambda kv: kv[1][0])
    if score < MIN_CATEGORY_SCORE:
        return NegativeCategory.OTHER, score, matched
    return category, score, matched


# =============================================================================
#  Clustering service
# =============================================================================
class SentimentAnalyzer:
    """Clusters negative reviews and prepares the LLM auto-reply dataset."""

    def __init__(self, min_score: float = MIN_CATEGORY_SCORE) -> None:
        self.min_score = min_score

    # ------------------------------------------------------------------
    def cluster(
        self,
        reviews: Iterable[Review],
        *,
        sku: str | None = None,
        negative_only: bool = True,
        build_llm_payload: bool = True,
        product_title: str | None = None,
        seller_name: str | None = None,
        max_llm_reviews: int = 50,
    ) -> SentimentClusterResult:
        """Group reviews by negativity reason (spec §5.1.1)."""
        candidates = [
            review
            for review in reviews
            if (review.is_negative or not negative_only) and review.has_text
        ]

        hits: list[ClusterHit] = []
        by_category: dict[NegativeCategory, list[Review]] = {}
        terms_by_category: dict[NegativeCategory, Counter[str]] = {}

        for review in candidates:
            category, score, matched = classify(review.text)
            hits.append(
                ClusterHit(
                    review_uuid=review.uuid,
                    category=category,
                    score=score,
                    matched_terms=matched,
                )
            )
            by_category.setdefault(category, []).append(review)
            terms_by_category.setdefault(category, Counter()).update(matched)

        total = len(candidates)
        buckets = [
            ClusterBucket(
                category=category,
                count=len(bucket_reviews),
                share=round(len(bucket_reviews) / total, 4) if total else 0.0,
                top_terms=[t for t, _ in terms_by_category[category].most_common(8)],
                sample_uuids=[r.uuid for r in bucket_reviews[:5]],
            )
            for category, bucket_reviews in by_category.items()
        ]
        buckets.sort(key=lambda b: b.count, reverse=True)

        result = SentimentClusterResult(sku=sku, analysed=total, buckets=buckets, hits=hits)

        if build_llm_payload and candidates:
            category_by_uuid = {hit.review_uuid: hit.category for hit in hits}
            result.llm_payload = self.build_llm_payload(
                candidates[:max_llm_reviews],
                category_by_uuid,
                sku=sku,
                product_title=product_title,
                seller_name=seller_name,
            )
        return result

    # ------------------------------------------------------------------
    def build_llm_payload(
        self,
        reviews: Sequence[Review],
        category_by_uuid: Mapping[str, NegativeCategory],
        *,
        sku: str | None = None,
        product_title: str | None = None,
        seller_name: str | None = None,
    ) -> dict[str, Any]:
        """Build the cleaned JSON payload for LLM-drafted replies (spec §5.1.2).

        Personal data is deliberately reduced to a first name — replies address
        the buyer, they do not need the buyer's identity — and the payload
        carries the cluster label so the model can pick the right remedy.
        """
        return {
            "task": "generate_review_replies",
            "language": "ru",
            "product": {
                "sku": sku,
                "title": product_title,
                "seller": seller_name,
            },
            "instruction": (
                "Ты — менеджер по работе с клиентами продавца на Ozon. "
                "Для каждого отзыва напиши персональный ответ на русском языке. "
                "Требования: обращайся по имени, если оно указано; признай "
                "конкретную проблему из отзыва, не отвечай шаблонно; предложи "
                "решение, соответствующее категории проблемы; уложись в 400–600 "
                "знаков; не обещай компенсаций, которые продавец не подтвердил; "
                "не упоминай, что ответ сгенерирован автоматически."
            ),
            "category_playbook": {
                NegativeCategory.DEFECT_OR_DAMAGE.value: (
                    "Извинись за брак, предложи возврат или замену через личный "
                    "кабинет Ozon, попроси фото дефекта для разбора с производством."
                ),
                NegativeCategory.PACKAGING.value: (
                    "Извинись за состояние упаковки, объясни, что усилен "
                    "транспортировочный слой, уточни, пострадал ли сам товар."
                ),
                NegativeCategory.SIZE_OR_DESCRIPTION_MISMATCH.value: (
                    "Извинись за несовпадение, уточни, какая характеристика не "
                    "совпала, сообщи, что карточка будет исправлена, предложи "
                    "возврат или подбор верного варианта."
                ),
                NegativeCategory.OZON_LOGISTICS.value: (
                    "Отметь, что доставка на стороне Ozon, вырази сожаление, "
                    "предложи обратиться в поддержку Ozon и подтверди, что товар "
                    "отгружен вовремя."
                ),
                NegativeCategory.OTHER.value: (
                    "Поблагодари за обратную связь, уточни детали проблемы, "
                    "предложи связаться для решения."
                ),
            },
            "reviews": [
                {
                    "uuid": review.uuid,
                    "score": review.score,
                    "buyer_first_name": self._first_name(review),
                    "category": category_by_uuid.get(
                        review.uuid, NegativeCategory.OTHER
                    ).value,
                    "dignity": review.dignity,
                    "shortcoming": review.shortcoming,
                    "comment": review.comment,
                    "has_media": bool(review.media),
                    "media_count": len(review.media),
                    "already_answered": bool(review.seller_answers),
                    "published_at": (
                        review.published_at.isoformat() if review.published_at else None
                    ),
                    "item_variant": review.item_variant,
                }
                for review in reviews
                # A review the seller already answered does not need a draft.
                if not review.seller_answers
            ],
            "response_format": {
                "type": "json",
                "schema": {
                    "replies": [
                        {"uuid": "string", "reply": "string", "tone": "string"}
                    ]
                },
            },
        }

    @staticmethod
    def _first_name(review: Review) -> str | None:
        if review.is_anonymous or not review.author:
            return None
        return review.author.strip().split()[0] or None

    # ------------------------------------------------------------------
    def summarise(self, result: SentimentClusterResult) -> str:
        """One-line human summary, for logs and Telegram digests."""
        if not result.buckets:
            return "Негативных отзывов с текстом не найдено."
        parts = [
            f"{bucket.category.value}: {bucket.count} ({bucket.share:.0%})"
            for bucket in result.buckets
        ]
        return f"Проанализировано {result.analysed} отзывов — " + ", ".join(parts)
