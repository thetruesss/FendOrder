"""Celery tasks for long-running scrapes (spec §2.1 stack: Celery / Redis).

A full review sweep on a 30 000-review card runs for minutes — far longer than
an HTTP client will hold a connection open. The REST endpoints stay for
interactive use; anything at scale should be submitted here and polled.

Each task builds its own :class:`HybridRequester` because Celery workers are
processes, not coroutines: a bridge daemon and a curl session cannot be shared
across a fork. That costs one browser bootstrap per task, which is why tasks
should be batched by SKU rather than by page.

Run a worker with::

    celery -A ozon_anti_bot_engine.tasks worker --loglevel=info --concurrency=2
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from celery import Celery

from .config import get_settings
from .core.requester import HybridRequester
from .models import RepriceRequest
from .scrapers import PriceScraper, ReviewScraper, SearchScraper, StockScraper
from .services import Repricer, SentimentAnalyzer

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery(
    "ozon_anti_bot_engine",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Moscow",
    enable_utc=True,
    # Browser bootstraps are slow; a short visibility timeout would redeliver a
    # task that is merely still working.
    broker_transport_options={"visibility_timeout": 3600},
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion inside a synchronous Celery worker."""
    return asyncio.run(coro)


async def _with_requester(fn: Any) -> Any:
    async with HybridRequester() as requester:
        return await fn(requester)


# =============================================================================
#  Tasks
# =============================================================================
@celery_app.task(name="ozon.scrape_reviews", bind=True, max_retries=2)
def scrape_reviews_task(self: Any, sku: str, **kwargs: Any) -> dict[str, Any]:
    async def job(requester: HybridRequester) -> dict[str, Any]:
        result = await ReviewScraper(requester).scrape(sku, **kwargs)
        return result.model_dump(mode="json")

    try:
        return _run(_with_requester(job))
    except Exception as exc:  # pragma: no cover - retry path
        logger.exception("review scrape failed for %s", sku)
        raise self.retry(exc=exc, countdown=120) from exc


@celery_app.task(name="ozon.scrape_pricing", bind=True, max_retries=2)
def scrape_pricing_task(self: Any, sku: str, **kwargs: Any) -> dict[str, Any]:
    async def job(requester: HybridRequester) -> dict[str, Any]:
        result = await PriceScraper(requester).scrape(sku, **kwargs)
        return result.model_dump(mode="json")

    try:
        return _run(_with_requester(job))
    except Exception as exc:  # pragma: no cover - retry path
        logger.exception("pricing scrape failed for %s", sku)
        raise self.retry(exc=exc, countdown=120) from exc


@celery_app.task(name="ozon.track_position")
def track_position_task(query: str, sku: str, **kwargs: Any) -> dict[str, Any]:
    async def job(requester: HybridRequester) -> dict[str, Any]:
        result = await SearchScraper(requester).find_position(query, sku, **kwargs)
        return result.model_dump(mode="json")

    return _run(_with_requester(job))


@celery_app.task(name="ozon.snapshot_stock")
def snapshot_stock_task(sku: str, **kwargs: Any) -> dict[str, Any]:
    """Periodic stock reading — schedule this to build the velocity series."""

    async def job(requester: HybridRequester) -> dict[str, Any]:
        result = await StockScraper(requester).snapshot(sku, **kwargs)
        return result.model_dump(mode="json")

    return _run(_with_requester(job))


@celery_app.task(name="ozon.cluster_sentiment")
def cluster_sentiment_task(sku: str, max_pages: int = 40) -> dict[str, Any]:
    async def job(requester: HybridRequester) -> dict[str, Any]:
        scraped = await ReviewScraper(requester).scrape(
            sku, max_pages=max_pages, with_text_only=True
        )
        clustered = SentimentAnalyzer().cluster(scraped.reviews, sku=sku)
        return clustered.model_dump(mode="json")

    return _run(_with_requester(job))


@celery_app.task(name="ozon.calculate_repricing")
def calculate_repricing_task(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure computation — no network, so no requester is built."""
    requests = [RepriceRequest.model_validate(p) for p in payloads]
    decisions = Repricer().calculate_many(requests)
    return [decision.model_dump(mode="json") for decision in decisions]
