from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, StrategyConfig, StrategyTrackOverride
from f1_strategist.metrics.history import LapHistorySnapshot, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import RaceState, RaceStateSnapshot
from f1_strategist.strategy.engine import StrategyEngine, _projected_lap_forecast
from f1_strategist.strategy.pace_model import CompoundModel, CompoundModels
from f1_strategist.strategy.types import (
    STRATEGY_SCHEMA_VERSION,
    DataQuality,
    StrategyAction,
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
