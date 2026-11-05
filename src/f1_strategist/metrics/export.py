"""Exportación estable de métricas e histórico para JSON, JSON Lines y CSV."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal

from ..config import MetricsConfig
from ..offline import OfflineResult, run_capture_offline
from ..parser import ParsedPacket
from ..race_state import RaceState, RaceStateSnapshot
from .engine import MetricsEngine, MetricsSnapshot
from .history import LapHistory, LapHistorySnapshot, LapRecord

EXPORT_SCHEMA_VERSION = 1
LAP_COLUMNS = tuple(field.name for field in fields(LapRecord))
ExportFormat = Literal["json", "jsonl", "csv"]
EXPORT_FORMATS = ("json", "jsonl", "csv")


@dataclass(frozen=True, slots=True)
class MetricsCaptureResult:
    """Resultado de procesar una captura con el motor de métricas."""

    offline: OfflineResult
    snapshot: MetricsSnapshot
    history: LapHistorySnapshot


def snapshot_to_dict(snapshot: MetricsSnapshot) -> dict[str, Any]:
    """Convierte una instantánea de métricas en un diccionario serializable."""
    metrics = {
        key: {
            "value": _json_value(metric.value),
            "unit": metric.unit,
            "quality": metric.quality.value,
            "window": {
                "kind": metric.window.kind,
                "size": _json_value(metric.window.size),
            },
            "sample_count": metric.sample_count,
            "computed_at_monotonic": metric.computed_at_monotonic,
            "sources": list(metric.sources),
            "notes": list(metric.notes),
        }
        for key, metric in sorted(snapshot.metrics.items())
    }
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "session_uid": snapshot.session_uid,
        "computed_at_monotonic": snapshot.computed_at_monotonic,
        "metrics": metrics,
    }


def laps_to_rows(history: LapHistory | LapHistorySnapshot) -> list[dict[str, Any]]:
    """Devuelve una fila serializable por coche y vuelta, ordenadas por coche y vuelta."""
    snapshot = history.snapshot() if isinstance(history, LapHistory) else history
    rows: list[dict[str, Any]] = []
    for car_index in sorted(snapshot.records):
        for record in sorted(snapshot.records[car_index], key=lambda item: item.lap_number):
            rows.append({name: _json_value(getattr(record, name)) for name in LAP_COLUMNS})
    return rows


def render_metrics_export(
    snapshot: MetricsSnapshot,
    history: LapHistory | LapHistorySnapshot,
    export_format: str,
) -> str:
    """Serializa métricas e histórico en el formato solicitado."""
    normalized_format = export_format.lower()
    if normalized_format not in EXPORT_FORMATS:
        valid = ", ".join(EXPORT_FORMATS)
        raise ValueError(f"Formato de exportación no válido: {export_format!r}; usa {valid}")

    if normalized_format == "json":
        payload = snapshot_to_dict(snapshot)
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    rows = laps_to_rows(history)
    if normalized_format == "jsonl":
        return "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            for row in rows
        )
    return _render_csv(rows)


def run_metrics_capture(
    path: Path,
    config: MetricsConfig | None = None,
    *,
    speed: float | None = None,
) -> MetricsCaptureResult:
    """Reproduce una captura offline y devuelve sus métricas e histórico finales."""
    collector = _MetricsCollector(MetricsEngine(config))
    offline = run_capture_offline(path, [collector], speed=speed)
    if collector.last_state is not None and collector.last_observed_at is not None:
        collector.engine.finalize(collector.last_state, collector.last_observed_at)
    return MetricsCaptureResult(
        offline=offline,
        snapshot=collector.engine.snapshot(),
        history=collector.engine.history.snapshot(),
    )


class _MetricsCollector:
    """Consumidor offline que mantiene estado de carrera y motor de métricas."""

    def __init__(self, engine: MetricsEngine) -> None:
        self.engine = engine
        self.state = RaceState()
        self.last_state: RaceStateSnapshot | None = None
        self.last_observed_at: float | None = None

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.state.apply(packet, observed_at)
        self.last_state = self.state.snapshot()
        self.last_observed_at = observed_at
        self.engine.apply(packet, observed_at, self.last_state)


def _render_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=LAP_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_value(row[column]) for column in LAP_COLUMNS})
    return output.getvalue()


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, frozenset, set, dict)):
        return json.dumps(_json_value(value), ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted(_json_value(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)
