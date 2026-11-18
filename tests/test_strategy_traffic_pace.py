"""Ritmo condicionado por tráfico y separación de prior (J2)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, StrategyConfig
from f1_strategist.metrics.engine import MetricsEngine
from f1_strategist.metrics.history import (
    LapHistorySnapshot,
    LapRecord,
    MetricInputs,
)
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import CarSnapshot, RaceState
from f1_strategist.strategy import NOTE_PACE_INSUFFICIENT, StrategyAction, evaluate_once
from f1_strategist.strategy.pace_model import (
    NOTE_DAMAGE_TAINTED,
    NOTE_PRIOR_EXCLUDED,
    NOTE_TRAFFIC_CONDITIONED,
    build_compound_models,
)
from f1_strategist.weekend_profile import CompoundProfile, ProfileValue, WeekendProfile
from tests.scenarios import ScenarioCar, build_race_timed


def _record(
    lap: int,
    *,
    compound: str = "soft",
    age_start: int | None = None,
    lap_time_s: float | None = None,
    flags: frozenset[str] = frozenset(),
    pit_exit: bool = False,
    closed: bool = True,
) -> LapRecord:
    if age_start is None:
        age_start = lap - 1
    return LapRecord(
        car_index=0,
        lap_number=lap,
        lap_time_s=lap_time_s,
        lap_time_source="lap_data",
        compound=compound,
        tyre_age_start=age_start,
        tyre_age_end=None if age_start is None else age_start + 1,
        pit_exit=pit_exit,
        flags=flags,
        closed=closed,
    )


def _traffic_laps(
    *,
    first: int = 1,
    count: int = 5,
    base: float = 90.0,
    slope: float = 0.10,
) -> tuple[LapRecord, ...]:
    return tuple(
        _record(
            first + index,
            age_start=index,
            lap_time_s=base + slope * index,
            flags=frozenset({"traffic"}),
        )
        for index in range(count)
    )


def _car(current_lap: int, compound: str = "soft") -> CarSnapshot:
    return CarSnapshot(
        car_index=0,
        current_lap=current_lap,
        actual_tyre_compound=compound,
        tyre_age_laps=max(0, current_lap - 1),
    )


def _inputs(
    records: tuple[LapRecord, ...],
    *,
    car: CarSnapshot | None = None,
    profile: WeekendProfile | None = None,
    config: MetricsConfig | None = None,
) -> MetricInputs:
    snapshot = RaceState().snapshot()
    cars = list(snapshot.cars)
    if car is not None:
        cars[car.car_index] = car
    state = replace(snapshot, cars=tuple(cars))
    return MetricInputs(
        state,
        LapHistorySnapshot(records={0: records}),
        config or MetricsConfig(),
        10.0,
        0,
        profile,
    )


def _profile(quality: Quality) -> WeekendProfile:
    unavailable = quality is Quality.UNAVAILABLE
    entry = CompoundProfile(
        compound="soft",
        visual_compounds=(),
        stints=2,
        representative_stints=2,
        laps=10,
        clean_laps=10,
        wear_rate_pct_per_lap=ProfileValue(
            value=None if unavailable else 9.0, samples=4, quality=quality
        ),
        pace_mean_s=ProfileValue(value=None if unavailable else 95.0, samples=4, quality=quality),
        pace_delta_vs_reference_s=ProfileValue(
            value=None if unavailable else 0.0, samples=4, quality=quality
        ),
        fuel_consumption_kg_per_lap=ProfileValue(
            value=None if unavailable else 1.5, samples=4, quality=quality
        ),
        pit_loss_s=ProfileValue(value=None),
        track_temperature_c=ProfileValue(
            value=None if unavailable else 30.0, samples=4, quality=quality
        ),
        fuel_range_kg=(40.0, 60.0),
        weather=("clear",),
        sessions=(1,),
        quality=quality,
    )
    return WeekendProfile(
        compounds=(entry,), reference_compound="soft", sessions=(), quality=quality
    )


# --- Tráfico: ajuste condicionado, nunca equivalente al limpio -------------------


def test_traffic_only_laps_produce_a_conditioned_model() -> None:
    records = _traffic_laps()
    conditioned = build_compound_models(_inputs(records, car=_car(5)), StrategyConfig()).get("soft")

    assert conditioned is not None
    assert conditioned.available
    assert conditioned.source == "race"
    assert conditioned.n_race_laps == 5
    assert NOTE_TRAFFIC_CONDITIONED in conditioned.notes
    assert conditioned.quality is Quality.LOW

    clean = build_compound_models(_inputs(_traffic_laps_clean(), car=_car(5)), StrategyConfig())
    clean_model = clean.get("soft")
    assert clean_model is not None
    assert NOTE_TRAFFIC_CONDITIONED not in clean_model.notes
    assert clean_model.quality is Quality.OK
    # El ritmo condicionado no mejora al limpio equivalente.
    assert conditioned.quality is not Quality.OK


def _traffic_laps_clean() -> tuple[LapRecord, ...]:
    return tuple(replace(record, flags=frozenset()) for record in _traffic_laps())


def test_clean_fit_wins_over_traffic_laps() -> None:
    clean = tuple(
        _record(1 + index, age_start=index, lap_time_s=90.0 + 0.10 * index) for index in range(5)
    )
    traffic = tuple(
        _record(
            6 + index,
            age_start=5 + index,
            lap_time_s=95.0 + 0.60 * index,
            flags=frozenset({"traffic"}),
        )
        for index in range(5)
    )
    model = build_compound_models(_inputs(clean + traffic, car=_car(10)), StrategyConfig()).get(
        "soft"
    )

    assert model is not None
    assert model.n_race_laps == 5
    assert model.slope_s_per_lap == pytest.approx(0.10)
    assert NOTE_TRAFFIC_CONDITIONED not in model.notes


def test_two_traffic_laps_do_not_invent_a_slope() -> None:
    models = build_compound_models(_inputs(_traffic_laps(count=2), car=_car(2)), StrategyConfig())

    assert models.models == ()


def test_traffic_laps_with_a_single_age_do_not_fit() -> None:
    records = tuple(
        _record(
            1 + index,
            age_start=1,
            lap_time_s=90.0 + 0.10 * index,
            flags=frozenset({"traffic"}),
        )
        for index in range(4)
    )

    models = build_compound_models(_inputs(records, car=_car(4)), StrategyConfig())

    assert models.models == ()


def test_traffic_fraction_threshold_is_respected() -> None:
    exact = tuple(
        replace(
            _record(1 + index, age_start=index, lap_time_s=90.0 + 0.10 * index),
            traffic_fraction=0.5,
        )
        for index in range(5)
    )
    below = tuple(replace(record, traffic_fraction=0.49) for record in exact)

    conditioned = build_compound_models(_inputs(exact, car=_car(5)), StrategyConfig()).get("soft")
    clean = build_compound_models(_inputs(below, car=_car(5)), StrategyConfig()).get("soft")

    assert conditioned is not None
    assert NOTE_TRAFFIC_CONDITIONED in conditioned.notes
    assert conditioned.quality is Quality.LOW
    assert clean is not None
    assert NOTE_TRAFFIC_CONDITIONED not in clean.notes
    assert clean.quality is Quality.OK


def test_traffic_does_not_rescue_dirty_laps() -> None:
    base = _traffic_laps(count=6)
    dirty = (
        replace(base[0], valid=False),
        replace(base[1], pit_entry=True),
        replace(base[2], flags=frozenset({"traffic", "flashback"})),
        replace(base[3], track_status_seen=frozenset({"safety_car"})),
        replace(base[4], track_status_seen=frozenset({"red_flag"})),
        replace(base[5], damage_max_delta_pct=10.0),
    )

    models = build_compound_models(_inputs(dirty, car=_car(6)), StrategyConfig())

    assert models.models == ()


def test_flashback_traffic_lap_is_not_eligible() -> None:
    base = _traffic_laps(count=3)
    records = (base[0], replace(base[1], flags=frozenset({"traffic", "flashback"})), base[2])

    models = build_compound_models(_inputs(records, car=_car(3)), StrategyConfig())

    assert models.models == ()


def test_missing_age_or_time_produce_no_fit() -> None:
    base = _traffic_laps(count=3)
    records = (
        replace(base[0], tyre_age_start=None),
        replace(base[1], lap_time_s=None),
        base[2],
    )

    models = build_compound_models(_inputs(records, car=_car(3)), StrategyConfig())

    assert models.models == ()


# --- Daño: persistente y nunca asumido sano --------------------------------------


def test_damage_taints_clean_laps_with_a_zero_delta() -> None:
    clean = tuple(
        _record(1 + index, age_start=index, lap_time_s=90.0 + 0.10 * index) for index in range(3)
    )
    collision = replace(_record(4, age_start=3, lap_time_s=96.0), damage_max_delta_pct=8.0)
    after = tuple(
        replace(
            _record(5 + index, age_start=4 + index, lap_time_s=97.0 + 0.10 * index),
            damage_max_delta_pct=0.0,
        )
        for index in range(3)
    )

    model = build_compound_models(
        _inputs(clean + (collision,) + after, car=_car(7)), StrategyConfig()
    ).get("soft")

    assert model is not None
    # Solo las vueltas anteriores al golpe ajustan la pendiente.
    assert model.n_race_laps == 3
    assert model.slope_s_per_lap == pytest.approx(0.10)
    assert NOTE_DAMAGE_TAINTED in model.notes


# --- Prior insuficiente frente a un ajuste de carrera defendible -----------------


@pytest.mark.parametrize("prior_quality", [Quality.INSUFFICIENT, Quality.UNAVAILABLE])
def test_insufficient_prior_does_not_degrade_a_race_fit(prior_quality: Quality) -> None:
    records = _traffic_laps_clean()

    model = build_compound_models(
        _inputs(records, car=_car(5), profile=_profile(prior_quality)), StrategyConfig()
    ).get("soft")

    assert model is not None
    assert model.source == "race"
    assert model.quality is Quality.OK
    assert model.base_lap_s == pytest.approx(90.0)
    assert model.wear_rate_pct_per_lap is None or model.wear_rate_pct_per_lap < 1.0
    assert NOTE_PRIOR_EXCLUDED in model.notes


def test_session_restart_without_laps_does_not_invent_pace() -> None:
    inputs = _inputs((), car=_car(1))
    inputs = replace(inputs, history=LapHistorySnapshot(records={}, tyre_stints={}))

    models = build_compound_models(inputs, StrategyConfig())

    assert models.models == ()


# --- Integración: carrera sintética tipo Jeddah ----------------------------------


def _jeddah_race() -> list:
    cars = (
        ScenarioCar(
            index=0,
            name="Piloto",
            grid_position=1,
            base_lap_time_s=90.5,
            initial_fuel_kg=100.0,
            fuel_per_lap_kg=1.9,
            tyre_wear_per_lap_pct=2.0,
            pace_slope_s_per_lap=0.04,
            pit_laps=(24,),
            compounds=("soft", "hard"),
        ),
        ScenarioCar(
            index=1,
            name="Rival",
            grid_position=2,
            base_lap_time_s=90.9,
            initial_fuel_kg=100.0,
            fuel_per_lap_kg=1.9,
            tyre_wear_per_lap_pct=2.0,
            pace_slope_s_per_lap=0.04,
            pit_laps=(27,),
            compounds=("medium", "hard"),
        ),
    )
    return list(build_race_timed(cars, total_laps=50, samples_per_lap=2))


def test_jeddah_synthetic_race_decides_without_a_predetermined_stop() -> None:
    state = RaceState()
    engine = MetricsEngine()
    observed_at = 0.0
    for packet, observed_at in _jeddah_race():
        state.apply(packet, observed_at)
        engine.apply(packet, observed_at, state.snapshot())
        lap = state.snapshot().current_lap
        if lap is not None and lap >= 40:
            break

    history = engine.history.snapshot()
    inputs = MetricInputs(state.snapshot(), history, MetricsConfig(), observed_at, 0)
    config = StrategyConfig()
    evaluation = evaluate_once(inputs, config)

    assert evaluation.laps_remaining is not None
    assert evaluation.alternatives
    for alternative in evaluation.alternatives:
        assert alternative.remaining_time_s >= 0.0
    assert evaluation.recommendation.action in set(StrategyAction)
    if evaluation.recommendation.action is StrategyAction.PIT_NOW:
        gain = evaluation.recommendation.gain_vs_baseline_s
        assert gain is not None
        assert gain > config.min_gain_s

    # Con la tanda actual marcada como tráfico, el ritmo se publica condicionado
    # y la decisión sigue exigiendo margen; no se inventa un adelanto en segundos.
    traffic_records = tuple(
        replace(record, traffic_fraction=1.0) if record.lap_number >= 25 else record
        for record in history.records_for(0)
    )
    traffic_history = replace(
        history,
        records={**history.records, 0: traffic_records},
    )
    traffic_inputs = replace(inputs, history=traffic_history)
    conditioned = build_compound_models(traffic_inputs, config)
    traffic_evaluation = evaluate_once(traffic_inputs, config)

    model = conditioned.get("hard") or conditioned.get("soft")
    assert model is not None
    assert model.n_race_laps >= 3
    assert NOTE_TRAFFIC_CONDITIONED in model.notes
    assert model.quality is not Quality.OK
    if traffic_evaluation.recommendation.action is StrategyAction.PIT_NOW:
        traffic_gain = traffic_evaluation.recommendation.gain_vs_baseline_s
        assert traffic_gain is not None
        assert traffic_gain > config.min_gain_s
    elif traffic_evaluation.recommendation.action is StrategyAction.INSUFFICIENT_DATA:
        assert NOTE_PACE_INSUFFICIENT in traffic_evaluation.recommendation.notes
