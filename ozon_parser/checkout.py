"""``/gocheckout`` split-packages, delivery-dates and PVZ parser.

From the cart we click *Перейти к оформлению*, land on
``https://www.ozon.ru/gocheckout?activeTab=0&session_uid=…`` and read Ozon's
embedded checkout state. The state ships as JSON inside the page (Ozon composer
payload / hydration blob); we locate the ``splitPackages`` / ``checkoutSplit``
object, then walk it defensively because the exact shape drifts between A/B
buckets.

Everything here degrades gracefully: any field that cannot be found is returned
empty rather than guessed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .models import Package

logger = logging.getLogger(__name__)

_CHECKOUT_BUTTON_SELECTORS = (
    "button:has-text('Перейти к оформлению')",
    "a:has-text('Перейти к оформлению')",
    "[data-widget='cartControls'] button",
)

# Keys that introduce the split object we care about.
_SPLIT_KEYS = ("splitPackages", "checkoutSplit", "split")
# Candidate field names for each datum (shape drifts between experiments).
_WAREHOUSE_KEYS = ("warehouse", "warehouseName", "clusterName", "shipmentWarehouse", "name")
_COUNT_KEYS = ("count", "quantity", "qty", "itemsCount", "totalItems")
_DATE_KEYS = ("deliveryDate", "date", "deliveryDateText", "arrivalDate", "deliveryTime")

_PVZ_KEYS = ("pickupPoint", "pvz", "deliveryPoint", "outpost")
_PVZ_ADDRESS_KEYS = ("address", "addressText", "fullAddress", "title", "name")

_SESSION_UID_RE = re.compile(r"session_uid=([0-9a-fA-F-]+)")


async def go_to_checkout(page: Page, *, checkout_url_tpl: str) -> bool:
    """Click *Перейти к оформлению* and wait for the gocheckout page.

    Falls back to a direct navigation (reusing the cart's ``session_uid`` when we
    can recover it) if the button click does not trigger the transition.
    """

    clicked = await _click_first(page, _CHECKOUT_BUTTON_SELECTORS)
    if clicked:
        try:
            await page.wait_for_url(re.compile(r"/gocheckout"), timeout=30000)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            return True
        except PlaywrightTimeoutError:
            logger.debug("Checkout button click did not navigate; trying direct URL")

    session = _extract_session_uid(page.url)
    session_qs = f"&session_uid={session}" if session else ""
    try:
        await page.goto(checkout_url_tpl.format(session=session_qs), wait_until="domcontentloaded")
        return "/gocheckout" in page.url
    except PlaywrightTimeoutError:
        logger.warning("Direct navigation to gocheckout timed out")
        return False


async def parse_checkout(page: Page) -> tuple[list[Package], str]:
    """Return ``(packages, pvz_address)`` parsed from the checkout state."""

    state = await _collect_state_json(page)
    packages: list[Package] = []
    pvz = ""

    for blob in state:
        if not packages:
            packages = _extract_packages(blob)
        if not pvz:
            pvz = _extract_pvz(blob)
        if packages and pvz:
            break

    if not packages:
        logger.warning("No split packages found in checkout state")
    if not pvz:
        # The selected PVZ address is often plain text on the page too.
        pvz = await _pvz_from_dom(page)

    return packages, pvz


# -- state collection ------------------------------------------------------


async def _collect_state_json(page: Page) -> list[Any]:
    """Pull every JSON object on the page that mentions a split key."""

    blobs: list[Any] = []
    try:
        html = await page.content()
    except Exception:
        return blobs

    for key in _SPLIT_KEYS:
        for obj in _find_json_objects_with_key(html, key):
            blobs.append(obj)

    # Also inspect discrete <script> payloads (Nuxt/JSON islands).
    try:
        scripts = await page.locator("script").all_text_contents()
    except Exception:
        scripts = []
    for script in scripts:
        if any(k in script for k in _SPLIT_KEYS):
            for key in _SPLIT_KEYS:
                for obj in _find_json_objects_with_key(script, key):
                    blobs.append(obj)

    return blobs


def _find_json_objects_with_key(text: str, key: str) -> list[Any]:
    """Find ``"key": {...}`` / ``"key": [...]`` and JSON-decode each match.

    Uses brace/bracket balancing (respecting strings and escapes) so nested
    structures are captured whole.
    """

    results: list[Any] = []
    needle = f'"{key}"'
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        colon = text.find(":", idx + len(needle))
        if colon == -1:
            break
        value_start = _skip_ws(text, colon + 1)
        if value_start >= len(text) or text[value_start] not in "{[":
            start = colon + 1
            continue
        end = _match_bracket(text, value_start)
        if end == -1:
            start = colon + 1
            continue
        snippet = text[value_start : end + 1]
        try:
            results.append(json.loads(snippet))
        except json.JSONDecodeError:
            pass
        start = end + 1
    return results


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _match_bracket(text: str, start: int) -> int:
    """Index of the bracket that closes the one at ``start`` (-1 if unbalanced)."""

    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


# -- extraction ------------------------------------------------------------


def _extract_packages(blob: Any) -> list[Package]:
    """Locate a list of package-like dicts anywhere inside *blob*."""

    package_lists = _find_package_lists(blob)
    packages: list[Package] = []
    for lst in package_lists:
        for i, item in enumerate(lst, start=1):
            if not isinstance(item, dict):
                continue
            warehouse = _first_str(item, _WAREHOUSE_KEYS)
            count = _first_int(item, _COUNT_KEYS)
            date = _first_str(item, _DATE_KEYS)
            if warehouse is None and count is None and date is None:
                continue
            packages.append(
                Package(
                    package_id=_first_int(item, ("packageId", "id", "index")) or i,
                    warehouse=warehouse or "",
                    count=count or 0,
                    delivery_date=date or "",
                )
            )
        if packages:
            break
    return packages


def _find_package_lists(node: Any) -> list[list]:
    """Depth-first search for lists whose items look like packages."""

    found: list[list] = []

    def looks_like_package(d: Any) -> bool:
        return isinstance(d, dict) and (
            _has_any_key(d, _WAREHOUSE_KEYS)
            or _has_any_key(d, _COUNT_KEYS)
            or _has_any_key(d, _DATE_KEYS)
        )

    def walk(n: Any) -> None:
        if isinstance(n, list):
            if n and all(isinstance(x, dict) for x in n) and any(looks_like_package(x) for x in n):
                found.append(n)
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            for v in n.values():
                walk(v)

    walk(node)
    return found


def _extract_pvz(blob: Any) -> str:
    """Find the selected pickup-point address inside *blob*."""

    result = ""

    def walk(n: Any) -> None:
        nonlocal result
        if result:
            return
        if isinstance(n, dict):
            for key in _PVZ_KEYS:
                point = n.get(key)
                if isinstance(point, dict):
                    address = _first_str(point, _PVZ_ADDRESS_KEYS)
                    if address:
                        result = address
                        return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(blob)
    return result


async def _pvz_from_dom(page: Page) -> str:
    """Best-effort text scrape of the selected delivery point on the page."""

    selectors = (
        "[data-widget='deliveryPoint']",
        "[data-widget='pickupPoint']",
        "[data-widget='checkoutDeliveryMethod']",
    )
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                text = (await locator.inner_text()).strip()
                if text:
                    return " ".join(text.split())
        except Exception:  # pragma: no cover - defensive
            continue
    return ""


# -- generic helpers -------------------------------------------------------


def _has_any_key(d: dict, keys) -> bool:
    return any(k in d for k in keys)


def _first_str(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return None


def _first_int(d: dict, keys) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            digits = re.sub(r"\D", "", v)
            if digits:
                return int(digits)
    return None


def _extract_session_uid(url: str) -> str | None:
    match = _SESSION_UID_RE.search(url)
    return match.group(1) if match else None


async def _click_first(page: Page, selectors) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() == 0:
                continue
            await locator.click(timeout=8000)
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:  # pragma: no cover - defensive
            continue
    return False
