"""FastAPI REST endpoints (spec §6).

The scrape endpoints are synchronous: a full review sweep on a 30k-review card
takes minutes, which is longer than most HTTP clients will hold a connection.
For production the same calls are exposed as Celery tasks in
:mod:`ozon_anti_bot_engine.tasks` — submit there and poll, and keep these
endpoints for interactive use and small cards.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from ..config import REGIONS, Settings, get_settings, resolve_region
from ..core.requester import HybridRequester, RequesterError
from ..models import (
    PriceScrapeResult,
    RepriceDecision,
    RepriceRequest,
    Review,
    ReviewScrapeResult,
    RichContentResult,
    SalesVelocity,
    SentimentClusterResult,
    SerpResult,
    StockSnapshot,
)
from ..scrapers import PriceScraper, ReviewScraper, SearchScraper, StockScraper
from ..services import Repricer, SentimentAnalyzer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["ozon"])


# =============================================================================
#  Dependencies
# =============================================================================
def get_requester(request: Request) -> HybridRequester:
    requester: HybridRequester | None = getattr(request.app.state, "requester", None)
    if requester is None:
        raise HTTPException(status_code=503, detail="Engine is not initialised")
    return requester


async def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """Shared-secret gate. Disabled when ``OZON_API_KEY`` is empty."""
    if not settings.api_key:
        return
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


Guarded = Depends(require_api_key)


# =============================================================================
#  Request bodies
# =============================================================================
class SentimentRequest(BaseModel):
    """Either pass reviews inline, or a SKU to scrape them first."""

    sku: str | None = None
    reviews: list[Review] | None = None
    negative_only: bool = True
    build_llm_payload: bool = True
    product_title: str | None = None
    seller_name: str | None = None
    max_llm_reviews: int = Field(default=50, ge=1, le=500)
    scrape_max_pages: int = Field(default=40, ge=1, le=500)


class RepricerRequest(BaseModel):
    requests: list[RepriceRequest] = Field(min_length=1)
    merge_payloads: bool = False


# =============================================================================
#  Health
# =============================================================================
@router.get("/health", summary="System and proxy-bridge health")
async def health(request: Request) -> dict[str, Any]:
    requester: HybridRequester | None = getattr(request.app.state, "requester", None)
    settings = get_settings()

    if requester is None:
        return {
            "status": "starting",
            "environment": settings.environment,
            "engine": None,
        }

    stats = requester.stats()
    bridge_ok = stats["bridge"]["running"] or not settings.has_proxies
    proxies_ok = requester.pool.is_empty or requester.pool.available_count > 0
    return {
        "status": "ok" if bridge_ok and proxies_ok else "degraded",
        "environment": settings.environment,
        "regions_tracked": len(REGIONS),
        "engine": stats,
    }


@router.get("/health/proxies", summary="Actively probe every proxy in the pool")
async def health_proxies(
    requester: Annotated[HybridRequester, Depends(get_requester)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Guarded] = None,
) -> dict[str, Any]:
    if requester.pool.is_empty:
        return {"configured": False, "results": {}}
    results = await requester.pool.health_check(
        settings.proxy_healthcheck_url,
        timeout=settings.proxy_healthcheck_timeout,
        impersonate=requester.profile.impersonate,
    )
    return {"configured": True, "results": results, "pool": requester.pool.stats()}


# =============================================================================
#  Module A — reviews
# =============================================================================
@router.get(
    "/reviews/{sku}",
    response_model=ReviewScrapeResult,
    summary="Multi-pass review scrape",
)
async def get_reviews(
    requester: Annotated[HybridRequester, Depends(get_requester)],
    sku: Annotated[str, Path(pattern=r"^\d+$")],
    max_pages: Annotated[int | None, Query(ge=1, le=1000)] = None,
    region: Annotated[str, Query()] = "moscow",
    with_text_only: Annotated[bool, Query()] = False,
    include_reviews: Annotated[bool, Query(description="Return bodies, not just stats")] = True,
    _: Annotated[None, Guarded] = None,
) -> ReviewScrapeResult:
    area_id = _area_id(region)
    try:
        result = await ReviewScraper(requester).scrape(
            sku, max_pages=max_pages, area_id=area_id, with_text_only=with_text_only
        )
    except RequesterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not include_reviews:
        result.reviews = []
    return result


# =============================================================================
#  Module B — pricing
# =============================================================================
@router.get(
    "/pricing/{sku}",
    response_model=PriceScrapeResult,
    summary="Regional prices, Ozon-card price and SPP across 8 macro-regions",
)
async def get_pricing(
    requester: Annotated[HybridRequester, Depends(get_requester)],
    sku: Annotated[str, Path(pattern=r"^\d+$")],
    regions: Annotated[
        str | None,
        Query(description="Comma-separated region keys/areaIds; default = all 8"),
    ] = None,
    concurrency: Annotated[int, Query(ge=1, le=8)] = 2,
    _: Annotated[None, Guarded] = None,
) -> PriceScrapeResult:
    selected = None
    if regions:
        try:
            selected = [
                resolve_region(token.strip())
                for token in regions.split(",")
                if token.strip()
            ]
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return await PriceScraper(requester).scrape(
            sku, regions=selected, concurrency=concurrency
        )
    except RequesterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# =============================================================================
#  Module C — search
# =============================================================================
@router.get(
    "/search/position",
    response_model=SerpResult,
    summary="SERP/GEO position of a SKU for a keyword",
)
async def get_search_position(
    requester: Annotated[HybridRequester, Depends(get_requester)],
    query: Annotated[str, Query(min_length=1, max_length=200)],
    sku: Annotated[str, Query(pattern=r"^\d+$")],
    region: Annotated[str, Query()] = "moscow",
    max_pages: Annotated[int | None, Query(ge=1, le=50)] = None,
    include_entries: Annotated[bool, Query()] = False,
    _: Annotated[None, Guarded] = None,
) -> SerpResult:
    try:
        return await SearchScraper(requester).find_position(
            query,
            sku,
            region=_region_or_400(region),
            max_pages=max_pages,
            keep_entries=include_entries,
        )
    except RequesterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/search/rich-content/{sku}",
    response_model=RichContentResult,
    summary="Rich content, hi-res media and SKU variant group",
)
async def get_rich_content(
    requester: Annotated[HybridRequester, Depends(get_requester)],
    sku: Annotated[str, Path(pattern=r"^\d+$")],
    region: Annotated[str, Query()] = "moscow",
    _: Annotated[None, Guarded] = None,
) -> RichContentResult:
    try:
        return await SearchScraper(requester).rich_content(
            sku, region=_region_or_400(region)
        )
    except RequesterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# =============================================================================
#  Module D — inventory
# =============================================================================
class InventoryResponse(BaseModel):
    snapshot: StockSnapshot | None = None
    velocity: SalesVelocity | None = None
    history_size: int = 0


@router.get(
    "/inventory/{sku}",
    response_model=InventoryResponse,
    summary="FBO/FBS stock estimate and sales velocity",
)
async def get_inventory(
    requester: Annotated[HybridRequester, Depends(get_requester)],
    sku: Annotated[str, Path(pattern=r"^\d+$")],
    region: Annotated[str, Query()] = "moscow",
    use_cart_probe: Annotated[
        bool, Query(description="Opt in to the cart-clamp probe (writes to a cart)")
    ] = False,
    _: Annotated[None, Guarded] = None,
) -> InventoryResponse:
    scraper = StockScraper(requester)
    try:
        snapshot = await scraper.snapshot(
            sku, region=_region_or_400(region), use_cart_probe=use_cart_probe
        )
    except RequesterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    history = scraper.history(sku)
    from ..scrapers.stock_scraper import compute_velocity

    return InventoryResponse(
        snapshot=snapshot,
        velocity=compute_velocity(history),
        history_size=len(history),
    )


# =============================================================================
#  Services
# =============================================================================
@router.post(
    "/services/sentiment/cluster",
    response_model=SentimentClusterResult,
    summary="Cluster negative reviews and build the LLM auto-reply payload",
)
async def cluster_sentiment(
    request: Request,
    body: Annotated[SentimentRequest, Body()],
    _: Annotated[None, Guarded] = None,
) -> SentimentClusterResult:
    reviews = body.reviews
    if not reviews:
        if not body.sku:
            raise HTTPException(
                status_code=400, detail="Provide either `reviews` or `sku`"
            )
        requester = get_requester(request)
        try:
            scraped = await ReviewScraper(requester).scrape(
                body.sku, max_pages=body.scrape_max_pages, with_text_only=True
            )
        except RequesterError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        reviews = scraped.reviews

    return SentimentAnalyzer().cluster(
        reviews,
        sku=body.sku,
        negative_only=body.negative_only,
        build_llm_payload=body.build_llm_payload,
        product_title=body.product_title,
        seller_name=body.seller_name,
        max_llm_reviews=body.max_llm_reviews,
    )


class RepricerResponse(BaseModel):
    decisions: list[RepriceDecision]
    merged_payload: dict[str, Any] | None = None


@router.post(
    "/services/repricer/calculate",
    response_model=RepricerResponse,
    summary="Compute repricing decisions and Seller API payloads",
)
async def calculate_repricing(
    body: Annotated[RepricerRequest, Body()],
    _: Annotated[None, Guarded] = None,
) -> RepricerResponse:
    repricer = Repricer()
    decisions = repricer.calculate_many(body.requests)
    return RepricerResponse(
        decisions=decisions,
        merged_payload=repricer.merge_payloads(decisions) if body.merge_payloads else None,
    )


# =============================================================================
#  Reference
# =============================================================================
@router.get("/regions", summary="The 8 tracked macro-regions")
async def list_regions() -> list[dict[str, Any]]:
    return [region.model_dump() for region in REGIONS]


# =============================================================================
#  Helpers
# =============================================================================
def _region_or_400(region: str) -> str:
    try:
        resolve_region(region)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return region


def _area_id(region: str) -> int:
    return _resolve_or_400(region).area_id


def _resolve_or_400(region: str):
    try:
        return resolve_region(region)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
