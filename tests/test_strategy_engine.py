from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, StrategyConfig, StrategyTrackOverride
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import (
    PacketFreshness,
    RaceEvent,
    RaceState,
    RaceStateSnapshot,
    TyreSetSnapshot,
)
from f1_strategist.strategy.engine import (
    StrategyEngine,
    _projected_lap_forecast,
    _risks,
    _unmodeled_conditions,
)
from f1_strategist.strategy.pace_model import CompoundModel, CompoundModels
from f1_strategist.strategy.types import (
    RISK_TYRE_LIFE_EXCEEDED,
    STRATEGY_SCHEMA_VERSION,
    AlternativeResult,
    DataQuality,
    StrategyAction,
    StrategyDecision,
    StrategyEvaluation,
    StrategyRecommendation,
)
from f1_strategist.strategy.tyre_options import TyreOptions


def _state(lap: int) -> RaceStateSnapshot:
    snapshot = RaceState().snapshot()
    session = replace(
        snapshot.session,
        uid=123,
        session_type="race",
        track="monza",
        total_laps=20,
        track_length_m=5_000.0,
    )
    return replace(snapshot, session=session, current_lap=lap)


def test_forecast_contains_full_laps_after_the_evaluation_lap() -> None:
    snapshot = RaceState().snapshot()
    session = replace(snapshot.session, session_type="race", total_laps=10)
    car = replace(snapshot.cars[0], current_lap=4)
    state = replace(
        snapshot,
        session=session,
        player_car_index=0,
        cars=(car, *snapshot.cars[1:]),
    )
    inputs = MetricInputs(
        state=state,
        history=LapHistorySnapshot(),
        config=MetricsConfig(),
        now=1.0,
        car_index=0,
    )
    models = CompoundModels(
        models=(
            CompoundModel(
                compound="soft",
                base_lap_s=90.0,
                slope_s_per_lap=0.5,
                wear_rate_pct_per_lap=None,
                n_race_laps=4,
                source="race",
                quality=Quality.OK,
                initial_age_laps=5.0,
            ),
        )
    )

    projected = _projected_lap_forecast(
        inputs,
        models,
        TyreOptions(current_compound="soft"),
        DataQuality(session_is_race=True),
    )

    assert projected == (93.0, 93.5, 94.0, 94.5, 95.0, 95.5)


def test_evaluate_once_attaches_forecast_to_the_serialized_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from f1_strategist.strategy.engine import evaluate_once

    snapshot = RaceState().snapshot()
    session = replace(
        snapshot.session,
        uid=123,
        session_type="race",
        track="monza",
        safety_car_status="no_safety_car",
        total_laps=10,
        track_length_m=5_000.0,
    )
    car = replace(
        snapshot.cars[0],
        current_lap=4,
        actual_tyre_compound="soft",
    )
    state = replace(
        snapshot,
        session=session,
        player_car_index=0,
        cars=(car, *snapshot.cars[1:]),
    )
    inputs = MetricInputs(
        state=state,
        history=LapHistorySnapshot(),
        config=MetricsConfig(),
        now=1.0,
        car_index=0,
    )
    forecast = (93.0, 93.5, 94.0)
    monkeypatch.setattr(
        "f1_strategist.strategy.engine._projected_lap_forecast",
        lambda *_args: forecast,
    )

    evaluation = evaluate_once(inputs, StrategyConfig())

    assert evaluation.forecast_car_index == 0
    assert evaluation.forecast_compound == "soft"
    assert evaluation.projected_lap_times_s == forecast
    assert evaluation.pit_loss_source == "circuit"
    assert evaluation.pit_loss_s == pytest.approx(24.14)
    assert evaluation.pit_loss_band_s == pytest.approx(2.0)


def _evaluation(
    revision: int,
    lap: int,
    action: StrategyAction,
) -> StrategyEvaluation:
    return StrategyEvaluation(
        revision=revision,
        session_uid="123",
        lap=lap,
        laps_remaining=None,
        baseline="stay_out",
        recommendation=StrategyRecommendation(action=action, key=action.value),
        alternatives=(),
        assumptions=(),
        risks=(),
        sensitive_to=(),
        data_quality=Quality.UNAVAILABLE,
        inputs_hash="a" * 64,
        schema_version=STRATEGY_SCHEMA_VERSION,
    )


def test_engine_evaluates_only_relevant_changes_after_the_minimum_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int | None, float]] = []

    def evaluate(
        inputs: MetricInputs,
        config: StrategyConfig,
        *,
        revision: int = 0,
    ) -> StrategyEvaluation:
        calls.append((inputs.state.current_lap, config.min_gain_s))
        return _evaluation(
            revision,
            inputs.state.current_lap or 1,
            StrategyAction.NO_CHANGE,
        )

    monkeypatch.setattr("f1_strategist.strategy.engine.evaluate_once", evaluate)
    config = StrategyConfig(
        evaluation_min_interval_s=1.0,
        tracks={"monza": StrategyTrackOverride(min_gain_s=2.5)},
    )
    engine = StrategyEngine(config)
    history = LapHistorySnapshot()

    engine.apply(_state(1), None, history, None, 0.0)
    engine.apply(_state(1), None, history, None, 0.5)
    engine.apply(_state(2), None, history, None, 0.75)
    assert calls == [(1, 2.5)]

    engine.apply(_state(2), None, history, None, 1.0)
    engine.apply(_state(2), None, history, None, 2.0)
    assert calls == [(1, 2.5), (2, 2.5)]


def test_engine_revision_changes_only_when_the_recommendation_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = iter(
        (
            StrategyAction.NO_CHANGE,
            StrategyAction.NO_CHANGE,
            StrategyAction.PIT_NOW,
            StrategyAction.PIT_NOW,
        )
    )

    def evaluate(
        inputs: MetricInputs,
        _config: StrategyConfig,
        *,
        revision: int = 0,
    ) -> StrategyEvaluation:
        return _evaluation(revision, inputs.state.current_lap or 1, next(actions))

    monkeypatch.setattr("f1_strategist.strategy.engine.evaluate_once", evaluate)
    engine = StrategyEngine(StrategyConfig(evaluation_min_interval_s=0.1))
    history = LapHistorySnapshot()
    revisions: list[int] = []

    for lap in range(1, 5):
        engine.apply(_state(lap), None, history, None, float(lap))
        evaluation = engine.snapshot().evaluation
        assert evaluation is not None
        revisions.append(evaluation.revision)

    assert revisions == [0, 0, 1, 1]
    assert engine.revision == 1


def test_engine_logs_failure_and_publishes_insufficient_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("model failed")

    monkeypatch.setattr("f1_strategist.strategy.engine.evaluate_once", fail)
    engine = StrategyEngine()

    with caplog.at_level(logging.ERROR, logger="f1_strategist.strategy.engine"):
        engine.apply(_state(1), None, LapHistorySnapshot(), None, 0.0)

    snapshot = engine.snapshot()
    assert snapshot.evaluation is not None
    assert snapshot.evaluation.recommendation.action is StrategyAction.INSUFFICIENT_DATA
    assert snapshot.error == "RuntimeError: model failed"
    assert "Strategy evaluation failed" in caplog.text


def test_unmodeled_rain_neutralization_and_red_flag_are_detected() -> None:
    snapshot = RaceState().snapshot()
    inputs = MetricInputs(snapshot, LapHistorySnapshot(), MetricsConfig(), 1.0, -1)
    options = TyreOptions(wet_conditions=True)

    rainy = _unmodeled_conditions(inputs, options)
    neutralized = _unmodeled_conditions(
        replace(
            inputs,
            state=replace(
                snapshot, session=replace(snapshot.session, safety_car_status="virtual_safety_car")
            ),
        ),
        TyreOptions(),
    )
    red_flag = _unmodeled_conditions(
        replace(
            inputs,
            state=replace(
                snapshot, session=replace(snapshot.session, safety_car_status="red_flag")
            ),
        ),
        TyreOptions(),
    )

    assert rainy == ("wet_conditions",)
    assert neutralized == ("safety_car_conditions",)
    assert red_flag == ("red_flag_conditions",)


def test_red_flag_event_blocks_until_a_normal_post_restart_lap_is_closed() -> None:
    snapshot = _state(2)
    event = RaceEvent("red_flag", 5.0, session_uid=123)
    state = replace(snapshot, events=(event,))
    options = TyreOptions()

    during = MetricInputs(state, LapHistorySnapshot(), MetricsConfig(), 6.0, 0)
    restart_lap = MetricInputs(
        state,
        LapHistorySnapshot(
            records={
                0: (
                    LapRecord(
                        car_index=0,
                        lap_number=2,
                        ended_at_monotonic=6.0,
                        track_status_seen=frozenset({"red_flag"}),
                    ),
                )
            }
        ),
        MetricsConfig(),
        6.0,
        0,
    )
    recovered = MetricInputs(
        state,
        LapHistorySnapshot(
            records={
                0: (
                    LapRecord(
                        car_index=0,
                        lap_number=3,
                        ended_at_monotonic=7.0,
                    ),
                )
            }
        ),
        MetricsConfig(),
        7.0,
        0,
    )

    assert "red_flag_conditions" in _unmodeled_conditions(during, options)
    assert "red_flag_conditions" in _unmodeled_conditions(restart_lap, options)
    assert "red_flag_conditions" not in _unmodeled_conditions(recovered, options)


def test_evaluation_risks_only_include_the_selected_alternatives_risks() -> None:
    safe = AlternativeResult(
        key="stay_out",
        action=StrategyAction.NO_CHANGE,
        remaining_time_s=100.0,
        projected_laps_s=100.0,
        quality=Quality.OK,
        gain_vs_baseline_s=0.0,
    )
    risky = AlternativeResult(
        key="pit_now:hard:set2",
        action=StrategyAction.PIT_NOW,
        remaining_time_s=99.0,
        projected_laps_s=79.0,
        quality=Quality.OK,
        gain_vs_baseline_s=1.0,
        notes=(RISK_TYRE_LIFE_EXCEEDED,),
    )

    assert RISK_TYRE_LIFE_EXCEEDED not in _risks(
        (safe, risky),
        StrategyDecision(action=StrategyAction.NO_CHANGE, key="stay_out"),
        (),
    )
    assert RISK_TYRE_LIFE_EXCEEDED in _risks(
        (safe, risky),
        StrategyDecision(action=StrategyAction.PIT_NOW, key="pit_now:hard:set2"),
        (),
    )


def test_engine_rechecks_changed_inputs_and_freshness_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def evaluate(
        inputs: MetricInputs,
        _config: StrategyConfig,
        *,
        revision: int = 0,
    ) -> StrategyEvaluation:
        calls.append(len(inputs.history.records_for(0)))
        return _evaluation(revision, inputs.state.current_lap or 1, StrategyAction.NO_CHANGE)

    monkeypatch.setattr("f1_strategist.strategy.engine.evaluate_once", evaluate)
    base = _state(1)
    car = replace(base.cars[0], current_lap=1, lap_distance_m=0.0)
    freshness = PacketFreshness("lap_data", 0.0, 3.0)
    state = replace(
        base,
        player_car_index=0,
        cars=(car, *base.cars[1:]),
        freshness=(freshness,),
    )
    initial_history = LapHistorySnapshot()
    updated_history = LapHistorySnapshot(
        records={0: (LapRecord(car_index=0, lap_number=1, compound="soft"),)}
    )
    inventory = TyreSetSnapshot(
        index=2,
        actual_tyre_compound="hard",
        visual_tyre_compound="hard",
        wear_pct=0.0,
        available=True,
        recommended_session="race",
        lifespan_laps=30,
        usable_life_laps=25,
        lap_delta_time_ms=0,
        fitted=False,
    )
    engine = StrategyEngine(StrategyConfig(evaluation_min_interval_s=1.0))

    engine.apply(state, None, initial_history, None, 1.0)
    engine.apply(state, None, updated_history, None, 1.5)
    engine.apply(state, None, updated_history, None, 2.0)
    engine.apply(state, None, updated_history, None, 10.0)
    engine.apply(replace(state, tyre_inventory=(inventory,)), None, updated_history, None, 11.0)

    assert calls == [0, 1, 1, 1]


def test_engine_retries_failure_and_revisions_both_error_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = iter((StrategyAction.PIT_NOW, RuntimeError("transient"), StrategyAction.NO_CHANGE))

    def evaluate(
        inputs: MetricInputs,
        _config: StrategyConfig,
        *,
        revision: int = 0,
    ) -> StrategyEvaluation:
        result = next(actions)
        if isinstance(result, RuntimeError):
            raise result
        return _evaluation(revision, inputs.state.current_lap or 1, result)

    monkeypatch.setattr("f1_strategist.strategy.engine.evaluate_once", evaluate)
    engine = StrategyEngine(StrategyConfig(evaluation_min_interval_s=1.0))
    state = _state(1)
    changed = replace(state, session=replace(state.session, weather="clear"))

    engine.apply(state, None, LapHistorySnapshot(), None, 1.0)
    first = engine.snapshot().evaluation
    engine.apply(changed, None, LapHistorySnapshot(), None, 2.0)
    failed = engine.snapshot().evaluation
    engine.apply(changed, None, LapHistorySnapshot(), None, 2.5)
    assert engine.snapshot().error is not None
    engine.apply(changed, None, LapHistorySnapshot(), None, 3.1)
    recovered = engine.snapshot().evaluation

    assert first is not None and first.revision == 0
    assert failed is not None and failed.revision == 1
    assert recovered is not None and recovered.revision == 2
    assert recovered.recommendation.action is StrategyAction.NO_CHANGE
    assert engine.snapshot().error is None
