"""Ciclo de vida determinista de las condiciones observadas por las reglas."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..metrics.types import Quality
from .types import (
    ConditionPolicy,
    EventPhase,
    EventSignal,
    Observation,
)


@dataclass(frozen=True, slots=True)
class ConditionTransition:
    """Señal producida por un cambio en el ciclo de vida de una condición."""

    signal: EventSignal


@dataclass(slots=True)
class _ConditionState:
    event_id: str | None = None
    pending_since: float | None = None
    pending_observations: int = 0
    closing_since: float | None = None
    material_key: str | None = None
    value: float | None = None
    one_shot_spent: bool = False

    @property
    def is_active(self) -> bool:
        return self.event_id is not None


class ConditionTracker:
    """Aplica persistencia, histéresis y actualizaciones a una política."""

    def __init__(self, policy: ConditionPolicy) -> None:
        self._policy = policy
        self._states: dict[str, _ConditionState] = {}
        self._event_numbers: dict[str, int] = {}

    def update(
        self,
        observation: Observation,
        *,
        now: float,
        lap: int,
        include_silent_closures: bool = False,
    ) -> tuple[ConditionTransition, ...]:
        transitions = self._update_lifecycle(observation, now=now, lap=lap)
        if include_silent_closures or self._policy.announce_close:
            return transitions
        return tuple(
            transition
            for transition in transitions
            if transition.signal.phase is not EventPhase.CLOSED
        )

    def _update_lifecycle(
        self,
        observation: Observation,
        *,
        now: float,
        lap: int,
    ) -> tuple[ConditionTransition, ...]:
        _validate_time_and_lap(now, lap)
        if observation.active is None or observation.quality in {
            Quality.INSUFFICIENT,
            Quality.UNAVAILABLE,
        }:
            return ()

        state = self._states.setdefault(observation.key, _ConditionState())
        desired = self._desired_active(observation, state.is_active)
        if desired is None:
            return ()

        if state.is_active:
            if self._policy.one_shot:
                transition = self._transition(EventPhase.CLOSED, state, observation, now, lap)
                self._close(state, observation.key)
                state.one_shot_spent = desired
                return (transition,)
            return self._update_active(state, observation, desired, now, lap)

        if state.one_shot_spent:
            if not desired:
                state.one_shot_spent = False
            return ()

        if not desired:
            self._cancel_pending(state)
            return ()

        return self._update_pending(state, observation, now, lap)

    def reset(self, reason: str, now: float) -> tuple[ConditionTransition, ...]:
        """Descarta las condiciones sin generar señales de cierre."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason no puede estar vacío")
        _validate_time(now)
        self._states.clear()
        if reason == "session_changed":
            self._event_numbers.clear()
        return ()

    def _desired_active(self, observation: Observation, is_active: bool) -> bool | None:
        if self._policy.enter is None:
            return observation.active
        if observation.value is None:
            return None

        threshold = self._policy.exit if is_active else self._policy.enter
        if threshold is None:
            raise RuntimeError("Una política con histéresis requiere ambos umbrales")
        if self._policy.direction is None:
            raise RuntimeError("Una política con histéresis requiere una dirección")
        if self._policy.direction == "below":
            if not is_active and self._policy.enter_inclusive:
                return observation.value <= threshold
            return observation.value < threshold
        if not is_active and self._policy.enter_inclusive:
            return observation.value >= threshold
        return observation.value > threshold

    def _update_pending(
        self,
        state: _ConditionState,
        observation: Observation,
        now: float,
        lap: int,
    ) -> tuple[ConditionTransition, ...]:
        if state.pending_since is None:
            state.pending_since = now
            state.pending_observations = 1
        else:
            state.pending_observations += 1

        elapsed = now - state.pending_since
        time_ready = elapsed >= self._policy.persistence_s
        observations_ready = (
            self._policy.persistence_observations == 0
            or state.pending_observations >= self._policy.persistence_observations
        )
        if not time_ready or not observations_ready:
            return ()

        event_number = self._event_numbers.get(observation.key, 0) + 1
        self._event_numbers[observation.key] = event_number
        state.event_id = f"{self._policy.rule}:{observation.key}:{event_number}"
        state.pending_since = None
        state.pending_observations = 0
        state.closing_since = None
        state.material_key = observation.material_key
        state.value = observation.value
        return (self._transition(EventPhase.OPENED, state, observation, now, lap),)

    def _update_active(
        self,
        state: _ConditionState,
        observation: Observation,
        desired: bool,
        now: float,
        lap: int,
    ) -> tuple[ConditionTransition, ...]:
        if not desired:
            if state.closing_since is None:
                state.closing_since = now
            if now - state.closing_since < self._policy.clear_persistence_s:
                return ()
            transition = self._transition(EventPhase.CLOSED, state, observation, now, lap)
            self._close(state, observation.key)
            return (transition,)

        state.closing_since = None
        material_changed = observation.material_key != state.material_key
        value_changed = (
            observation.value is not None
            and state.value is not None
            and observation.value != state.value
            and self._policy.update_min_delta is not None
            and abs(observation.value - state.value) >= self._policy.update_min_delta
        )
        if not material_changed and not value_changed:
            return ()

        state.material_key = observation.material_key
        state.value = observation.value
        return (self._transition(EventPhase.UPDATED, state, observation, now, lap),)

    def _transition(
        self,
        phase: EventPhase,
        state: _ConditionState,
        observation: Observation,
        now: float,
        lap: int,
    ) -> ConditionTransition:
        event_id = state.event_id
        if event_id is None:
            raise RuntimeError("Una transición requiere una condición activa")
        signal = EventSignal(
            rule=self._policy.rule,
            event_id=event_id,
            phase=phase,
            priority=observation.priority or self._policy.priority,
            category=self._policy.category,
            message_key=self._policy.message_key,
            facts=observation.facts,
            material_key=observation.material_key,
            lap=lap,
            observed_at=now,
        )
        return ConditionTransition(signal)

    def _close(self, state: _ConditionState, key: str) -> None:
        state.event_id = None
        state.pending_since = None
        state.pending_observations = 0
        state.closing_since = None
        state.material_key = None
        state.value = None
        if not self._policy.one_shot:
            state.one_shot_spent = False
        self._states[key] = state

    @staticmethod
    def _cancel_pending(state: _ConditionState) -> None:
        state.pending_since = None
        state.pending_observations = 0


def _validate_time_and_lap(now: float, lap: int) -> None:
    _validate_time(now)
    if type(lap) is not int or lap < 0:
        raise ValueError("lap debe ser un entero no negativo")


def _validate_time(now: float) -> None:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now debe ser un número finito")
    if now < 0:
        raise ValueError("now no puede ser negativo")
