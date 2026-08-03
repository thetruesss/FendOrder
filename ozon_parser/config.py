"""Central configuration for the Ozon stock / delivery / PVZ parser.

Every value can be overridden through an environment variable so that secrets do
not have to live in source control in a real deployment. The defaults reproduce
the values from the technical specification so the module also works out of the
box for local runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(slots=True)
class ProxyConfig:
    """Upstream Soxway SOCKS5 residential proxy."""

    host: str = field(default_factory=lambda: _env("SOXWAY_HOST", "v1.soxway.io"))
    port: int = field(default_factory=lambda: int(_env("SOXWAY_PORT", "1080")))
    username: str = field(
        default_factory=lambda: _env(
            "SOXWAY_USER",
            "90123ebb9f_country-RU_region-Moscow_city-Voskresensk_s-0sv997b",
        )
    )
    password: str = field(
        default_factory=lambda: _env("SOXWAY_PASS", "764e0986a8e943161e05ce91e0336222")
    )


@dataclass(slots=True)
class BridgeConfig:
    """Local HTTP-to-SOCKS5 bridge that Camoufox connects to."""

    host: str = field(default_factory=lambda: _env("BRIDGE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("BRIDGE_PORT", "8889")))

    @property
    def server_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(slots=True)
class BrowserConfig:
    """Camoufox / Playwright behaviour."""

    headless: bool = field(default_factory=lambda: _env("HEADLESS", "true").lower() == "true")
    locale: str = field(default_factory=lambda: _env("LOCALE", "ru-RU"))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Europe/Moscow"))
    # Camoufox uses the proxy exit IP to synthesise a matching geolocation when
    # geoip is enabled. Keep it on so the Moscow residential exit stays coherent.
    geoip: bool = field(default_factory=lambda: _env("GEOIP", "true").lower() == "true")
    # Milliseconds. Ozon is heavy and the residential hop adds latency.
    navigation_timeout: int = field(default_factory=lambda: int(_env("NAV_TIMEOUT", "60000")))


@dataclass(slots=True)
class Settings:
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)

    cookies_path: str = field(default_factory=lambda: _env("COOKIES_PATH", "/app/cookies.txt"))
    output_dir: str = field(default_factory=lambda: _env("OUTPUT_DIR", "."))

    # The subset of cookies that must be present for an authorised session.
    required_cookies: tuple[str, ...] = ("__Secure-access-token", "xcid", "abt_att", "__rr")

    # Base marketplace endpoints.
    product_url_tpl: str = "https://www.ozon.ru/product/{sku}/"
    cart_url: str = "https://www.ozon.ru/cart"
    checkout_url_tpl: str = "https://www.ozon.ru/gocheckout?activeTab=0{session}"


def load_settings() -> Settings:
    """Build a fresh :class:`Settings` instance from the current environment."""

    return Settings()
