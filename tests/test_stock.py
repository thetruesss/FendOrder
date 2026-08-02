"""Stock estimation and sales velocity (spec §4.4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ozon_anti_bot_engine.core.requester import parse_widget_states
from ozon_anti_bot_engine.models import StockSnapshot
from ozon_anti_bot_engine.scrapers.stock_scraper import (
    SnapshotStore,
    compute_velocity,
    detect_scheme,
    extract_passive_stock,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _snapshot(hours: float, available: int | None, sku: str = "1234567") -> StockSnapshot:
    return StockSnapshot(
        sku=sku,
        captured_at=T0 + timedelta(hours=hours),
        region_key="moscow",
        total_available=available,
    )


# =============================================================================
#  Passive stock reading
# =============================================================================
def _states(widgets: dict[str, dict]) -> dict:
    return parse_widget_states(
        {"widgetStates": {k: json.dumps(v) for k, v in widgets.items()}}
    )


def test_quantity_picker_limit_is_read_as_stock() -> None:
    states = _states({"webAddToCart-1": {"maxItems": 37}})
    assert extract_passive_stock(states) == 37


def test_the_999_cap_is_not_mistaken_for_real_stock() -> None:
    # Ozon caps the picker at 999 on unlimited stock; that number means nothing.
    assert extract_passive_stock(_states({"webAddToCart-1": {"maxItems": 999}})) is None


def test_remaining_label_is_parsed() -> None:
    states = _states({"webAddToCart-1": {"subtitle": "Осталось 4 шт."}})
    assert extract_passive_stock(states) == 4


def test_no_stock_signal_returns_none() -> None:
    assert extract_passive_stock(_states({"webPrice-1": {"price": "100 ₽"}})) is None


# =============================================================================
#  Fulfilment scheme
# =============================================================================
@pytest.mark.parametrize(
    ("widget", "expected"),
    [
        ({"webCurrentSeller-1": {"deliverySchema": "FBO"}}, "FBO"),
        ({"webCurrentSeller-1": {"deliverySchema": "FBS"}}, "FBS"),
        ({"webCurrentSeller-1": {"deliverySchema": "rFBS"}}, "FBS"),
        ({"webDeliveryDetails-1": {"text": "Доставка Ozon"}}, "FBO"),
        ({"webDeliveryDetails-1": {"text": "Отгружает продавец"}}, "FBS"),
        ({"webPrice-1": {"price": "1 ₽"}}, "UNKNOWN"),
    ],
)
def test_scheme_detection(widget: dict, expected: str) -> None:
    assert detect_scheme(_states(widget)) == expected


# =============================================================================
#  Velocity
# =============================================================================
def test_velocity_is_the_negative_derivative_of_stock() -> None:
    velocity = compute_velocity([_snapshot(0, 100), _snapshot(24, 80)])
    assert velocity is not None
    assert velocity.units_sold == 20
    assert velocity.units_per_day == 20.0
    assert velocity.window_hours == 24.0
    assert velocity.restock_events == 0


def test_restocks_are_recorded_separately_not_as_negative_sales() -> None:
    velocity = compute_velocity(
        [_snapshot(0, 100), _snapshot(24, 80), _snapshot(48, 120), _snapshot(72, 100)]
    )
    assert velocity is not None
    # 20 sold, then +40 delivered, then 20 sold. Naive differencing would
    # report -20 net and a nonsensical negative velocity.
    assert velocity.units_sold == 40
    assert velocity.restock_events == 1
    assert velocity.restocked_units == 40
    assert velocity.units_per_day == pytest.approx(13.33, abs=0.01)


def test_snapshots_are_sorted_before_differencing() -> None:
    scrambled = [_snapshot(48, 60), _snapshot(0, 100), _snapshot(24, 80)]
    velocity = compute_velocity(scrambled)
    assert velocity is not None
    assert velocity.units_sold == 40


def test_snapshots_without_a_reading_are_ignored() -> None:
    velocity = compute_velocity([_snapshot(0, 100), _snapshot(12, None), _snapshot(24, 80)])
    assert velocity is not None
    assert velocity.samples == 2
    assert velocity.units_sold == 20


def test_a_single_sample_cannot_produce_a_rate() -> None:
    assert compute_velocity([_snapshot(0, 100)]) is None
    assert compute_velocity([]) is None


def test_zero_length_window_is_rejected() -> None:
    assert compute_velocity([_snapshot(0, 100), _snapshot(0, 80)]) is None


@pytest.mark.parametrize(
    ("count", "hours", "expected"),
    [(2, 6, "low"), (4, 24, "medium"), (12, 96, "high")],
)
def test_confidence_scales_with_samples_and_window(
    count: int, hours: float, expected: str
) -> None:
    step = hours / (count - 1)
    snapshots = [_snapshot(i * step, 1000 - i) for i in range(count)]
    velocity = compute_velocity(snapshots)
    assert velocity is not None
    assert velocity.confidence == expected


# =============================================================================
#  Snapshot store
# =============================================================================
def test_store_round_trips_snapshots(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    store.append(_snapshot(0, 100))
    store.append(_snapshot(24, 80))

    history = store.history("1234567")
    assert [s.total_available for s in history] == [100, 80]
    assert compute_velocity(history).units_sold == 20


def test_store_isolates_skus(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    store.append(_snapshot(0, 100, sku="111"))
    store.append(_snapshot(0, 50, sku="222"))
    assert len(store.history("111")) == 1
    assert store.history("222")[0].total_available == 50


def test_unknown_sku_has_an_empty_history(tmp_path) -> None:
    assert SnapshotStore(tmp_path).history("does-not-exist") == []


def test_corrupt_lines_are_skipped(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    store.append(_snapshot(0, 100))
    with (tmp_path / "stock-1234567.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n\n")
    assert len(store.history("1234567")) == 1


def test_history_limit_keeps_the_most_recent(tmp_path) -> None:
    store = SnapshotStore(tmp_path)
    for hour in range(5):
        store.append(_snapshot(hour, 100 - hour))
    recent = store.history("1234567", limit=2)
    assert [s.total_available for s in recent] == [97, 96]
