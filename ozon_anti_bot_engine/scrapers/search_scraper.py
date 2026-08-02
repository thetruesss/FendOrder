"""Module C — SEO, SERP position tracking and rich content (spec §4.3).

Three jobs:

* **SERP/GEO position.** Walk the search results for a keyword, region-pinned via
  ``areaId``, and report where a SKU lands. Paid placements (Трафареты,
  «Продвижение в поиске») are counted separately from organic ones — a card
  sitting at slot 4 because of an ad budget is not ranked 4th, and conflating
  the two makes SEO work unmeasurable.
* **SKU variant groups (склейки).** Enumerate the sizes/colours/bundles glued to
  one card and identify the strongest variant in the group.
* **Rich content.** Extract the rich description structure and full-resolution
  media for content-funnel analysis.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..config import Region, Settings, get_settings, resolve_region
from ..core.requester import (
    HybridRequester,
    OzonUrls,
    RequesterError,
    parse_widget_states,
)
from ..models import RichContentResult, SerpEntry, SerpResult, SkuVariant

logger = logging.getLogger(__name__)

_SERP_WIDGETS = ("searchResultsV2", "tileGridDesktop", "searchResults", "skuGrid")
_ASPECT_WIDGETS = ("webAspects", "webProductAspects")
_RICH_WIDGETS = ("webRichContent", "webDescription", "webCharacteristics")
_GALLERY_WIDGETS = ("webGallery", "webProductGallery")

_SKU_FROM_LINK = re.compile(r"/product/(?:[^/]*?-)?(\d+)")
_AD_KEY_HINTS = ("isadv", "isadvert", "advbadge", "isadvertising", "ispromo")
_AD_TEXT_HINTS = ("реклама", "спонсор")

# Ozon CDN thumbnails carry a size segment; upgrade for content analysis.
_IMAGE_SIZE_RE = re.compile(r"/wc\d+/")


def sku_from_link(link: str) -> str | None:
    """``/product/nice-shirt-123456/`` -> ``"123456"``."""
    match = _SKU_FROM_LINK.search(link or "")
    return match.group(1) if match else None


def _upgrade_image(url: str) -> str:
    return _IMAGE_SIZE_RE.sub("/wc1000/", url) if url else url


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d,.]", "", value.replace(" ", "")).replace(",", ".")
        # Guard against strings like "4.8.1" that survive the character filter.
        if cleaned.count(".") > 1:
            cleaned = cleaned.replace(".", "", cleaned.count(".") - 1)
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None


def _flatten_text(node: Any, depth: int = 0, budget: int = 400) -> str:
    """Serialise the visible strings of a widget subtree, for marker matching."""
    if depth > 6 or budget <= 0:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, Mapping):
        parts = []
        for value in node.values():
            parts.append(_flatten_text(value, depth + 1, budget - len(parts)))
        return " ".join(p for p in parts if p)
    if isinstance(node, (list, tuple)):
        return " ".join(
            _flatten_text(v, depth + 1, budget - i) for i, v in enumerate(node[:40])
        )
    return ""


def is_advertisement(item: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Classify a SERP tile as paid or organic.

    Ozon marks paid slots inconsistently across layouts — a boolean flag on the
    tile, a tracking-info type, or only a rendered «Реклама» label — so all three
    are checked.
    """
    for key, value in item.items():
        if key.lower() in _AD_KEY_HINTS and bool(value):
            return True, "template"

    tracking = item.get("trackingInfo")
    if isinstance(tracking, Mapping):
        marker = str(tracking.get("type") or tracking.get("adType") or "").lower()
        if "adv" in marker or "promo" in marker or "banner" in marker:
            return True, "search_promo"
        if bool(tracking.get("isAdv")):
            return True, "search_promo"

    text = _flatten_text(item).lower()
    if any(hint in text for hint in _AD_TEXT_HINTS):
        return True, "template"
    return False, None


def extract_serp_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pull result tiles out of a search Composer response."""
    states = parse_widget_states(payload)
    items: list[dict[str, Any]] = []

    for key, state in states.items():
        widget = key.split("-", 1)[0]
        if widget not in _SERP_WIDGETS and not any(key.startswith(p) for p in _SERP_WIDGETS):
            continue
        if not isinstance(state, Mapping):
            continue
        for field in ("items", "products", "tiles"):
            container = state.get(field)
            if isinstance(container, (list, tuple)):
                items.extend(dict(i) for i in container if isinstance(i, Mapping))
    return items


def normalise_serp_item(
    item: Mapping[str, Any], position: int, organic_position: int | None, page: int
) -> SerpEntry | None:
    link = str(item.get("link") or item.get("action", {}).get("link", "") or "")
    sku = str(item.get("sku") or "") or sku_from_link(link) or ""
    if not sku:
        return None

    is_ad, ad_kind = is_advertisement(item)

    title = None
    price = None
    rating = None
    reviews = None

    # Tile content lives in `mainState`/`templateState` atom lists.
    for container_key in ("mainState", "templateState", "leftBottomState"):
        container = item.get(container_key)
        if not isinstance(container, (list, tuple)):
            continue
        for atom in container:
            if not isinstance(atom, Mapping):
                continue
            atom_id = str(atom.get("id") or atom.get("type") or "").lower()
            if title is None and "name" in atom_id:
                title = _flatten_text(atom.get("atom") or atom).strip() or None
            if price is None and "price" in atom_id:
                price_node = atom.get("priceV2") or atom.get("atom") or atom
                price = _extract_price_from_atom(price_node)
            if rating is None and ("rating" in atom_id or "review" in atom_id):
                rating, reviews = _extract_rating_from_atom(atom)

    return SerpEntry(
        position=position,
        organic_position=organic_position,
        sku=sku,
        title=title,
        price=price,
        rating=rating,
        review_count=reviews,
        is_ad=is_ad,
        ad_kind=ad_kind,
        page=page,
    )


def _extract_price_from_atom(node: Any) -> float | None:
    if isinstance(node, Mapping):
        prices = node.get("price")
        if isinstance(prices, (list, tuple)):
            for entry in prices:
                if isinstance(entry, Mapping):
                    value = _as_float(entry.get("price") or entry.get("text"))
                    if value:
                        return value
        for key in ("price", "text", "value"):
            value = _as_float(node.get(key))
            if value:
                return value
        for value in node.values():
            found = _extract_price_from_atom(value)
            if found:
                return found
    return None


def _extract_rating_from_atom(node: Any) -> tuple[float | None, int | None]:
    text = _flatten_text(node)
    rating_match = re.search(r"\b([1-5][.,]\d)\b", text)
    reviews_match = re.search(r"(\d[\d\s ]*)\s*отзыв", text)
    rating = _as_float(rating_match.group(1)) if rating_match else None
    reviews = None
    if reviews_match:
        digits = re.sub(r"\D", "", reviews_match.group(1))
        reviews = int(digits) if digits else None
    return rating, reviews


# =============================================================================
#  Scraper
# =============================================================================
class SearchScraper:
    """SERP position tracking, variant-group analysis and rich-content extraction."""

    def __init__(
        self, requester: HybridRequester, settings: Settings | None = None
    ) -> None:
        self.requester = requester
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    async def find_position(
        self,
        query: str,
        sku: str | int | None = None,
        *,
        region: Region | str | int = "moscow",
        max_pages: int | None = None,
        keep_entries: bool = False,
    ) -> SerpResult:
        """Scan the SERP for ``query`` and locate ``sku`` (organic and absolute)."""
        target_region = region if isinstance(region, Region) else resolve_region(region)
        sku = str(sku) if sku is not None else None
        pages = max_pages or self.settings.serp_max_pages
        session_name = f"serp-{target_region.key}"

        await self.requester.bootstrap(
            url=f"https://www.ozon.ru/?areaId={target_region.area_id}",
            area_id=target_region.area_id,
            session_name=session_name,
        )

        result = SerpResult(
            query=query,
            region_key=target_region.key,
            area_id=target_region.area_id,
            target_sku=sku,
        )
        absolute = 0
        organic = 0

        for page in range(1, pages + 1):
            path = OzonUrls.search(query, page=page, area_id=target_region.area_id)
            try:
                payload = await self.requester.get_json(
                    path,
                    referer=f"https://www.ozon.ru{path}",
                    page_type="search",
                    session_name=session_name,
                )
            except RequesterError as exc:
                logger.warning("SERP page %d failed: %s", page, exc)
                break

            result.pages_scanned += 1
            items = extract_serp_items(payload)
            if not items:
                logger.info("no SERP tiles on page %d; stopping", page)
                break

            for item in items:
                absolute += 1
                is_ad, _ = is_advertisement(item)
                if not is_ad:
                    organic += 1
                entry = normalise_serp_item(
                    item, absolute, None if is_ad else organic, page
                )
                if entry is None:
                    continue
                result.total_entries += 1
                if keep_entries:
                    result.entries.append(entry)

                if sku and entry.sku == sku and result.found_position is None:
                    result.found_position = entry.position
                    result.found_organic_position = entry.organic_position
                    result.found_page = page
                    result.is_ad_placement = entry.is_ad
                    logger.info(
                        "found %s at absolute #%d (organic #%s) on page %d%s",
                        sku, entry.position, entry.organic_position, page,
                        " [AD]" if entry.is_ad else "",
                    )

            if sku and result.found_position is not None:
                break

        return result

    # ------------------------------------------------------------------
    async def rich_content(
        self,
        sku: str | int,
        *,
        region: Region | str | int = "moscow",
    ) -> RichContentResult:
        """Extract rich description, hi-res media and the SKU variant group."""
        sku = str(sku)
        target_region = region if isinstance(region, Region) else resolve_region(region)
        session_name = f"pdp-{sku}"

        await self.requester.bootstrap(
            sku, area_id=target_region.area_id, session_name=session_name
        )
        payload = await self.requester.get_json(
            OzonUrls.product(sku, target_region.area_id),
            referer=OzonUrls.product_url(sku, target_region.area_id),
            page_type="pdp",
            session_name=session_name,
        )
        states = parse_widget_states(payload)

        variants = self._extract_variants(states, current_sku=sku)
        return RichContentResult(
            sku=sku,
            title=self._extract_title(states),
            rich_content_json=self._extract_rich_json(states),
            rich_content_html=self._extract_rich_html(states),
            images=self._extract_images(states),
            variants=variants,
            best_selling_variant=self._best_selling_variant(variants),
            characteristics=self._extract_characteristics(states),
        )

    # ------------------------------------------------------------------
    def _extract_title(self, states: Mapping[str, Any]) -> str | None:
        for key, state in states.items():
            if key.startswith(("webProductHeading", "webProductMainWidget")) and isinstance(
                state, Mapping
            ):
                title = state.get("title") or state.get("name")
                if isinstance(title, str) and title.strip():
                    return title.strip()
        return None

    def _extract_rich_json(self, states: Mapping[str, Any]) -> dict[str, Any] | None:
        for key, state in states.items():
            if not key.startswith(_RICH_WIDGETS) or not isinstance(state, Mapping):
                continue
            rich = state.get("richAnnotationJson") or state.get("richContent")
            if isinstance(rich, str):
                try:
                    return json.loads(rich)
                except json.JSONDecodeError:
                    continue
            if isinstance(rich, Mapping):
                return dict(rich)
        return None

    def _extract_rich_html(self, states: Mapping[str, Any]) -> str | None:
        for key, state in states.items():
            if not key.startswith(_RICH_WIDGETS) or not isinstance(state, Mapping):
                continue
            for field in ("richAnnotation", "html", "content", "text"):
                value = state.get(field)
                if isinstance(value, str) and "<" in value:
                    return value
        return None

    def _extract_images(self, states: Mapping[str, Any]) -> list[str]:
        images: list[str] = []
        seen: set[str] = set()
        for key, state in states.items():
            if not key.startswith(_GALLERY_WIDGETS) or not isinstance(state, Mapping):
                continue
            for field in ("images", "coverImage", "gallery"):
                container = state.get(field)
                entries = container if isinstance(container, (list, tuple)) else [container]
                for entry in entries:
                    url = None
                    if isinstance(entry, str):
                        url = entry
                    elif isinstance(entry, Mapping):
                        url = entry.get("src") or entry.get("url") or entry.get("link")
                    if isinstance(url, str) and url and url not in seen:
                        seen.add(url)
                        images.append(_upgrade_image(url))
        return images

    def _extract_variants(
        self, states: Mapping[str, Any], *, current_sku: str
    ) -> list[SkuVariant]:
        variants: list[SkuVariant] = []
        seen: set[str] = set()

        for key, state in states.items():
            if not key.startswith(_ASPECT_WIDGETS) or not isinstance(state, Mapping):
                continue
            aspects = state.get("aspects")
            if not isinstance(aspects, (list, tuple)):
                continue
            for aspect in aspects:
                if not isinstance(aspect, Mapping):
                    continue
                kind = str(aspect.get("type") or aspect.get("name") or "") or None
                for variant in aspect.get("variants") or []:
                    if not isinstance(variant, Mapping):
                        continue
                    link = str(variant.get("link") or "")
                    variant_sku = str(variant.get("sku") or "") or sku_from_link(link) or ""
                    if not variant_sku or variant_sku in seen:
                        continue
                    seen.add(variant_sku)
                    data = variant.get("data") if isinstance(variant.get("data"), Mapping) else {}
                    variants.append(
                        SkuVariant(
                            sku=variant_sku,
                            kind=kind,
                            value=_flatten_text(
                                data.get("title") or variant.get("title") or data.get("text")
                            ).strip()
                            or None,
                            is_current=variant_sku == current_sku
                            or bool(variant.get("active")),
                            is_available=variant.get("availability", 1) != 0,
                            price=_as_float(variant.get("price")),
                            review_count=_as_float(variant.get("reviewsCount")) and int(
                                _as_float(variant.get("reviewsCount")) or 0
                            ),
                        )
                    )
        return variants

    def _best_selling_variant(self, variants: Sequence[SkuVariant]) -> str | None:
        """Strongest variant in the group.

        Ozon does not publish per-variant sales, so review count is used as the
        proxy: it is the only public monotone-with-sales signal on the card.
        This is an estimate, not a measurement.
        """
        rated = [v for v in variants if v.review_count]
        if not rated:
            return None
        return max(rated, key=lambda v: v.review_count or 0).sku

    def _extract_characteristics(self, states: Mapping[str, Any]) -> dict[str, str]:
        characteristics: dict[str, str] = {}
        for key, state in states.items():
            if not key.startswith("webCharacteristics") or not isinstance(state, Mapping):
                continue
            for group in state.get("characteristics") or []:
                if not isinstance(group, Mapping):
                    continue
                for record in group.get("short") or group.get("records") or []:
                    if not isinstance(record, Mapping):
                        continue
                    name = _flatten_text(record.get("key") or record.get("name")).strip()
                    value = _flatten_text(record.get("values") or record.get("value")).strip()
                    if name and value:
                        characteristics[name] = value
        return characteristics


async def find_position(
    query: str,
    sku: str | int,
    *,
    requester: HybridRequester | None = None,
    **kwargs: Any,
) -> SerpResult:
    if requester is not None:
        return await SearchScraper(requester).find_position(query, sku, **kwargs)
    async with HybridRequester() as owned:
        return await SearchScraper(owned).find_position(query, sku, **kwargs)
