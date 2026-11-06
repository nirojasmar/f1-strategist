"""Esquema versionado del almacén SQLite.

La versión del esquema vive en ``PRAGMA user_version``. Cada entrada de
:data:`MIGRATIONS` añade un escalón y se aplica en orden; una base creada por una
versión más nueva de la herramienta se rechaza en lugar de modificarse.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

#: Versión que entiende este código.
SCHEMA_VERSION = 1


class StoreError(Exception):
    """Error de esquema, migración o acceso al almacén."""


@dataclass(frozen=True, slots=True)
class Migration:
    """Conjunto de sentencias DDL que llevan el esquema a ``version``."""

    version: int
    statements: tuple[str, ...]


#: Columnas de ``laps`` que viajan como JSON (tuplas y conjuntos).
LAP_JSON_COLUMNS: frozenset[str] = frozenset(
    {
        "tyre_wear_start_pct",
        "tyre_wear_end_pct",
        "track_status_seen",
        "weather_seen",
        "flags",
    }
)

#: Columnas de ``laps`` que se guardan como 0/1 (o NULL cuando el valor es None).
LAP_BOOL_COLUMNS: frozenset[str] = frozenset({"valid", "pit_entry", "pit_exit", "closed"})

#: Tipo SQLite de cada campo de :class:`~f1_strategist.metrics.history.LapRecord`.
#: ``car_index`` y ``lap_number`` forman parte de la clave primaria.
LAP_COLUMN_TYPES: dict[str, str] = {
    "car_index": "INTEGER NOT NULL",
    "lap_number": "INTEGER NOT NULL",
    "lap_time_s": "REAL",
    "lap_time_source": "TEXT",
    "sector1_s": "REAL",
    "sector2_s": "REAL",
    "sector3_s": "REAL",
    "valid": "INTEGER",
    "compound": "TEXT",
    "tyre_age_start": "INTEGER",
    "tyre_age_end": "INTEGER",
    "fuel_start_kg": "REAL",
    "fuel_end_kg": "REAL",
    "tyre_wear_start_pct": "TEXT",
    "tyre_wear_end_pct": "TEXT",
    "position_start": "INTEGER",
    "position_end": "INTEGER",
    "gap_ahead_start_s": "REAL",
    "gap_ahead_end_s": "REAL",
    "ers_harvested_j": "REAL",
    "ers_deployed_j": "REAL",
    "pit_entry": "INTEGER NOT NULL DEFAULT 0",
    "pit_exit": "INTEGER NOT NULL DEFAULT 0",
    "pit_lane_time_s": "REAL",
    "pit_stop_time_s": "REAL",
    "track_status_seen": "TEXT NOT NULL DEFAULT '[]'",
    "weather_seen": "TEXT NOT NULL DEFAULT '[]'",
    "track_temperature_c": "REAL",
    "traffic_fraction": "REAL",
    "damage_max_delta_pct": "REAL",
    "flags": "TEXT NOT NULL DEFAULT '[]'",
    "closed": "INTEGER NOT NULL DEFAULT 0",
    "started_at_monotonic": "REAL",
    "ended_at_monotonic": "REAL",
}

#: Campos de ``LapRecord`` que se omite a propósito al persistir la vuelta.
#: Vacío a día de hoy: todas las columnas del registro tienen sitio en ``laps``.
LAP_COLUMN_EXCLUSIONS: frozenset[str] = frozenset()

_LAP_TABLE_SQL = (
    "CREATE TABLE laps (\n"
    "    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,\n    "
)
_LAP_TABLE_SQL += ",\n    ".join(
    f"{name} {declaration}" for name, declaration in LAP_COLUMN_TYPES.items()
)
_LAP_TABLE_SQL += ",\n    PRIMARY KEY (session_id, car_index, lap_number)\n)"

_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_uid TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('live', 'ingest')),
        started_at_utc TEXT NOT NULL,
        ended_at_utc TEXT,
        incomplete INTEGER NOT NULL DEFAULT 0,
        game_format TEXT,
        game_year INTEGER,
        game_version TEXT,
        track TEXT,
        session_type TEXT,
        total_laps INTEGER,
        capture_path TEXT,
        capture_sha256 TEXT,
        tool_version TEXT,
        parser_version TEXT,
        config_hash TEXT,
        config_json TEXT,
        fingerprint TEXT
    )
    """,
    """
    CREATE TABLE participants (
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        car_index INTEGER NOT NULL,
        name TEXT,
        team TEXT,
        ai_controlled INTEGER,
        PRIMARY KEY (session_id, car_index)
    )
    """,
    _LAP_TABLE_SQL,
    """
    CREATE TABLE metric_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        t_session_s REAL,
        t_sim_s REAL NOT NULL,
        lap INTEGER,
        key TEXT NOT NULL,
        value_num REAL,
        value_text TEXT,
        unit TEXT NOT NULL,
        quality TEXT NOT NULL,
        sample_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL,
        t_sim_s REAL NOT NULL,
        car_index INTEGER,
        lap_number INTEGER,
        value TEXT,
        previous_value TEXT
    )
    """,
    """
    CREATE TABLE app_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        t_sim_s REAL NOT NULL,
        code TEXT NOT NULL,
        params_json TEXT
    )
    """,
    "CREATE INDEX idx_sessions_started_at ON sessions(started_at_utc)",
    "CREATE INDEX idx_sessions_capture_sha256 ON sessions(capture_sha256)",
    "CREATE INDEX idx_metric_samples_lookup ON metric_samples(session_id, key, id)",
    "CREATE INDEX idx_events_lookup ON events(session_id, seq)",
    "CREATE INDEX idx_app_errors_session ON app_errors(session_id)",
)

#: Migraciones ordenadas; el índice 0 lleva el esquema a la versión 1.
MIGRATIONS: tuple[Migration, ...] = (Migration(version=1, statements=_V1_STATEMENTS),)


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> int:
    """Aplica las migraciones pendientes y devuelve la versión resultante.

    Cada migración se ejecuta en su propia transacción de escritura inmediata. La
    versión se relee dentro de la transacción para no repetir una migración que
    otro proceso haya aplicado; ante cualquier fallo se revierte DDL y versión.
    Una base con una versión posterior a la última migración conocida se rechaza
    sin tocarla.
    """
    ordered = _validate_migrations(migrations)
    current = _read_user_version(connection)
    latest = ordered[-1].version
    if current > latest:
        raise StoreError(
            f"La base de datos usa el esquema v{current}, más nuevo que el admitido "
            f"por esta versión de f1-strategist (v{latest}). Actualiza la herramienta."
        )
    for migration in ordered:
        if migration.version <= current:
            continue
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _read_user_version(connection)
            if migration.version <= current:
                connection.rollback()
                continue
            for statement in migration.statements:
                connection.execute(statement)
            # user_version no admite parámetros; ya está validado como entero.
            connection.execute(f"PRAGMA user_version = {migration.version}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        current = migration.version
    return current


def check_supported_version(connection: sqlite3.Connection) -> int:
    """Comprueba que la base no sea más nueva que el código (sin migrar)."""
    current = _read_user_version(connection)
    if current > SCHEMA_VERSION:
        raise StoreError(
            f"La base de datos usa el esquema v{current}, más nuevo que el admitido "
            f"por esta versión de f1-strategist (v{SCHEMA_VERSION}). Actualiza la herramienta."
        )
    return current


def _validate_migrations(migrations: tuple[Migration, ...]) -> tuple[Migration, ...]:
    if not migrations:
        raise StoreError("No hay migraciones definidas")
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    for expected, migration in enumerate(ordered, start=1):
        if migration.version != expected:
            raise StoreError(
                f"Migraciones no contiguas: se esperaba la v{expected} y llegó la "
                f"v{migration.version}"
            )
    return ordered


def _read_user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        return 0
    return int(row[0])
