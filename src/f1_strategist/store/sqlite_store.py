"""Acceso al almacén SQLite de sesiones grabadas.

Cada instancia de :class:`SessionStore` posee su propia conexión y debe usarse
desde un único hilo. Las lecturas pueden abrirse en modo solo lectura mientras
otra conexión escribe (WAL).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..metrics.history import LapRecord
from ..metrics.stint_summary import StintSummary
from ..metrics.types import Quality
from .schema import (
    LAP_BOOL_COLUMNS,
    LAP_COLUMN_TYPES,
    LAP_JSON_COLUMNS,
    MIGRATIONS,
    Migration,
    StoreError,
    apply_migrations,
    check_supported_version,
)

BUSY_TIMEOUT_MS = 5000
SessionSource = Literal["live", "ingest"]
SESSION_SOURCES: frozenset[str] = frozenset({"live", "ingest"})
WeekendSource = Literal["game_ids", "manual"]
WEEKEND_SOURCES: frozenset[str] = frozenset({"game_ids", "manual"})
InventoryKind = Literal["session_end", "race_start"]
INVENTORY_KINDS: frozenset[str] = frozenset({"session_end", "race_start"})

_SESSION_COLUMNS: tuple[str, ...] = (
    "id",
    "session_uid",
    "source",
    "started_at_utc",
    "ended_at_utc",
    "incomplete",
    "game_format",
    "game_year",
    "game_version",
    "track",
    "session_type",
    "total_laps",
    "capture_path",
    "capture_sha256",
    "tool_version",
    "parser_version",
    "config_hash",
    "config_json",
    "fingerprint",
    "final_metrics_json",
    "season_link_id",
    "weekend_link_id",
    "session_link_id",
    "weekend_structure_json",
    "weekend_id",
    "profile_json",
    "profile_hash",
)

_WEEKEND_COLUMNS: tuple[str, ...] = (
    "id",
    "label",
    "track",
    "weekend_link_id",
    "season_link_id",
    "source",
    "created_at_utc",
)

_LAP_COLUMNS: tuple[str, ...] = tuple(LAP_COLUMN_TYPES)
_LAP_WRITE_COLUMNS: tuple[str, ...] = ("session_id", *_LAP_COLUMNS)
_LAP_UPDATE_COLUMNS: tuple[str, ...] = tuple(
    column for column in _LAP_COLUMNS if column not in {"car_index", "lap_number"}
)

_UPSERT_LAP_SQL = (
    f"INSERT INTO laps ({', '.join(_LAP_WRITE_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _LAP_WRITE_COLUMNS)}) "
    "ON CONFLICT(session_id, car_index, lap_number) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in _LAP_UPDATE_COLUMNS)
)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Metadatos de una sesión grabada.

    Al crear una sesión, ``id`` se deja a ``None``; las lecturas lo rellenan.
    """

    session_uid: int
    source: SessionSource
    started_at_utc: str
    id: int | None = None
    ended_at_utc: str | None = None
    incomplete: bool = False
    game_format: str | None = None
    game_year: int | None = None
    game_version: str | None = None
    track: str | None = None
    session_type: str | None = None
    total_laps: int | None = None
    capture_path: str | None = None
    capture_sha256: str | None = None
    tool_version: str | None = None
    parser_version: str | None = None
    config_hash: str | None = None
    config_json: str | None = None
    fingerprint: str | None = None
    final_metrics_json: str | None = None
    season_link_id: int | None = None
    weekend_link_id: int | None = None
    session_link_id: int | None = None
    weekend_structure_json: str | None = None
    weekend_id: int | None = None
    profile_json: str | None = None
    profile_hash: str | None = None

    def __post_init__(self) -> None:
        if self.session_uid < 0 or self.session_uid > 2**64 - 1:
            raise ValueError("session_uid debe ser un entero sin signo de 64 bits")
        if self.source not in SESSION_SOURCES:
            raise ValueError(f"source no válido: {self.source!r}")
        if self.id is not None and self.id < 1:
            raise ValueError("id debe ser positivo")
        # Un identificador a cero significa «sin enlace» y se guarda como NULL.
        for name in ("season_link_id", "weekend_link_id", "session_link_id"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                object.__setattr__(self, name, None)
        if self.weekend_id is not None and self.weekend_id < 1:
            raise ValueError("weekend_id debe ser positivo")


@dataclass(frozen=True, slots=True)
class WeekendInfo:
    """Un fin de semana que agrupa sesiones del mismo circuito."""

    id: int
    source: WeekendSource
    created_at_utc: str
    label: str | None = None
    track: str | None = None
    weekend_link_id: int | None = None
    season_link_id: int | None = None

    def __post_init__(self) -> None:
        if self.id < 1:
            raise ValueError("id debe ser positivo")
        if self.source not in WEEKEND_SOURCES:
            raise ValueError(f"source no válido: {self.source!r}")


@dataclass(frozen=True, slots=True)
class Participant:
    """Un coche presente en la sesión."""

    car_index: int
    name: str | None = None
    team: str | None = None
    ai_controlled: bool | None = None

    def __post_init__(self) -> None:
        if self.car_index < 0:
            raise ValueError("car_index no puede ser negativo")


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """Evento de carrera tal como se guarda en la tabla ``events``."""

    seq: int
    kind: str
    t_sim_s: float
    car_index: int | None = None
    lap_number: int | None = None
    value: str | None = None
    previous_value: str | None = None


@dataclass(frozen=True, slots=True)
class MetricSample:
    """Muestra de una métrica; se guarda cuando cambia su valor o calidad."""

    t_sim_s: float
    key: str
    quality: str
    unit: str
    sample_count: int = 0
    t_session_s: float | None = None
    lap: int | None = None
    value_num: float | None = None
    value_text: str | None = None


@dataclass(frozen=True, slots=True)
class StoredError:
    """Error del consumidor asociado a una sesión."""

    t_sim_s: float
    code: str
    params_json: str | None = None


@dataclass(frozen=True, slots=True)
class TyreInventorySnapshot:
    """Una fila de ``tyre_inventory_snapshots``: un juego de neumáticos del piloto."""

    kind: InventoryKind
    set_index: int
    actual_tyre_compound: str | None
    visual_tyre_compound: str | None
    wear_pct: float
    available: bool | None
    lifespan_laps: int
    usable_life_laps: int
    lap_delta_time_ms: int
    recommended_session: str | None
    fitted: bool | None

    def __post_init__(self) -> None:
        if self.kind not in INVENTORY_KINDS:
            raise ValueError(f"kind no válido: {self.kind!r}")
        if self.set_index < 0:
            raise ValueError("set_index no puede ser negativo")


class SessionStore:
    """Conexión a una base de sesiones con API de escritura y lectura."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        readonly: bool = False,
        migrations: tuple[Migration, ...] = MIGRATIONS,
    ) -> SessionStore:
        """Abre (y si hace falta crea y migra) una base de sesiones."""
        location = str(path)
        connection = _open_readonly(location) if readonly else _open_writable(location)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if readonly:
                check_supported_version(connection)
            else:
                apply_migrations(connection, migrations)
                connection.execute("PRAGMA journal_mode = WAL").fetchone()
                connection.execute("PRAGMA synchronous = NORMAL")
        except BaseException:
            connection.close()
            raise
        return cls(connection)

    @property
    def connection(self) -> sqlite3.Connection:
        """Conexión subyacente, para diagnósticos y pruebas."""
        return self._connection

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Transacciones y escritura
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Agrupa varias escrituras en una sola transacción.

        Si ya hay una transacción abierta se reutiliza, de modo que los métodos
        de escritura pueden invocarse tanto sueltos como dentro de un lote.
        """
        if self._connection.in_transaction:
            yield self._connection
            return
        self._connection.execute("BEGIN")
        try:
            yield self._connection
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def create_session(self, info: SessionInfo) -> int:
        """Inserta la cabecera de una sesión y devuelve su id.

        Si la sesión trae un identificador de fin de semana del juego distinto de
        cero, se crea (o se reutiliza) el fin de semana correspondiente. Un
        identificador nulo o cero deja la sesión sin enlace.
        """
        values = [
            str(info.session_uid),
            info.source,
            info.started_at_utc,
            info.ended_at_utc,
            int(info.incomplete),
            info.game_format,
            info.game_year,
            info.game_version,
            info.track,
            info.session_type,
            info.total_laps,
            info.capture_path,
            info.capture_sha256,
            info.tool_version,
            info.parser_version,
            info.config_hash,
            info.config_json,
            info.fingerprint,
            info.final_metrics_json,
            info.season_link_id,
            info.weekend_link_id,
            info.session_link_id,
            info.weekend_structure_json,
            info.weekend_id,
            info.profile_json,
            info.profile_hash,
        ]
        columns = _SESSION_COLUMNS[1:]
        weekend_index = _SESSION_COLUMNS.index("weekend_id") - 1
        with self.transaction() as connection:
            if info.weekend_id is None and info.weekend_link_id is not None:
                values[weekend_index] = self._ensure_game_weekend(
                    connection,
                    weekend_link_id=info.weekend_link_id,
                    season_link_id=info.season_link_id,
                    track=info.track,
                )
            cursor = connection.execute(
                f"INSERT INTO sessions ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
        if cursor.lastrowid is None:
            raise StoreError("SQLite no devolvió el id de la sesión creada")
        return int(cursor.lastrowid)

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
        """Cierra una sesión abierta con su estado final."""
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET ended_at_utc = ?, incomplete = ?, "
                "fingerprint = COALESCE(?, fingerprint), "
                "final_metrics_json = COALESCE(?, final_metrics_json), "
                "capture_sha256 = COALESCE(?, capture_sha256) WHERE id = ?",
                (
                    ended_at_utc,
                    int(incomplete),
                    fingerprint,
                    final_metrics_json,
                    capture_sha256,
                    session_id,
                ),
            )
        if cursor.rowcount == 0:
            raise StoreError(f"No existe la sesión {session_id}")

    def update_session(self, session_id: int, info: SessionInfo) -> None:
        """Completa los metadatos de una sesión sin borrar los ya conocidos.

        Solo se rellenan los campos que llegan con valor; los que ya estaban
        guardados se conservan mediante ``COALESCE``.
        """
        with self.transaction() as connection:
            weekend_id = info.weekend_id
            if weekend_id is None and info.weekend_link_id is not None:
                current = connection.execute(
                    "SELECT weekend_id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if current is not None and _optional_int(current["weekend_id"]) is None:
                    weekend_id = self._ensure_game_weekend(
                        connection,
                        weekend_link_id=info.weekend_link_id,
                        season_link_id=info.season_link_id,
                        track=info.track,
                    )
            cursor = connection.execute(
                "UPDATE sessions SET "
                "game_format = COALESCE(?, game_format), "
                "game_year = COALESCE(?, game_year), "
                "game_version = COALESCE(?, game_version), "
                "track = COALESCE(?, track), "
                "session_type = COALESCE(?, session_type), "
                "total_laps = COALESCE(?, total_laps), "
                "capture_path = COALESCE(?, capture_path), "
                "capture_sha256 = COALESCE(?, capture_sha256), "
                "tool_version = COALESCE(?, tool_version), "
                "parser_version = COALESCE(?, parser_version), "
                "config_hash = COALESCE(?, config_hash), "
                "config_json = COALESCE(?, config_json), "
                "season_link_id = COALESCE(?, season_link_id), "
                "weekend_link_id = COALESCE(?, weekend_link_id), "
                "session_link_id = COALESCE(?, session_link_id), "
                "weekend_structure_json = COALESCE(?, weekend_structure_json), "
                "weekend_id = COALESCE(?, weekend_id), "
                "profile_json = COALESCE(?, profile_json), "
                "profile_hash = COALESCE(?, profile_hash) "
                "WHERE id = ?",
                (
                    info.game_format,
                    info.game_year,
                    info.game_version,
                    info.track,
                    info.session_type,
                    info.total_laps,
                    info.capture_path,
                    info.capture_sha256,
                    info.tool_version,
                    info.parser_version,
                    info.config_hash,
                    info.config_json,
                    info.season_link_id,
                    info.weekend_link_id,
                    info.session_link_id,
                    info.weekend_structure_json,
                    weekend_id,
                    info.profile_json,
                    info.profile_hash,
                    session_id,
                ),
            )
        if cursor.rowcount == 0:
            raise StoreError(f"No existe la sesión {session_id}")

    def update_capture_sha256(self, session_id: int, sha256: str) -> None:
        """Asocia el hash de una captura ya cerrada a una sesión de esa ejecución."""
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET capture_sha256 = ? WHERE id = ?",
                (sha256, session_id),
            )
        if cursor.rowcount == 0:
            raise StoreError(f"No existe la sesión {session_id}")

    def add_participants(self, session_id: int, participants: Iterable[Participant]) -> None:
        """Inserta o actualiza los participantes de una sesión."""
        rows = [
            (
                session_id,
                participant.car_index,
                participant.name,
                participant.team,
                _bool_or_none(participant.ai_controlled),
            )
            for participant in participants
        ]
        if not rows:
            return
        with self.transaction() as connection:
            connection.executemany(
                "INSERT INTO participants (session_id, car_index, name, team, ai_controlled) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, car_index) DO UPDATE SET "
                "name = excluded.name, team = excluded.team, "
                "ai_controlled = excluded.ai_controlled",
                rows,
            )

    def upsert_laps(self, session_id: int, laps: Iterable[LapRecord]) -> None:
        """Inserta o actualiza vueltas (el tiempo oficial puede llegar tarde)."""
        rows = [
            (
                session_id,
                *(_encode_lap_column(name, getattr(record, name)) for name in _LAP_COLUMNS),
            )
            for record in laps
        ]
        if not rows:
            return
        with self.transaction() as connection:
            connection.executemany(_UPSERT_LAP_SQL, rows)

    def add_events(self, session_id: int, events: Iterable[StoredEvent]) -> None:
        """Añade eventos de carrera."""
        rows = [
            (
                session_id,
                event.seq,
                event.kind,
                event.t_sim_s,
                event.car_index,
                event.lap_number,
                event.value,
                event.previous_value,
            )
            for event in events
        ]
        if not rows:
            return
        with self.transaction() as connection:
            connection.executemany(
                "INSERT INTO events "
                "(session_id, seq, kind, t_sim_s, car_index, lap_number, value, previous_value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def add_metric_samples(self, session_id: int, samples: Iterable[MetricSample]) -> None:
        """Añade muestras de métricas (una fila por cambio de valor o calidad)."""
        rows = [
            (
                session_id,
                sample.t_session_s,
                sample.t_sim_s,
                sample.lap,
                sample.key,
                sample.value_num,
                sample.value_text,
                sample.unit,
                sample.quality,
                sample.sample_count,
            )
            for sample in samples
        ]
        if not rows:
            return
        with self.transaction() as connection:
            connection.executemany(
                "INSERT INTO metric_samples "
                "(session_id, t_session_s, t_sim_s, lap, key, value_num, value_text, unit, "
                "quality, sample_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def add_errors(self, session_id: int, errors: Iterable[StoredError]) -> None:
        """Añade errores del consumidor."""
        rows = [(session_id, error.t_sim_s, error.code, error.params_json) for error in errors]
        if not rows:
            return
        with self.transaction() as connection:
            connection.executemany(
                "INSERT INTO app_errors (session_id, t_sim_s, code, params_json) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )

    def delete_session(self, session_id: int) -> None:
        """Elimina una sesión y todos sus datos asociados (cascada)."""
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        if cursor.rowcount == 0:
            raise StoreError(f"No existe la sesión {session_id}")

    def add_stint_summaries(
        self,
        session_id: int,
        summaries: Iterable[StintSummary],
    ) -> None:
        """Guarda los resúmenes de tanda, reemplazando los de la misma tanda."""
        rows = [
            (
                session_id,
                summary.car_index,
                summary.stint_index,
                summary.start_lap,
                summary.end_lap,
                summary.laps,
                summary.compound,
                summary.visual_compound,
                summary.clean_laps,
                summary.tyre_age_start,
                summary.tyre_age_end,
                summary.fuel_start_kg,
                summary.fuel_end_kg,
                summary.pace_mean_s,
                summary.pace_mean_fuel_corrected_s,
                summary.pace_trend_s_per_lap,
                summary.pace_trend_fuel_corrected_s_per_lap,
                summary.wear_start_pct,
                summary.wear_end_pct,
                summary.wear_rate_pct_per_lap,
                summary.track_temperature_mean_c,
                json.dumps(list(summary.weather), ensure_ascii=False, separators=(",", ":")),
                int(summary.representative),
                summary.quality.value,
            )
            for summary in summaries
        ]
        if not rows:
            return
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO stint_summaries "
                "(session_id, car_index, stint_index, start_lap, end_lap, laps, compound, "
                "visual_compound, clean_laps, tyre_age_start, tyre_age_end, fuel_start_kg, "
                "fuel_end_kg, pace_mean_s, pace_mean_fuel_corrected_s, pace_trend_s_per_lap, "
                "pace_trend_fuel_corrected_s_per_lap, wear_start_pct, wear_end_pct, "
                "wear_rate_pct_per_lap, track_temperature_mean_c, weather_json, "
                "representative, quality) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def add_tyre_inventory(
        self,
        session_id: int,
        snapshots: Iterable[TyreInventorySnapshot],
    ) -> None:
        """Guarda una instantánea del inventario de neumáticos del piloto.

        La clave incluye el tipo de instantánea, así que volver a escribir la misma
        sesión no duplica filas.
        """
        rows = [
            (
                session_id,
                snapshot.kind,
                snapshot.set_index,
                snapshot.actual_tyre_compound,
                snapshot.visual_tyre_compound,
                snapshot.wear_pct,
                _bool_or_none(snapshot.available),
                snapshot.lifespan_laps,
                snapshot.usable_life_laps,
                snapshot.lap_delta_time_ms,
                snapshot.recommended_session,
                _bool_or_none(snapshot.fitted),
            )
            for snapshot in snapshots
        ]
        if not rows:
            return
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO tyre_inventory_snapshots "
                "(session_id, kind, set_index, actual_tyre_compound, visual_tyre_compound, "
                "wear_pct, available, lifespan_laps, usable_life_laps, lap_delta_time_ms, "
                "recommended_session, fitted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    # ------------------------------------------------------------------
    # Fines de semana
    # ------------------------------------------------------------------

    def create_weekend(
        self,
        *,
        source: WeekendSource,
        label: str | None = None,
        track: str | None = None,
        weekend_link_id: int | None = None,
        season_link_id: int | None = None,
        created_at_utc: str | None = None,
    ) -> int:
        """Crea un fin de semana y devuelve su id."""
        created = created_at_utc or datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO weekends "
                "(label, track, weekend_link_id, season_link_id, source, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    label or _weekend_label(track, season_link_id, weekend_link_id),
                    track,
                    weekend_link_id,
                    season_link_id,
                    source,
                    created,
                ),
            )
        if cursor.lastrowid is None:
            raise StoreError("SQLite no devolvió el id del fin de semana creado")
        return int(cursor.lastrowid)

    def get_weekend(self, weekend_id: int) -> WeekendInfo | None:
        """Devuelve un fin de semana por id, o ``None`` si no existe."""
        row = self._connection.execute(
            f"SELECT {', '.join(_WEEKEND_COLUMNS)} FROM weekends WHERE id = ?",
            (weekend_id,),
        ).fetchone()
        return None if row is None else _weekend_from_row(row)

    def list_weekends(self) -> list[WeekendInfo]:
        """Devuelve todos los fines de semana, del más antiguo al más reciente."""
        rows = self._connection.execute(
            f"SELECT {', '.join(_WEEKEND_COLUMNS)} FROM weekends ORDER BY id"
        ).fetchall()
        return [_weekend_from_row(row) for row in rows]

    def find_weekend(
        self,
        *,
        weekend_link_id: int,
        season_link_id: int | None = None,
    ) -> WeekendInfo | None:
        """Busca el fin de semana que corresponde a los identificadores del juego."""
        row = self._connection.execute(
            f"SELECT {', '.join(_WEEKEND_COLUMNS)} FROM weekends "
            "WHERE weekend_link_id = ? AND season_link_id IS ? ORDER BY id LIMIT 1",
            (weekend_link_id, season_link_id),
        ).fetchone()
        return None if row is None else _weekend_from_row(row)

    def list_weekend_sessions(self, weekend_id: int) -> list[SessionInfo]:
        """Devuelve las sesiones enlazadas a un fin de semana, por orden de id."""
        rows = self._connection.execute(
            f"SELECT {', '.join(_SESSION_COLUMNS)} FROM sessions WHERE weekend_id = ? ORDER BY id",
            (weekend_id,),
        ).fetchall()
        return [_session_from_row(row) for row in rows]

    def unlinked_sessions(self) -> list[SessionInfo]:
        """Devuelve las sesiones que no pertenecen a ningún fin de semana."""
        rows = self._connection.execute(
            f"SELECT {', '.join(_SESSION_COLUMNS)} FROM sessions "
            "WHERE weekend_id IS NULL ORDER BY id"
        ).fetchall()
        return [_session_from_row(row) for row in rows]

    def link_sessions(self, session_ids: Sequence[int], *, label: str | None = None) -> int:
        """Enlaza manualmente sesiones del mismo circuito en un fin de semana.

        Rechaza sesiones sin circuito conocido y sesiones de circuitos distintos.
        Si todas las sesiones ya comparten un fin de semana, lo reutiliza y solo
        incorpora las que falten. Devuelve el id del fin de semana resultante.
        """
        unique_ids = list(dict.fromkeys(session_ids))
        if not unique_ids:
            raise StoreError("Indica al menos una sesión para enlazar.")
        with self.transaction() as connection:
            tracks: list[str] = []
            current_weekends: list[int | None] = []
            for session_id in unique_ids:
                row = connection.execute(
                    "SELECT track, weekend_id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    raise StoreError(f"No existe la sesión {session_id}")
                track = _optional_str(row["track"])
                if track is None:
                    raise StoreError(
                        f"La sesión {session_id} no tiene circuito conocido; "
                        "no se puede validar el enlace."
                    )
                tracks.append(track)
                current_weekends.append(_optional_int(row["weekend_id"]))
            distinct = sorted(set(tracks))
            if len(distinct) > 1:
                raise StoreError(
                    "No se pueden enlazar sesiones de circuitos distintos: " + ", ".join(distinct)
                )
            track = distinct[0]
            existing = {weekend for weekend in current_weekends if weekend is not None}
            if len(existing) == 1:
                weekend_id = next(iter(existing))
                connection.executemany(
                    "UPDATE sessions SET weekend_id = ? WHERE id = ?",
                    [(weekend_id, session_id) for session_id in unique_ids],
                )
                if label is not None:
                    connection.execute(
                        "UPDATE weekends SET label = ? WHERE id = ?", (label, weekend_id)
                    )
                return weekend_id

            cursor = connection.execute(
                "INSERT INTO weekends "
                "(label, track, weekend_link_id, season_link_id, source, created_at_utc) "
                "VALUES (?, ?, NULL, NULL, 'manual', ?)",
                (label or track, track, datetime.now(UTC).isoformat()),
            )
            if cursor.lastrowid is None:
                raise StoreError("SQLite no devolvió el id del fin de semana creado")
            weekend_id = int(cursor.lastrowid)
            connection.executemany(
                "UPDATE sessions SET weekend_id = ? WHERE id = ?",
                [(weekend_id, session_id) for session_id in unique_ids],
            )
            for previous in existing:
                if previous is None or previous == weekend_id:
                    continue
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE weekend_id = ?", (previous,)
                ).fetchone()[0]
                if remaining == 0:
                    connection.execute("DELETE FROM weekends WHERE id = ?", (previous,))
        return weekend_id

    def unlink_session(self, session_id: int) -> int | None:
        """Desvincula una sesión de su fin de semana.

        Devuelve el id del fin de semana del que se separó, o ``None`` si no estaba
        enlazada. Un fin de semana que se queda sin sesiones se elimina.
        """
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT weekend_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"No existe la sesión {session_id}")
            weekend_id = _optional_int(row["weekend_id"])
            if weekend_id is None:
                return None
            connection.execute("UPDATE sessions SET weekend_id = NULL WHERE id = ?", (session_id,))
            remaining = connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE weekend_id = ?", (weekend_id,)
            ).fetchone()[0]
            if remaining == 0:
                connection.execute("DELETE FROM weekends WHERE id = ?", (weekend_id,))
        return weekend_id

    def _ensure_game_weekend(
        self,
        connection: sqlite3.Connection,
        *,
        weekend_link_id: int,
        season_link_id: int | None,
        track: str | None,
    ) -> int:
        """Devuelve el fin de semana de los identificadores del juego, creándolo si falta."""
        row = connection.execute(
            "SELECT id, track FROM weekends WHERE weekend_link_id = ? "
            "AND season_link_id IS ? ORDER BY id LIMIT 1",
            (weekend_link_id, season_link_id),
        ).fetchone()
        if row is not None:
            weekend_id = int(row["id"])
            if track is not None and _optional_str(row["track"]) is None:
                connection.execute(
                    "UPDATE weekends SET track = ?, label = COALESCE(label, ?) WHERE id = ?",
                    (track, _weekend_label(track, season_link_id, weekend_link_id), weekend_id),
                )
            return weekend_id
        cursor = connection.execute(
            "INSERT INTO weekends "
            "(label, track, weekend_link_id, season_link_id, source, created_at_utc) "
            "VALUES (?, ?, ?, ?, 'game_ids', ?)",
            (
                _weekend_label(track, season_link_id, weekend_link_id),
                track,
                weekend_link_id,
                season_link_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        if cursor.lastrowid is None:  # pragma: no cover - defensivo
            raise StoreError("SQLite no devolvió el id del fin de semana creado")
        return int(cursor.lastrowid)

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[SessionInfo]:
        """Devuelve todas las sesiones, de la más antigua a la más reciente."""
        rows = self._connection.execute(
            f"SELECT {', '.join(_SESSION_COLUMNS)} FROM sessions ORDER BY id"
        ).fetchall()
        return [_session_from_row(row) for row in rows]

    def get_session(self, session_id: int) -> SessionInfo | None:
        """Devuelve una sesión por id, o ``None`` si no existe."""
        row = self._connection.execute(
            f"SELECT {', '.join(_SESSION_COLUMNS)} FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else _session_from_row(row)

    def iter_laps(self, session_id: int, *, car_index: int | None = None) -> Iterator[LapRecord]:
        """Itera las vueltas de una sesión ordenadas por coche y número de vuelta."""
        sql = (
            f"SELECT {', '.join(_LAP_COLUMNS)} FROM laps WHERE session_id = ?"
            + (" AND car_index = ?" if car_index is not None else "")
            + " ORDER BY car_index, lap_number"
        )
        parameters: tuple[Any, ...] = (
            (session_id, car_index) if car_index is not None else (session_id,)
        )
        for row in self._connection.execute(sql, parameters):
            yield _lap_from_row(row)

    def iter_events(self, session_id: int, *, kind: str | None = None) -> Iterator[StoredEvent]:
        """Itera los eventos de una sesión en orden de secuencia."""
        sql = (
            "SELECT seq, kind, t_sim_s, car_index, lap_number, value, previous_value "
            "FROM events WHERE session_id = ?"
            + (" AND kind = ?" if kind is not None else "")
            + " ORDER BY seq, id"
        )
        parameters: tuple[Any, ...] = (session_id, kind) if kind is not None else (session_id,)
        for row in self._connection.execute(sql, parameters):
            yield StoredEvent(
                seq=int(row["seq"]),
                kind=str(row["kind"]),
                t_sim_s=float(row["t_sim_s"]),
                car_index=_optional_int(row["car_index"]),
                lap_number=_optional_int(row["lap_number"]),
                value=_optional_str(row["value"]),
                previous_value=_optional_str(row["previous_value"]),
            )

    def iter_metric_samples(
        self,
        session_id: int,
        *,
        key: str | None = None,
        lap: int | None = None,
    ) -> Iterator[MetricSample]:
        """Itera las muestras de métricas en orden de inserción."""
        clauses = ["session_id = ?"]
        parameters: list[Any] = [session_id]
        if key is not None:
            clauses.append("key = ?")
            parameters.append(key)
        if lap is not None:
            clauses.append("lap = ?")
            parameters.append(lap)
        sql = (
            "SELECT t_session_s, t_sim_s, lap, key, value_num, value_text, unit, quality, "
            "sample_count FROM metric_samples WHERE " + " AND ".join(clauses) + " ORDER BY id"
        )
        for row in self._connection.execute(sql, parameters):
            yield MetricSample(
                t_sim_s=float(row["t_sim_s"]),
                key=str(row["key"]),
                quality=str(row["quality"]),
                unit=str(row["unit"]),
                sample_count=int(row["sample_count"]),
                t_session_s=_optional_float(row["t_session_s"]),
                lap=_optional_int(row["lap"]),
                value_num=_optional_float(row["value_num"]),
                value_text=_optional_str(row["value_text"]),
            )

    def iter_errors(self, session_id: int) -> Iterator[StoredError]:
        """Itera los errores registrados de una sesión en orden de inserción."""
        for row in self._connection.execute(
            "SELECT t_sim_s, code, params_json FROM app_errors WHERE session_id = ? ORDER BY id",
            (session_id,),
        ):
            yield StoredError(
                t_sim_s=float(row["t_sim_s"]),
                code=str(row["code"]),
                params_json=_optional_str(row["params_json"]),
            )

    def iter_stint_summaries(self, session_id: int) -> Iterator[StintSummary]:
        """Itera los resúmenes de tanda de una sesión por coche y tanda."""
        cursor = self._connection.execute(
            "SELECT car_index, stint_index, start_lap, end_lap, laps, compound, "
            "visual_compound, clean_laps, tyre_age_start, tyre_age_end, fuel_start_kg, "
            "fuel_end_kg, pace_mean_s, pace_mean_fuel_corrected_s, pace_trend_s_per_lap, "
            "pace_trend_fuel_corrected_s_per_lap, wear_start_pct, wear_end_pct, "
            "wear_rate_pct_per_lap, track_temperature_mean_c, weather_json, representative, "
            "quality FROM stint_summaries WHERE session_id = ? "
            "ORDER BY car_index, stint_index",
            (session_id,),
        )
        for row in cursor:
            yield StintSummary(
                car_index=int(row["car_index"]),
                stint_index=int(row["stint_index"]),
                start_lap=int(row["start_lap"]),
                end_lap=_optional_int(row["end_lap"]),
                laps=int(row["laps"]),
                compound=_optional_str(row["compound"]),
                visual_compound=_optional_str(row["visual_compound"]),
                clean_laps=int(row["clean_laps"]),
                tyre_age_start=_optional_int(row["tyre_age_start"]),
                tyre_age_end=_optional_int(row["tyre_age_end"]),
                fuel_start_kg=_optional_float(row["fuel_start_kg"]),
                fuel_end_kg=_optional_float(row["fuel_end_kg"]),
                pace_mean_s=_optional_float(row["pace_mean_s"]),
                pace_mean_fuel_corrected_s=_optional_float(row["pace_mean_fuel_corrected_s"]),
                pace_trend_s_per_lap=_optional_float(row["pace_trend_s_per_lap"]),
                pace_trend_fuel_corrected_s_per_lap=_optional_float(
                    row["pace_trend_fuel_corrected_s_per_lap"]
                ),
                wear_start_pct=_optional_float(row["wear_start_pct"]),
                wear_end_pct=_optional_float(row["wear_end_pct"]),
                wear_rate_pct_per_lap=_optional_float(row["wear_rate_pct_per_lap"]),
                track_temperature_mean_c=_optional_float(row["track_temperature_mean_c"]),
                weather=tuple(str(item) for item in json.loads(row["weather_json"])),
                representative=bool(row["representative"]),
                quality=Quality(str(row["quality"])),
            )

    def iter_tyre_inventory(
        self,
        session_id: int,
        *,
        kind: InventoryKind | None = None,
    ) -> Iterator[TyreInventorySnapshot]:
        """Itera las instantáneas del inventario de neumáticos de una sesión."""
        sql = (
            "SELECT kind, set_index, actual_tyre_compound, visual_tyre_compound, wear_pct, "
            "available, lifespan_laps, usable_life_laps, lap_delta_time_ms, "
            "recommended_session, fitted FROM tyre_inventory_snapshots WHERE session_id = ?"
            + (" AND kind = ?" if kind is not None else "")
            + " ORDER BY kind, set_index"
        )
        parameters: tuple[Any, ...] = (session_id, kind) if kind is not None else (session_id,)
        for row in self._connection.execute(sql, parameters):
            yield TyreInventorySnapshot(
                kind=row["kind"],
                set_index=int(row["set_index"]),
                actual_tyre_compound=_optional_str(row["actual_tyre_compound"]),
                visual_tyre_compound=_optional_str(row["visual_tyre_compound"]),
                wear_pct=float(row["wear_pct"]),
                available=_bool_from_column(row["available"]),
                lifespan_laps=int(row["lifespan_laps"]),
                usable_life_laps=int(row["usable_life_laps"]),
                lap_delta_time_ms=int(row["lap_delta_time_ms"]),
                recommended_session=_optional_str(row["recommended_session"]),
                fitted=_bool_from_column(row["fitted"]),
            )


def _open_writable(location: str) -> sqlite3.Connection:
    if location == ":memory:":
        path = location
    else:
        expanded = Path(location).expanduser()
        expanded.parent.mkdir(parents=True, exist_ok=True)
        path = str(expanded)
    return sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000,
        check_same_thread=False,
    )


def _open_readonly(location: str) -> sqlite3.Connection:
    if location == ":memory:":
        raise StoreError("Una base en memoria no puede abrirse en solo lectura")
    path = Path(location).expanduser().resolve()
    if not path.is_file():
        raise StoreError(f"No existe la base de datos: {path}")
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
        )
    except sqlite3.Error as exc:  # pragma: no cover - defensivo
        raise StoreError(f"No se pudo abrir la base de datos {path}: {exc}") from exc
    connection.execute("PRAGMA query_only = ON")
    return connection


def _session_from_row(row: sqlite3.Row) -> SessionInfo:
    return SessionInfo(
        id=int(row["id"]),
        session_uid=int(row["session_uid"]),
        source=row["source"],
        started_at_utc=str(row["started_at_utc"]),
        ended_at_utc=_optional_str(row["ended_at_utc"]),
        incomplete=bool(row["incomplete"]),
        game_format=_optional_str(row["game_format"]),
        game_year=_optional_int(row["game_year"]),
        game_version=_optional_str(row["game_version"]),
        track=_optional_str(row["track"]),
        session_type=_optional_str(row["session_type"]),
        total_laps=_optional_int(row["total_laps"]),
        capture_path=_optional_str(row["capture_path"]),
        capture_sha256=_optional_str(row["capture_sha256"]),
        tool_version=_optional_str(row["tool_version"]),
        parser_version=_optional_str(row["parser_version"]),
        config_hash=_optional_str(row["config_hash"]),
        config_json=_optional_str(row["config_json"]),
        fingerprint=_optional_str(row["fingerprint"]),
        final_metrics_json=_optional_str(row["final_metrics_json"]),
        season_link_id=_optional_int(row["season_link_id"]),
        weekend_link_id=_optional_int(row["weekend_link_id"]),
        session_link_id=_optional_int(row["session_link_id"]),
        weekend_structure_json=_optional_str(row["weekend_structure_json"]),
        weekend_id=_optional_int(row["weekend_id"]),
        profile_json=_optional_str(row["profile_json"]),
        profile_hash=_optional_str(row["profile_hash"]),
    )


def _weekend_from_row(row: sqlite3.Row) -> WeekendInfo:
    return WeekendInfo(
        id=int(row["id"]),
        source=row["source"],
        created_at_utc=str(row["created_at_utc"]),
        label=_optional_str(row["label"]),
        track=_optional_str(row["track"]),
        weekend_link_id=_optional_int(row["weekend_link_id"]),
        season_link_id=_optional_int(row["season_link_id"]),
    )


def _weekend_label(
    track: str | None, season_link_id: int | None, weekend_link_id: int | None
) -> str | None:
    """Etiqueta legible cuando no se indica una explícita."""
    if track and weekend_link_id is not None and season_link_id is not None:
        return f"{track} (T{season_link_id} · fin de semana {weekend_link_id})"
    if track and weekend_link_id is not None:
        return f"{track} (fin de semana {weekend_link_id})"
    return track


def _lap_from_row(row: sqlite3.Row) -> LapRecord:
    values = {column: _decode_lap_column(column, row[column]) for column in _LAP_COLUMNS}
    return LapRecord(**values)


def _encode_lap_column(name: str, value: Any) -> Any:
    if name in LAP_JSON_COLUMNS:
        if value is None:
            return None
        return json.dumps(
            _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    if name in LAP_BOOL_COLUMNS:
        return _bool_or_none(value)
    return value


def _decode_lap_column(name: str, value: Any) -> Any:
    if name in LAP_JSON_COLUMNS:
        if value is None:
            return None
        parsed = json.loads(value)
        if name in {"track_status_seen", "weather_seen", "flags"}:
            return frozenset(parsed)
        return tuple(parsed)
    if name in LAP_BOOL_COLUMNS:
        return None if value is None else bool(value)
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, (frozenset, set)):
        return sorted(_json_value(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _bool_or_none(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _bool_from_column(value: Any) -> bool | None:
    """Interpreta una columna entera como booleano, conservando NULL."""
    return None if value is None else bool(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
