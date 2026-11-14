from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.metrics.pit import PitMetrics
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import CarSnapshot, PacketFreshness, RaceState


def _inputs(
    records: dict[int, tuple[LapRecord, ...]],
    *,
    car: CarSnapshot | None = None,
    session_fields: dict | None = None,
    freshness: tuple[PacketFreshness, ...] | None = None,
) -> MetricInputs:
    state = RaceState().snapshot()
    cars = list(state.cars)
    if car is not None:
        cars[car.car_index] = car
    session = replace(state.session, **(session_fields or {}))
    state = replace(
        state,
        cars=tuple(cars),
        session=session,
        freshness=freshness if freshness is not None else state.freshness,
    )
    return MetricInputs(state, LapHistorySnapshot(records=records), MetricsConfig(), 10.0, 0)


def _event_records(
    car_index: int,
    *,
    safety_car: bool = False,
    with_reference: bool = True,
) -> tuple[LapRecord, ...]:
    track = frozenset({"full_safety_car"}) if safety_car else frozenset()
    return (
        LapRecord(
            car_index,
            1,
            lap_time_s=90.0,
            valid=True,
            closed=True,
            flags=frozenset({"first_lap"}),
        ),
        LapRecord(
            car_index,
            2,
            lap_time_s=100.0,
            valid=True,
            pit_entry=True,
            pit_lane_time_s=25.0,
            pit_stop_time_s=2.5,
            track_status_seen=track,
            closed=True,
        ),
        LapRecord(
            car_index,
            3,
            lap_time_s=100.0,
            valid=True,
            pit_exit=True,
            track_status_seen=track,
            closed=True,
        ),
        *(
            (
                LapRecord(car_index, 4, lap_time_s=90.0, valid=True, closed=True),
                LapRecord(car_index, 5, lap_time_s=90.0, valid=True, closed=True),
            )
            if with_reference
            else (
                LapRecord(
                    car_index,
                    4,
                    lap_time_s=90.0,
                    valid=True,
                    flags=frozenset({"incomplete"}),
                    closed=True,
                ),
                LapRecord(
                    car_index,
                    5,
                    lap_time_s=90.0,
                    valid=True,
                    flags=frozenset({"partial_lap"}),
                    closed=True,
                ),
            )
        ),
    )


def _metric_map(metrics):
    return {metric.key: metric for metric in metrics}


def test_pit_loss_uses_median_clean_reference_near_each_stop() -> None:
    records = {index: _event_records(index) for index in range(3)}
    metrics = _metric_map(
        PitMetrics().compute(
            _inputs(
                records,
                car=CarSnapshot(0, pit_stops=1),
                session_fields={
                    "pit_window_ideal_lap": 12,
                    "pit_window_latest_lap": 18,
                    "pit_rejoin_position": 4,
                },
            )
        )
    )

    assert metrics["pit.loss_estimate_s"].value == 20.0
    assert metrics["pit.loss_estimate_s"].quality is Quality.LOW
    assert metrics["pit.sample_count"].value == 3
    assert metrics["pit.sample_count"].quality is Quality.LOW
    assert metrics["pit.stops_done"].value == 1
    assert metrics["pit.last_lane_time_s"].value == 25.0
    assert metrics["pit.last_stationary_s"].value == 2.5
    assert metrics["pit.window_ideal_lap"].value == 12
    assert metrics["pit.window_latest_lap"].value == 18
    assert metrics["pit.rejoin_position"].value == 4


def test_pit_loss_omits_safety_car_events_and_stops_without_clean_reference() -> None:
    inputs = _inputs(
        {
            0: _event_records(0, safety_car=True),
            1: _event_records(1, with_reference=False),
        },
        car=CarSnapshot(0, pit_stops=1),
    )
    metrics = _metric_map(PitMetrics().compute(inputs))

    assert metrics["pit.loss_estimate_s"].quality is Quality.INSUFFICIENT
    assert metrics["pit.loss_estimate_s"].value is None
    assert metrics["pit.sample_count"].value == 0


def test_pit_missing_game_data_and_missing_stop_data_are_explicit() -> None:
    metrics = _metric_map(PitMetrics().compute(_inputs({}, car=None)))

    assert metrics["pit.stops_done"].quality is Quality.INSUFFICIENT
    assert metrics["pit.last_lane_time_s"].value is None
    assert metrics["pit.window_ideal_lap"].quality is Quality.INSUFFICIENT


def test_stale_pit_telemetry_marks_available_values_stale() -> None:
    freshness = (PacketFreshness("lap_data", 1.0, 1.5),)
    metrics = _metric_map(
        PitMetrics().compute(
            _inputs({0: _event_records(0)}, car=CarSnapshot(0, pit_stops=1), freshness=freshness)
        )
    )

    assert metrics["pit.last_lane_time_s"].quality is Quality.STALE


def test_same_lap_entry_and_exit_are_not_counted_as_a_complete_stop_pair() -> None:
    records = (
        LapRecord(0, 1, lap_time_s=90.0, valid=True, closed=True),
        LapRecord(
            0,
            2,
            lap_time_s=110.0,
            valid=True,
            pit_entry=True,
            pit_exit=True,
            closed=True,
        ),
        LapRecord(0, 3, lap_time_s=90.0, valid=True, closed=True),
    )
    inputs = _inputs({0: records})

    metrics = _metric_map(PitMetrics().compute(inputs))

    assert metrics["pit.sample_count"].value == 0
    assert metrics["pit.loss_estimate_s"].quality is Quality.INSUFFICIENT


@pytest.mark.parametrize(
    ("flags", "valid"),
    [
        (frozenset({"incomplete"}), True),
        (frozenset({"partial_lap"}), True),
        (frozenset({"retired"}), True),
        (frozenset(), False),
    ],
)
def test_ineligible_entry_or_exit_lap_is_not_a_pit_loss_sample(
    flags: frozenset[str],
    valid: bool,
) -> None:
    records = list(_event_records(0))
    records[1] = replace(records[1], flags=flags, valid=valid)
    records[2] = replace(records[2], flags=flags, valid=valid)

    metrics = _metric_map(PitMetrics().compute(_inputs({0: tuple(records)})))

    assert metrics["pit.sample_count"].value == 0
