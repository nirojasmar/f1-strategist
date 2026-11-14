"""Validación de extremo a extremo de la grabación, la reproducción y el rendimiento."""

from __future__ import annotations

import json
import os
import struct
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.config import AppConfig, CaptureConfig, RecordingConfig, TelemetryConfig
from f1_strategist.fingerprint import (
    compute_fingerprint,
    file_sha256,
    fingerprint_capture,
)
from f1_strategist.metrics.engine import MetricsEngine, MetricsSnapshot
from f1_strategist.metrics.export import (
    render_metrics_export,
    run_metrics_capture,
    snapshot_to_dict,
)
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.offline import run_capture_offline
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import ParsedPacket
from f1_strategist.playback import PlaybackSession
from f1_strategist.race_state import (
    RaceEvent,
    RaceSessionSnapshot,
    RaceState,
    RaceStateSnapshot,
)
from f1_strategist.recorder import SessionRecorder, ingest_capture
from f1_strategist.replay import replay_capture
from f1_strategist.session import CaptureSession
from f1_strategist.store import MetricSample, SessionInfo, SessionStore
from f1_strategist.store.queries import render_session_export
from f1_strategist.weekend_profile import WeekendProfile, profile_from_json
from f1_strategist.weekend_provider import build_profile_for_weekend
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed, build_weekend_timed

_REAL_FIXTURE = Path(__file__).parent / "fixtures" / "real" / "monza-13-laps.f1cap.gz"
_EXPORT_FORMATS = ("json", "jsonl", "csv")
_CLOCK_FIELDS = frozenset({"started_at_monotonic", "ended_at_monotonic"})


class _EventCollector:
    """Acumula todos los eventos y aplica el mismo pipeline que ``metrics``."""

    def __init__(self, *, profile: WeekendProfile | None = None) -> None:
        self.events: list[RaceEvent] = []
        self.state = RaceState(self.events.append)
        self.engine = MetricsEngine(profile=profile)
        self.last_state: RaceStateSnapshot | None = None
        self.last_observed_at = 0.0

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.state.apply(packet, observed_at)
        self.last_state = self.state.snapshot()
        self.last_observed_at = observed_at
        self.engine.apply(packet, observed_at, self.last_state)

    def finish(self) -> None:
        if self.last_state is not None:
            self.engine.finalize(self.last_state, self.last_observed_at)


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(1, "Driver B", 2, 90.5, 40.0, 2.0, 2.3, compounds=("medium",)),
    )


def _scenario() -> list[tuple[ParsedPacket, float]]:
    return list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))


def _write_capture(
    path: Path,
    packets: list[tuple[ParsedPacket, float]],
    *,
    interval_s: float = 0.0001,
    index_offset: int = 0,
) -> None:
    with CaptureWriter(path) as writer:
        for index, _ in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=int(index * interval_s * 1e9),
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=make_packet(
                        1,
                        extra=struct.pack("<I", index_offset + index),
                    ),
                )
            )


def _patch_parser(
    monkeypatch: pytest.MonkeyPatch,
    packets: list[tuple[ParsedPacket, float]],
) -> None:
    parsed = [packet for packet, _ in packets]

    def fake_parse(payload: bytes) -> ParsedPacket:
        index = struct.unpack_from("<I", payload, HEADER_SIZE)[0]
        return parsed[index]

    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)
    monkeypatch.setattr("f1_strategist.playback.parse_packet", fake_parse)
    monkeypatch.setattr("f1_strategist.session.parse_packet", fake_parse)


def _capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    interval_s: float = 0.0001,
) -> tuple[Path, list[tuple[ParsedPacket, float]]]:
    packets = _scenario()
    path = tmp_path / "race.f1cap"
    _write_capture(path, packets, interval_s=interval_s)
    _patch_parser(monkeypatch, packets)
    return path, packets


def _record_without_clocks(record: LapRecord) -> dict[str, Any]:
    return {
        field.name: getattr(record, field.name)
        for field in fields(LapRecord)
        if field.name not in _CLOCK_FIELDS
    }


def _records_by_key(
    records: Mapping[int, tuple[LapRecord, ...]],
) -> dict[tuple[int, int], LapRecord]:
    return {
        (record.car_index, record.lap_number): record
        for items in records.values()
        for record in items
    }


def _wait_until(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_ingest_matches_metrics_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    expected = run_metrics_capture(path)

    collector = _EventCollector()
    run_capture_offline(path, [collector])
    collector.finish()

    with SessionStore.open(tmp_path / "sessions.db") as store:
        result = ingest_capture(path, store, AppConfig())
        session = store.get_session(result.session_id)
        assert session is not None
        stored_laps = _records_by_key(
            {
                car: tuple(store.iter_laps(result.session_id, car_index=car))
                for car in expected.history.records
            }
        )
        stored_events = [
            (
                event.kind,
                event.t_sim_s,
                event.car_index,
                event.lap_number,
                event.value,
                event.previous_value,
            )
            for event in store.iter_events(result.session_id)
        ]
        expected_events = [
            (
                event.kind,
                event.occurred_at_monotonic,
                event.car_index,
                event.lap_number,
                event.value,
                event.previous_value,
            )
            for event in collector.events
        ]
        expected_laps = _records_by_key(expected.history.records)

        for export_format in _EXPORT_FORMATS:
            assert render_session_export(
                store, result.session_id, export_format
            ) == render_metrics_export(expected.snapshot, expected.history, export_format)

    assert stored_laps.keys() == expected_laps.keys()
    for key, record in stored_laps.items():
        assert _record_without_clocks(record) == _record_without_clocks(expected_laps[key])
    assert stored_events == expected_events


def _offline_fingerprint(path: Path) -> str:
    collector = _EventCollector()
    run_capture_offline(path, [collector])
    collector.finish()
    assert collector.last_state is not None
    return compute_fingerprint(
        collector.last_state,
        collector.engine.history,
        collector.engine.snapshot(),
    )


def _playback_fingerprint(path: Path, *, speed: float, pause: bool = False) -> str:
    with PlaybackSession(path, speed=speed) as session:
        if pause:
            assert _wait_until(lambda: session.snapshot().received >= 2)
            session.pause()
            assert session.paused
            time.sleep(0.02)
            session.resume()
        assert session.wait(20.0)
        assert session.error is None
        assert session.finished
        return compute_fingerprint(
            session.race_snapshot(),
            session.history_snapshot(),
            session.metrics_snapshot(),
        )


def test_playback_fingerprint_matches_offline_at_every_speed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch)
    expected = _offline_fingerprint(path)

    assert _playback_fingerprint(path, speed=0.0) == expected
    assert _playback_fingerprint(path, speed=1.0) == expected
    assert _playback_fingerprint(path, speed=4.0) == expected


def test_pausing_and_resuming_does_not_change_the_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = _capture(tmp_path, monkeypatch, interval_s=0.01)
    expected = _offline_fingerprint(path)

    assert _playback_fingerprint(path, speed=1.0, pause=True) == expected


def _live_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        telemetry=TelemetryConfig(listen_port=0),
        capture=CaptureConfig(enabled=True, directory=tmp_path / "captures"),
        recording=RecordingConfig(
            enabled=True,
            database=tmp_path / "sessions.db",
            batch_size=1,
            flush_interval_s=0.01,
        ),
    )


def test_live_capture_matches_ingest_for_laps_and_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _capture(tmp_path, monkeypatch, interval_s=0.01)
    config = _live_config(tmp_path)
    session = CaptureSession(config)
    session.start()
    try:
        sent = replay_capture(source, "127.0.0.1", session.bound_port, speed=1)
        assert _wait_until(
            lambda: (
                session.snapshot().capture_packets >= sent
                and session.race_snapshot().session.ended is True
            )
        )
    finally:
        session.stop()

    assert session.capture_path is not None
    capture_path = Path(session.capture_path)
    assert capture_path.exists()

    database = config.recording.database
    with SessionStore.open(database) as store:
        live_sessions = [item for item in store.list_sessions() if item.source == "live"]
        assert len(live_sessions) == 1
        live = live_sessions[0]
        assert live.id is not None
        assert live.tool_version is not None
        assert live.parser_version is not None
        assert live.config_hash is not None
        assert live.capture_sha256 == file_sha256(capture_path)
        live_laps = _records_by_key(
            {
                car: tuple(store.iter_laps(live.id, car_index=car))
                for car in (item.index for item in _cars())
            }
        )
        live_events = [event.kind for event in store.iter_events(live.id)]

        ingested = ingest_capture(source, store, config)
        ingested_laps = _records_by_key(
            {
                car: tuple(store.iter_laps(ingested.session_id, car_index=car))
                for car in (item.index for item in _cars())
            }
        )
        ingested_events = [event.kind for event in store.iter_events(ingested.session_id)]

    assert live_laps.keys() == ingested_laps.keys()
    for key, record in live_laps.items():
        other = ingested_laps[key]
        assert (
            record.lap_time_s,
            record.sector1_s,
            record.sector2_s,
            record.sector3_s,
            record.valid,
            record.compound,
            record.pit_entry,
            record.pit_exit,
        ) == (
            other.lap_time_s,
            other.sector1_s,
            other.sector2_s,
            other.sector3_s,
            other.valid,
            other.compound,
            other.pit_entry,
            other.pit_exit,
        )
    assert live_events == ingested_events


def _two_session_scenario() -> tuple[list[tuple[ParsedPacket, float]], int]:
    first = _scenario()
    second = [
        (
            replace(
                packet, header=replace(packet.header, session_uid=packet.header.session_uid + 1)
            ),
            at,
        )
        for packet, at in _scenario()
    ]
    return [*first, *second], len(first)


def _expected_segment(
    packets: list[tuple[ParsedPacket, float]],
    *,
    start_index: int,
) -> _EventCollector:
    collector = _EventCollector()
    for offset, (packet, _) in enumerate(packets):
        recv_time_ns = (start_index + offset) * 100_000
        collector.apply(packet, recv_time_ns / 1_000_000_000)
    collector.finish()
    return collector


def test_multisession_ingest_persists_each_final_snapshot_and_replaces_as_a_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets, split = _two_session_scenario()
    path = tmp_path / "multisession.f1cap"
    _write_capture(path, packets)
    _patch_parser(monkeypatch, packets)
    expected = (
        _expected_segment(packets[:split], start_index=0),
        _expected_segment(packets[split:], start_index=split),
    )
    config = AppConfig()

    with SessionStore.open(tmp_path / "sessions.db") as store:
        first_ingest = ingest_capture(path, store, config)
        sessions = [
            item
            for item in store.list_sessions()
            if item.source == "ingest" and item.capture_sha256 == file_sha256(path)
        ]
        assert len(sessions) == 2
        assert all(item.ended_at_utc is not None for item in sessions)
        assert all(item.final_metrics_json is not None for item in sessions)

        for session, segment in zip(sessions, expected, strict=True):
            assert session.id is not None
            for export_format in _EXPORT_FORMATS:
                assert render_session_export(
                    store, session.id, export_format
                ) == render_metrics_export(
                    segment.engine.snapshot(),
                    segment.engine.history,
                    export_format,
                )

        replaced = ingest_capture(path, store, config, replace=True)
        replacement_sessions = [
            item
            for item in store.list_sessions()
            if item.source == "ingest" and item.capture_sha256 == file_sha256(path)
        ]

    assert first_ingest.session_id != replaced.session_id
    assert len(replacement_sessions) == 2
    assert all(item.id is not None for item in replacement_sessions)
    assert all(item.final_metrics_json is not None for item in replacement_sessions)


def test_live_multisession_capture_hashes_every_session_after_writer_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets, _ = _two_session_scenario()
    source = tmp_path / "multisession-source.f1cap"
    _write_capture(source, packets)
    _patch_parser(monkeypatch, packets)
    config = _live_config(tmp_path)
    session = CaptureSession(config)
    session.start()
    try:
        sent = replay_capture(source, "127.0.0.1", session.bound_port, speed=0)
        assert _wait_until(lambda: session.snapshot().capture_packets >= sent)
    finally:
        session.stop()

    assert session.capture_path is not None
    expected_hash = file_sha256(Path(session.capture_path))
    with SessionStore.open(config.recording.database) as store:
        live_sessions = [item for item in store.list_sessions() if item.source == "live"]

    assert len(live_sessions) == 2
    assert all(item.capture_path == str(session.capture_path) for item in live_sessions)
    assert all(item.capture_sha256 == expected_hash for item in live_sessions)
    assert all(item.tool_version is not None for item in live_sessions)
    assert all(item.parser_version is not None for item in live_sessions)
    assert all(item.config_hash is not None for item in live_sessions)
    assert all(item.final_metrics_json is not None for item in live_sessions)


def test_synthetic_weekend_profile_replays_with_the_registered_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weekend_cars = (
        replace(_cars()[0], compounds=("c5",)),
        replace(_cars()[1], compounds=("c4",)),
    )
    packets = list(build_weekend_timed(weekend_cars, total_laps=8, samples_per_lap=2))
    _patch_parser(monkeypatch, packets)
    session_uids = tuple(dict.fromkeys(packet.header.session_uid for packet, _ in packets))
    assert len(session_uids) == 3, "weekend: expected practice, qualifying, and race"
    prior_packets = [item for item in packets if item[0].header.session_uid != session_uids[-1]]
    race_packets = [item for item in packets if item[0].header.session_uid == session_uids[-1]]
    prior_capture = tmp_path / "practice-qualifying.f1cap"
    race_source = tmp_path / "race-source.f1cap"
    _write_capture(prior_capture, prior_packets, interval_s=0.01)
    _write_capture(
        race_source,
        race_packets,
        interval_s=0.01,
        index_offset=len(prior_packets),
    )

    base_config = _live_config(tmp_path)
    prior_config = base_config.model_copy(
        update={
            "capture": CaptureConfig(
                enabled=True,
                directory=tmp_path / "prior-captures",
            )
        }
    )
    race_config = base_config.model_copy(
        update={
            "capture": CaptureConfig(
                enabled=True,
                directory=tmp_path / "race-captures",
            )
        }
    )
    prior_session = CaptureSession(prior_config)
    prior_session.start()
    try:
        sent = replay_capture(prior_capture, "127.0.0.1", prior_session.bound_port, speed=0)
        assert _wait_until(lambda: prior_session.snapshot().capture_packets >= sent)
    finally:
        prior_session.stop()

    with SessionStore.open(base_config.recording.database, readonly=True) as store:
        prior_rows = [
            item
            for item in store.list_sessions()
            if item.source == "live" and item.session_uid in session_uids[:2]
        ]
        assert len(prior_rows) == 2, "practice/qualifying: expected 2 closed sessions"
        assert all(item.ended_at_utc is not None for item in prior_rows), (
            "practice/qualifying: expected both sessions to be closed"
        )
        assert len({item.weekend_id for item in prior_rows}) == 1, (
            "practice/qualifying: expected shared game weekend identifier"
        )
        assert all(item.track == prior_rows[0].track for item in prior_rows), (
            "practice/qualifying: expected the same track"
        )
        for item in prior_rows:
            assert item.id is not None
            summaries = tuple(store.iter_stint_summaries(item.id))
            assert summaries, f"{item.session_type} / c5: expected persisted stint summaries"
            player_summary = next(
                (summary for summary in summaries if summary.car_index == 0),
                None,
            )
            assert player_summary is not None and player_summary.compound == "c5", (
                f"{item.session_type} / c5: expected player stint summary, got {player_summary}"
            )
            assert player_summary.wear_rate_pct_per_lap is not None, (
                f"{item.session_type} / c5: expected a measured wear rate"
            )

    race_session = CaptureSession(race_config)
    race_session.start()
    try:
        sent = replay_capture(race_source, "127.0.0.1", race_session.bound_port, speed=0)
        assert _wait_until(lambda: race_session.snapshot().capture_packets >= sent)
    finally:
        race_session.stop()

    assert race_session.capture_path is not None
    race_capture = Path(race_session.capture_path)
    with SessionStore.open(base_config.recording.database) as store:
        race_rows = [
            item
            for item in store.list_sessions()
            if item.source == "live" and item.session_uid == session_uids[-1]
        ]
        assert len(race_rows) == 1, "race: expected exactly one recorded session"
        race_row = race_rows[0]
        assert race_row.id is not None
        assert race_row.profile_json is not None, "race: expected registered profile JSON"
        assert race_row.profile_hash is not None, "race: expected registered profile hash"
        registered_profile = profile_from_json(race_row.profile_json)
        assert registered_profile.hash == race_row.profile_hash, (
            "race profile: expected stored hash to match its serialized value"
        )
        race_inventory = tuple(store.iter_tyre_inventory(race_row.id, kind="race_start"))
        assert registered_profile.inventory == race_inventory, (
            "race profile: expected the current race-start inventory"
        )
        profile_sessions = set(registered_profile.sessions)
        expected_profile_sessions = {item.id for item in prior_rows if item.id is not None}
        assert profile_sessions == expected_profile_sessions, (
            f"race profile: expected practice/qualifying sessions "
            f"{sorted(expected_profile_sessions)}, got {sorted(profile_sessions)}"
        )
        c5 = registered_profile.compound("c5")
        assert c5 is not None and c5.wear_rate_pct_per_lap.value is not None, (
            "race / c5: expected prior wear rate from practice and qualifying"
        )
        recorded_profile_hash = race_row.profile_hash
        live_final_metrics = race_row.final_metrics_json

        ingested = ingest_capture(race_capture, store, race_config)
        replayed_row = store.get_session(ingested.session_id)

    assert replayed_row is not None
    assert replayed_row.profile_hash == recorded_profile_hash, (
        "race replay: expected the exact profile recorded by the live session"
    )
    assert live_final_metrics is not None
    assert replayed_row.final_metrics_json is not None
    replay_metrics = json.loads(replayed_row.final_metrics_json)["metrics"]
    assert replay_metrics["profile.wear_rate_pct_per_lap"]["value"] == (
        c5.wear_rate_pct_per_lap.value
    ), "race / c5 wear rate: expected replay to use the registered profile"

    playback = PlaybackSession(
        race_capture,
        race_config.metrics,
        speed=0,
        profiles_by_uid={session_uids[-1]: registered_profile},
    )
    playback.play()
    assert playback.wait(timeout=10)
    assert playback.error is None
    playback_metrics = snapshot_to_dict(playback.metrics_snapshot())
    assert playback_metrics["metrics"]["profile.wear_rate_pct_per_lap"]["value"] == (
        c5.wear_rate_pct_per_lap.value
    )

    replay_fingerprint = fingerprint_capture(
        race_capture,
        race_config,
        profiles_by_uid={session_uids[-1]: registered_profile},
    )
    assert ingested.fingerprint == replay_fingerprint.fingerprint


def test_continuous_weekend_waits_for_previous_sessions_to_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cars = (
        replace(_cars()[0], compounds=("c5",)),
        replace(_cars()[1], compounds=("c4",)),
    )
    packets = list(build_weekend_timed(cars, total_laps=5, samples_per_lap=1))
    _patch_parser(monkeypatch, packets)
    source = tmp_path / "continuous-weekend.f1cap"
    _write_capture(source, packets, interval_s=0.01)

    add_stint_summaries = SessionStore.add_stint_summaries

    def slow_stint_write(
        store: SessionStore,
        session_id: int,
        summaries: Any,
    ) -> None:
        time.sleep(0.02)
        add_stint_summaries(store, session_id, summaries)

    monkeypatch.setattr(SessionStore, "add_stint_summaries", slow_stint_write)
    config = _live_config(tmp_path)
    session = CaptureSession(config)
    session.start()
    try:
        sent = replay_capture(source, "127.0.0.1", session.bound_port, speed=0)
        assert _wait_until(
            lambda: (
                session.snapshot().capture_packets >= sent
                and session.race_snapshot().session.ended is True
            )
        )
    finally:
        session.stop()

    session_uids = tuple(dict.fromkeys(packet.header.session_uid for packet, _ in packets))
    with SessionStore.open(config.recording.database) as store:
        rows = [
            item
            for item in store.list_sessions()
            if item.source == "live" and item.session_uid in session_uids
        ]
        assert len(rows) == 3
        race = next(item for item in rows if item.session_uid == session_uids[-1])
        assert race.id is not None
        assert race.weekend_id is not None
        assert race.profile_json is not None
        profile = profile_from_json(race.profile_json)
        previous_ids = tuple(
            sorted(
                item.id
                for item in rows
                if item.session_uid != session_uids[-1] and item.id is not None
            )
        )
        assert profile.sessions == previous_ids
        race_inventory = tuple(store.iter_tyre_inventory(race.id, kind="race_start"))
        assert race_inventory
        assert profile.inventory == race_inventory
        expected = build_profile_for_weekend(
            store,
            race.weekend_id,
            config.metrics,
            exclude_session_uid=race.session_uid,
            inventory_override=profile.inventory,
        )

    assert profile.to_json() == expected.to_json()


class _MemoryStore:
    """Almacén en memoria compatible con el protocolo del grabador."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.sessions: list[SessionInfo] = []
        self.finished: list[tuple[int, str, bool]] = []
        self.samples: list[MetricSample] = []
        self._next_id = 1

    @contextmanager
    def transaction(self) -> Iterator[object]:
        if self.delay:
            time.sleep(self.delay)
        yield object()

    def create_session(self, info: SessionInfo) -> int:
        session_id = self._next_id
        self._next_id += 1
        self.sessions.append(info)
        return session_id

    def update_session(self, session_id: int, info: SessionInfo) -> None:
        return None

    def update_capture_sha256(self, session_id: int, sha256: str) -> None:
        return None

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
        self.finished.append((session_id, ended_at_utc, incomplete))

    def add_participants(self, session_id: int, participants: Any) -> None:
        return None

    def upsert_laps(self, session_id: int, laps: Any) -> None:
        return None

    def add_events(self, session_id: int, events: Any) -> None:
        return None

    def add_metric_samples(self, session_id: int, samples: Any) -> None:
        self.samples.extend(samples)

    def add_errors(self, session_id: int, errors: Any) -> None:
        return None

    def add_stint_summaries(self, session_id: int, summaries: Any) -> None:
        return None

    def add_tyre_inventory(self, session_id: int, snapshots: Any) -> None:
        return None

    def add_strategy_evaluations(self, session_id: int, evaluations: Any) -> None:
        return None


def _benchmark_inputs(count: int) -> list[tuple[RaceStateSnapshot, MetricsSnapshot]]:
    session = RaceSessionSnapshot(
        uid=1,
        session_type="Race",
        track="Monza",
        total_laps=count,
        started=True,
        ended=False,
    )
    inputs: list[tuple[RaceStateSnapshot, MetricsSnapshot]] = []
    for lap in range(1, count + 1):
        state = RaceStateSnapshot(
            session=session,
            current_lap=lap,
            player_car_index=0,
            cars=(),
            freshness=(),
            events=(),
        )
        metric = Metric(
            key="fuel.in_tank_kg",
            value=40.0 - lap * 0.1,
            unit="kg",
            window=Window("instant"),
            quality=Quality.OK,
            sample_count=lap,
            computed_at_monotonic=float(lap),
        )
        metrics = MetricsSnapshot(
            metrics={"fuel.in_tank_kg": metric},
            computed_at_monotonic=float(lap),
            session_uid=1,
        )
        inputs.append((state, metrics))
    return inputs


@pytest.mark.benchmark
def test_observe_costs_less_than_a_millisecond_on_average() -> None:
    inputs = _benchmark_inputs(200)
    store = _MemoryStore()
    recorder = SessionRecorder(
        store,
        RecordingConfig(queue_size=10_000, batch_size=1_000, flush_interval_s=10.0),
    )
    try:
        started = time.perf_counter()
        for index, (state, metrics) in enumerate(inputs):
            recorder.observe(float(index), float(index), state, metrics, _empty_history())
        elapsed = time.perf_counter() - started
    finally:
        recorder.stop()

    assert recorder.dropped_batches == 0
    assert elapsed / len(inputs) < 0.001


@pytest.mark.benchmark
def test_a_slow_store_does_not_block_live_observations() -> None:
    store = _MemoryStore(delay=0.02)
    recorder = SessionRecorder(
        store,
        RecordingConfig(queue_size=1_000, batch_size=1, flush_interval_s=0.01),
    )
    snapshots = _benchmark_inputs(100)
    try:
        started = time.perf_counter()
        for index, (state, metrics) in enumerate(snapshots):
            recorder.observe(float(index), float(index), state, metrics, _empty_history())
        elapsed = time.perf_counter() - started
    finally:
        recorder.stop()

    assert recorder.dropped_batches == 0
    # Una escritura síncrona tardaría unos 2 s (100 lotes de 20 ms).
    assert elapsed < 0.5


@pytest.mark.benchmark
def test_slow_store_does_not_delay_or_drop_udp_consumer_packets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets = _scenario()
    source = tmp_path / "consumer-load.f1cap"
    _write_capture(source, packets)
    _patch_parser(monkeypatch, packets)

    def run_case(label: str, delay: float) -> tuple[str, float, float, int, int, int, int]:
        config = _live_config(tmp_path / label)
        with monkeypatch.context() as patch:
            store = SessionStore.open(config.recording.database)
            store_proxy = _SlowSQLiteStore(store, delay)
            session = CaptureSession(config, queue_size=10_000)
            queue_peak = 0
            consumer_latencies: list[float] = []
            original_put = session._queue.put_nowait
            original_write = session._write_packet

            def track_queue(item: CapturedPacket) -> None:
                nonlocal queue_peak
                original_put(item)
                queue_peak = max(queue_peak, session._queue.qsize())

            def track_consumer(packet: CapturedPacket) -> None:
                if packet.recv_monotonic is not None:
                    consumer_latencies.append(time.monotonic() - packet.recv_monotonic)
                original_write(packet)

            patch.setattr(session._queue, "put_nowait", track_queue)
            patch.setattr(session, "_write_packet", track_consumer)
            patch.setattr(
                SessionStore,
                "open",
                classmethod(lambda cls, path: store_proxy),
            )
            session.start()
            recorder = session._recorder
            assert recorder is not None
            started = time.perf_counter()
            try:
                sent = replay_capture(source, "127.0.0.1", session.bound_port, speed=0)
                assert _wait_until(lambda: session.snapshot().received >= sent)
            finally:
                session.stop()
            elapsed = time.perf_counter() - started
            stats = session.snapshot()
            return (
                label,
                elapsed,
                max(consumer_latencies, default=0.0),
                queue_peak,
                stats.received,
                stats.dropped_queue_full,
                recorder.dropped_batches,
            )

    results = [run_case(label, delay) for label, delay in (("normal", 0.0), ("slow", 0.02))]
    for result in results:
        label, _, _, _, received, dropped_udp, dropped_batches = result
        assert received == len(packets), label
        assert dropped_udp == 0, label
        assert dropped_batches == 0, label
    print(
        "UDP consumer benchmark (mode, completion_s, max_receive_to_consumer_s, "
        "queue_peak, received, dropped_udp, dropped_store_batches): "
        f"{results}"
    )


def _empty_history() -> LapHistorySnapshot:
    return LapHistorySnapshot(records={}, session_uid=1)


class _SlowSQLiteStore:
    """Envuelve SQLite para medir el consumidor UDP con escrituras lentas."""

    def __init__(self, inner: SessionStore, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @contextmanager
    def transaction(self) -> Iterator[object]:
        if self._delay:
            time.sleep(self._delay)
        with self._inner.transaction() as connection:
            yield connection


def _real_capture_path() -> Path:
    override = os.environ.get("F1_CAPTURE_FIXTURE")
    if override is not None:
        path = Path(override)
        if not path.is_file():
            pytest.fail(f"F1_CAPTURE_FIXTURE no apunta a un archivo: {path}")
        return path
    if not _REAL_FIXTURE.is_file():
        pytest.skip(f"No existe la captura real: {_REAL_FIXTURE}")
    return _REAL_FIXTURE


@pytest.mark.real_capture
def test_real_capture_round_trip(tmp_path: Path) -> None:
    path = _real_capture_path()
    expected = run_metrics_capture(path)
    verify = fingerprint_capture(path, AppConfig())

    with SessionStore.open(tmp_path / "sessions.db") as store:
        result = ingest_capture(path, store, AppConfig())
        session = store.get_session(result.session_id)
        assert session is not None
        assert not session.incomplete
        assert session.ended_at_utc is not None
        assert result.fingerprint == verify.fingerprint
        stored_laps = _records_by_key(
            {
                car: tuple(store.iter_laps(result.session_id, car_index=car))
                for car in expected.history.records
            }
        )
        expected_laps = _records_by_key(expected.history.records)
        assert stored_laps.keys() == expected_laps.keys()
        for key, record in stored_laps.items():
            assert _record_without_clocks(record) == _record_without_clocks(expected_laps[key])
        for export_format in _EXPORT_FORMATS:
            assert render_session_export(
                store, result.session_id, export_format
            ) == render_metrics_export(expected.snapshot, expected.history, export_format)
