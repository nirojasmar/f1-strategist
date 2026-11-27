from __future__ import annotations

from dataclasses import replace
from math import ceil
from time import perf_counter

import pytest

from f1_strategist.config import PilotConfig
from f1_strategist.events.types import EventsSnapshot
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot
from f1_strategist.pilot.engine import PilotEngine, PilotInputs
from f1_strategist.pilot.evaluation import (
    DEFAULT_PILOT_DATASET,
    build_race_state_from_grid,
    load_pilot_dataset,
)
from f1_strategist.pilot.types import AnswerKind, Intent
from f1_strategist.race_state import PacketFreshness
from f1_strategist.strategy.types import StrategySnapshot


@pytest.mark.benchmark
def test_pilot_deterministic_answer_mean_under_20ms_and_p99_under_50ms() -> None:
    dataset = load_pilot_dataset(DEFAULT_PILOT_DATASET)
    state = replace(
        build_race_state_from_grid(dataset.default_grid),
        freshness=(PacketFreshness("lap_data", 9.8, 1.5),),
    )
    inputs = PilotInputs(
        state=state,
        metrics=MetricsSnapshot(),
        strategy=StrategySnapshot(),
        history=LapHistorySnapshot(),
        events=EventsSnapshot(),
    )
    engine = PilotEngine(PilotConfig(), snapshots=lambda: inputs)
    question = "¿Cuánto combustible queda?"

    answer = engine.ask(question, now=10.0)
    for _ in range(9):
        answer = engine.ask(question, now=10.0)
    assert answer.intent is Intent.FUEL_STATUS
    assert answer.kind in {AnswerKind.ANSWER, AnswerKind.UNAVAILABLE}

    samples_s: list[float] = []
    for _ in range(300):
        started = perf_counter()
        answer = engine.ask(question, now=10.0)
        samples_s.append(perf_counter() - started)

    mean_s = sum(samples_s) / len(samples_s)
    p99_s = sorted(samples_s)[ceil(0.99 * len(samples_s)) - 1]
    if mean_s >= 0.020 or p99_s >= 0.050:
        pytest.fail(
            "Pilot latency threshold exceeded: "
            f"question={question!r}, intent={answer.intent.value}, "
            "tool=fuel_status, expected=mean<20ms and p99<50ms, "
            f"mean={mean_s * 1000:.3f}ms, p99={p99_s * 1000:.3f}ms"
        )
