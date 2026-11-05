from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, SampleThresholds
from f1_strategist.metrics.fuel import FuelMetrics
from f1_strategist.metrics.history import LapHistory, LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import CarSnapshot, RaceSessionSnapshot, RaceState


def _record(
    lap: int,
    start: float,
    end: float,
    *,
    flags: frozenset[str] = frozenset(),
    closed: bool = True,
    track_status: frozenset[str] = frozenset(),
    compound: str | None = None,
    pit_exit: bool = False,
    tyre_age_start: int | None = None,
    tyre_age_end: int | None = None,
) -> LapRecord:
    return LapRecord(
        car_index=0,
        lap_number=lap,
        fuel_start_kg=start,
        fuel_end_kg=end,
        closed=closed,
        flags=flags,
        track_status_seen=track_status,
        compound=compound,
        pit_exit=pit_exit,
        tyre_age_start=tyre_age_start,
        tyre_age_end=tyre_age_end,
    )


def _inputs(
    records: tuple[LapRecord, ...] = (),
    *,
    car: CarSnapshot | None = None,
    total_laps: int | None = 10,
    now: float = 10.0,
    stale_group: str | None = None,
    fuel_window_laps: int = 5,
) -> MetricInputs:
    snapshot = RaceState().snapshot()
    cars = list(snapshot.cars)
    if car is not None:
        cars[car.car_index] = car
    freshness = tuple(
        replace(item, updated_at_monotonic=0.0) if item.group == stale_group else item
        for item in snapshot.freshness
    )
    state = replace(
        snapshot,
        cars=tuple(cars),
        freshness=freshness,
        session=RaceSessionSnapshot(
            uid=1,
            session_type="race",
            track="track",
            total_laps=total_laps,
            started=True,
            ended=False,
            track_length_m=5000.0,
        ),
    )
    return MetricInputs(
        state,
        LapHistorySnapshot(records={0: records}),
        MetricsConfig(fuel_window_laps=fuel_window_laps),
        now,
        0,
    )


def _metrics(inputs: MetricInputs):
    return {metric.key: metric for metric in FuelMetrics().compute(inputs)}


def test_constant_fuel_consumption_and_finish_margins() -> None:
    inputs = _inputs(
        tuple(_record(lap, 40.0 - 2 * (lap - 1), 38.0 - 2 * (lap - 1)) for lap in range(1, 4)),
        car=CarSnapshot(
            car_index=0,
            current_lap=6,
            lap_distance_m=1000.0,
            fuel_kg=30.0,
            fuel_remaining_laps=14.0,
        ),
    )
    metrics = _metrics(inputs)
    assert metrics["fuel.in_tank_kg"].value == 30.0
    assert metrics["fuel.consumption_per_lap"].value == 2.0
    assert metrics["fuel.consumption_stddev"].value == 0.0
    assert metrics["fuel.consumption_per_lap"].quality is Quality.OK
    assert metrics["fuel.laps_of_fuel"].value == 15.0
    assert metrics["fuel.margin_laps_at_finish"].value == 10.2
    assert metrics["fuel.margin_kg_at_finish"].value == 20.4
    assert metrics["fuel.game_remaining_laps"].value == 14.0
    assert metrics["fuel.game_deviation_laps"].value == pytest.approx(-3.8)


def test_fuel_uses_last_window_including_safety_car_and_reports_comparable_count() -> None:
    records = (
        _record(2, 30, 20),
        _record(3, 20, 18, track_status=frozenset({"full_safety_car"})),
        _record(4, 18, 14),
        _record(5, 14, 8, flags=frozenset({"partial_lap"})),
        _record(6, 8, 4, flags=frozenset({"incomplete"})),
        _record(7, 4, 0, flags=frozenset({"retired"})),
        _record(8, 5, 3, closed=False),
    )
    metrics = _metrics(
        _inputs(
            records,
            car=CarSnapshot(car_index=0, fuel_kg=20.0),
            fuel_window_laps=3,
        )
    )
    assert metrics["fuel.consumption_per_lap"].value == 16 / 3
    assert metrics["fuel.consumption_per_lap"].sample_count == 3
    assert metrics["fuel.comparable_laps_count"].value == 2
    assert metrics["fuel.comparable_laps_count"].sample_count == 3


def test_fuel_consumption_uses_the_latest_stint_and_compound() -> None:
    records = (
        _record(2, 50.0, 49.0, compound="soft"),
        _record(3, 49.0, 48.0, compound="soft"),
        _record(4, 48.0, 45.0, compound="medium"),
        _record(5, 45.0, 42.0, compound="medium"),
        _record(6, 42.0, 39.0, compound="medium"),
    )

    metrics = _metrics(_inputs(records, car=CarSnapshot(car_index=0, fuel_kg=30.0)))

    assert metrics["fuel.consumption_per_lap"].value == 3.0
    assert metrics["fuel.consumption_per_lap"].sample_count == 3
    assert metrics["fuel.comparable_laps_count"].value == 5
    assert metrics["fuel.comparable_laps_count"].sample_count == 5


def test_fuel_falls_back_to_one_complete_prior_stint_after_a_pit_stop() -> None:
    records = (
        _record(1, 50.0, 48.0, compound="soft"),
        _record(2, 48.0, 46.0, compound="soft"),
        _record(3, 46.0, 44.0, compound="soft"),
        _record(
            4,
            44.0,
            43.0,
            compound="medium",
            pit_exit=True,
            flags=frozenset({"partial_lap"}),
        ),
        _record(5, 43.0, 41.0, compound="medium", tyre_age_start=1, tyre_age_end=2),
    )
    inputs = _inputs(
        records,
        car=CarSnapshot(
            car_index=0,
            current_lap=6,
            lap_distance_m=1000.0,
            fuel_kg=30.0,
            actual_tyre_compound="medium",
            tyre_age_laps=2,
        ),
    )

    metrics = _metrics(inputs)

    assert metrics["fuel.consumption_per_lap"].value == 2.0
    assert metrics["fuel.consumption_per_lap"].sample_count == 3
    assert metrics["fuel.consumption_per_lap"].quality is Quality.LOW
    assert metrics["fuel.consumption_per_lap"].notes == ("fuel_previous_stint_fallback",)
    assert metrics["fuel.margin_laps_at_finish"].value is not None
    assert metrics["fuel.margin_laps_at_finish"].quality is Quality.LOW


def test_official_timing_does_not_make_partial_fuel_sample_eligible() -> None:
    history = LapHistory()
    history.upsert(_record(1, 40.0, 39.0, flags=frozenset({"partial_lap"}), compound="soft"))
    history.upsert(_record(2, 39.0, 37.0, compound="soft"))
    history.upsert(_record(3, 37.0, 35.0, compound="soft"))
    history.update_lap_timing(
        0,
        1,
        lap_time_s=90.0,
        sector1_s=None,
        sector2_s=None,
        sector3_s=None,
        valid=True,
    )
    inputs = _inputs(
        car=CarSnapshot(car_index=0, current_lap=4, fuel_kg=30.0, actual_tyre_compound="soft")
    )
    inputs = replace(inputs, history=history.snapshot())

    metrics = _metrics(inputs)

    timed_partial = history.get(0, 1)
    assert timed_partial is not None
    assert "telemetry_partial" in timed_partial.flags
    assert "partial_lap" not in timed_partial.flags
    assert metrics["fuel.consumption_per_lap"].value == 2.0
    assert metrics["fuel.consumption_per_lap"].sample_count == 2


def test_fuel_excludes_partial_pit_exit_before_finding_same_compound_stint() -> None:
    records = (
        _record(1, 60.0, 58.0, compound="soft"),
        _record(2, 58.0, 56.0, compound="soft"),
        _record(3, 56.0, 54.0, compound="soft"),
        _record(
            4,
            54.0,
            53.0,
            compound="soft",
            pit_exit=True,
            flags=frozenset({"partial_lap"}),
        ),
        _record(5, 53.0, 52.0, compound="soft", tyre_age_start=1, tyre_age_end=2),
        _record(6, 52.0, 51.0, compound="soft", tyre_age_start=2, tyre_age_end=3),
    )

    metrics = _metrics(
        _inputs(
            records,
            car=CarSnapshot(
                car_index=0,
                current_lap=7,
                fuel_kg=30.0,
                actual_tyre_compound="soft",
                tyre_age_laps=3,
            ),
        )
    )

    assert metrics["fuel.consumption_per_lap"].value == 1.0
    assert metrics["fuel.consumption_per_lap"].sample_count == 2
    assert metrics["fuel.comparable_laps_count"].value == 4
    assert metrics["fuel.comparable_laps_count"].sample_count == 5


def test_fuel_requires_two_closed_samples_and_handles_missing_data() -> None:
    one_sample = _metrics(
        _inputs(
            (_record(2, 20, 18),),
            car=CarSnapshot(car_index=0, fuel_kg=20.0),
        )
    )
    assert one_sample["fuel.consumption_per_lap"].quality is Quality.INSUFFICIENT
    assert one_sample["fuel.consumption_per_lap"].sample_count == 1
    assert one_sample["fuel.consumption_stddev"].quality is Quality.INSUFFICIENT
    assert one_sample["fuel.game_remaining_laps"].quality is Quality.UNAVAILABLE

    no_data = _metrics(_inputs(car=CarSnapshot(car_index=0, current_lap=1, lap_distance_m=0.0)))
    assert no_data["fuel.in_tank_kg"].quality is Quality.UNAVAILABLE
    assert no_data["fuel.consumption_per_lap"].quality is Quality.INSUFFICIENT
    assert no_data["fuel.margin_laps_at_finish"].quality is Quality.INSUFFICIENT


def test_fuel_requires_the_configured_low_sample_count() -> None:
    inputs = _inputs(
        (_record(1, 20, 18), _record(2, 18, 16)),
        car=CarSnapshot(car_index=0, fuel_kg=20.0),
    )
    inputs = replace(
        inputs,
        config=MetricsConfig(
            fuel_window_laps=5,
            min_samples={"fuel": SampleThresholds(low=3, ok=5)},
        ),
    )

    metrics = _metrics(inputs)

    assert metrics["fuel.consumption_per_lap"].quality is Quality.INSUFFICIENT
    assert metrics["fuel.consumption_per_lap"].value is None
    assert metrics["fuel.consumption_stddev"].quality is Quality.INSUFFICIENT


def test_fuel_variable_consumption_and_stale_status() -> None:
    metrics = _metrics(
        _inputs(
            (_record(2, 20, 18), _record(3, 18, 14), _record(4, 14, 8)),
            car=CarSnapshot(car_index=0, fuel_kg=15.0),
            stale_group="car_status",
        )
    )
    assert metrics["fuel.consumption_per_lap"].value == 4.0
    assert isinstance(metrics["fuel.consumption_stddev"].value, float)
    assert metrics["fuel.consumption_stddev"].value > 0
    assert metrics["fuel.consumption_per_lap"].quality is Quality.STALE
    assert metrics["fuel.in_tank_kg"].quality is Quality.STALE


def test_fuel_margins_are_unavailable_without_laps_remaining() -> None:
    metrics = _metrics(
        _inputs(
            (_record(2, 20, 18), _record(3, 18, 16)),
            car=CarSnapshot(car_index=0, fuel_kg=20.0),
            total_laps=None,
        )
    )
    assert metrics["fuel.margin_laps_at_finish"].quality is Quality.UNAVAILABLE
    assert metrics["fuel.margin_kg_at_finish"].quality is Quality.UNAVAILABLE


def test_fuel_margin_uses_session_freshness_without_staling_tank_metric() -> None:
    inputs = _inputs(
        (_record(2, 20, 18), _record(3, 18, 16), _record(4, 16, 14)),
        car=CarSnapshot(car_index=0, current_lap=5, lap_distance_m=1000, fuel_kg=10),
        stale_group="session",
    )

    metrics = _metrics(inputs)

    assert metrics["fuel.margin_laps_at_finish"].quality is Quality.STALE
    assert metrics["fuel.margin_kg_at_finish"].quality is Quality.STALE
    assert metrics["fuel.in_tank_kg"].quality is Quality.OK
