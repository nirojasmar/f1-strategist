from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import f1_strategist.cli as cli_module
from f1_strategist.agent.types import AgentMessage, MessageSource
from f1_strategist.capture import CaptureWriter
from f1_strategist.config import (
    AppConfig,
    CaptureConfig,
    ConfigError,
    PilotConfig,
    TelemetryConfig,
    load_config,
)
from f1_strategist.events.types import EventPriority
from f1_strategist.metrics.types import Quality
from f1_strategist.pilot import TOOLS
from f1_strategist.pilot.types import (
    AnswerKind,
    Clarification,
    DriverCandidate,
    EntityRef,
    FactKind,
    Intent,
    PilotAnswer,
    PilotExchange,
    PilotQuery,
    PilotSnapshot,
    RoutedQuery,
    ToolFact,
    ToolResult,
)
from f1_strategist.playback import PlaybackSession
from f1_strategist.session import CaptureSession


def _query() -> PilotQuery:
    return PilotQuery(
        query_id="q-1",
        text="¿Cómo voy respecto a Hamilton?",
        language="es",
        received_at=12.5,
        source="console",
    )


def _result() -> ToolResult:
    return ToolResult(
        intent=Intent.COMPARE_DRIVER,
        quality=Quality.OK,
        facts=(
            ToolFact(
                name="gap_s",
                value=1.8,
                unit="s",
                kind=FactKind.MEASURED,
                quality=Quality.OK,
            ),
            ToolFact(
                name="pace_delta_s_per_lap",
                value=-0.2,
                unit="s",
                kind=FactKind.ESTIMATED,
                quality=Quality.LOW,
            ),
        ),
        missing=("traffic",),
        data_lap=12,
        notes=("el rival ha parado",),
    )


def _answer() -> PilotAnswer:
    return PilotAnswer(
        query_id="q-1",
        intent=Intent.COMPARE_DRIVER,
        kind=AnswerKind.ANSWER,
        text="Hamilton va 1,8 s por delante.",
        message_key="pilot.compare_driver",
        params={"driver": "Hamilton", "gap_s": 1.8},
        tool_result=_result(),
        latency_s=0.004,
    )


def test_json_round_trip() -> None:
    query = _query()
    assert PilotQuery.from_json(query.to_json()) == query

    driver = DriverCandidate(car_index=0, name="Lewis Hamilton", race_number=44, position=3)
    entity = EntityRef(raw="Hamilton", car_index=0, candidates=(driver,))
    clarification = Clarification(
        message_key="pilot.clarify_driver",
        options=(driver,),
        expires_at=42.0,
    )
    routed = RoutedQuery(
        query=query,
        intent=Intent.COMPARE_DRIVER,
        confidence=0.9,
        entities=(entity,),
        clarification=clarification,
        normalized_text="como voy respecto a hamilton",
    )
    assert RoutedQuery.from_json(routed.to_json()) == routed

    answer = _answer()
    assert PilotAnswer.from_json(answer.to_json()) == answer
    restored = PilotAnswer.from_json(answer.to_json())
    assert restored.tool_result is not None
    assert restored.tool_result.facts[0].kind is FactKind.MEASURED

    snapshot = PilotSnapshot(
        recent=(PilotExchange(query=query, answer=answer),),
        pending_clarification=clarification,
        counters={"answers": 1},
    )
    assert PilotSnapshot.from_json(snapshot.to_json()) == snapshot


def test_json_is_stable_and_sorted() -> None:
    first = _answer().to_json()
    second = _answer().to_json()
    assert first == second
    assert first.startswith('{"followup_pending":')


def test_types_are_immutable() -> None:
    query = _query()
    with pytest.raises(FrozenInstanceError):
        query.text = "otra"  # type: ignore[misc]

    answer = _answer()
    with pytest.raises(TypeError):
        answer.params["driver"] = "Verstappen"  # type: ignore[index]

    assert set(TOOLS) == set(Intent) - {Intent.UNKNOWN}


def test_tool_fact_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        ToolFact(
            name="gap_s",
            value=float("nan"),
            unit="s",
            kind=FactKind.MEASURED,
            quality=Quality.OK,
        )
    with pytest.raises(ValueError):
        ToolFact(
            name="gap_s",
            value=(1, 2),  # type: ignore[arg-type]
            unit="s",
            kind=FactKind.MEASURED,
            quality=Quality.OK,
        )


def test_capture_session_pilot_without_collaborators_is_unavailable() -> None:
    session = CaptureSession(
        AppConfig(telemetry=TelemetryConfig(listen_port=0), capture=CaptureConfig(enabled=False))
    )
    answer = session.ask_pilot("¿Cuánto combustible queda?")
    assert answer.kind is AnswerKind.UNAVAILABLE
    assert answer.message_key == "pilot.answer.fuel_status.no_data"
    assert answer.text.startswith("No tengo datos de")
    snapshot = session.pilot_snapshot()
    assert len(snapshot.recent) == 1
    assert snapshot.counters == {"unavailable": 1}


def test_playback_session_pilot_without_collaborators_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "empty.f1cap"
    with CaptureWriter(path, include_created_utc=False):
        pass
    session = PlaybackSession(path)
    assert session.ask_pilot("¿Cómo voy?").kind is AnswerKind.UNAVAILABLE
    snapshot = session.pilot_snapshot()
    assert len(snapshot.recent) == 1
    assert snapshot.counters == {"unavailable": 1}


def test_agent_message_serialization_is_unchanged_without_query_fields() -> None:
    message = AgentMessage(
        message_id="msg-1",
        alert_id=None,
        event_id=None,
        priority=EventPriority.INFORMATIONAL,
        language="es",
        text="Todo bajo control.",
        source=MessageSource.TEMPLATE,
    )
    payload = message.to_dict()
    assert "query_id" not in payload
    assert "kind" not in payload
    assert AgentMessage.from_dict(payload) == message

    with_query = AgentMessage(
        message_id="msg-2",
        alert_id=None,
        event_id=None,
        priority=EventPriority.INFORMATIONAL,
        language="es",
        text="Detalle.",
        source=MessageSource.TEMPLATE,
        query_id="q-1",
        kind=AnswerKind.FOLLOWUP.value,
    )
    assert with_query.to_dict()["query_id"] == "q-1"
    assert with_query.to_dict()["kind"] == "followup"
    assert AgentMessage.from_json(with_query.to_json()) == with_query


def test_pilot_config_defaults_and_validation() -> None:
    cfg = PilotConfig()
    assert cfg.enabled
    assert cfg.min_intent_confidence == 0.6
    assert cfg.llm_followup_intents == ("ask_strategy", "pace_diagnosis", "pit_undercut")
    assert AppConfig().pilot == cfg

    with pytest.raises(ValueError):
        PilotConfig(min_intent_confidence=1.5)
    with pytest.raises(ValueError):
        PilotConfig(dialogue_turns=-1)
    with pytest.raises(ValueError):
        PilotConfig(llm_followup_intents=("inventada",))


def test_invalid_pilot_config_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[pilot]\nmin_intent_confidence = 2.0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_cli_registers_ask_and_queries_groups() -> None:
    parser = cli_module._build_parser()
    assert parser.parse_args(["ask"]).command == "ask"
    assert parser.parse_args(["queries"]).command == "queries"
