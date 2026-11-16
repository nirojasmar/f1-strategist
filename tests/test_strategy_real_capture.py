"""Validación de la estrategia sobre las capturas reales del manifiesto (S10).

Cada escenario se reproduce una vez con ``run_capture_offline`` y se evalúa la
estrategia al cierre de cada vuelta de carrera, sin perfil de fin de semana.
Se comprueba que no hay excepciones, que toda acción publicada es explícita y
que la recomendación es estable en ventanas de cinco vueltas.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from f1_strategist.config import MetricsConfig, StrategyConfig
from f1_strategist.metrics.engine import MetricsEngine
from f1_strategist.metrics.history import MetricInputs
from f1_strategist.offline import run_capture_offline
from f1_strategist.parser import ParsedPacket
from f1_strategist.race_state import RaceState
from f1_strategist.strategy.engine import evaluate_once
from f1_strategist.strategy.types import StrategyAction, StrategyEvaluation

pytestmark = pytest.mark.real_capture

_REAL = Path(__file__).parent / "fixtures" / "real"
_MANIFEST = _REAL / "manifest.toml"

_RACE_SESSIONS = frozenset({"race", "race_2", "race_3"})

#: Cambios de acción admitidos dentro de una ventana de cinco vueltas.
_MAX_CHANGES_PER_WINDOW = 2
_WINDOW_LAPS = 5


@dataclass(frozen=True, slots=True)
class _Scenario:
    """Entrada del manifiesto relevante para la estrategia."""

    name: str
    path: Path


def _race_scenarios() -> tuple[_Scenario, ...]:
    """Escenarios del manifiesto cuya sesión principal es una carrera."""
    with _MANIFEST.open("rb") as handle:
        data = tomllib.load(handle)
    scenarios: list[_Scenario] = []
    for entry in data.get("scenario", ()):
        expected = entry.get("expected", {})
        if expected.get("session_type") != "race":
            continue
        scenarios.append(_Scenario(str(entry["id"]), _REAL / str(entry["file"])))
    return tuple(scenarios)


class _StrategyReplay:
    """Consumidor offline que evalúa la estrategia al cierre de cada vuelta."""

    def __init__(self) -> None:
        self.metrics = MetricsEngine(MetricsConfig())
        self.state = RaceState()
        self.evaluations: list[tuple[int, StrategyEvaluation]] = []
        self._lap: int | None = None

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.state.apply(packet, observed_at)
        snapshot = self.state.snapshot()
        self.metrics.apply(packet, observed_at, snapshot)
        session = snapshot.session
        if (session.session_type or "").lower() not in _RACE_SESSIONS:
            return
        player = snapshot.player_car
        lap = player.current_lap if player is not None else snapshot.current_lap
        if lap is None or lap == self._lap:
            return
        self._lap = lap
        inputs = MetricInputs(
            state=snapshot,
            history=self.metrics.history.snapshot(),
            config=self.metrics.config,
            now=observed_at,
            car_index=snapshot.player_car_index if snapshot.player_car_index is not None else -1,
            profile=None,
        )
        self.evaluations.append((lap, evaluate_once(inputs, StrategyConfig())))


def _replay_once(
    scenario: _Scenario, *, clock_scale: float | None = None
) -> tuple[tuple[int, StrategyEvaluation], ...]:
    replay = _StrategyReplay()
    result = run_capture_offline(scenario.path, [replay], clock_scale=clock_scale)
    assert not result.integrity_problems(), (
        f"{scenario.name}: captura íntegra requerida; " + "; ".join(result.integrity_problems())
    )
    return tuple(replay.evaluations)


@cache
def _replay(
    scenario: _Scenario, clock_scale: float | None = None
) -> tuple[tuple[int, StrategyEvaluation], ...]:
    """Reproducción memorizada: cada escenario se procesa una vez por escala."""
    return _replay_once(scenario, clock_scale=clock_scale)


def _signature(evaluation: StrategyEvaluation) -> tuple[str, str, float | None]:
    recommendation = evaluation.recommendation
    gain = recommendation.gain_vs_baseline_s
    return (
        recommendation.action.value,
        recommendation.key,
        None if gain is None else round(gain, 9),
    )


def _max_action_changes(
    evaluations: tuple[tuple[int, StrategyEvaluation], ...],
) -> int:
    actions = [evaluation.recommendation.action for _, evaluation in evaluations]
    worst = 0
    for start in range(len(actions)):
        window = actions[start : start + _WINDOW_LAPS]
        changes = sum(
            1 for before, after in zip(window, window[1:], strict=False) if before is not after
        )
        worst = max(worst, changes)
    return worst


@pytest.mark.parametrize("scenario", _race_scenarios(), ids=lambda item: item.name)
def test_real_capture_evaluates_every_race_lap_without_inventing_a_stop(
    scenario: _Scenario,
) -> None:
    if not scenario.path.is_file():
        pytest.skip(f"falta la captura {scenario.path.name}")
    evaluations = _replay(scenario)

    assert evaluations, f"{scenario.name}: no se evaluó ninguna vuelta de carrera"
    for lap, evaluation in evaluations:
        assert isinstance(evaluation.recommendation.action, StrategyAction), (
            f"{scenario.name}, vuelta {lap}: acción inválida"
        )
        if evaluation.recommendation.action in {StrategyAction.PIT_NOW, StrategyAction.PIT_IN}:
            assert evaluation.recommendation.key, f"{scenario.name}, vuelta {lap}: parada sin clave"
            assert evaluation.recommendation.gain_vs_baseline_s is not None, (
                f"{scenario.name}, vuelta {lap}: parada sin ganancia"
            )
        if evaluation.recommendation.action is StrategyAction.INSUFFICIENT_DATA:
            assert evaluation.recommendation.notes, (
                f"{scenario.name}, vuelta {lap}: datos insuficientes sin motivo"
            )
    assert _max_action_changes(evaluations) <= _MAX_CHANGES_PER_WINDOW, (
        f"{scenario.name}: la recomendación cambia más de "
        f"{_MAX_CHANGES_PER_WINDOW} veces en {_WINDOW_LAPS} vueltas"
    )


def test_repeated_replays_are_deterministic() -> None:
    """Dos reproducciones idénticas producen las mismas evaluaciones completas."""
    scenario = _smallest_race_scenario()
    first = _replay_once(scenario)
    second = _replay_once(scenario)

    assert [evaluation.to_dict() for _, evaluation in first] == [
        evaluation.to_dict() for _, evaluation in second
    ]


def test_clock_scale_does_not_change_the_recommendation_on_this_capture() -> None:
    """Observación sobre la captura: comprimir el reloj no cambió la acción.

    No es una garantía general: los umbrales de obsolescencia están en segundos
    absolutos y comprimir el reloj puede cambiar la frescura (y la huella). La
    invariancia del reloj de referencia se comprueba con ``play`` en
    ``tests/test_playback.py``.
    """
    scenario = _smallest_race_scenario()
    baseline = _replay(scenario)
    faster = _replay(scenario, 4.0)
    realtime = _replay(scenario, 1.0)

    assert (
        [lap for lap, _ in baseline] == [lap for lap, _ in faster] == [lap for lap, _ in realtime]
    )
    for index, (lap, evaluation) in enumerate(baseline):
        for other in (faster[index][1], realtime[index][1]):
            assert _signature(other) == _signature(evaluation), (
                f"vuelta {lap}: la recomendación cambia con la escala del reloj"
            )


def _smallest_race_scenario() -> _Scenario:
    scenarios = tuple(scenario for scenario in _race_scenarios() if scenario.path.is_file())
    if not scenarios:
        pytest.skip("no hay capturas de carrera disponibles")
    return min(scenarios, key=lambda item: item.path.stat().st_size)
