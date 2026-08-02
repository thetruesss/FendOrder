"""Module A — extended review scraper (spec §4.1).

Ozon caps review pagination well below the review count on large cards, so a
single sorted walk can never reach 100%. The multi-pass algorithm attacks the
list from three ends and unions the results by review UUID:

* ``score_desc`` — the positive tail (4–5★),
* ``score_asc``  — the negative head (1–3★), which the default order buries,
* ``published_at_desc`` — whatever landed since the score-ordered passes were
  themselves paginated.

Pagination URLs are built through :func:`OzonUrls.reviews`, which enforces the
no-slash-before-``?`` rule; a ``/reviews/?page=2`` request 307s back to page 1
and silently poisons the whole run.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ..config import Settings, get_settings
from ..core.requester import (
    HybridRequester,
    OzonUrls,
    RequesterError,
    find_key,
    parse_widget_states,
)
from ..models import (
    MediaAsset,
    Review,
    ReviewPassStats,
    ReviewScrapeResult,
    ReviewSort,
    SellerAnswer,
)

logger = logging.getLogger(__name__)

# Widget prefixes that carry review payloads.
_REVIEW_WIDGETS = ("webListReviews", "webReviewList", "webReviews")
_SCORE_WIDGETS = ("webReviewProductScore", "webSingleProductScore", "webReviewTabs")

# Ozon serves images through size-segmented CDN paths (`/wc50/`, `/wc250/`…).
# Upgrading the segment yields the original-quality asset (spec §4.1.3).
_IMAGE_SIZE_RE = re.compile(r"/wc\d+/")
_ORIGINAL_SIZE = "/wc1000/"

_DEFAULT_PASSES: tuple[tuple[ReviewSort, str], ...] = (
    (ReviewSort.SCORE_DESC, "positive"),
    (ReviewSort.SCORE_ASC, "negative"),
    (ReviewSort.DATE_DESC, "fresh"),
)


# =============================================================================
#  Field extraction helpers
# =============================================================================
def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        return int(digits) if digits else None
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "value", "title", "content"):
            if isinstance(value.get(key), str):
                return str(value[key]).strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value if _as_text(v))
    return ""


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        # Ozon mixes seconds and milliseconds depending on the widget.
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str) and value:
        candidate = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def upgrade_media_url(url: str) -> str:
    """Rewrite a CDN thumbnail path to its original-quality equivalent."""
    if not url:
        return url
    if _IMAGE_SIZE_RE.search(url):
        return _IMAGE_SIZE_RE.sub(_ORIGINAL_SIZE, url)
    return url


def _collect_media(raw: Mapping[str, Any]) -> list[MediaAsset]:
    assets: list[MediaAsset] = []
    seen: set[str] = set()

    def add(entry: Any, kind: str) -> None:
        if isinstance(entry, str):
            url, preview, width, height = entry, None, None, None
        elif isinstance(entry, Mapping):
            url = (
                entry.get("url")
                or entry.get("link")
                or entry.get("src")
                or entry.get("originalUrl")
                or entry.get("videoUrl")
                or ""
            )
            preview = entry.get("previewUrl") or entry.get("preview") or entry.get("thumbnail")
            width = _as_int(entry.get("width"))
            height = _as_int(entry.get("height"))
        else:
            return
        if not isinstance(url, str) or not url or url in seen:
            return
        seen.add(url)
        assets.append(
            MediaAsset(
                kind=kind,
                url=upgrade_media_url(url),
                preview_url=upgrade_media_url(preview) if isinstance(preview, str) else None,
                width=width,
                height=height,
            )
        )

    containers: list[tuple[str, Any]] = []
    for key in ("photos", "images", "itemImages", "photosV2"):
        containers.append(("photo", raw.get(key)))
    for key in ("videos", "itemVideos", "videosV2"):
        containers.append(("video", raw.get(key)))

    content = raw.get("content")
    if isinstance(content, Mapping):
        for key in ("photos", "images"):
            containers.append(("photo", content.get(key)))
        for key in ("videos",):
            containers.append(("video", content.get(key)))

    for kind, container in containers:
        if isinstance(container, (list, tuple)):
            for entry in container:
                add(entry, kind)
        elif container:
            add(container, kind)
    return assets


def _collect_seller_answers(raw: Mapping[str, Any]) -> list[SellerAnswer]:
    answers: list[SellerAnswer] = []
    for key in ("comments", "answers", "sellerComments", "commentsList"):
        container = raw.get(key)
        if not isinstance(container, (list, tuple)):
            continue
        for entry in container:
            if not isinstance(entry, Mapping):
                continue
            text = _as_text(entry.get("text") or entry.get("content") or entry.get("comment"))
            if not text:
                continue
            author = entry.get("author")
            author_name = (
                _as_text(author.get("title") or author.get("name"))
                if isinstance(author, Mapping)
                else _as_text(author)
            )
            answers.append(
                SellerAnswer(
                    author=author_name or None,
                    text=text,
                    published_at=_parse_datetime(
                        entry.get("publishedAt") or entry.get("createdAt")
                    ),
                )
            )
    return answers


def _extract_points_for_review(raw: Mapping[str, Any]) -> int | None:
    """«Баллы за отзыв» — the bonus Ozon paid the buyer for writing this review."""
    for key in ("pointsForReview", "points", "bonusAmount", "reviewPoints"):
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, Mapping):
            for inner in ("amount", "value", "points", "count"):
                found = _as_int(value.get(inner))
                if found is not None:
                    return found
            continue
        found = _as_int(value)
        if found is not None:
            return found
    return None


def _looks_like_review(entry: Any) -> bool:
    if not isinstance(entry, Mapping):
        return False
    has_id = any(k in entry for k in ("uuid", "reviewUuid", "id", "reviewId"))
    has_body = any(
        k in entry for k in ("content", "score", "rating", "text", "comment", "positive")
    )
    return has_id and has_body


def extract_review_dicts(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pull raw review objects out of a Composer response, tolerating layout churn."""
    states = parse_widget_states(payload)
    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def harvest(container: Any) -> None:
        if not isinstance(container, (list, tuple)):
            return
        for entry in container:
            if not _looks_like_review(entry):
                continue
            identifier = str(
                entry.get("uuid") or entry.get("reviewUuid") or entry.get("id") or ""
            )
            if identifier and identifier in seen_ids:
                continue
            if identifier:
                seen_ids.add(identifier)
            collected.append(dict(entry))

    # Preferred path: the known review widgets.
    for key, state in states.items():
        widget = key.split("-", 1)[0]
        if widget in _REVIEW_WIDGETS or any(key.startswith(p) for p in _REVIEW_WIDGETS):
            for field in ("reviews", "items", "list", "reviewsList"):
                harvest(_get(state, field))

    # Fallback: any widget carrying a review-shaped list.
    if not collected:
        for state in states.values():
            for field in ("reviews", "items"):
                harvest(_get(state, field))

    # Last resort: breadth-first hunt anywhere in the payload.
    if not collected:
        harvest(find_key(states, "reviews", "reviewsList"))
    return collected


def _get(state: Any, field: str) -> Any:
    return state.get(field) if isinstance(state, Mapping) else None


def extract_total_reviews(payload: Mapping[str, Any]) -> int | None:
    """Read the card's own review count, used to size the pagination depth."""
    states = parse_widget_states(payload)
    for key, state in states.items():
        widget = key.split("-", 1)[0]
        if widget not in _SCORE_WIDGETS and not any(
            key.startswith(p) for p in _SCORE_WIDGETS + _REVIEW_WIDGETS
        ):
            continue
        for field in ("totalReviewsCount", "reviewsCount", "totalCount", "count", "total"):
            value = _as_int(_get(state, field))
            if value:
                return value
    fallback = find_key(
        states, "totalReviewsCount", "reviewsCount", "totalCount", "totalReviews"
    )
    return _as_int(fallback)


def normalise_review(
    raw: Mapping[str, Any], sku: str, discovered_via: str
) -> Review | None:
    """Map one raw Ozon review object onto the :class:`Review` schema."""
    uuid = str(
        raw.get("uuid") or raw.get("reviewUuid") or raw.get("id") or raw.get("reviewId") or ""
    ).strip()
    if not uuid:
        return None

    score = _as_int(raw.get("score") or raw.get("rating") or raw.get("stars"))
    if score is None or not 1 <= score <= 5:
        return None

    content = raw.get("content") if isinstance(raw.get("content"), Mapping) else {}
    dignity = _as_text(
        content.get("positive")
        or raw.get("positive")
        or raw.get("advantages")
        or raw.get("dignity")
    )
    shortcoming = _as_text(
        content.get("negative")
        or raw.get("negative")
        or raw.get("disadvantages")
        or raw.get("shortcoming")
    )
    comment = _as_text(
        content.get("comment") or raw.get("comment") or raw.get("text") or content.get("text")
    )

    author_raw = raw.get("author")
    if isinstance(author_raw, Mapping):
        author = _as_text(
            author_raw.get("title")
            or author_raw.get("name")
            or " ".join(
                filter(None, [
                    str(author_raw.get("firstName") or ""),
                    str(author_raw.get("lastName") or ""),
                ])
            )
        )
    else:
        author = _as_text(author_raw)

    usefulness = raw.get("usefulness") if isinstance(raw.get("usefulness"), Mapping) else {}
    useful = _as_int(usefulness.get("useful") or raw.get("usefulCount") or raw.get("likes")) or 0
    unuseful = (
        _as_int(usefulness.get("unuseful") or raw.get("unusefulCount") or raw.get("dislikes")) or 0
    )

    item = raw.get("item") if isinstance(raw.get("item"), Mapping) else {}
    variant = _as_text(item.get("title") or raw.get("itemTitle") or raw.get("variant")) or None

    return Review(
        uuid=uuid,
        sku=sku,
        score=score,
        author=author or None,
        published_at=_parse_datetime(
            raw.get("publishedAt") or raw.get("createdAt") or raw.get("publishedAtRaw")
        ),
        dignity=dignity,
        shortcoming=shortcoming,
        comment=comment,
        media=_collect_media(raw),
        seller_answers=_collect_seller_answers(raw),
        points_for_review=_extract_points_for_review(raw),
        useful_count=useful,
        unuseful_count=unuseful,
        item_variant=variant,
        is_anonymous=bool(raw.get("isAnonymous") or raw.get("anonymous")),
        discovered_via=discovered_via,
    )


# =============================================================================
#  Scraper
# =============================================================================
class ReviewScraper:
    """Multi-pass review collector with adaptive pagination depth."""

    def __init__(
        self, requester: HybridRequester, settings: Settings | None = None
    ) -> None:
        self.requester = requester
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    async def scrape(
        self,
        sku: str | int,
        *,
        max_pages: int | None = None,
        sorts: Sequence[tuple[ReviewSort, str]] | None = None,
        area_id: int | None = None,
        with_text_only: bool = False,
        session_name: str | None = None,
    ) -> ReviewScrapeResult:
        """Collect reviews for ``sku`` across every configured pass."""
        sku = str(sku)
        started = time.monotonic()
        session_name = session_name or f"reviews-{sku}"
        passes = tuple(sorts) if sorts else _DEFAULT_PASSES
        ceiling = max_pages or self.settings.review_max_pages

        # Warm the session on the product card itself: the reviews endpoint
        # expects a session that has already touched the PDP.
        await self.requester.bootstrap(
            sku, area_id=area_id, session_name=session_name
        )

        collected: dict[str, Review] = {}
        pass_stats: list[ReviewPassStats] = []
        total_reported: int | None = None

        for sort, label in passes:
            stats, reported = await self._run_pass(
                sku,
                sort=sort,
                label=label,
                ceiling=ceiling,
                area_id=area_id,
                session_name=session_name,
                collected=collected,
                total_reported=total_reported,
            )
            total_reported = total_reported or reported
            pass_stats.append(stats)

            if total_reported and self._coverage_reached(len(collected), total_reported):
                logger.info(
                    "coverage target reached after %s pass (%d/%d)",
                    label, len(collected), total_reported,
                )
                break

        reviews = list(collected.values())
        if with_text_only:
            reviews = [r for r in reviews if r.has_text]

        with_text = sum(1 for r in reviews if r.has_text)
        return ReviewScrapeResult(
            sku=sku,
            total_reported=total_reported,
            total_collected=len(reviews),
            with_text=with_text,
            bare_ratings=len(reviews) - with_text,
            positive=sum(1 for r in reviews if r.score >= 4),
            negative=sum(1 for r in reviews if r.score <= 3),
            passes=pass_stats,
            reviews=reviews,
            duration_seconds=round(time.monotonic() - started, 2),
        )

    # ------------------------------------------------------------------
    async def _run_pass(
        self,
        sku: str,
        *,
        sort: ReviewSort,
        label: str,
        ceiling: int,
        area_id: int | None,
        session_name: str,
        collected: dict[str, Review],
        total_reported: int | None,
    ) -> tuple[ReviewPassStats, int | None]:
        stats = ReviewPassStats(sort=sort)
        before = len(collected)
        empty_streak = 0
        no_new_streak = 0
        depth = ceiling
        reported = total_reported

        for page in range(1, ceiling + 1):
            path = OzonUrls.reviews(sku, sort=sort.value, page=page, area_id=area_id)
            try:
                payload = await self.requester.get_json(
                    path,
                    referer=OzonUrls.product_url(sku, area_id),
                    page_type="reviews",
                    session_name=session_name,
                )
            except RequesterError as exc:
                stats.stopped_reason = f"request failed on page {page}: {exc}"
                logger.warning("%s pass stopped: %s", label, exc)
                break

            stats.pages_fetched += 1

            if reported is None:
                reported = extract_total_reviews(payload)
                if reported:
                    depth = self._adaptive_depth(reported, ceiling)
                    logger.info(
                        "card reports %d reviews -> pagination depth %d (cap %d)",
                        reported, depth, ceiling,
                    )

            raw_reviews = extract_review_dicts(payload)
            if not raw_reviews:
                empty_streak += 1
                # Two empty pages in a row is the end of the list; one can be a
                # transient widget miss on an otherwise valid page.
                if empty_streak >= 2:
                    stats.stopped_reason = f"two empty pages at {page}"
                    break
                continue
            empty_streak = 0

            new_on_page = 0
            for raw in raw_reviews:
                review = normalise_review(raw, sku, label)
                if review is None:
                    continue
                stats.reviews_seen += 1
                if review.uuid not in collected:
                    collected[review.uuid] = review
                    new_on_page += 1

            if new_on_page == 0:
                no_new_streak += 1
                # Ozon repeats the last page indefinitely past the real end, so
                # "no new UUIDs" is the reliable terminator, not an empty body.
                if no_new_streak >= 3:
                    stats.stopped_reason = f"no new reviews for 3 pages at {page}"
                    break
            else:
                no_new_streak = 0

            if page >= depth:
                stats.stopped_reason = f"reached adaptive depth {depth}"
                break

            if reported and self._coverage_reached(len(collected), reported):
                stats.stopped_reason = "coverage target reached"
                break
        else:
            stats.stopped_reason = f"reached page ceiling {ceiling}"

        stats.new_reviews = len(collected) - before
        logger.info(
            "%s pass (%s): %d pages, %d new reviews (%s)",
            label, sort.value, stats.pages_fetched, stats.new_reviews, stats.stopped_reason,
        )
        return stats, reported

    # ------------------------------------------------------------------
    def _adaptive_depth(self, total_reviews: int, ceiling: int) -> int:
        """Pages to walk per pass, sized from the card's own review count.

        Each pass only needs to cover roughly its half of the distribution, so
        we take 60% of the full depth per pass and let the union close the gap —
        that is cheaper than three full walks and loses nothing after dedup.
        """
        page_size = max(1, self.settings.review_page_size)
        full_depth = math.ceil(total_reviews / page_size)
        per_pass = math.ceil(full_depth * 0.6) + 2
        return max(1, min(per_pass, ceiling))

    def _coverage_reached(self, collected: int, total: int) -> bool:
        if not total:
            return False
        return collected >= total * self.settings.review_target_coverage

    # ------------------------------------------------------------------
    @staticmethod
    def split_by_text(reviews: Iterable[Review]) -> tuple[list[Review], list[Review]]:
        """Split written reviews from bare star ratings (spec §4.1.3)."""
        written: list[Review] = []
        bare: list[Review] = []
        for review in reviews:
            (written if review.has_text else bare).append(review)
        return written, bare

    @staticmethod
    def split_by_sentiment(reviews: Iterable[Review]) -> tuple[list[Review], list[Review]]:
        """Split 4–5★ from 1–3★."""
        positive: list[Review] = []
        negative: list[Review] = []
        for review in reviews:
            (negative if review.is_negative else positive).append(review)
        return positive, negative


async def scrape_reviews(
    sku: str | int,
    *,
    requester: HybridRequester | None = None,
    **kwargs: Any,
) -> ReviewScrapeResult:
    """Convenience wrapper that owns the requester lifecycle when none is given."""
    if requester is not None:
        return await ReviewScraper(requester).scrape(sku, **kwargs)
    async with HybridRequester() as owned:
        return await ReviewScraper(owned).scrape(sku, **kwargs)
