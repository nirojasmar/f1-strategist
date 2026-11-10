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
    INVENTORY_KINDS,
    WEEKEND_SOURCES,
    InventoryKind,
    MetricSample,
    Participant,
    SessionInfo,
    SessionStore,
    StoredError,
    StoredEvent,
    TyreInventorySnapshot,
    WeekendInfo,
    WeekendSource,
)

__all__ = [
    "INVENTORY_KINDS",
    "LAP_COLUMN_EXCLUSIONS",
    "LAP_COLUMN_TYPES",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "WEEKEND_SOURCES",
    "InventoryKind",
    "MetricSample",
    "Migration",
    "Participant",
    "SessionInfo",
    "SessionStore",
    "StoreError",
    "StoredError",
    "StoredEvent",
    "TyreInventorySnapshot",
    "WeekendInfo",
    "WeekendSource",
    "apply_migrations",
]
