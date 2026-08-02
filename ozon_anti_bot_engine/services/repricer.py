"""Dynamic repricing engine and Ozon Seller API payload generator (spec §5.2).

Three strategies, one hard constraint. ``min_allowed_price`` is the
unit-economics floor and is enforced *after* every other calculation — a
strategy may propose whatever it likes, but a price below cost never leaves this
module. The floor is applied last precisely so that a future strategy cannot
accidentally route around it.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Iterable, Sequence
from typing import Any

from ..models import (
    CompetitorOffer,
    RepriceDecision,
    RepriceRequest,
    RepricerStrategy,
)

logger = logging.getLogger(__name__)

# Below this, a price change is not worth an API call or the ranking churn.
MIN_MEANINGFUL_DELTA = 0.01


class Repricer:
    """Computes target prices and renders Ozon Seller API update payloads."""

    def __init__(self, currency: str = "RUB") -> None:
        self.currency = currency

    # ------------------------------------------------------------------
    def calculate(self, request: RepriceRequest) -> RepriceDecision:
        """Run one repricing decision end to end."""
        offers = self._eligible_offers(request)
        decision = RepriceDecision(
            sku=request.sku,
            strategy=request.strategy,
            current_price=request.current_price,
            target_price=request.current_price,
            applied_price=request.current_price,
            delta_rub=0.0,
            delta_percent=0.0,
            competitors_considered=len(offers),
        )

        if not offers:
            decision.reason = "no eligible competitor offers; price held"
            return decision

        prices = sorted(offer.price for offer in offers)
        decision.market_min = prices[0]
        decision.market_median = round(statistics.median(prices), 2)

        target = self._target_price(request, prices, decision.market_median)
        target = self._round(target, request.round_to, request.strategy)
        decision.target_price = target

        applied = target
        reasons: list[str] = []

        # --- The floor. Applied last, on purpose. ---------------------
        if applied < request.min_allowed_price:
            applied = request.min_allowed_price
            decision.floor_hit = True
            reasons.append(
                f"target {target:.2f} below unit-economics floor "
                f"{request.min_allowed_price:.2f}; clamped"
            )

        if request.max_allowed_price is not None and applied > request.max_allowed_price:
            applied = request.max_allowed_price
            decision.ceiling_hit = True
            reasons.append(f"clamped to ceiling {request.max_allowed_price:.2f}")

        decision.applied_price = round(applied, 2)
        decision.delta_rub = round(decision.applied_price - request.current_price, 2)
        decision.delta_percent = (
            round(decision.delta_rub / request.current_price * 100, 2)
            if request.current_price
            else 0.0
        )
        decision.should_update = abs(decision.delta_rub) >= MIN_MEANINGFUL_DELTA

        if not decision.should_update:
            reasons.append("price already at target")
        else:
            direction = "cut" if decision.delta_rub < 0 else "raise"
            reasons.insert(
                0,
                f"{request.strategy.value}: {direction} to {decision.applied_price:.2f} "
                f"(market min {decision.market_min:.2f}, median {decision.market_median:.2f})",
            )
        decision.reason = "; ".join(reasons)

        if decision.should_update:
            decision.seller_api_payload = self.build_seller_payload(request, decision)

        logger.info(
            "repricer %s: %.2f -> %.2f (%s)",
            request.sku, request.current_price, decision.applied_price, decision.reason,
        )
        return decision

    # ------------------------------------------------------------------
    def _eligible_offers(self, request: RepriceRequest) -> list[CompetitorOffer]:
        return [
            offer
            for offer in request.competitors
            # An out-of-stock rival is not competing for the buy box; matching it
            # gives away margin to a listing nobody can order.
            if (offer.in_stock or request.include_out_of_stock)
            and offer.price > 0
            and offer.sku != request.sku
        ]

    def _target_price(
        self, request: RepriceRequest, prices: Sequence[float], median: float
    ) -> float:
        match request.strategy:
            case RepricerStrategy.UNDERCUT_LOWEST:
                return prices[0] - request.undercut_by
            case RepricerStrategy.MATCH_LOWEST:
                return prices[0]
            case RepricerStrategy.MATCH_AVERAGE:
                return median
        raise ValueError(f"Unhandled strategy: {request.strategy}")

    @staticmethod
    def _round(price: float, step: float, strategy: RepricerStrategy) -> float:
        if step <= 0:
            return round(price, 2)
        if strategy is RepricerStrategy.UNDERCUT_LOWEST:
            # Round *down* — rounding up could land back above the competitor
            # and silently defeat the undercut.
            return round(math.floor(price / step) * step, 2)
        return round(round(price / step) * step, 2)

    # ------------------------------------------------------------------
    def build_seller_payload(
        self, request: RepriceRequest, decision: RepriceDecision
    ) -> dict[str, Any]:
        """Render the body for ``POST /v1/product/import/prices``.

        The Seller API takes prices as *strings*; sending numbers is rejected.
        ``min_price`` is forwarded so Ozon's own auto-actions cannot discount the
        item below the floor either.
        """
        item: dict[str, Any] = {
            "price": f"{decision.applied_price:.2f}",
            "min_price": f"{request.min_allowed_price:.2f}",
            "currency_code": self.currency,
            # Auto-actions can move the price after we set it; leave them off so
            # the floor stays authoritative.
            "auto_action_enabled": "DISABLED",
        }
        if request.offer_id:
            item["offer_id"] = request.offer_id
        else:
            item["product_id"] = int(request.sku) if request.sku.isdigit() else request.sku

        old_price = request.old_price
        if old_price is not None and old_price > decision.applied_price:
            item["old_price"] = f"{old_price:.2f}"
        else:
            # A stale old_price below the new price renders as a negative
            # discount on the card; "0" clears it.
            item["old_price"] = "0"

        return {
            "endpoint": "/v1/product/import/prices",
            "method": "POST",
            "body": {"prices": [item]},
        }

    # ------------------------------------------------------------------
    def calculate_many(self, requests: Iterable[RepriceRequest]) -> list[RepriceDecision]:
        return [self.calculate(request) for request in requests]

    def merge_payloads(self, decisions: Sequence[RepriceDecision]) -> dict[str, Any]:
        """Batch every actionable decision into one Seller API call.

        Ozon accepts up to 1000 items per request; callers pushing more should
        chunk before calling this.
        """
        items = [
            decision.seller_api_payload["body"]["prices"][0]
            for decision in decisions
            if decision.should_update and decision.seller_api_payload
        ]
        return {
            "endpoint": "/v1/product/import/prices",
            "method": "POST",
            "body": {"prices": items},
            "item_count": len(items),
        }
