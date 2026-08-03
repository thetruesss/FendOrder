"""Total-stock extraction and cart interaction.

Primary stock source: regex over the rendered product card text
(``… осталось`` counter). Fallback source: the *cart probe* — request an absurd
quantity (10 000) in the cart and read back the server-enforced maximum, which
equals the real seller stock.

The cart interaction (``add_to_cart`` + ``read_cart_max``) is also what stages
the item for the ``/gocheckout`` split step, so the parser runs it even when the
card counter already yielded a number.

There are **no** hard-coded stock numbers in this module. If neither source
yields a figure the caller receives ``0`` plus a hydration-error status.
"""

from __future__ import annotations

import logging
import re

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

_PROBE_QUANTITY = "10000"

# "294 осталось", "1 250 штук осталось" (RU counter on the card).
_STOCK_RE = re.compile(
    r"(\d+[\d\s]*)\s*(?:единиц|единицы|штук|шт)?\s*осталось",
    re.IGNORECASE,
)
# Reverse order variant: "осталось 294 шт".
_STOCK_RE_REVERSED = re.compile(
    r"осталось\s*(\d+[\d\s]*)\s*(?:единиц|единицы|штук|шт)?",
    re.IGNORECASE,
)

# Server limit message shown when the requested quantity exceeds availability,
# e.g. "Осталось 33 шт", "Максимум 33 шт", "Доступно 33 штуки".
_LIMIT_RE = re.compile(
    r"(?:максимум|осталось|доступно|в наличии)\s*(\d+[\d\s]*)\s*(?:шт|штук|единиц)",
    re.IGNORECASE,
)

_ADD_TO_CART_SELECTORS = (
    "button:has-text('Добавить в корзину')",
    "[data-widget='webAddToCart'] button",
    "div[data-widget='webAddToCart'] button",
)
_QTY_INPUT_SELECTORS = (
    "input[type='number']",
    "input[inputmode='numeric']",
    "[data-widget='cartList'] input",
)


def _to_int(raw: str) -> int | None:
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def _search_stock(text: str) -> int | None:
    for pattern in (_STOCK_RE, _STOCK_RE_REVERSED):
        match = pattern.search(text)
        if match:
            value = _to_int(match.group(1))
            if value is not None:
                return value
    return None


async def extract_from_card(page: Page) -> int | None:
    """Regex the visible card text for the ``осталось`` counter."""

    text = await _safe_body_text(page)
    value = _search_stock(text)
    if value is not None:
        logger.info("Stock read from card counter: %d", value)
    return value


async def add_to_cart(page: Page) -> bool:
    """Click *Добавить в корзину* on the product page (force=True)."""

    if await _click_first(page, _ADD_TO_CART_SELECTORS, force=True):
        return True
    logger.warning("'Добавить в корзину' control not found")
    return False


async def read_cart_max(page: Page, *, cart_url: str) -> int | None:
    """Navigate to the cart, request 10 000 units and read the enforced maximum.

    Returns the server-limited quantity, or ``None`` if the probe could not be
    completed (e.g. the quantity control never hydrated).
    """

    try:
        await page.goto(cart_url, wait_until="domcontentloaded")
    except PlaywrightTimeoutError:
        logger.warning("Cart probe: navigation to cart timed out")
        return None

    qty_input = await _first_visible(page, _QTY_INPUT_SELECTORS)
    if qty_input is None:
        logger.warning("Cart probe: quantity input not found; reading limit from text")
        return _limit_from_text(await _safe_body_text(page))

    try:
        await qty_input.fill(_PROBE_QUANTITY)
        await qty_input.press("Enter")
        # Let the cart re-price / clamp the quantity and surface the limit note.
        await page.wait_for_timeout(2500)
    except PlaywrightTimeoutError:
        pass

    limit = _limit_from_text(await _safe_body_text(page))
    if limit is None:
        # The input itself is usually clamped to the real maximum after submit.
        clamped = await _read_input_value(qty_input)
        if clamped is not None and clamped < int(_PROBE_QUANTITY):
            limit = clamped

    if limit is not None:
        logger.info("Stock inferred from cart probe: %d", limit)
    return limit


def _limit_from_text(text: str) -> int | None:
    match = _LIMIT_RE.search(text)
    if match:
        return _to_int(match.group(1))
    return None


# -- small Playwright helpers ---------------------------------------------


async def _click_first(page: Page, selectors, *, force: bool) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() == 0:
                continue
            await locator.click(force=force, timeout=8000)
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:  # pragma: no cover - defensive
            continue
    return False


async def _first_visible(page: Page, selectors) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                return locator
        except Exception:  # pragma: no cover - defensive
            continue
    return None


async def _read_input_value(locator: Locator) -> int | None:
    try:
        return _to_int(await locator.input_value())
    except Exception:
        return None


async def _safe_body_text(page: Page) -> str:
    try:
        return await page.inner_text("body")
    except Exception:
        return ""
