"""Result schemas for the parser output."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class ParseStatus(str, Enum):
    """Machine-readable outcome of a single SKU parse."""

    OK = "ok"
    # Stock read from the card / cart probe but checkout split was unavailable.
    STOCK_ONLY = "stock_only"
    # The product DOM never hydrated a usable stock figure.
    HYDRATION_ERROR = "hydration_error"
    # The anti-bot challenge could not be cleared.
    CHALLENGE_BLOCKED = "challenge_blocked"


@dataclass(slots=True)
class Package:
    """A single fulfilment package inside a checkout split."""

    package_id: int
    warehouse: str
    count: int
    delivery_date: str


@dataclass(slots=True)
class StockResult:
    """Full parse result, serialised to ``stock_{sku}.json``."""

    sku: str
    total_stock: int
    product_title: str = ""
    delivery_pvz: str = ""
    packages_split: list[Package] = field(default_factory=list)
    status: ParseStatus = ParseStatus.OK

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data
