"""Evaluación de alternativas y tiempo restante (S5)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, StrategyConfig
from f1_strategist.metrics.history import LapHistorySnapshot, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import CarSnapshot, RaceState
from f1_strategist.strategy.evaluator import (
    NOTE_BASELINE,
    NOTE_NO_MODEL,
    NOTE_PERTURBATION,
    NOTE_PIT_LOSS_UNAVAILABLE,
    NOTE_STAY_OUT_NOT_VIABLE,
    NOTE_STAY_OUT_UNAVAILABLE,
    NOTE_TRAFFIC_COST,
    evaluate_alternatives,
)
from f1_strategist.strategy.pace_model import CompoundModel, CompoundModels
from f1_strategist.strategy.pit_model import PitLossEstimate
from f1_strategist.strategy.types import (
    ASSUMPTION_RIVALS_DO_NOT_PIT,
    RISK_TRAFFIC_NOT_PRICED,
    AlternativeResult,
    Perturbation,
    StrategyAction,
)
from f1_strategist.strategy.tyre_options import TyreOption, TyreOptions

TRACK_LENGTH_M = 5_000.0


def _car(index: int = 0, **fields: object) -> CarSnapshot:
    return CarSnapshot(car_index=index, **fields)  # type: ignore[arg-type]


def _inputs(
    *,
    car: CarSnapshot | None = None,
    cars: tuple[CarSnapshot, ...] = (),
    session_fields: dict[str, object] | None = None,
    now: float = 10.0,
) -> MetricInputs:
    state = RaceState().snapshot()
    slots = list(state.cars)
    for item in cars:
        slots[item.car_index] = item
    if car is not None:
        slots[car.car_index] = car
    fields = _race_state()
    fields.update(session_fields or {})
    session = replace(state.session, **fields)
    state = replace(state, cars=tuple(slots), session=session)
    return MetricInputs(state, LapHistorySnapshot(), MetricsConfig(), now, 0)


def _race_state(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "total_laps": 30,
        "track_length_m": TRACK_LENGTH_M,
        "session_type": "race",
    }
    fields.update(overrides)
    return fields


def _model(
    compound: str,
    base_lap_s: float,
    slope_s_per_lap: float,
    *,
    initial_age_laps: float | None = None,
    usable_life_laps: float | None = None,
    cliff_penalty_s_per_lap: float | None = None,
    quality: Quality = Quality.OK,
    notes: tuple[str, ...] = (),
) -> CompoundModel:
    return CompoundModel(
        compound=compound,
        base_lap_s=base_lap_s,
        slope_s_per_lap=slope_s_per_lap,
        wear_rate_pct_per_lap=0.5,
        n_race_laps=5,
        source="race",
        quality=quality,
        initial_age_laps=initial_age_laps,
        usable_life_laps=usable_life_laps,
        cliff_penalty_s_per_lap=cliff_penalty_s_per_lap,
        notes=notes,
    )


def _models(*models: CompoundModel) -> CompoundModels:
    return CompoundModels(models=models, reference_compound=models[0].compound)


def _option(
    compound: str = "hard",
    *,
    set_index: int = 0,
    initial_wear_pct: float = 0.0,
    useful_life_laps: float | None = 30.0,
    life_risk: bool = False,
) -> TyreOption:
    return TyreOption(
        compound=compound,
        visual_compound="c1",
        set_index=set_index,
        initial_wear_pct=initial_wear_pct,
        useful_life_laps=useful_life_laps,
        life_risk=life_risk,
    )


def _options(*options: TyreOption, stay_out_allowed: bool = True) -> TyreOptions:
    return TyreOptions(
        options=options,
        current_compound="soft",
        requires_stop=not stay_out_allowed,
        stay_out_allowed=stay_out_allowed,
        quality=Quality.OK,
    )


def _pit(value_s: float | None = 20.0) -> PitLossEstimate:
    if value_s is None:
        return PitLossEstimate(
            value_s=None,
            band_s=None,
            dispersion_s=None,
            source="none",
            sample_count=0,
            quality=Quality.UNAVAILABLE,
            notes=(),
        )
    return PitLossEstimate(
        value_s=value_s,
        band_s=2.0,
        dispersion_s=None,
        source="override",
        sample_count=0,
        quality=Quality.OK,
    )


def _by_key(results: tuple[AlternativeResult, ...]) -> dict[str, AlternativeResult]:
    return {result.key: result for result in results}


def test_example_5_10_reproduces_the_baseline_and_the_stop_to_the_millisecond() -> None:
    # Quedan 20 vueltas. Montado con 10 vueltas: 88.0 + 0.10·edad.
    # Juego nuevo: 88.3 + 0.05·edad. Pérdida en boxes: 20 s.
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    fresh = _model("hard", 88.3, 0.05)
    results = evaluate_alternatives(
        _models(stay, fresh),
        _pit(20.0),
        _options(_option("hard")),
        _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
        StrategyConfig(),
    )

    by_key = _by_key(results)
    assert results[0].key == "stay_out"
    assert by_key["stay_out"].remaining_time_s == pytest.approx(1799.0, abs=1e-9)
    assert by_key["stay_out"].gain_vs_baseline_s == pytest.approx(0.0, abs=1e-9)
    assert by_key["pit_now:hard:set0"].remaining_time_s == pytest.approx(1795.5, abs=1e-9)
    assert by_key["pit_now:hard:set0"].gain_vs_baseline_s == pytest.approx(3.5, abs=1e-9)
    assert by_key["stay_out"].action is StrategyAction.NO_CHANGE
    assert by_key["pit_now:hard:set0"].action is StrategyAction.PIT_NOW


def test_mid_lap_projection_applies_fraction_to_current_lap_before_full_laps() -> None:
    # Media vuelta recorrida: quedan 19.5 vueltas.
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    results = evaluate_alternatives(
        _models(stay, _model("hard", 88.3, 0.05)),
        _pit(20.0),
        _options(_option("hard")),
        _inputs(
            car=_car(
                current_lap=11,
                lap_distance_m=TRACK_LENGTH_M / 2.0,
                actual_tyre_compound="soft",
            )
        ),
        StrategyConfig(),
    )

    # 0.5·(88 + 0.10·10) + 19·88 + 0.10·(11..29) = 1754.5
    assert results[0].remaining_time_s == pytest.approx(1754.5, abs=1e-9)


def test_mid_lap_stop_splits_old_and_new_tyre_projection_chronologically() -> None:
    results = _by_key(
        evaluate_alternatives(
            _models(
                _model("soft", 88.0, 0.10, initial_age_laps=10.0),
                _model("hard", 88.3, 0.05),
            ),
            _pit(20.0),
            _options(_option("hard")),
            _inputs(
                car=_car(
                    current_lap=11,
                    lap_distance_m=TRACK_LENGTH_M / 2.0,
                    actual_tyre_compound="soft",
                )
            ),
            StrategyConfig(),
        )
    )

    assert results["pit_in_1:hard:set0"].remaining_time_s == pytest.approx(1751.15)


def test_mandatory_stop_uses_latest_viable_stop_as_baseline_without_a_window() -> None:
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    results = evaluate_alternatives(
        _models(stay, _model("hard", 88.3, 0.05)),
        _pit(20.0),
        _options(_option("hard"), stay_out_allowed=False),
        _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
        StrategyConfig(),
    )

    assert "stay_out" not in _by_key(results)
    # Sin ventana ideal, el plan de referencia conserva la última parada viable.
    baseline = results[0]
    assert baseline.action in (StrategyAction.PIT_NOW, StrategyAction.PIT_IN)
    assert baseline.pit_lap == 21
    assert baseline.gain_vs_baseline_s == pytest.approx(0.0, abs=1e-9)
    assert NOTE_BASELINE in baseline.notes
    assert NOTE_STAY_OUT_NOT_VIABLE in baseline.notes


def test_mandatory_stop_uses_the_game_ideal_lap_as_baseline() -> None:
    results = evaluate_alternatives(
        _models(
            _model("soft", 88.0, 0.10, initial_age_laps=10.0),
            _model("hard", 88.3, 0.05),
        ),
        _pit(20.0),
        _options(_option("hard"), stay_out_allowed=False),
        _inputs(
            car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft"),
            session_fields={"pit_window_ideal_lap": 15, "pit_window_latest_lap": 20},
        ),
        StrategyConfig(),
    )

    assert results[0].pit_lap == 15
    assert results[0].gain_vs_baseline_s == pytest.approx(0.0)


def test_stay_out_is_omitted_when_the_tyre_cannot_reach_the_end() -> None:
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0, usable_life_laps=20.0)
    results = evaluate_alternatives(
        _models(stay, _model("hard", 88.3, 0.05)),
        _pit(20.0),
        _options(_option("hard")),
        _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
        StrategyConfig(),
    )

    assert "stay_out" not in _by_key(results)
    assert NOTE_STAY_OUT_NOT_VIABLE in results[0].notes


def test_perturbation_scales_the_slope_and_shifts_the_pit_loss() -> None:
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    inputs = _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft"))
    neutral = _by_key(
        evaluate_alternatives(
            _models(stay, _model("hard", 88.3, 0.05)),
            _pit(20.0),
            _options(_option("hard")),
            inputs,
            StrategyConfig(),
        )
    )

    stressed = _by_key(
        evaluate_alternatives(
            _models(stay, _model("hard", 88.3, 0.05)),
            _pit(20.0),
            _options(_option("hard")),
            inputs,
            StrategyConfig(),
            Perturbation(slope_scale=2.0, pit_loss_delta_s=5.0),
        )
    )

    # Duplicar la pendiente añade 0.10 a cada edad: 20 vueltas desde edad 10
    # suman 0.10·(10..29) = 39.0 s extra.
    assert stressed["stay_out"].remaining_time_s == pytest.approx(
        neutral["stay_out"].remaining_time_s + 39.0, abs=1e-9
    )
    assert stressed["pit_now:hard:set0"].pit_loss_s == pytest.approx(25.0, abs=1e-9)
    assert NOTE_PERTURBATION in stressed["stay_out"].notes


def test_compound_without_a_model_is_omitted_with_a_note() -> None:
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    results = evaluate_alternatives(
        _models(stay),
        _pit(20.0),
        _options(_option("hard")),
        _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
        StrategyConfig(),
    )

    assert tuple(result.key for result in results) == ("stay_out",)
    assert NOTE_NO_MODEL in results[0].notes


def test_missing_pit_loss_leaves_only_the_stay_out_alternative() -> None:
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    results = evaluate_alternatives(
        _models(stay, _model("hard", 88.3, 0.05)),
        _pit(None),
        _options(_option("hard")),
        _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
        StrategyConfig(),
    )

    assert tuple(result.key for result in results) == ("stay_out",)
    assert NOTE_PIT_LOSS_UNAVAILABLE in results[0].notes


def test_without_remaining_laps_there_is_nothing_to_evaluate() -> None:
    results = evaluate_alternatives(
        _models(_model("soft", 88.0, 0.10, initial_age_laps=10.0)),
        _pit(20.0),
        _options(_option("hard")),
        _inputs(
            car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft"),
            session_fields={"total_laps": None},
        ),
        StrategyConfig(),
    )

    assert results == ()


def test_pit_in_alternatives_span_the_stop_window_and_carry_their_lap() -> None:
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    results = evaluate_alternatives(
        _models(stay, _model("hard", 88.3, 0.05)),
        _pit(20.0),
        _options(_option("hard")),
        _inputs(
            car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft"),
            session_fields=_race_state(pit_window_latest_lap=13),
        ),
        StrategyConfig(max_pit_in_laps=10),
    )

    pit_ins = [result for result in results if result.action is StrategyAction.PIT_IN]
    assert [result.key for result in pit_ins] == [
        "pit_in_1:hard:set0",
        "pit_in_2:hard:set0",
    ]
    assert [result.pit_lap for result in pit_ins] == [12, 13]


def test_traffic_cost_is_added_only_when_configured_and_only_to_the_immediate_stop() -> None:
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    player = _car(
        current_lap=11,
        lap_distance_m=0.0,
        actual_tyre_compound="soft",
        position=1,
        delta_to_leader_ms=0,
    )
    rival = _car(1, current_lap=11, position=2, delta_to_leader_ms=19_500)
    inputs = _inputs(car=player, cars=(rival,))
    models = _models(stay, _model("hard", 88.3, 0.05))

    priced = _by_key(
        evaluate_alternatives(
            models,
            _pit(20.0),
            _options(_option("hard")),
            inputs,
            StrategyConfig(traffic_cost_s_per_lap=0.4),
        )
    )
    unpriced = _by_key(
        evaluate_alternatives(
            models, _pit(20.0), _options(_option("hard")), inputs, StrategyConfig()
        )
    )

    assert priced["pit_now:hard:set0"].traffic_risk
    assert priced["pit_now:hard:set0"].traffic_cost_s == pytest.approx(0.2, abs=1e-9)
    assert priced["pit_now:hard:set0"].rejoin_position == 2
    assert NOTE_TRAFFIC_COST in priced["pit_now:hard:set0"].notes
    assert priced["pit_in_1:hard:set0"].traffic_cost_s is None

    assert unpriced["pit_now:hard:set0"].traffic_cost_s is None
    assert unpriced["pit_now:hard:set0"].traffic_risk
    assert RISK_TRAFFIC_NOT_PRICED in unpriced["pit_now:hard:set0"].notes
    assert ASSUMPTION_RIVALS_DO_NOT_PIT in unpriced["pit_now:hard:set0"].notes


def test_risk_of_tyre_life_is_declared_on_the_stop() -> None:
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    results = _by_key(
        evaluate_alternatives(
            _models(stay, _model("hard", 88.3, 0.05)),
            _pit(20.0),
            _options(_option("hard", useful_life_laps=10.0)),
            _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
            StrategyConfig(),
        )
    )

    assert "tyre_life_exceeded" in results["pit_now:hard:set0"].notes
    assert "tyre_life_exceeded" not in results["pit_in_10:hard:set0"].notes


def test_same_compound_sets_have_distinct_alternative_keys() -> None:
    options = _options(
        _option("hard", set_index=2, initial_wear_pct=30.0),
        _option("hard", set_index=5, initial_wear_pct=0.0),
    )
    results = evaluate_alternatives(
        _models(
            _model("soft", 88.0, 0.10, initial_age_laps=10.0),
            _model("hard", 88.3, 0.05),
        ),
        _pit(20.0),
        options,
        _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
        StrategyConfig(),
    )

    keys = [item.key for item in results]
    assert len(keys) == len(set(keys))
    assert "pit_now:hard:set2" in keys
    assert "pit_now:hard:set5" in keys


def test_delayed_stop_quality_includes_mounted_model_but_immediate_stop_does_not() -> None:
    results = evaluate_alternatives(
        _models(
            _model("soft", 88.0, 0.10, initial_age_laps=10.0, quality=Quality.STALE),
            _model("hard", 88.3, 0.05),
        ),
        _pit(20.0),
        _options(_option("hard")),
        _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
        StrategyConfig(max_pit_in_laps=1),
    )
    by_key = _by_key(results)

    assert by_key["pit_now:hard:set0"].quality is Quality.OK
    assert by_key["pit_in_1:hard:set0"].quality is Quality.STALE


def test_missing_mounted_model_marks_allowed_stay_out_as_unavailable() -> None:
    results = evaluate_alternatives(
        _models(_model("hard", 88.3, 0.05)),
        _pit(20.0),
        _options(_option("hard")),
        _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
        StrategyConfig(max_pit_in_laps=1),
    )

    assert results
    assert all(NOTE_STAY_OUT_UNAVAILABLE in item.notes for item in results)


def test_life_risk_accounts_for_old_set_until_a_delayed_stop() -> None:
    results = evaluate_alternatives(
        _models(
            _model("soft", 88.0, 0.10, initial_age_laps=10.0, usable_life_laps=12.0),
            _model("hard", 88.3, 0.05),
        ),
        _pit(20.0),
        _options(_option("hard", useful_life_laps=20.0)),
        _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft")),
        StrategyConfig(max_pit_in_laps=4),
    )

    by_key = _by_key(results)
    assert "tyre_life_exceeded" in by_key["pit_in_3:hard:set0"].notes
    assert "tyre_life_exceeded" not in by_key["pit_in_2:hard:set0"].notes
    assert "tyre_life_exceeded" not in by_key["pit_in_1:hard:set0"].notes


def test_evaluation_is_deterministic() -> None:
    stay = _model("soft", 88.0, 0.10, initial_age_laps=10.0)
    models = _models(stay, _model("hard", 88.3, 0.05))
    options = _options(_option("hard"))
    inputs = _inputs(car=_car(current_lap=11, lap_distance_m=0.0, actual_tyre_compound="soft"))

    first = evaluate_alternatives(models, _pit(20.0), options, inputs, StrategyConfig())
    second = evaluate_alternatives(models, _pit(20.0), options, inputs, StrategyConfig())

    assert first == second


def test_sakhir_lap_fifteen_does_not_discard_stay_out() -> None:
    """Vida del neumático en edad total: 6 + 5,5 vueltas caben en 16, no en 11."""
    car = _car(
        current_lap=15,
        lap_distance_m=TRACK_LENGTH_M / 2,
        actual_tyre_compound="soft",
    )
    inputs = _inputs(car=car, session_fields=_race_state(total_laps=20))
    models = _models(
        _model("soft", 90.0, 0.05, initial_age_laps=6.0, usable_life_laps=16.0),
        _model("hard", 90.3, 0.04),
    )

    results = evaluate_alternatives(
        models,
        _pit(20.0),
        _options(_option("hard")),
        inputs,
        StrategyConfig(),
    )

    stay = _by_key(results)["stay_out"]
    assert NOTE_STAY_OUT_NOT_VIABLE not in stay.notes


def test_sakhir_lap_fourteen_keeps_stay_out_viable() -> None:
    car = _car(
        current_lap=14,
        lap_distance_m=TRACK_LENGTH_M / 2,
        actual_tyre_compound="soft",
    )
    inputs = _inputs(car=car, session_fields=_race_state(total_laps=20))
    models = _models(
        _model("soft", 90.0, 0.05, initial_age_laps=5.0, usable_life_laps=16.0),
        _model("hard", 90.3, 0.04),
    )

    results = evaluate_alternatives(
        models,
        _pit(20.0),
        _options(_option("hard")),
        inputs,
        StrategyConfig(),
    )

    assert "stay_out" in _by_key(results)


def test_recommended_life_limit_still_discards_stay_out() -> None:
    """El límite recomendado se aplica en edad total: 6 + 5,5 > 11."""
    car = _car(
        current_lap=15,
        lap_distance_m=TRACK_LENGTH_M / 2,
        actual_tyre_compound="soft",
    )
    inputs = _inputs(car=car, session_fields=_race_state(total_laps=20))
    models = _models(
        _model("soft", 90.0, 0.05, initial_age_laps=6.0, usable_life_laps=11.0),
        _model("hard", 90.3, 0.04),
    )

    results = evaluate_alternatives(
        models,
        _pit(20.0),
        _options(_option("hard")),
        inputs,
        StrategyConfig(),
    )

    assert "stay_out" not in _by_key(results)


def test_zero_remaining_fitted_tyre_life_rejects_stay_out() -> None:
    stay = _model("soft", 90.0, 0.05, initial_age_laps=6.0, usable_life_laps=6.0)
    results = evaluate_alternatives(
        _models(stay, _model("hard", 90.3, 0.04)),
        _pit(20.0),
        _options(_option("hard")),
        _inputs(
            car=_car(current_lap=15, lap_distance_m=TRACK_LENGTH_M / 2, actual_tyre_compound="soft")
        ),
        StrategyConfig(),
    )

    assert "stay_out" not in _by_key(results)
