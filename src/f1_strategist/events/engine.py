"""Motor de eventos: reglas, trackers y compuerta dentro del pipeline.

El motor no decide por sí mismo: ejecuta las reglas registradas, deja que
``ConditionTracker`` gestione el ciclo de vida de cada condición y entrega las
señales a ``EventGate``. La cadencia se apoya en las publicaciones de métricas
y estrategia, nunca en la llegada de cada paquete.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from ..config import AppConfig, EventsConfig, GateConfig
from ..metrics.engine import MetricsSnapshot
from ..race_state import RaceEvent, RaceStateSnapshot
from ..strategy.types import StrategySnapshot
from .gate import EventGate
from .registry import EVENT_RULES
from .tracker import ConditionTracker
from .types import (
    EventInputs,
    EventPhase,
    EventRule,
    EventSignal,
    EventsSnapshot,
    GateDecision,
    Observation,
)

_DEFAULT_EVALUATION_INTERVAL_S = 1.0


@dataclass(frozen=True, slots=True)
class EventEvaluation:
    """Resultado puro de una evaluación del detector."""

    signals: tuple[EventSignal, ...]
    decisions: tuple[GateDecision, ...]
    snapshot: EventsSnapshot


@dataclass(frozen=True, slots=True)
class _CollectedSignals:
    signals: tuple[EventSignal, ...]
    announced_closures: frozenset[str]


class _SignalCollectionError(Exception):
    def __init__(self, cause: Exception, collected: _CollectedSignals) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.collected = collected


def evaluate_once(
    inputs: EventInputs,
    gate_config: GateConfig | None = None,
    *,
    rules: Sequence[EventRule] | None = None,
) -> EventEvaluation:
    """Evalúa una instantánea con estado nuevo, sin reloj ni efectos externos.

    Es la función que usan las pruebas y las consultas puntuales. No conserva
    persistencia entre llamadas; el motor usa sus propios trackers persistentes.
    """
    gate_cfg = gate_config or GateConfig()
    if not inputs.config.enabled:
        return EventEvaluation((), (), EventsSnapshot(muted=gate_cfg.muted))

    active_rules = tuple(rules) if rules is not None else _configured_rules(inputs.config)
    trackers = tuple(ConditionTracker(rule.policy) for rule in active_rules)
    collected = _collect_signals(active_rules, trackers, inputs)
    gate = EventGate(gate_cfg)
    decisions = (
        gate.process(
            collected.signals,
            now=inputs.now,
            lap=inputs.lap,
            announce_close_event_ids=collected.announced_closures,
        )
        if gate_cfg.enabled
        else ()
    )
    snapshot = gate.snapshot() if gate_cfg.enabled else EventsSnapshot(muted=gate_cfg.muted)
    return EventEvaluation(collected.signals, tuple(decisions), snapshot)


class EventEngine:
    """Orquesta las reglas del detector con cadencia y reinicios de sesión."""

    def __init__(
        self,
        config: AppConfig | EventsConfig,
        *,
        gate: GateConfig | None = None,
        evaluation_min_interval_s: float | None = None,
        rules: Sequence[EventRule] | None = None,
    ) -> None:
        if isinstance(config, AppConfig):
            events_config = config.events
            gate_cfg = gate or config.gate
            interval = (
                config.strategy.evaluation_min_interval_s
                if evaluation_min_interval_s is None
                else evaluation_min_interval_s
            )
        else:
            events_config = config
            gate_cfg = gate or GateConfig()
            interval = (
                _DEFAULT_EVALUATION_INTERVAL_S
                if evaluation_min_interval_s is None
                else evaluation_min_interval_s
            )
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("evaluation_min_interval_s debe ser un número positivo")

        self._config = events_config
        self._gate_config = gate_cfg
        self._interval_s = interval
        self._provided_rules = tuple(rules) if rules is not None else None
        self._rules: tuple[EventRule, ...] = ()
        self._trackers: tuple[ConditionTracker, ...] = ()
        self._gate = EventGate(gate_cfg)
        self._snapshot = EventsSnapshot(muted=gate_cfg.muted)
        self._last_evaluation: EventEvaluation | None = None
        self._error: str | None = None
        self._reset_rules()

        self._last_session_uid: int | None = None
        self._last_lap: int | None = None
        self._last_evaluation_at: float | None = None
        self._evaluated_metrics_token: tuple[float | None, int | None] | None = None
        self._evaluated_strategy_token: tuple[float, int | None, str | None] | None = None
        self._seen_state_events: set[RaceEvent] = set()
        self._pilot_lock = threading.Lock()
        self._pending_pilot_interaction: float | None = None

    @property
    def config(self) -> EventsConfig:
        return self._config

    def note_pilot_interaction(self, now: float) -> None:
        """Registra una interacción sin tocar la compuerta desde otro hilo.

        Solo conserva el instante más reciente; el consumidor lo aplica en
        ``apply`` antes de evaluar, de modo que la compuerta sigue teniendo un
        único escritor.
        """
        _validate_observed_at(now)
        with self._pilot_lock:
            self._pending_pilot_interaction = now

    def apply(
        self,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot,
        strategy: StrategySnapshot,
        observed_at: float,
    ) -> None:
        """Procesa una instantánea si la cadencia lo permite y publica el resultado.

        Nunca propaga un fallo al consumidor: lo deja en ``EventsSnapshot.error``
        y conserva el estado previamente publicado.
        """
        self._last_evaluation = None
        if not self._config.enabled:
            return

        signals: tuple[EventSignal, ...] = ()
        decisions: tuple[GateDecision, ...] = ()
        try:
            _validate_observed_at(observed_at)
            with self._pilot_lock:
                pending_interaction = self._pending_pilot_interaction
                self._pending_pilot_interaction = None
            if pending_interaction is not None:
                self._gate.note_pilot_interaction(pending_interaction)
            lap = state.current_lap or 0
            uid = state.session.uid
            if self._needs_reset(uid, state.current_lap):
                self._reset(state, uid, state.current_lap, observed_at)

            new_events = self._consume_state_events(state.events)
            metrics_token = (
                metrics.updated_at_monotonic,
                metrics.session_uid,
            )
            strategy_token = (
                strategy.updated_at_monotonic,
                strategy.evaluation.revision if strategy.evaluation is not None else None,
                strategy.evaluation.session_uid if strategy.evaluation is not None else None,
            )
            first = self._last_evaluation_at is None
            metrics_changed = metrics_token != self._evaluated_metrics_token
            strategy_changed = strategy_token != self._evaluated_strategy_token
            due = (
                first
                or bool(new_events)
                or (
                    (metrics_changed or strategy_changed)
                    and self._last_evaluation_at is not None
                    and observed_at - self._last_evaluation_at >= self._interval_s
                )
            )

            if due:
                try:
                    inputs = EventInputs(
                        state=state,
                        metrics=metrics,
                        strategy=strategy,
                        config=self._config,
                        now=observed_at,
                        lap=lap,
                        new_state_events=new_events,
                    )
                    collected = _collect_signals(self._rules, self._trackers, inputs)
                    signals = collected.signals
                    decisions = (
                        tuple(
                            self._gate.process(
                                signals,
                                now=observed_at,
                                lap=lap,
                                announce_close_event_ids=collected.announced_closures,
                            )
                        )
                        if self._gate_config.enabled
                        else ()
                    )
                    self._error = None
                except _SignalCollectionError as exc:
                    signals = exc.collected.signals
                    try:
                        decisions = (
                            tuple(
                                self._gate.process(
                                    signals,
                                    now=observed_at,
                                    lap=lap,
                                    announce_close_event_ids=exc.collected.announced_closures,
                                )
                            )
                            if self._gate_config.enabled
                            else ()
                        )
                    except Exception as gate_exc:
                        self._error = f"{type(gate_exc).__name__}: {gate_exc}"
                    else:
                        self._error = f"{type(exc.cause).__name__}: {exc.cause}"
                except Exception as exc:
                    self._error = f"{type(exc).__name__}: {exc}"
                finally:
                    # La cadencia avanza aunque falle: un fallo no debe provocar
                    # una reevaluación por paquete ni perder la captura.
                    self._last_evaluation_at = observed_at
                    self._evaluated_metrics_token = metrics_token
                    self._evaluated_strategy_token = strategy_token
                    self._mark_state_events_seen(state.events)
                    self._publish()
                    if self._error is None or signals or decisions:
                        self._last_evaluation = EventEvaluation(signals, decisions, self._snapshot)

            self._last_lap = state.current_lap if state.current_lap is not None else self._last_lap
            if uid is not None:
                self._last_session_uid = uid
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._publish()

    def snapshot(self) -> EventsSnapshot:
        """Último estado publicado; inmutable y seguro para consumidores."""
        return self._snapshot

    @property
    def last_evaluation(self) -> EventEvaluation | None:
        """Última evaluación completada por esta llamada a :meth:`apply`, si la hubo."""
        return self._last_evaluation

    # ------------------------------------------------------------------
    # Cadencia y reinicios
    # ------------------------------------------------------------------

    def _needs_reset(
        self,
        uid: int | None,
        current_lap: int | None,
    ) -> bool:
        if uid is not None and self._last_session_uid is not None and uid != self._last_session_uid:
            return True
        return (
            current_lap is not None and self._last_lap is not None and current_lap < self._last_lap
        )

    def _reset(
        self,
        state: RaceStateSnapshot,
        uid: int | None,
        current_lap: int | None,
        observed_at: float,
    ) -> None:
        reason = "session_changed" if uid != self._last_session_uid else "flashback"
        for tracker in self._trackers:
            tracker.reset(reason, observed_at)
        self._gate.reset()
        if self._provided_rules is None:
            self._reset_rules()
        self._last_evaluation_at = None
        self._evaluated_metrics_token = None
        self._evaluated_strategy_token = None
        self._last_session_uid = uid
        self._last_lap = current_lap
        self._mark_state_events_seen(state.events)
        self._error = None
        self._snapshot = EventsSnapshot(
            muted=self._gate_config.muted, reset_seq=self._gate.reset_seq
        )

    def _reset_rules(self) -> None:
        if self._provided_rules is not None:
            self._rules = self._provided_rules
        else:
            self._rules = _configured_rules(self._config)
        self._trackers = tuple(ConditionTracker(rule.policy) for rule in self._rules)

    def _consume_state_events(self, events: Sequence[RaceEvent]) -> tuple[RaceEvent, ...]:
        new_events: list[RaceEvent] = []
        batch_seen: set[RaceEvent] = set()
        for event in events:
            if event in self._seen_state_events or event in batch_seen:
                continue
            batch_seen.add(event)
            new_events.append(event)
        return tuple(new_events)

    def _mark_state_events_seen(self, events: Sequence[RaceEvent]) -> None:
        self._seen_state_events = set(events)

    def _publish(self) -> None:
        if not self._gate_config.enabled:
            self._snapshot = EventsSnapshot(
                muted=self._gate_config.muted,
                error=self._error,
                reset_seq=self._gate.reset_seq,
            )
            return
        current = self._gate.snapshot()
        self._snapshot = EventsSnapshot(
            alerts=current.alerts,
            active_events=current.active_events,
            counters=current.counters,
            muted=current.muted,
            error=self._error,
            reset_seq=current.reset_seq,
        )


def _configured_rules(config: EventsConfig) -> tuple[EventRule, ...]:
    """Crea reglas aisladas para que sus cachés no se compartan entre motores."""
    return tuple(cast(type[Any], type(rule))(config) for rule in EVENT_RULES)


def _collect_signals(
    rules: Sequence[EventRule],
    trackers: Sequence[ConditionTracker],
    inputs: EventInputs,
) -> _CollectedSignals:
    signals: list[EventSignal] = []
    announced_closures: set[str] = set()
    for rule, tracker in zip(rules, trackers, strict=True):
        try:
            for observation in rule.observe(inputs):
                _validate_observation(observation)
                for transition in tracker.update(
                    observation,
                    now=inputs.now,
                    lap=inputs.lap,
                    include_silent_closures=True,
                ):
                    signal = transition.signal
                    signals.append(signal)
                    if signal.phase is EventPhase.CLOSED and rule.policy.announce_close:
                        announced_closures.add(signal.event_id)
        except Exception as exc:
            raise _SignalCollectionError(
                exc,
                _CollectedSignals(tuple(signals), frozenset(announced_closures)),
            ) from exc
    return _CollectedSignals(tuple(signals), frozenset(announced_closures))


def _validate_observation(observation: Observation) -> None:
    if not isinstance(observation, Observation):
        raise TypeError("Las reglas deben devolver observaciones de tipo Observation")


def _validate_observed_at(observed_at: float) -> None:
    if not _is_valid_time(observed_at):
        raise ValueError("observed_at debe ser un número finito no negativo")


def _is_valid_time(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


__all__ = ["EventEngine", "EventEvaluation", "evaluate_once"]
