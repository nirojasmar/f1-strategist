from __future__ import annotations

import json
import re
import struct
import threading
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter

import pytest

from f1_strategist.agent.communicator import AgentCommunicator, AlertSources
from f1_strategist.agent.context import ContextBuilder
from f1_strategist.agent.llm_client import FakeLLMClient, RecordedLLMClient, request_fingerprint
from f1_strategist.agent.types import (
    LLMHealth,
    LLMRequest,
    LLMResponse,
    LLMStatus,
    MessageSource,
)
from f1_strategist.agent.validator import ResponseValidator
from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.config import (
    AgentConfig,
    AppConfig,
    EventsConfig,
    GateConfig,
    GateCooldownConfig,
    LLMConfig,
)
from f1_strategist.datasets import load_manifest
from f1_strategist.events.engine import EventEngine
from f1_strategist.events.types import (
    AgentAlert,
    ConditionPolicy,
    EventInputs,
    EventPhase,
    EventPriority,
    EventSignal,
    EventsSnapshot,
    Observation,
)
from f1_strategist.fingerprint import compute_agent_fingerprint
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.offline import OfflineEventPipeline, run_capture_offline
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import ParsedPacket
from f1_strategist.playback import PlaybackSession
from f1_strategist.race_state import CarSnapshot, RaceSessionSnapshot, RaceStateSnapshot
from f1_strategist.strategy.types import StrategySnapshot
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed

_REAL_MANIFEST = load_manifest()
_MANIFEST_IDS = ("monza-13-laps", "baku-13-laps-vsc-retirement", "sakhir-20-laps-high-wear")


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(1, "Driver B", 2, 90.5, 40.0, 2.0, 2.3, compounds=("medium",)),
    )


def _state(lap: int = 12) -> RaceStateSnapshot:
    return RaceStateSnapshot(
        session=RaceSessionSnapshot(
            uid=1,
            session_type="race",
            track="Monza",
            total_laps=50,
            started=True,
            ended=False,
        ),
        current_lap=lap,
        player_car_index=0,
        cars=(
            CarSnapshot(car_index=0, position=3, actual_tyre_compound="MEDIUM", tyre_age_laps=7),
        ),
        freshness=(),
        events=(),
    )


def _metrics(lap: int = 12) -> MetricsSnapshot:
    return MetricsSnapshot(computed_at_monotonic=float(lap), session_uid=1)


def _strategy() -> StrategySnapshot:
    return StrategySnapshot()


def _sources(
    *,
    state: RaceStateSnapshot | None = None,
    events: EventsSnapshot | None = None,
    observed_at: float = 12.0,
) -> AlertSources:
    return AlertSources(
        state=state or _state(),
        metrics=_metrics(),
        strategy=_strategy(),
        events=events or EventsSnapshot(),
        observed_at=observed_at,
    )


def _alert(
    alert_id: str = "alert1",
    *,
    event_id: str = "event1",
    priority: EventPriority = EventPriority.STRATEGIC,
    message_key: str = "event.strategy_pit_recommended",
    facts: tuple[tuple[str, object], ...] = (
        ("gain_s", 2.4),
        ("confidence", 0.7),
        ("laps_remaining", 2),
    ),
    emitted_at: float = 12.0,
    supersedes: str | None = None,
) -> AgentAlert:
    return AgentAlert(
        alert_id=alert_id,
        event_id=event_id,
        phase=EventPhase.OPENED,
        priority=priority,
        category="strategy",
        message_key=message_key,
        facts=facts,  # type: ignore[arg-type]
        lap=12,
        emitted_at=emitted_at,
        supersedes=supersedes,
    )


def _signal(event_id: str) -> EventSignal:
    return EventSignal(
        rule="test",
        event_id=event_id,
        phase=EventPhase.OPENED,
        priority=EventPriority.STRATEGIC,
        category="strategy",
        message_key="event.test",
    )


def _communicator(
    client: object,
    *,
    mode: str = "live",
    agent: AgentConfig | None = None,
) -> AgentCommunicator:
    agent = agent or AgentConfig(critical_uses_llm=True)
    llm = LLMConfig(enabled=True, model="fake")
    return AgentCommunicator(
        agent,
        llm,
        client=client,  # type: ignore[arg-type]
        builder=ContextBuilder(agent),
        validator=ResponseValidator(agent),
        mode=mode,  # type: ignore[arg-type]
        clock=lambda: 12.0,
    )


class _RecordingClient:
    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.records: dict[str, LLMResponse] = {}

    def generate(
        self, request: LLMRequest, *, cancel: threading.Event | None = None
    ) -> LLMResponse:
        self.records[request_fingerprint(request)] = self.response
        return self.response

    def health(self) -> LLMHealth:
        return LLMHealth(status=LLMStatus.AVAILABLE)


class _BlockingClient:
    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.calls: list[LLMRequest] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def generate(
        self, request: LLMRequest, *, cancel: threading.Event | None = None
    ) -> LLMResponse:
        self.calls.append(request)
        self.started.set()
        self.release.wait(timeout=5.0)
        return self.response

    def health(self) -> LLMHealth:
        return LLMHealth(status=LLMStatus.AVAILABLE)


def _reply(**changes: object) -> str:
    payload: dict[str, object] = {
        "should_speak": True,
        "priority": "strategic",
        "language": "es",
        "message": "Box esta vuelta.",
        "reason": "prueba",
        "used_facts": [],
    }
    payload.update(changes)
    return json.dumps(payload)


def _numbers(text: str) -> list[float]:
    return [float(token.replace(",", ".")) for token in re.findall(r"\d+(?:[.,]\d+)?", text)]


def _assert_numbers_from_facts(message: str, facts: Sequence[tuple[str, object]]) -> None:
    allowed = {float(value) for _, value in facts if isinstance(value, (int, float))}
    for number in _numbers(message):
        assert any(abs(number - candidate) < 1e-9 for candidate in allowed), (
            f"cifra inventada {number} en {message!r}; hechos={facts!r}"
        )


def _gate(**changes: object) -> GateConfig:
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


class _LapRule:
    def __init__(self, rule: str, priority: EventPriority) -> None:
        self.policy = ConditionPolicy(
            rule=rule,
            category="track" if priority is EventPriority.CRITICAL else "strategy",
            priority=priority,
            message_key=f"event.{rule}",
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        return (Observation(key=self.policy.rule, active=True, facts=(("lap", inputs.lap),)),)


class _OneAlertEngine:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self._snapshot = EventsSnapshot()

    def apply(
        self,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot,
        strategy: StrategySnapshot,
        observed_at: float,
    ) -> None:
        alert = AgentAlert(
            alert_id="alert1",
            event_id="event1",
            phase=EventPhase.OPENED,
            priority=EventPriority.CRITICAL,
            category="track",
            message_key="event.safety_car_deployed",
            facts=(("lap", 2),),
            lap=2,
            emitted_at=observed_at,
        )
        self._snapshot = EventsSnapshot(alerts=(alert,))

    def snapshot(self) -> EventsSnapshot:
        return self._snapshot


class _FastClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def wait(self, timeout: float | None) -> bool:
        self.now += timeout if timeout is not None else 0.0
        return True


def _write_capture(path: Path, packets: Sequence[tuple[ParsedPacket, float]]) -> None:
    with CaptureWriter(path) as writer:
        for index, _ in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=index,
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=make_packet(1, extra=struct.pack("<I", index)),
                )
            )


def test_normal_synthetic_scenario_makes_no_llm_calls() -> None:
    client = FakeLLMClient()
    communicator = _communicator(client, mode="recorded")
    pipeline = OfflineEventPipeline(AppConfig(), agent_communicator=communicator)

    for packet, observed_at in build_race_timed(_cars(), total_laps=4, samples_per_lap=2):
        pipeline.apply(packet, observed_at)
    pipeline.stop()

    assert pipeline.events_snapshot().alerts == ()
    assert client.requests == []
    assert pipeline.agent_snapshot().messages == ()


def test_muted_gate_suppresses_alerts_and_calls() -> None:
    client = FakeLLMClient()
    communicator = _communicator(client, mode="recorded")
    engine = EventEngine(
        EventsConfig(),
        gate=_gate(muted=True),
        evaluation_min_interval_s=0.001,
        rules=[_LapRule("test.lap", EventPriority.CRITICAL)],
    )

    for lap in range(1, 4):
        state = _state(lap)
        metrics = _metrics(lap)
        engine.apply(state, metrics, _strategy(), float(lap))
        events = engine.snapshot()
        communicator.observe(events, _sources(state=state, events=events, observed_at=float(lap)))

    assert engine.snapshot().alerts == ()
    assert client.requests == []
    assert communicator.snapshot().messages == ()


@pytest.mark.parametrize(
    ("case", "reply"),
    [
        ("invented_number", _reply(message="Box ahora: 99.9 segundos de ventaja.")),
        ("changed_unit", _reply(message="Box ahora: 42.0 kg de desgaste.")),
        ("altered_priority", _reply(priority="critical")),
        ("wrong_language", _reply(language="en", message="Box now.")),
        ("broken_json", "not json"),
    ],
)
def test_adversarial_reply_never_reaches_the_radio(case: str, reply: str) -> None:
    alert = _alert(
        message_key="event.tyre_life_near_limit",
        facts=(
            ("wear_mean_pct", 42.0),
            ("age_laps", 7),
            ("laps_left", 3),
            ("compound", "soft"),
        ),
    )
    events = EventsSnapshot(alerts=(alert,), active_events=(_signal(alert.event_id),))
    sources = _sources(events=events)
    communicator = _communicator(FakeLLMClient([reply]), mode="recorded")

    communicator.observe(events, sources)
    calls, _ = communicator.drain_records()
    snapshot = communicator.snapshot()

    assert calls, case
    assert calls[0].validation_valid is False, case
    assert calls[0].fallback_reason == "validation_failed", case
    assert len(snapshot.messages) == 1, case
    message = snapshot.messages[0]
    assert message.source is MessageSource.TEMPLATE, case
    assert message.message_id == f"{alert.alert_id}:template", case
    assert "99.9" not in message.text, case
    _assert_numbers_from_facts(message.text, alert.facts)


def test_critical_template_is_immediate_without_calling_the_model() -> None:
    client = _BlockingClient(LLMResponse(text=_reply()))
    communicator = _communicator(client, mode="live", agent=AgentConfig())
    communicator.start()
    alert = _alert(priority=EventPriority.CRITICAL)
    started = perf_counter()
    try:
        communicator.submit(alert, _sources(events=EventsSnapshot(alerts=(alert,))))
        elapsed = perf_counter() - started
    finally:
        communicator.stop()

    assert elapsed < 0.05
    assert client.calls == []
    assert communicator.snapshot().messages[0].source is MessageSource.TEMPLATE


def test_model_timeout_falls_back_with_reason() -> None:
    alert = _alert()
    events = EventsSnapshot(alerts=(alert,), active_events=(_signal(alert.event_id),))
    client = FakeLLMClient([LLMResponse(error="timeout", total_s=10.0)])
    communicator = _communicator(client, mode="recorded")

    communicator.observe(events, _sources(events=events))
    calls, _ = communicator.drain_records()
    snapshot = communicator.snapshot()

    assert client.requests[0].timeout_s == 10.0
    assert calls[0].fallback_reason == "timeout"
    assert calls[0].total_s == 10.0
    assert snapshot.counters["fallback:timeout"] == 1
    assert snapshot.messages[0].source is MessageSource.TEMPLATE
    assert snapshot.messages[0].message_id == f"{alert.alert_id}:template"
    _assert_numbers_from_facts(snapshot.messages[0].text, alert.facts)


def test_slow_client_does_not_delay_submission() -> None:
    client = _BlockingClient(LLMResponse(error="timeout"))
    communicator = _communicator(client, mode="live")
    communicator.start()
    sources = _sources()
    try:
        communicator.submit(_alert("alert1", event_id="event1"), sources)
        assert client.started.wait(timeout=2.0)
        started = perf_counter()
        for index in range(2, 12):
            communicator.submit(
                _alert(f"alert{index}", event_id=f"event{index}"),
                sources,
            )
        elapsed = perf_counter() - started
        client.release.set()
        assert communicator.wait_idle(timeout=5.0)
    finally:
        client.release.set()
        communicator.stop()

    assert elapsed < 0.5
    snapshot = communicator.snapshot()
    assert snapshot.counters.get("fallback:timeout", 0) >= 1
    assert snapshot.messages
    assert all(message.source is MessageSource.TEMPLATE for message in snapshot.messages)


def test_closed_alert_during_generation_is_discarded() -> None:
    client = _BlockingClient(LLMResponse(text=_reply()))
    communicator = _communicator(client, mode="live")
    communicator.start()
    alert = _alert()
    events = EventsSnapshot(alerts=(alert,), active_events=(_signal(alert.event_id),))
    try:
        communicator.submit(alert, _sources(events=events))
        assert client.started.wait(timeout=2.0)
        communicator.cancel_obsolete(EventsSnapshot(), now=100.0)
        client.release.set()
        assert communicator.wait_idle(timeout=5.0)
    finally:
        client.release.set()
        communicator.stop()

    snapshot = communicator.snapshot()
    assert snapshot.counters.get("discarded_obsolete", 0) >= 1
    assert all(message.source is not MessageSource.LLM for message in snapshot.messages)


def test_superseded_alert_is_discarded() -> None:
    client = _BlockingClient(LLMResponse(text=_reply()))
    communicator = _communicator(client, mode="live")
    communicator.start()
    first = _alert("alert1", event_id="event1", supersedes=None)
    second = _alert("alert2", event_id="event2", supersedes="alert1")
    try:
        communicator.submit(first, _sources(events=EventsSnapshot(alerts=(first,))))
        assert client.started.wait(timeout=2.0)
        communicator.submit(second, _sources(events=EventsSnapshot(alerts=(second,))))
        client.release.set()
        assert communicator.wait_idle(timeout=5.0)
    finally:
        client.release.set()
        communicator.stop()

    snapshot = communicator.snapshot()
    assert snapshot.counters.get("discarded_obsolete", 0) >= 1
    assert all(message.alert_id != "alert1" for message in snapshot.messages)


def test_template_playback_fingerprint_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "race.f1cap"
    packets = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    _write_capture(path, packets)
    parsed = [packet for packet, _ in packets]

    def fake_parse(payload: bytes) -> ParsedPacket:
        return parsed[struct.unpack_from("<I", payload, HEADER_SIZE)[0]]

    monkeypatch.setattr("f1_strategist.playback.parse_packet", fake_parse)
    monkeypatch.setattr("f1_strategist.playback.EventEngine", _OneAlertEngine)

    fingerprints: list[str] = []
    for speed in (0.0, 1.0, 4.0, 1.0):
        clock = _FastClock()
        session = PlaybackSession(path, speed=speed, clock=clock, wait_timeout=clock.wait)
        session.play()
        assert session.wait(timeout=30.0)
        assert session.error is None
        assert session.runs_completed == 1
        fingerprints.append(compute_agent_fingerprint(session.agent_snapshot().messages))

    assert fingerprints[0]
    assert len(set(fingerprints)) == 1


def test_recorded_playback_reproduces_the_same_texts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "race.f1cap"
    packets = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    _write_capture(path, packets)
    parsed = [packet for packet, _ in packets]

    def fake_parse(payload: bytes) -> ParsedPacket:
        return parsed[struct.unpack_from("<I", payload, HEADER_SIZE)[0]]

    monkeypatch.setattr("f1_strategist.playback.parse_packet", fake_parse)
    monkeypatch.setattr("f1_strategist.playback.EventEngine", _OneAlertEngine)

    agent = AgentConfig(critical_uses_llm=True)
    response = LLMResponse(
        text=_reply(
            priority="critical",
            message="Coche de seguridad desplegado.",
            reason="seguridad",
        )
    )
    recorder = _RecordingClient(response)
    first = _communicator(recorder, mode="recorded", agent=agent)
    clock_one = _FastClock()
    session_one = PlaybackSession(
        path,
        speed=1.0,
        clock=clock_one,
        wait_timeout=clock_one.wait,
        agent_communicator=first,
        agent_config=agent,
    )
    session_one.play()
    assert session_one.wait(timeout=30.0)
    assert session_one.error is None

    replay = _communicator(RecordedLLMClient(recorder.records), mode="recorded", agent=agent)
    clock_two = _FastClock()
    session_two = PlaybackSession(
        path,
        speed=1.0,
        clock=clock_two,
        wait_timeout=clock_two.wait,
        agent_communicator=replay,
        agent_config=agent,
    )
    session_two.play()
    assert session_two.wait(timeout=30.0)
    assert session_two.error is None

    first_texts = [message.text for message in session_one.agent_snapshot().messages]
    second_texts = [message.text for message in session_two.agent_snapshot().messages]
    assert first_texts
    assert first_texts == second_texts


@pytest.mark.real_capture
@pytest.mark.parametrize("scenario_id", _MANIFEST_IDS)
def test_manifest_scenario_with_recorded_context(scenario_id: str) -> None:
    scenario = _REAL_MANIFEST.get(scenario_id)
    if not scenario.available:
        pytest.skip(f"captura no disponible: {scenario.file.name}")

    config = AppConfig()
    recorder = _RecordingClient(LLMResponse(text=_reply(message="Pit ahora.")))
    first = OfflineEventPipeline(
        config, agent_communicator=_communicator(recorder, mode="recorded")
    )
    result = run_capture_offline(scenario.file, [first])
    first.stop()
    first_snapshot = first.agent_snapshot()

    assert recorder.records
    assert result.integrity_problems() == ()
    assert first_snapshot.error is None

    replay = OfflineEventPipeline(
        config,
        agent_communicator=_communicator(RecordedLLMClient(recorder.records), mode="recorded"),
    )
    replay_result = run_capture_offline(scenario.file, [replay])
    replay.stop()
    replay_snapshot = replay.agent_snapshot()

    assert replay_result.integrity_problems() == ()
    assert replay_snapshot.error is None
    first_messages = tuple(
        (message.alert_id, message.text, message.emitted_at) for message in first_snapshot.messages
    )
    replay_messages = tuple(
        (message.alert_id, message.text, message.emitted_at) for message in replay_snapshot.messages
    )
    assert first_messages
    assert first_messages == replay_messages
    assert compute_agent_fingerprint(first_snapshot.messages) == compute_agent_fingerprint(
        replay_snapshot.messages
    )


@pytest.mark.benchmark
def test_context_build_and_validation_mean_under_two_ms() -> None:
    agent = AgentConfig()
    builder = ContextBuilder(agent)
    validator = ResponseValidator(agent)
    alert = _alert()
    state = _state()
    reply = _reply(message="Box esta vuelta: ganamos 2,4 s, con confianza 0,7.")

    for _ in range(5):
        context = builder.build_alert(alert, state=state)
        assert validator.validate(reply, context, alert).valid

    iterations = 200
    started = perf_counter()
    for _ in range(iterations):
        context = builder.build_alert(alert, state=state)
        assert validator.validate(reply, context, alert).valid
    mean_s = (perf_counter() - started) / iterations
    assert mean_s < 0.002


@pytest.mark.benchmark
def test_slow_client_does_not_block_the_consumer() -> None:
    client = _BlockingClient(LLMResponse(error="timeout"))
    communicator = _communicator(client, mode="live")
    communicator.start()
    sources = _sources()
    try:
        started = perf_counter()
        for index in range(12):
            communicator.submit(
                _alert(f"alert{index}", event_id=f"event{index}"),
                sources,
            )
        elapsed = perf_counter() - started
        assert client.started.wait(timeout=2.0)
        client.release.set()
        assert communicator.wait_idle(timeout=5.0)
    finally:
        client.release.set()
        communicator.stop()

    assert elapsed < 0.5
