from __future__ import annotations

from math import ceil
from time import perf_counter

import pytest

from f1_strategist.config import MetricsConfig, PilotConfig, StrategyConfig
from f1_strategist.events.types import EventsSnapshot
from f1_strategist.metrics.engine import MetricsEngine
from f1_strategist.pilot.engine import PilotEngine, PilotInputs
from f1_strategist.pilot.types import AnswerKind, Intent
from f1_strategist.race_state import RaceState
from f1_strategist.strategy.engine import StrategyEngine

from .scenarios import ScenarioCar, build_race_timed


def _build_populated_pilot_inputs() -> tuple[PilotInputs, float]:
    cars = (
        ScenarioCar(
            index=0,
            name="Alex Albon",
            grid_position=2,
            base_lap_time_s=90.5,
            initial_fuel_kg=30.0,
            fuel_per_lap_kg=2.0,
            tyre_wear_per_lap_pct=3.5,
        ),
        ScenarioCar(
            index=1,
            name="Lewis Hamilton",
            grid_position=1,
            base_lap_time_s=90.2,
            initial_fuel_kg=30.0,
            fuel_per_lap_kg=2.0,
            tyre_wear_per_lap_pct=3.0,
        ),
        ScenarioCar(
            index=2,
            name="Charles Leclerc",
            grid_position=3,
            base_lap_time_s=90.8,
            initial_fuel_kg=30.0,
            fuel_per_lap_kg=2.0,
            tyre_wear_per_lap_pct=3.2,
        ),
    )
    state = RaceState()
    metrics_engine = MetricsEngine(MetricsConfig())
    strategy_engine = StrategyEngine(StrategyConfig())
    last_observed_at = 0.0
    for packet, observed_at in build_race_timed(cars, total_laps=10, samples_per_lap=2):
        state.apply(packet, observed_at)
        state_snapshot = state.snapshot()
        metrics_engine.apply(packet, observed_at, state_snapshot)
        strategy_engine.apply(
            state_snapshot,
            metrics_engine.snapshot(),
            metrics_engine.history.snapshot(),
            None,
            observed_at,
        )
        last_observed_at = observed_at

    final_state = state.snapshot()
    metrics_engine.finalize(final_state, last_observed_at)
    final_metrics = metrics_engine.snapshot()
    final_history = metrics_engine.history.snapshot()
    strategy_engine.apply(final_state, final_metrics, final_history, None, last_observed_at)
    inputs = PilotInputs(
        state=final_state,
        metrics=final_metrics,
        strategy=strategy_engine.snapshot(),
        history=final_history,
        events=EventsSnapshot(),
    )
    return inputs, last_observed_at


@pytest.mark.benchmark
def test_pilot_deterministic_answer_mean_under_20ms_and_p99_under_50ms() -> None:
    inputs, now = _build_populated_pilot_inputs()
    engine = PilotEngine(PilotConfig(), snapshots=lambda: inputs)
    batch: tuple[tuple[str, Intent], ...] = (
        ("¿Cuánto combustible queda?", Intent.FUEL_STATUS),
        ("¿Cómo voy respecto a Hamilton?", Intent.COMPARE_DRIVER),
        ("¿Por qué estoy perdiendo tiempo?", Intent.PACE_DIAGNOSIS),
        ("¿Me puede hacer undercut Hamilton?", Intent.PIT_UNDERCUT),
        ("¿Cuál es la estrategia?", Intent.ASK_STRATEGY),
    )

    # Calentamiento y verificación de contrato sobre snapshots poblados.
    for question, expected_intent in batch:
        answer = engine.ask(question, now=now)
        assert answer.intent is expected_intent
        assert answer.kind is AnswerKind.ANSWER, (
            f"Expected AnswerKind.ANSWER for {question!r} ({expected_intent.value}), "
            f"got {answer.kind.value}: {answer.text}"
        )

    samples_s: list[float] = []
    worst: tuple[float, str, Intent] = (0.0, batch[0][0], batch[0][1])
    for idx in range(300):
        question, expected_intent = batch[idx % len(batch)]
        started = perf_counter()
        answer = engine.ask(question, now=now)
        elapsed = perf_counter() - started
        assert answer.intent is expected_intent
        assert answer.kind is AnswerKind.ANSWER
        samples_s.append(elapsed)
        if elapsed > worst[0]:
            worst = (elapsed, question, expected_intent)

    mean_s = sum(samples_s) / len(samples_s)
    p99_s = sorted(samples_s)[ceil(0.99 * len(samples_s)) - 1]
    if mean_s >= 0.020 or p99_s >= 0.050:
        pytest.fail(
            "Pilot latency threshold exceeded: "
            f"worst_question={worst[1]!r}, worst_intent={worst[2].value}, "
            "expected=mean<20ms and p99<50ms, "
            f"mean={mean_s * 1000:.3f}ms, p99={p99_s * 1000:.3f}ms"
        )
