"""Registro del agente en SQLite: migración v9, ida y vuelta e idempotencia."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from f1_strategist.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SessionInfo,
    SessionStore,
    StoredAgentMessage,
    StoredLLMCall,
    apply_migrations,
)


def _session(session_uid: int = 0xF125, **overrides: object) -> SessionInfo:
    values: dict[str, object] = {
        "session_uid": session_uid,
        "source": "live",
        "started_at_utc": "2026-11-05T10:00:00Z",
        "track": "monza",
        "session_type": "race",
    }
    values.update(overrides)
    return SessionInfo(**values)  # type: ignore[arg-type]


def _call(**overrides: object) -> StoredLLMCall:
    values: dict[str, object] = {
        "seq": 1,
        "t_sim_s": 12.5,
        "kind": "alert",
        "model": "local-model",
        "outcome": "used",
        "alert_id": "alert:1",
        "endpoint_host": "127.0.0.1",
        "prompt_version": "1",
        "parameters_json": '{"temperature": 0.3}',
        "context_hash": "c" * 64,
        "context_json": '{"schema_version": 1, "kind": "alert"}',
        "response_text": '{"message": "box"}',
        "reply_json": '{"message": "box", "priority": "strategic"}',
        "validation_valid": True,
        "validation_errors_json": "[]",
        "fallback_reason": None,
        "error": None,
        "ttft_s": 0.2,
        "total_s": 0.4,
        "prompt_tokens": 120,
        "completion_tokens": 18,
    }
    values.update(overrides)
    return StoredLLMCall(**values)  # type: ignore[arg-type]


def _message(**overrides: object) -> StoredAgentMessage:
    values: dict[str, object] = {
        "seq": 1,
        "t_sim_s": 12.5,
        "message_id": "msg-1",
        "priority": "strategic",
        "language": "es",
        "text": "Box this lap.",
        "source": "template",
        "alert_id": "alert:1",
        "event_id": "test:1",
        "used_facts_json": '["gap_s=1.2"]',
        "lap": 3,
        "llm_call_id": "call-1",
        "interrupt": False,
    }
    values.update(overrides)
    return StoredAgentMessage(**values)  # type: ignore[arg-type]


def test_v8_database_migrates_to_v9_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "v8.db"
    connection = sqlite3.connect(path)
    try:
        apply_migrations(connection, MIGRATIONS[:-1])
        connection.execute(
            "INSERT INTO sessions (session_uid, source, started_at_utc, fingerprint, "
            "gate_fingerprint) VALUES ('123', 'live', '2026-11-05T10:00:00Z', ?, ?)",
            ("f" * 64, "g" * 64),
        )
        connection.execute(
            "INSERT INTO detected_events (session_id, seq, t_sim_s, rule, event_id, phase, "
            "priority, category, message_key, facts_json) "
            "VALUES (1, 1, 5.0, 'test', 'test:1', 'opened', 'strategic', 'test', "
            "'test.event', '{}')"
        )
        connection.commit()
    finally:
        connection.close()

    with SessionStore.open(path) as store:
        session = store.get_session(1)
        assert session is not None
        assert session.session_uid == 123
        assert session.fingerprint == "f" * 64
        assert session.gate_fingerprint == "g" * 64
        assert session.agent_fingerprint is None
        assert len(list(store.iter_detected_events(1))) == 1
        assert list(store.iter_llm_calls(1)) == []
        assert list(store.iter_agent_messages(1)) == []
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_llm_calls_and_agent_messages_round_trip(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session(agent_fingerprint="d" * 64))
        store.add_llm_calls(session_id, [_call()])
        store.add_agent_messages(session_id, [_message()])

        assert list(store.iter_llm_calls(session_id)) == [_call()]
        assert list(store.iter_agent_messages(session_id)) == [_message()]
        session = store.get_session(session_id)
        assert session is not None
        assert session.agent_fingerprint == "d" * 64


def test_rewriting_the_same_sequence_updates_without_duplicating(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        store.add_llm_calls(session_id, [_call()])
        store.add_agent_messages(session_id, [_message()])
        store.add_llm_calls(session_id, [_call(response_text='{"message": "stay out"}')])
        store.add_agent_messages(session_id, [_message(text="Stay out.")])

        calls = list(store.iter_llm_calls(session_id))
        assert len(calls) == 1
        assert calls[0].response_text == '{"message": "stay out"}'
        messages = list(store.iter_agent_messages(session_id))
        assert len(messages) == 1
        assert messages[0].text == "Stay out."


def test_agent_tables_store_no_credentials(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        for table in ("sessions", "llm_calls", "agent_messages"):
            columns = {
                str(row[1]).lower()
                for row in store.connection.execute(f"PRAGMA table_info({table})")
            }
            assert columns
            assert not any(
                secret in column
                for column in columns
                for secret in ("key", "auth", "header", "secret")
            )
