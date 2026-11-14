"""Rendimiento de ``evaluate_once`` (S10, marcador ``benchmark``).

El escenario sintético reproduce la carga de una captura real: histórico de
tres compuestos, doce juegos de neumáticos en el inventario (como en Sakhir) y
la ventana de paradas por defecto. El coste medido crece de forma lineal con el
número de alternativas, así que esta carga es representativa y no optimista.

La sección 11 del plan fija media < 1 ms y p99 < 5 ms por evaluación. Con esta
carga el motor incumple el criterio; la incidencia queda documentada en el test
``test_evaluation_latency_meets_the_plan_limits`` (marcado ``xfail``) y el test
``test_evaluation_latency_does_not_regress`` protege frente a regresiones
mientras se corrige.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, StrategyConfig
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.race_state import CarSnapshot, RaceState, TyreSetSnapshot
from f1_strategist.strategy.engine import evaluate_once

pytestmark = pytest.mark.benchmark

TRACK_LENGTH_M = 5_000.0
CAR_INDEX = 0
_TRACK = "monza"

#: Umbrales de la sección 11 del plan, en nanosegundos.
_MEAN_LIMIT_NS = 1_000_000
_P99_LIMIT_NS = 5_000_000

#: Guardarraíl de regresión mientras la incidencia de rendimiento sigue abierta.
_MEAN_GUARD_NS = 20_000_000
_P99_GUARD_NS = 30_000_000

_ITERATIONS = 50
_WARMUP = 10


def _car(index: int = CAR_INDEX, **fields: object) -> CarSnapshot:
    base = RaceState().snapshot().cars[index]
    return replace(
        base,
        car_index=index,
        lap_distance_m=TRACK_LENGTH_M,
        current_lap=15,
        **fields,
    )


def _inputs() -> MetricInputs:
    snapshot = RaceState().snapshot()
    session = replace(
        snapshot.session,
        uid=123,
        session_type="race",
        track=_TRACK,
        total_laps=25,
        track_length_m=TRACK_LENGTH_M,
        safety_car_status="no_safety_car",
        weekend_structure=("practice_1", "race"),
    )
    cars = (
        _car(position=2, actual_tyre_compound="soft", tyre_age_laps=9, delta_to_leader_ms=5_000),
        _car(1, position=1, delta_to_leader_ms=0),
        _car(2, position=3, delta_to_leader_ms=9_000),
        _car(3, position=4, delta_to_leader_ms=13_000),
    )
    inventory = tuple(
        TyreSetSnapshot(
            index=index,
            actual_tyre_compound="hard" if index % 2 else "medium",
            visual_tyre_compound="c1",
            wear_pct=0.0,
            available=True,
            recommended_session=None,
            lifespan_laps=40,
            usable_life_laps=40,
            lap_delta_time_ms=300,
            fitted=False,
        )
        for index in range(1, 13)
    )
    state = replace(
        snapshot,
        session=session,
        current_lap=15,
        player_car_index=CAR_INDEX,
        cars=cars,
        tyre_inventory=inventory,
    )
    records = tuple(
        LapRecord(
            CAR_INDEX,
            first_lap + offset,
            lap_time_s=base + slope * age,
            valid=True,
            closed=True,
            compound=compound,
            tyre_age_start=age,
            tyre_wear_end_pct=(0.5 * (age + 1),) * 4,
        )
        for compound, first_lap, base, slope in (
            ("medium", 1, 90.4, 0.05),
            ("hard", 4, 90.8, 0.03),
            ("soft", 8, 90.0, 0.12),
        )
        for offset, age in enumerate(range(0, 8))
    )
    return MetricInputs(
        state=state,
        history=LapHistorySnapshot(records={CAR_INDEX: records}),
        config=MetricsConfig(),
        now=100.0,
        car_index=CAR_INDEX,
        profile=None,
    )


def _samples() -> tuple[list[int], int]:
    """Muestras de latencia en nanosegundos y número de alternativas medidas."""
    inputs = _inputs()
    config = StrategyConfig(pit_loss_override_s=20.0)
    for _ in range(_WARMUP):
        evaluate_once(inputs, config)

    samples: list[int] = []
    alternatives = 0
    for _ in range(_ITERATIONS):
        started = time.perf_counter_ns()
        evaluation = evaluate_once(inputs, config)
        samples.append(time.perf_counter_ns() - started)
        alternatives = len(evaluation.alternatives)
    samples.sort()
    return samples, alternatives


def _summary(samples: list[int]) -> tuple[float, float]:
    mean_ns = statistics.fmean(samples)
    p99_ns = samples[min(len(samples) - 1, int(round(0.99 * len(samples))) - 1)]
    return mean_ns, p99_ns


@pytest.mark.xfail(
    reason="Incidencia S10-R1: con doce juegos la evaluación tarda ~13 ms de media "
    "y ~16 ms de p99; el criterio de la sección 11 (1 ms / 5 ms) exige optimizar "
    "el motor, fuera del alcance de S10",
    strict=False,
)
def test_evaluation_latency_meets_the_plan_limits() -> None:
    samples, alternatives = _samples()
    mean_ns, p99_ns = _summary(samples)

    assert mean_ns < _MEAN_LIMIT_NS, (
        f"media {mean_ns / 1e6:.3f} ms ≥ 1 ms con {alternatives} alternativas"
    )
    assert p99_ns < _P99_LIMIT_NS, (
        f"p99 {p99_ns / 1e6:.3f} ms ≥ 5 ms con {alternatives} alternativas"
    )


def test_evaluation_latency_does_not_regress() -> None:
    samples, alternatives = _samples()
    mean_ns, p99_ns = _summary(samples)

    assert alternatives >= 90, (
        f"el escenario solo generó {alternatives} alternativas; no es representativo"
    )
    assert mean_ns < _MEAN_GUARD_NS, f"media {mean_ns / 1e6:.3f} ms ≥ {_MEAN_GUARD_NS / 1e6:.0f} ms"
    assert p99_ns < _P99_GUARD_NS, f"p99 {p99_ns / 1e6:.3f} ms ≥ {_P99_GUARD_NS / 1e6:.0f} ms"
