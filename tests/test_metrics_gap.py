from __future__ import annotations

from dataclasses import replace

from f1_strategist.config import MetricsConfig, SampleThresholds
from f1_strategist.metrics.gap import (
    CarComparison,
    GapMetrics,
    GapSample,
    GapTracker,
    compare_car,
)
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs, TyreStint
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import CarSnapshot, PacketFreshness, RaceState


def _inputs(
    cars: tuple[CarSnapshot, ...],
    *,
    now: float = 10.0,
    history: LapHistorySnapshot | None = None,
    player_index: int = 0,
    freshness: tuple[PacketFreshness, ...] | None = None,
) -> MetricInputs:
    state = RaceState().snapshot()
    state = replace(
        state,
        cars=cars,
        player_car_index=player_index,
        freshness=freshness if freshness is not None else state.freshness,
    )
    config = MetricsConfig(min_samples={"gap": SampleThresholds(low=2, ok=3)})
    return MetricInputs(state, history or LapHistorySnapshot(), config, now, player_index)


def _cars(*, tied: bool = False) -> tuple[CarSnapshot, ...]:
    return (
        CarSnapshot(
            0,
            position=2,
            delta_to_car_in_front_ms=1250,
            delta_to_leader_ms=1250,
            tyre_age_laps=4,
            actual_tyre_compound="soft",
            pit_stops=1,
        ),
        CarSnapshot(
            1,
            position=1,
            delta_to_leader_ms=None,
            actual_tyre_compound="medium",
            tyre_age_laps=5,
            pit_stops=0,
        ),
        CarSnapshot(
            2,
            position=2 if tied else 3,
            delta_to_car_in_front_ms=2000,
            delta_to_leader_ms=3250,
            actual_tyre_compound="hard",
            tyre_age_laps=8,
            pit_stops=2,
        ),
    )


def _metric_map(metrics):
    return {metric.key: metric for metric in metrics}


def test_gap_metrics_distinguish_ahead_behind_and_leader() -> None:
    metrics = _metric_map(GapMetrics().compute(_inputs(_cars())))

    assert metrics["gap.ahead_s"].value == 1.25
    assert metrics["gap.behind_s"].value == 2.0
    assert metrics["gap.leader_s"].value == 1.25


def test_gap_trends_are_negative_when_intervals_close() -> None:
    tracker = GapTracker()
    tracker.add_sample(GapSample(1.0, 5.0, 4.0, 5.0))
    tracker.add_sample(GapSample(3.0, 4.0, 3.0, 4.0))
    cars = list(_cars())
    cars[0] = replace(cars[0], delta_to_car_in_front_ms=3000)

    metrics = _metric_map(GapMetrics(tracker).compute(_inputs(tuple(cars), now=5.0)))

    assert metrics["gap.ahead_trend_s_per_s"].value == -0.5
    assert metrics["gap.behind_trend_s_per_s"].value == -0.5


def test_leader_and_last_car_have_missing_neighbor_quality() -> None:
    player = replace(_cars()[0], position=1, delta_to_car_in_front_ms=None)
    leader = replace(_cars()[1], position=2, delta_to_car_in_front_ms=2000)
    leader_state = (player, leader, replace(_cars()[2], position=3))
    leader_metrics = _metric_map(GapMetrics().compute(_inputs(leader_state)))

    assert leader_metrics["gap.ahead_s"].quality is Quality.UNAVAILABLE
    assert leader_metrics["gap.behind_s"].value == 2.0
    assert leader_metrics["gap.leader_s"].value == 0.0

    last_state = (
        replace(_cars()[0], position=3, delta_to_car_in_front_ms=2000),
        replace(_cars()[1], position=1),
        replace(_cars()[2], position=2, delta_to_car_in_front_ms=1000),
    )
    last_metrics = _metric_map(GapMetrics().compute(_inputs(last_state)))
    assert last_metrics["gap.behind_s"].quality is Quality.UNAVAILABLE


def test_tied_positions_are_not_assigned_an_ahead_or_behind_direction() -> None:
    tied = _metric_map(GapMetrics().compute(_inputs(_cars(tied=True))))

    assert tied["gap.ahead_s"].quality is Quality.UNAVAILABLE
    assert tied["gap.behind_s"].quality is Quality.UNAVAILABLE

    comparison = compare_car(_inputs(_cars(tied=True)), 2)
    assert comparison.interval_direction == "tied"
    assert comparison.interval_s is None


def test_compare_car_uses_signed_intervals_and_reports_rival_data() -> None:
    player_laps = tuple(
        LapRecord(
            0,
            lap,
            lap_time_s=90.0,
            compound="soft",
            tyre_age_end=lap,
            valid=True,
            closed=True,
        )
        for lap in (2, 3, 4)
    )
    rival_laps = tuple(
        LapRecord(
            2,
            lap,
            lap_time_s=89.0,
            compound="hard",
            tyre_age_end=lap,
            valid=True,
            closed=True,
        )
        for lap in (2, 3, 4)
    )
    history = LapHistorySnapshot(
        records={0: player_laps, 2: rival_laps},
        tyre_stints={0: (TyreStint("soft", 2),), 2: (TyreStint("hard", 2),)},
    )
    inputs = _inputs(_cars(), history=history)

    ahead = compare_car(inputs, 1)
    behind = compare_car(inputs, 2)

    assert ahead.interval_direction == "ahead"
    assert ahead.interval_s == -1.25
    assert behind.interval_direction == "behind"
    assert behind.interval_s == 2.0
    assert behind.pace_delta_s == -1.0
    assert behind.compound == "hard"
    assert behind.tyre_age_laps == 8
    assert behind.stops_done == 2
    assert isinstance(behind, CarComparison)


def test_retired_rival_is_unavailable_and_tracker_is_bounded() -> None:
    cars = list(_cars())
    cars[2] = replace(cars[2], result_status="retired")
    comparison = compare_car(_inputs(tuple(cars)), 2)
    assert comparison.quality is Quality.UNAVAILABLE
    assert comparison.notes == ("rival_retired",)

    tracker = GapTracker(max_samples=2)
    for timestamp in (1.0, 2.0, 3.0):
        tracker.add_sample(GapSample(timestamp, 1.0, 1.0, 1.0))
    assert [sample.observed_at_monotonic for sample in tracker.samples] == [2.0, 3.0]


def test_stale_gap_telemetry_marks_available_intervals_stale() -> None:
    freshness = (PacketFreshness("lap_data", 1.0, 1.5),)
    inputs = _inputs(_cars(), freshness=freshness)
    metrics = _metric_map(GapMetrics().compute(inputs))

    assert metrics["gap.ahead_s"].quality is Quality.STALE
    assert compare_car(inputs, 2).quality is Quality.STALE
