from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, SampleThresholds
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs, TyreStint
from f1_strategist.metrics.pace import PaceMetrics
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import PacketFreshness, RaceState


def _inputs(
    records: tuple[LapRecord, ...],
    *,
    config: MetricsConfig | None = None,
    freshness: tuple[PacketFreshness, ...] | None = None,
    stints: tuple[TyreStint, ...] = (TyreStint("soft", 1),),
    current_lap: int | None = None,
    tyre_age_laps: int | None = None,
    compound: str | None = None,
) -> MetricInputs:
    state = RaceState().snapshot()
    cars = list(state.cars)
    cars[0] = replace(
        cars[0],
        current_lap=current_lap,
        tyre_age_laps=tyre_age_laps,
        actual_tyre_compound=compound,
    )
    state = replace(state, cars=tuple(cars))
    if freshness is not None:
        state = replace(state, freshness=freshness)
    history = LapHistorySnapshot(
        records={0: records},
        tyre_stints={0: stints},
    )
    return MetricInputs(state, history, config or MetricsConfig(), 10.0, 0)


def _record(
    lap: int,
    time: float,
    *,
    flags: frozenset[str] = frozenset(),
    track_status: frozenset[str] = frozenset(),
    traffic: float | None = None,
    fuel: float | None = None,
) -> LapRecord:
    return LapRecord(
        car_index=0,
        lap_number=lap,
        lap_time_s=time,
        lap_time_source="session_history",
        valid=True,
        fuel_start_kg=fuel,
        flags=flags | ({"first_lap"} if lap == 1 else set()),
        track_status_seen=track_status,
        traffic_fraction=traffic,
        closed=True,
    )


def _by_key(metrics):
    return {metric.key: metric for metric in metrics}


def test_pace_metrics_calculate_clean_mean_and_linear_trend() -> None:
    metrics = _by_key(
        PaceMetrics().compute(_inputs(tuple(_record(lap, 90 + lap) for lap in range(2, 7))))
    )

    assert metrics["pace.last_lap_s"].value == 96.0
    assert metrics["pace.best_lap_s"].value == 92.0
    assert metrics["pace.clean_mean_s"].value == 94.0
    assert metrics["pace.trend_s_per_lap"].value == 1.0
    assert metrics["pace.clean_laps_used"].value == 5


def test_dirty_last_lap_is_reported_but_excluded_from_clean_samples() -> None:
    records = (
        _record(1, 90.0),
        _record(2, 91.0),
        _record(3, 92.0),
        _record(4, 93.0),
        _record(5, 94.0, track_status=frozenset({"full_safety_car"})),
        _record(6, 99.0, traffic=0.8),
    )
    metrics = _by_key(PaceMetrics().compute(_inputs(records)))

    assert metrics["pace.last_lap_s"].value == 99.0
    assert metrics["pace.best_lap_s"].value == 90.0
    assert metrics["pace.clean_mean_s"].value == pytest.approx(92.0)
    assert metrics["pace.clean_laps_used"].value == 3
    assert metrics["pace.trend_s_per_lap"].value == 1.0
    assert metrics["pace.trend_s_per_lap"].quality is Quality.LOW


def test_best_lap_includes_valid_official_lap_with_traffic() -> None:
    records = (
        _record(2, 90.0),
        _record(3, 80.863, traffic=0.8),
        _record(4, 91.0),
    )

    metrics = _by_key(PaceMetrics().compute(_inputs(records)))

    assert metrics["pace.best_lap_s"].value == 80.863
    assert metrics["pace.clean_mean_s"].sample_count == 2


@pytest.mark.parametrize("flag", ["partial_lap", "incomplete", "retired"])
def test_partial_incomplete_and_retired_laps_are_never_pace_samples(flag: str) -> None:
    records = (
        _record(2, 91.0),
        _record(3, 92.0),
        _record(4, 93.0),
        _record(5, 50.0, flags=frozenset({flag})),
    )
    metrics = _by_key(PaceMetrics().compute(_inputs(records)))

    assert metrics["pace.last_lap_s"].value == 93.0
    assert metrics["pace.clean_mean_s"].value == pytest.approx(92.0)
    assert metrics["pace.clean_laps_used"].value == 3
    assert metrics["pace.trend_s_per_lap"].value == 1.0


def test_fuel_corrected_metrics_only_exist_when_configured() -> None:
    records = tuple(_record(lap, 90.0 + lap, fuel=50.0 - 10.0 * (lap - 1)) for lap in range(2, 5))
    plain = _by_key(PaceMetrics().compute(_inputs(records)))
    corrected = _by_key(
        PaceMetrics().compute(_inputs(records, config=MetricsConfig(fuel_effect_s_per_kg=0.1)))
    )

    assert "pace.clean_mean_fuel_corrected_s" not in plain
    assert corrected["pace.clean_mean_fuel_corrected_s"].value == pytest.approx(90.0)
    assert corrected["pace.trend_fuel_corrected_s_per_lap"].value == pytest.approx(2.0)


def test_missing_history_is_insufficient_and_stale_inputs_are_marked() -> None:
    empty = _by_key(PaceMetrics().compute(_inputs(())))
    assert empty["pace.last_lap_s"].quality is Quality.INSUFFICIENT
    assert empty["pace.clean_mean_s"].value is None

    freshness = (
        PacketFreshness("lap_data", 1.0, 1.5),
        PacketFreshness("session_history", 1.0, 5.0),
    )
    stale = _by_key(PaceMetrics().compute(_inputs((_record(1, 90.0),), freshness=freshness)))
    assert stale["pace.last_lap_s"].quality is Quality.STALE


@pytest.mark.parametrize("sample_count", [0, 1, 2, 3])
def test_pace_trend_reports_required_clean_sample_count(sample_count: int) -> None:
    records = tuple(_record(lap, 90.0 + lap) for lap in range(2, sample_count + 2))

    trend = _by_key(PaceMetrics().compute(_inputs(records)))["pace.trend_s_per_lap"]

    assert trend.sample_count == sample_count
    if sample_count < 3:
        assert trend.quality is Quality.INSUFFICIENT
        assert "required_samples:3" in trend.notes
    else:
        assert trend.value == pytest.approx(1.0)
        assert "required_samples:3" not in trend.notes


def test_pace_trend_uses_configured_minimum_above_three_samples() -> None:
    config = MetricsConfig(
        min_samples={"pace": SampleThresholds(low=4, ok=5)},
    )
    records = tuple(_record(lap, 90.0 + lap) for lap in (2, 3, 4))

    trend = _by_key(PaceMetrics().compute(_inputs(records, config=config)))["pace.trend_s_per_lap"]

    assert trend.quality is Quality.INSUFFICIENT
    assert trend.sample_count == 3
    assert "required_samples:4" in trend.notes


def test_pace_uses_stint_containing_current_lap_not_future_stint() -> None:
    records = tuple(replace(_record(lap, 90.0 + lap), compound="soft") for lap in (2, 3, 4))
    metrics = _by_key(
        PaceMetrics().compute(
            _inputs(
                records,
                stints=(TyreStint("soft", 1, 4), TyreStint("medium", 5)),
                current_lap=4,
                tyre_age_laps=2,
                compound="soft",
            )
        )
    )

    assert metrics["pace.clean_mean_s"].value == 93.0
    assert metrics["pace.clean_laps_used"].value == 3
