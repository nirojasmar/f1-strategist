from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from f1_strategist.cli import main
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.export import render_metrics_export
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord
from f1_strategist.store import (
    MetricSample,
    Participant,
    SessionInfo,
    SessionStore,
    StoredError,
    StoredEvent,
    StoreError,
)
from f1_strategist.store.queries import (
    held_metric_values,
    list_session_summaries,
    render_session_export,
    require_session,
    session_events,
    session_history,
    session_laps,
)
from tests.scenarios import build_race_timed
from tests.test_metrics_export import _cars, _patch_parser, _write_capture


def _session_info() -> SessionInfo:
    return SessionInfo(
        session_uid=0xF125,
        source="ingest",
        started_at_utc="2026-11-05T10:00:00Z",
        game_format="F1 25",
        game_year=2025,
        game_version="1.26",
        track="monza",
        session_type="race",
        total_laps=4,
        capture_path="captures/monza.f1cap",
        capture_sha256="abc123",
        tool_version="0.1.0",
        parser_version="1",
        config_hash="cfg123",
        config_json='{"recording": {"enabled": true}}',
        fingerprint="a" * 64,
    )


def _laps() -> list[LapRecord]:
    return [
        LapRecord(
            car_index=0,
            lap_number=1,
            lap_time_s=90.0,
            compound="soft",
            tyre_age_end=2,
            valid=True,
            position_end=1,
            track_status_seen=frozenset({"green"}),
            closed=True,
        ),
        LapRecord(
            car_index=0,
            lap_number=2,
            lap_time_s=91.0,
            compound="soft",
            tyre_age_end=3,
            valid=True,
            position_end=1,
            pit_entry=True,
            pit_stop_time_s=2.5,
            closed=True,
        ),
        LapRecord(
            car_index=1,
            lap_number=1,
            lap_time_s=92.0,
            compound="medium",
            tyre_wear_start_pct=(1.0, 2.0),
            tyre_age_end=5,
            valid=False,
            position_end=2,
            flags=frozenset({"incomplete"}),
            closed=False,
        ),
    ]


def _events() -> list[StoredEvent]:
    return [
        StoredEvent(1, "overtake", 101.0, car_index=0, lap_number=1, value="1"),
        StoredEvent(2, "pit_entry", 111.0, car_index=1, lap_number=1, value="soft"),
    ]


def _samples() -> list[MetricSample]:
    return [
        MetricSample(100.0, "fuel.margin_laps_at_finish", "ok", "lap", 3, 100.0, 1, 1.5),
        MetricSample(110.0, "fuel.margin_laps_at_finish", "ok", "lap", 4, 110.0, 2, 1.0),
        MetricSample(120.0, "fuel.margin_laps_at_finish", "low", "lap", 5, 120.0, 3, None, "hola"),
        MetricSample(105.0, "pace.last_lap_s", "ok", "s", 2, 105.0, 1, 90.0),
        MetricSample(106.0, "tyres.phase", "ok", "", 1, 106.0, 1, None, "true"),
        MetricSample(107.0, "tyres.compounds", "ok", "", 1, 107.0, 1, None, '["soft","medium"]'),
        MetricSample(125.0, "env.track_temp_c", "ok", "°C", 1, 125.0, 4, 30.0),
        MetricSample(130.0, "progress.lap", "ok", "lap", 1, 130.0, None, 4.0),
    ]


def _seed(db: Path) -> int:
    with SessionStore.open(db) as store:
        session_id = store.create_session(_session_info())
        store.add_participants(
            session_id,
            [
                Participant(0, "Driver A", "Team A", False),
                Participant(1, "Driver B", "Team B", True),
            ],
        )
        store.upsert_laps(session_id, _laps())
        store.add_events(session_id, _events())
        store.add_metric_samples(session_id, _samples())
        store.add_errors(session_id, [StoredError(140.0, "test_error", '{"detail":"x"}')])
        store.finish_session(
            session_id,
            ended_at_utc="2026-11-05T11:00:00Z",
            fingerprint="a" * 64,
        )
    return session_id


def _history() -> LapHistorySnapshot:
    laps = _laps()
    return LapHistorySnapshot(
        records={0: (laps[0], laps[1]), 1: (laps[2],)},
        session_uid=0xF125,
    )


def test_list_session_summaries_counts_rows(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    with SessionStore.open(db, readonly=True) as store:
        summaries = list_session_summaries(store)

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.session.id == session_id
    assert summary.laps == 3
    assert summary.events == 2
    assert summary.metric_samples == 8
    assert summary.errors == 1
    assert summary.session.fingerprint == "a" * 64


def test_list_session_summaries_is_empty_without_sessions(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    with SessionStore.open(db):
        pass

    with SessionStore.open(db, readonly=True) as store:
        assert list_session_summaries(store) == []


def test_require_session_rejects_unknown_ids(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)

    with (
        SessionStore.open(db, readonly=True) as store,
        pytest.raises(StoreError, match="No existe la sesión 99"),
    ):
        require_session(store, 99)


def test_session_readers_apply_filters(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    with SessionStore.open(db, readonly=True) as store:
        assert [lap.lap_number for lap in session_laps(store, session_id)] == [1, 2, 1]
        assert [lap.lap_number for lap in session_laps(store, session_id, car_index=1)] == [1]
        assert [event.kind for event in session_events(store, session_id)] == [
            "overtake",
            "pit_entry",
        ]
        assert [event.kind for event in session_events(store, session_id, kind="pit_entry")] == [
            "pit_entry"
        ]


def test_held_metric_values_carry_the_last_value_per_lap(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    with SessionStore.open(db, readonly=True) as store:
        held = held_metric_values(store, session_id, key_pattern="fuel.*")

    assert [(item.lap, item.value) for item in held] == [
        (1, 1.5),
        (2, 1.0),
        (3, "hola"),
    ]
    assert {item.quality for item in held} == {"ok", "low"}
    assert held[2].sample_count == 5
    assert held[2].t_sim_s == 120.0


def test_held_metric_values_decode_values_and_filter_by_lap(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    with SessionStore.open(db, readonly=True) as store:
        held = held_metric_values(store, session_id, key_pattern="tyres.*", lap=1)

    assert [(item.key, item.value) for item in held] == [
        ("tyres.compounds", ("soft", "medium")),
        ("tyres.phase", True),
    ]


def test_held_metric_values_return_nothing_outside_the_range(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    with SessionStore.open(db, readonly=True) as store:
        assert held_metric_values(store, session_id, key_pattern="env.*", lap=3) == []
        assert held_metric_values(store, session_id, key_pattern="unknown.*") == []


def test_session_history_round_trips_the_stored_laps(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    with SessionStore.open(db, readonly=True) as store:
        history = session_history(store, session_id)

    assert history.session_uid == 0xF125
    assert [(lap.car_index, lap.lap_number) for lap in history.records[0]] == [(0, 1), (0, 2)]
    assert history.records[0][0].track_status_seen == frozenset({"green"})
    assert history.records[1][0].tyre_wear_start_pct == (1.0, 2.0)
    assert history.records[1][0].flags == frozenset({"incomplete"})


def test_export_matches_the_metrics_command_for_laps(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    with SessionStore.open(db, readonly=True) as store:
        jsonl_text = render_session_export(store, session_id, "jsonl")
        csv_text = render_session_export(store, session_id, "csv")

    expected_jsonl = render_metrics_export(MetricsSnapshot(), _history(), "jsonl")
    expected_csv = render_metrics_export(MetricsSnapshot(), _history(), "csv")
    assert jsonl_text == expected_jsonl
    assert csv_text == expected_csv


def test_export_json_rebuilds_the_final_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    with SessionStore.open(db, readonly=True) as store:
        payload = json.loads(render_session_export(store, session_id, "json"))

    assert payload["session_uid"] == 0xF125
    assert payload["computed_at_monotonic"] == 130.0
    fuel = payload["metrics"]["fuel.margin_laps_at_finish"]
    assert fuel["value"] == "hola"
    assert fuel["quality"] == "low"
    assert fuel["sample_count"] == 5
    assert fuel["window"] == {"kind": "instant", "size": None}
    assert payload["metrics"]["tyres.phase"]["value"] is True
    assert payload["metrics"]["tyres.compounds"]["value"] == ["soft", "medium"]


def test_export_rejects_unknown_formats(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    with (
        SessionStore.open(db, readonly=True) as store,
        pytest.raises(ValueError, match="Formato de exportación"),
    ):
        render_session_export(store, session_id, "xml")


def test_sessions_cli_lists_sessions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)

    assert main(["sessions", "list", "--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "monza" in out
    assert "race" in out
    assert "ingest" in out
    assert "aaaaaaaaaaaa…" in out


def test_sessions_cli_list_reports_an_empty_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "empty.db"
    with SessionStore.open(db):
        pass

    assert main(["sessions", "list", "--db", str(db)]) == 0
    assert "No hay sesiones grabadas." in capsys.readouterr().out


def test_sessions_cli_shows_metadata(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)

    assert main(["sessions", "show", "1", "--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "Sesión 1" in out
    assert "abc123" in out
    assert "0.1.0" in out
    assert "Huella" in out
    assert "a" * 20 in out


def test_sessions_cli_filters_laps_and_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)

    assert main(["sessions", "laps", "1", "--car", "1", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "92.000" in out
    assert "90.000" not in out

    assert main(["sessions", "events", "1", "--kind", "overtake", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "overtake" in out
    assert "pit_entry" not in out


def test_sessions_cli_metrics_uses_a_glob_and_forward_fill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)

    assert main(["sessions", "metrics", "1", "--key", "fuel.*", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "".join(out.split()).count("fuel.margin_laps_at_finish") == 3
    assert "hola" in out

    assert main(["sessions", "metrics", "1", "--key", "fuel.*", "--lap", "2", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "1.0" in out
    assert "1.5" not in out


def test_sessions_cli_reports_empty_filters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)

    assert main(["sessions", "laps", "1", "--car", "9", "--db", str(db)]) == 0
    assert "No hay vueltas para los filtros indicados." in capsys.readouterr().out

    assert main(["sessions", "events", "1", "--kind", "unknown", "--db", str(db)]) == 0
    assert "No hay eventos para los filtros indicados." in capsys.readouterr().out

    assert main(["sessions", "metrics", "1", "--key", "unknown.*", "--db", str(db)]) == 0
    assert "No hay métricas para los filtros indicados." in capsys.readouterr().out


def test_sessions_cli_exports_to_stdout_and_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)

    assert main(["sessions", "export", str(session_id), "--format", "jsonl", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    with SessionStore.open(db, readonly=True) as store:
        expected = render_session_export(store, session_id, "jsonl")
    assert out == expected

    target = tmp_path / "laps.csv"
    assert (
        main(
            [
                "sessions",
                "export",
                str(session_id),
                "--format",
                "csv",
                "--out",
                str(target),
                "--db",
                str(db),
            ]
        )
        == 0
    )
    assert "Exportación de la sesión 1 escrita en" in capsys.readouterr().out
    with SessionStore.open(db, readonly=True) as store:
        expected = render_session_export(store, session_id, "csv")
    assert target.read_text(encoding="utf-8") == expected


def test_sessions_cli_rejects_unknown_ids_and_formats(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)

    assert main(["sessions", "show", "99", "--db", str(db)]) == 2
    assert "No existe la sesión 99" in capsys.readouterr().err

    assert main(["sessions", "export", "1", "--format", "xml", "--db", str(db)]) == 2
    assert "Formato de exportación" in capsys.readouterr().err


def test_sessions_cli_rejects_a_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.db"

    assert main(["sessions", "list", "--db", str(missing)]) == 2
    assert "No existe la base de datos" in capsys.readouterr().err
    assert not missing.exists()


def test_sessions_cli_refuses_to_overwrite_the_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    session_id = _seed(db)
    original = db.read_bytes()

    assert (
        main(
            [
                "sessions",
                "export",
                str(session_id),
                "--out",
                str(db),
                "--db",
                str(db),
            ]
        )
        == 2
    )

    assert db.read_bytes() == original
    assert "no puede sobrescribir la base de datos" in capsys.readouterr().err


def test_queries_do_not_modify_the_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)
    original = db.read_bytes()

    assert main(["sessions", "list", "--db", str(db)]) == 0
    assert main(["sessions", "show", "1", "--db", str(db)]) == 0
    assert main(["sessions", "laps", "1", "--db", str(db)]) == 0
    assert main(["sessions", "events", "1", "--db", str(db)]) == 0
    assert main(["sessions", "metrics", "1", "--key", "*", "--db", str(db)]) == 0
    assert main(["sessions", "export", "1", "--db", str(db)]) == 0
    capsys.readouterr()

    assert db.read_bytes() == original


def test_sessions_export_matches_metrics_for_the_same_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    packets = list(build_race_timed(_cars(), total_laps=3, samples_per_lap=1))
    _patch_parser(monkeypatch, packets)
    capture = tmp_path / "race.f1cap"
    _write_capture(capture, packets)
    db = tmp_path / "sessions.db"

    assert main(["ingest", str(capture), "--db", str(db)]) == 0
    capsys.readouterr()

    for export_format in ("json", "jsonl", "csv"):
        assert main(["metrics", str(capture), "--format", export_format]) == 0
        expected = capsys.readouterr().out

        assert main(["sessions", "export", "1", "--format", export_format, "--db", str(db)]) == 0
        assert capsys.readouterr().out == expected


def test_sessions_export_rejects_the_capture_file_as_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capture = tmp_path / "race.f1cap"
    capture.write_bytes(b"capture")
    db = tmp_path / "sessions.db"
    with SessionStore.open(db) as store:
        session_id = store.create_session(replace(_session_info(), capture_path=str(capture)))
        store.finish_session(session_id, ended_at_utc="2026-11-05T11:00:00Z")

    assert main(["sessions", "export", "1", "--out", str(capture), "--db", str(db)]) == 2
    assert capture.read_bytes() == b"capture"
    assert "no puede sobrescribir la captura asociada" in capsys.readouterr().err


def test_sessions_export_rejects_hard_links_to_the_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    _seed(db)
    alias = tmp_path / "alias.db"
    os.link(db, alias)

    assert main(["sessions", "export", "1", "--out", str(alias), "--db", str(db)]) == 2
    assert "no puede sobrescribir la base de datos" in capsys.readouterr().err


def test_held_metric_values_use_the_pre_lap_state_without_extrapolating(
    tmp_path: Path,
) -> None:
    db = tmp_path / "sessions.db"
    with SessionStore.open(db) as store:
        session_id = store.create_session(_session_info())
        store.add_metric_samples(
            session_id,
            [
                MetricSample(90.0, "brake.bias", "ok", "", 1, 90.0, None, 55.0),
                MetricSample(100.0, "brake.bias", "ok", "", 1, 100.0, 1, 54.0),
                MetricSample(110.0, "brake.bias", "ok", "", 1, 110.0, 2, 53.0),
                MetricSample(120.0, "progress.lap", "ok", "lap", 1, 120.0, None, 4.0),
            ],
        )
        store.finish_session(session_id, ended_at_utc="2026-11-05T11:00:00Z")

    with SessionStore.open(db, readonly=True) as store:
        held = held_metric_values(store, session_id, key_pattern="brake.*")
        orphan = held_metric_values(store, session_id, key_pattern="progress.*")

    assert [(item.lap, item.value) for item in held] == [(1, 54.0), (2, 53.0)]
    assert orphan == []
