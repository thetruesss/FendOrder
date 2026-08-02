"""Module D — FBO/FBS stock estimation and competitor sales velocity (spec §4.4).

Ozon publishes no stock figure. Two techniques recover an estimate:

1. **Passive read** (default). The add-to-cart widget clamps its quantity picker
   to what is actually available, and low stock surfaces as an «осталось N шт»
   label. Reading those is free and mutates nothing.
2. **Cart probe** (opt-in). Request an absurd quantity; the server clamps the
   line to the real stock and echoes it back. More reliable on cards that hide
   the picker limit, but it writes to a cart, so it is behind an explicit flag
   rather than on by default.

Sales velocity is then the negative first derivative of the stock series, with
increases reclassified as restock events instead of negative sales.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Region, Settings, get_settings, resolve_region
from ..core.requester import HybridRequester, OzonUrls, parse_widget_states
from ..models import SalesVelocity, StockSnapshot, WarehouseStock

logger = logging.getLogger(__name__)

_CART_WIDGETS = ("webAddToCart", "webAddToCartButton", "webOutOfStock")
_DELIVERY_WIDGETS = ("webDeliveryDetails", "webShipping", "webCurrentSeller")

_REMAINING_RE = re.compile(r"осталось\s+(\d+)\s*шт", re.IGNORECASE)

# Ozon fulfilment schemes as they appear in widget payloads.
_SCHEME_HINTS: tuple[tuple[str, str], ...] = (
    ("fbo", "FBO"),
    ("fbs", "FBS"),
    ("rfbs", "RFBS"),
    ("crossborder", "CROSSBORDER"),
)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"\D", "", value)
        return int(digits) if digits else None
    return None


def _flatten_text(node: Any, depth: int = 0) -> str:
    if depth > 5:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, Mapping):
        return " ".join(_flatten_text(v, depth + 1) for v in node.values())
    if isinstance(node, (list, tuple)):
        return " ".join(_flatten_text(v, depth + 1) for v in node[:30])
    return ""


def detect_scheme(states: Mapping[str, Any]) -> str:
    """Infer the fulfilment scheme (FBO / FBS / RFBS) from the delivery widgets."""
    haystack = ""
    for key, state in states.items():
        if key.startswith(_DELIVERY_WIDGETS):
            haystack += " " + _flatten_text(state).lower()
        if isinstance(state, Mapping):
            for field in ("deliverySchema", "schema", "sellerType", "fulfillment"):
                value = state.get(field)
                if isinstance(value, str):
                    haystack += " " + value.lower()

    for hint, scheme in _SCHEME_HINTS:
        if hint in haystack:
            return scheme

    # Fall back on the delivery copy: Ozon's own warehouses read as "Ozon",
    # seller warehouses as "продавец".
    if "продавц" in haystack or "продавец" in haystack:
        return "FBS"
    if "ozon" in haystack or "озон" in haystack:
        return "FBO"
    return "UNKNOWN"


def extract_passive_stock(states: Mapping[str, Any]) -> int | None:
    """Read stock from the quantity picker limit or the «осталось N шт» label."""
    for key, state in states.items():
        if not isinstance(state, Mapping):
            continue
        if key.startswith(_CART_WIDGETS):
            for field in ("maxItems", "maxQuantity", "quantityMax", "availableQuantity"):
                value = _as_int(state.get(field))
                # The picker is capped at 999 on unlimited stock, so that value
                # tells us nothing about the real figure.
                if value and value < 999:
                    return value

    match = _REMAINING_RE.search(_flatten_text(states))
    return int(match.group(1)) if match else None


# =============================================================================
#  Snapshot history
# =============================================================================
class SnapshotStore:
    """Append-only JSONL history of stock snapshots, one file per SKU.

    Deliberately dependency-free so velocity works before Redis/Postgres are
    wired up; the interface is narrow enough to swap for a real store later.
    """

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, sku: str) -> Path:
        return self.directory / f"stock-{sku}.jsonl"

    def append(self, snapshot: StockSnapshot) -> None:
        line = snapshot.model_dump_json()
        with self._path(snapshot.sku).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def history(self, sku: str, limit: int | None = None) -> list[StockSnapshot]:
        path = self._path(sku)
        if not path.is_file():
            return []
        snapshots: list[StockSnapshot] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                snapshots.append(StockSnapshot.model_validate_json(line))
            except Exception:
                logger.debug("skipping malformed snapshot line for %s", sku)
        snapshots.sort(key=lambda s: s.captured_at)
        return snapshots[-limit:] if limit else snapshots


# =============================================================================
#  Velocity
# =============================================================================
def compute_velocity(snapshots: Sequence[StockSnapshot]) -> SalesVelocity | None:
    """Derive units/day from a stock series, separating restocks from sales."""
    usable = [s for s in snapshots if s.total_available is not None]
    if len(usable) < 2:
        return None

    usable = sorted(usable, key=lambda s: s.captured_at)
    units_sold = 0
    restock_events = 0
    restocked_units = 0

    # Deliberately unequal lengths — this is a pairwise walk, so strict=False.
    for previous, current in zip(usable, usable[1:], strict=False):
        delta = (previous.total_available or 0) - (current.total_available or 0)
        if delta > 0:
            units_sold += delta
        elif delta < 0:
            # Stock went up: a delivery landed. Sales inside that interval are
            # unobservable — counting the rise as negative sales would corrupt
            # the whole series, so it is recorded separately instead.
            restock_events += 1
            restocked_units += -delta

    window_seconds = (usable[-1].captured_at - usable[0].captured_at).total_seconds()
    window_hours = window_seconds / 3600
    if window_hours <= 0:
        return None

    units_per_day = units_sold / (window_hours / 24)

    if len(usable) >= 10 and window_hours >= 72:
        confidence = "high"
    elif len(usable) >= 4 and window_hours >= 24:
        confidence = "medium"
    else:
        confidence = "low"

    return SalesVelocity(
        sku=usable[0].sku,
        window_hours=round(window_hours, 2),
        units_sold=units_sold,
        units_per_day=round(units_per_day, 2),
        restock_events=restock_events,
        restocked_units=restocked_units,
        samples=len(usable),
        confidence=confidence,
    )


# =============================================================================
#  Scraper
# =============================================================================
class StockScraper:
    """Stock snapshots and sales-velocity estimation."""

    def __init__(
        self,
        requester: HybridRequester,
        settings: Settings | None = None,
        store: SnapshotStore | None = None,
    ) -> None:
        self.requester = requester
        self.settings = settings or get_settings()
        self.store = store or SnapshotStore(self.settings.session_dir / "stock")

    # ------------------------------------------------------------------
    async def snapshot(
        self,
        sku: str | int,
        *,
        region: Region | str | int = "moscow",
        use_cart_probe: bool = False,
        persist: bool = True,
    ) -> StockSnapshot:
        """Capture one stock reading for ``sku``."""
        sku = str(sku)
        target_region = region if isinstance(region, Region) else resolve_region(region)
        session_name = f"stock-{sku}"

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

        scheme = detect_scheme(states)
        available = extract_passive_stock(states)
        method = "passive_read"

        if available is None and use_cart_probe:
            available = await self._cart_probe(sku, session_name=session_name)
            method = "cart_probe"

        snapshot = StockSnapshot(
            sku=sku,
            captured_at=datetime.now(UTC),
            region_key=target_region.key,
            total_available=available,
            warehouses=[
                WarehouseStock(
                    scheme=scheme,
                    available=available,
                    warehouse_name=None,
                    is_estimate=True,
                )
            ],
            probe_quantity=self.settings.stock_probe_quantity if use_cart_probe else None,
            method=method,
        )
        if persist:
            self.store.append(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    async def _cart_probe(self, sku: str, *, session_name: str) -> int | None:
        """Ask for far more units than exist and read back the clamped quantity.

        Writes to a cart. Only reachable via ``use_cart_probe=True``.
        """
        quantity = self.settings.stock_probe_quantity
        logger.info("cart probe for %s with quantity=%d", sku, quantity)
        try:
            payload = await self.requester.get_json(
                f"/cart/add?id={sku}&quantity={quantity}",
                referer=OzonUrls.product_url(sku),
                page_type="cart",
                session_name=session_name,
                max_retries=2,
            )
        except Exception as exc:
            logger.warning("cart probe failed for %s: %s", sku, exc)
            return None

        states = parse_widget_states(payload)
        for state in states.values():
            if not isinstance(state, Mapping):
                continue
            for field in ("quantity", "maxQuantity", "availableQuantity", "count"):
                value = _as_int(state.get(field))
                if value and value < quantity:
                    return value
        return None

    # ------------------------------------------------------------------
    async def velocity(
        self,
        sku: str | int,
        *,
        region: Region | str | int = "moscow",
        capture_now: bool = True,
        history_limit: int | None = None,
    ) -> tuple[StockSnapshot | None, SalesVelocity | None]:
        """Take a reading (optionally) and compute velocity over stored history."""
        sku = str(sku)
        current: StockSnapshot | None = None
        if capture_now:
            current = await self.snapshot(sku, region=region)

        history = self.store.history(sku, limit=history_limit)
        return current, compute_velocity(history)

    # ------------------------------------------------------------------
    def history(self, sku: str | int, limit: int | None = None) -> list[StockSnapshot]:
        return self.store.history(str(sku), limit=limit)
