"""Almacén SQLite de sesiones grabadas (esquema, migraciones y API)."""

from __future__ import annotations

from .schema import (
    LAP_COLUMN_EXCLUSIONS,
    LAP_COLUMN_TYPES,
    MIGRATIONS,
    SCHEMA_VERSION,
    Migration,
    StoreError,
    apply_migrations,
)
from .sqlite_store import (
    WEEKEND_SOURCES,
    MetricSample,
    Participant,
    SessionInfo,
    SessionStore,
    StoredError,
    StoredEvent,
    WeekendInfo,
    WeekendSource,
)

__all__ = [
    "LAP_COLUMN_EXCLUSIONS",
    "LAP_COLUMN_TYPES",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "WEEKEND_SOURCES",
    "MetricSample",
    "Migration",
    "Participant",
    "SessionInfo",
    "SessionStore",
    "StoreError",
    "StoredError",
    "StoredEvent",
    "WeekendInfo",
    "WeekendSource",
    "apply_migrations",
]
