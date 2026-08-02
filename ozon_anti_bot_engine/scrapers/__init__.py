"""Product data scrapers: reviews, regional pricing, SERP/rich content, stock."""

from .price_scraper import PriceScraper, extract_embedded_state, parse_price, scrape_prices
from .review_scraper import (
    ReviewScraper,
    extract_review_dicts,
    extract_total_reviews,
    normalise_review,
    scrape_reviews,
    upgrade_media_url,
)
from .search_scraper import (
    SearchScraper,
    extract_serp_items,
    find_position,
    is_advertisement,
    sku_from_link,
)
from .stock_scraper import (
    SnapshotStore,
    StockScraper,
    compute_velocity,
    detect_scheme,
    extract_passive_stock,
)

__all__ = [
    "PriceScraper",
    "ReviewScraper",
    "SearchScraper",
    "SnapshotStore",
    "StockScraper",
    "compute_velocity",
    "detect_scheme",
    "extract_embedded_state",
    "extract_passive_stock",
    "extract_review_dicts",
    "extract_serp_items",
    "extract_total_reviews",
    "find_position",
    "is_advertisement",
    "normalise_review",
    "parse_price",
    "scrape_prices",
    "scrape_reviews",
    "sku_from_link",
    "upgrade_media_url",
]
