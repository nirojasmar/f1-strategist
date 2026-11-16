"""Tipos, configuración y esqueleto del detector de eventos (E1)."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from f1_strategist.cli import main
from f1_strategist.config import (
    AppConfig,
    CaptureConfig,
    ConfigError,
    RecordingConfig,
    TelemetryConfig,
    load_config,
)
from f1_strategist.events.types import (
    EVENTS_SCHEMA_VERSION,
    AgentAlert,
    ConditionPolicy,
    EventPhase,
    EventPriority,
    EventSignal,
    EventsSnapshot,
    GateDecision,
    GateOutcome,
)
from f1_strategist.playback import PlaybackSession
from f1_strategist.session import CaptureSession


def _signal(**changes: object) -> EventSignal:
    values: dict[str, object] = {
        "rule": "strategy_pit_recommended",
        "event_id": "strategy_pit_recommended:pit_now:1",
        "phase": EventPhase.OPENED,
        "priority": EventPriority.STRATEGIC,
        "category": "strategy",
        "message_key": "event.strategy_pit_recommended",
        "facts": (
            ("gain_s", 3.5),
            ("action", "PIT_NOW"),
            ("confidence", 0.7),
        ),
        "material_key": "pit_now:hard:set2",
        "lap": 12,
        "observed_at": 123.456789123,
    }
    values.update(changes)
    return EventSignal(**values)  # type: ignore[arg-type]


def _alert(**changes: object) -> AgentAlert:
    values: dict[str, object] = {
        "alert_id": "alert-1",
        "event_id": "strategy_pit_recommended:pit_now:1",
        "phase": EventPhase.OPENED,
        "priority": EventPriority.STRATEGIC,
        "category": "strategy",
        "message_key": "event.strategy_pit_recommended",
        "facts": (("gain_s", 3.5), ("action", "PIT_NOW")),
        "lap": 12,
        "emitted_at": 200.0,
        "interrupt": False,
        "supersedes": None,
    }
    values.update(changes)
    return AgentAlert(**values)  # type: ignore[arg-type]


def _decision(**changes: object) -> GateDecision:
    values: dict[str, object] = {
        "event_id": "strategy_pit_recommended:pit_now:1",
        "phase": EventPhase.OPENED,
        "outcome": GateOutcome.EMIT,
        "reason": "emitted",
        "alert": _alert(),
        "observed_at": 200.0,
    }
    values.update(changes)
    return GateDecision(**values)  # type: ignore[arg-type]


def test_signal_json_round_trip_is_canonical_and_order_independent() -> None:
    first = _signal()
    second = _signal(facts=(("confidence", 0.7), ("action", "PIT_NOW"), ("gain_s", 3.5)))

    assert first.to_json() == second.to_json()
    assert json.loads(first.to_json())["schema_version"] == EVENTS_SCHEMA_VERSION
    assert EventSignal.from_json(first.to_json()) == first
    assert json.loads(first.to_json())["facts"] == [
        ["action", "PIT_NOW"],
        ["confidence", 0.7],
        ["gain_s", 3.5],
    ]


def test_alert_json_round_trip_is_canonical_and_order_independent() -> None:
    first = _alert()
    second = _alert(facts=(("action", "PIT_NOW"), ("gain_s", 3.5)))

    assert first.to_json() == second.to_json()
    assert AgentAlert.from_json(first.to_json()) == first


def test_decision_json_round_trip_includes_the_alert() -> None:
    decision = _decision()

    payload = json.loads(decision.to_json())

    assert payload["alert"]["alert_id"] == "alert-1"
    assert GateDecision.from_json(decision.to_json()) == decision


def test_suppressed_decision_has_no_alert() -> None:
    decision = _decision(outcome=GateOutcome.SUPPRESS, reason="muted", alert=None)

    assert json.loads(decision.to_json())["alert"] is None
    assert GateDecision.from_json(decision.to_json()) == decision


def test_emit_decision_requires_a_matching_alert() -> None:
    with pytest.raises(ValueError, match="requieren un aviso"):
        _decision(alert=None)
    with pytest.raises(ValueError, match="mismo event_id"):
        _decision(alert=_alert(event_id="otro"))
    with pytest.raises(ValueError, match="fase"):
        _decision(phase=EventPhase.UPDATED)


def test_deserialization_rejects_unknown_schema_versions() -> None:
    payload = _signal().to_dict()
    payload["schema_version"] = 99

    with pytest.raises(ValueError, match="schema_version"):
        EventSignal.from_dict(payload)

    del payload["schema_version"]

    with pytest.raises(ValueError, match="schema_version"):
        EventSignal.from_dict(payload)


def test_types_are_immutable() -> None:
    signal = _signal()

    with pytest.raises(FrozenInstanceError):
        signal.lap = 13  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        _alert().interrupt = True  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        EventsSnapshot().muted = True  # type: ignore[misc]


def test_condition_policy_rejects_exit_on_the_wrong_side_of_enter() -> None:
    with pytest.raises(ValueError, match="exit debe ser mayor"):
        ConditionPolicy(
            rule="fuel_margin_low",
            category="fuel",
            priority=EventPriority.STRATEGIC,
            message_key="event.fuel_margin_low",
            enter=0.5,
            exit=0.4,
            direction="below",
        )

    with pytest.raises(ValueError, match="exit debe ser menor"):
        ConditionPolicy(
            rule="wear_high",
            category="tyres",
            priority=EventPriority.INFORMATIONAL,
            message_key="event.tyre_life_near_limit",
            enter=0.5,
            exit=0.6,
            direction="above",
        )


def test_condition_policy_accepts_valid_hysteresis() -> None:
    below = ConditionPolicy(
        rule="fuel_margin_low",
        category="fuel",
        priority=EventPriority.STRATEGIC,
        message_key="event.fuel_margin_low",
        enter=0.5,
        exit=0.6,
        direction="below",
    )
    above = ConditionPolicy(
        rule="wear_high",
        category="tyres",
        priority=EventPriority.INFORMATIONAL,
        message_key="event.tyre_life_near_limit",
        enter=0.5,
        exit=0.4,
        direction="above",
    )

    assert below.exit == 0.6
    assert above.exit == 0.4


def test_condition_policy_rejects_incomplete_or_negative_values() -> None:
    with pytest.raises(ValueError, match="juntos"):
        ConditionPolicy(
            rule="rule",
            category="category",
            priority=EventPriority.INFORMATIONAL,
            message_key="event.x",
            enter=0.5,
        )
    with pytest.raises(ValueError, match="persistence_s"):
        ConditionPolicy(
            rule="rule",
            category="category",
            priority=EventPriority.INFORMATIONAL,
            message_key="event.x",
            persistence_s=-1.0,
        )


def test_events_snapshot_is_canonical_and_read_only() -> None:
    alert_late = _alert(alert_id="b", emitted_at=20.0)
    alert_early = _alert(alert_id="a", emitted_at=10.0)
    signal_b = _signal(event_id="b")
    signal_a = _signal(event_id="a")

    snapshot = EventsSnapshot(
        alerts=(alert_late, alert_early),
        active_events=(signal_b, signal_a),
        counters={"suppressed": 2, "emitted": 1},
    )

    assert [alert.alert_id for alert in snapshot.alerts] == ["a", "b"]
    assert [signal.event_id for signal in snapshot.active_events] == ["a", "b"]
    assert list(snapshot.counters) == ["emitted", "suppressed"]


def test_event_config_defaults() -> None:
    cfg = AppConfig()

    assert cfg.events.enabled
    assert cfg.events.strategy.min_confidence == 0.5
    assert cfg.events.strategy.update_delta_s == 2.0
    assert cfg.events.pit_window.lead_laps == 1
    assert cfg.events.fuel.warn_laps == 0.5
    assert cfg.events.fuel.critical_laps == 0.0
    assert cfg.events.fuel.hysteresis_laps == 0.3
    assert cfg.events.tyres.warn_laps == 3.0
    assert cfg.events.tyres.degradation_ratio == 0.25
    assert cfg.events.tyres.hysteresis == 0.10
    assert cfg.events.tyres.min_stint_laps == 4
    assert cfg.events.weather.rain_enter_pct == 50.0
    assert cfg.events.weather.rain_exit_pct == 30.0
    assert cfg.events.data.stale_persistence_s == 3.0
    assert cfg.events.damage.warn_pct == 15.0
    assert cfg.events.damage.update_delta_pct == 10.0
    assert cfg.events.rivals.gap_s == 2.0
    assert cfg.gate.enabled
    assert cfg.gate.global_min_gap_s == 8.0
    assert cfg.gate.cooldown_s.strategic == 45.0
    assert cfg.gate.cooldown_s.informational == 120.0
    assert cfg.gate.equivalence_window_s == 90.0
    assert cfg.gate.update_min_interval_s == 30.0
    assert cfg.gate.defer_max_s == 20.0
    assert cfg.gate.quiet_first_laps == 1
    assert cfg.gate.informational_space_s == 30.0
    assert cfg.gate.pilot_quiet_s == 15.0
    assert cfg.gate.interruption_level == "all"
    assert cfg.gate.muted is False


@pytest.mark.parametrize(
    "body",
    [
        "[events.fuel]\nwarn_laps = 0.5\ncritical_laps = 1.0\n",
        "[events.weather]\nrain_enter_pct = 30\nrain_exit_pct = 50\n",
        '[gate]\ninterruption_level = "none"\n',
        "[events.tyres]\nmin_stint_laps = 0\n",
        "[gate]\nglobal_min_gap_s = -1\n",
    ],
)
def test_invalid_event_config_raises_config_error(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body)

    with pytest.raises(ConfigError):
        load_config(path)


def test_capture_session_exposes_an_empty_events_snapshot() -> None:
    session = CaptureSession(
        AppConfig(
            telemetry=TelemetryConfig(listen_port=0),
            capture=CaptureConfig(enabled=False),
            recording=RecordingConfig(enabled=False),
        )
    )
    try:
        snapshot = session.events_snapshot()
    finally:
        session.stop()

    assert isinstance(snapshot, EventsSnapshot)
    assert snapshot.alerts == ()
    assert snapshot.active_events == ()
    assert not snapshot.muted
    assert snapshot.error is None


def test_playback_session_exposes_an_empty_events_snapshot(tmp_path: Path) -> None:
    from f1_strategist.capture import CapturedPacket, CaptureWriter
    from tests.conftest import make_packet

    path = tmp_path / "capture.f1cap"
    with CaptureWriter(path) as writer:
        writer.write(
            CapturedPacket(
                recv_time_ns=0,
                source_ip="127.0.0.1",
                source_port=4000,
                payload=make_packet(1),
            )
        )
    session = PlaybackSession(path)

    snapshot = session.events_snapshot()

    assert isinstance(snapshot, EventsSnapshot)
    assert snapshot.alerts == ()
    assert snapshot.active_events == ()


def test_events_group_is_available_without_subcommands() -> None:
    assert main(["events"]) == 0
