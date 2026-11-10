"""Migración v2/v3, enlace de sesiones y comando ``weekends`` (Fase 3.5, P1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from f1_strategist.cli import main
from f1_strategist.metrics.stint_summary import StintSummary
from f1_strategist.metrics.types import Quality
from f1_strategist.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SessionInfo,
    SessionStore,
    StoreError,
    apply_migrations,
)
from f1_strategist.weekend_profile import WeekendProfile


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
        "tool_version": "0.1.0",
        "parser_version": "1",
        "config_hash": "b" * 64,
        "config_json": "{}",
    }
    values.update(overrides)
    return SessionInfo(**values)  # type: ignore[arg-type]


def test_a_v1_database_migrates_without_loss(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    try:
        apply_migrations(connection, MIGRATIONS[:1])
        connection.execute(
            "INSERT INTO sessions "
            "(session_uid, source, started_at_utc, track, session_type, total_laps, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("1234", "live", "2026-11-05T10:00:00Z", "monza", "race", 13, "c" * 64),
        )
        connection.execute(
            "INSERT INTO participants (session_id, car_index, name, team, ai_controlled) "
            "VALUES (1, 0, 'Piloto', 'Escudería', 0)"
        )
        connection.commit()
    finally:
        connection.close()

    with SessionStore.open(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        session = store.get_session(1)
        assert session is not None
        assert session.session_uid == 1234
        assert session.track == "monza"
        assert session.session_type == "race"
        assert session.total_laps == 13
        assert session.fingerprint == "c" * 64
        assert session.final_metrics_json is None
        assert session.season_link_id is None
        assert session.weekend_link_id is None
        assert session.session_link_id is None
        assert session.weekend_structure_json is None
        assert session.weekend_id is None
        assert store.connection.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 1
        assert store.list_weekends() == []


def test_a_v2_database_migrates_without_loss(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    connection = sqlite3.connect(path)
    try:
        apply_migrations(connection, MIGRATIONS[:2])
        connection.execute(
            "INSERT INTO sessions "
            "(session_uid, source, started_at_utc, track, session_type, total_laps, "
            "fingerprint, final_metrics_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("1234", "ingest", "2026-11-05T10:00:00Z", "spa", "race", 44, "d" * 64, '{"a": 1}'),
        )
        connection.commit()
    finally:
        connection.close()

    with SessionStore.open(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        session = store.get_session(1)
        assert session is not None
        assert session.source == "ingest"
        assert session.track == "spa"
        assert session.total_laps == 44
        assert session.final_metrics_json == '{"a": 1}'
        assert session.weekend_id is None
        assert store.list_weekends() == []


def test_zero_identifiers_are_stored_as_null() -> None:
    session = _session(season_link_id=0, weekend_link_id=0, session_link_id=0)
    assert session.season_link_id is None
    assert session.weekend_link_id is None
    assert session.session_link_id is None


def test_a_session_without_identifiers_creates_no_weekend(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        assert store.get_session(session_id).weekend_id is None  # type: ignore[union-attr]
        assert store.list_weekends() == []
        assert [session.id for session in store.unlinked_sessions()] == [session_id]


def test_sessions_with_game_identifiers_share_a_weekend(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        practice = store.create_session(
            _session(
                session_uid=1,
                session_type="practice",
                season_link_id=7,
                weekend_link_id=42,
                session_link_id=1,
                weekend_structure_json='["practice","qualifying","race"]',
            )
        )
        race = store.create_session(
            _session(session_uid=2, season_link_id=7, weekend_link_id=42, session_link_id=3)
        )
        other = store.create_session(
            _session(session_uid=3, season_link_id=7, weekend_link_id=99, session_link_id=1)
        )

        weekends = store.list_weekends()
        assert len(weekends) == 2
        weekend = weekends[0]
        assert weekend.source == "game_ids"
        assert weekend.weekend_link_id == 42
        assert weekend.season_link_id == 7
        assert weekend.track == "monza"
        assert weekend.label is not None
        assert [session.id for session in store.list_weekend_sessions(weekend.id)] == [
            practice,
            race,
        ]
        stored = store.get_session(practice)
        assert stored is not None
        assert stored.weekend_id == weekend.id
        assert stored.session_link_id == 1
        assert stored.weekend_structure_json == '["practice","qualifying","race"]'
        assert store.get_session(other).weekend_id == weekends[1].id  # type: ignore[union-attr]
        assert store.find_weekend(weekend_link_id=42, season_link_id=7) == weekend


def test_a_session_joins_its_game_weekend_when_metadata_arrives_later(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        session_id = store.create_session(_session())
        assert store.list_weekends() == []

        store.update_session(
            session_id,
            _session(
                season_link_id=7,
                weekend_link_id=42,
                session_link_id=2,
                weekend_structure_json='["practice","race"]',
            ),
        )

        weekends = store.list_weekends()
        assert len(weekends) == 1
        assert weekends[0].source == "game_ids"
        stored = store.get_session(session_id)
        assert stored is not None
        assert stored.weekend_id == weekends[0].id
        assert stored.weekend_link_id == 42
        assert stored.weekend_structure_json == '["practice","race"]'

        # Un segundo aviso con los mismos identificadores no duplica el fin de semana.
        store.update_session(session_id, _session(season_link_id=7, weekend_link_id=42))
        assert len(store.list_weekends()) == 1


def test_manual_link_groups_sessions_of_the_same_track(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice"))
        qualifying = store.create_session(_session(session_uid=2, session_type="qualifying"))

        weekend_id = store.link_sessions([practice, qualifying], label="Monza")

        weekend = store.get_weekend(weekend_id)
        assert weekend is not None
        assert weekend.source == "manual"
        assert weekend.label == "Monza"
        assert weekend.track == "monza"
        assert weekend.weekend_link_id is None
        assert [session.id for session in store.list_weekend_sessions(weekend_id)] == [
            practice,
            qualifying,
        ]
        assert store.unlinked_sessions() == []


def test_manual_link_is_idempotent_and_absorbs_sessions(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice"))
        qualifying = store.create_session(_session(session_uid=2, session_type="qualifying"))
        race = store.create_session(_session(session_uid=3, session_type="race"))

        weekend_id = store.link_sessions([practice, qualifying], label="Monza")
        assert store.link_sessions([practice, qualifying], label="Monza GP") == weekend_id
        assert store.get_weekend(weekend_id).label == "Monza GP"  # type: ignore[union-attr]

        regrouped = store.link_sessions([practice, qualifying, race])
        assert regrouped == weekend_id
        assert store.get_weekend(weekend_id).label == "Monza GP"  # type: ignore[union-attr]
        assert [session.id for session in store.list_weekend_sessions(regrouped)] == [
            practice,
            qualifying,
            race,
        ]


def test_manual_link_absorbs_a_previously_separate_weekend(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice"))
        qualifying = store.create_session(_session(session_uid=2, session_type="qualifying"))
        race = store.create_session(_session(session_uid=3, session_type="race"))

        first = store.link_sessions([practice])
        second = store.link_sessions([qualifying])
        assert first != second

        merged = store.link_sessions([practice, qualifying, race])
        assert store.get_weekend(first) is None
        assert store.get_weekend(second) is None
        assert [session.id for session in store.list_weekend_sessions(merged)] == [
            practice,
            qualifying,
            race,
        ]
        assert len(store.list_weekends()) == 1


def test_manual_link_rejects_sessions_from_different_tracks(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        monza = store.create_session(_session(session_uid=1))
        spa = store.create_session(_session(session_uid=2, track="spa"))

        with pytest.raises(StoreError, match="circuitos distintos"):
            store.link_sessions([monza, spa])

        assert store.list_weekends() == []
        assert len(store.unlinked_sessions()) == 2


def test_manual_link_requires_known_tracks_and_existing_sessions(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        without_track = store.create_session(_session(session_uid=1, track=None))
        with pytest.raises(StoreError, match="no tiene circuito conocido"):
            store.link_sessions([without_track])
        with pytest.raises(StoreError, match="No existe la sesión 99"):
            store.link_sessions([99])
        with pytest.raises(StoreError, match="al menos una sesión"):
            store.link_sessions([])


def test_unlink_removes_a_weekend_left_without_sessions(tmp_path: Path) -> None:
    with SessionStore.open(tmp_path / "sessions.db") as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice"))
        race = store.create_session(_session(session_uid=2))
        weekend_id = store.link_sessions([practice, race])

        assert store.unlink_session(practice) == weekend_id
        assert store.get_weekend(weekend_id) is not None
        assert store.get_session(practice).weekend_id is None  # type: ignore[union-attr]

        assert store.unlink_session(race) == weekend_id
        assert store.get_weekend(weekend_id) is None
        assert len(store.unlinked_sessions()) == 2
        assert store.unlink_session(race) is None


def test_weekends_cli_lists_links_and_unlinks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    with SessionStore.open(db) as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice"))
        race = store.create_session(_session(session_uid=2))

    assert main(["weekends", "list", "--db", str(db)]) == 0
    output = capsys.readouterr().out
    assert "No hay fines de semana enlazados." in output
    assert "Sesiones sin fin de semana: 2" in output

    assert main(["weekends", "link", str(practice), str(race), "--db", str(db)]) == 0
    assert "enlazadas en el fin de semana 1" in capsys.readouterr().out

    assert main(["weekends", "list", "--db", str(db)]) == 0
    output = capsys.readouterr().out
    assert "Fines de semana" in output
    assert "manual" in output
    assert "monza" in output

    assert main(["weekends", "show", "1", "--db", str(db)]) == 0
    output = capsys.readouterr().out
    assert "Fin de semana 1" in output
    assert "practice" in output
    assert "Sesiones del fin de semana" in output

    assert main(["weekends", "unlink", str(practice), "--db", str(db)]) == 0
    assert "separada del fin de semana 1" in capsys.readouterr().out

    assert main(["weekends", "show", "99", "--db", str(db)]) == 2
    assert "No existe el fin de semana 99" in capsys.readouterr().err


def test_weekends_link_cli_rejects_different_tracks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    with SessionStore.open(db) as store:
        monza = store.create_session(_session(session_uid=1))
        spa = store.create_session(_session(session_uid=2, track="spa"))

    assert main(["weekends", "link", str(monza), str(spa), "--db", str(db)]) == 2
    assert "circuitos distintos" in capsys.readouterr().err


def _registered_profile() -> tuple[str, str]:
    profile = WeekendProfile()
    return profile.to_json(), profile.hash


def _summary() -> StintSummary:
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


def test_weekend_show_renders_the_registered_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    profile_json, profile_hash = _registered_profile()
    with SessionStore.open(db) as store:
        session_id = store.create_session(
            _session(profile_json=profile_json, profile_hash=profile_hash)
        )

    assert main(["weekend", "show", "--session", str(session_id), "--db", str(db)]) == 0
    output = capsys.readouterr().out
    assert f"Perfil del fin de semana de la sesión {session_id}" in output
    assert "registrado al empezar la carrera" in output
    assert profile_hash[:12] in output


def test_weekend_show_calculates_a_profile_when_none_is_registered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    with SessionStore.open(db) as store:
        practice = store.create_session(_session(session_uid=1, session_type="practice"))
        store.add_stint_summaries(practice, [_summary()])
        race = store.create_session(_session(session_uid=2))
        weekend_id = store.link_sessions([practice, race])

    assert main(["weekend", "show", "--session", str(race), "--db", str(db)]) == 0
    output = capsys.readouterr().out
    assert "calculado ahora (no registrado)" in output
    assert str(weekend_id) in output
    assert "c3" in output


def test_weekend_show_explains_that_a_session_has_no_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    with SessionStore.open(db) as store:
        session_id = store.create_session(_session(session_uid=1))

    assert main(["weekend", "show", "--session", str(session_id), "--db", str(db)]) == 0
    assert "no tiene perfil registrado ni un fin de semana enlazado" in capsys.readouterr().out


def test_weekend_show_without_session_uses_the_last_registered_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    profile_json, profile_hash = _registered_profile()
    with SessionStore.open(db) as store:
        store.create_session(_session(session_uid=1))
        latest = store.create_session(
            _session(session_uid=2, profile_json=profile_json, profile_hash=profile_hash)
        )

    assert main(["weekend", "show", "--db", str(db)]) == 0
    output = capsys.readouterr().out
    assert f"Perfil del fin de semana de la sesión {latest}" in output
    assert "registrado al empezar la carrera" in output


def test_weekend_show_without_any_profile_reports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    with SessionStore.open(db) as store:
        store.create_session(_session(session_uid=1))

    assert main(["weekend", "show", "--db", str(db)]) == 0
    assert "No hay ninguna sesión con un perfil del fin de semana registrado." in (
        capsys.readouterr().out
    )


def test_weekend_show_rejects_a_missing_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sessions.db"
    with SessionStore.open(db):
        pass

    assert main(["weekend", "show", "--session", "99", "--db", str(db)]) == 2
    assert "No existe la sesión 99" in capsys.readouterr().err
