"""Consultas del piloto en SQLite: migración v10, ida y vuelta y huella."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from f1_strategist.fingerprint import compute_pilot_fingerprint
from f1_strategist.metrics.types import Quality
from f1_strategist.pilot.types import (
    AnswerKind,
    FactKind,
    Intent,
    PilotAnswer,
    PilotExchange,
    PilotQuery,
    ToolFact,
    ToolResult,
)
from f1_strategist.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SessionInfo,
    SessionStore,
    StoredAgentMessage,
    StoredPilotQuery,
    apply_migrations,
)


def _session(**overrides: object) -> SessionInfo:
    values: dict[str, object] = {
        "session_uid": 0xF125,
        "source": "live",
        "started_at_utc": "2026-11-05T10:00:00Z",
        "track": "monza",
        "session_type": "race",
    }
    values.update(overrides)
    return SessionInfo(**values)  # type: ignore[arg-type]


def _query(**overrides: object) -> StoredPilotQuery:
    values: dict[str, object] = {
        "seq": 1,
        "t_sim_s": 12.5,
        "query_id": "q-1",
        "text": "¿Cuánto combustible queda?",
        "language": "es",
        "intent": "fuel_status",
        "confidence": 0.9,
        "entities_json": "[]",
        "facts_json": '[{"name":"fuel.in_tank_kg","value":12.4}]',
        "missing_json": "[]",
        "answer_text": "Quedan 12,4 kg (medido).",
        "answer_kind": "answer",
        "message_key": "pilot_fuel_status",
        "latency_s": 0.004,
        "followup_status": "none",
    }
    values.update(overrides)
    return StoredPilotQuery(**values)  # type: ignore[arg-type]


def _exchange(
    query_id: str = "q-1",
    *,
    text: str = "¿Cuánto combustible queda?",
    answer_text: str = "Quedan 12,4 kg (medido).",
    received_at: float = 10.0,
    latency_s: float | None = 0.004,
) -> PilotExchange:
    query = PilotQuery(
        query_id=query_id,
        text=text,
        language="es",
        received_at=received_at,
    )
    answer = PilotAnswer(
        query_id=query_id,
        intent=Intent.FUEL_STATUS,
        kind=AnswerKind.ANSWER,
        text=answer_text,
        message_key="pilot_fuel_status",
        latency_s=latency_s,
        tool_result=ToolResult(
            intent=Intent.FUEL_STATUS,
            quality=Quality.OK,
            facts=(
                ToolFact(
                    name="fuel.in_tank_kg",
                    value=12.4,
                    unit="kg",
                    kind=FactKind.MEASURED,
                    quality=Quality.OK,
                ),
            ),
            missing=("fuel.margin_laps_at_finish",),
            data_lap=5,
        ),
    )
    return PilotExchange(query=query, answer=answer)


def test_v9_database_migrates_to_v10_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "v9.db"
    connection = sqlite3.connect(path)
    try:
        apply_migrations(connection, MIGRATIONS[:-1])
        connection.execute(
            "INSERT INTO sessions (session_uid, source, started_at_utc, agent_fingerprint) "
            "VALUES ('123', 'live', '2026-11-05T10:00:00Z', ?)",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO agent_messages (session_id, seq, t_sim_s, message_id, priority, "
            "language, text, source) VALUES (1, 1, 5.0, 'msg-1', 'strategic', 'es', "
            "'Box this lap.', 'template')"
        )
        connection.commit()
    finally:
        connection.close()

    with SessionStore.open(path) as store:
        session = store.get_session(1)
        assert session is not None
        assert session.agent_fingerprint == "a" * 64
        assert session.pilot_fingerprint is None
        assert list(store.iter_pilot_queries(1)) == []
        messages = list(store.iter_agent_messages(1))
        assert len(messages) == 1
        assert messages[0].text == "Box this lap."
        assert messages[0].query_id is None
        assert messages[0].kind is None
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_pilot_queries_round_trip_and_rewrite_without_duplicates(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session(pilot_fingerprint="p" * 64))
        store.add_pilot_queries(session_id, [_query(), _query(seq=2, query_id="q-2")])
        store.add_pilot_queries(session_id, [_query(answer_text="Sin datos de combustible.")])

        rows = list(store.iter_pilot_queries(session_id))
        assert [row.seq for row in rows] == [1, 2]
        assert rows[0].answer_text == "Sin datos de combustible."
        assert rows[0].facts_json == '[{"name":"fuel.in_tank_kg","value":12.4}]'
        assert rows[0].missing_json == "[]"
        assert rows[0].confidence == 0.9
        session = store.get_session(session_id)
        assert session is not None
        assert session.pilot_fingerprint == "p" * 64


def test_pilot_queries_filter_by_intent(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        store.add_pilot_queries(
            session_id,
            [_query(), _query(seq=2, query_id="q-2", intent="tyre_life")],
        )
        filtered = store.iter_pilot_queries(session_id, intent="tyre_life")
        assert [row.query_id for row in filtered] == ["q-2"]


def test_agent_messages_link_query_id_and_kind(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        store.add_agent_messages(
            session_id,
            [
                StoredAgentMessage(
                    seq=1,
                    t_sim_s=2.0,
                    message_id="msg-1",
                    priority="strategic",
                    language="es",
                    text="Contexto",
                    source="llm",
                    query_id="q-1",
                    kind="followup",
                )
            ],
        )
        messages = list(store.iter_agent_messages(session_id))
        assert messages[0].query_id == "q-1"
        assert messages[0].kind == "followup"


def test_pilot_fingerprint_ignores_ids_times_and_latencies() -> None:
    first = _exchange("q-1", received_at=10.0, latency_s=0.001)
    second = _exchange("q-2", received_at=99.0, latency_s=2.5)
    assert compute_pilot_fingerprint((first,)) == compute_pilot_fingerprint((second,))
    assert compute_pilot_fingerprint((first,)) != compute_pilot_fingerprint(
        (_exchange(answer_text="Quedan 13 kg."),)
    )
    assert compute_pilot_fingerprint((first,)) != compute_pilot_fingerprint((first, first))


def test_readonly_store_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    with SessionStore.open(path) as store:
        session_id = store.create_session(_session())
    with SessionStore.open(path, readonly=True) as store:
        with pytest.raises(sqlite3.OperationalError):
            store.add_pilot_queries(session_id, [_query()])
        assert list(store.iter_pilot_queries(session_id)) == []


def test_readonly_v9_store_reads_agent_messages_without_query_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v9.db"
    connection = sqlite3.connect(path)
    try:
        apply_migrations(connection, MIGRATIONS[:-2])
        connection.execute(
            "INSERT INTO sessions (session_uid, source, started_at_utc, agent_fingerprint) "
            "VALUES ('123', 'live', '2026-11-05T10:00:00Z', ?)",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO agent_messages (session_id, seq, t_sim_s, message_id, priority, "
            "language, text, source) VALUES (1, 1, 5.0, 'msg-1', 'strategic', 'es', "
            "'Box this lap.', 'template')"
        )
        connection.commit()
    finally:
        connection.close()

    with SessionStore.open(path, readonly=True) as store:
        messages = list(store.iter_agent_messages(1))
        assert len(messages) == 1
        assert messages[0].text == "Box this lap."
        assert messages[0].query_id is None
        assert messages[0].kind is None
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 9


def test_readonly_v10_store_reads_pilot_queries_without_exchange_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v10.db"
    connection = sqlite3.connect(path)
    try:
        apply_migrations(connection, MIGRATIONS[:-1])
        connection.execute(
            "INSERT INTO sessions (session_uid, source, started_at_utc, agent_fingerprint) "
            "VALUES ('123', 'live', '2026-11-05T10:00:00Z', ?)",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO pilot_queries (session_id, seq, t_sim_s, query_id, language, intent, "
            "answer_text, answer_kind, message_key, followup_status) VALUES "
            "(1, 1, 12.5, 'q-1', 'es', 'fuel_status', 'Quedan 12,4 kg.', 'answer', "
            "'pilot_fuel_status', 'none')"
        )
        connection.commit()
    finally:
        connection.close()

    with SessionStore.open(path, readonly=True) as store:
        rows = list(store.iter_pilot_queries(1))
        assert len(rows) == 1
        assert rows[0].query_id == "q-1"
        assert rows[0].exchange_json is None
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 10
