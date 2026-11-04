from __future__ import annotations

from dataclasses import replace

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.history import LapHistorySnapshot, MetricInputs
from f1_strategist.metrics.progress import ProgressMetrics
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import CarSnapshot, RaceSessionSnapshot, RaceState


def _inputs(
    *,
    car: CarSnapshot | None = None,
    session: RaceSessionSnapshot | None = None,
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
    state = replace(
        snapshot,
        cars=tuple(cars),
        session=session or snapshot.session,
        freshness=freshness,
    )
    return MetricInputs(
        state, LapHistorySnapshot(), MetricsConfig(), now, car.car_index if car else 0
    )


def _metrics(inputs: MetricInputs):
    return {metric.key: metric for metric in ProgressMetrics().compute(inputs)}


def test_progress_metrics_at_start_and_mid_race() -> None:
    start = _metrics(
        _inputs(
            car=CarSnapshot(car_index=0, current_lap=1, lap_distance_m=0.0),
            session=RaceSessionSnapshot(
                uid=1,
                session_type="race",
                track="track",
                total_laps=10,
                started=True,
                ended=False,
                session_time_left_s=600.0,
                track_length_m=5000.0,
            ),
        )
    )
    assert start["progress.current_lap"].value == 1
    assert start["progress.laps_completed"].value == 0
    assert start["progress.laps_remaining"].value == 10.0
    assert start["progress.race_progress_pct"].value == 0.0

    middle = _metrics(
        _inputs(
            car=CarSnapshot(car_index=0, current_lap=6, lap_distance_m=1000.0),
            session=RaceSessionSnapshot(
                uid=1,
                session_type="race",
                track="track",
                total_laps=10,
                started=True,
                ended=False,
                track_length_m=5000.0,
            ),
        )
    )
    assert middle["progress.laps_completed"].value == 5
    assert middle["progress.laps_remaining"].value == 4.8
    assert middle["progress.race_progress_pct"].value == 52.0


def test_progress_handles_finished_race_time_session_and_formation() -> None:
    finished = _metrics(
        _inputs(
            car=CarSnapshot(car_index=0, current_lap=10),
            session=RaceSessionSnapshot(
                uid=2,
                session_type="race",
                track="track",
                total_laps=10,
                started=True,
                ended=True,
            ),
        )
    )
    assert finished["progress.laps_completed"].value == 10
    assert finished["progress.laps_remaining"].value == 0.0
    assert finished["progress.race_progress_pct"].value == 100.0

    timed = _metrics(
        _inputs(
            car=CarSnapshot(car_index=0, current_lap=4),
            session=RaceSessionSnapshot(
                uid=3,
                session_type="sprint_shootout",
                track="track",
                total_laps=None,
                started=True,
                ended=False,
                session_time_left_s=120.0,
            ),
        )
    )
    assert timed["progress.session_time_left_s"].value == 120.0
    assert timed["progress.laps_remaining"].quality is Quality.UNAVAILABLE
    assert timed["progress.race_progress_pct"].quality is Quality.UNAVAILABLE

    formation = _metrics(
        _inputs(
            car=CarSnapshot(car_index=0, current_lap=1, lap_distance_m=2500.0),
            session=RaceSessionSnapshot(
                uid=4,
                session_type="race",
                track="track",
                total_laps=20,
                started=False,
                ended=False,
                safety_car_status="formation_lap",
                track_length_m=5000.0,
            ),
        )
    )
    assert formation["progress.current_lap"].value == 1
    assert formation["progress.laps_completed"].value == 0
    assert formation["progress.laps_remaining"].value == 20.0
    assert formation["progress.race_progress_pct"].value == 0.0


def test_progress_marks_stale_inputs_and_missing_time() -> None:
    metrics = _metrics(
        _inputs(
            car=CarSnapshot(car_index=0, current_lap=2, lap_distance_m=0.0),
            session=RaceSessionSnapshot(
                uid=5,
                session_type="race",
                track="track",
                total_laps=5,
                started=True,
                ended=False,
                track_length_m=5000.0,
            ),
            stale_group="lap_data",
        )
    )
    assert metrics["progress.current_lap"].quality is Quality.STALE
    assert metrics["progress.laps_remaining"].quality is Quality.STALE
    assert metrics["progress.session_time_left_s"].quality is Quality.UNAVAILABLE
