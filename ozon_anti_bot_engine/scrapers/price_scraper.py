"""Module B — regional pricing, Ozon-card price and SPP subsidy (spec §4.2).

**The accuracy rule.** Swapping the ``areaId`` cookie and re-reading the API
does *not* produce the right price. Ozon binds the logistics component to the
region the session was established in, so a cookie-only switch leaves a 7–14 ₽
error on most cards — small enough to look plausible and large enough to poison
a repricing decision.

The only reliable method is a full physical navigation to
``https://www.ozon.ru/product/<SKU>/?areaId=<ID>`` per region, letting the page
re-resolve delivery from scratch. That is what this module does, and it is why
pricing is the one scraper that stays on the browser transport rather than
dropping to the fast HTTP path.

Prices are read from the Composer payloads the page itself fetched (captured via
the response hook) rather than from rendered DOM text, with an embedded-state
fallback parsed out of the HTML.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..config import REGIONS, Region, Settings, get_settings, resolve_region
from ..core.requester import BrowserCapture, HybridRequester, OzonUrls, parse_widget_states
from ..models import PriceScrapeResult, RegionalPrice

logger = logging.getLogger(__name__)

_PRICE_WIDGETS = ("webPrice", "webSale", "webOzonAccountPrice", "webPriceWithDiscount")
_TITLE_WIDGETS = ("webProductHeading", "webProductMainWidget")
_SELLER_WIDGETS = ("webCurrentSeller", "webStickyProducts")

# «Лучшая цена» — the badge Ozon grants for an additional ranking boost.
_BEST_PRICE_MARKERS = ("лучшая цена", "bestprice", "best_price")

# Whitespace inside Ozon prices is a mix of NBSP, thin space and narrow NBSP.
_PRICE_CLEAN_RE = re.compile(r"[^\d,.\-]")
_SPACE_RE = re.compile(r"[\s   ]")


def parse_price(value: Any) -> float | None:
    """Parse ``"1 299 ₽"`` / ``"1 299,50 ₽"`` / ``1299`` into a float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        for key in ("price", "value", "text", "amount", "priceValue"):
            if key in value:
                parsed = parse_price(value[key])
                if parsed is not None:
                    return parsed
        return None
    if not isinstance(value, str):
        return None

    text = _SPACE_RE.sub("", value)
    text = _PRICE_CLEAN_RE.sub("", text)
    if not text:
        return None
    # Russian formatting uses ',' as the decimal separator.
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text:
        text = text.replace(",", "")
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def extract_embedded_state(html: str) -> dict[str, Any]:
    """Recover the Composer payload Ozon inlines into the PDP HTML.

    Fallback for when the response hook missed the XHR (served from bfcache, or
    the payload arrived before the listener attached).
    """
    marker = '"widgetStates"'
    index = html.find(marker)
    if index == -1:
        return {}

    # Walk backwards to the opening brace of the object that holds widgetStates,
    # then forwards with a depth counter to its matching close.
    start = html.rfind("{", 0, index)
    if start == -1:
        return {}

    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, min(len(html), start + 6_000_000)):
        char = html[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : pos + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def _merge_states(capture: BrowserCapture) -> dict[str, Any]:
    """Union the widget states from every captured payload plus the HTML fallback."""
    states: dict[str, Any] = {}
    for payload in capture.captured:
        if isinstance(payload, Mapping):
            states.update(parse_widget_states(payload))
    if not states:
        embedded = extract_embedded_state(capture.html)
        if embedded:
            states.update(parse_widget_states(embedded))
    return states


def _states_matching(states: Mapping[str, Any], prefixes: Sequence[str]) -> list[Any]:
    matched: list[Any] = []
    for key, state in states.items():
        widget = key.split("-", 1)[0]
        if widget in prefixes or any(key.startswith(p) for p in prefixes):
            matched.append(state)
    return matched


# =============================================================================
#  Scraper
# =============================================================================
class PriceScraper:
    """Collects showcase / Ozon-card / base prices across the 8 macro-regions."""

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
        regions: Iterable[Region | str | int] | None = None,
        concurrency: int = 2,
    ) -> PriceScrapeResult:
        """Price ``sku`` in every requested region via full physical navigation."""
        sku = str(sku)
        started = time.monotonic()
        targets = self._resolve_regions(regions)

        # Each region needs its own browser navigation, so concurrency is capped
        # low by default: parallel Camoufox instances are memory-hungry and burn
        # proxy traffic that the hybrid design exists to save.
        semaphore = asyncio.Semaphore(max(1, concurrency))
        result = PriceScrapeResult(sku=sku)

        async def run(region: Region) -> RegionalPrice:
            async with semaphore:
                return await self._scrape_region(sku, region, result)

        prices = await asyncio.gather(
            *(run(region) for region in targets), return_exceptions=True
        )
        for region, price in zip(targets, prices, strict=True):
            if isinstance(price, BaseException):
                logger.error("pricing failed for %s: %s", region.city, price)
                result.regions.append(
                    RegionalPrice(
                        region_key=region.key,
                        city=region.city,
                        macro_region=region.macro_region,
                        area_id=region.area_id,
                        is_available=False,
                        error=str(price),
                    )
                )
            else:
                result.regions.append(price)

        # Keep the configured region order regardless of completion order.
        order = {region.key: i for i, region in enumerate(targets)}
        result.regions.sort(key=lambda r: order.get(r.region_key, 999))
        result.duration_seconds = round(time.monotonic() - started, 2)
        return result

    # ------------------------------------------------------------------
    def _resolve_regions(
        self, regions: Iterable[Region | str | int] | None
    ) -> list[Region]:
        if regions is None:
            return list(REGIONS)
        resolved: list[Region] = []
        for entry in regions:
            resolved.append(entry if isinstance(entry, Region) else resolve_region(entry))
        return resolved

    # ------------------------------------------------------------------
    async def _scrape_region(
        self, sku: str, region: Region, result: PriceScrapeResult
    ) -> RegionalPrice:
        # THE accuracy-critical line: a full navigation, not a cookie swap.
        url = OzonUrls.product_url(sku, region.area_id)
        logger.info("pricing %s in %s (areaId=%d)", sku, region.city, region.area_id)

        capture = await self.requester.navigate(url, area_id=region.area_id)
        states = _merge_states(capture)

        if not states:
            return RegionalPrice(
                region_key=region.key,
                city=region.city,
                macro_region=region.macro_region,
                area_id=region.area_id,
                is_available=False,
                error="no widget states captured (challenge or layout change)",
            )

        price = self._build_regional_price(region, states, capture.html)

        # Title and seller are region-invariant; take them from the first region
        # that yields them rather than re-deriving on every pass.
        if result.title is None:
            result.title = self._extract_title(states)
        if result.seller is None:
            result.seller = self._extract_seller(states)
        return price

    # ------------------------------------------------------------------
    def _build_regional_price(
        self, region: Region, states: Mapping[str, Any], html: str
    ) -> RegionalPrice:
        base = showcase = card = None
        available = True
        delivery_note: str | None = None

        for state in _states_matching(states, _PRICE_WIDGETS):
            if not isinstance(state, Mapping):
                continue
            # `price` is the any-bank showcase price, `originalPrice` the
            # struck-through seller price, `cardPrice` the green Ozon-card price.
            showcase = showcase or parse_price(
                state.get("price") or state.get("showcasePrice")
            )
            base = base or parse_price(
                state.get("originalPrice") or state.get("basePrice") or state.get("oldPrice")
            )
            card = card or parse_price(
                state.get("cardPrice")
                or state.get("ozonCardPrice")
                or state.get("greenPrice")
            )
            if "isAvail" in state:
                available = available and bool(state.get("isAvail"))

        # An out-of-stock card still renders a price block; treat a missing
        # showcase price as unavailable rather than reporting a null price.
        if showcase is None and card is None:
            available = False

        # Some cards omit `cardPrice` when the Ozon-card price equals the
        # showcase price. Falling back keeps SPP at 0 instead of null.
        if card is None and showcase is not None:
            card = showcase

        for state in _states_matching(states, ("webDeliveryDetails", "webShipping")):
            if isinstance(state, Mapping):
                note = state.get("title") or state.get("text")
                if isinstance(note, str) and note.strip():
                    delivery_note = note.strip()
                    break

        return RegionalPrice(
            region_key=region.key,
            city=region.city,
            macro_region=region.macro_region,
            area_id=region.area_id,
            base_price=base,
            showcase_price=showcase,
            ozon_card_price=card,
            best_price_badge=self._detect_best_price(states, html),
            is_available=available,
            delivery_note=delivery_note,
        )

    # ------------------------------------------------------------------
    def _detect_best_price(self, states: Mapping[str, Any], html: str) -> bool:
        """Detect the «Лучшая цена» badge (spec §4.2 — extra Ozon boost)."""
        for key, state in states.items():
            if "bestprice" in key.lower():
                return True
            if isinstance(state, Mapping):
                for value in state.values():
                    if isinstance(value, str) and "лучшая цена" in value.lower():
                        return True
        lowered = html.lower()
        return any(marker in lowered for marker in _BEST_PRICE_MARKERS)

    def _extract_title(self, states: Mapping[str, Any]) -> str | None:
        for state in _states_matching(states, _TITLE_WIDGETS):
            if isinstance(state, Mapping):
                title = state.get("title") or state.get("name")
                if isinstance(title, str) and title.strip():
                    return title.strip()
        return None

    def _extract_seller(self, states: Mapping[str, Any]) -> str | None:
        for state in _states_matching(states, _SELLER_WIDGETS):
            if not isinstance(state, Mapping):
                continue
            seller = state.get("name") or state.get("title")
            if isinstance(seller, Mapping):
                seller = seller.get("text") or seller.get("title")
            if isinstance(seller, str) and seller.strip():
                return seller.strip()
        return None


async def scrape_prices(
    sku: str | int,
    *,
    requester: HybridRequester | None = None,
    **kwargs: Any,
) -> PriceScrapeResult:
    if requester is not None:
        return await PriceScraper(requester).scrape(sku, **kwargs)
    async with HybridRequester() as owned:
        return await PriceScraper(owned).scrape(sku, **kwargs)
