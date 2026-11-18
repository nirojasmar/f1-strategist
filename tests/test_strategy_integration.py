"""Validación de extremo a extremo del motor de estrategia con casos analíticos (S10).

Cada escenario construye el estado y el histórico mínimos, llama a
``evaluate_once`` y comprueba el resultado contra el cálculo manual descrito en
el propio test. Un fallo indica el escenario, la vuelta y el valor esperado.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, StrategyConfig
from f1_strategist.metrics.history import (
    LapHistorySnapshot,
    LapRecord,
    MetricInputs,
    TyreStint,
)
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import (
    CarSnapshot,
    PacketFreshness,
    RaceEvent,
    RaceState,
    RaceStateSnapshot,
    TyreSetSnapshot,
)
from f1_strategist.strategy import (
    ASSUMPTION_PROFILE_PRIOR_USED,
    NOTE_MANDATORY_STOP,
    NOTE_PACE_INSUFFICIENT,
    NOTE_STALE_DATA,
    RISK_TRAFFIC_NOT_PRICED,
    StrategyAction,
    evaluate_once,
)
from f1_strategist.strategy.types import BASELINE_STAY_OUT, compute_inputs_hash

TRACK_LENGTH_M = 5_000.0
CAR_INDEX = 0


def _car(index: int = CAR_INDEX, **fields: object) -> CarSnapshot:
    # Al final de la vuelta, la fracción restante es cero y las proyecciones
    # son sumas exactas de vueltas completas.
    fields.setdefault("lap_distance_m", TRACK_LENGTH_M)
    return replace(RaceState().snapshot().cars[index], car_index=index, **fields)


def _player(**fields: object) -> CarSnapshot:
    return _car(CAR_INDEX, **fields)


def _state(
    *,
    lap: int,
    total_laps: int = 25,
    cars: tuple[CarSnapshot, ...],
    inventory: tuple[TyreSetSnapshot, ...] = (),
    safety_car_status: str = "no_safety_car",
    freshness: tuple[PacketFreshness, ...] = (),
    weekend_structure: tuple[str, ...] = (),
) -> RaceStateSnapshot:
    snapshot = RaceState().snapshot()
    session = replace(
        snapshot.session,
        uid=123,
        session_type="race",
        track="monza",
        total_laps=total_laps,
        track_length_m=TRACK_LENGTH_M,
        safety_car_status=safety_car_status,
        weekend_structure=weekend_structure,
    )
    return replace(
        snapshot,
        session=session,
        current_lap=lap,
        player_car_index=CAR_INDEX,
        cars=cars,
        tyre_inventory=inventory,
        freshness=freshness,
    )


def _stint(
    *,
    compound: str,
    first_lap: int,
    ages: range,
    base_lap_s: float,
    slope_s_per_lap: float,
    wear_slope_pct: float = 0.5,
) -> tuple[LapRecord, ...]:
    """Vueltas limpias con ``tiempo = base + pendiente · edad`` exactos."""
    return tuple(
        LapRecord(
            CAR_INDEX,
            first_lap + offset,
            lap_time_s=base_lap_s + slope_s_per_lap * age,
            valid=True,
            closed=True,
            compound=compound,
            tyre_age_start=age,
            tyre_wear_end_pct=(min(100.0, wear_slope_pct * (age + 1)),) * 4,
        )
        for offset, age in enumerate(ages)
    )


def _hard_set(index: int = 1, *, wear_pct: float = 0.0) -> TyreSetSnapshot:
    return TyreSetSnapshot(
        index=index,
        actual_tyre_compound="hard",
        visual_tyre_compound="c1",
        wear_pct=wear_pct,
        available=True,
        recommended_session=None,
        lifespan_laps=40,
        usable_life_laps=40,
        lap_delta_time_ms=300,
        fitted=False,
    )


def _inputs(
    *,
    state: RaceStateSnapshot,
    records: tuple[LapRecord, ...],
    now: float = 100.0,
) -> MetricInputs:
    return MetricInputs(
        state=state,
        history=LapHistorySnapshot(records={CAR_INDEX: records}),
        config=MetricsConfig(),
        now=now,
        car_index=CAR_INDEX,
    )


def _stop_config(*, pit_loss_s: float = 10.0) -> StrategyConfig:
    return StrategyConfig(pit_loss_override_s=pit_loss_s)


def _rivals(*, gap_s: float) -> tuple[CarSnapshot, ...]:
    """Líder y un rival justo detrás, con intervalos conocidos."""
    leader = _car(1, position=1, current_lap=15, delta_to_leader_ms=0)
    chaser = _car(2, position=3, current_lap=15, delta_to_leader_ms=int(gap_s * 1000))
    return (leader, chaser)


# --- Escenario 1: la parada gana con margen y el valor es el calculado a mano -----


def test_clear_stop_gain_matches_the_analytical_value() -> None:
    """Vuelta 15 de 25 con un blando que pierde 1 s/vuelta.

    Continuar: edades 10..19 → 10·90 + (10+…+19) = 1045,0 s.
    Parar ahora por un duro fresco (0,02 s/vuelta): edades 1..10 →
    10·90 + 0,02·(1+…+10) = 901,10 s, más 10 s de pérdida en boxes.
    Ganancia = 1045,0 − 911,10 = 133,90 s.
    """
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=5_000,
    )
    state = _state(lap=15, cars=(car, *_rivals(gap_s=5.0)), inventory=(_hard_set(),))
    records = _stint(
        compound="hard",
        first_lap=1,
        ages=range(0, 5),
        base_lap_s=90.0,
        slope_s_per_lap=0.02,
    ) + _stint(
        compound="soft",
        first_lap=6,
        ages=range(0, 10),
        base_lap_s=90.0,
        slope_s_per_lap=1.0,
    )
    # El coche del piloto es el índice 0; los rivales ocupan 1 y 2.

    evaluation = evaluate_once(_inputs(state=state, records=records), _stop_config())

    assert evaluation.recommendation.action is StrategyAction.PIT_NOW
    assert evaluation.recommendation.compound == "hard"
    assert evaluation.recommendation.gain_vs_baseline_s == pytest.approx(133.90, abs=1e-6)
    assert evaluation.pit_loss_s == pytest.approx(10.0)
    assert evaluation.pit_loss_source == "override"


def test_stop_gain_interval_brackets_the_neutral_value() -> None:
    """Sin degradación, la única banda es la pérdida en boxes: ±2 s → 4 s."""
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=5_000,
    )
    state = _state(lap=15, cars=(car, *_rivals(gap_s=5.0)), inventory=(_hard_set(),))
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=85.0, slope_s_per_lap=0.0
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.0
    )

    evaluation = evaluate_once(_inputs(state=state, records=records), _stop_config())
    low, high = evaluation.recommendation.gain_interval_s or (math.nan, math.nan)

    # Continuar 10 vueltas a 90 s = 900 s; parar por un duro a 85 s + 10 s = 860 s.
    assert evaluation.recommendation.gain_vs_baseline_s == pytest.approx(40.0, abs=1e-6)
    assert (low, high) == (pytest.approx(38.0, abs=1e-6), pytest.approx(42.0, abs=1e-6))


# --- Escenario 2: mantener es mejor ----------------------------------------------


def test_staying_out_wins_when_the_stop_cannot_pay_back() -> None:
    """Duro de referencia más lento (95 s) y pérdida de 30 s: no compensa parar."""
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=5_000,
    )
    state = _state(lap=15, cars=(car, *_rivals(gap_s=5.0)), inventory=(_hard_set(),))
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=95.0, slope_s_per_lap=0.0
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.01
    )

    evaluation = evaluate_once(_inputs(state=state, records=records), _stop_config(pit_loss_s=30.0))

    assert evaluation.recommendation.action is StrategyAction.NO_CHANGE
    assert evaluation.recommendation.key == BASELINE_STAY_OUT
    assert evaluation.recommendation.gain_vs_baseline_s is None


def test_absent_weekend_profile_is_declared_and_never_assumed() -> None:
    """Sin perfil del fin de semana el modelo sale solo del histórico de carrera."""
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=5_000,
    )
    state = _state(lap=15, cars=(car, *_rivals(gap_s=5.0)), inventory=(_hard_set(),))
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=90.0, slope_s_per_lap=0.02
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=1.0
    )

    evaluation = evaluate_once(_inputs(state=state, records=records), _stop_config())

    assert evaluation.profile_hash is None
    assert ASSUMPTION_PROFILE_PRIOR_USED not in evaluation.assumptions


# --- Escenario 3: final de carrera ----------------------------------------------


def test_last_lap_has_no_laps_remaining_and_does_not_invent_a_stop() -> None:
    car = _player(
        current_lap=25,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=5_000,
    )
    state = _state(lap=25, total_laps=25, cars=(car, *_rivals(gap_s=5.0)))
    records = _stint(
        compound="soft", first_lap=16, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.5
    )

    evaluation = evaluate_once(_inputs(state=state, records=records), _stop_config())

    assert evaluation.laps_remaining == pytest.approx(0.0)
    assert evaluation.recommendation.action is StrategyAction.INSUFFICIENT_DATA
    assert "laps_remaining_unavailable" in evaluation.recommendation.notes


# --- Escenario 4: datos insuficientes -------------------------------------------


def test_two_clean_laps_are_reported_as_insufficient_pace() -> None:
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=1,
        delta_to_leader_ms=5_000,
    )
    state = _state(lap=15, cars=(car, *_rivals(gap_s=5.0)), inventory=(_hard_set(),))
    records = _stint(
        compound="soft", first_lap=14, ages=range(0, 2), base_lap_s=90.0, slope_s_per_lap=0.5
    )

    evaluation = evaluate_once(_inputs(state=state, records=records), _stop_config())

    assert evaluation.recommendation.action is StrategyAction.INSUFFICIENT_DATA
    assert NOTE_PACE_INSUFFICIENT in evaluation.recommendation.notes
    assert evaluation.alternatives == ()


def test_missing_inventory_never_produces_a_stop_alternative() -> None:
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=5_000,
    )
    state = _state(lap=15, cars=(car, *_rivals(gap_s=5.0)))
    records = _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.5
    )

    evaluation = evaluate_once(_inputs(state=state, records=records), _stop_config())

    assert evaluation.recommendation.action is StrategyAction.NO_CHANGE
    assert not {item.key for item in evaluation.alternatives if item.key != BASELINE_STAY_OUT}


def test_stale_history_is_declared_and_blocks_the_recommendation() -> None:
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=5_000,
    )
    fast_laps = PacketFreshness("lap_data", updated_at_monotonic=0.0, stale_after_seconds=5.0)
    state = _state(
        lap=15,
        cars=(car, *_rivals(gap_s=5.0)),
        inventory=(_hard_set(),),
        freshness=(fast_laps,),
    )
    records = _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.5
    )

    evaluation = evaluate_once(_inputs(state=state, records=records, now=100.0), _stop_config())

    assert evaluation.recommendation.action is StrategyAction.INSUFFICIENT_DATA
    assert NOTE_STALE_DATA in evaluation.recommendation.notes


# --- Escenario 5: parada obligatoria --------------------------------------------


def test_single_dry_compound_forces_a_stop() -> None:
    """Solo se ha usado blando: la regla de dos compuestos secos obliga a parar.

    La parada no gana por ritmo (duro más lento y pérdida de 40 s), así que la
    recomendación solo puede venir de la regla.
    """
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=5_000,
    )
    state = _state(
        lap=15,
        cars=(car, *_rivals(gap_s=5.0)),
        inventory=(_hard_set(),),
        weekend_structure=("practice_1", "race"),
    )
    records = _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.0
    )
    inputs = replace(
        _inputs(state=state, records=records),
        history=LapHistorySnapshot(
            records={CAR_INDEX: records},
            tyre_stints={
                CAR_INDEX: (TyreStint(compound="soft", visual_compound="soft", start_lap=1),)
            },
        ),
    )

    evaluation = evaluate_once(inputs, _stop_config(pit_loss_s=40.0))

    assert evaluation.recommendation.action in {StrategyAction.PIT_NOW, StrategyAction.PIT_IN}
    assert NOTE_MANDATORY_STOP in evaluation.recommendation.notes


# --- Escenario 6: tráfico en la reincorporación ---------------------------------


def test_rejoin_traffic_without_cost_is_declared_as_a_risk() -> None:
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=1_000,
    )
    leader = _car(1, position=1, current_lap=15, delta_to_leader_ms=0)
    # A 10,5 s del líder: al salir de boxes (1,0 + 10,0 s) queda medio segundo por delante.
    traffic = _car(2, position=3, current_lap=15, delta_to_leader_ms=10_500)
    missing_gap = _car(9, position=10, current_lap=15, delta_to_leader_ms=None)
    state = _state(lap=15, cars=(car, leader, traffic, missing_gap), inventory=(_hard_set(),))
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=90.0, slope_s_per_lap=0.02
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=1.0
    )

    evaluation = evaluate_once(_inputs(state=state, records=records), _stop_config())

    assert evaluation.recommendation.action is StrategyAction.PIT_NOW
    assert RISK_TRAFFIC_NOT_PRICED in evaluation.risks
    assert evaluation.rejoin_position_range is None
    assert "rival_gap_unavailable" in evaluation.risks
    assert "rejoin_current_gaps_not_projected" in evaluation.assumptions


# --- Escenario 7: explicabilidad y determinismo ---------------------------------


def test_every_evaluation_is_traceable_and_has_the_same_fingerprint() -> None:
    car = _player(
        current_lap=15,
        position=2,
        actual_tyre_compound="soft",
        tyre_age_laps=9,
        delta_to_leader_ms=5_000,
    )
    state = _state(lap=15, cars=(car, *_rivals(gap_s=5.0)), inventory=(_hard_set(),))
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=90.0, slope_s_per_lap=0.02
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=1.0
    )
    inputs = _inputs(state=state, records=records)

    runs = [evaluate_once(inputs, _stop_config()) for _ in range(3)]

    assert len({run.inputs_hash for run in runs}) == 1
    assert len({run.to_dict().__repr__() for run in runs}) == 1
    first = runs[0]
    assert len(first.inputs_hash) == 64
    int(first.inputs_hash, 16)  # huella SHA-256 en hexadecimal
    assert first.profile_hash is None
    assert first.recommendation.gain_interval_s is not None
    assert isinstance(first.assumptions, tuple)
    assert isinstance(first.sensitive_to, tuple)
    assert first.schema_version >= 1
    assert first.recommendation.confidence is not None
    assert first.data_quality in set(Quality)


def test_inputs_hash_ignores_the_clock_and_changes_with_the_configuration() -> None:
    """La huella identifica datos y configuración, no el instante de captura."""
    car = _player(current_lap=15, position=2, actual_tyre_compound="soft", tyre_age_laps=9)
    state = _state(lap=15, cars=(car, *_rivals(gap_s=5.0)), inventory=(_hard_set(),))
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=90.0, slope_s_per_lap=0.02
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.5
    )
    inputs = _inputs(state=state, records=records)
    config = _stop_config()
    baseline = evaluate_once(inputs, config).inputs_hash

    later = evaluate_once(replace(inputs, now=inputs.now + 1.0), config).inputs_hash
    aged = evaluate_once(
        replace(
            inputs,
            state=_state(
                lap=15,
                cars=(
                    _player(
                        current_lap=15,
                        position=2,
                        actual_tyre_compound="soft",
                        tyre_age_laps=10,
                    ),
                    *_rivals(gap_s=5.0),
                ),
                inventory=(_hard_set(),),
            ),
        ),
        config,
    ).inputs_hash
    configured = evaluate_once(
        inputs,
        StrategyConfig(min_gain_s=StrategyConfig().min_gain_s + 1.0),
    ).inputs_hash

    assert later == baseline
    assert aged != baseline
    assert configured != baseline


def test_inputs_hash_ignores_the_monotonic_clocks_of_the_captured_data() -> None:
    """Mover solo los relojes locales no cambia la huella (incidencia S10-H1)."""
    car = _player(current_lap=15, position=2, actual_tyre_compound="soft", tyre_age_laps=9)
    freshness = PacketFreshness(
        "lap_data",
        updated_at_monotonic=10.0,
        stale_after_seconds=5.0,
    )
    state = _state(
        lap=15,
        cars=(car, *_rivals(gap_s=5.0)),
        inventory=(_hard_set(),),
        freshness=(freshness,),
    )
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=90.0, slope_s_per_lap=0.02
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.5
    )
    inputs = _inputs(state=state, records=records)
    config = _stop_config()

    shifted_records = tuple(
        replace(record, started_at_monotonic=500.0, ended_at_monotonic=590.0) for record in records
    )
    shifted = replace(
        inputs,
        state=replace(
            state,
            # El reloj se mueve sin cambiar la clasificación: con ``now=100``
            # ambos valores dejan el grupo obsoleto (edad 80 y 90 s > 5 s).
            freshness=(replace(freshness, updated_at_monotonic=20.0),),
            tyre_inventory_updated_at_monotonic=99.0,
        ),
        history=replace(inputs.history, records={0: shifted_records}),
    )

    assert evaluate_once(shifted, config).inputs_hash == evaluate_once(inputs, config).inputs_hash


def test_inputs_hash_distinguishes_stale_groups_from_fresh_ones() -> None:
    """La huella registra qué grupos están obsoletos, nunca sus relojes."""
    car = _player(current_lap=15, position=2, actual_tyre_compound="soft", tyre_age_laps=9)
    fresh = PacketFreshness("lap_data", updated_at_monotonic=99.0, stale_after_seconds=5.0)
    stale = replace(fresh, updated_at_monotonic=0.0)
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=90.0, slope_s_per_lap=0.02
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.5
    )
    config = _stop_config()

    def _hash(freshness: PacketFreshness) -> str:
        state = _state(
            lap=15,
            cars=(car, *_rivals(gap_s=5.0)),
            inventory=(_hard_set(),),
            freshness=(freshness,),
        )
        return evaluate_once(_inputs(state=state, records=records), config).inputs_hash

    assert _hash(fresh) != _hash(stale)


def test_inputs_hash_distinguishes_red_flag_state_from_lap_order() -> None:
    """La huella registra la bandera roja derivada, no los relojes.

    Las dos entradas solo cambian en si la última vuelta limpia terminó antes o
    después del evento. Sin el marcador derivado, al excluir los relojes ambas
    tendrían la misma huella aunque el estado de bandera roja fuese distinto.
    """
    car = _player(current_lap=15, position=2, actual_tyre_compound="soft", tyre_age_laps=9)
    state = _state(
        lap=15,
        cars=(car, *_rivals(gap_s=5.0)),
        inventory=(_hard_set(),),
        freshness=(
            PacketFreshness("lap_data", updated_at_monotonic=99.0, stale_after_seconds=5.0),
        ),
    )
    state = replace(state, events=(RaceEvent("red_flag", 105.0, session_uid=123),))
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=90.0, slope_s_per_lap=0.02
    )
    clean_lap = records[-1]
    before = _inputs(
        state=state,
        records=(*records[:-1], replace(clean_lap, ended_at_monotonic=104.0)),
    )
    after = _inputs(
        state=state,
        records=(*records[:-1], replace(clean_lap, ended_at_monotonic=106.0)),
    )
    config = _stop_config()

    assert evaluate_once(before, config).inputs_hash != evaluate_once(after, config).inputs_hash


def test_fresh_inputs_keep_the_h1_fingerprint() -> None:
    """Sin grupos obsoletos la huella es exactamente la de H1.

    La clasificación de obsolescencia solo se añade al payload cuando existe,
    para no invalidar comparaciones con evaluaciones anteriores.
    """
    car = _player(current_lap=15, position=2, actual_tyre_compound="soft", tyre_age_laps=9)
    freshness = PacketFreshness("lap_data", updated_at_monotonic=99.0, stale_after_seconds=5.0)
    state = _state(
        lap=15,
        cars=(car, *_rivals(gap_s=5.0)),
        inventory=(_hard_set(),),
        freshness=(freshness,),
    )
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=90.0, slope_s_per_lap=0.02
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.5
    )
    inputs = _inputs(state=state, records=records)
    config = _stop_config()
    h1_payload = {
        "state": inputs.state,
        "history": inputs.history,
        "profile": inputs.profile,
        "car_index": inputs.car_index,
        "metrics_config": inputs.config.model_dump(mode="json"),
        "strategy_config": config.model_dump(mode="json"),
    }

    assert evaluate_once(inputs, config).inputs_hash == compute_inputs_hash(h1_payload)


def test_clock_compression_can_change_staleness_and_the_fingerprint() -> None:
    """Comprimir el reloj cambia la frescura y, con ella, la huella.

    Los umbrales de obsolescencia están en segundos absolutos, así que
    ``clock_scale`` no garantiza invariancia: con una actualización en 0 s y un
    umbral de 5 s, la evaluación a 8 s ve el grupo obsoleto y la comprimida a
    2 s lo ve fresco.
    """
    car = _player(current_lap=15, position=2, actual_tyre_compound="soft", tyre_age_laps=9)
    freshness = PacketFreshness("lap_data", updated_at_monotonic=0.0, stale_after_seconds=5.0)
    state = _state(
        lap=15,
        cars=(car, *_rivals(gap_s=5.0)),
        inventory=(_hard_set(),),
        freshness=(freshness,),
    )
    records = _stint(
        compound="hard", first_lap=1, ages=range(0, 5), base_lap_s=90.0, slope_s_per_lap=0.02
    ) + _stint(
        compound="soft", first_lap=6, ages=range(0, 10), base_lap_s=90.0, slope_s_per_lap=0.5
    )
    config = _stop_config()
    realtime = _inputs(state=state, records=records, now=8.0)
    compressed = replace(realtime, now=2.0)

    assert (
        evaluate_once(realtime, config).inputs_hash != evaluate_once(compressed, config).inputs_hash
    )
