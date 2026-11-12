from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from f1_strategist.config import StrategyConfig, StrategyTrackOverride
from f1_strategist.metrics.history import LapHistorySnapshot, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import RaceState, RaceStateSnapshot
from f1_strategist.strategy.engine import StrategyEngine
from f1_strategist.strategy.types import (
    STRATEGY_SCHEMA_VERSION,
    StrategyAction,
    StrategyEvaluation,
    StrategyRecommendation,
)


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
