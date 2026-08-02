"""Central configuration for the Ozon B2B Engine.

Every tunable — credentials, proxy pool, timeouts, region table — is resolved
here from environment variables via ``pydantic-settings``. Nothing in the source
tree hardcodes a secret; see ``.env.example`` for the full surface.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


# =============================================================================
#  Region registry — the 8 RU macro-regions tracked for pricing (spec §4.2)
# =============================================================================
class Region(BaseModel):
    """One Ozon delivery region, keyed by the ``areaId`` query parameter."""

    key: str
    city: str
    macro_region: str
    area_id: int

    @property
    def product_url_suffix(self) -> str:
        return f"?areaId={self.area_id}"


REGIONS: tuple[Region, ...] = (
    Region(key="moscow", city="Москва", macro_region="Центр", area_id=2),
    Region(key="spb", city="Санкт-Петербург", macro_region="Северо-Запад", area_id=3),
    Region(key="krasnodar", city="Краснодар", macro_region="Юг", area_id=14),
    Region(key="kazan", city="Казань", macro_region="Поволжье", area_id=16),
    Region(key="ekaterinburg", city="Екатеринбург", macro_region="Урал", area_id=58),
    Region(key="novosibirsk", city="Новосибирск", macro_region="Сибирь", area_id=67),
    Region(key="khabarovsk", city="Хабаровск", macro_region="Дальний Восток", area_id=79),
    Region(key="arkhangelsk", city="Архангельск", macro_region="Север", area_id=2495),
)

REGIONS_BY_KEY: dict[str, Region] = {r.key: r for r in REGIONS}
REGIONS_BY_AREA_ID: dict[int, Region] = {r.area_id: r for r in REGIONS}


def resolve_region(ref: str | int) -> Region:
    """Look a region up by key (``"moscow"``), areaId (``2``) or city name."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        area_id = int(ref)
        if area_id in REGIONS_BY_AREA_ID:
            return REGIONS_BY_AREA_ID[area_id]
        raise KeyError(f"Unknown areaId: {area_id}")
    key = str(ref).strip().lower()
    if key in REGIONS_BY_KEY:
        return REGIONS_BY_KEY[key]
    for region in REGIONS:
        if region.city.lower() == key:
            return region
    raise KeyError(f"Unknown region: {ref!r}")


# =============================================================================
#  Settings
# =============================================================================
class Settings(BaseSettings):
    """Runtime configuration, loaded from the environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="OZON_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---------------------------------------------------------
    app_name: str = "Ozon B2B Engine"
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str = Field(default="", repr=False)

    # --- Proxy pool ----------------------------------------------------------
    # Kept as a raw string so pydantic-settings does not attempt JSON decoding.
    proxy_pool: str = ""
    proxy_pool_file: str = ""
    proxy_bridge_host: str = "127.0.0.1"
    proxy_bridge_port: int = 0
    proxy_bridge_max_threads: int = 64
    proxy_cooldown_seconds: int = 180
    proxy_sticky_ttl_seconds: int = 900
    proxy_healthcheck_url: str = "https://www.ozon.ru/"
    proxy_healthcheck_timeout: int = 15

    # --- Anti-bot / transport ------------------------------------------------
    impersonate_profile: str = "chrome124"
    request_timeout: int = 45
    max_retries: int = 4
    http_concurrency: int = 8
    min_request_delay: float = 0.35
    max_request_delay: float = 1.10

    # --- Browser -------------------------------------------------------------
    browser_headless: bool = True
    browser_locale: str = "ru-RU"
    browser_timezone: str = "Europe/Moscow"
    browser_nav_timeout: int = 60_000
    browser_challenge_timeout: int = 45
    camoufox_executable: str = ""

    # --- Session vault -------------------------------------------------------
    session_dir: Path = PROJECT_ROOT / ".sessions"
    cookie_format: Literal["json", "netscape", "both"] = "json"
    cookie_refresh_margin: int = 300  # spec §3.3 — refresh 5 min before expiry
    cookie_watch_interval: int = 60

    # --- Scraper tuning ------------------------------------------------------
    review_max_pages: int = 500
    review_page_size: int = 25
    review_target_coverage: float = 1.0
    stock_probe_quantity: int = 990
    serp_max_pages: int = 15

    # --- Storage -------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = ""
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- Downstream LLM ------------------------------------------------------
    llm_provider: Literal["none", "anthropic", "openai", "gemini"] = "none"
    llm_api_key: str = Field(default="", repr=False)
    llm_model: str = ""

    # --- Ozon Seller API -----------------------------------------------------
    seller_client_id: str = Field(default="", repr=False)
    seller_api_key: str = Field(default="", repr=False)

    # -------------------------------------------------------------------------
    @field_validator("min_request_delay", "max_request_delay")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("request delays must be >= 0")
        return v

    @field_validator("review_target_coverage")
    @classmethod
    def _coverage_range(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("review_target_coverage must be in (0, 1]")
        return v

    # -------------------------------------------------------------------------
    @property
    def proxy_uris(self) -> list[str]:
        """Merged proxy list from the inline env var and the optional file."""
        uris: list[str] = [
            chunk.strip() for chunk in self.proxy_pool.split(",") if chunk.strip()
        ]
        if self.proxy_pool_file:
            path = Path(self.proxy_pool_file).expanduser()
            if path.is_file():
                uris.extend(
                    line.strip()
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                )
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(uris))

    @property
    def has_proxies(self) -> bool:
        return bool(self.proxy_uris)

    @property
    def regions(self) -> tuple[Region, ...]:
        return REGIONS

    def ensure_dirs(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Cached so ``.env`` is parsed exactly once."""
    settings = Settings()
    settings.ensure_dirs()
    return settings
