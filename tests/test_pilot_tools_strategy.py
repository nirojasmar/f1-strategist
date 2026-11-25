"""Pruebas de las herramientas de estrategia del piloto (Q8)."""

from __future__ import annotations

from f1_strategist.config import PilotConfig
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot
from f1_strategist.metrics.types import Quality
from f1_strategist.pilot.tools_strategy import (
    NOTE_COMPOUND_RULE,
    NOTE_COMPOUND_UNAVAILABLE,
    NOTE_EVALUATION_FAILED,
    NOTE_EVALUATION_MISSING,
    NOTE_LAP_OUT_OF_WINDOW,
    NOTE_SIMULATED,
    ask_strategy,
    simulate_strategy,
)
from f1_strategist.pilot.types import Intent, PilotQuery, RoutedQuery, ToolInputs
from f1_strategist.race_state import RaceSessionSnapshot, RaceStateSnapshot
from f1_strategist.strategy.decision import NOTE_NO_ALTERNATIVES
from f1_strategist.strategy.types import (
    RISK_SAFETY_CAR,
    STRATEGY_SCHEMA_VERSION,
    StrategyAction,
    StrategyAlternative,
    StrategyEvaluation,
    StrategyRecommendation,
    StrategySnapshot,
)


def _evaluation(
    *,
    action: StrategyAction = StrategyAction.PIT_IN,
    pit_lap: int | None = 16,
    compound: str | None = "medium",
    notes: tuple[str, ...] = (),
    alternatives: tuple[StrategyAlternative, ...] = (),
    assumptions: tuple[str, ...] = (),
    risks: tuple[str, ...] = (),
    sensitive_to: tuple[str, ...] = (),
    data_quality: Quality = Quality.OK,
    session_uid: str = "123",
    forecast_compound: str | None = "soft",
) -> StrategyEvaluation:
    return StrategyEvaluation(
        revision=1,
        session_uid=session_uid,
        lap=13,
        laps_remaining=7.0,
        baseline="stay_out",
        recommendation=StrategyRecommendation(
            action=action,
            key="pit_in:medium",
            pit_lap=pit_lap,
            compound=compound,
            gain_vs_baseline_s=2.4 if action is not StrategyAction.INSUFFICIENT_DATA else None,
            gain_interval_s=(1.8, 3.0) if action is not StrategyAction.INSUFFICIENT_DATA else None,
            confidence=0.58 if action is not StrategyAction.INSUFFICIENT_DATA else None,
            set_index=1 if action is not StrategyAction.INSUFFICIENT_DATA else None,
            notes=notes,
        ),
        alternatives=alternatives,
        assumptions=assumptions,
        risks=risks,
        sensitive_to=sensitive_to,
        data_quality=data_quality,
        inputs_hash="a" * 64,
        schema_version=STRATEGY_SCHEMA_VERSION,
        forecast_compound=forecast_compound,
    )


def _inputs(
    evaluation: StrategyEvaluation | None,
    *,
    error: str | None = None,
    uid: int = 123,
    safety_car_status: str | None = None,
) -> ToolInputs:
    return ToolInputs(
        state=RaceStateSnapshot(
            session=RaceSessionSnapshot(
                uid=uid,
                session_type="race",
                track="test",
                total_laps=20,
                started=True,
                ended=False,
                safety_car_status=safety_car_status,
            ),
            current_lap=13,
            player_car_index=None,
            cars=(),
            freshness=(),
            events=(),
        ),
        metrics=MetricsSnapshot(),
        strategy=StrategySnapshot(evaluation=evaluation, error=error),
        history=LapHistorySnapshot(),
        config=PilotConfig(),
        now=10.0,
    )


def _query(text: str) -> RoutedQuery:
    return RoutedQuery(
        query=PilotQuery(
            query_id="q1",
            text=text,
            language="es",
            received_at=10.0,
        ),
        intent=Intent.ASK_STRATEGY,
        confidence=0.9,
        normalized_text=text,
    )


def _facts(result):
    return {fact.name: fact for fact in result.facts}


def test_ask_strategy_translates_pit_in_recommendation() -> None:
    evaluation = _evaluation(
        assumptions=("fuel_effect_not_separated",),
        risks=("damage_not_modelled",),
        sensitive_to=("pit_loss_s",),
    )

    result = ask_strategy(_query("¿Cambiamos de estrategia?"), _inputs(evaluation))

    facts = _facts(result)
    assert result.intent is Intent.ASK_STRATEGY
    assert result.quality is Quality.OK
    assert result.data_lap == 13
    assert facts["strategy_action"].value == StrategyAction.PIT_IN.value
    assert facts["strategy_action"].kind.value == "recommended"
    assert facts["strategy_pit_lap"].value == 16
    assert facts["strategy_compound"].value == "medium"
    assert facts["strategy_gain_s"].value == 2.4
    assert facts["strategy_gain_s"].kind.value == "estimated"
    assert facts["strategy_confidence"].value == 0.58
    assert facts["strategy_gain_low_s"].value == 1.8
    assert facts["strategy_gain_high_s"].value == 3.0
    assert "fuel_effect_not_separated" in result.notes
    assert "damage_not_modelled" in result.notes
    assert "pit_loss_s" in result.notes


def test_ask_strategy_translates_each_action() -> None:
    for action in (StrategyAction.NO_CHANGE, StrategyAction.PIT_NOW):
        result = ask_strategy(_query("estrategia"), _inputs(_evaluation(action=action)))

        facts = _facts(result)
        assert facts["strategy_action"].value == action.value


def test_ask_strategy_insufficient_data_keeps_reason() -> None:
    evaluation = _evaluation(
        action=StrategyAction.INSUFFICIENT_DATA,
        pit_lap=None,
        compound=None,
        notes=("pace_quality_insufficient",),
    )

    result = ask_strategy(_query("estrategia"), _inputs(evaluation))

    assert result.quality is Quality.INSUFFICIENT
    assert "pace_quality_insufficient" in result.notes
    assert "strategy_gain_s" not in _facts(result)


def test_ask_strategy_marks_safety_car_and_keeps_risk_key() -> None:
    evaluation = _evaluation(risks=(RISK_SAFETY_CAR,))

    result = ask_strategy(
        _query("estrategia"), _inputs(evaluation, safety_car_status="virtual_safety_car")
    )

    assert _facts(result)["strategy_safety_car"].value is True
    assert RISK_SAFETY_CAR in result.notes


def test_ask_strategy_flags_stale_evaluation() -> None:
    evaluation = _evaluation(session_uid="999")

    result = ask_strategy(_query("estrategia"), _inputs(evaluation))

    assert result.stale is True


def test_ask_strategy_without_evaluation_declares_missing_data() -> None:
    result = ask_strategy(_query("estrategia"), _inputs(None))

    assert result.quality is Quality.UNAVAILABLE
    assert result.missing == ("strategy_evaluation",)
    assert NOTE_EVALUATION_MISSING in result.notes
    assert result.facts == ()


def test_ask_strategy_distinguishes_engine_error() -> None:
    result = ask_strategy(_query("estrategia"), _inputs(None, error="boom"))

    assert NOTE_EVALUATION_FAILED in result.notes


def _alternatives() -> tuple[StrategyAlternative, ...]:
    return (
        StrategyAlternative(key="stay_out", remaining_time_s=1000.0, gain_vs_baseline_s=0.0),
        StrategyAlternative(
            key="pit_now:medium:set1",
            remaining_time_s=990.0,
            gain_vs_baseline_s=1.0,
            rejoin_position=8,
        ),
        StrategyAlternative(
            key="pit_now:soft:set2",
            remaining_time_s=980.0,
            gain_vs_baseline_s=2.0,
            rejoin_position=7,
            traffic_risk=True,
            set_index=2,
        ),
        StrategyAlternative(
            key="pit_in_3:medium:set1",
            remaining_time_s=985.0,
            gain_vs_baseline_s=3.0,
            rejoin_position=6,
        ),
        StrategyAlternative(
            key="pit_in_3:hard:set3",
            remaining_time_s=990.0,
            gain_vs_baseline_s=2.5,
        ),
        StrategyAlternative(
            key="pit_in_1:hard:set3",
            remaining_time_s=995.0,
            gain_vs_baseline_s=0.5,
        ),
    )


def test_simulate_pit_now_picks_best_alternative() -> None:
    evaluation = _evaluation(alternatives=_alternatives())

    result = simulate_strategy(_query("¿Y si paro ahora?"), _inputs(evaluation))

    facts = _facts(result)
    assert result.intent is Intent.ASK_STRATEGY
    assert result.quality is Quality.OK
    assert facts["strategy_simulated"].value is True
    assert facts["strategy_action"].value == StrategyAction.PIT_NOW.value
    assert facts["strategy_pit_lap"].value == 13
    assert facts["strategy_compound"].value == "soft"
    assert facts["strategy_set_index"].value == 2
    assert facts["strategy_gain_s"].value == 2.0
    assert facts["strategy_rejoin_position"].value == 7
    assert facts["strategy_traffic_risk"].value is True
    assert NOTE_SIMULATED in result.notes


def test_simulate_pit_in_laps_and_compound() -> None:
    evaluation = _evaluation(alternatives=_alternatives())

    result = simulate_strategy(_query("¿Y si paro en 3 vueltas con medios?"), _inputs(evaluation))

    facts = _facts(result)
    assert facts["strategy_action"].value == StrategyAction.PIT_IN.value
    assert facts["strategy_pit_lap"].value == 16
    assert facts["strategy_compound"].value == "medium"
    assert facts["strategy_gain_s"].value == 3.0


def test_simulate_compound_not_evaluated_for_requested_lap_reports_window() -> None:
    evaluation = _evaluation(alternatives=_alternatives())

    result = simulate_strategy(_query("¿Y si paro en 3 vueltas con blandos?"), _inputs(evaluation))

    assert result.quality is Quality.UNAVAILABLE
    assert result.missing == ("strategy_alternative",)
    assert NOTE_LAP_OUT_OF_WINDOW in result.notes


def test_simulate_current_compound_reports_rule() -> None:
    alternatives = tuple(
        item for item in _alternatives() if not item.key.startswith("pit_now:soft:")
    )
    evaluation = _evaluation(
        alternatives=alternatives,
        forecast_compound="soft",
    )

    result = simulate_strategy(_query("¿Y si paro ahora con blandos?"), _inputs(evaluation))

    assert NOTE_COMPOUND_RULE in result.notes


def test_simulate_unavailable_compound_reports_reason() -> None:
    evaluation = _evaluation(
        alternatives=(
            StrategyAlternative(
                key="pit_now:medium:set1",
                remaining_time_s=990.0,
                gain_vs_baseline_s=1.0,
            ),
        ),
        forecast_compound="soft",
    )

    result = simulate_strategy(_query("¿Y si paro ahora con duros?"), _inputs(evaluation))

    assert NOTE_COMPOUND_UNAVAILABLE in result.notes


def test_simulate_without_tyre_options_reports_compound_unavailable() -> None:
    evaluation = _evaluation(
        action=StrategyAction.INSUFFICIENT_DATA,
        alternatives=(),
        notes=(NOTE_NO_ALTERNATIVES,),
    )

    result = simulate_strategy(_query("¿Y si paro ahora?"), _inputs(evaluation))

    assert NOTE_COMPOUND_UNAVAILABLE in result.notes


def test_simulate_without_evaluation_declares_missing_data() -> None:
    result = simulate_strategy(_query("¿Y si paro en 3 vueltas?"), _inputs(None))

    assert result.quality is Quality.UNAVAILABLE
    assert result.missing == ("strategy_evaluation",)
    assert NOTE_EVALUATION_MISSING in result.notes
