"""Configuración externa de la aplicación (TOML validado con pydantic)."""

from __future__ import annotations

import ipaddress
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


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


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)


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
