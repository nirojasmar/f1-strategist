"""Esquema versionado del almacén SQLite.

La versión del esquema vive en ``PRAGMA user_version``. Cada entrada de
:data:`MIGRATIONS` añade un escalón y se aplica en orden; una base creada por una
versión más nueva de la herramienta se rechaza en lugar de modificarse.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

#: Versión que entiende este código.
SCHEMA_VERSION = 6


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

#: La instantánea final de métricas permite exportar el JSON tal cual se publicó.
_V2_STATEMENTS: tuple[str, ...] = ("ALTER TABLE sessions ADD COLUMN final_metrics_json TEXT",)

#: Enlace del fin de semana: tabla ``weekends`` y columnas de enlace en ``sessions``.
#:
#: ``weekends`` solo existe para los fines de semana enlazados (identificadores del
#: juego distintos de cero o agrupación manual). Un fin de semana nunca se crea por
#: el mero hecho de grabar una sesión: los identificadores a cero significan «sin
#: enlace». ``sessions.weekend_id`` queda a NULL en ese caso.
_V3_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE weekends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT,
        track TEXT,
        weekend_link_id INTEGER,
        season_link_id INTEGER,
        source TEXT NOT NULL CHECK (source IN ('game_ids', 'manual')),
        created_at_utc TEXT NOT NULL
    )
    """,
    "ALTER TABLE sessions ADD COLUMN season_link_id INTEGER",
    "ALTER TABLE sessions ADD COLUMN weekend_link_id INTEGER",
    "ALTER TABLE sessions ADD COLUMN session_link_id INTEGER",
    "ALTER TABLE sessions ADD COLUMN weekend_structure_json TEXT",
    "ALTER TABLE sessions ADD COLUMN weekend_id INTEGER REFERENCES weekends(id) ON DELETE SET NULL",
    """
    CREATE UNIQUE INDEX idx_weekends_game_link
        ON weekends(season_link_id, weekend_link_id)
        WHERE weekend_link_id IS NOT NULL AND weekend_link_id <> 0
    """,
    "CREATE INDEX idx_sessions_weekend_id ON sessions(weekend_id)",
)

#: Resúmenes por tanda e instantáneas del inventario de neumáticos del piloto.
#:
#: Se guardan agregados, no vueltas adicionales: un fin de semana entero cabe en unas
#: pocas decenas de filas. ``tyre_inventory_snapshots`` distingue la instantánea del
#: final de cada sesión de la del comienzo de la carrera, que es la que documenta con
#: qué juegos se arrancó.
_V4_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE stint_summaries (
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        car_index INTEGER NOT NULL,
        stint_index INTEGER NOT NULL,
        start_lap INTEGER NOT NULL,
        end_lap INTEGER,
        laps INTEGER NOT NULL,
        compound TEXT,
        visual_compound TEXT,
        clean_laps INTEGER NOT NULL,
        tyre_age_start INTEGER,
        tyre_age_end INTEGER,
        fuel_start_kg REAL,
        fuel_end_kg REAL,
        pace_mean_s REAL,
        pace_mean_fuel_corrected_s REAL,
        pace_trend_s_per_lap REAL,
        pace_trend_fuel_corrected_s_per_lap REAL,
        wear_start_pct REAL,
        wear_end_pct REAL,
        wear_rate_pct_per_lap REAL,
        track_temperature_mean_c REAL,
        weather_json TEXT NOT NULL DEFAULT '[]',
        representative INTEGER NOT NULL DEFAULT 0,
        quality TEXT NOT NULL,
        PRIMARY KEY (session_id, car_index, stint_index)
    )
    """,
    """
    CREATE TABLE tyre_inventory_snapshots (
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('session_end', 'race_start')),
        set_index INTEGER NOT NULL,
        actual_tyre_compound TEXT,
        visual_tyre_compound TEXT,
        wear_pct REAL NOT NULL,
        available INTEGER,
        lifespan_laps INTEGER NOT NULL,
        usable_life_laps INTEGER NOT NULL,
        lap_delta_time_ms INTEGER NOT NULL,
        recommended_session TEXT,
        fitted INTEGER,
        PRIMARY KEY (session_id, kind, set_index)
    )
    """,
)

#: Perfil del fin de semana exacto con el que arrancó la carrera.
#:
#: Se guarda el JSON canónico y su huella para que una reproducción use el mismo
#: perfil aunque los datos del fin de semana hayan cambiado después.
_V5_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE sessions ADD COLUMN profile_json TEXT",
    "ALTER TABLE sessions ADD COLUMN profile_hash TEXT",
)

_V6_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE sessions ADD COLUMN strategy_fingerprint TEXT",
    """
    CREATE TABLE strategy_evaluations (
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL CHECK (seq > 0),
        t_sim_s REAL NOT NULL,
        lap INTEGER NOT NULL CHECK (lap > 0),
        action TEXT NOT NULL,
        recommended_key TEXT NOT NULL,
        gain_vs_baseline_s REAL,
        gain_low_s REAL,
        gain_high_s REAL,
        confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
        data_quality TEXT NOT NULL,
        inputs_hash TEXT NOT NULL,
        profile_hash TEXT,
        evaluation_json TEXT NOT NULL,
        PRIMARY KEY (session_id, seq)
    )
    """,
)

#: Migraciones ordenadas; el índice 0 lleva el esquema a la versión 1.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, statements=_V1_STATEMENTS),
    Migration(version=2, statements=_V2_STATEMENTS),
    Migration(version=3, statements=_V3_STATEMENTS),
    Migration(version=4, statements=_V4_STATEMENTS),
    Migration(version=5, statements=_V5_STATEMENTS),
    Migration(version=6, statements=_V6_STATEMENTS),
)


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
    """Comprueba versión y tablas requeridas para lectura, sin migrar."""
    current = _read_user_version(connection)
    if current > SCHEMA_VERSION:
        raise StoreError(
            f"La base de datos usa el esquema v{current}, más nuevo que el admitido "
            f"por esta versión de f1-strategist (v{SCHEMA_VERSION}). Actualiza la herramienta."
        )
    if current < SCHEMA_VERSION:
        raise StoreError(
            f"La base de datos usa el esquema v{current}; se requiere v{SCHEMA_VERSION}. "
            "Ábrela en modo escritura para aplicar las migraciones antes de leerla."
        )
    required_tables = {
        "sessions",
        "participants",
        "laps",
        "events",
        "metric_samples",
        "app_errors",
        "weekends",
        "stint_summaries",
        "tyre_inventory_snapshots",
        "strategy_evaluations",
    }
    existing_tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing_tables = sorted(required_tables - existing_tables)
    if missing_tables:
        raise StoreError(
            f"La base de datos declara el esquema v{current}, pero faltan tablas: "
            f"{', '.join(missing_tables)}."
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
