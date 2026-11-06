from __future__ import annotations

import sqlite3
import threading
from dataclasses import fields
from pathlib import Path

import pytest

from f1_strategist.config import AppConfig, ConfigError, RecordingConfig, load_config
from f1_strategist.metrics.history import LapRecord
from f1_strategist.store import (
    LAP_COLUMN_EXCLUSIONS,
    LAP_COLUMN_TYPES,
    MIGRATIONS,
    SCHEMA_VERSION,
    MetricSample,
    Migration,
    Participant,
    SessionInfo,
    SessionStore,
    StoredError,
    StoredEvent,
    StoreError,
    apply_migrations,
)

MAX_UINT64 = 2**64 - 1


def _session(session_uid: int = 0xF125, **overrides: object) -> SessionInfo:
    values: dict[str, object] = {
        "session_uid": session_uid,
        "source": "live",
        "started_at_utc": "2026-11-05T10:00:00Z",
        "game_format": "F1 25",
        "game_year": 2025,
        "game_version": "1.26",
        "track": "monza",
        "session_type": "race",
        "total_laps": 13,
        "capture_path": "captures/race.f1cap",
        "capture_sha256": "a" * 64,
        "tool_version": "0.1.0",
        "parser_version": "1",
        "config_hash": "b" * 64,
        "config_json": '{"metrics":{"publish_interval_s":0.5}}',
    }
    values.update(overrides)
    return SessionInfo(**values)  # type: ignore[arg-type]


def _lap(car_index: int = 0, lap_number: int = 1, **overrides: object) -> LapRecord:
    values: dict[str, object] = {
        "car_index": car_index,
        "lap_number": lap_number,
        "lap_time_s": 91.234,
        "lap_time_source": "session_history",
        "sector1_s": 30.1,
        "sector2_s": 31.2,
        "sector3_s": 29.934,
        "valid": True,
        "compound": "soft",
        "tyre_age_start": 4,
        "tyre_age_end": 5,
        "fuel_start_kg": 40.5,
        "fuel_end_kg": 38.25,
        "tyre_wear_start_pct": (1.0, 2.0, 3.0, 4.0),
        "tyre_wear_end_pct": (5.0, 6.0, 7.0, 8.0),
        "position_start": 2,
        "position_end": 1,
        "gap_ahead_start_s": 1.5,
        "gap_ahead_end_s": 0.75,
        "ers_harvested_j": 100_000.0,
        "ers_deployed_j": 90_000.0,
        "pit_entry": False,
        "pit_exit": True,
        "pit_lane_time_s": 21.4,
        "pit_stop_time_s": 2.3,
        "track_status_seen": frozenset({"green", "safety_car"}),
        "weather_seen": frozenset({"clear"}),
        "track_temperature_c": 34.5,
        "traffic_fraction": 0.25,
        "damage_max_delta_pct": 3.0,
        "flags": frozenset({"first_lap", "track_limits"}),
        "closed": True,
        "started_at_monotonic": 100.0,
        "ended_at_monotonic": 191.234,
    }
    values.update(overrides)
    return LapRecord(**values)  # type: ignore[arg-type]


def test_create_write_close_and_reopen_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    store = SessionStore.open(path)
    session_id = store.create_session(_session(session_uid=MAX_UINT64))
    store.add_participants(
        session_id,
        [
            Participant(0, "Driver A", "Team A", ai_controlled=False),
            Participant(1, "Driver B", None, ai_controlled=True),
        ],
    )
    store.upsert_laps(session_id, [_lap(), _lap(car_index=1, lap_number=2)])
    store.add_events(
        session_id,
        [
            StoredEvent(1, "safety_car", 12.5, value="deployed", previous_value="green"),
            StoredEvent(2, "weather", 20.0, value="rain"),
        ],
    )
    store.add_metric_samples(
        session_id,
        [
            MetricSample(
                t_session_s=12.0,
                t_sim_s=13.5,
                lap=2,
                key="fuel.margin_laps_at_finish",
                value_num=1.5,
                unit="lap",
                quality="ok",
                sample_count=3,
            ),
            MetricSample(
                t_sim_s=14.0,
                lap=2,
                key="tyres.compound",
                value_text="medium",
                unit="",
                quality="low",
                sample_count=1,
            ),
        ],
    )
    store.add_errors(session_id, [StoredError(15.0, "recorder_error", '{"reason":"full"}')])
    store.finish_session(
        session_id,
        ended_at_utc="2026-11-05T11:00:00Z",
        incomplete=True,
        fingerprint="c" * 64,
    )
    store.close()

    reopened = SessionStore.open(path, readonly=True)
    try:
        session = reopened.get_session(session_id)
        assert session == _session(
            session_uid=MAX_UINT64,
            id=session_id,
            ended_at_utc="2026-11-05T11:00:00Z",
            incomplete=True,
            fingerprint="c" * 64,
        )
        assert reopened.list_sessions() == [session]
        laps = list(reopened.iter_laps(session_id))
        assert [(lap.car_index, lap.lap_number) for lap in laps] == [(0, 1), (1, 2)]
        assert laps[0] == _lap()
        events = list(reopened.iter_events(session_id))
        assert events == [
            StoredEvent(1, "safety_car", 12.5, value="deployed", previous_value="green"),
            StoredEvent(2, "weather", 20.0, value="rain"),
        ]
        samples = list(reopened.iter_metric_samples(session_id))
        assert samples[0] == MetricSample(
            t_session_s=12.0,
            t_sim_s=13.5,
            lap=2,
            key="fuel.margin_laps_at_finish",
            value_num=1.5,
            unit="lap",
            quality="ok",
            sample_count=3,
        )
        assert samples[1].value_text == "medium"
        assert list(reopened.iter_errors(session_id)) == [
            StoredError(15.0, "recorder_error", '{"reason":"full"}')
        ]
    finally:
        reopened.close()


def test_participants_are_upserted(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        store.add_participants(session_id, [Participant(0, "Old", "Team", False)])
        store.add_participants(session_id, [Participant(0, "New", "Team 2", None)])
        rows = store.connection.execute(
            "SELECT name, team, ai_controlled FROM participants"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("New", "Team 2", None)]


def test_lap_upsert_updates_a_late_official_time(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        store.upsert_laps(session_id, [_lap(lap_time_s=None, valid=None, closed=False)])
        store.upsert_laps(session_id, [_lap(lap_time_s=90.5, flags=frozenset({"invalid"}))])
        laps = list(store.iter_laps(session_id))
    assert len(laps) == 1
    assert laps[0].lap_time_s == 90.5
    assert laps[0].flags == frozenset({"invalid"})
    assert laps[0].closed is True


def test_metric_and_event_filters(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        store.add_events(
            session_id,
            [
                StoredEvent(1, "weather", 1.0),
                StoredEvent(2, "safety_car", 2.0),
                StoredEvent(3, "weather", 3.0),
            ],
        )
        store.add_metric_samples(
            session_id,
            [
                MetricSample(
                    t_sim_s=1.0,
                    lap=1,
                    key="fuel.margin_laps_at_finish",
                    quality="ok",
                    unit="lap",
                ),
                MetricSample(
                    t_sim_s=2.0,
                    lap=2,
                    key="fuel.margin_laps_at_finish",
                    quality="ok",
                    unit="lap",
                ),
                MetricSample(
                    t_sim_s=3.0,
                    lap=2,
                    key="tyres.wear_rate_pct_per_lap",
                    quality="low",
                    unit="%/lap",
                ),
            ],
        )
        assert [event.seq for event in store.iter_events(session_id, kind="weather")] == [1, 3]
        assert [
            sample.t_sim_s
            for sample in store.iter_metric_samples(session_id, key="fuel.margin_laps_at_finish")
        ] == [1.0, 2.0]
        assert [sample.t_sim_s for sample in store.iter_metric_samples(session_id, lap=2)] == [
            2.0,
            3.0,
        ]
        assert [lap.car_index for lap in store.iter_laps(session_id, car_index=1)] == []


def test_empty_iterables_are_noops(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        store.add_participants(session_id, [])
        store.upsert_laps(session_id, [])
        store.add_events(session_id, [])
        store.add_metric_samples(session_id, [])
        store.add_errors(session_id, [])
        assert list(store.iter_laps(session_id)) == []


def test_none_values_are_stored_as_null(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    with SessionStore.open(path) as store:
        session_id = store.create_session(
            _session(
                game_format=None,
                game_year=None,
                track=None,
                capture_path=None,
                config_json=None,
            )
        )
        store.upsert_laps(session_id, [LapRecord(car_index=0, lap_number=1)])
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT game_format, game_year, track, capture_path, config_json "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        lap_row = connection.execute(
            "SELECT lap_time_s, valid, tyre_wear_start_pct, compound FROM laps"
        ).fetchone()
    finally:
        connection.close()
    assert row == (None, None, None, None, None)
    assert lap_row == (None, None, None, None)


def test_session_uid_is_preserved_exactly(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session(session_uid=MAX_UINT64))
        stored = store.connection.execute(
            "SELECT session_uid FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert stored[0] == str(MAX_UINT64)
        assert store.get_session(session_id).session_uid == MAX_UINT64  # type: ignore[union-attr]


def test_finish_unknown_session_raises(tmp_path: Path) -> None:
    with (
        SessionStore.open(tmp_path / "sessions.db") as store,
        pytest.raises(StoreError, match="No existe la sesión 99"),
    ):
        store.finish_session(99, ended_at_utc="2026-11-05T11:00:00Z")


def test_migrations_are_idempotent_and_enable_wal(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    SessionStore.open(path).close()
    SessionStore.open(path).close()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_a_test_migration_upgrades_v1_to_v2(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    SessionStore.open(path).close()
    upgrades = (*MIGRATIONS, Migration(2, ("ALTER TABLE sessions ADD COLUMN note TEXT",)))

    with SessionStore.open(path, migrations=upgrades) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        store.connection.execute("UPDATE sessions SET note = 'ok'")

    with SessionStore.open(path, migrations=upgrades) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_a_newer_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    finally:
        connection.close()

    before = sqlite3.connect(path)
    original_schema = before.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    original_journal_mode = before.execute("PRAGMA journal_mode").fetchone()[0]
    before.close()

    with pytest.raises(StoreError, match="más nuevo"):
        SessionStore.open(path)
    with pytest.raises(StoreError, match="más nuevo"):
        SessionStore.open(path, readonly=True)

    after = sqlite3.connect(path)
    try:
        assert after.execute("PRAGMA user_version").fetchone()[0] == 99
        assert after.execute("PRAGMA journal_mode").fetchone()[0] == original_journal_mode
        assert (
            after.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            == original_schema
        )
    finally:
        after.close()


def test_non_contiguous_migrations_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    with SessionStore.open(path) as store, pytest.raises(StoreError, match="no contiguas"):
        apply_migrations(
            store.connection,
            (Migration(1, ()), Migration(3, ())),
        )


def test_failed_migration_rolls_back_ddl_and_version(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    upgrades = (
        *MIGRATIONS,
        Migration(2, ("CREATE TABLE migration_v2 (id INTEGER)",)),
        Migration(
            3,
            (
                "CREATE TABLE migration_v3 (id INTEGER)",
                "CREATE TABLE migration_v3 (id INTEGER)",
            ),
        ),
    )
    with SessionStore.open(path) as store:
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            apply_migrations(store.connection, upgrades)

        version = store.connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert version == 2
        assert "migration_v2" in tables
        assert "migration_v3" not in tables


def test_readonly_store_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    with SessionStore.open(path) as store:
        session_id = store.create_session(_session())
    with (
        SessionStore.open(path, readonly=True) as store,
        pytest.raises(sqlite3.OperationalError),
    ):
        store.add_events(session_id, [StoredEvent(1, "weather", 1.0)])


def test_readonly_open_requires_an_existing_database(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="No existe la base de datos"):
        SessionStore.open(tmp_path / "missing.db", readonly=True)


def test_readonly_open_rejects_a_database_without_a_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()

    with pytest.raises(StoreError, match="se requiere v"):
        SessionStore.open(path, readonly=True)


def test_readonly_open_rejects_a_schema_missing_required_tables(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.db"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()
    connection.close()

    with pytest.raises(StoreError, match="faltan tablas"):
        SessionStore.open(path, readonly=True)


def test_lap_record_fields_have_a_column_or_an_explicit_exclusion(tmp_path: Path) -> None:
    names = {field.name for field in fields(LapRecord)}
    assert names == set(LAP_COLUMN_TYPES) | set(LAP_COLUMN_EXCLUSIONS)
    with SessionStore.open(tmp_path / "sessions.db") as store:
        columns = {
            row["name"]
            for row in store.connection.execute("PRAGMA table_info(laps)")
            if row["name"] != "session_id"
        }
    assert names == columns | set(LAP_COLUMN_EXCLUSIONS)


def test_reads_work_while_another_connection_writes(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    SessionStore.open(path).close()
    with SessionStore.open(path) as initializer:
        session_id = initializer.create_session(_session())
    with SessionStore.open(path, readonly=True) as reader:
        assert reader.get_session(session_id) is not None
        errors: list[BaseException] = []
        finished = threading.Event()

        def write_many() -> None:
            # Un escritor por conexión: la conexión se crea y se usa en este hilo.
            try:
                with SessionStore.open(path) as writer:
                    for index in range(200):
                        writer.add_events(session_id, [StoredEvent(index, "weather", float(index))])
            except BaseException as exc:  # pragma: no cover - fallo del hilo
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=write_many)
        thread.start()
        while not finished.is_set():
            list(reader.iter_events(session_id))
        thread.join(timeout=5)
        if errors:
            raise errors[0]
        assert len(list(reader.iter_events(session_id))) == 200


def test_session_info_validation() -> None:
    with pytest.raises(ValueError, match="64 bits"):
        SessionInfo(session_uid=2**64, source="live", started_at_utc="x")
    with pytest.raises(ValueError, match="source"):
        SessionInfo(session_uid=1, source="bad", started_at_utc="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="id"):
        SessionInfo(session_uid=1, source="live", started_at_utc="x", id=0)


def test_recording_config_defaults_and_toml(tmp_path: Path) -> None:
    cfg = AppConfig()
    assert cfg.recording == RecordingConfig()
    assert not cfg.recording.enabled
    assert cfg.recording.database == Path("captures/sessions.db")
    assert cfg.recording.metrics_interval_s == 1.0
    assert cfg.recording.queue_size == 1000
    assert cfg.recording.batch_size == 100
    assert cfg.recording.flush_interval_s == 0.5

    path = tmp_path / "config.toml"
    path.write_text(
        "[recording]\nenabled = false\n"
        'database = "data/race.db"\nmetrics_interval_s = 2.0\n'
        "queue_size = 10\nbatch_size = 5\nflush_interval_s = 0.25\n",
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert not loaded.recording.enabled
    assert loaded.recording.database == Path("data/race.db")
    assert loaded.recording.metrics_interval_s == 2.0
    assert loaded.recording.queue_size == 10
    assert loaded.recording.batch_size == 5
    assert loaded.recording.flush_interval_s == 0.25

    path.write_text("[recording]\nqueue_size = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="recording.queue_size"):
        load_config(path)
