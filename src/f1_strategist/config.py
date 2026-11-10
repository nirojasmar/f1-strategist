"""Configuración externa de la aplicación (TOML validado con pydantic)."""

from __future__ import annotations

import ipaddress
import tomllib
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigError(Exception):
    """Error al leer o validar la configuración."""


def _check_ip(value: str) -> str:
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError as exc:
        raise ValueError(f"'{value}' no es una dirección IP válida") from exc


class TelemetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listen_address: str = "127.0.0.1"
    # 0 = puerto efímero, útil solo para pruebas.
    listen_port: int = Field(default=20777, ge=0, le=65535)
    allowed_sources: list[str] = Field(default_factory=list)
    no_data_warning_s: float = Field(default=3.0, gt=0)

    @field_validator("listen_address")
    @classmethod
    def _validate_listen_address(cls, value: str) -> str:
        return _check_ip(value)

    @field_validator("allowed_sources")
    @classmethod
    def _validate_allowed_sources(cls, value: list[str]) -> list[str]:
        return [_check_ip(item) for item in value]

    @property
    def ip_version(self) -> int:
        return ipaddress.ip_address(self.listen_address).version

    @property
    def is_loopback_only(self) -> bool:
        return ipaddress.ip_address(self.listen_address).is_loopback


class CaptureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    directory: Path = Path("captures")


class RecordingConfig(BaseModel):
    """Grabación de la sesión en SQLite (derivada de la captura cruda)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    database: Path = Path("captures/sessions.db")
    metrics_interval_s: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    queue_size: int = Field(default=1000, ge=1)
    batch_size: int = Field(default=100, ge=1)
    flush_interval_s: float = Field(default=0.5, gt=0, allow_inf_nan=False)


METRIC_FAMILIES = frozenset(
    {"progress", "fuel", "tyres", "pace", "gap", "pit", "ers", "damage", "env", "track"}
)


class SampleThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    low: int = Field(ge=1)
    ok: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_order(self) -> SampleThresholds:
        if self.ok < self.low:
            raise ValueError("ok debe ser mayor o igual que low")
        return self


class MetricsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publish_interval_s: float = Field(default=0.5, gt=0, allow_inf_nan=False)
    history_max_laps: int = Field(default=100, ge=1, le=100)
    pace_window_laps: int = Field(default=5, ge=1)
    fuel_window_laps: int = Field(default=5, ge=1)
    min_samples_low: int = Field(default=3, ge=1)
    min_samples_ok: int = Field(default=5, ge=1)
    min_samples: dict[str, SampleThresholds] = Field(
        default_factory=lambda: {"fuel": SampleThresholds(low=2, ok=3)}
    )
    traffic_gap_s: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    traffic_lap_fraction: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    gap_trend_window_s: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    damage_change_threshold_pct: float = Field(default=5, ge=0, le=100, allow_inf_nan=False)
    fuel_effect_s_per_kg: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tyre_wear_limit_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    ers_capacity_joules: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    min_representative_laps: int = Field(default=4, ge=1)

    @field_validator("min_samples")
    @classmethod
    def _validate_metric_families(
        cls, value: dict[str, SampleThresholds]
    ) -> dict[str, SampleThresholds]:
        value = dict(value)
        unknown = set(value) - METRIC_FAMILIES
        if unknown:
            raise ValueError(
                f"Familias desconocidas en min_samples: {', '.join(sorted(unknown))}; "
                f"válidas: {', '.join(sorted(METRIC_FAMILIES))}"
            )
        value.setdefault("fuel", SampleThresholds(low=2, ok=3))
        return value

    @model_validator(mode="after")
    def _validate_sample_thresholds(self) -> MetricsConfig:
        if self.min_samples_ok < self.min_samples_low:
            raise ValueError("min_samples_ok debe ser mayor o igual que min_samples_low")
        pace_ok = self.thresholds("pace")[1]
        if self.pace_window_laps < pace_ok:
            raise ValueError(
                f"pace_window_laps ({self.pace_window_laps}) debe ser >= "
                f"min_samples pace ok ({pace_ok})"
            )
        fuel_ok = self.thresholds("fuel")[1]
        if self.fuel_window_laps < fuel_ok:
            raise ValueError(
                f"fuel_window_laps ({self.fuel_window_laps}) debe ser >= "
                f"min_samples fuel ok ({fuel_ok})"
            )
        return self

    def thresholds(self, family: str) -> tuple[int, int]:
        if family not in METRIC_FAMILIES:
            raise ValueError(
                f"Familia desconocida {family!r}; válidas: {', '.join(sorted(METRIC_FAMILIES))}"
            )
        thresholds = self.min_samples.get(family)
        if thresholds is not None:
            return thresholds.low, thresholds.ok
        return self.min_samples_low, self.min_samples_ok


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)


def load_config(path: Path | None) -> AppConfig:
    """Carga la configuración desde un TOML; sin ruta devuelve los valores por defecto."""
    if path is None:
        return AppConfig()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"No existe el archivo de configuración: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML inválido en {path}: {exc}") from exc
    return _validate(data)


def apply_overrides(
    cfg: AppConfig,
    *,
    listen_address: str | None = None,
    listen_port: int | None = None,
    allowed_sources: list[str] | None = None,
    capture_enabled: bool | None = None,
    capture_directory: Path | None = None,
) -> AppConfig:
    """Aplica opciones de línea de comandos y vuelve a validar el resultado."""
    data = cfg.model_dump(mode="json")
    if listen_address is not None:
        data["telemetry"]["listen_address"] = listen_address
    if listen_port is not None:
        data["telemetry"]["listen_port"] = listen_port
    if allowed_sources:
        data["telemetry"]["allowed_sources"] = allowed_sources
    if capture_enabled is not None:
        data["capture"]["enabled"] = capture_enabled
    if capture_directory is not None:
        data["capture"]["directory"] = str(capture_directory)
    return _validate(data)


def _validate(data: dict[str, Any]) -> AppConfig:
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"Configuración inválida: {details}") from exc
