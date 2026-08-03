"""Ozon Defender challenge resolver.

When Ozon serves the anti-bot interstitial instead of the product card, the page
title changes and a ``#reload-button`` appears. Clicking it (with ``force=True``,
because the button is often covered by an overlay) triggers a server redirect
back to the requested URL carrying ``?__rr=1&abt_att=1``.

Crucially we must **not** call ``page.reload()`` while that redirect is in
flight: Firefox aborts the in-progress navigation and Playwright surfaces
``NS_BINDING_ABORTED``. Instead we wait for the URL to gain the challenge-cleared
query parameters.
"""

from __future__ import annotations

import logging
import re

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

# Substrings that appear in the challenge page <title> (RU + EN variants).
_CHALLENGE_TITLE_MARKERS = (
    "antibot",
    "anti-bot",
    "challenge",
    "проверка",
    "доступ ограничен",
    "just a moment",
    "проверяем",
    "ozon defender",
)

_RELOAD_BUTTON = "#reload-button"
_CLEARED_URL_RE = re.compile(r"__rr=1")


async def is_challenge_page(page: Page) -> bool:
    """Heuristic: challenge markers in the title, or the reload button present."""

    try:
        title = (await page.title()) or ""
    except Exception:  # navigation may still be settling
        title = ""

    lowered = title.lower()
    if any(marker in lowered for marker in _CHALLENGE_TITLE_MARKERS):
        return True

    # Some variants ship an empty/neutral title; fall back to the DOM marker.
    try:
        return await page.locator(_RELOAD_BUTTON).count() > 0
    except Exception:
        return False


async def resolve_challenge(page: Page, *, max_attempts: int = 3, timeout: int = 30000) -> bool:
    """Clear the Defender challenge if present.

    Returns ``True`` when the page is (or becomes) a normal page, ``False`` if
    the challenge could not be cleared within ``max_attempts``.
    """

    for attempt in range(1, max_attempts + 1):
        if not await is_challenge_page(page):
            return True

        logger.info("Defender challenge detected (attempt %d/%d)", attempt, max_attempts)
        button = page.locator(_RELOAD_BUTTON)
        try:
            if await button.count() == 0:
                # Title says challenge but no button — give the page a moment.
                await page.wait_for_selector(_RELOAD_BUTTON, timeout=timeout)
            # force=True: the button is frequently behind a transparent overlay.
            await button.click(force=True)
        except PlaywrightTimeoutError:
            logger.warning("Reload button never appeared on attempt %d", attempt)
            continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Reload click failed on attempt %d: %s", attempt, exc)
            continue

        # Wait for the server-driven redirect that carries the cleared markers.
        # Deliberately NOT page.reload() — that would abort with NS_BINDING_ABORTED.
        try:
            await page.wait_for_url(_CLEARED_URL_RE, timeout=timeout)
        except PlaywrightTimeoutError:
            logger.debug("Cleared-URL marker not observed; re-checking page state")

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except PlaywrightTimeoutError:
            pass

    cleared = not await is_challenge_page(page)
    if not cleared:
        logger.error("Failed to clear Defender challenge after %d attempts", max_attempts)
    return cleared
