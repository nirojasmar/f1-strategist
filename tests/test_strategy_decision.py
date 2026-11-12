"""Sensibilidad, decisión y confianza (S6)."""

from __future__ import annotations

import pytest

from f1_strategist.config import StrategyConfig
from f1_strategist.metrics.types import Quality
from f1_strategist.strategy.decision import (
    NOTE_ALTERNATIVE_QUALITY,
    NOTE_MANDATORY_STOP,
    NOTE_NO_ALTERNATIVES,
    NOTE_NO_LAPS_REMAINING,
    NOTE_NOT_A_RACE,
    NOTE_PACE_INSUFFICIENT,
    NOTE_PIT_LOSS_INSUFFICIENT,
    NOTE_STALE_DATA,
    NOTE_STAY_OUT_UNAVAILABLE,
    SENSITIVE_PIT_LOSS,
    SENSITIVE_WEAR_SLOPE,
    decide,
)
from f1_strategist.strategy.types import (
    BASELINE_STAY_OUT,
    RISK_RED_FLAG,
    RISK_SAFETY_CAR,
    RISK_WET_CONDITIONS,
    AlternativeResult,
    DataQuality,
    Perturbation,
    StrategyAction,
)


def _stay_out(gain: float = 0.0, key: str = BASELINE_STAY_OUT) -> AlternativeResult:
    return AlternativeResult(
        key=key,
        action=StrategyAction.NO_CHANGE,
        remaining_time_s=1800.0,
        gain_vs_baseline_s=gain,
        quality=Quality.OK,
    )


def _pit(
    key: str = "pit_now:hard",
    *,
    gain: float = 3.5,
    action: StrategyAction = StrategyAction.PIT_NOW,
    pit_lap: int | None = 10,
    compound: str | None = "hard",
    set_index: int | None = None,
    quality: Quality = Quality.OK,
) -> AlternativeResult:
    return AlternativeResult(
        key=key,
        action=action,
        remaining_time_s=1800.0 - gain,
        gain_vs_baseline_s=gain,
        quality=quality,
        pit_lap=pit_lap,
        compound=compound,
        set_index=set_index,
    )


def _quality(**fields: object) -> DataQuality:
    data: dict[str, object] = {
        "pace": Quality.OK,
        "pit_loss": Quality.OK,
        "session_is_race": True,
        "laps_remaining": 20.0,
        "stale": False,
        "pace_samples": 8,
        "prior_weight": 0.0,
    }
    data.update(fields)
    return DataQuality(**data)  # type: ignore[arg-type]


def test_clear_gain_recommends_the_best_stop() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        shift = perturbation.pit_loss_delta_s
        return (_stay_out(), _pit(gain=3.5 - shift))

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.action is StrategyAction.PIT_NOW
    assert decision.key == "pit_now:hard"
    assert decision.pit_lap == 10
    assert decision.compound == "hard"
    assert decision.gain_interval_s is not None
    low, high = decision.gain_interval_s
    assert low == pytest.approx(1.5)
    assert high == pytest.approx(5.5)
    assert 0.0 < decision.confidence <= 1.0


def test_gain_inside_the_noise_makes_no_change() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        shift = perturbation.pit_loss_delta_s
        return (_stay_out(), _pit(gain=2.0 - shift))

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.action is StrategyAction.NO_CHANGE
    assert decision.key == BASELINE_STAY_OUT
    assert decision.gain_interval_s is None
    assert decision.gain_vs_baseline_s is None


def test_mandatory_stop_is_reported_even_without_robust_gain() -> None:
    def evaluate(_perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (
            _pit(
                key="pit_in_4:hard:set2",
                gain=-2.0,
                action=StrategyAction.PIT_IN,
                pit_lap=15,
                set_index=2,
            ),
        )

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.action is StrategyAction.PIT_IN
    assert decision.key == "pit_in_4:hard:set2"
    assert decision.set_index == 2
    assert NOTE_MANDATORY_STOP in decision.notes


def test_unstable_mandatory_stop_is_not_used_as_a_fallback() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        if perturbation.pit_loss_delta_s > 0.0:
            return ()
        return (_pit(gain=-2.0),)

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.action is StrategyAction.INSUFFICIENT_DATA
    assert decision.confidence == 0.0
    assert NOTE_MANDATORY_STOP in decision.notes
    assert NOTE_ALTERNATIVE_QUALITY not in decision.notes


def test_stable_mandatory_stop_is_used_when_baseline_is_unstable() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        unstable = (_pit(key="pit_now:soft", gain=-1.0),)
        stable = _pit(key="pit_in_2:hard", gain=-2.0, action=StrategyAction.PIT_IN)
        if perturbation.slope_scale != 1.0:
            return (stable,)
        return (*unstable, stable)

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.action is StrategyAction.PIT_IN
    assert decision.key == "pit_in_2:hard"
    assert NOTE_MANDATORY_STOP in decision.notes


def test_allowed_but_unavailable_stay_out_is_not_treated_as_mandatory_stop() -> None:
    def evaluate(_perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (
            _pit(gain=-1.0),
            AlternativeResult(
                key="pit_in_1:hard",
                action=StrategyAction.PIT_IN,
                remaining_time_s=1801.0,
                gain_vs_baseline_s=-1.0,
                quality=Quality.OK,
                notes=(NOTE_STAY_OUT_UNAVAILABLE,),
            ),
        )

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.action is StrategyAction.INSUFFICIENT_DATA
    assert NOTE_STAY_OUT_UNAVAILABLE in decision.notes
    assert NOTE_MANDATORY_STOP not in decision.notes


def test_lower_bound_must_exceed_min_gain() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_stay_out(), _pit(gain=4.0))

    strict = decide(evaluate, _quality(), StrategyConfig(min_gain_s=4.5))
    relaxed = decide(evaluate, _quality(), StrategyConfig(min_gain_s=1.0))

    assert strict.action is StrategyAction.NO_CHANGE
    assert relaxed.action is StrategyAction.PIT_NOW


@pytest.mark.parametrize(
    ("fields", "note"),
    [
        ({"session_is_race": False}, NOTE_NOT_A_RACE),
        ({"laps_remaining": None}, NOTE_NO_LAPS_REMAINING),
        ({"laps_remaining": 0.0}, NOTE_NO_LAPS_REMAINING),
        ({"pace": Quality.INSUFFICIENT}, NOTE_PACE_INSUFFICIENT),
        ({"pace": Quality.UNAVAILABLE}, NOTE_PACE_INSUFFICIENT),
        ({"pit_loss": Quality.UNAVAILABLE}, NOTE_PIT_LOSS_INSUFFICIENT),
        ({"stale": True}, NOTE_STALE_DATA),
    ],
)
def test_missing_inputs_yield_insufficient_data(fields: dict[str, object], note: str) -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_stay_out(), _pit(gain=9.0))

    decision = decide(evaluate, _quality(**fields), StrategyConfig())

    assert decision.action is StrategyAction.INSUFFICIENT_DATA
    assert note in decision.notes
    assert decision.confidence == 0.0


@pytest.mark.parametrize(
    "condition",
    [RISK_WET_CONDITIONS, RISK_SAFETY_CAR, RISK_RED_FLAG],
)
def test_unmodeled_conditions_block_stop_recommendations(condition: str) -> None:
    def evaluate(_perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_stay_out(), _pit(gain=20.0))

    decision = decide(
        evaluate,
        _quality(unmodeled_conditions=(condition,)),
        StrategyConfig(),
    )

    assert decision.action is StrategyAction.INSUFFICIENT_DATA
    assert condition in decision.notes


def test_stale_candidate_is_not_selected_and_mandatory_stale_candidate_is_insufficient() -> None:
    def with_stay(_perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_stay_out(), _pit(gain=20.0, quality=Quality.STALE))

    def mandatory(_perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_pit(gain=0.0, quality=Quality.STALE),)

    no_stop = decide(with_stay, _quality(), StrategyConfig())
    no_data = decide(mandatory, _quality(), StrategyConfig())

    assert no_stop.action is StrategyAction.NO_CHANGE
    assert no_data.action is StrategyAction.INSUFFICIENT_DATA
    assert NOTE_ALTERNATIVE_QUALITY in no_data.notes


def test_candidate_quality_reduces_confidence() -> None:
    def evaluate(quality: Quality):
        return lambda _perturbation: (_stay_out(), _pit(gain=8.0, quality=quality))

    strong = decide(evaluate(Quality.OK), _quality(), StrategyConfig())
    weak = decide(evaluate(Quality.LOW), _quality(), StrategyConfig())

    assert weak.confidence < strong.confidence


def test_without_alternatives_the_decision_is_insufficient() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return ()

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.action is StrategyAction.INSUFFICIENT_DATA
    assert decision.notes == (NOTE_NO_ALTERNATIVES,)


def test_pit_loss_assumption_is_reported_as_sensitive() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_stay_out(), _pit(gain=2.5 - perturbation.pit_loss_delta_s))

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert SENSITIVE_PIT_LOSS in decision.sensitive_to
    assert SENSITIVE_WEAR_SLOPE not in decision.sensitive_to


def test_wear_slope_assumption_is_reported_as_sensitive() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        # La ganancia crece con la pendiente: subir la banda cambia la decisión.
        gain = 0.5 + (perturbation.slope_scale - 1.0) * 20.0
        return (_stay_out(), _pit(gain=gain))

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert SENSITIVE_WEAR_SLOPE in decision.sensitive_to


def test_insensitive_scenario_reports_nothing() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_stay_out(), _pit(gain=6.0))

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.sensitive_to == ()


def test_confidence_grows_with_data_quality() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_stay_out(), _pit(gain=4.0))

    strong = decide(evaluate, _quality(pace=Quality.OK, pace_samples=20), StrategyConfig())
    weak = decide(
        evaluate,
        _quality(pace=Quality.LOW, pace_samples=3, prior_weight=0.5),
        StrategyConfig(),
    )

    assert weak.confidence < strong.confidence


def test_confidence_falls_with_wider_intervals() -> None:
    def narrow(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_stay_out(), _pit(gain=5.0))

    def wide(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        return (_stay_out(), _pit(gain=5.0 - 3.0 * perturbation.pit_loss_delta_s))

    narrow_decision = decide(narrow, _quality(), StrategyConfig())
    wide_decision = decide(wide, _quality(), StrategyConfig())

    assert wide_decision.confidence < narrow_decision.confidence


def test_decision_is_deterministic() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        shift = perturbation.pit_loss_delta_s
        return (_stay_out(), _pit(gain=3.0 - shift), _pit(key="pit_in_2:soft", gain=2.0))

    first = decide(evaluate, _quality(), StrategyConfig())
    second = decide(evaluate, _quality(), StrategyConfig())

    assert first == second
    assert first.to_dict() == second.to_dict()


def test_unstable_alternative_is_not_recommended() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        if perturbation.pit_loss_delta_s > 0.0:
            return (_stay_out(),)
        return (_stay_out(), _pit(gain=5.0))

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.action is StrategyAction.NO_CHANGE


def test_pit_in_is_selected_when_it_wins() -> None:
    def evaluate(perturbation: Perturbation) -> tuple[AlternativeResult, ...]:
        shift = perturbation.pit_loss_delta_s
        return (
            _stay_out(),
            _pit(gain=2.0 - shift),
            _pit(key="pit_in_2:hard", gain=4.0 - shift, action=StrategyAction.PIT_IN, pit_lap=12),
        )

    decision = decide(evaluate, _quality(), StrategyConfig())

    assert decision.action is StrategyAction.PIT_IN
    assert decision.key == "pit_in_2:hard"
    assert decision.pit_lap == 12


def test_decision_round_trips_through_json() -> None:
    from f1_strategist.strategy.types import StrategyDecision

    decision = StrategyDecision(
        action=StrategyAction.PIT_IN,
        key="pit_in_2:hard",
        pit_lap=12,
        compound="hard",
        gain_vs_baseline_s=2.4,
        gain_interval_s=(0.6, 4.1),
        confidence=0.58,
        sensitive_to=("pit_loss_s",),
        notes=("b", "a"),
    )

    restored = StrategyDecision.from_dict(decision.to_dict())

    assert restored == decision
    assert restored.to_dict() == decision.to_dict()
