"""Consultas de solo lectura sobre las sesiones grabadas.

Este módulo no abre conexiones ni modifica la base de datos: recibe un
:class:`~f1_strategist.store.sqlite_store.SessionStore` ya abierto y se limita
a leer de su API pública.

La tabla ``metric_samples`` guarda una muestra cada vez que cambia el valor o
la calidad de una métrica, además de una muestra al cerrar cada vuelta del
piloto. Para reconstruir el valor "mantenido" de una vuelta se arrastra la
última muestra conocida, de la primera vuelta con datos hasta el final de la
sesión.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from typing import Any

from ..metrics.engine import MetricsSnapshot
from ..metrics.export import laps_to_rows, render_metrics_export
from ..metrics.history import LapHistorySnapshot, LapRecord
from ..metrics.types import Metric, MetricValue, Quality, Window
from .schema import StoreError
from .sqlite_store import MetricSample, SessionInfo, SessionStore, StoredEvent

__all__ = [
    "HeldMetric",
    "SessionSummary",
    "held_metric_values",
    "list_session_summaries",
    "render_session_export",
    "require_session",
    "session_events",
    "session_history",
    "session_laps",
    "session_metric_samples",
]


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Resumen de una sesión con el número de filas de cada tabla hija."""

    session: SessionInfo
    laps: int
    events: int
    metric_samples: int
    errors: int


@dataclass(frozen=True, slots=True)
class HeldMetric:
    """Valor de una métrica mantenido al comienzo de una vuelta."""

    lap: int
    key: str
    value: MetricValue
    unit: str
    quality: str
    sample_count: int
    t_sim_s: float


def require_session(store: SessionStore, session_id: int) -> SessionInfo:
    """Devuelve la sesión indicada o lanza :class:`StoreError` si no existe."""
    session = store.get_session(session_id)
    if session is None:
        raise StoreError(f"No existe la sesión {session_id} en la base de datos.")
    return session


def list_session_summaries(store: SessionStore) -> list[SessionSummary]:
    """Devuelve todas las sesiones con el conteo de sus datos asociados."""
    summaries: list[SessionSummary] = []
    for session in store.list_sessions():
        assert session.id is not None
        summaries.append(
            SessionSummary(
                session=session,
                laps=sum(1 for _ in store.iter_laps(session.id)),
                events=sum(1 for _ in store.iter_events(session.id)),
                metric_samples=sum(1 for _ in store.iter_metric_samples(session.id)),
                errors=sum(1 for _ in store.iter_errors(session.id)),
            )
        )
    return summaries


def session_laps(
    store: SessionStore, session_id: int, *, car_index: int | None = None
) -> list[LapRecord]:
    """Devuelve las vueltas de una sesión, opcionalmente filtradas por coche."""
    require_session(store, session_id)
    return list(store.iter_laps(session_id, car_index=car_index))


def session_events(
    store: SessionStore, session_id: int, *, kind: str | None = None
) -> list[StoredEvent]:
    """Devuelve los eventos de una sesión, opcionalmente filtrados por tipo."""
    require_session(store, session_id)
    return list(store.iter_events(session_id, kind=kind))


def session_metric_samples(
    store: SessionStore,
    session_id: int,
    *,
    key: str | None = None,
    lap: int | None = None,
) -> list[MetricSample]:
    """Devuelve las muestras de métricas de una sesión con filtros opcionales."""
    require_session(store, session_id)
    return list(store.iter_metric_samples(session_id, key=key, lap=lap))


def _decode_value(sample: MetricSample) -> MetricValue:
    """Convierte las columnas ``value_text``/``value_num`` al tipo original."""
    text = sample.value_text
    if text is None:
        return sample.value_num
    if text == "true":
        return True
    if text == "false":
        return False
    if text.startswith(("[", "{")):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, list):
            return tuple(str(item) for item in parsed)
        return text
    return text


def _held_by_key(
    samples: list[MetricSample],
) -> tuple[dict[str, dict[int, MetricSample]], dict[str, MetricSample]]:
    """Agrupa por clave y vuelta y conserva el estado previo sin vuelta.

    Una muestra sin vuelta solo sirve como estado inicial si aparece antes de
    la primera muestra con vuelta de esa clave; las posteriores no se asignan a
    ninguna vuelta para no inventar datos.
    """
    grouped: dict[str, dict[int, MetricSample]] = {}
    initial: dict[str, MetricSample] = {}
    with_lap: set[str] = set()
    for sample in samples:
        if sample.lap is None:
            if sample.key not in with_lap:
                initial[sample.key] = sample
            continue
        grouped.setdefault(sample.key, {})[sample.lap] = sample
        with_lap.add(sample.key)
    return grouped, initial


def held_metric_values(
    store: SessionStore,
    session_id: int,
    *,
    key_pattern: str,
    lap: int | None = None,
) -> list[HeldMetric]:
    """Reconstruye el valor mantenido de cada métrica en cada vuelta.

    ``key_pattern`` es un patrón glob (por ejemplo ``fuel.*``). Se devuelve una
    fila por vuelta observada de la clave, desde la primera hasta la última
    muestra con vuelta, arrastrando el último valor conocido. Una muestra sin
    vuelta anterior a la primera vuelta se usa como estado inicial. Con ``lap``
    se limita la salida a esa vuelta.
    """
    require_session(store, session_id)
    matching = [
        sample
        for sample in store.iter_metric_samples(session_id)
        if fnmatch.fnmatchcase(sample.key, key_pattern)
    ]
    grouped, initial = _held_by_key(matching)
    held: list[HeldMetric] = []
    for key, by_lap in grouped.items():
        first_lap = min(by_lap)
        last_lap = max(by_lap)
        current: MetricSample | None = initial.get(key)
        for lap_number in range(first_lap, last_lap + 1):
            current = by_lap.get(lap_number, current)
            if current is None:
                continue
            if lap is not None and lap_number != lap:
                continue
            held.append(
                HeldMetric(
                    lap=lap_number,
                    key=key,
                    value=_decode_value(current),
                    unit=current.unit,
                    quality=current.quality,
                    sample_count=current.sample_count,
                    t_sim_s=current.t_sim_s,
                )
            )
    held.sort(key=lambda item: (item.lap, item.key))
    return held


def session_history(store: SessionStore, session_id: int) -> LapHistorySnapshot:
    """Reconstruye un histórico inmutable de vueltas a partir de la tabla."""
    session = require_session(store, session_id)
    records: dict[int, list[LapRecord]] = {}
    for lap in store.iter_laps(session_id):
        records.setdefault(lap.car_index, []).append(lap)
    return LapHistorySnapshot(
        records={car_index: tuple(laps) for car_index, laps in records.items()},
        session_uid=session.session_uid,
    )


def _final_snapshot(store: SessionStore, session: SessionInfo) -> MetricsSnapshot:
    """Reconstruye el último valor publicado de cada métrica.

    La base no guarda ``window``, ``sources``, ``notes`` ni el instante exacto
    de publicación, así que se usan valores neutros. ``t_sim_s`` de la última
    muestra hace de ``computed_at_monotonic``.
    """
    assert session.id is not None
    metrics: dict[str, Metric] = {}
    computed_at: float | None = None
    for sample in store.iter_metric_samples(session.id):
        value = _decode_value(sample)
        metrics[sample.key] = Metric(
            key=sample.key,
            value=value,
            unit=sample.unit,
            window=Window("instant"),
            quality=Quality(sample.quality),
            sample_count=sample.sample_count,
            computed_at_monotonic=sample.t_sim_s,
        )
        computed_at = sample.t_sim_s
    return MetricsSnapshot(
        metrics=metrics,
        computed_at_monotonic=computed_at,
        session_uid=session.session_uid,
    )


def render_session_export(store: SessionStore, session_id: int, export_format: str) -> str:
    """Renderiza una sesión con el mismo formato que el comando ``metrics``.

    ``jsonl`` y ``csv`` contienen las vueltas y coinciden exactamente con
    ``metrics``. ``json`` reproduce la instantánea final guardada por el
    grabador; si la sesión es antigua y no la tiene, se reconstruye el último
    valor mantenido con los campos disponibles.
    """
    session = require_session(store, session_id)
    history = session_history(store, session_id)
    normalized = export_format.lower()
    if normalized == "json" and session.final_metrics_json is not None:
        try:
            payload = json.loads(session.final_metrics_json)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    snapshot = _final_snapshot(store, session) if normalized == "json" else MetricsSnapshot()
    return render_metrics_export(snapshot, history, export_format)


def session_export_rows(store: SessionStore, session_id: int) -> list[dict[str, Any]]:
    """Devuelve las vueltas de una sesión como filas listas para exportar."""
    return laps_to_rows(session_history(store, session_id))
