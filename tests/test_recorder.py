"""Grabador de sesión en vivo e ingesta de capturas."""

from __future__ import annotations

import shutil
import struct
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.cli import main
from f1_strategist.config import AppConfig, CaptureConfig, RecordingConfig, TelemetryConfig
from f1_strategist.fingerprint import fingerprint_capture
from f1_strategist.messages import message
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.export import run_metrics_capture
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.packets import HEADER_SIZE, HEADER_STRUCT
from f1_strategist.parser import ParsedPacket
from f1_strategist.race_state import (
    CarSnapshot,
    RaceEvent,
    RaceSessionSnapshot,
    RaceStateSnapshot,
)
from f1_strategist.recorder import SessionRecorder, ingest_capture
from f1_strategist.session import CaptureSession
from f1_strategist.store import (
    MetricSample,
    Participant,
    SessionInfo,
    SessionStore,
    StoredError,
    StoredEvent,
)
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed


class _FakeStore:
    """Almacén mínimo en memoria que puede simular lentitud o fallos."""

    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.sessions: list[SessionInfo] = []
        self.finished: list[tuple[int, str, bool, str | None, str | None, str | None]] = []
        self.updated: list[tuple[int, SessionInfo]] = []
        self.participants: list[Participant] = []
        self.laps: list[LapRecord] = []
        self.events: list[StoredEvent] = []
        self.samples: list[MetricSample] = []
        self.errors: list[StoredError] = []
        self.capture_hash_updates: list[tuple[int, str]] = []
        self._next_id = 1

    @contextmanager
    def transaction(self) -> Iterator[object]:
        if self.fail:
            raise RuntimeError("store boom")
        if self.delay:
            time.sleep(self.delay)
        yield object()

    def create_session(self, info: SessionInfo) -> int:
        session_id = self._next_id
        self._next_id += 1
        self.sessions.append(info)
        return session_id

    def finish_session(
        self,
        session_id: int,
        *,
        ended_at_utc: str,
        incomplete: bool = False,
        fingerprint: str | None = None,
        final_metrics_json: str | None = None,
        capture_sha256: str | None = None,
    ) -> None:
        self.finished.append(
            (
                session_id,
                ended_at_utc,
                incomplete,
                fingerprint,
                final_metrics_json,
                capture_sha256,
            )
        )

    def update_session(self, session_id: int, info: SessionInfo) -> None:
        self.updated.append((session_id, info))

    def update_capture_sha256(self, session_id: int, sha256: str) -> None:
        self.capture_hash_updates.append((session_id, sha256))

    def add_participants(self, session_id: int, participants: Iterable[Participant]) -> None:
        self.participants.extend(participants)

    def upsert_laps(self, session_id: int, laps: Iterable[LapRecord]) -> None:
        self.laps.extend(laps)

    def add_events(self, session_id: int, events: Iterable[StoredEvent]) -> None:
        self.events.extend(events)

    def add_metric_samples(self, session_id: int, samples: Iterable[MetricSample]) -> None:
        self.samples.extend(samples)

    def add_errors(self, session_id: int, errors: Iterable[StoredError]) -> None:
        self.errors.extend(errors)


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(
            1,
            "Driver B",
            2,
            90.5,
            40.0,
            2.0,
            2.3,
            pit_laps=(2,),
            compounds=("soft", "medium"),
        ),
    )


def _capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, total_laps: int = 3) -> Path:
    packets = list(build_race_timed(_cars(), total_laps=total_laps, samples_per_lap=1))
    path = tmp_path / "race.f1cap"
    with CaptureWriter(path) as writer:
        for index, _ in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=index * 1_000_000,
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=make_packet(1, extra=struct.pack("<I", index)),
                )
            )

    parsed = [packet for packet, _ in packets]

    def fake_parse(payload: bytes) -> ParsedPacket:
        index = struct.unpack_from("<I", payload, HEADER_SIZE)[0]
        return parsed[index]

    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)
    return path


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


def _snapshot(
    uid: int = 1,
    *,
    lap: int = 1,
    events: tuple[RaceEvent, ...] = (),
    cars: tuple[CarSnapshot, ...] = (),
) -> RaceStateSnapshot:
    session = RaceSessionSnapshot(
        uid=uid,
        session_type="Race",
        track="Monza",
        total_laps=10,
        started=True,
        ended=False,
    )
    return RaceStateSnapshot(
        session=session,
        current_lap=lap,
        player_car_index=0,
        cars=cars,
        freshness=(),
        events=events,
    )


def _metrics(value: float = 10.0, *, computed_at: float = 1.0) -> MetricsSnapshot:
    metric = Metric(
        key="fuel.in_tank_kg",
        value=value,
        unit="kg",
        window=Window("instant"),
        quality=Quality.OK,
        sample_count=1,
        computed_at_monotonic=computed_at,
    )
    return MetricsSnapshot(
        metrics={metric.key: metric},
        computed_at_monotonic=computed_at,
        session_uid=1,
    )


def _history(*records: LapRecord) -> LapHistorySnapshot:
    grouped: dict[int, list[LapRecord]] = {}
    for record in records:
        grouped.setdefault(record.car_index, []).append(record)
    return LapHistorySnapshot(
        records={car_index: tuple(items) for car_index, items in grouped.items()},
        session_uid=1,
    )


def _session_id(recorder: SessionRecorder) -> int:
    session_id = recorder.session_id
    assert session_id is not None
    return session_id


def _wait_until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("La condición no se cumplió a tiempo")


def test_ingest_stores_the_same_laps_as_the_metrics_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    expected = run_metrics_capture(path)
    database = tmp_path / "sessions.db"

    with SessionStore.open(database) as store:
        result = ingest_capture(path, store, AppConfig())
        session = store.get_session(result.session_id)
        assert session is not None
        assert session.source == "ingest"
        assert session.ended_at_utc is not None
        assert session.fingerprint == result.fingerprint
        assert session.capture_sha256
        assert session.config_hash

        stored = {
            (lap.car_index, lap.lap_number): lap for lap in store.iter_laps(result.session_id)
        }
        expected_records = [
            record for records in expected.history.records.values() for record in records
        ]
        assert len(stored) == len(expected_records)
        for record in expected_records:
            assert stored[(record.car_index, record.lap_number)] == record

        events = list(store.iter_events(result.session_id))
        assert result.events == len(events)
        assert events
        assert [event.seq for event in events] == list(range(1, len(events) + 1))

    assert result.laps == len(expected_records)
    assert result.packets_read == result.packets_parsed
    assert result.offline is not None and not result.offline.truncated
    assert result.fingerprint == fingerprint_capture(path, AppConfig()).fingerprint


def test_ingesting_twice_is_idempotent_and_replace_does_not_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)

    with SessionStore.open(tmp_path / "sessions.db") as store:
        first = ingest_capture(path, store, AppConfig())
        second = ingest_capture(path, store, AppConfig())
        assert second.skipped
        assert second.session_id == first.session_id
        assert len(store.list_sessions()) == 1

        replaced = ingest_capture(path, store, AppConfig(), replace=True)
        assert not replaced.skipped
        assert replaced.session_id != first.session_id
        assert replaced.laps == first.laps
        assert replaced.events == first.events
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].id == replaced.session_id


def test_cli_ingest_reports_the_stored_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _capture(tmp_path, monkeypatch)
    database = tmp_path / "cli.db"

    assert main(["ingest", str(path), "--db", str(database)]) == 0
    output = capsys.readouterr().out
    assert "Sesión 1 ingerida" in output
    assert "Huella determinista:" in output

    assert main(["ingest", str(path), "--db", str(database)]) == 0
    assert "ya estaba ingerida" in capsys.readouterr().out


def test_observe_does_not_wait_for_a_slow_store() -> None:
    store = _FakeStore(delay=0.2)
    recorder = SessionRecorder(
        store,
        RecordingConfig(queue_size=1, batch_size=1, flush_interval_s=0.01),
    )
    try:
        for lap in range(1, 8):
            started = time.perf_counter()
            recorder.observe(
                float(lap),
                float(lap),
                _snapshot(lap=lap),
                _metrics(computed_at=float(lap)),
                _history(),
            )
            assert time.perf_counter() - started < 0.05

        assert recorder.dropped_batches > 0
        assert recorder.incomplete
    finally:
        recorder.stop()


def test_write_failure_is_reported_without_reaching_the_consumer() -> None:
    recorder = SessionRecorder(
        _FakeStore(fail=True),
        RecordingConfig(batch_size=1, flush_interval_s=0.01),
    )
    try:
        recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        _wait_until(lambda: recorder.error is not None)
        assert isinstance(recorder.error, RuntimeError)
        assert recorder.incomplete
    finally:
        recorder.stop()


def test_session_change_creates_a_second_row_and_closes_the_first(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(
            store,
            RecordingConfig(batch_size=1, flush_interval_s=0.01),
            config_json="{}",
        )
        try:
            recorder.observe(1.0, 1.0, _snapshot(uid=11), _metrics(computed_at=1.0), _history())
            recorder.observe(2.0, 2.0, _snapshot(uid=22), _metrics(computed_at=2.0), _history())
        finally:
            recorder.stop()

        sessions = store.list_sessions()
        assert [session.session_uid for session in sessions] == [11, 22]
        assert sessions[0].ended_at_utc is not None
        assert sessions[1].ended_at_utc is not None


def test_events_are_not_duplicated_across_observations(tmp_path: Path) -> None:
    event = RaceEvent(kind="flag", occurred_at_monotonic=5.0, value="yellow")
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            recorder.observe(1.0, 1.0, _snapshot(events=(event,)), _metrics(), _history())
            recorder.observe(
                1.0,
                2.0,
                _snapshot(events=(event,)),
                _metrics(computed_at=2.0),
                _history(),
            )
        finally:
            recorder.stop()

        events = list(store.iter_events(_session_id(recorder)))
        assert len(events) == 1
        assert events[0].kind == "flag"
        assert events[0].value == "yellow"
        assert events[0].seq == 1


def test_lap_updates_replace_the_existing_row(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            recorder.observe(
                1.0,
                1.0,
                _snapshot(),
                _metrics(),
                _history(LapRecord(car_index=0, lap_number=1, lap_time_s=90.0)),
            )
            recorder.observe(
                2.0,
                2.0,
                _snapshot(lap=2),
                _metrics(computed_at=2.0),
                _history(LapRecord(car_index=0, lap_number=1, lap_time_s=89.5)),
            )
        finally:
            recorder.stop()

        laps = list(store.iter_laps(_session_id(recorder)))
        assert len(laps) == 1
        assert laps[0].lap_number == 1
        assert laps[0].lap_time_s == 89.5


def test_metric_samples_respect_the_interval_and_lap_changes(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(
            store,
            RecordingConfig(batch_size=1, flush_interval_s=0.01, metrics_interval_s=1.0),
        )
        try:
            recorder.observe(
                1.0, 1.0, _snapshot(lap=1), _metrics(10.0, computed_at=1.0), _history()
            )
            recorder.observe(
                1.1, 1.1, _snapshot(lap=1), _metrics(10.0, computed_at=2.0), _history()
            )
            recorder.observe(3.0, 3.0, _snapshot(lap=1), _metrics(9.0, computed_at=3.0), _history())
            recorder.observe(3.1, 3.1, _snapshot(lap=2), _metrics(9.0, computed_at=4.0), _history())
        finally:
            recorder.stop()

        samples = list(store.iter_metric_samples(_session_id(recorder)))
        assert [sample.value_num for sample in samples] == [10.0, 9.0, 9.0, 9.0]
        assert [sample.lap for sample in samples] == [1, 1, 2, 2]
        assert all(sample.key == "fuel.in_tank_kg" for sample in samples)


def test_record_error_before_the_first_observation_is_stored(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            recorder.record_error(message("metrics_update_error", error="boom"))
            recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        finally:
            recorder.stop()

        errors = list(store.iter_errors(_session_id(recorder)))
        assert len(errors) == 1
        assert errors[0].code == "metrics_update_error"
        assert errors[0].params_json is not None
        assert "boom" in errors[0].params_json


def test_capture_session_writes_live_sessions_to_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    parsed = iter(packet for packet, _ in packets)
    monkeypatch.setattr("f1_strategist.session.parse_packet", lambda _: next(parsed))

    database = tmp_path / "live.db"
    session = CaptureSession(
        AppConfig(
            telemetry=TelemetryConfig(listen_port=0),
            capture=CaptureConfig(enabled=False),
            recording=RecordingConfig(
                enabled=True,
                database=database,
                batch_size=1,
                flush_interval_s=0.01,
            ),
        )
    )
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

    session._start_recorder()
    session._consume()
    session.stop()

    with SessionStore.open(database, readonly=True) as store:
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].source == "live"
        assert sessions[0].ended_at_utc is not None
        session_id = sessions[0].id
        assert session_id is not None
        assert sum(1 for _ in store.iter_laps(session_id)) > 0
        assert sum(1 for _ in store.iter_events(session_id)) > 0
        assert sum(1 for _ in store.iter_metric_samples(session_id)) > 0


class _FakeRecorder:
    """Grabador mínimo para probar la propagación de su estado."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.dropped_batches = 0
        self.recorded: list[object] = []

    def record_error(self, issue: object) -> None:
        self.recorded.append(issue)


def test_capture_session_propagates_recorder_state_only_once() -> None:
    session = CaptureSession(AppConfig(recording=RecordingConfig(enabled=False)))
    recorder = _FakeRecorder()
    session._recorder = recorder  # type: ignore[assignment]

    session._check_recorder_state()
    assert session.stats.snapshot().consumer_errors == 0

    recorder.error = RuntimeError("boom")
    session._check_recorder_state()
    snapshot = session.stats.snapshot()
    assert snapshot.consumer_errors == 1
    assert snapshot.consumer_error is not None
    assert snapshot.consumer_error.key == "recorder_error"

    session._check_recorder_state()
    assert session.stats.snapshot().consumer_errors == 1

    recorder.dropped_batches = 3
    session._check_recorder_state()
    snapshot = session.stats.snapshot()
    assert snapshot.consumer_errors == 2
    assert snapshot.consumer_error is not None
    assert snapshot.consumer_error.key == "recorder_dropped"
    assert snapshot.consumer_error.get("count") == 3

    session._check_recorder_state()
    assert session.stats.snapshot().consumer_errors == 2
    assert recorder.recorded == []


def test_ingest_does_not_depend_on_the_capture_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    copy = tmp_path / "copy.f1cap"
    shutil.copyfile(path, copy)

    with SessionStore.open(tmp_path / "sessions.db") as store:
        first = ingest_capture(path, store, AppConfig())
        second = ingest_capture(copy, store, AppConfig())
        assert second.skipped
        assert second.session_id == first.session_id
        assert len(store.list_sessions()) == 1


def test_late_metadata_is_merged_into_the_session(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            initial = _snapshot()
            empty_session = replace(
                initial.session,
                track=None,
                session_type=None,
                total_laps=None,
            )
            recorder.observe(
                1.0,
                1.0,
                replace(initial, session=empty_session),
                _metrics(),
                _history(),
            )
            recorder.observe(
                2.0,
                2.0,
                _snapshot(lap=2),
                _metrics(9.0, computed_at=2.0),
                _history(),
            )
        finally:
            recorder.stop()

        session = store.get_session(_session_id(recorder))
        assert session is not None
        assert session.track == "Monza"
        assert session.session_type == "Race"
        assert session.total_laps == 10


def test_session_links_are_stored_and_grouped_by_game_identifiers(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            initial = _snapshot()
            linked_session = replace(
                initial.session,
                season_link_id=7,
                weekend_link_id=42,
                session_link_id=3,
                weekend_structure=("practice_1", "race"),
            )
            recorder.observe(
                1.0,
                1.0,
                replace(initial, session=linked_session),
                _metrics(),
                _history(),
            )
        finally:
            recorder.stop()

        session = store.get_session(_session_id(recorder))
        assert session is not None
        assert session.season_link_id == 7
        assert session.weekend_link_id == 42
        assert session.session_link_id == 3
        assert session.weekend_structure_json == '["practice_1","race"]'
        assert session.weekend_id is not None

        weekend = store.get_weekend(session.weekend_id)
        assert weekend is not None
        assert weekend.source == "game_ids"
        assert weekend.track == "Monza"
        assert weekend.weekend_link_id == 42
        assert weekend.season_link_id == 7


def test_metric_samples_keep_capture_and_session_clocks(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            recorder.observe(5.0, 100.0, _snapshot(), _metrics(), _history())
        finally:
            recorder.stop()

        samples = list(store.iter_metric_samples(_session_id(recorder)))
        assert samples
        assert samples[0].t_sim_s == pytest.approx(100.0)
        assert samples[0].t_session_s == pytest.approx(5.0)


def test_identical_consecutive_events_are_preserved(tmp_path: Path) -> None:
    event = RaceEvent(kind="overtake", occurred_at_monotonic=1.0, car_index=0, lap_number=1)
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            state = _snapshot(events=(event, event))
            recorder.observe(1.0, 1.0, state, _metrics(), _history())
            recorder.observe(1.0, 1.0, state, _metrics(), _history())
        finally:
            recorder.stop()

        events = list(store.iter_events(_session_id(recorder)))
        assert len(events) == 2
        assert [stored.seq for stored in events] == [1, 2]


def test_truncated_capture_marks_the_session_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    data = path.read_bytes()
    path.write_bytes(data[:-3])

    with SessionStore.open(tmp_path / "sessions.db") as store:
        result = ingest_capture(path, store, AppConfig())
        session = store.get_session(result.session_id)

    assert result.offline is not None and result.offline.truncated
    assert session is not None and session.incomplete


def test_dropped_batches_mark_only_their_own_session_incomplete() -> None:
    store = _FakeStore(delay=0.2)
    recorder = SessionRecorder(
        store,
        RecordingConfig(queue_size=1, batch_size=1, flush_interval_s=0.01),
    )
    try:
        for lap in range(1, 7):
            recorder.observe(
                float(lap),
                float(lap),
                _snapshot(uid=11, lap=lap),
                _metrics(10.0 - lap, computed_at=float(lap)),
                _history(),
            )
        time.sleep(0.3)
        recorder.observe(
            30.0, 30.0, _snapshot(uid=22, lap=1), _metrics(computed_at=30.0), _history()
        )
    finally:
        recorder.stop()

    assert recorder.dropped_batches > 0
    assert [info.session_uid for info in store.sessions] == [11, 22]
    assert [entry[2] for entry in store.finished] == [True, False]


def test_replace_keeps_the_old_session_when_the_new_ingest_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)

    with SessionStore.open(tmp_path / "sessions.db") as store:
        first = ingest_capture(path, store, AppConfig())

        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("fallo simulado")

        monkeypatch.setattr("f1_strategist.recorder.run_capture_offline", boom)
        with pytest.raises(RuntimeError, match="fallo simulado"):
            ingest_capture(path, store, AppConfig(), replace=True)

        sessions = store.list_sessions()
        assert [session.id for session in sessions] == [first.session_id]


def test_incomplete_sessions_are_ingested_again_instead_of_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)

    with SessionStore.open(tmp_path / "sessions.db") as store:
        first = ingest_capture(path, store, AppConfig())
        store.connection.execute(
            "UPDATE sessions SET fingerprint = NULL WHERE id = ?", (first.session_id,)
        )
        store.connection.commit()

        second = ingest_capture(path, store, AppConfig())
        assert not second.skipped
        assert second.session_id != first.session_id


class _SlowStore:
    """Envuelve un almacén real y retrasa cada transacción."""

    def __init__(self, inner: SessionStore, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @contextmanager
    def transaction(self) -> Iterator[object]:
        time.sleep(self._delay)
        with self._inner.transaction() as connection:
            yield connection


def test_block_when_full_avoids_dropped_batches() -> None:
    store = _FakeStore(delay=0.05)
    recorder = SessionRecorder(
        store,
        RecordingConfig(queue_size=1, batch_size=1, flush_interval_s=0.01),
        block_when_full=True,
    )
    try:
        for lap in range(1, 7):
            recorder.observe(
                float(lap),
                float(lap),
                _snapshot(lap=lap),
                _metrics(10.0 - lap, computed_at=float(lap)),
                _history(),
            )
    finally:
        recorder.stop()

    assert recorder.dropped_batches == 0
    assert [entry[2] for entry in store.finished] == [False]


def test_ingest_waits_for_the_writer_instead_of_dropping_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    config = AppConfig(recording=RecordingConfig(queue_size=1, batch_size=1, flush_interval_s=0.01))

    with SessionStore.open(tmp_path / "sessions.db") as store:
        slow = _SlowStore(store, 0.005)
        result = ingest_capture(path, slow, config)  # type: ignore[arg-type]
        session = store.get_session(result.session_id)

    assert session is not None
    assert not session.incomplete
