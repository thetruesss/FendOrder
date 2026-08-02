"""Pydantic v2 schemas shared by scrapers, services and the REST layer."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field


# =============================================================================
#  Reviews (spec §4.1)
# =============================================================================
class ReviewSort(StrEnum):
    """Sort orders accepted by the Ozon reviews endpoint."""

    SCORE_DESC = "score_desc"
    SCORE_ASC = "score_asc"
    DATE_DESC = "published_at_desc"
    DATE_ASC = "published_at_asc"
    USEFUL_DESC = "usefulness_desc"


class MediaAsset(BaseModel):
    kind: str = "photo"  # photo | video
    url: str
    preview_url: str | None = None
    width: int | None = None
    height: int | None = None


class SellerAnswer(BaseModel):
    author: str | None = None
    text: str
    published_at: datetime | None = None


class Review(BaseModel):
    """One normalised Ozon review."""

    uuid: str
    sku: str
    score: int = Field(ge=1, le=5)
    author: str | None = None
    published_at: datetime | None = None

    # Ozon splits the body into three named fields.
    dignity: str = ""       # «Достоинства»
    shortcoming: str = ""   # «Недостатки»
    comment: str = ""       # «Комментарий»

    media: list[MediaAsset] = Field(default_factory=list)
    seller_answers: list[SellerAnswer] = Field(default_factory=list)
    points_for_review: int | None = None  # «Баллы за отзыв»

    useful_count: int = 0
    unuseful_count: int = 0
    item_variant: str | None = None
    is_anonymous: bool = False
    discovered_via: str | None = None  # which pass surfaced it first

    @computed_field  # type: ignore[prop-decorator]
    @property
    def text(self) -> str:
        """Concatenated body, used for clustering and coverage accounting."""
        parts = [self.dignity, self.shortcoming, self.comment]
        return "\n".join(p.strip() for p in parts if p and p.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_text(self) -> bool:
        """``True`` for a written review, ``False`` for a bare star rating."""
        return bool(self.text.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_negative(self) -> bool:
        return self.score <= 3


class ReviewPassStats(BaseModel):
    sort: ReviewSort
    pages_fetched: int = 0
    reviews_seen: int = 0
    new_reviews: int = 0
    stopped_reason: str = ""


class ReviewScrapeResult(BaseModel):
    sku: str
    total_reported: int | None = None   # what the card claims it has
    total_collected: int = 0
    with_text: int = 0
    bare_ratings: int = 0
    positive: int = 0                   # 4–5 stars
    negative: int = 0                   # 1–3 stars
    passes: list[ReviewPassStats] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    duration_seconds: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def coverage(self) -> float | None:
        if not self.total_reported:
            return None
        return round(self.total_collected / self.total_reported, 4)


# =============================================================================
#  Pricing (spec §4.2)
# =============================================================================
class RegionalPrice(BaseModel):
    region_key: str
    city: str
    macro_region: str
    area_id: int

    base_price: float | None = None       # struck-through seller price
    showcase_price: float | None = None   # any-bank card price
    ozon_card_price: float | None = None  # «зелёная цена»
    currency: str = "RUB"

    best_price_badge: bool = False        # «Лучшая цена» boost badge
    is_available: bool = True
    delivery_note: str | None = None
    error: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spp_rub(self) -> float | None:
        """Ozon subsidy in roubles: showcase price minus Ozon-card price."""
        if self.showcase_price is None or self.ozon_card_price is None:
            return None
        return round(self.showcase_price - self.ozon_card_price, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spp_percent(self) -> float | None:
        """Ozon subsidy as a share of the showcase price."""
        if not self.showcase_price or self.ozon_card_price is None:
            return None
        return round((self.showcase_price - self.ozon_card_price) / self.showcase_price * 100, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def seller_discount_percent(self) -> float | None:
        """Seller's own discount off the struck-through base price."""
        if not self.base_price or self.showcase_price is None:
            return None
        return round((self.base_price - self.showcase_price) / self.base_price * 100, 2)


class PriceScrapeResult(BaseModel):
    sku: str
    title: str | None = None
    seller: str | None = None
    regions: list[RegionalPrice] = Field(default_factory=list)
    duration_seconds: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price_spread_rub(self) -> float | None:
        """Max−min showcase price across regions: the logistics surcharge band."""
        prices = [r.showcase_price for r in self.regions if r.showcase_price is not None]
        if len(prices) < 2:
            return None
        return round(max(prices) - min(prices), 2)


# =============================================================================
#  Search / SERP (spec §4.3)
# =============================================================================
class SerpEntry(BaseModel):
    position: int          # 1-based position across the whole result stream
    organic_position: int | None = None  # 1-based ignoring ad slots
    sku: str
    title: str | None = None
    price: float | None = None
    rating: float | None = None
    review_count: int | None = None
    is_ad: bool = False
    ad_kind: str | None = None  # "template" (Трафарет) | "search_promo" | None
    page: int = 1


class SerpResult(BaseModel):
    query: str
    region_key: str
    area_id: int
    target_sku: str | None = None
    found_position: int | None = None
    found_organic_position: int | None = None
    found_page: int | None = None
    is_ad_placement: bool = False
    pages_scanned: int = 0
    total_entries: int = 0
    entries: list[SerpEntry] = Field(default_factory=list)


class SkuVariant(BaseModel):
    sku: str
    kind: str | None = None   # size | color | bundle …
    value: str | None = None
    is_current: bool = False
    is_available: bool = True
    price: float | None = None
    review_count: int | None = None


class RichContentResult(BaseModel):
    sku: str
    title: str | None = None
    rich_content_json: dict[str, Any] | None = None
    rich_content_html: str | None = None
    images: list[str] = Field(default_factory=list)
    variants: list[SkuVariant] = Field(default_factory=list)
    best_selling_variant: str | None = None
    characteristics: dict[str, str] = Field(default_factory=dict)


# =============================================================================
#  Stock / velocity (spec §4.4)
# =============================================================================
class WarehouseStock(BaseModel):
    scheme: str            # FBO | FBS | RFBS | CROSSBORDER | UNKNOWN
    available: int | None = None
    warehouse_name: str | None = None
    delivery_days: int | None = None
    is_estimate: bool = True


class StockSnapshot(BaseModel):
    sku: str
    captured_at: datetime
    region_key: str
    total_available: int | None = None
    warehouses: list[WarehouseStock] = Field(default_factory=list)
    probe_quantity: int | None = None
    method: str = "cart_probe"


class SalesVelocity(BaseModel):
    sku: str
    window_hours: float
    units_sold: int
    units_per_day: float
    restock_events: int = 0
    restocked_units: int = 0
    samples: int = 0
    confidence: str = "low"  # low | medium | high


# =============================================================================
#  Sentiment AI (spec §5.1)
# =============================================================================
class NegativeCategory(StrEnum):
    DEFECT_OR_DAMAGE = "defect_or_damage"
    PACKAGING = "packaging"
    SIZE_OR_DESCRIPTION_MISMATCH = "size_or_description_mismatch"
    OZON_LOGISTICS = "ozon_logistics"
    OTHER = "other"


class ClusterHit(BaseModel):
    review_uuid: str
    category: NegativeCategory
    score: float
    matched_terms: list[str] = Field(default_factory=list)


class ClusterBucket(BaseModel):
    category: NegativeCategory
    count: int
    share: float
    top_terms: list[str] = Field(default_factory=list)
    sample_uuids: list[str] = Field(default_factory=list)


class SentimentClusterResult(BaseModel):
    sku: str | None = None
    analysed: int
    buckets: list[ClusterBucket] = Field(default_factory=list)
    hits: list[ClusterHit] = Field(default_factory=list)
    llm_payload: dict[str, Any] | None = None


# =============================================================================
#  Repricer (spec §5.2)
# =============================================================================
class RepricerStrategy(StrEnum):
    UNDERCUT_LOWEST = "undercut_lowest"
    MATCH_LOWEST = "match_lowest"
    MATCH_AVERAGE = "match_average"


class CompetitorOffer(BaseModel):
    sku: str
    price: float
    seller: str | None = None
    in_stock: bool = True


class RepriceRequest(BaseModel):
    sku: str
    offer_id: str | None = None
    current_price: float
    min_allowed_price: float          # unit-economics floor — hard constraint
    max_allowed_price: float | None = None
    old_price: float | None = None    # struck-through price for the payload
    strategy: RepricerStrategy = RepricerStrategy.UNDERCUT_LOWEST
    undercut_by: float = 1.0
    competitors: list[CompetitorOffer] = Field(default_factory=list)
    include_out_of_stock: bool = False
    round_to: float = 1.0


class RepriceDecision(BaseModel):
    sku: str
    strategy: RepricerStrategy
    current_price: float
    target_price: float
    applied_price: float
    delta_rub: float
    delta_percent: float
    floor_hit: bool = False
    ceiling_hit: bool = False
    should_update: bool = False
    reason: str = ""
    competitors_considered: int = 0
    market_min: float | None = None
    market_median: float | None = None
    seller_api_payload: dict[str, Any] | None = None
