from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, cast

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.config import AppConfig, CaptureConfig, EventsConfig, GateConfig, RecordingConfig
from f1_strategist.events.engine import EventEngine, EventEvaluation, evaluate_once
from f1_strategist.events.types import (
    ConditionPolicy,
    EventInputs,
    EventPriority,
    EventsSnapshot,
    Observation,
)
from f1_strategist.fingerprint import compute_gate_fingerprint
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.offline import OfflineEventPipeline, run_capture_offline
from f1_strategist.packets import HEADER_SIZE, HEADER_STRUCT
from f1_strategist.parser import ParsedPacket
from f1_strategist.playback import PlaybackSession
from f1_strategist.race_state import RaceEvent, RaceSessionSnapshot, RaceStateSnapshot
from f1_strategist.recorder import ingest_capture
from f1_strategist.session import CaptureSession
from f1_strategist.store import SessionStore
from f1_strategist.strategy.types import StrategySnapshot
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed


class _SpyRule:
    """Regla de prueba que cuenta evaluaciones y eventos de estado vistos."""

    def __init__(
        self,
        config: EventsConfig | None = None,
        *,
        active: bool = False,
    ) -> None:
        del config
        self.policy = ConditionPolicy(
            rule="spy",
            category="test",
            priority=EventPriority.CRITICAL,
            message_key="event.spy",
        )
        self.active = active
        self.calls = 0
        self.seen_events: list[tuple[RaceEvent, ...]] = []

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        self.calls += 1
        self.seen_events.append(tuple(inputs.new_state_events))
        return (
            Observation(
                key="spy",
                active=self.active,
                value=1.0,
                facts=(("calls", self.calls),),
            ),
        )


class _AlwaysActiveRule(_SpyRule):
    """Regla activa que el registro puede instanciar con la configuración."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        super().__init__(config, active=True)


class _FailingRule:
    """Regla que falla siempre para comprobar el aislamiento del motor."""

    def __init__(self) -> None:
        self.policy = ConditionPolicy(
            rule="boom",
            category="test",
            priority=EventPriority.CRITICAL,
            message_key="event.boom",
        )
        self.calls = 0

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        del inputs
        self.calls += 1
        raise RuntimeError("event boom")


class _FailOnceRule(_FailingRule):
    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        del inputs
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("event boom once")
        return (Observation(key="boom", active=False),)


class _SilentCloseRule(_SpyRule):
    def __init__(self) -> None:
        super().__init__(active=True)
        self.policy = ConditionPolicy(
            rule="silent_close",
            category="test",
            priority=EventPriority.CRITICAL,
            message_key="event.silent_close",
            announce_close=False,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        self.calls += 1
        self.seen_events.append(tuple(inputs.new_state_events))
        return (Observation(key="silent_close", active=self.active),)


class _StrategicRule(_SpyRule):
    def __init__(self) -> None:
        super().__init__(active=True)
        self.policy = ConditionPolicy(
            rule="strategic_test",
            category="strategy",
            priority=EventPriority.STRATEGIC,
            message_key="event.strategic_test",
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        self.calls += 1
        return (Observation(key="strategic_test", active=True),)


class _FailingEventEngine:
    """Doble del motor para la prueba de fallo a nivel de captura."""

    def apply(
        self,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot,
        strategy: StrategySnapshot,
        observed_at: float,
    ) -> None:
        del state, metrics, strategy, observed_at
        raise RuntimeError("events boom")

    def snapshot(self) -> EventsSnapshot:
        return EventsSnapshot()


def _state(
    *,
    uid: int = 1,
    lap: int = 1,
    events: tuple[RaceEvent, ...] = (),
) -> RaceStateSnapshot:
    session = RaceSessionSnapshot(
        uid=uid,
        session_type="Race",
        track="Test",
        total_laps=10,
        started=True,
        ended=False,
    )
    return RaceStateSnapshot(
        session=session,
        current_lap=lap,
        player_car_index=None,
        cars=(),
        freshness=(),
        events=events,
    )


def _metrics(computed_at: float, *, uid: int = 1) -> MetricsSnapshot:
    return MetricsSnapshot(computed_at_monotonic=computed_at, session_uid=uid)


def _serialize(snapshot: EventsSnapshot) -> tuple[Any, ...]:
    return (
        tuple(alert.to_dict() for alert in snapshot.alerts),
        tuple(signal.to_dict() for signal in snapshot.active_events),
        dict(snapshot.counters),
        snapshot.muted,
        snapshot.error,
    )


def test_note_pilot_interaction_is_delivered_by_writer_and_reset_clears_it() -> None:
    engine = EventEngine(EventsConfig(), evaluation_min_interval_s=10.0, rules=[])

    engine.note_pilot_interaction(10.0)
    engine.note_pilot_interaction(12.0)
    engine.apply(_state(), _metrics(0.0), StrategySnapshot(), 13.0)
    assert engine._gate._pilot_interaction_at == 12.0

    engine.note_pilot_interaction(14.0)
    engine.apply(_state(uid=2), _metrics(0.0, uid=2), StrategySnapshot(), 15.0)
    assert engine._gate._pilot_interaction_at is None


def test_regular_evaluations_respect_the_interval_and_state_events_force_one() -> None:
    rule = _SpyRule()
    engine = EventEngine(
        EventsConfig(),
        evaluation_min_interval_s=10.0,
        rules=[rule],
    )

    engine.apply(_state(), _metrics(0.0), StrategySnapshot(), 0.0)
    assert rule.calls == 1
    assert engine.last_evaluation is not None
    assert engine.last_evaluation.snapshot == engine.snapshot()

    engine.apply(_state(), _metrics(0.0), StrategySnapshot(), 5.0)
    assert rule.calls == 1
    assert engine.last_evaluation is None

    engine.apply(_state(), _metrics(1.0), StrategySnapshot(), 6.0)
    assert rule.calls == 1

    engine.apply(_state(), _metrics(1.0), StrategySnapshot(), 10.0)
    assert rule.calls == 2

    event = RaceEvent(kind="pit_entry", occurred_at_monotonic=12.0)
    events = (event,)
    engine.apply(_state(events=events), _metrics(1.0), StrategySnapshot(), 10.1)
    assert rule.calls == 3

    engine.apply(_state(events=events), _metrics(1.0), StrategySnapshot(), 10.2)
    assert rule.calls == 3


def test_state_events_are_not_replayed_across_evaluations() -> None:
    rule = _SpyRule()
    engine = EventEngine(EventsConfig(), rules=[rule])
    event = RaceEvent(kind="pit_entry", occurred_at_monotonic=5.0)
    events = (event,)

    engine.apply(_state(), _metrics(0.0), StrategySnapshot(), 0.0)
    assert rule.seen_events == [()]

    engine.apply(_state(events=events), _metrics(0.0), StrategySnapshot(), 0.1)
    assert rule.seen_events[-1] == events

    engine.apply(_state(events=(event, event)), _metrics(0.0), StrategySnapshot(), 0.2)
    assert len(rule.seen_events) == 2


def test_rule_failure_is_reported_once_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = EventEngine(EventsConfig(), rules=[_SpyRule()])
    engine.apply(_state(), _metrics(0.0), StrategySnapshot(), 0.0)
    assert engine.last_evaluation is not None

    def fail(*args: object, **kwargs: object) -> tuple[()]:
        raise RuntimeError("event boom")

    monkeypatch.setattr("f1_strategist.events.engine._collect_signals", fail)
    engine.apply(_state(), _metrics(1.0), StrategySnapshot(), 1.0)

    snapshot = engine.snapshot()
    assert snapshot.error is not None
    assert "RuntimeError" in snapshot.error
    assert "event boom" in snapshot.error
    assert engine.last_evaluation is None


def test_successful_rule_transition_is_published_when_a_later_rule_fails_once() -> None:
    active = _SpyRule(active=True)
    failing = _FailOnceRule()
    engine = EventEngine(EventsConfig(), rules=[active, failing])

    engine.apply(_state(), _metrics(0.0), StrategySnapshot(), 0.0)
    failed_snapshot = engine.snapshot()
    assert failed_snapshot.error is not None
    assert len(failed_snapshot.alerts) == 1
    opened_id = failed_snapshot.alerts[0].event_id

    engine.apply(_state(), _metrics(1.0), StrategySnapshot(), 1.0)
    recovered = engine.snapshot()
    assert recovered.error is None
    assert len(recovered.alerts) == 1
    assert recovered.active_events[0].event_id == opened_id


def test_silent_tracker_closure_cleans_gate_and_active_condition_stays_deferred() -> None:
    silent = _SilentCloseRule()
    engine = EventEngine(EventsConfig(), rules=[silent])
    engine.apply(_state(), _metrics(0.0), StrategySnapshot(), 0.0)
    assert len(engine.snapshot().active_events) == 1

    silent.active = False
    engine.apply(_state(), _metrics(1.0), StrategySnapshot(), 1.0)
    assert engine.snapshot().active_events == ()
    assert len(engine.snapshot().alerts) == 1

    critical = _SpyRule(active=True)
    strategic = _StrategicRule()
    deferred_engine = EventEngine(EventsConfig(), rules=[critical, strategic])
    deferred_engine.apply(_state(), _metrics(0.0), StrategySnapshot(), 0.0)
    assert {item.event_id for item in deferred_engine.snapshot().active_events} == {"spy:spy:1"}

    deferred_engine.apply(_state(), _metrics(1.0), StrategySnapshot(), 1.0)
    assert {item.event_id for item in deferred_engine.snapshot().active_events} == {
        "spy:spy:1",
        "strategic_test:strategic_test:1",
    }


def test_session_change_and_flashback_reset_the_engine() -> None:
    rule = _SpyRule(active=True)
    engine = EventEngine(EventsConfig(), evaluation_min_interval_s=1.0, rules=[rule])

    engine.apply(_state(uid=1, lap=3), _metrics(0.0), StrategySnapshot(), 0.0)
    first = engine.snapshot()
    assert len(first.alerts) == 1

    engine.apply(_state(uid=2, lap=1), _metrics(0.0, uid=2), StrategySnapshot(), 0.5)
    second = engine.snapshot()
    assert len(second.alerts) == 1
    assert second.alerts[0].event_id == first.alerts[0].event_id

    engine.apply(_state(uid=2, lap=1), _metrics(1.0, uid=2), StrategySnapshot(), 1.5)
    assert rule.calls == 3

    engine.apply(_state(uid=2, lap=0), _metrics(0.0, uid=2), StrategySnapshot(), 2.0)
    third = engine.snapshot()
    assert len(third.alerts) == 1
    assert third.alerts[0].event_id != second.alerts[0].event_id
    assert rule.calls == 4


def test_muted_agent_detects_and_records_without_emitting() -> None:
    rule = _SpyRule(active=True)
    engine = EventEngine(
        EventsConfig(),
        gate=GateConfig(muted=True),
        rules=[rule],
    )

    engine.apply(_state(), _metrics(0.0), StrategySnapshot(), 0.0)

    snapshot = engine.snapshot()
    assert rule.calls == 1
    assert snapshot.muted is True
    assert snapshot.alerts == ()
    assert snapshot.counters["muted"] == 1
    assert snapshot.counters["suppress"] == 1


def test_evaluate_once_is_pure_and_returns_signals_and_decisions() -> None:
    rule = _SpyRule(active=True)
    event = RaceEvent(kind="pit_entry", occurred_at_monotonic=2.0)
    inputs = EventInputs(
        state=_state(events=(event,)),
        metrics=_metrics(0.0),
        strategy=StrategySnapshot(),
        config=EventsConfig(),
        now=1.0,
        lap=1,
        new_state_events=(event,),
    )

    result = evaluate_once(inputs, rules=[rule])

    assert rule.calls == 1
    assert len(result.signals) == 1
    assert result.decisions[0].outcome.value == "emit"
    assert len(result.snapshot.alerts) == 1


def test_capture_survives_an_event_engine_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets = list(build_race_timed(_cars(), total_laps=2, samples_per_lap=1))
    parsed = iter(packet for packet, _ in packets)
    monkeypatch.setattr("f1_strategist.session.parse_packet", lambda _: next(parsed))

    session = CaptureSession(
        AppConfig(
            capture=CaptureConfig(enabled=False),
            recording=RecordingConfig(enabled=False),
        )
    )
    session._event_engine = cast(Any, _FailingEventEngine())
    session._stop_consumer.set()
    for packet, observed_at in packets:
        session._queue.put(
            CapturedPacket(
                recv_time_ns=0,
                source_ip="127.0.0.1",
                source_port=4000,
                payload=_payload(packet),
                recv_monotonic=observed_at,
            )
        )

    session._consume()

    stats = session.snapshot()
    assert stats.consumer_errors == 1
    assert stats.consumer_error is not None
    assert stats.consumer_error.key == "events_engine_error"
    assert stats.consumer_error.get("error") == "events boom"
    assert session.race_snapshot().session.uid == packets[0][0].header.session_uid


def test_events_are_invariant_to_playback_speed_and_offline_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, packets = _events_capture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "f1_strategist.events.engine.EVENT_RULES",
        (_AlwaysActiveRule(),),
    )
    evaluations: list[EventEvaluation] = []
    apply = EventEngine.apply

    def apply_and_collect(
        engine: EventEngine,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot,
        strategy: StrategySnapshot,
        observed_at: float,
    ) -> None:
        apply(engine, state, metrics, strategy, observed_at)
        if engine.last_evaluation is not None:
            evaluations.append(engine.last_evaluation)

    monkeypatch.setattr(EventEngine, "apply", apply_and_collect)

    def gate_fingerprint() -> str:
        records = (
            record
            for evaluation in evaluations
            for record in (*evaluation.signals, *evaluation.decisions)
        )
        return compute_gate_fingerprint(records)

    snapshots = []
    fingerprints = []
    for speed in (0.0, 1.0, 4.0):
        evaluations.clear()
        session = PlaybackSession(path, speed=speed)
        session.play()
        assert session.wait(timeout=10.0), "la reproducción no terminó a tiempo"
        assert session.error is None
        snapshots.append(_serialize(session.events_snapshot()))
        fingerprints.append(gate_fingerprint())

    evaluations.clear()
    offline = OfflineEventPipeline(AppConfig())
    run_capture_offline(path, [offline])
    snapshots.append(_serialize(offline.events_snapshot()))
    fingerprints.append(gate_fingerprint())

    assert snapshots[0][0], "el escenario debe generar al menos un aviso"
    assert snapshots[0] == snapshots[1] == snapshots[2] == snapshots[3]
    assert len(set(fingerprints)) == 1

    evaluations.clear()
    with SessionStore.open(tmp_path / "events.db") as store:
        first = ingest_capture(path, store, AppConfig())
        first_session = store.get_session(first.session_id)
        assert first_session is not None
        assert first_session.gate_fingerprint == fingerprints[0]
        assert gate_fingerprint() == fingerprints[0]

        evaluations.clear()
        replaced = ingest_capture(path, store, AppConfig(), replace=True)
        replaced_session = store.get_session(replaced.session_id)
        assert replaced_session is not None
        assert replaced_session.gate_fingerprint == first_session.gate_fingerprint
        assert gate_fingerprint() == fingerprints[0]


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(1, "Driver B", 2, 90.5, 40.0, 2.0, 2.3, compounds=("medium",)),
    )


def _events_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, list[tuple[ParsedPacket, float]]]:
    packets = list(
        build_race_timed(
            _cars(),
            total_laps=3,
            samples_per_lap=1,
            safety_car_laps=(2, 2),
        )
    )
    path = tmp_path / "events.f1cap"
    with CaptureWriter(path) as writer:
        for index, _ in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=index * 2_000_000,
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=make_packet(1, extra=struct.pack("<I", index)),
                )
            )

    parsed = [packet for packet, _ in packets]

    def fake_parse(payload: bytes) -> ParsedPacket:
        index = struct.unpack_from("<I", payload, HEADER_SIZE)[0]
        return parsed[index]

    monkeypatch.setattr("f1_strategist.playback.parse_packet", fake_parse)
    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)
    return path, packets


def _payload(packet: ParsedPacket) -> bytes:
    header = packet.header
    return HEADER_STRUCT.pack(
        header.packet_format,
        header.game_year,
        header.game_major_version,
        header.game_minor_version,
        header.packet_version,
        header.packet_id,
        header.session_uid,
        header.session_time,
        header.frame_identifier,
        header.overall_frame_identifier,
        header.player_car_index,
        header.secondary_player_car_index,
    )
