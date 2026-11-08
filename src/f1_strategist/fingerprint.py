"""Huella determinista de una ejecución offline y metadatos que la produjeron."""

from __future__ import annotations

import hashlib
import json
import math
import platform
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig, MetricsConfig
from .metrics.engine import MetricsEngine, MetricsSnapshot
from .metrics.history import LapHistory, LapHistorySnapshot
from .offline import OfflineResult, run_capture_offline
from .parser import PARSER_VERSION, ParsedPacket
from .race_state import RaceEvent, RaceState, RaceStateSnapshot

FINGERPRINT_SCHEMA_VERSION = 1

_IGNORED_KEYS = frozenset({"capture_path"})


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Metadatos reproducibles de una ejecución."""

    tool_version: str
    parser_version: str
    config_hash: str
    capture_sha256: str
    python_version: str
    platform: str

    def to_dict(self) -> dict[str, str]:
        return {
            "tool_version": self.tool_version,
            "parser_version": self.parser_version,
            "config_hash": self.config_hash,
            "capture_sha256": self.capture_sha256,
            "python_version": self.python_version,
            "platform": self.platform,
        }


@dataclass(frozen=True, slots=True)
class FingerprintRun:
    """Resultado de procesar una captura y calcular su huella."""

    fingerprint: str
    run_info: RunInfo
    offline: OfflineResult
    state: RaceStateSnapshot | None
    metrics: MetricsSnapshot
    history: LapHistorySnapshot
    events: tuple[RaceEvent, ...]


def build_run_info(
    config: AppConfig | MetricsConfig | None = None,
    capture_path: Path | None = None,
) -> RunInfo:
    """Construye los metadatos de una ejecución a partir de su configuración.

    ``config_hash`` cubre la configuración completa suministrada; si se recibe
    solo ``MetricsConfig``, las demás secciones usan sus valores por defecto.
    """
    resolved = _complete_config(config)
    config_hash = compute_config_hash(resolved)
    capture_sha256 = file_sha256(capture_path) if capture_path is not None else ""
    return RunInfo(
        tool_version=__version__,
        parser_version=PARSER_VERSION,
        config_hash=config_hash,
        capture_sha256=capture_sha256,
        python_version=platform.python_version(),
        platform=platform.platform(),
    )


def compute_config_hash(config: AppConfig | MetricsConfig) -> str:
    """Hash canónico de lo suministrado: toda ``AppConfig`` o solo ``MetricsConfig``."""
    payload = _config_value(config.model_dump())
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _effective_metrics_config(config: AppConfig | MetricsConfig | None) -> MetricsConfig:
    """Extrae la configuración de métricas que realmente usa el motor."""
    if config is None:
        return MetricsConfig()
    if isinstance(config, AppConfig):
        return config.metrics
    return config


def _complete_config(config: AppConfig | MetricsConfig | None) -> AppConfig:
    if config is None:
        return AppConfig()
    if isinstance(config, AppConfig):
        return config
    return AppConfig(metrics=config)


def compute_fingerprint(
    events: Iterable[RaceEvent] | RaceStateSnapshot,
    laps: LapHistory | LapHistorySnapshot,
    final_metrics: MetricsSnapshot | Mapping[str, Any],
    *,
    float_digits: int = 9,
) -> str:
    """Devuelve una huella estable de los datos derivados de una ejecución.

    Los tiempos de captura (``observed_at``) son deterministas y forman parte
    de la huella, aunque sus campos conserven el sufijo ``_at_monotonic``; se
    serializan como ``*_at_s``. Solo se excluyen las rutas y los valores que
    dependen del reloj de pared. Los flotantes se redondean antes de serializar
    el JSON canónico.
    """
    if isinstance(float_digits, bool) or not isinstance(float_digits, int) or float_digits < 0:
        raise ValueError("float_digits debe ser un entero no negativo")

    if isinstance(events, RaceStateSnapshot):
        event_values: Iterable[RaceEvent] = events.events
    else:
        event_values = events
    if isinstance(laps, LapHistory):
        lap_values: object = laps.snapshot()
    else:
        lap_values = laps
    normalized_events = _normalize_iterable(event_values, float_digits)
    payload = {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "events": normalized_events,
        "laps": _normalize(lap_values, float_digits),
        "metrics": _normalize(final_metrics, float_digits),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def compute_capture_fingerprint(
    events: Iterable[RaceEvent] | RaceStateSnapshot,
    laps: LapHistory | LapHistorySnapshot,
    metrics: MetricsSnapshot,
    config: AppConfig | MetricsConfig | None = None,
    capture_path: Path | None = None,
    *,
    run_info: RunInfo | None = None,
    float_digits: int = 9,
) -> str:
    """Calcula la huella canónica de una captura con su configuración y metadatos.

    Es la misma construcción que usa ``fingerprint_capture`` para el comando
    ``verify``; ``ingest`` la reutiliza para que ambas huellas coincidan.
    """
    complete_config = _complete_config(config)
    info = run_info if run_info is not None else build_run_info(complete_config, capture_path)
    return compute_fingerprint(
        events,
        laps,
        {
            "metrics": metrics,
            "config": _config_value(complete_config.model_dump()),
            "run_info": {
                "parser_version": info.parser_version,
                "capture_sha256": info.capture_sha256,
            },
        },
        float_digits=float_digits,
    )


def fingerprint_capture(
    path: Path,
    config: AppConfig | MetricsConfig | None = None,
    *,
    float_digits: int = 9,
) -> FingerprintRun:
    """Reproduce una captura offline y calcula su huella determinista."""
    metrics_config = _effective_metrics_config(config)
    complete_config = _complete_config(config)
    collector = _FingerprintCollector(MetricsEngine(metrics_config))
    offline = run_capture_offline(path, [collector])
    if collector.last_state is not None and collector.last_observed_at is not None:
        collector.engine.finalize(collector.last_state, collector.last_observed_at)

    state = collector.last_state
    metrics = collector.engine.snapshot()
    history = collector.engine.history.snapshot()
    events = tuple(collector.events)
    run_info = build_run_info(complete_config, path)
    fingerprint = compute_capture_fingerprint(
        events,
        history,
        metrics,
        complete_config,
        path,
        run_info=run_info,
        float_digits=float_digits,
    )
    return FingerprintRun(
        fingerprint=fingerprint,
        run_info=run_info,
        offline=offline,
        state=state,
        metrics=metrics,
        history=history,
        events=events,
    )


class _FingerprintCollector:
    def __init__(self, engine: MetricsEngine) -> None:
        self.engine = engine
        self.events: list[RaceEvent] = []
        self.state = RaceState(self.events.append)
        self.last_state: RaceStateSnapshot | None = None
        self.last_observed_at: float | None = None

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        self.state.apply(packet, observed_at)
        self.last_state = self.state.snapshot()
        self.last_observed_at = observed_at
        self.engine.apply(packet, observed_at, self.last_state)


def _normalize_iterable(values: Iterable[RaceEvent], float_digits: int) -> list[Any]:
    normalized = [_normalize(value, float_digits) for value in values]
    if isinstance(values, (set, frozenset)):
        normalized.sort(key=_canonical_json)
    return normalized


def _normalize(value: object, float_digits: int) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            _normalize_key(field.name): _normalize(getattr(value, field.name), float_digits)
            for field in fields(value)
            if not _is_ignored_key(field.name)
        }
    if isinstance(value, Mapping):
        return {
            _normalize_key(str(key)): _normalize(item, float_digits)
            for key, item in value.items()
            if not _is_ignored_key(str(key))
        }
    if isinstance(value, Enum):
        return _normalize(value.value, float_digits)
    if isinstance(value, (frozenset, set)):
        return sorted(
            (_normalize(item, float_digits) for item in value),
            key=_canonical_json,
        )
    if isinstance(value, (list, tuple)):
        return [_normalize(item, float_digits) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("La huella no admite valores flotantes no finitos")
        rounded = round(value, float_digits)
        return 0.0 if rounded == 0 else rounded
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


def _config_value(value: object) -> object:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _config_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_config_value(item) for item in value]
    return value


def _normalize_key(key: str) -> str:
    """Nombra los tiempos de captura como segundos sin depender del reloj de pared."""
    if key.endswith("_at_monotonic"):
        return f"{key.removesuffix('_at_monotonic')}_at_s"
    return key


def _is_ignored_key(key: str) -> bool:
    return key in _IGNORED_KEYS


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def file_sha256(path: Path) -> str:
    """Devuelve el SHA-256 de un archivo leyéndolo por bloques."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
