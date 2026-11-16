"""Pruebas del ciclo de vida de las condiciones (E2)."""

from __future__ import annotations

from f1_strategist.events.tracker import ConditionTracker
from f1_strategist.events.types import (
    ConditionPolicy,
    EventPhase,
    EventPriority,
    Observation,
)
from f1_strategist.metrics.types import Quality


def _policy(**changes: object) -> ConditionPolicy:
    values: dict[str, object] = {
        "rule": "fuel_margin_low",
        "category": "resources",
        "priority": EventPriority.STRATEGIC,
        "message_key": "event.fuel_margin_low",
    }
    values.update(changes)
    return ConditionPolicy(**values)  # type: ignore[arg-type]


def _observation(
    active: bool | None,
    *,
    key: str = "car",
    value: float | None = 1.0,
    material_key: str | None = None,
    quality: Quality | None = None,
) -> Observation:
    return Observation(
        key=key,
        active=active,
        value=value,
        material_key=material_key,
        quality=quality,
        facts=(("margin_laps", value),),
    )


def _phases(transitions: tuple[object, ...]) -> tuple[EventPhase, ...]:
    return tuple(transition.signal.phase for transition in transitions)  # type: ignore[attr-defined]


def test_open_persistence_requires_time_and_consecutive_observations() -> None:
    tracker = ConditionTracker(_policy(persistence_s=2.0, persistence_observations=2))

    assert tracker.update(_observation(True), now=1.0, lap=1) == ()
    opened = tracker.update(_observation(True), now=3.0, lap=1)

    assert _phases(opened) == (EventPhase.OPENED,)
    assert opened[0].signal.event_id == "fuel_margin_low:car:1"


def test_false_observation_breaks_pending_persistence() -> None:
    tracker = ConditionTracker(_policy(persistence_observations=2))

    assert tracker.update(_observation(True), now=1.0, lap=1) == ()
    assert tracker.update(_observation(False), now=2.0, lap=1) == ()
    assert tracker.update(_observation(True), now=3.0, lap=1) == ()
    opened = tracker.update(_observation(True), now=4.0, lap=1)

    assert _phases(opened) == (EventPhase.OPENED,)


def test_none_and_insufficient_observations_do_not_change_lifecycle() -> None:
    tracker = ConditionTracker(_policy())

    assert tracker.update(_observation(None), now=1.0, lap=1) == ()
    assert tracker.update(_observation(True, quality=Quality.INSUFFICIENT), now=2.0, lap=1) == ()
    assert _phases(tracker.update(_observation(True), now=3.0, lap=1)) == (EventPhase.OPENED,)


def test_hysteresis_prevents_reopening_while_value_is_between_thresholds() -> None:
    tracker = ConditionTracker(
        _policy(
            enter=2.0,
            exit=3.0,
            direction="below",
            persistence_observations=1,
            announce_close=True,
        )
    )

    opened = tracker.update(_observation(True, value=1.9), now=1.0, lap=1)
    assert _phases(opened) == (EventPhase.OPENED,)
    assert tracker.update(_observation(False, value=2.5), now=2.0, lap=1) == ()
    assert tracker.update(_observation(True, value=2.1), now=3.0, lap=1) == ()
    closed = tracker.update(_observation(False, value=3.1), now=4.0, lap=1)

    assert _phases(closed) == (EventPhase.CLOSED,)


def test_close_persistence_must_be_satisfied() -> None:
    tracker = ConditionTracker(_policy(clear_persistence_s=2.0, announce_close=True))

    tracker.update(_observation(True), now=1.0, lap=1)
    assert tracker.update(_observation(False), now=2.0, lap=1) == ()
    assert tracker.update(_observation(True), now=3.0, lap=1) == ()
    assert tracker.update(_observation(False), now=4.0, lap=1) == ()
    closed = tracker.update(_observation(False), now=6.0, lap=1)

    assert _phases(closed) == (EventPhase.CLOSED,)


def test_material_key_and_value_delta_emit_updates() -> None:
    tracker = ConditionTracker(_policy(update_min_delta=2.0))

    tracker.update(_observation(True, value=10.0, material_key="plan-a"), now=1.0, lap=1)
    assert (
        tracker.update(_observation(True, value=11.0, material_key="plan-a"), now=2.0, lap=1) == ()
    )
    material_update = tracker.update(
        _observation(True, value=11.0, material_key="plan-b"), now=3.0, lap=1
    )
    value_update = tracker.update(
        _observation(True, value=13.0, material_key="plan-b"), now=4.0, lap=1
    )

    assert _phases(material_update) == (EventPhase.UPDATED,)
    assert _phases(value_update) == (EventPhase.UPDATED,)


def test_one_shot_closes_once_and_waits_for_inactive_before_rearming() -> None:
    tracker = ConditionTracker(_policy(one_shot=True, announce_close=True))

    opened = tracker.update(_observation(True), now=1.0, lap=1)
    closed = tracker.update(_observation(True), now=2.0, lap=1)

    assert _phases(opened) == (EventPhase.OPENED,)
    assert _phases(closed) == (EventPhase.CLOSED,)
    assert tracker.update(_observation(True), now=3.0, lap=1) == ()
    assert tracker.update(_observation(False), now=4.0, lap=1) == ()
    reopened = tracker.update(_observation(True), now=5.0, lap=1)
    assert reopened[0].signal.event_id == "fuel_margin_low:car:2"


def test_reset_for_session_or_flashback_closes_silently_and_restarts_state() -> None:
    tracker = ConditionTracker(_policy())

    tracker.update(_observation(True), now=1.0, lap=7)
    assert tracker.reset("session_changed", now=2.0) == ()
    tracker.update(_observation(True), now=3.0, lap=8)
    assert tracker.reset("flashback", now=4.0) == ()
    opened = tracker.update(_observation(True), now=5.0, lap=3)

    assert opened[0].signal.event_id == "fuel_margin_low:car:3"


def test_multiple_keys_have_independent_condition_ids_and_lifecycles() -> None:
    tracker = ConditionTracker(_policy(announce_close=True))

    first = tracker.update(_observation(True, key="car-1"), now=1.0, lap=1)
    second = tracker.update(_observation(True, key="car-2"), now=1.0, lap=1)
    closed = tracker.update(_observation(False, key="car-1"), now=2.0, lap=1)

    assert first[0].signal.event_id == "fuel_margin_low:car-1:1"
    assert second[0].signal.event_id == "fuel_margin_low:car-2:1"
    assert _phases(closed) == (EventPhase.CLOSED,)
