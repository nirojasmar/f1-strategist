from __future__ import annotations

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.ers import ERSMetrics
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import (
    CarSnapshot,
    PacketFreshness,
    RaceSessionSnapshot,
    RaceStateSnapshot,
)


def _inputs(
    *,
    car: CarSnapshot | None = None,
    records: tuple[LapRecord, ...] = (),
    capacity: float | None = 4_000_000.0,
    stale: bool = False,
) -> MetricInputs:
    now = 100.0
    state = RaceStateSnapshot(
        session=RaceSessionSnapshot(1, "race", "track", 10, True, False),
        current_lap=4,
        player_car_index=0 if car is not None else None,
        cars=(car,) if car is not None else (),
        freshness=(PacketFreshness("car_status", now - (5.0 if stale else 0.1), 2.0),),
        events=(),
    )
    history = LapHistorySnapshot(records={0: records} if records else {})
    return MetricInputs(
        state=state,
        history=history,
        config=MetricsConfig(ers_capacity_joules=capacity),
        now=now,
        car_index=0,
    )


def _metric(inputs: MetricInputs, key: str):
    return {metric.key: metric for metric in ERSMetrics().compute(inputs)}[key]


def test_current_ers_state_and_lap_totals() -> None:
    car = CarSnapshot(
        car_index=0,
        ers_store_j=2_000_000.0,
        ers_deploy_mode="overtake",
    )
    records = tuple(
        LapRecord(
            car_index=0,
            lap_number=lap,
            ers_harvested_j=1000.0 * lap,
            ers_deployed_j=500.0 * lap,
            closed=True,
        )
        for lap in range(1, 4)
    )

    metrics = {
        metric.key: metric for metric in ERSMetrics().compute(_inputs(car=car, records=records))
    }

    assert metrics["ers.store_j"].value == 2_000_000.0
    assert metrics["ers.capacity_j"].value == 4_000_000.0
    assert metrics["ers.store_pct"].value == 50.0
    assert metrics["ers.deploy_mode"].value == "overtake"
    assert metrics["ers.harvested_last_lap_j"].value == 3000.0
    assert metrics["ers.deployed_last_lap_j"].value == 1500.0
    assert metrics["ers.net_last_lap_j"].value == 1500.0
    assert metrics["ers.net_mean_3_laps_j"].value == 1000.0
    assert metrics["ers.net_mean_3_laps_j"].quality is Quality.OK


def test_store_percentage_is_unavailable_without_capacity() -> None:
    metric = _metric(
        _inputs(car=CarSnapshot(car_index=0, ers_store_j=2_000_000.0), capacity=None),
        "ers.store_pct",
    )

    assert metric.value is None
    assert metric.quality is Quality.UNAVAILABLE
    assert metric.notes == ("ers_capacity_not_configured",)
    capacity = _metric(
        _inputs(car=CarSnapshot(car_index=0, ers_store_j=2_000_000.0), capacity=None),
        "ers.capacity_j",
    )
    assert capacity.value is None
    assert capacity.quality is Quality.UNAVAILABLE


def test_incomplete_lap_history_is_low_quality_and_missing_state_is_unavailable() -> None:
    record = LapRecord(
        car_index=0,
        lap_number=1,
        ers_harvested_j=1000.0,
        ers_deployed_j=250.0,
        closed=True,
    )
    inputs = _inputs(records=(record,))

    assert _metric(inputs, "ers.net_mean_3_laps_j").quality is Quality.LOW
    assert _metric(inputs, "ers.net_last_lap_j").quality is Quality.OK

    missing = _metric(_inputs(), "ers.store_j")
    assert missing.value is None
    assert missing.quality is Quality.UNAVAILABLE


def test_ers_lap_totals_skip_open_partial_incomplete_and_retired_records() -> None:
    records = (
        LapRecord(
            car_index=0,
            lap_number=1,
            ers_harvested_j=1000.0,
            ers_deployed_j=500.0,
            closed=True,
        ),
        LapRecord(
            car_index=0,
            lap_number=2,
            ers_harvested_j=5000.0,
            ers_deployed_j=0.0,
            closed=True,
            flags=frozenset({"incomplete"}),
        ),
        LapRecord(
            car_index=0,
            lap_number=3,
            ers_harvested_j=7000.0,
            ers_deployed_j=0.0,
            closed=False,
        ),
    )

    assert _metric(_inputs(records=records), "ers.net_last_lap_j").value == 500.0
    assert _metric(_inputs(records=records), "ers.net_mean_3_laps_j").value == 500.0


def test_stale_live_and_history_values_are_marked_stale() -> None:
    record = LapRecord(
        car_index=0,
        lap_number=1,
        ers_harvested_j=1000.0,
        ers_deployed_j=250.0,
        closed=True,
    )
    inputs = _inputs(
        car=CarSnapshot(car_index=0, ers_store_j=1_000_000.0),
        records=(record,),
        stale=True,
    )

    assert _metric(inputs, "ers.store_j").quality is Quality.STALE
    assert _metric(inputs, "ers.net_last_lap_j").quality is Quality.STALE
