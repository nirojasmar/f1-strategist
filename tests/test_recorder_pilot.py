"""Grabación de consultas del piloto: persistencia, huella y cola llena."""

from __future__ import annotations

import struct
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.config import AppConfig, RecordingConfig
from f1_strategist.fingerprint import PilotFingerprintAccumulator, compute_pilot_fingerprint
from f1_strategist.metrics.types import Quality
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import ParsedPacket
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
    RoutedQuery,
    ToolFact,
    ToolResult,
)
from f1_strategist.recorder import SessionRecorder, ingest_capture
from f1_strategist.store import SessionStore, StoredPilotQuery
from tests.conftest import make_packet
from tests.scenarios import build_weekend_timed

from .test_recorder import (
    _agent_message,
    _BlockingStore,
    _capture,
    _cars,
    _FakeStore,
    _history,
    _metrics,
    _session_id,
    _snapshot,
)


def _exchange(
    query_id: str,
    *,
    intent: Intent = Intent.FUEL_STATUS,
    answer_text: str = "Quedan 12,4 kg (medido).",
    followup_pending: bool = False,
) -> PilotExchange:
    query = PilotQuery(
        query_id=query_id,
        text="¿Cuánto combustible queda?",
        language="es",
        received_at=10.0,
    )
    answer = PilotAnswer(
        query_id=query_id,
        intent=intent,
        kind=AnswerKind.ANSWER,
        text=answer_text,
        message_key="pilot_fuel_status",
        latency_s=0.004,
        followup_pending=followup_pending,
        tool_result=ToolResult(
            intent=intent,
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
        ),
    )
    return PilotExchange(query=query, answer=answer)


class _PilotBlockingStore(_BlockingStore):
    def __init__(self) -> None:
        super().__init__()
        self.pilot_queries: list[tuple[int, StoredPilotQuery]] = []
        self.pilot_written = threading.Event()

    def add_pilot_queries(self, session_id: int, queries: Iterable[StoredPilotQuery]) -> None:
        self.pilot_queries.extend((session_id, item) for item in queries)
        self.pilot_written.set()


def test_record_pilot_persists_queries_facts_and_fingerprint(tmp_path: Path) -> None:
    exchanges = (
        _exchange("q-1"),
        _exchange("q-2", intent=Intent.ASK_STRATEGY, followup_pending=True),
    )
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        recorder.record_pilot(exchanges, 2.0)
        recorder.stop()
        session_id = _session_id(recorder)

        rows = list(store.iter_pilot_queries(session_id))
        assert [row.seq for row in rows] == [1, 2]
        assert [row.query_id for row in rows] == ["q-1", "q-2"]
        assert rows[0].text == "¿Cuánto combustible queda?"
        assert rows[0].intent == "fuel_status"
        assert rows[0].answer_kind == "answer"
        assert rows[0].message_key == "pilot_fuel_status"
        assert rows[0].latency_s == 0.004
        assert rows[0].facts_json is not None and "fuel.in_tank_kg" in rows[0].facts_json
        assert rows[0].missing_json == '["fuel.margin_laps_at_finish"]'
        assert rows[0].followup_status == "none"
        assert rows[1].followup_status == "pending"

        session = store.get_session(session_id)
        assert session is not None
        assert session.pilot_fingerprint == compute_pilot_fingerprint(exchanges)


def test_record_pilot_omits_text_when_record_text_is_disabled(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(
            store,
            RecordingConfig(batch_size=1, flush_interval_s=0.01),
            pilot_record_text=False,
        )
        recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        recorder.record_pilot((_exchange("q-1"),), 2.0)
        recorder.stop()
        rows = list(store.iter_pilot_queries(_session_id(recorder)))
        assert len(rows) == 1
        assert rows[0].text is None


def test_record_pilot_persists_routed_confidence_entities_and_clarification(
    tmp_path: Path,
) -> None:
    exchange = _exchange("q-routed")
    routed = RoutedQuery(
        query=exchange.query,
        intent=exchange.answer.intent,
        confidence=0.82,
        entities=(EntityRef("Peña", car_index=7),),
        clarification=Clarification(
            message_key="pilot_clarify_driver",
            options=(DriverCandidate(7, "Peña"),),
            expires_at=20.0,
        ),
        normalized_text="cuanto combustible queda",
    )
    exchange = PilotExchange(query=exchange.query, answer=exchange.answer, routed=routed)

    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        recorder.record_pilot((exchange,), 2.0)
        recorder.stop()

        row = next(iter(store.iter_pilot_queries(_session_id(recorder))))
        assert row.confidence == 0.82
        assert row.entities_json is not None
        assert '"raw":"Peña"' in row.entities_json
        assert row.clarification_json is not None
        assert '"message_key":"pilot_clarify_driver"' in row.clarification_json


def test_record_pilot_drops_without_advancing_sequence_when_queue_is_full() -> None:
    store = _PilotBlockingStore()
    recorder = SessionRecorder(
        store, RecordingConfig(queue_size=1, batch_size=1, flush_interval_s=0.01)
    )
    try:
        recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        assert store.transaction_entered.wait(timeout=3.0)
        recorder.record_pilot((_exchange("q-1"),), 1.0)
        recorder.record_pilot((_exchange("q-2"),), 2.0)
        assert recorder.dropped_batches > 0
        assert recorder.incomplete
        store.release_transaction.set()
        assert store.pilot_written.wait(timeout=3.0)
        recorder.record_pilot((_exchange("q-3"),), 3.0)
    finally:
        store.release_transaction.set()
        recorder.stop()

    assert [row.seq for _, row in store.pilot_queries] == [1, 2]
    assert [row.query_id for _, row in store.pilot_queries] == ["q-1", "q-3"]


def test_record_pilot_discards_state_when_session_changes_while_enqueuing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = SessionRecorder(_FakeStore(), RecordingConfig(batch_size=1, flush_interval_s=0.01))
    try:
        recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        enqueue = recorder._enqueue

        def enqueue_then_switch(batch: Any) -> bool:
            enqueued = enqueue(batch)
            # La sesión cambia mientras el lote espera en la cola.
            recorder._reset_session(2)
            return enqueued

        monkeypatch.setattr(recorder, "_enqueue", enqueue_then_switch)
        recorder.record_pilot((_exchange("q-1"),), 2.0)

        assert recorder._pilot_seq == 0
        assert recorder._pilot_fingerprint.fingerprint == (
            PilotFingerprintAccumulator().fingerprint
        )
        assert recorder._producer_info is None
    finally:
        recorder.stop()


def test_record_agent_persists_query_link_and_kind(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        recorder.record_agent((), (_agent_message(query_id="q-1", kind="followup"),), 2.0)
        recorder.stop()

        row = next(iter(store.iter_agent_messages(_session_id(recorder))))
        assert row.query_id == "q-1"
        assert row.kind == "followup"


def test_appending_pilots_without_payload_invalidates_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    config = AppConfig().model_copy(
        update={"pilot": AppConfig().pilot.model_copy(update={"record_text": False})}
    )
    with SessionStore.open(tmp_path / "sessions.db") as store:
        first = ingest_capture(path, store, config, pilot_exchanges=((2.0, _exchange("q-1")),))
        session = store.get_session(first.session_id)
        assert session is not None
        assert session.pilot_fingerprint is not None

        # Sin texto no hay payload y la huella ya no puede recomponerse.
        second = ingest_capture(path, store, config, pilot_exchanges=((3.0, _exchange("q-2")),))

        assert second.skipped
        session = store.get_session(first.session_id)
        assert session is not None
        assert session.id is not None
        assert [row.query_id for row in store.iter_pilot_queries(session.id)] == ["q-1", "q-2"]
        assert session.pilot_fingerprint is None


def test_appending_pilots_with_payload_recomputes_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    with SessionStore.open(tmp_path / "sessions.db") as store:
        first = ingest_capture(path, store, AppConfig(), pilot_exchanges=((2.0, _exchange("q-1")),))
        second = ingest_capture(
            path, store, AppConfig(), pilot_exchanges=((3.0, _exchange("q-2")),)
        )

        assert second.skipped
        session = store.get_session(first.session_id)
        assert session is not None
        assert session.pilot_fingerprint is not None


def _weekend_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    packets = [
        packet for packet, _ in build_weekend_timed(_cars(), total_laps=3, samples_per_lap=1)
    ]
    path = tmp_path / "weekend.f1cap"
    with CaptureWriter(path) as writer:
        for index, _ in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=index * 1_000_000_000,
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=make_packet(1, extra=struct.pack("<I", index)),
                )
            )

    def fake_parse(payload: bytes) -> ParsedPacket:
        return packets[struct.unpack_from("<I", payload, HEADER_SIZE)[0]]

    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)
    return path


def test_ingest_capture_records_each_pilot_exchange_in_its_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _weekend_capture(tmp_path, monkeypatch)
    exchanges = (
        (20.0, _exchange("q-practice")),
        (40.0, _exchange("q-qualifying")),
        (60.0, _exchange("q-race")),
    )

    with SessionStore.open(tmp_path / "sessions.db") as store:
        ingest_capture(path, store, AppConfig(), pilot_exchanges=exchanges)
        sessions = {session.session_uid: session for session in store.list_sessions()}
        expected = {
            0xF12501: ["q-practice"],
            0xF12502: ["q-qualifying"],
            0xF12503: ["q-race"],
        }

        assert set(sessions) == set(expected)
        for uid, query_ids in expected.items():
            session = sessions[uid]
            assert session.id is not None
            assert [row.query_id for row in store.iter_pilot_queries(session.id)] == query_ids
