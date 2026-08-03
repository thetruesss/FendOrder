"""Camoufox (AsyncCamoufox) launch helper.

The browser is pointed at the local HTTP bridge, not directly at Soxway, so that
the authenticated SOCKS5 hop is handled by :mod:`ozon_parser.proxy_bridge`.
"""

from __future__ import annotations

import logging
from typing import Any

from camoufox.async_api import AsyncCamoufox

from .config import BridgeConfig, BrowserConfig

logger = logging.getLogger(__name__)


def build_camoufox(bridge: BridgeConfig, browser: BrowserConfig) -> AsyncCamoufox:
    """Return an un-started :class:`AsyncCamoufox` context manager.

    The caller is responsible for entering/exiting it (``async with`` or the
    parser's own lifecycle) so the browser and the proxy bridge share one scope.
    """

    options: dict[str, Any] = {
        "headless": browser.headless,
        "proxy": {"server": bridge.server_url},
        "geoip": browser.geoip,
        "locale": browser.locale,
        "humanize": True,  # human-like cursor movement to soften behavioural checks
        "i_know_what_im_doing": True,
    }
    logger.info(
        "Launching Camoufox (headless=%s, proxy=%s, geoip=%s)",
        browser.headless,
        bridge.server_url,
        browser.geoip,
    )
    return AsyncCamoufox(**options)
