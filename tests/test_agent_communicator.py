from __future__ import annotations

import struct
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import pytest

from f1_strategist.agent.communicator import (
    AgentCommunicator,
    AgentMode,
    AlertSources,
    PromptSet,
    RenderedPrompt,
    build_communicator,
)
from f1_strategist.agent.types import (
    ContextKind,
    ContextPayload,
    LLMHealth,
    LLMMessage,
    LLMReply,
    LLMRequest,
    LLMResponse,
    LLMStatus,
    MessageSource,
    ValidationResult,
)
from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.config import AgentConfig, AppConfig, LLMConfig
from f1_strategist.events.types import (
    AgentAlert,
    EventPhase,
    EventPriority,
    EventSignal,
    EventsSnapshot,
    Fact,
)
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import ParsedPacket
from f1_strategist.playback import PlaybackSession
from f1_strategist.race_state import RaceState, RaceStateSnapshot
from f1_strategist.session import CaptureSession
from f1_strategist.strategy.types import StrategySnapshot
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed


class FakeClient:
    def __init__(self, response: LLMResponse | None = None, *, blocking: bool = False) -> None:
        self.response = response or LLMResponse(text='{"message": "ok"}')
        self.calls: list[LLMRequest] = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not blocking:
            self.release.set()

    def generate(
        self, request: LLMRequest, *, cancel: threading.Event | None = None
    ) -> LLMResponse:
        self.calls.append(request)
        self.started.set()
        self.release.wait(timeout=5.0)
        return self.response

    def health(self) -> LLMHealth:
        return LLMHealth(status=LLMStatus.AVAILABLE)


class FakeBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def build_alert(
        self,
        alert: AgentAlert,
        *,
        state: RaceStateSnapshot,
        metrics: MetricsSnapshot,
        strategy: StrategySnapshot,
        events: EventsSnapshot,
        recent: Sequence[str],
    ) -> ContextPayload:
        self.calls += 1
        return ContextPayload(
            kind=ContextKind.ALERT,
            language="es",
            alert={"message_key": alert.message_key, "recent": list(recent)},
        )


class _Rendered:
    messages: Sequence[LLMMessage] = (LLMMessage(role="user", content="hola"),)
    prompt_version = "test/1"
    response_schema: Mapping[str, Any] = {}


class _PromptSet:
    def render(self, kind: ContextKind, context: ContextPayload) -> RenderedPrompt:
        return _Rendered()


class FakeRegistry:
    def load(self, language: str) -> PromptSet:
        return _PromptSet()


class FakeValidator:
    def __init__(self, *, valid: bool = True, should_speak: bool = True) -> None:
        self.valid = valid
        self.should_speak = should_speak
        self.calls = 0

    def validate(self, text: str, context: ContextPayload, alert: AgentAlert) -> ValidationResult:
        self.calls += 1
        if not self.valid:
            return ValidationResult(valid=False, errors=("bad_fact",))
        reply = LLMReply(
            should_speak=self.should_speak,
            priority=alert.priority,
            language="es",
            message=f"LLM {alert.alert_id}",
        )
        return ValidationResult(valid=True, reply=reply)


def _renderer(message_key: str, facts: tuple[Fact, ...], language: str) -> str:
    return f"T:{message_key}"


def _alert(
    alert_id: str = "alert1",
    *,
    priority: EventPriority = EventPriority.STRATEGIC,
    emitted_at: float = 10.0,
    supersedes: str | None = None,
    event_id: str = "event1",
) -> AgentAlert:
    return AgentAlert(
        alert_id=alert_id,
        event_id=event_id,
        phase=EventPhase.OPENED,
        priority=priority,
        category="strategy",
        message_key="event.test",
        facts=(("lap", 3),),
        lap=3,
        emitted_at=emitted_at,
        supersedes=supersedes,
    )


def _signal(event_id: str = "event1") -> EventSignal:
    return EventSignal(
        rule="test",
        event_id=event_id,
        phase=EventPhase.OPENED,
        priority=EventPriority.STRATEGIC,
        category="strategy",
        message_key="event.test",
    )


def _sources(observed_at: float = 10.0) -> AlertSources:
    return AlertSources(
        state=RaceState().snapshot(),
        metrics=MetricsSnapshot(),
        strategy=StrategySnapshot(),
        events=EventsSnapshot(),
        observed_at=observed_at,
    )


def _communicator(
    client: FakeClient | None = None,
    *,
    mode: AgentMode = "live",
    validator: FakeValidator | None = None,
    critical_uses_llm: bool = False,
    fallback_on_failure: Literal["template", "drop"] = "template",
    queue_capacity: int = 64,
) -> AgentCommunicator:
    return AgentCommunicator(
        AgentConfig(
            critical_uses_llm=critical_uses_llm,
            fallback_on_failure=fallback_on_failure,
            recent_messages=3,
        ),
        LLMConfig(enabled=True, model="fake-model"),
        client=client or FakeClient(),
        builder=FakeBuilder(),
        registry=FakeRegistry(),
        validator=validator or FakeValidator(),
        template_renderer=_renderer,
        mode=mode,
        queue_capacity=queue_capacity,
    )


def test_valid_response_emits_llm_message() -> None:
    client = FakeClient()
    communicator = _communicator(client)
    communicator.start()
    communicator.submit(_alert(), _sources())
    assert communicator.wait_idle(timeout=5.0)
    communicator.stop()

    snapshot = communicator.snapshot()
    assert [message.source for message in snapshot.messages] == [MessageSource.LLM]
    assert snapshot.messages[0].text == "LLM alert1"
    assert snapshot.counters["used"] == 1
    assert len(client.calls) == 1


def test_generation_sends_schema_and_records_call() -> None:
    client = FakeClient()
    communicator = _communicator(client, mode="recorded")
    communicator.submit(_alert(), _sources())

    calls, messages = communicator.drain_records()
    assert client.calls[0].response_format == {
        "json_schema": {"name": "llm_reply", "schema": {}, "strict": True}
    }
    assert calls[0].outcome == "used"
    assert calls[0].alert_id == "alert1"
    assert messages[0].llm_call_id == "alert1:llm"


def test_build_communicator_respects_llm_enabled() -> None:
    live = build_communicator(AgentConfig(), LLMConfig(enabled=True, model="fake-model"))
    assert live.mode == "live"
    live.stop()
    disabled = build_communicator(AgentConfig(), LLMConfig())
    assert disabled.mode == "template"


def test_invalid_response_falls_back_to_template() -> None:
    communicator = _communicator(FakeClient(), validator=FakeValidator(valid=False))
    communicator.start()
    communicator.submit(_alert(), _sources())
    assert communicator.wait_idle(timeout=5.0)
    communicator.stop()

    snapshot = communicator.snapshot()
    assert [message.source for message in snapshot.messages] == [MessageSource.TEMPLATE]
    assert snapshot.counters["fallback:validation_failed"] == 1


def test_error_response_falls_back_with_reason() -> None:
    for error, reason in (("HTTP 500", "http_error"), ("timeout", "timeout")):
        communicator = _communicator(FakeClient(LLMResponse(error=error)))
        communicator.start()
        communicator.submit(_alert(), _sources())
        assert communicator.wait_idle(timeout=5.0)
        communicator.stop()
        snapshot = communicator.snapshot()
        assert snapshot.messages[0].source is MessageSource.TEMPLATE
        assert snapshot.counters[f"fallback:{reason}"] == 1


def test_late_response_for_closed_event_is_discarded() -> None:
    client = FakeClient(blocking=True)
    communicator = _communicator(client)
    communicator.start()
    try:
        communicator.submit(_alert(), _sources())
        assert client.started.wait(timeout=5.0)
        communicator.cancel_obsolete(EventsSnapshot(active_events=(_signal(),)), 10.5)
        communicator.cancel_obsolete(EventsSnapshot(), 11.0)
        client.release.set()
        assert communicator.wait_idle(timeout=5.0)
    finally:
        client.release.set()
        communicator.stop()

    snapshot = communicator.snapshot()
    assert snapshot.messages == ()
    assert snapshot.counters["discarded_obsolete"] == 1


def test_superseded_alert_is_discarded() -> None:
    client = FakeClient(blocking=True)
    communicator = _communicator(client)
    communicator.start()
    try:
        communicator.submit(_alert("alert1"), _sources())
        assert client.started.wait(timeout=5.0)
        communicator.submit(_alert("alert2", supersedes="alert1"), _sources())
        client.release.set()
        assert communicator.wait_idle(timeout=5.0)
    finally:
        client.release.set()
        communicator.stop()

    snapshot = communicator.snapshot()
    assert [message.alert_id for message in snapshot.messages] == ["alert2"]
    assert snapshot.counters["discarded_obsolete"] == 1
    assert snapshot.counters["used"] == 1


def test_full_queue_drops_the_oldest_without_blocking() -> None:
    client = FakeClient(blocking=True)
    communicator = _communicator(client, queue_capacity=1)
    communicator.start()
    try:
        communicator.submit(_alert("alert1"), _sources())
        assert client.started.wait(timeout=5.0)
        communicator.submit(_alert("alert2"), _sources())
        communicator.submit(_alert("alert3"), _sources())
        client.release.set()
        assert communicator.wait_idle(timeout=5.0)
    finally:
        client.release.set()
        communicator.stop()

    snapshot = communicator.snapshot()
    assert [message.alert_id for message in snapshot.messages] == ["alert1", "alert3"]
    assert snapshot.counters["queue_overflow"] == 1


def test_critical_is_immediate_and_does_not_wait_for_the_model() -> None:
    client = FakeClient(blocking=True)
    communicator = _communicator(client)
    communicator.start()
    try:
        communicator.submit(_alert(priority=EventPriority.CRITICAL), _sources())
        snapshot = communicator.snapshot()
        assert snapshot.messages[0].source is MessageSource.TEMPLATE
        assert snapshot.messages[0].priority is EventPriority.CRITICAL
        assert client.calls == []
    finally:
        client.release.set()
        communicator.stop()


def test_critical_during_an_in_flight_generation_still_uses_the_template() -> None:
    client = FakeClient(blocking=True)
    communicator = _communicator(client)
    communicator.start()
    try:
        communicator.submit(_alert("alert1"), _sources())
        assert client.started.wait(timeout=5.0)
        communicator.submit(_alert("alert2", priority=EventPriority.CRITICAL), _sources())
        assert communicator.snapshot().messages[0].alert_id == "alert2"
        assert len(client.calls) == 1
        client.release.set()
        assert communicator.wait_idle(timeout=5.0)
    finally:
        client.release.set()
        communicator.stop()


def test_recorded_mode_is_synchronous() -> None:
    client = FakeClient()
    communicator = _communicator(client, mode="recorded")
    communicator.submit(_alert(), _sources())
    snapshot = communicator.snapshot()
    assert snapshot.messages[0].source is MessageSource.LLM
    assert len(client.calls) == 1


def test_template_mode_never_calls_the_model() -> None:
    client = FakeClient()
    communicator = _communicator(client, mode="template")
    communicator.start()
    communicator.submit(_alert(), _sources())
    communicator.stop()
    snapshot = communicator.snapshot()
    assert snapshot.messages[0].source is MessageSource.TEMPLATE
    assert snapshot.counters["fallback:llm_disabled"] == 1
    assert "used" not in snapshot.counters
    assert client.calls == []


def test_model_down_keeps_emitting_templates() -> None:
    class _DownClient(FakeClient):
        def generate(
            self, request: LLMRequest, *, cancel: threading.Event | None = None
        ) -> LLMResponse:
            self.calls.append(request)
            raise ConnectionError("connection refused")

    client = _DownClient()
    communicator = _communicator(client)
    communicator.start()
    communicator.submit(_alert("alert1"), _sources())
    communicator.submit(_alert("alert2", priority=EventPriority.INFORMATIONAL), _sources())
    communicator.submit(_alert("alert3", priority=EventPriority.CRITICAL), _sources())
    assert communicator.wait_idle(timeout=5.0)
    communicator.stop()
    snapshot = communicator.snapshot()
    assert len(snapshot.messages) == 3
    assert all(message.source is MessageSource.TEMPLATE for message in snapshot.messages)


def test_observe_sends_only_new_alerts_and_survives_engine_reset() -> None:
    client = FakeClient()
    communicator = _communicator(client, mode="template")
    alert = _alert()
    events = EventsSnapshot(alerts=(alert,))
    communicator.observe(events, _sources())
    communicator.observe(events, _sources())
    assert len(communicator.snapshot().messages) == 1
    communicator.observe(EventsSnapshot(), _sources(11.0))
    communicator.observe(events, _sources(12.0))
    assert len(communicator.snapshot().messages) == 2
    assert client.calls == []


def test_no_speak_is_respected_for_non_critical_only() -> None:
    validator = FakeValidator(should_speak=False)
    communicator = _communicator(FakeClient(), validator=validator)
    communicator.start()
    communicator.submit(_alert("alert1"), _sources())
    assert communicator.wait_idle(timeout=5.0)
    communicator.submit(_alert("alert2", priority=EventPriority.CRITICAL), _sources())
    communicator.stop()
    snapshot = communicator.snapshot()
    assert [message.alert_id for message in snapshot.messages] == ["alert2"]
    assert snapshot.messages[0].source is MessageSource.TEMPLATE
    assert snapshot.counters["declined"] == 1


def test_context_is_absent_without_a_builder() -> None:
    communicator = AgentCommunicator(
        AgentConfig(),
        LLMConfig(enabled=True, model="fake-model"),
        client=FakeClient(),
        template_renderer=_renderer,
    )
    communicator.submit(_alert(), _sources())
    snapshot = communicator.snapshot()
    assert snapshot.messages[0].source is MessageSource.TEMPLATE
    assert snapshot.counters["fallback:unavailable"] == 1


def test_no_alerts_mean_no_model_calls() -> None:
    client = FakeClient()
    communicator = _communicator(client)
    communicator.observe(EventsSnapshot(), _sources())
    assert communicator.snapshot().messages == ()
    assert client.calls == []


def test_capture_session_exposes_the_injected_communicator() -> None:
    communicator = _communicator(mode="template")
    communicator.submit(_alert(), _sources())
    session = CaptureSession(AppConfig(), agent_communicator=communicator)
    assert session.agent_snapshot().messages == communicator.snapshot().messages


def test_capture_session_builds_live_agent_when_llm_is_enabled() -> None:
    session = CaptureSession(AppConfig(llm=LLMConfig(enabled=True, model="fake-model")))
    assert session._agent.mode == "live"


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


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(1, "Driver B", 2, 90.5, 40.0, 2.0, 2.3, compounds=("medium",)),
    )


class _OneAlertEngine:
    """Motor de eventos mínimo: publica siempre la misma alerta por paquete."""

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


def test_playback_template_messages_are_invariant_to_speed(
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

    results = []
    for speed in (0.0, 1.0, 4.0):
        session = PlaybackSession(path, speed=speed)
        session.play()
        assert session.wait(timeout=30.0)
        snapshot = session.agent_snapshot()
        results.append(snapshot.messages)

    assert results[0]
    assert results[0] == results[1] == results[2]
