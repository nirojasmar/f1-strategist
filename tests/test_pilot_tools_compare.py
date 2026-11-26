from __future__ import annotations

from dataclasses import replace

from f1_strategist.config import PilotConfig
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.pilot.tools_compare import compare_driver, gap_status
from f1_strategist.pilot.types import (
    EntityRef,
    Intent,
    PilotQuery,
    RoutedQuery,
    ToolInputs,
    ToolResult,
)
from f1_strategist.race_state import CarSnapshot, RaceState
from f1_strategist.strategy.types import StrategySnapshot


def _car(
    car_index: int,
    name: str,
    position: int,
    delta_to_leader_ms: int | None,
    *,
    compound: str = "soft",
    tyre_age_laps: int = 3,
    stops: int = 0,
    status: str | None = None,
) -> CarSnapshot:
    return CarSnapshot(
        car_index,
        name=name,
        position=position,
        current_lap=5,
        tyre_age_laps=tyre_age_laps,
        actual_tyre_compound=compound,
        pit_stops=stops,
        delta_to_leader_ms=delta_to_leader_ms,
        result_status=status,
    )


def _laps(
    car_index: int, times: tuple[float, ...], *, compound: str = "medium"
) -> tuple[LapRecord, ...]:
    return tuple(
        LapRecord(
            car_index,
            2 + offset,
            lap_time_s=time_s,
            valid=True,
            closed=True,
            compound=compound,
            tyre_age_start=offset,
        )
        for offset, time_s in enumerate(times)
    )


def _inputs(
    cars: tuple[CarSnapshot, ...],
    records: dict[int, tuple[LapRecord, ...]] | None = None,
    metrics: tuple[Metric, ...] = (),
    *,
    player_index: int = 0,
) -> ToolInputs:
    state = RaceState().snapshot()
    state = replace(state, cars=cars, player_car_index=player_index, current_lap=5)
    return ToolInputs(
        state=state,
        metrics=MetricsSnapshot(metrics={metric.key: metric for metric in metrics}),
        strategy=StrategySnapshot(),
        history=LapHistorySnapshot(records=records or {}),
        config=PilotConfig(),
        now=10.0,
    )


def _routed(intent: Intent, car_index: int | None) -> RoutedQuery:
    entities = () if car_index is None else (EntityRef("Rival", car_index=car_index),)
    query = PilotQuery(query_id="q1", text="¿cómo va el rival?", language="es", received_at=1.0)
    return RoutedQuery(query, intent, 0.9, entities=entities)


def _maybe_fact(result: ToolResult, name: str):
    return next((fact for fact in result.facts if fact.name == name), None)


def _fact(result: ToolResult, name: str):
    fact = _maybe_fact(result, name)
    assert fact is not None
    return fact


def _metric(key: str, value: float, quality: Quality = Quality.OK) -> Metric:
    return Metric(key, value, "s", Window("instant"), quality, 3, 10.0)


def _ahead_cars(rival_status: str | None = None) -> tuple[CarSnapshot, ...]:
    return (
        _car(0, "Piloto", 2, 1000, compound="medium", stops=1),
        _car(1, "Rival", 1, 0, status=rival_status),
    )


def test_compare_driver_reports_interval_and_negative_pace_when_rival_closes() -> None:
    records = {
        0: _laps(0, (90.5, 90.5, 90.5)),
        1: _laps(1, (90.0, 90.0, 90.0), compound="soft"),
    }

    result = compare_driver(
        _routed(Intent.COMPARE_DRIVER, 1),
        _inputs(_ahead_cars(), records),
    )

    interval = _fact(result, "compare.interval_s")
    pace = _fact(result, "compare.pace_delta_s")
    assert interval is not None and interval.value == 1.0
    assert interval.kind.value == "measured"
    assert pace is not None and pace.value == -0.5
    assert pace.kind.value == "estimated"
    assert _fact(result, "compare.interval_direction").value == "ahead"
    assert result.quality is Quality.OK
    assert result.missing == ()


def test_compare_driver_reports_positive_pace_when_rival_pulls_away() -> None:
    records = {
        0: _laps(0, (90.0, 90.0, 90.0)),
        1: _laps(1, (91.0, 91.0, 91.0), compound="soft"),
    }

    result = compare_driver(
        _routed(Intent.COMPARE_DRIVER, 1),
        _inputs(_ahead_cars(), records),
    )

    assert _fact(result, "compare.pace_delta_s").value == 1.0
    assert _fact(result, "compare.interval_direction").value == "ahead"


def test_compare_driver_without_enough_clean_rival_laps_omits_pace() -> None:
    records = {
        0: _laps(0, (90.5, 90.5, 90.5)),
        1: _laps(1, (90.0,), compound="soft"),
    }

    result = compare_driver(
        _routed(Intent.COMPARE_DRIVER, 1),
        _inputs(_ahead_cars(), records),
    )

    assert _fact(result, "compare.interval_s").value == 1.0
    assert _maybe_fact(result, "compare.pace_delta_s") is None
    assert "compare.pace_delta_s" in result.missing
    assert result.quality is Quality.LOW


def test_compare_driver_handles_retired_rival_without_error() -> None:
    result = compare_driver(
        _routed(Intent.COMPARE_DRIVER, 1),
        _inputs(_ahead_cars("retired")),
    )

    assert result.quality is Quality.UNAVAILABLE
    assert _fact(result, "compare.rival.retired").value is True
    assert _maybe_fact(result, "compare.interval_s") is None
    assert "compare.interval_s" in result.missing


def test_compare_driver_handles_lapped_rival_without_error() -> None:
    cars = (
        _car(0, "Piloto", 2, 1000),
        _car(1, "Doblado", 9, 95000),
    )

    result = compare_driver(
        _routed(Intent.COMPARE_DRIVER, 1),
        _inputs(cars),
    )

    assert _fact(result, "compare.interval_s").value == 94.0
    assert _fact(result, "compare.interval_direction").value == "behind"


def test_compare_driver_requires_a_rival() -> None:
    result = compare_driver(
        _routed(Intent.COMPARE_DRIVER, None),
        _inputs(_ahead_cars()),
    )

    assert result.quality is Quality.INSUFFICIENT
    assert result.missing == ("compare.rival",)


def test_gap_status_for_leader_publishes_zero_leader_gap() -> None:
    result = gap_status(
        _routed(Intent.GAP_STATUS, None),
        _inputs(_ahead_cars(), metrics=(_metric("gap.leader_s", 0.0),)),
    )

    leader = _fact(result, "gap.leader_s")
    assert leader is not None and leader.value == 0.0
    assert "gap.ahead_s" in result.missing
    assert result.quality is Quality.OK


def test_gap_status_for_last_publishes_available_gaps() -> None:
    metrics = (
        _metric("gap.ahead_s", 2.0),
        _metric("gap.leader_s", 30.0),
    )

    result = gap_status(
        _routed(Intent.GAP_STATUS, None),
        _inputs(_ahead_cars(), metrics=metrics),
    )

    assert _fact(result, "gap.ahead_s").value == 2.0
    assert _fact(result, "gap.leader_s").value == 30.0
    assert "gap.behind_s" in result.missing


def test_gap_status_marks_stale_metrics() -> None:
    result = gap_status(
        _routed(Intent.GAP_STATUS, None),
        _inputs(_ahead_cars(), metrics=(_metric("gap.ahead_s", 2.0, Quality.STALE),)),
    )

    assert result.quality is Quality.STALE
    assert result.stale is True
    assert _fact(result, "gap.ahead_s").quality is Quality.STALE


def test_gap_status_never_answers_another_driver_with_player_gaps() -> None:
    cars = (
        _car(0, "Piloto", 5, 12000),
        _car(7, "Kessler", 8, 20000),
    )
    metrics = (
        _metric("gap.ahead_s", 2.0),
        _metric("gap.behind_s", 3.0),
        _metric("gap.leader_s", 12.0),
    )

    result = gap_status(_routed(Intent.GAP_STATUS, 7), _inputs(cars, metrics=metrics))

    assert result.quality is Quality.INSUFFICIENT
    assert result.facts == ()
    assert result.notes == ("pilot.unsupported.rival_gap",)


def test_gap_status_for_the_leader_keeps_only_the_leader_gap() -> None:
    cars = (
        _car(0, "Piloto", 2, 1000),
        _car(1, "Líder", 1, 0),
    )
    metrics = (
        _metric("gap.ahead_s", 1.0),
        _metric("gap.behind_s", 5.0),
        _metric("gap.leader_s", 1.0),
    )

    result = gap_status(_routed(Intent.GAP_STATUS, 1), _inputs(cars, metrics=metrics))

    assert [fact.name for fact in result.facts] == ["gap.ahead_s", "gap.leader_s"]
