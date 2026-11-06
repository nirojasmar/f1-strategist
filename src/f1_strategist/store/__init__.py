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
    MetricSample,
    Participant,
    SessionInfo,
    SessionStore,
    StoredError,
    StoredEvent,
)

__all__ = [
    "LAP_COLUMN_EXCLUSIONS",
    "LAP_COLUMN_TYPES",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "MetricSample",
    "Migration",
    "Participant",
    "SessionInfo",
    "SessionStore",
    "StoreError",
    "StoredError",
    "StoredEvent",
    "apply_migrations",
]
