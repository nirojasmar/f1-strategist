"""Compuerta determinista de avisos.

Recibe señales del detector y decide, en el orden documentado de la fase 5,
si se emiten, actualizan, difieren o suprimen. Toda decisión lleva un motivo
con clave estable y el estado interno es una función pura del tiempo que llega
como parámetro.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from ..config import GateConfig
from .types import (
    AgentAlert,
    EventPhase,
    EventPriority,
    EventSignal,
    EventsSnapshot,
    Fact,
    GateDecision,
    GateOutcome,
)

MAX_DEFERRED = 32
MAX_ALERTS = 100


class GateReason(StrEnum):
    """Motivos estables de emisión, actualización y supresión."""

    EMITTED = "emitted"
    UPDATED = "updated"
    CLOSED_ANNOUNCED = "closed_announced"
    MUTED = "muted"
    BELOW_INTERRUPTION_LEVEL = "below_interruption_level"
    CANCELLED_CLOSED = "cancelled_closed"
    CLOSED_NOT_ANNOUNCED = "closed_not_announced"
    DUPLICATE = "duplicate"
    QUIET_PERIOD = "quiet_period"
    DEFERRED = "deferred"
    COOLDOWN_GLOBAL = "cooldown_global"
    COOLDOWN_CATEGORY = "cooldown_category"
    NO_MATERIAL_CHANGE = "no_material_change"
    UPDATE_RATE_LIMITED = "update_rate_limited"
    EQUIVALENT_RECENT = "equivalent_recent"
    LOWER_PRIORITY_ACTIVE = "lower_priority_active"
    DEFERRED_EXPIRED = "deferred_expired"
    DEFERRED_QUEUE_FULL = "deferred_queue_full"


_PRIORITY_RANK: dict[EventPriority, int] = {
    EventPriority.INFORMATIONAL: 0,
    EventPriority.STRATEGIC: 1,
    EventPriority.CRITICAL: 2,
}
_LEVEL_RANK: dict[str, int] = {"all": 0, "strategic": 1, "critical": 2}


@dataclass(slots=True)
class _ActiveEvent:
    signal: EventSignal
    alert: AgentAlert
    last_alert_at: float


@dataclass(slots=True)
class _DeferredSignal:
    signal: EventSignal
    deferred_at: float


class EventGate:
    """Ordena, limita y explica la salida de avisos del detector."""

    def __init__(self, cfg: GateConfig) -> None:
        self._cfg = cfg
        self._muted = cfg.muted
        self._alerts: list[AgentAlert] = []
        self._active: dict[str, _ActiveEvent] = {}
        self._deferred: dict[str, _DeferredSignal] = {}
        self._pending_updates: dict[str, _DeferredSignal] = {}
        self._equivalents: dict[tuple[str, str], tuple[float, tuple[Fact, ...]]] = {}
        self._last_noncritical_at: float | None = None
        self._last_major_at: float | None = None
        self._last_category_at: dict[tuple[EventPriority, str], float] = {}
        self._pilot_interaction_at: float | None = None
        self._counters: dict[str, int] = {}
        self._sequence = 0

    def process(
        self,
        signals: Sequence[EventSignal],
        *,
        now: float,
        lap: int,
        announce_close_event_ids: frozenset[str] | None = None,
    ) -> tuple[GateDecision, ...]:
        """Procesa las señales y devuelve una decisión por cada una."""
        _validate_time_and_lap(now, lap)
        ordered = sorted(
            enumerate(signals),
            key=lambda item: (-_PRIORITY_RANK[item[1].priority], item[0]),
        )
        decisions: list[GateDecision | None] = [None] * len(signals)
        emitted_this_call = False
        for index, signal in ordered:
            decision, emitted = self._process_signal(
                signal,
                now=now,
                lap=lap,
                emitted_this_call=emitted_this_call,
                announce_close=(
                    announce_close_event_ids is None or signal.event_id in announce_close_event_ids
                ),
            )
            decisions[index] = decision
            emitted_this_call = emitted_this_call or emitted

        result = [decision for decision in decisions if decision is not None]
        if not self._muted:
            pending_decisions, emitted = self._review_pending_updates(
                now=now,
                lap=lap,
                emitted_this_call=emitted_this_call,
            )
            result.extend(pending_decisions)
            result.extend(
                self._review_deferred(
                    now=now,
                    lap=lap,
                    emitted_this_call=emitted_this_call or emitted,
                )
            )
        return tuple(result)

    def set_muted(self, muted: bool) -> None:
        """Activa o desactiva el silencio global sin perder el estado."""
        if not isinstance(muted, bool):
            raise ValueError("muted debe ser booleano")
        self._muted = muted

    def note_pilot_interaction(self, now: float) -> None:
        """Registra una interacción del piloto para el silencio contextual."""
        _validate_time(now)
        self._pilot_interaction_at = now

    def snapshot(self) -> EventsSnapshot:
        """Devuelve una vista inmutable del estado de la compuerta."""
        return EventsSnapshot(
            alerts=tuple(self._alerts),
            active_events=tuple(item.signal for item in self._active.values()),
            counters=dict(self._counters),
            muted=self._muted,
            error=None,
        )

    def reset(self) -> None:
        """Descarta el estado de la sesión y conserva el silencio elegido."""
        self._alerts.clear()
        self._active.clear()
        self._deferred.clear()
        self._pending_updates.clear()
        self._equivalents.clear()
        self._last_noncritical_at = None
        self._last_major_at = None
        self._last_category_at.clear()
        self._pilot_interaction_at = None
        self._counters.clear()
        self._sequence = 0

    def _process_signal(
        self,
        signal: EventSignal,
        *,
        now: float,
        lap: int,
        emitted_this_call: bool,
        announce_close: bool,
    ) -> tuple[GateDecision, bool]:
        if self._muted:
            if signal.phase is EventPhase.CLOSED:
                self._cleanup_closed(signal.event_id)
            return self._decide(signal, GateOutcome.SUPPRESS, GateReason.MUTED, now), False
        if signal.phase is EventPhase.CLOSED:
            if not self._level_allows(signal.priority):
                self._cleanup_closed(signal.event_id)
                return (
                    self._decide(
                        signal,
                        GateOutcome.SUPPRESS,
                        GateReason.BELOW_INTERRUPTION_LEVEL,
                        now,
                    ),
                    False,
                )
            return self._process_closed(
                signal,
                now=now,
                lap=lap,
                announce=announce_close,
            )
        if not self._level_allows(signal.priority):
            return (
                self._decide(
                    signal,
                    GateOutcome.SUPPRESS,
                    GateReason.BELOW_INTERRUPTION_LEVEL,
                    now,
                ),
                False,
            )
        if signal.event_id in self._deferred:
            self._deferred[signal.event_id].signal = replace(
                signal,
                phase=EventPhase.OPENED,
            )
            return self._decide(signal, GateOutcome.DEFER, GateReason.DEFERRED, now), False
        if signal.phase is EventPhase.UPDATED:
            return self._process_updated(
                signal,
                now=now,
                lap=lap,
                emitted_this_call=emitted_this_call,
            )
        return self._process_opened(
            signal,
            now=now,
            lap=lap,
            emitted_this_call=emitted_this_call,
        )

    def _process_opened(
        self,
        signal: EventSignal,
        *,
        now: float,
        lap: int,
        emitted_this_call: bool,
    ) -> tuple[GateDecision, bool]:
        if signal.event_id in self._active:
            return self._decide(signal, GateOutcome.SUPPRESS, GateReason.DUPLICATE, now), False
        if self._in_quiet_period(signal, now=now, lap=lap):
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.QUIET_PERIOD, now),
                False,
            )
        if signal.priority is not EventPriority.CRITICAL:
            blocked = self._cooldown_reason(signal, now)
            if blocked is not None:
                if signal.priority is EventPriority.STRATEGIC:
                    return self._defer(signal, now=now, reason=GateReason.DEFERRED)
                return self._decide(signal, GateOutcome.SUPPRESS, blocked, now), False
        if self._equivalent_recent(signal, now):
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.EQUIVALENT_RECENT, now),
                False,
            )
        if emitted_this_call and signal.priority is not EventPriority.CRITICAL:
            return self._defer(
                signal,
                now=now,
                reason=GateReason.LOWER_PRIORITY_ACTIVE,
            )
        return self._emit_opened(signal, now=now, lap=lap)

    def _process_updated(
        self,
        signal: EventSignal,
        *,
        now: float,
        lap: int,
        emitted_this_call: bool,
    ) -> tuple[GateDecision, bool]:
        if self._in_quiet_period(signal, now=now, lap=lap):
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.QUIET_PERIOD, now),
                False,
            )
        active = self._active.get(signal.event_id)
        if active is None:
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.NO_MATERIAL_CHANGE, now),
                False,
            )
        if (
            signal.material_key == active.signal.material_key
            and signal.facts == active.signal.facts
        ):
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.NO_MATERIAL_CHANGE, now),
                False,
            )
        if emitted_this_call and signal.priority is not EventPriority.CRITICAL:
            self._pending_updates[signal.event_id] = _DeferredSignal(signal=signal, deferred_at=now)
            return self._decide(
                signal, GateOutcome.DEFER, GateReason.LOWER_PRIORITY_ACTIVE, now
            ), False
        if now - active.last_alert_at < self._cfg.update_min_interval_s:
            self._pending_updates[signal.event_id] = _DeferredSignal(signal=signal, deferred_at=now)
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.UPDATE_RATE_LIMITED, now),
                False,
            )
        if self._equivalent_recent(signal, now):
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.EQUIVALENT_RECENT, now),
                False,
            )
        self._pending_updates.pop(signal.event_id, None)
        return self._emit_updated(active, signal, now=now, lap=lap)

    def _process_closed(
        self,
        signal: EventSignal,
        *,
        now: float,
        lap: int,
        announce: bool,
    ) -> tuple[GateDecision, bool]:
        deferred = self._deferred.pop(signal.event_id, None)
        self._pending_updates.pop(signal.event_id, None)
        if deferred is not None:
            return (
                self._decide(signal, GateOutcome.CANCEL, GateReason.CANCELLED_CLOSED, now),
                False,
            )
        active = self._active.pop(signal.event_id, None)
        if active is not None and not announce:
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.CLOSED_NOT_ANNOUNCED, now),
                False,
            )
        if active is None:
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.CLOSED_NOT_ANNOUNCED, now),
                False,
            )
        alert = self._make_alert(signal, now=now, lap=lap, supersedes=None)
        self._append_alert(alert)
        self._remember_emission(signal, now, equivalence=False)
        return (
            self._decide(
                signal,
                GateOutcome.EMIT,
                GateReason.CLOSED_ANNOUNCED,
                now,
                alert=alert,
            ),
            True,
        )

    def _cleanup_closed(self, event_id: str) -> None:
        self._active.pop(event_id, None)
        self._deferred.pop(event_id, None)
        self._pending_updates.pop(event_id, None)

    def _emit_opened(
        self,
        signal: EventSignal,
        *,
        now: float,
        lap: int,
    ) -> tuple[GateDecision, bool]:
        alert = self._make_alert(signal, now=now, lap=lap, supersedes=None)
        self._append_alert(alert)
        self._active[signal.event_id] = _ActiveEvent(
            signal=signal,
            alert=alert,
            last_alert_at=now,
        )
        self._remember_emission(signal, now, equivalence=True)
        return (
            self._decide(signal, GateOutcome.EMIT, GateReason.EMITTED, now, alert=alert),
            True,
        )

    def _emit_updated(
        self,
        active: _ActiveEvent,
        signal: EventSignal,
        *,
        now: float,
        lap: int,
    ) -> tuple[GateDecision, bool]:
        alert = self._make_alert(
            signal,
            now=now,
            lap=lap,
            supersedes=active.alert.alert_id,
        )
        self._append_alert(alert)
        active.signal = signal
        active.alert = alert
        active.last_alert_at = now
        self._remember_emission(signal, now, equivalence=True)
        return (
            self._decide(signal, GateOutcome.UPDATE, GateReason.UPDATED, now, alert=alert),
            True,
        )

    def _defer(
        self,
        signal: EventSignal,
        *,
        now: float,
        reason: GateReason,
    ) -> tuple[GateDecision, bool]:
        if len(self._deferred) >= MAX_DEFERRED:
            return (
                self._decide(signal, GateOutcome.SUPPRESS, GateReason.DEFERRED_QUEUE_FULL, now),
                False,
            )
        self._deferred[signal.event_id] = _DeferredSignal(
            signal=replace(signal, phase=EventPhase.OPENED),
            deferred_at=now,
        )
        return self._decide(signal, GateOutcome.DEFER, reason, now), False

    def _review_deferred(
        self,
        *,
        now: float,
        lap: int,
        emitted_this_call: bool,
    ) -> list[GateDecision]:
        decisions: list[GateDecision] = []
        for event_id, item in list(self._deferred.items()):
            if now - item.deferred_at >= self._cfg.defer_max_s:
                del self._deferred[event_id]
                decisions.append(
                    self._decide(
                        item.signal,
                        GateOutcome.SUPPRESS,
                        GateReason.DEFERRED_EXPIRED,
                        now,
                    )
                )
        if emitted_this_call:
            return decisions

        candidates = sorted(
            self._deferred.values(),
            key=lambda item: (
                -_PRIORITY_RANK[item.signal.priority],
                item.deferred_at,
                item.signal.event_id,
            ),
        )
        for item in candidates:
            if not self._level_allows(item.signal.priority):
                continue
            if self._in_quiet_period(item.signal, now=now, lap=lap):
                continue
            if (
                item.signal.priority is not EventPriority.CRITICAL
                and self._cooldown_reason(item.signal, now) is not None
            ):
                continue
            if self._equivalent_recent(item.signal, now):
                del self._deferred[item.signal.event_id]
                decisions.append(
                    self._decide(
                        item.signal,
                        GateOutcome.SUPPRESS,
                        GateReason.EQUIVALENT_RECENT,
                        now,
                    )
                )
                continue
            del self._deferred[item.signal.event_id]
            alert = self._make_alert(item.signal, now=now, lap=lap, supersedes=None)
            self._append_alert(alert)
            self._active[item.signal.event_id] = _ActiveEvent(
                signal=item.signal,
                alert=alert,
                last_alert_at=now,
            )
            self._remember_emission(item.signal, now, equivalence=True)
            decisions.append(
                self._decide(
                    item.signal,
                    GateOutcome.EMIT,
                    GateReason.EMITTED,
                    now,
                    alert=alert,
                )
            )
            break
        return decisions

    def _review_pending_updates(
        self,
        *,
        now: float,
        lap: int,
        emitted_this_call: bool,
    ) -> tuple[list[GateDecision], bool]:
        decisions: list[GateDecision] = []
        if emitted_this_call:
            return decisions, False
        candidates = sorted(
            self._pending_updates.items(),
            key=lambda item: (
                -_PRIORITY_RANK[item[1].signal.priority],
                item[1].deferred_at,
                item[0],
            ),
        )
        for event_id, item in candidates:
            active = self._active.get(event_id)
            if active is None:
                del self._pending_updates[event_id]
                continue
            if now - active.last_alert_at < self._cfg.update_min_interval_s:
                continue
            del self._pending_updates[event_id]
            decision, emitted = self._process_updated(
                item.signal,
                now=now,
                lap=lap,
                emitted_this_call=False,
            )
            decisions.append(decision)
            if emitted:
                return decisions, True
        return decisions, False

    def _make_alert(
        self,
        signal: EventSignal,
        *,
        now: float,
        lap: int,
        supersedes: str | None,
    ) -> AgentAlert:
        self._sequence += 1
        return AgentAlert(
            alert_id=f"{signal.event_id}:{self._sequence}",
            event_id=signal.event_id,
            phase=signal.phase,
            priority=signal.priority,
            category=signal.category,
            message_key=signal.message_key,
            facts=signal.facts,
            lap=lap,
            emitted_at=now,
            interrupt=signal.priority is EventPriority.CRITICAL,
            supersedes=supersedes,
        )

    def _decide(
        self,
        signal: EventSignal,
        outcome: GateOutcome,
        reason: GateReason,
        now: float,
        *,
        alert: AgentAlert | None = None,
    ) -> GateDecision:
        self._record(outcome, reason)
        return GateDecision(
            event_id=signal.event_id,
            phase=signal.phase,
            outcome=outcome,
            reason=reason.value,
            alert=alert,
            observed_at=now,
        )

    def _record(self, outcome: GateOutcome, reason: GateReason) -> None:
        self._counters[outcome.value] = self._counters.get(outcome.value, 0) + 1
        self._counters[reason.value] = self._counters.get(reason.value, 0) + 1

    def _append_alert(self, alert: AgentAlert) -> None:
        self._alerts.append(alert)
        if len(self._alerts) > MAX_ALERTS:
            del self._alerts[: len(self._alerts) - MAX_ALERTS]

    def _remember_emission(
        self,
        signal: EventSignal,
        now: float,
        *,
        equivalence: bool,
    ) -> None:
        if signal.priority is not EventPriority.CRITICAL:
            self._last_noncritical_at = now
            self._last_category_at[(signal.priority, signal.category)] = now
        if _PRIORITY_RANK[signal.priority] >= _PRIORITY_RANK[EventPriority.STRATEGIC]:
            self._last_major_at = now
        if equivalence and signal.material_key is not None:
            self._equivalents[(signal.rule, signal.material_key)] = (now, signal.facts)

    def _level_allows(self, priority: EventPriority) -> bool:
        return _PRIORITY_RANK[priority] >= _LEVEL_RANK[self._cfg.interruption_level]

    def _in_quiet_period(self, signal: EventSignal, *, now: float, lap: int) -> bool:
        if signal.priority is not EventPriority.INFORMATIONAL:
            return False
        if lap <= self._cfg.quiet_first_laps:
            return True
        if (
            self._pilot_interaction_at is not None
            and now - self._pilot_interaction_at < self._cfg.pilot_quiet_s
        ):
            return True
        return (
            self._last_major_at is not None
            and now - self._last_major_at < self._cfg.informational_space_s
        )

    def _cooldown_reason(
        self,
        signal: EventSignal,
        now: float,
    ) -> GateReason | None:
        if (
            self._last_noncritical_at is not None
            and now - self._last_noncritical_at < self._cfg.global_min_gap_s
        ):
            return GateReason.COOLDOWN_GLOBAL
        last = self._last_category_at.get((signal.priority, signal.category))
        cooldown = (
            self._cfg.cooldown_s.informational
            if signal.priority is EventPriority.INFORMATIONAL
            else self._cfg.cooldown_s.strategic
        )
        if last is not None and now - last < cooldown:
            return GateReason.COOLDOWN_CATEGORY
        return None

    def _equivalent_recent(self, signal: EventSignal, now: float) -> bool:
        if signal.material_key is None:
            return False
        last = self._equivalents.get((signal.rule, signal.material_key))
        return (
            last is not None
            and now - last[0] < self._cfg.equivalence_window_s
            and last[1] == signal.facts
        )


def _validate_time_and_lap(now: float, lap: int) -> None:
    _validate_time(now)
    if type(lap) is not int or lap < 0:
        raise ValueError("lap debe ser un entero no negativo")


def _validate_time(now: float) -> None:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now debe ser un número finito")
    if now < 0:
        raise ValueError("now no puede ser negativo")
