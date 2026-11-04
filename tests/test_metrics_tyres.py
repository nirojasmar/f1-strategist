from __future__ import annotations

from dataclasses import replace

from f1_strategist.config import MetricsConfig, SampleThresholds
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs, TyreStint
from f1_strategist.metrics.types import Quality
from f1_strategist.metrics.tyres import TyreMetrics
from f1_strategist.race_state import (
    CarDamageSnapshot,
    CarSnapshot,
    FittedTyreSnapshot,
    RaceState,
)


def _damage(wear: tuple[float, ...] = (30.0, 40.0, 50.0, 60.0)) -> CarDamageSnapshot:
    return CarDamageSnapshot(
        tyres_wear=wear,
        tyres_damage=(1, 4, 2, 3),
        brakes_damage=(0, 0, 0, 0),
        tyre_blisters=(2, 5, 1, 3),
        front_left_wing_damage=0,
        front_right_wing_damage=0,
        rear_wing_damage=0,
        floor_damage=0,
        diffuser_damage=0,
        sidepod_damage=0,
        drs_fault=None,
        ers_fault=None,
        gearbox_damage=0,
        engine_damage=0,
        engine_mguh_wear=0,
        engine_es_wear=0,
        engine_ce_wear=0,
        engine_ice_wear=0,
        engine_mguk_wear=0,
        engine_tc_wear=0,
        engine_blown=None,
        engine_seized=None,
    )


def _record(
    lap: int,
    compound: str,
    age: int,
    wear_mean: float,
    *,
    flags: frozenset[str] = frozenset(),
) -> LapRecord:
    return LapRecord(
        car_index=0,
        lap_number=lap,
        compound=compound,
        tyre_age_start=age - 1,
        tyre_age_end=age,
        tyre_wear_end_pct=(wear_mean - 3, wear_mean - 1, wear_mean + 1, wear_mean + 3),
        closed=True,
        flags=flags,
    )


def _inputs(
    *,
    records: tuple[LapRecord, ...] = (),
    stints: tuple[TyreStint, ...] = (),
    car: CarSnapshot | None = None,
    config: MetricsConfig | None = None,
    now: float = 10.0,
    stale_group: str | None = None,
) -> MetricInputs:
    snapshot = RaceState().snapshot()
    cars = list(snapshot.cars)
    if car is not None:
        cars[car.car_index] = car
    freshness = tuple(
        replace(item, updated_at_monotonic=0.0) if item.group == stale_group else item
        for item in snapshot.freshness
    )
    state = replace(snapshot, cars=tuple(cars), freshness=freshness)
    return MetricInputs(
        state,
        LapHistorySnapshot(records={0: records}, tyre_stints={0: stints}),
        config or MetricsConfig(),
        now,
        0,
    )


def _metrics(inputs: MetricInputs):
    return {metric.key: metric for metric in TyreMetrics().compute(inputs)}


def test_tyre_snapshot_reports_each_wheel_in_damage_tuple_order() -> None:
    car = CarSnapshot(
        car_index=0,
        actual_tyre_compound="soft",
        tyre_age_laps=3,
        damage=_damage(),
        fitted_tyre=FittedTyreSnapshot("soft", "soft", 50.0, 18, 9),
    )
    metrics = _metrics(_inputs(car=car))
    assert [metrics[f"tyres.wear_wheel_{index}_pct"].value for index in range(1, 5)] == [
        30.0,
        40.0,
        50.0,
        60.0,
    ]
    assert metrics["tyres.compound"].value == "soft"
    assert metrics["tyres.age_laps"].value == 3
    assert metrics["tyres.wear_mean_pct"].value == 45.0
    assert metrics["tyres.wear_max_pct"].value == 60.0
    assert metrics["tyres.max_blister_pct"].value == 5
    assert metrics["tyres.max_damage_pct"].value == 4
    assert metrics["tyres.game_lifespan_laps"].value == 9


def test_tyre_regression_uses_only_clean_samples_in_current_stint() -> None:
    records = (
        _record(1, "medium", 1, 1),
        _record(2, "medium", 2, 100),
        _record(3, "medium", 3, 200),
        _record(4, "soft", 1, 10),
        _record(5, "soft", 2, 20),
        _record(6, "soft", 3, 30),
        _record(7, "soft", 4, 90, flags=frozenset({"partial_lap"})),
        _record(8, "soft", 5, 100, flags=frozenset({"incomplete"})),
        _record(9, "soft", 6, 110, flags=frozenset({"retired"})),
    )
    car = CarSnapshot(
        car_index=0,
        actual_tyre_compound="soft",
        tyre_age_laps=6,
        damage=_damage(),
    )
    metrics = _metrics(
        _inputs(
            records=records,
            stints=(TyreStint("medium", 1, 3), TyreStint("soft", 4)),
            car=car,
            config=MetricsConfig(tyre_wear_limit_pct=65.0),
        )
    )
    assert metrics["tyres.wear_rate_pct_per_lap"].value == 10.0
    assert metrics["tyres.wear_rate_pct_per_lap"].sample_count == 3
    assert metrics["tyres.laps_to_wear_limit"].value == 2.0


def test_tyre_regression_requires_the_configured_low_sample_count() -> None:
    car = CarSnapshot(car_index=0, actual_tyre_compound="soft", tyre_age_laps=4, damage=_damage())
    inputs = _inputs(
        records=(_record(2, "soft", 2, 10), _record(3, "soft", 3, 20), _record(4, "soft", 4, 30)),
        car=car,
        config=MetricsConfig(min_samples={"tyres": SampleThresholds(low=5, ok=6)}),
    )

    metrics = _metrics(inputs)

    assert metrics["tyres.wear_rate_pct_per_lap"].quality is Quality.INSUFFICIENT
    assert metrics["tyres.wear_rate_pct_per_lap"].value is None
    assert metrics["tyres.wear_rate_pct_per_lap"].sample_count == 3


def test_tyre_outputs_insufficient_or_unavailable_values_as_appropriate() -> None:
    car = CarSnapshot(car_index=0, actual_tyre_compound="soft", tyre_age_laps=1)
    metrics = _metrics(_inputs(car=car))
    assert metrics["tyres.wear_rate_pct_per_lap"].quality is Quality.INSUFFICIENT
    assert metrics["tyres.laps_to_wear_limit"].quality is Quality.UNAVAILABLE
    assert metrics["tyres.compound"].quality is Quality.OK
    assert metrics["tyres.wear_mean_pct"].quality is Quality.UNAVAILABLE


def test_tyre_metrics_mark_stale_sources_and_missing_stint_samples() -> None:
    car = CarSnapshot(car_index=0, actual_tyre_compound="soft", tyre_age_laps=2, damage=_damage())
    metrics = _metrics(
        _inputs(
            records=(_record(2, "soft", 1, 10), _record(3, "soft", 2, 20)),
            stints=(TyreStint("soft", 2),),
            car=car,
            stale_group="car_damage",
        )
    )
    assert metrics["tyres.wear_mean_pct"].quality is Quality.STALE
    assert metrics["tyres.wear_rate_pct_per_lap"].quality is Quality.INSUFFICIENT
    assert metrics["tyres.game_lifespan_laps"].quality is Quality.UNAVAILABLE
