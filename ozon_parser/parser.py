"""High-level orchestrator: proxy bridge + Camoufox + parse pipeline.

Typical use::

    async with OzonStockParser() as parser:
        result = await parser.parse("2160940482")
        parser.save(result)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from . import checkout as checkout_mod
from . import stock as stock_mod
from .browser import build_camoufox
from .config import Settings, load_settings
from .cookies import load_cookies
from .defender import resolve_challenge
from .models import Package, ParseStatus, StockResult
from .proxy_bridge import SocksHttpBridge

logger = logging.getLogger(__name__)


class OzonStockParser:
    """Owns the bridge + browser lifecycle and runs the per-SKU pipeline."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or load_settings()
        self._bridge: Optional[SocksHttpBridge] = None
        self._camoufox = None
        self._browser = None
        self._context = None

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "OzonStockParser":
        await self.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def start(self) -> None:
        s = self.settings

        self._bridge = SocksHttpBridge(
            socks_host=s.proxy.host,
            socks_port=s.proxy.port,
            socks_user=s.proxy.username,
            socks_pass=s.proxy.password,
            listen_host=s.bridge.host,
            listen_port=s.bridge.port,
        )
        await self._bridge.start()

        self._camoufox = build_camoufox(s.bridge, s.browser)
        self._browser = await self._camoufox.__aenter__()

        self._context = await self._browser.new_context(
            locale=s.browser.locale,
            timezone_id=s.browser.timezone,
        )
        self._context.set_default_navigation_timeout(s.browser.navigation_timeout)

        cookies = load_cookies(
            s.cookies_path,
            required=s.required_cookies,
        )
        await self._context.add_cookies(cookies)
        logger.info("Browser context ready with %d authorised cookies", len(cookies))

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # pragma: no cover - defensive
                pass
            self._context = None
        if self._camoufox is not None:
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception:  # pragma: no cover - defensive
                pass
            self._camoufox = None
            self._browser = None
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None

    # -- pipeline ----------------------------------------------------------

    async def parse(self, sku: str) -> StockResult:
        """Parse one SKU end-to-end and return a :class:`StockResult`."""

        if self._context is None:
            raise RuntimeError("Parser not started; use 'async with OzonStockParser()'")

        sku = str(sku).strip()
        page = await self._context.new_page()
        try:
            return await self._parse_on_page(page, sku)
        finally:
            try:
                await page.close()
            except Exception:  # pragma: no cover - defensive
                pass

    async def _parse_on_page(self, page: Page, sku: str) -> StockResult:
        s = self.settings
        url = s.product_url_tpl.format(sku=sku)

        logger.info("Opening product %s", url)
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            logger.warning("Initial product navigation timed out for %s", sku)

        if not await resolve_challenge(page):
            return StockResult(sku=sku, total_stock=0, status=ParseStatus.CHALLENGE_BLOCKED)

        title = await self._read_title(page)

        # 1) Primary stock source: the card "осталось" counter.
        card_stock = await stock_mod.extract_from_card(page)

        # 2) Stage the item in the cart. This is required for the checkout split
        #    step regardless of how stock is read, and doubles as the cart-probe
        #    fallback: requesting 10 000 units returns the server-enforced max.
        added = await stock_mod.add_to_cart(page)
        cart_max = None
        if added:
            cart_max = await stock_mod.read_cart_max(page, cart_url=s.cart_url)
        elif card_stock is None:
            logger.warning("Add-to-cart unavailable and no card counter for %s", sku)

        total_stock = card_stock if card_stock is not None else cart_max
        if total_stock is None:
            # Neither source produced a real number — never fabricate one.
            return StockResult(
                sku=sku,
                total_stock=0,
                product_title=title,
                status=ParseStatus.HYDRATION_ERROR,
            )

        # 3) Move to checkout for the split (only possible if the item is in cart).
        packages: list[Package] = []
        pvz = ""
        status = ParseStatus.OK
        if not added:
            status = ParseStatus.STOCK_ONLY
        else:
            try:
                if "/cart" not in page.url:
                    await page.goto(s.cart_url, wait_until="domcontentloaded")
                if await checkout_mod.go_to_checkout(page, checkout_url_tpl=s.checkout_url_tpl):
                    packages, pvz = await checkout_mod.parse_checkout(page)
                else:
                    status = ParseStatus.STOCK_ONLY
            except PlaywrightTimeoutError:
                logger.warning("Checkout stage timed out for %s", sku)
                status = ParseStatus.STOCK_ONLY

        if not packages and status is ParseStatus.OK:
            status = ParseStatus.STOCK_ONLY

        return StockResult(
            sku=sku,
            total_stock=total_stock,
            product_title=title,
            delivery_pvz=pvz,
            packages_split=packages,
            status=status,
        )

    # -- output ------------------------------------------------------------

    def save(self, result: StockResult) -> Path:
        """Write ``stock_{sku}.json`` into the configured output directory."""

        out_dir = Path(self.settings.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"stock_{result.sku}.json"
        path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Wrote %s", path)
        return path

    # -- helpers -----------------------------------------------------------

    @staticmethod
    async def _read_title(page: Page) -> str:
        selectors = ("h1", "[data-widget='webProductHeading'] h1", "[data-widget='webProductHeading']")
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    text = (await locator.inner_text()).strip()
                    if text:
                        return " ".join(text.split())
            except Exception:  # pragma: no cover - defensive
                continue
        try:
            return (await page.title()).strip()
        except Exception:
            return ""
