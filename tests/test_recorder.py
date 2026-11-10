"""Grabador de sesión en vivo e ingesta de capturas."""

from __future__ import annotations

import json
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
from f1_strategist.fingerprint import file_sha256, fingerprint_capture
from f1_strategist.messages import message
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.export import run_metrics_capture
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, TyreStint
from f1_strategist.metrics.stint_summary import StintSummary
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.packets import HEADER_SIZE, HEADER_STRUCT
from f1_strategist.parser import ParsedPacket
from f1_strategist.race_state import (
    CarSnapshot,
    RaceEvent,
    RaceSessionSnapshot,
    RaceStateSnapshot,
    TyreSetSnapshot,
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
    TyreInventorySnapshot,
)
from f1_strategist.weekend_profile import WeekendProfile
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed, build_weekend_timed


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
        self.stint_summaries: list[tuple[int, StintSummary]] = []
        self.tyre_inventory: list[tuple[int, TyreInventorySnapshot]] = []
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

    def add_stint_summaries(self, session_id: int, summaries: Iterable[StintSummary]) -> None:
        self.stint_summaries.extend((session_id, summary) for summary in summaries)

    def add_tyre_inventory(
        self, session_id: int, snapshots: Iterable[TyreInventorySnapshot]
    ) -> None:
        self.tyre_inventory.extend((session_id, snapshot) for snapshot in snapshots)


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
    session_type: str = "Race",
    tyre_inventory: tuple[TyreSetSnapshot, ...] = (),
) -> RaceStateSnapshot:
    session = RaceSessionSnapshot(
        uid=uid,
        session_type=session_type,
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
        tyre_inventory=tyre_inventory,
        tyre_inventory_updated_at_monotonic=1.0 if tyre_inventory else None,
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


def test_ingest_stores_stint_summaries_and_inventory_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)

    with SessionStore.open(tmp_path / "sessions.db") as store:
        result = ingest_capture(path, store, AppConfig())
        summaries = list(store.iter_stint_summaries(result.session_id))
        inventory = list(store.iter_tyre_inventory(result.session_id))

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.car_index == 0
    assert summary.stint_index == 0
    assert summary.compound == "soft"
    assert summary.laps == 3
    assert summary.tyre_age_start == 0
    assert summary.tyre_age_end == 3
    assert summary.fuel_start_kg == 38.0
    assert summary.fuel_end_kg == 34.0
    assert summary.weather == ("clear",)
    assert summary.quality is Quality.LOW
    assert not summary.representative

    kinds = {snapshot.kind for snapshot in inventory}
    assert kinds == {"race_start", "session_end"}
    assert len(inventory) == 40
    assert [snapshot.set_index for snapshot in inventory if snapshot.kind == "race_start"] == list(
        range(20)
    )
    fitted = [snapshot for snapshot in inventory if snapshot.fitted]
    assert len(fitted) == 2
    assert {snapshot.kind for snapshot in fitted} == {"race_start", "session_end"}


def test_reingesting_replaces_summaries_and_inventory_without_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)

    with SessionStore.open(tmp_path / "sessions.db") as store:
        first = ingest_capture(path, store, AppConfig())
        ingest_capture(path, store, AppConfig(), replace=True)
        assert len(store.list_sessions()) == 1

        summaries = list(store.iter_stint_summaries(first.session_id))
        inventory = list(store.iter_tyre_inventory(first.session_id))

    assert summaries == []
    assert inventory == []


def test_an_interrupted_session_keeps_its_summaries_and_is_marked_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    path.write_bytes(path.read_bytes()[:-3])

    with SessionStore.open(tmp_path / "sessions.db") as store:
        result = ingest_capture(path, store, AppConfig())
        session = store.get_session(result.session_id)
        summaries = list(store.iter_stint_summaries(result.session_id))
        inventory = list(store.iter_tyre_inventory(result.session_id))

    assert session is not None and session.incomplete
    assert summaries
    assert {snapshot.kind for snapshot in inventory} == {"race_start", "session_end"}


def test_stint_summaries_are_stored_when_a_live_session_is_closed(tmp_path: Path) -> None:
    lap = LapRecord(
        car_index=0,
        lap_number=1,
        lap_time_s=90.0,
        tyre_age_start=0,
        tyre_age_end=1,
        fuel_start_kg=40.0,
        fuel_end_kg=38.0,
        closed=True,
        track_temperature_c=30.0,
    )
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            recorder.observe(
                1.0,
                1.0,
                _snapshot(),
                _metrics(),
                LapHistorySnapshot(
                    records={0: (lap,)}, tyre_stints={0: (TyreStint(),)}, session_uid=1
                ),
            )
        finally:
            recorder.stop()

        summaries = list(store.iter_stint_summaries(_session_id(recorder)))
        inventory = list(store.iter_tyre_inventory(_session_id(recorder)))

    assert len(summaries) == 1
    assert summaries[0].laps == 1
    assert summaries[0].compound is None
    assert inventory == []


def test_race_start_inventory_is_stored_once_per_session(tmp_path: Path) -> None:
    inventory = (
        TyreSetSnapshot(
            index=0,
            actual_tyre_compound="c5",
            visual_tyre_compound="soft",
            wear_pct=0.0,
            available=True,
            recommended_session="qualifying_1",
            lifespan_laps=18,
            usable_life_laps=18,
            lap_delta_time_ms=0,
            fitted=True,
        ),
    )
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            recorder.observe(1.0, 1.0, _snapshot(tyre_inventory=inventory), _metrics(), _history())
            recorder.observe(
                2.0,
                2.0,
                _snapshot(lap=2, tyre_inventory=inventory),
                _metrics(computed_at=2.0),
                _history(),
            )
        finally:
            recorder.stop()

        snapshots = list(store.iter_tyre_inventory(_session_id(recorder)))

    assert [snapshot.kind for snapshot in snapshots] == ["race_start", "session_end"]
    assert all(snapshot.set_index == 0 for snapshot in snapshots)
    assert all(snapshot.fitted for snapshot in snapshots)


def test_practice_sessions_get_no_race_start_inventory(tmp_path: Path) -> None:
    inventory = (
        TyreSetSnapshot(
            index=0,
            actual_tyre_compound="c5",
            visual_tyre_compound="soft",
            wear_pct=0.0,
            available=True,
            recommended_session="practice_1",
            lifespan_laps=18,
            usable_life_laps=18,
            lap_delta_time_ms=0,
            fitted=True,
        ),
    )
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            recorder.observe(
                1.0,
                1.0,
                _snapshot(session_type="Practice", tyre_inventory=inventory),
                _metrics(),
                _history(),
            )
        finally:
            recorder.stop()

        snapshots = list(store.iter_tyre_inventory(_session_id(recorder)))

    assert [snapshot.kind for snapshot in snapshots] == ["session_end"]


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


def test_the_weekend_profile_is_registered_in_the_session(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(store, RecordingConfig(batch_size=1, flush_interval_s=0.01))
        try:
            recorder.set_weekend_profile(1, '{"schema_version":1}', "abc123")
            recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        finally:
            recorder.stop()

        session = store.get_session(_session_id(recorder))
        assert session is not None
        assert session.profile_json == '{"schema_version":1}'
        assert session.profile_hash == "abc123"


def test_the_default_profile_only_applies_to_race_sessions(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(
            store,
            RecordingConfig(batch_size=1, flush_interval_s=0.01),
            default_profile=('{"schema_version":1}', "abc123"),
        )
        try:
            recorder.observe(1.0, 1.0, _snapshot(session_type="Practice"), _metrics(), _history())
        finally:
            recorder.stop()

        session = store.get_session(_session_id(recorder))
        assert session is not None
        assert session.profile_json is None
        assert session.profile_hash is None


def test_the_default_profile_is_used_by_race_sessions(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        recorder = SessionRecorder(
            store,
            RecordingConfig(batch_size=1, flush_interval_s=0.01),
            default_profile=('{"schema_version":1}', "abc123"),
        )
        try:
            recorder.observe(1.0, 1.0, _snapshot(), _metrics(), _history())
        finally:
            recorder.stop()

        session = store.get_session(_session_id(recorder))
        assert session is not None
        assert session.profile_json == '{"schema_version":1}'
        assert session.profile_hash == "abc123"


def test_ingest_reuses_the_profile_registered_for_the_same_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _capture(tmp_path, monkeypatch)
    profile = WeekendProfile()

    with SessionStore.open(tmp_path / "sessions.db") as store:
        first = ingest_capture(path, store, AppConfig())
        stored = store.get_session(first.session_id)
        assert stored is not None
        store.update_session(
            first.session_id,
            replace(
                stored,
                profile_json=profile.to_json(),
                profile_hash=profile.hash,
            ),
        )

        second = ingest_capture(path, store, AppConfig(), replace=True)
        ingested = store.get_session(second.session_id)

    assert ingested is not None
    assert ingested.profile_json == profile.to_json()
    assert ingested.profile_hash == profile.hash


def test_ingest_selects_profiles_per_race_uid_in_a_multisession_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    race = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    first_uid = race[0][0].header.session_uid
    second_uid = first_uid + 1
    packets = race + [
        (
            replace(
                packet,
                header=replace(packet.header, session_uid=second_uid),
            ),
            observed_at + max(timestamp for _, timestamp in race) + 1.0,
        )
        for packet, observed_at in race
    ]
    path = tmp_path / "two-races.f1cap"
    with CaptureWriter(path) as writer:
        for index in range(len(packets)):
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
    capture_hash = file_sha256(path)
    first_profile = WeekendProfile()
    second_profile = replace(first_profile, sessions=(99,))
    database = tmp_path / "sessions.db"
    with SessionStore.open(database) as store:
        for uid, profile in ((first_uid, first_profile), (second_uid, second_profile)):
            store.create_session(
                SessionInfo(
                    session_uid=uid,
                    source="live",
                    started_at_utc="2026-11-05T10:00:00Z",
                    capture_sha256=capture_hash,
                    session_type="race",
                    profile_json=profile.to_json(),
                    profile_hash=profile.hash,
                )
            )

        ingest_capture(path, store, AppConfig())
        ingested = {
            session.session_uid: session
            for session in store.list_sessions()
            if session.source == "ingest"
        }

    assert ingested[first_uid].profile_hash == first_profile.hash
    assert ingested[second_uid].profile_hash == second_profile.hash


def _summary() -> StintSummary:
    """Resumen de tanda representativo para alimentar un perfil del fin de semana."""
    return StintSummary(
        car_index=0,
        stint_index=0,
        start_lap=1,
        end_lap=10,
        laps=10,
        compound="c3",
        visual_compound="soft",
        clean_laps=6,
        tyre_age_start=0,
        tyre_age_end=10,
        fuel_start_kg=40.0,
        fuel_end_kg=38.0,
        pace_mean_s=90.0,
        pace_mean_fuel_corrected_s=None,
        pace_trend_s_per_lap=None,
        pace_trend_fuel_corrected_s_per_lap=None,
        wear_start_pct=0.0,
        wear_end_pct=1.0,
        wear_rate_pct_per_lap=0.10,
        track_temperature_mean_c=30.0,
        weather=("clear",),
        representative=True,
        quality=Quality.OK,
    )


def _prepare_weekend(database: Path) -> int:
    """Crea un fin de semana manual con una sesión previa del mismo circuito."""
    with SessionStore.open(database) as store:
        session_id = store.create_session(
            SessionInfo(
                session_uid=99,
                source="live",
                started_at_utc="2026-11-05T10:00:00Z",
                track="monza",
                session_type="practice_1",
                season_link_id=1269315945,
                weekend_link_id=1269315945,
            )
        )
        store.add_stint_summaries(session_id, [_summary()])
        store.link_sessions([session_id])
    return session_id


def _run_live_session(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    *,
    packets: list[tuple[ParsedPacket, float]] | None = None,
) -> CaptureSession:
    """Ejecuta una carrera sintética en vivo contra la base de datos indicada."""
    if packets is None:
        packets = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    parsed = iter(packet for packet, _ in packets)
    monkeypatch.setattr("f1_strategist.session.parse_packet", lambda _: next(parsed))

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
    return session


def test_a_live_race_registers_the_weekend_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "live.db"
    _prepare_weekend(database)

    session = _run_live_session(monkeypatch, database)

    with SessionStore.open(database, readonly=True) as store:
        sessions = store.list_sessions()

    assert len(sessions) == 2
    race = sessions[-1]
    assert race.session_type is not None and race.session_type.lower() == "race"
    assert race.profile_json is not None
    assert race.profile_hash is not None
    assert json.loads(race.profile_json)["schema_version"] == 1
    assert session.weekend_profile is not None
    assert session.stats.snapshot().consumer_errors == 0


def test_a_live_race_without_a_weekend_keeps_capturing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "live.db"
    with SessionStore.open(database):
        pass

    session = _run_live_session(monkeypatch, database)

    with SessionStore.open(database, readonly=True) as store:
        sessions = store.list_sessions()

    assert len(sessions) == 1
    assert sessions[0].profile_json is None
    assert session.weekend_profile is None
    assert session.stats.snapshot().consumer_errors == 0


def test_a_weekend_read_failure_does_not_stop_the_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "live.db"
    _prepare_weekend(database)

    def broken_find_weekend(
        self: SessionStore,
        *,
        weekend_link_id: int,
        season_link_id: int | None = None,
    ) -> object:
        raise RuntimeError("base de datos rota")

    monkeypatch.setattr(SessionStore, "find_weekend", broken_find_weekend)

    session = _run_live_session(monkeypatch, database)

    with SessionStore.open(database, readonly=True) as store:
        sessions = store.list_sessions()

    snapshot = session.stats.snapshot()
    assert snapshot.consumer_errors == 1
    assert snapshot.consumer_error is not None
    assert snapshot.consumer_error.key == "weekend_profile_error"
    assert "base de datos rota" in str(snapshot.consumer_error.get("error"))
    assert len(sessions) == 2
    assert sessions[-1].profile_json is None


def test_live_profile_is_cleared_when_the_session_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "live.db"
    _prepare_weekend(database)
    weekend = list(build_weekend_timed(_cars(), total_laps=3, samples_per_lap=1))
    session_uids = tuple(dict.fromkeys(packet.header.session_uid for packet, _ in weekend))
    race_packets = [item for item in weekend if item[0].header.session_uid == session_uids[-1]]
    practice_source = [item for item in weekend if item[0].header.session_uid == session_uids[0]]
    practice_origin = practice_source[0][1]
    next_session_origin = max(observed_at for _, observed_at in weekend) + 1.0
    practice_packets = [
        (packet, next_session_origin + observed_at - practice_origin)
        for packet, observed_at in practice_source
    ]

    session = _run_live_session(monkeypatch, database, packets=race_packets + practice_packets)

    metric = session.metrics_snapshot().metrics["profile.wear_rate_pct_per_lap"]
    assert session.race_snapshot().session.session_type == "practice_1"
    assert session.weekend_profile is None
    assert metric.value is None
    assert metric.quality is Quality.UNAVAILABLE


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
