"""Reglas de estrategia y ventana de parada (E4)."""

from __future__ import annotations

from ..config import EventsConfig
from ..metrics.types import Quality
from ..strategy.types import StrategyAction, StrategyEvaluation, StrategyRecommendation
from .types import ConditionPolicy, EventInputs, EventPriority, Fact, Observation

_STRATEGY_KEY = "pit_recommendation"
_PIT_WINDOW_KEY = "pit_window"

_USABLE_QUALITIES = frozenset({Quality.OK, Quality.LOW})
_UNKNOWN_QUALITIES = frozenset({Quality.STALE, Quality.INSUFFICIENT, Quality.UNAVAILABLE})
_PIT_ACTIONS = frozenset({StrategyAction.PIT_NOW, StrategyAction.PIT_IN})


class StrategyPitRecommendedRule:
    """Anuncia una parada con ventaja material sostenida."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()
        self._last_signature: tuple[str | None, int, float] | None = None

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="strategy_pit_recommended",
            category="strategy",
            priority=EventPriority.STRATEGIC,
            message_key="event.strategy_pit_recommended",
            persistence_observations=2,
            update_min_delta=self._config.strategy.update_delta_s,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        evaluation = inputs.strategy.evaluation
        signature = (
            (evaluation.session_uid, evaluation.revision, inputs.strategy.updated_at_monotonic)
            if evaluation is not None
            else (None, -1, inputs.strategy.updated_at_monotonic)
        )
        if signature == self._last_signature:
            return ()
        self._last_signature = signature

        if evaluation is None:
            return (self._observation(active=None),)
        if evaluation.data_quality in _UNKNOWN_QUALITIES:
            return (self._observation(active=None, evaluation=evaluation),)
        return (self._observation(active=self._recommends(evaluation), evaluation=evaluation),)

    def _recommends(self, evaluation: StrategyEvaluation) -> bool:
        recommendation = evaluation.recommendation
        if recommendation.action not in _PIT_ACTIONS:
            return False
        if evaluation.data_quality not in _USABLE_QUALITIES:
            return False
        confidence = recommendation.confidence
        return confidence is not None and confidence >= self._config.strategy.min_confidence

    def _observation(
        self,
        *,
        active: bool | None,
        evaluation: StrategyEvaluation | None = None,
    ) -> Observation:
        recommendation = evaluation.recommendation if evaluation is not None else None
        return Observation(
            key=_STRATEGY_KEY,
            active=active,
            value=_gain(recommendation),
            facts=self._facts(evaluation),
            material_key=_material_key(recommendation),
            quality=evaluation.data_quality if evaluation is not None else None,
        )

    @staticmethod
    def _facts(evaluation: StrategyEvaluation | None) -> tuple[Fact, ...]:
        if evaluation is None:
            return (
                ("action", None),
                ("pit_lap", None),
                ("compound", None),
                ("gain_s", None),
                ("gain_low_s", None),
                ("gain_high_s", None),
                ("confidence", None),
                ("laps_remaining", None),
            )
        recommendation = evaluation.recommendation
        low, high = recommendation.gain_interval_s or (None, None)
        return (
            ("action", recommendation.action.value),
            ("pit_lap", recommendation.pit_lap),
            ("compound", recommendation.compound),
            ("gain_s", recommendation.gain_vs_baseline_s),
            ("gain_low_s", low),
            ("gain_high_s", high),
            ("confidence", recommendation.confidence),
            ("laps_remaining", evaluation.laps_remaining),
        )


class PitWindowOpenRule:
    """Anuncia la ventana de parada que publica el juego."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()
        self._last_signature: tuple[int | None, int | None, int | None, int | None] | None = None

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="pit_window_open",
            category="strategy",
            priority=EventPriority.INFORMATIONAL,
            message_key="event.pit_window_open",
            persistence_observations=1,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        session = inputs.state.session
        ideal = session.pit_window_ideal_lap
        latest = session.pit_window_latest_lap
        current = inputs.state.current_lap if inputs.state.current_lap is not None else inputs.lap
        signature = (session.uid, current, ideal, latest)
        if signature == self._last_signature:
            return ()
        self._last_signature = signature

        if ideal is None or latest is None or current is None or current < 1:
            return (Observation(key=_PIT_WINDOW_KEY, active=None, facts=_window_facts(None, None)),)

        laps_to_ideal = ideal - current
        active = laps_to_ideal <= self._config.pit_window.lead_laps and current <= latest
        return (
            Observation(
                key=_PIT_WINDOW_KEY,
                active=active,
                facts=_window_facts(ideal, latest, laps_to_ideal),
            ),
        )


def _gain(recommendation: StrategyRecommendation | None) -> float | None:
    if recommendation is None:
        return None
    if recommendation.gain_vs_baseline_s is not None:
        return recommendation.gain_vs_baseline_s
    if recommendation.gain_interval_s is not None:
        return recommendation.gain_interval_s[0]
    return None


def _material_key(recommendation: StrategyRecommendation | None) -> str | None:
    if recommendation is None:
        return None
    return f"{recommendation.action.value}|{recommendation.pit_lap}|{recommendation.compound}"


def _window_facts(
    ideal: int | None,
    latest: int | None,
    laps_to_ideal: int | None = None,
) -> tuple[Fact, ...]:
    return (
        ("ideal_lap", ideal),
        ("latest_lap", latest),
        ("laps_to_ideal", laps_to_ideal),
    )
