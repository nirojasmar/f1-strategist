from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import f1_strategist.cli as cli_module
from f1_strategist.agent.types import (
    AgentMessage,
    AgentSnapshot,
    CallOutcome,
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
from f1_strategist.capture import CaptureWriter
from f1_strategist.config import AppConfig, CaptureConfig, TelemetryConfig
from f1_strategist.events.types import EventPriority
from f1_strategist.playback import PlaybackSession
from f1_strategist.session import CaptureSession


def _payload(**overrides: object) -> ContextPayload:
    values: dict[str, object] = {
        "kind": ContextKind.ALERT,
        "language": "es",
        "alert": {"message_key": "events.tyre_wear", "priority": "critical", "lap": 12},
        "situation": {"lap": 12, "position": 3, "compound": "MEDIUM"},
        "strategy": {"action": "pit", "lap": 13, "confidence": 0.7},
        "metrics": {"fuel_delta_kg": -0.4},
        "uncertainties": ("low:fresh_tyre_pace",),
        "recent_messages": ("Cuidado con el graining.",),
        "glossary": {"graining": "pérdida de agarre"},
        "constraints": {"max_words": 30},
    }
    values.update(overrides)
    return ContextPayload(**values)  # type: ignore[arg-type]


def test_context_payload_json_round_trip() -> None:
    payload = _payload()
    restored = ContextPayload.from_json(payload.to_json())
    assert restored == payload
    assert restored.context_hash == payload.context_hash
    assert restored.situation["position"] == 3


def test_context_hash_is_stable_and_content_based() -> None:
    first = _payload()
    second = ContextPayload(
        kind=ContextKind.ALERT,
        language="es",
        alert={"lap": 12, "priority": "critical", "message_key": "events.tyre_wear"},
        situation={"compound": "MEDIUM", "position": 3, "lap": 12},
        strategy={"confidence": 0.7, "lap": 13, "action": "pit"},
        metrics={"fuel_delta_kg": -0.4},
        uncertainties=("low:fresh_tyre_pace",),
        recent_messages=("Cuidado con el graining.",),
        glossary={"graining": "pérdida de agarre"},
        constraints={"max_words": 30},
    )
    assert first.context_hash == second.context_hash
    assert len(first.context_hash) == 64
    changed = _payload(language="en")
    assert changed.context_hash != first.context_hash


def test_context_payload_is_immutable() -> None:
    payload = _payload()
    with pytest.raises(FrozenInstanceError):
        payload.language = "en"  # type: ignore[misc]
    with pytest.raises(TypeError):
        payload.situation["lap"] = 13  # type: ignore[index]
    nested = ContextPayload(
        kind=ContextKind.QUESTION,
        language="es",
        situation={"strategy": {"assumptions": ["sin Safety Car"]}},
    )
    with pytest.raises(TypeError):
        nested.situation["strategy"]["assumptions"] = ()  # type: ignore[index]


def test_context_payload_rejects_a_mismatched_hash() -> None:
    with pytest.raises(ValueError):
        _payload(context_hash="0" * 64)


def test_llm_reply_rejects_unknown_fields() -> None:
    reply = LLMReply.from_json(
        '{"should_speak": true, "priority": "strategic", "language": "es",'
        ' "message": "Boxea esta vuelta.", "reason": "desgaste", "used_facts": ["tyre_wear"]}'
    )
    assert reply.message == "Boxea esta vuelta."
    assert reply.priority is EventPriority.STRATEGIC
    assert LLMReply.from_json(reply.to_json()) == reply
    with pytest.raises(ValueError):
        LLMReply.from_json('{"should_speak": true, "extra": 1}')


def test_request_and_response_round_trip() -> None:
    request = LLMRequest(
        model="local-model",
        messages=(LLMMessage("system", "Responde en español."), LLMMessage("user", "{}")),
        max_tokens=120,
        stream=True,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    assert LLMRequest.from_json(request.to_json()) == request

    response = LLMResponse(
        text="Mantén el ritmo.",
        finish_reason="stop",
        prompt_tokens=80,
        completion_tokens=12,
        ttft_s=0.2,
        total_s=0.9,
        model_id="local-model",
        structured_mode="json_schema",
    )
    assert LLMResponse.from_json(response.to_json()) == response


def test_message_and_snapshot_round_trip() -> None:
    message = AgentMessage(
        message_id="msg-1",
        alert_id="alert-1",
        event_id="event-1",
        priority=EventPriority.STRATEGIC,
        language="es",
        text="Boxea esta vuelta.",
        source=MessageSource.LLM,
        used_facts=("tyre_wear",),
        lap=12,
        emitted_at=10.5,
        latency_s=0.8,
        llm_call_id="call-1",
    )
    assert AgentMessage.from_json(message.to_json()) == message

    snapshot = AgentSnapshot(
        messages=(message,),
        llm_status=LLMHealth(status=LLMStatus.AVAILABLE, model="local-model", latency_s=0.2),
        counters={CallOutcome.USED.value: 1},
        last_latency_s=0.8,
    )
    restored = AgentSnapshot.from_json(snapshot.to_json())
    assert restored == snapshot
    assert restored.llm_status.status is LLMStatus.AVAILABLE


def test_validation_result_round_trip() -> None:
    reply = LLMReply(priority=EventPriority.INFORMATIONAL, language="es", message="Ok.")
    result = ValidationResult(valid=False, errors=("too_many_words",), reply=reply)
    restored = ValidationResult.from_json(result.to_json())
    assert restored == result
    assert restored.reply is not None


def test_capture_session_agent_snapshot_stub_is_disabled() -> None:
    session = CaptureSession(
        AppConfig(telemetry=TelemetryConfig(listen_port=0), capture=CaptureConfig(enabled=False))
    )
    snapshot = session.agent_snapshot()
    assert snapshot == AgentSnapshot()
    assert snapshot.llm_status.status is LLMStatus.DISABLED


def test_playback_session_agent_snapshot_stub_is_disabled(tmp_path: Path) -> None:
    path = tmp_path / "empty.f1cap"
    with CaptureWriter(path, include_created_utc=False):
        pass
    session = PlaybackSession(path)
    snapshot = session.agent_snapshot()
    assert snapshot.messages == ()
    assert snapshot.llm_status.status is LLMStatus.DISABLED


def test_cli_registers_agent_and_llm_commands() -> None:
    parser = cli_module._build_parser()
    assert parser.parse_args(["agent", "show", "--session", "7"]).agent_command == "show"
    assert parser.parse_args(["llm", "check"]).llm_command == "check"
    assert parser.parse_args(["llm", "bench"]).llm_command == "bench"
