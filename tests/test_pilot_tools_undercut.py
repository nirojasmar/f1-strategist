"""Herramienta de undercut (Q9)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, PilotConfig, StrategyConfig
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.pilot.answers import compose_answer
from f1_strategist.pilot.tools_undercut import (
    MISSING_PILOT_LEADS,
    MISSING_RIVAL_CLEAN_LAPS,
    UndercutEstimate,
    evaluate_undercut,
    pit_undercut,
)
from f1_strategist.pilot.types import (
    EntityRef,
    Intent,
    PilotQuery,
    RoutedQuery,
    ToolInputs,
)
from f1_strategist.race_state import CarSnapshot, RaceState
from f1_strategist.strategy.pace_model import CompoundModel
from f1_strategist.strategy.types import StrategySnapshot

PLAYER = 0
RIVAL = 1
TRAFFIC = 2


def _car(index: int, **fields: object) -> CarSnapshot:
    return CarSnapshot(car_index=index, **fields)  # type: ignore[arg-type]


def _inputs(
    records: dict[int, tuple[LapRecord, ...]] | None = None,
    *,
    cars: tuple[CarSnapshot, ...] = (),
    car_index: int = PLAYER,
) -> MetricInputs:
    state = RaceState().snapshot()
    slots = list(state.cars)
    for car in cars:
        slots[car.car_index] = car
    state = replace(state, cars=tuple(slots), player_car_index=car_index)
    return MetricInputs(
        state,
        LapHistorySnapshot(records=records or {}),
        MetricsConfig(),
        10.0,
        car_index,
    )


def _record(
    car_index: int,
    lap: int,
    *,
    age_start: int,
    lap_time_s: float,
    compound: str = "soft",
    pit_entry: bool = False,
    pit_exit: bool = False,
    pit_lane_time_s: float | None = None,
    pit_stop_time_s: float | None = None,
) -> LapRecord:
    return LapRecord(
        car_index=car_index,
        lap_number=lap,
        lap_time_s=lap_time_s,
        lap_time_source="lap_data",
        valid=True,
        compound=compound,
        tyre_age_start=age_start,
        tyre_age_end=age_start + 1,
        pit_entry=pit_entry,
        pit_exit=pit_exit,
        pit_lane_time_s=pit_lane_time_s,
        pit_stop_time_s=pit_stop_time_s,
        closed=True,
    )


def _model(
    *,
    compound: str = "soft",
    base_s: float = 90.0,
    slope_s_per_lap: float = 0.0,
) -> CompoundModel:
    return CompoundModel(
        compound=compound,
        base_lap_s=base_s,
        slope_s_per_lap=slope_s_per_lap,
        wear_rate_pct_per_lap=None,
        n_race_laps=5,
        source="race",
        quality=Quality.OK,
    )


def _routed(car_index: int | None = None) -> RoutedQuery:
    query = PilotQuery("q1", "¿undercut?", "es", 0.0)
    entities = () if car_index is None else (EntityRef(raw="rival", car_index=car_index),)
    return RoutedQuery(query, Intent.PIT_UNDERCUT, 1.0, entities=entities)


def _tool_inputs(inputs: MetricInputs) -> ToolInputs:
    return ToolInputs(
        state=inputs.state,
        metrics=MetricsSnapshot(),
        strategy=StrategySnapshot(),
        history=inputs.history,
        config=PilotConfig(),
        now=inputs.now,
    )


def _estimate(
    inputs: MetricInputs,
    *,
    player_model: CompoundModel | None,
    rival_model: CompoundModel | None,
    rival_age_laps: float | None = 10.0,
    rival_clean_laps: int = 5,
    pit_loss_s: float | None = 20.0,
) -> UndercutEstimate:
    return evaluate_undercut(
        inputs,
        RIVAL,
        player_model=player_model,
        rival_model=rival_model,
        rival_age_laps=rival_age_laps,
        rival_clean_laps=rival_clean_laps,
        pit_loss_s=pit_loss_s,
        cfg=StrategyConfig(),
        pilot=PilotConfig(),
    )


def _gap_state(gap_s: float, *, traffic_gap_s: float | None = None) -> MetricInputs:
    cars = [
        _car(PLAYER, position=2, current_lap=10, delta_to_leader_ms=gap_s * 1000.0),
        _car(RIVAL, position=1, current_lap=10, delta_to_leader_ms=0.0),
    ]
    if traffic_gap_s is not None:
        cars.append(
            _car(
                TRAFFIC,
                position=3,
                current_lap=10,
                delta_to_leader_ms=traffic_gap_s * 1000.0,
            )
        )
    return _inputs(cars=tuple(cars))


def test_analytic_gain_per_k_and_best_k() -> None:
    """Caso construido a mano: la ganancia por k coincide con el cálculo manual."""
    estimate = _estimate(
        _gap_state(8.0),
        player_model=_model(base_s=90.0, slope_s_per_lap=0.0),
        rival_model=_model(base_s=90.0, slope_s_per_lap=0.5),
    )

    assert estimate.gains_s == pytest.approx((5.0, 10.5, 16.5))
    assert estimate.best_k == 3
    assert estimate.best_gain_s == pytest.approx(16.5)
    assert estimate.viable is True
    assert estimate.gap_s == pytest.approx(8.0)


def test_high_wear_gains_more_than_low_wear() -> None:
    """Un rival con más degradación regala más tiempo por vuelta."""
    inputs = _gap_state(8.0)
    player = _model()
    high = _estimate(
        inputs,
        player_model=player,
        rival_model=_model(slope_s_per_lap=0.5),
    )
    low = _estimate(
        inputs,
        player_model=player,
        rival_model=_model(slope_s_per_lap=0.05),
    )

    assert all(
        high_gain > low_gain for high_gain, low_gain in zip(high.gains_s, low.gains_s, strict=False)
    )


def test_gap_above_and_below_gain() -> None:
    """El intervalo decide la viabilidad, no la ganancia bruta."""
    player = _model()
    rival = _model(slope_s_per_lap=0.5)
    wide = _estimate(_gap_state(100.0), player_model=player, rival_model=rival)
    tight = _estimate(_gap_state(8.0), player_model=player, rival_model=rival)

    assert wide.viable is False
    assert tight.viable is True


def test_rejoin_traffic_is_reported() -> None:
    """La reincorporación usa estimate_rejoin y avisa del tráfico inmediato."""
    estimate = _estimate(
        _gap_state(10.0, traffic_gap_s=29.5),
        player_model=_model(),
        rival_model=_model(slope_s_per_lap=0.5),
    )

    assert estimate.rejoin is not None
    assert estimate.rejoin.position == 3
    assert estimate.rejoin.traffic_risk is True


def test_leader_never_gets_a_number() -> None:
    """Si el piloto es líder no hay undercut posible."""
    inputs = _inputs(cars=(_car(PLAYER, position=1, current_lap=10),))
    estimate = _estimate(
        inputs,
        player_model=_model(),
        rival_model=_model(),
    )

    assert estimate.quality is Quality.INSUFFICIENT
    assert estimate.missing == (MISSING_PILOT_LEADS,)
    assert estimate.gains_s == ()
    assert estimate.best_k is None


def test_rival_without_clean_laps_is_insufficient() -> None:
    """Sin vueltas limpias suficientes no se inventa una ganancia."""
    estimate = _estimate(
        _gap_state(8.0),
        player_model=_model(),
        rival_model=_model(),
        rival_clean_laps=2,
    )

    assert estimate.quality is Quality.INSUFFICIENT
    assert MISSING_RIVAL_CLEAN_LAPS in estimate.missing
    assert estimate.best_gain_s is None


def _player_records() -> tuple[LapRecord, ...]:
    return (
        _record(PLAYER, 1, age_start=0, lap_time_s=90.0),
        _record(
            PLAYER,
            2,
            age_start=1,
            lap_time_s=100.0,
            pit_entry=True,
            pit_lane_time_s=25.0,
            pit_stop_time_s=2.5,
        ),
        _record(PLAYER, 3, age_start=0, lap_time_s=100.0, pit_exit=True, pit_lane_time_s=25.0),
        _record(PLAYER, 4, age_start=0, lap_time_s=90.0),
        _record(PLAYER, 5, age_start=1, lap_time_s=90.1),
        _record(PLAYER, 6, age_start=2, lap_time_s=90.2),
        _record(PLAYER, 7, age_start=3, lap_time_s=90.3),
    )


def _rival_records() -> tuple[LapRecord, ...]:
    return tuple(
        _record(RIVAL, lap, age_start=age, lap_time_s=90.5 + 0.5 * age)
        for lap, age in zip(range(1, 5), range(4), strict=False)
    )


def test_indicated_rival_wins_over_car_ahead() -> None:
    """Con un rival indicado se usa ese coche, no el de delante."""
    records = {
        PLAYER: _player_records(),
        RIVAL: _rival_records(),
    }
    cars = (
        _car(
            PLAYER,
            position=3,
            current_lap=7,
            tyre_age_laps=4,
            actual_tyre_compound="soft",
            delta_to_leader_ms=10_000.0,
        ),
        _car(
            RIVAL,
            position=1,
            current_lap=4,
            tyre_age_laps=4,
            actual_tyre_compound="soft",
            delta_to_leader_ms=0.0,
        ),
        _car(
            2,
            position=2,
            current_lap=7,
            actual_tyre_compound="soft",
            delta_to_leader_ms=9_000.0,
        ),
    )
    inputs = _tool_inputs(_inputs(records, cars=cars))

    result = pit_undercut(_routed(RIVAL), inputs)

    facts = {fact.name: fact.value for fact in result.facts}
    assert result.missing == ()
    assert facts["rival_car_index"] == RIVAL
    assert facts["best_k"] == 3
    assert facts["viable"] is False


def test_tool_leader_reports_pilot_leads() -> None:
    """Integración: líder sin coche delante => «vas primero», sin cifras."""
    inputs = _tool_inputs(
        _inputs(cars=(_car(PLAYER, position=1, current_lap=10, delta_to_leader_ms=0.0),))
    )

    result = pit_undercut(_routed(), inputs)

    assert result.quality is Quality.INSUFFICIENT
    assert result.missing == (MISSING_PILOT_LEADS,)
    assert result.facts == ()

    joined = compose_answer(_routed(), result, language="es", cfg=PilotConfig())
    assert joined.text == "Vas primero: no hay nadie delante."


def test_tool_rival_without_model_is_insufficient() -> None:
    """Integración: rival sin vueltas ni modelo => INSUFFICIENT_DATA."""
    records = {PLAYER: _player_records()}
    cars = (
        _car(
            PLAYER,
            position=2,
            current_lap=7,
            tyre_age_laps=4,
            actual_tyre_compound="soft",
            delta_to_leader_ms=5_000.0,
        ),
        _car(
            RIVAL,
            position=1,
            current_lap=20,
            tyre_age_laps=10,
            actual_tyre_compound="soft",
            delta_to_leader_ms=0.0,
        ),
    )
    inputs = _tool_inputs(_inputs(records, cars=cars))

    result = pit_undercut(_routed(), inputs)

    assert result.quality is Quality.INSUFFICIENT
    assert MISSING_RIVAL_CLEAN_LAPS in result.missing
