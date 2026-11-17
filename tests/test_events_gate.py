"""Pruebas de la compuerta de avisos (E3)."""

from __future__ import annotations

import pytest

from f1_strategist.config import GateConfig, GateCooldownConfig
from f1_strategist.events.gate import EventGate
from f1_strategist.events.types import (
    EventPhase,
    EventPriority,
    EventSignal,
    GateOutcome,
)


def _cfg(**changes: object) -> GateConfig:
    values: dict[str, object] = {
        "global_min_gap_s": 0.0,
        "cooldown_s": GateCooldownConfig(strategic=0.0, informational=0.0),
        "equivalence_window_s": 0.0,
        "update_min_interval_s": 0.0,
        "defer_max_s": 60.0,
        "quiet_first_laps": 0,
        "informational_space_s": 0.0,
        "pilot_quiet_s": 0.0,
    }
    values.update(changes)
    return GateConfig(**values)  # type: ignore[arg-type]


def _signal(
    *,
    event_id: str = "strategy_pit:car:1",
    phase: EventPhase = EventPhase.OPENED,
    priority: EventPriority = EventPriority.STRATEGIC,
    category: str = "strategy",
    rule: str = "strategy_pit",
    material_key: str | None = None,
    facts: tuple[tuple[str, float | int | str | None], ...] = (),
    lap: int = 2,
    observed_at: float = 0.0,
) -> EventSignal:
    return EventSignal(
        rule=rule,
        event_id=event_id,
        phase=phase,
        priority=priority,
        category=category,
        message_key=f"event.{rule}",
        facts=facts,
        material_key=material_key,
        lap=lap,
        observed_at=observed_at,
    )


def test_empty_input_produces_no_decisions() -> None:
    gate = EventGate(_cfg())

    assert gate.process((), now=10.0, lap=2) == ()
    snapshot = gate.snapshot()
    assert snapshot.alerts == ()
    assert snapshot.counters == {}


def test_critical_ignores_cooldown_and_interrupts() -> None:
    gate = EventGate(
        _cfg(
            global_min_gap_s=999.0,
            cooldown_s=GateCooldownConfig(strategic=999.0, informational=999.0),
        )
    )
    strategic = _signal(event_id="strategy:1", category="strategy")
    critical = _signal(
        event_id="track:1",
        priority=EventPriority.CRITICAL,
        category="track",
        rule="track_incident",
    )

    first = gate.process((strategic,), now=10.0, lap=2)[0]
    second = gate.process((critical,), now=11.0, lap=2)[0]

    assert first.outcome is GateOutcome.EMIT
    assert first.alert is not None
    assert first.alert.interrupt is False
    assert second.outcome is GateOutcome.EMIT
    assert second.alert is not None
    assert second.alert.interrupt is True


def test_strategic_blocked_by_global_gap_is_deferred_then_released() -> None:
    gate = EventGate(_cfg(global_min_gap_s=10.0))
    first = _signal(event_id="strategy:1")
    second = _signal(event_id="strategy:2")

    assert gate.process((first,), now=10.0, lap=2)[0].outcome is GateOutcome.EMIT
    deferred = gate.process((second,), now=12.0, lap=2)[0]
    assert deferred.outcome is GateOutcome.DEFER
    assert deferred.reason == "deferred"
    assert len(gate.snapshot().active_events) == 1

    released = gate.process((), now=25.0, lap=2)
    assert len(released) == 1
    assert released[0].event_id == "strategy:2"
    assert released[0].outcome is GateOutcome.EMIT
    assert len(gate.snapshot().active_events) == 2


def test_deferred_signal_is_cancelled_when_closed() -> None:
    gate = EventGate(_cfg(global_min_gap_s=100.0))
    first = _signal(event_id="strategy:1")
    second = _signal(event_id="strategy:2")

    gate.process((first,), now=0.0, lap=2)
    assert gate.process((second,), now=1.0, lap=2)[0].outcome is GateOutcome.DEFER

    closed = _signal(
        event_id="strategy:2",
        phase=EventPhase.CLOSED,
    )
    decision = gate.process((closed,), now=2.0, lap=2)[0]

    assert decision.outcome is GateOutcome.CANCEL
    assert decision.reason == "cancelled_closed"
    assert gate.process((), now=200.0, lap=2) == ()


def test_deferred_signal_expires() -> None:
    gate = EventGate(_cfg(global_min_gap_s=100.0, defer_max_s=5.0))
    first = _signal(event_id="strategy:1")
    second = _signal(event_id="strategy:2")

    gate.process((first,), now=0.0, lap=2)
    gate.process((second,), now=1.0, lap=2)

    expired = gate.process((), now=6.0, lap=2)

    assert len(expired) == 1
    assert expired[0].event_id == "strategy:2"
    assert expired[0].outcome is GateOutcome.SUPPRESS
    assert expired[0].reason == "deferred_expired"


def test_muted_suppresses_everything_and_reset_preserves_mute() -> None:
    gate = EventGate(_cfg())
    critical = _signal(
        event_id="track:1",
        priority=EventPriority.CRITICAL,
        category="track",
        rule="track_incident",
    )
    gate.set_muted(True)

    decision = gate.process((critical,), now=10.0, lap=2)[0]

    assert decision.outcome is GateOutcome.SUPPRESS
    assert decision.reason == "muted"
    snapshot = gate.snapshot()
    assert snapshot.muted is True
    assert snapshot.counters["suppress"] == 1
    assert snapshot.counters["muted"] == 1

    gate.set_muted(False)
    assert gate.process((critical,), now=12.0, lap=2)[0].outcome is GateOutcome.EMIT

    gate.set_muted(True)
    gate.reset()
    reset_snapshot = gate.snapshot()
    assert reset_snapshot.muted is True
    assert reset_snapshot.alerts == ()
    assert reset_snapshot.counters == {}


def test_muted_closed_signal_cleans_up_active_event_without_announcement() -> None:
    gate = EventGate(_cfg())
    opened = _signal(event_id="strategy:1")
    gate.process((opened,), now=10.0, lap=2)
    gate.set_muted(True)

    decision = gate.process(
        (_signal(event_id="strategy:1", phase=EventPhase.CLOSED),),
        now=11.0,
        lap=2,
    )[0]

    assert decision.outcome is GateOutcome.SUPPRESS
    assert decision.reason == "muted"
    assert gate.snapshot().active_events == ()


def test_silent_close_cancels_deferred_event() -> None:
    gate = EventGate(_cfg(global_min_gap_s=100.0))
    gate.process((_signal(event_id="strategy:1"),), now=0.0, lap=2)
    second = _signal(event_id="strategy:2")
    assert gate.process((second,), now=1.0, lap=2)[0].outcome is GateOutcome.DEFER

    closed = _signal(event_id="strategy:2", phase=EventPhase.CLOSED)
    decision = gate.process(
        (closed,),
        now=2.0,
        lap=2,
        announce_close_event_ids=frozenset(),
    )[0]
    assert decision.outcome is GateOutcome.CANCEL
    assert gate.process((), now=200.0, lap=2) == ()


def test_interruption_level_suppresses_below_threshold() -> None:
    gate = EventGate(_cfg(interruption_level="critical"))
    strategic = _signal(event_id="strategy:1", category="strategy")
    critical = _signal(
        event_id="track:1",
        priority=EventPriority.CRITICAL,
        category="track",
        rule="track_incident",
    )

    suppressed = gate.process((strategic,), now=10.0, lap=2)[0]
    emitted = gate.process((critical,), now=11.0, lap=2)[0]

    assert suppressed.outcome is GateOutcome.SUPPRESS
    assert suppressed.reason == "below_interruption_level"
    assert emitted.outcome is GateOutcome.EMIT

    strategic_gate = EventGate(_cfg(interruption_level="strategic"))
    informational = _signal(
        event_id="weather:1",
        priority=EventPriority.INFORMATIONAL,
        category="weather",
        rule="weather_change",
    )
    assert (
        strategic_gate.process((informational,), now=10.0, lap=2)[0].reason
        == "below_interruption_level"
    )
    assert strategic_gate.process((strategic,), now=11.0, lap=2)[0].outcome is GateOutcome.EMIT


def test_duplicate_open_is_suppressed() -> None:
    gate = EventGate(_cfg())
    signal = _signal(event_id="strategy:1")

    gate.process((signal,), now=10.0, lap=2)
    duplicate = gate.process((signal,), now=11.0, lap=2)[0]

    assert duplicate.outcome is GateOutcome.SUPPRESS
    assert duplicate.reason == "duplicate"


def test_closed_announces_and_suppresses_unknown() -> None:
    gate = EventGate(_cfg())
    opened = _signal(event_id="strategy:1")
    closed = _signal(event_id="strategy:1", phase=EventPhase.CLOSED)
    unknown = _signal(event_id="strategy:missing", phase=EventPhase.CLOSED)

    gate.process((opened,), now=10.0, lap=2)
    announced = gate.process((closed,), now=11.0, lap=2)[0]

    assert announced.outcome is GateOutcome.EMIT
    assert announced.reason == "closed_announced"
    assert announced.alert is not None
    assert announced.alert.phase is EventPhase.CLOSED
    assert gate.snapshot().active_events == ()

    suppressed = gate.process((unknown,), now=12.0, lap=2)[0]
    assert suppressed.outcome is GateOutcome.SUPPRESS
    assert suppressed.reason == "closed_not_announced"


def test_informational_quiet_period() -> None:
    lap_gate = EventGate(_cfg(quiet_first_laps=1))
    informational = _signal(
        event_id="weather:1",
        priority=EventPriority.INFORMATIONAL,
        category="weather",
        rule="weather_change",
    )
    assert lap_gate.process((informational,), now=10.0, lap=1)[0].reason == "quiet_period"
    assert lap_gate.process((informational,), now=11.0, lap=2)[0].outcome is GateOutcome.EMIT

    pilot_gate = EventGate(_cfg(pilot_quiet_s=15.0))
    pilot_gate.note_pilot_interaction(10.0)
    assert pilot_gate.process((informational,), now=12.0, lap=2)[0].reason == "quiet_period"
    assert pilot_gate.process((informational,), now=26.0, lap=2)[0].outcome is GateOutcome.EMIT

    major_gate = EventGate(_cfg(informational_space_s=30.0))
    strategic = _signal(event_id="strategy:1", category="strategy")
    major_gate.process((strategic,), now=10.0, lap=2)
    assert major_gate.process((informational,), now=12.0, lap=2)[0].reason == "quiet_period"
    assert major_gate.process((informational,), now=41.0, lap=2)[0].outcome is GateOutcome.EMIT


def test_cooldown_global_suppresses_informational() -> None:
    gate = EventGate(_cfg(global_min_gap_s=10.0))
    strategic = _signal(event_id="strategy:1", category="strategy")
    informational = _signal(
        event_id="weather:1",
        priority=EventPriority.INFORMATIONAL,
        category="weather",
        rule="weather_change",
    )

    gate.process((strategic,), now=10.0, lap=2)
    blocked = gate.process((informational,), now=12.0, lap=2)[0]
    allowed = gate.process((informational,), now=21.0, lap=2)[0]

    assert blocked.outcome is GateOutcome.SUPPRESS
    assert blocked.reason == "cooldown_global"
    assert allowed.outcome is GateOutcome.EMIT


def test_cooldown_category_suppresses_informational() -> None:
    gate = EventGate(
        _cfg(
            global_min_gap_s=0.0,
            cooldown_s=GateCooldownConfig(strategic=0.0, informational=20.0),
        )
    )
    first = _signal(
        event_id="weather:1",
        priority=EventPriority.INFORMATIONAL,
        category="weather",
        rule="weather_change",
    )
    second = _signal(
        event_id="weather:2",
        priority=EventPriority.INFORMATIONAL,
        category="weather",
        rule="weather_change",
    )

    gate.process((first,), now=10.0, lap=2)
    blocked = gate.process((second,), now=12.0, lap=2)[0]
    allowed = gate.process((second,), now=31.0, lap=2)[0]

    assert blocked.outcome is GateOutcome.SUPPRESS
    assert blocked.reason == "cooldown_category"
    assert allowed.outcome is GateOutcome.EMIT


def test_update_requires_material_change_and_rate_limit() -> None:
    gate = EventGate(_cfg(update_min_interval_s=30.0))
    opened = _signal(event_id="strategy:1", material_key="a")
    unchanged = _signal(
        event_id="strategy:1",
        phase=EventPhase.UPDATED,
        material_key="a",
    )
    changed = _signal(
        event_id="strategy:1",
        phase=EventPhase.UPDATED,
        material_key="b",
    )
    later = _signal(
        event_id="strategy:1",
        phase=EventPhase.UPDATED,
        material_key="c",
    )

    first = gate.process((opened,), now=10.0, lap=2)[0]
    no_change = gate.process((unchanged,), now=11.0, lap=2)[0]
    limited = gate.process((changed,), now=12.0, lap=2)[0]
    updated = gate.process((later,), now=45.0, lap=2)[0]

    assert no_change.outcome is GateOutcome.SUPPRESS
    assert no_change.reason == "no_material_change"
    assert limited.outcome is GateOutcome.SUPPRESS
    assert limited.reason == "update_rate_limited"
    assert updated.outcome is GateOutcome.UPDATE
    assert updated.reason == "updated"
    assert updated.alert is not None
    assert first.alert is not None
    assert updated.alert.supersedes == first.alert.alert_id
    assert gate.snapshot().active_events[0].material_key == "c"


def test_equivalent_recent_is_suppressed() -> None:
    gate = EventGate(_cfg(equivalence_window_s=90.0))
    first = _signal(event_id="strategy:1", material_key="gain:5")
    equivalent = _signal(event_id="strategy:2", material_key="gain:5")
    different = _signal(event_id="strategy:3", material_key="gain:7")

    assert gate.process((first,), now=10.0, lap=2)[0].outcome is GateOutcome.EMIT
    suppressed = gate.process((equivalent,), now=20.0, lap=2)[0]
    allowed = gate.process((different,), now=21.0, lap=2)[0]

    assert suppressed.outcome is GateOutcome.SUPPRESS
    assert suppressed.reason == "equivalent_recent"
    assert allowed.outcome is GateOutcome.EMIT


def test_updated_value_with_stable_material_key_is_accepted() -> None:
    gate = EventGate(_cfg(equivalence_window_s=90.0))
    opened = _signal(
        event_id="strategy:1",
        material_key="same-plan",
        facts=(("gain_s", 5.0),),
    )
    updated = _signal(
        event_id="strategy:1",
        phase=EventPhase.UPDATED,
        material_key="same-plan",
        facts=(("gain_s", 8.0),),
    )

    first = gate.process((opened,), now=10.0, lap=2)[0]
    decision = gate.process((updated,), now=11.0, lap=2)[0]

    assert first.alert is not None
    assert decision.outcome is GateOutcome.UPDATE
    assert decision.alert is not None
    assert decision.alert.supersedes == first.alert.alert_id
    assert gate.snapshot().active_events[0].facts == (("gain_s", 8.0),)


def test_burst_defers_update_and_releases_it_with_its_phase_and_parent() -> None:
    gate = EventGate(_cfg())
    opened = _signal(
        event_id="strategy:1",
        material_key="plan",
        facts=(("gain_s", 1.0),),
    )
    initial = gate.process((opened,), now=1.0, lap=2)[0]
    critical = _signal(
        event_id="track:1",
        priority=EventPriority.CRITICAL,
        category="track",
        rule="track_incident",
    )
    updated = _signal(
        event_id="strategy:1",
        phase=EventPhase.UPDATED,
        material_key="plan",
        facts=(("gain_s", 2.0),),
    )

    decisions = gate.process((critical, updated), now=2.0, lap=2)
    by_id = {decision.event_id: decision for decision in decisions}
    assert by_id["strategy:1"].outcome is GateOutcome.DEFER
    assert by_id["strategy:1"].phase is EventPhase.UPDATED

    released = gate.process((), now=3.0, lap=2)
    assert len(released) == 1
    assert released[0].outcome is GateOutcome.UPDATE
    assert released[0].phase is EventPhase.UPDATED
    assert released[0].alert is not None
    assert initial.alert is not None
    assert released[0].alert.supersedes == initial.alert.alert_id


def test_rate_limited_update_retries_latest_value_and_closes_cleanly() -> None:
    gate = EventGate(_cfg(update_min_interval_s=10.0))
    opened = _signal(
        event_id="strategy:1",
        material_key="stable",
        facts=(("gain_s", 1.0),),
    )
    first = gate.process((opened,), now=0.0, lap=2)[0]
    early = _signal(
        event_id="strategy:1",
        phase=EventPhase.UPDATED,
        material_key="stable",
        facts=(("gain_s", 2.0),),
    )
    latest = _signal(
        event_id="strategy:1",
        phase=EventPhase.UPDATED,
        material_key="stable",
        facts=(("gain_s", 3.0),),
    )
    assert gate.process((early,), now=1.0, lap=2)[0].reason == "update_rate_limited"
    assert gate.process((latest,), now=2.0, lap=2)[0].reason == "update_rate_limited"

    released = gate.process((), now=10.0, lap=2)
    assert len(released) == 1
    assert released[0].outcome is GateOutcome.UPDATE
    assert released[0].alert is not None
    assert released[0].alert.facts == (("gain_s", 3.0),)
    assert first.alert is not None
    assert released[0].alert.supersedes == first.alert.alert_id

    closing = _signal(event_id="strategy:1", phase=EventPhase.CLOSED)
    gate.process((closing,), now=11.0, lap=2)
    assert gate.process((), now=30.0, lap=2) == ()


def test_burst_emits_highest_priority_and_defers_the_rest() -> None:
    gate = EventGate(_cfg())
    informational = _signal(
        event_id="weather:1",
        priority=EventPriority.INFORMATIONAL,
        category="weather",
        rule="weather_change",
    )
    strategic = _signal(event_id="strategy:1", category="strategy")
    critical = _signal(
        event_id="track:1",
        priority=EventPriority.CRITICAL,
        category="track",
        rule="track_incident",
    )

    decisions = gate.process(
        (informational, strategic, critical),
        now=10.0,
        lap=2,
    )
    by_id = {decision.event_id: decision for decision in decisions}

    assert by_id["track:1"].outcome is GateOutcome.EMIT
    assert by_id["strategy:1"].outcome is GateOutcome.DEFER
    assert by_id["strategy:1"].reason == "lower_priority_active"
    assert by_id["weather:1"].outcome is GateOutcome.DEFER
    assert by_id["weather:1"].reason == "lower_priority_active"
    active_ids = {signal.event_id for signal in gate.snapshot().active_events}
    assert active_ids == {"track:1"}


def test_deferred_queue_is_bounded() -> None:
    gate = EventGate(_cfg(global_min_gap_s=1000.0, defer_max_s=1000.0))
    baseline = _signal(event_id="strategy:baseline", category="strategy")
    gate.process((baseline,), now=0.0, lap=2)

    signals = tuple(
        _signal(event_id=f"strategy:{index}", category="strategy") for index in range(33)
    )
    decisions = gate.process(signals, now=1.0, lap=2)

    assert len(decisions) == 33
    assert [decision.outcome for decision in decisions[:32]] == [GateOutcome.DEFER] * 32
    assert decisions[32].outcome is GateOutcome.SUPPRESS
    assert decisions[32].reason == "deferred_queue_full"
    counters = gate.snapshot().counters
    assert counters["defer"] == 32
    assert counters["deferred"] == 32
    assert counters["deferred_queue_full"] == 1


def test_snapshot_exposes_alerts_active_events_and_counters() -> None:
    gate = EventGate(_cfg())
    signal = _signal(event_id="strategy:1")

    gate.process((signal,), now=10.0, lap=2)
    snapshot = gate.snapshot()

    assert len(snapshot.alerts) == 1
    assert snapshot.alerts[0].event_id == "strategy:1"
    assert len(snapshot.active_events) == 1
    assert snapshot.active_events[0].event_id == "strategy:1"
    assert snapshot.counters["emit"] == 1
    assert snapshot.counters["emitted"] == 1
    assert snapshot.error is None


def test_decisions_are_deterministic_for_simulated_time() -> None:
    signals = (
        _signal(event_id="strategy:1", material_key="a"),
        _signal(
            event_id="weather:1",
            priority=EventPriority.INFORMATIONAL,
            category="weather",
            rule="weather_change",
        ),
    )
    first_gate = EventGate(_cfg())
    second_gate = EventGate(_cfg())

    first = first_gate.process(signals, now=10.0, lap=2)
    second = second_gate.process(signals, now=10.0, lap=2)

    assert first == second
    assert first_gate.snapshot().counters == second_gate.snapshot().counters


@pytest.mark.parametrize(
    ("now", "lap"),
    [(-1.0, 2), (float("nan"), 2), (0.0, -1), (0.0, 1.5)],
)
def test_invalid_time_and_lap_are_rejected(now: float, lap: int) -> None:
    gate = EventGate(_cfg())

    with pytest.raises(ValueError):
        gate.process((), now=now, lap=lap)
