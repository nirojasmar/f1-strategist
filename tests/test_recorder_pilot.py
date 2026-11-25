"""Grabación de consultas del piloto: persistencia, huella y cola llena."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path

from f1_strategist.config import RecordingConfig
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
from f1_strategist.recorder import SessionRecorder
from f1_strategist.store import SessionStore, StoredPilotQuery

from .test_recorder import _BlockingStore, _history, _metrics, _session_id, _snapshot


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
