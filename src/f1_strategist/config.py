"""Configuración externa de la aplicación (TOML validado con pydantic)."""

from __future__ import annotations

import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
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

    enabled: bool = True
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


class StrategyTrackOverride(BaseModel):
    """Anulaciones de ``[strategy]`` para un circuito concreto.

    Solo se declaran los campos que cambian; el resto se hereda de la
    configuración global.
    """

    model_config = ConfigDict(extra="forbid")

    evaluation_min_interval_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    min_gain_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wear_slope_band_pct: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    pit_loss_band_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    prior_strength_k: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_pit_in_laps: int | None = Field(default=None, ge=1)
    require_two_dry_compounds: bool | None = None
    traffic_cost_s_per_lap: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cliff_penalty_s_per_lap: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    pit_loss_override_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class StrategyConfig(BaseModel):
    """Parámetros del motor de estrategia (sección 6 del plan de la Fase 4).

    Los valores marcados como provisionales se calibrarán en la Fase 9. Los
    campos sin valor universal quedan en ``None``: las funciones que dependen
    de ellos declaran ``unavailable`` en vez de inventar un número.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    evaluation_min_interval_s: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    min_gain_s: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    wear_slope_band_pct: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    pit_loss_band_s: float = Field(default=2.0, ge=0, allow_inf_nan=False)
    prior_strength_k: float = Field(default=3.0, ge=0, allow_inf_nan=False)
    max_pit_in_laps: int = Field(default=10, ge=1)
    require_two_dry_compounds: bool = True
    traffic_cost_s_per_lap: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cliff_penalty_s_per_lap: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    pit_loss_override_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tracks: dict[str, StrategyTrackOverride] = Field(default_factory=dict)

    @field_validator("tracks")
    @classmethod
    def _validate_track_names(
        cls, value: dict[str, StrategyTrackOverride]
    ) -> dict[str, StrategyTrackOverride]:
        normalized: dict[str, StrategyTrackOverride] = {}
        for name, override in value.items():
            key = name.strip().lower()
            if not key:
                raise ValueError("El nombre de circuito no puede estar vacío")
            if key in normalized:
                raise ValueError(f"Circuito duplicado en strategy.tracks: {key!r}")
            normalized[key] = override
        return normalized

    def for_track(self, track: str | None) -> StrategyConfig:
        """Devuelve la configuración efectiva para un circuito, sin mutar la original."""
        if track is None:
            return self
        override = self.tracks.get(track.strip().lower())
        if override is None:
            return self
        changes = override.model_dump(exclude_none=True)
        if not changes:
            return self
        return self.model_copy(update=changes)


class StrategyEventsConfig(BaseModel):
    """Parámetros de los avisos de estrategia (provisionales)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_confidence: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    update_delta_s: float = Field(default=2.0, ge=0, allow_inf_nan=False)


class PitWindowEventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lead_laps: int = Field(default=1, ge=0)


class FuelEventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warn_laps: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    critical_laps: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    hysteresis_laps: float = Field(default=0.3, gt=0, allow_inf_nan=False)
    # Sin valor universal: si se omite, el margen no fuerza actualizaciones.
    update_delta_laps: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _validate_order(self) -> FuelEventsConfig:
        if self.critical_laps > self.warn_laps:
            raise ValueError("critical_laps no puede ser mayor que warn_laps")
        return self


class TyreEventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warn_laps: float = Field(default=3.0, ge=0, allow_inf_nan=False)
    degradation_ratio: float = Field(default=0.25, ge=0, allow_inf_nan=False)
    hysteresis: float = Field(default=0.10, gt=0, allow_inf_nan=False)
    min_stint_laps: int = Field(default=4, ge=1)


class WeatherEventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rain_enter_pct: float = Field(default=50.0, ge=0, le=100, allow_inf_nan=False)
    rain_exit_pct: float = Field(default=30.0, ge=0, le=100, allow_inf_nan=False)
    # Sin valor universal: si se omite, la probabilidad no fuerza actualizaciones.
    update_delta_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def _validate_order(self) -> WeatherEventsConfig:
        if self.rain_exit_pct >= self.rain_enter_pct:
            raise ValueError("rain_exit_pct debe ser menor que rain_enter_pct")
        return self


class DataEventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stale_persistence_s: float = Field(default=3.0, gt=0, allow_inf_nan=False)


class DamageEventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warn_pct: float = Field(default=15.0, ge=0, le=100, allow_inf_nan=False)
    update_delta_pct: float = Field(default=10.0, ge=0, le=100, allow_inf_nan=False)


class RivalsEventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gap_s: float = Field(default=2.0, ge=0, allow_inf_nan=False)


class EventsConfig(BaseModel):
    """Detección de eventos (Fase 5); todos los valores son provisionales."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    strategy: StrategyEventsConfig = Field(default_factory=StrategyEventsConfig)
    pit_window: PitWindowEventsConfig = Field(default_factory=PitWindowEventsConfig)
    fuel: FuelEventsConfig = Field(default_factory=FuelEventsConfig)
    tyres: TyreEventsConfig = Field(default_factory=TyreEventsConfig)
    weather: WeatherEventsConfig = Field(default_factory=WeatherEventsConfig)
    data: DataEventsConfig = Field(default_factory=DataEventsConfig)
    damage: DamageEventsConfig = Field(default_factory=DamageEventsConfig)
    rivals: RivalsEventsConfig = Field(default_factory=RivalsEventsConfig)


class GateCooldownConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategic: float = Field(default=45.0, ge=0, allow_inf_nan=False)
    informational: float = Field(default=120.0, ge=0, allow_inf_nan=False)


class GateConfig(BaseModel):
    """Compuerta de avisos (Fase 5); todos los valores son provisionales."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    global_min_gap_s: float = Field(default=8.0, ge=0, allow_inf_nan=False)
    cooldown_s: GateCooldownConfig = Field(default_factory=GateCooldownConfig)
    equivalence_window_s: float = Field(default=90.0, ge=0, allow_inf_nan=False)
    update_min_interval_s: float = Field(default=30.0, ge=0, allow_inf_nan=False)
    defer_max_s: float = Field(default=20.0, ge=0, allow_inf_nan=False)
    quiet_first_laps: int = Field(default=1, ge=0)
    informational_space_s: float = Field(default=30.0, ge=0, allow_inf_nan=False)
    pilot_quiet_s: float = Field(default=15.0, ge=0, allow_inf_nan=False)
    interruption_level: Literal["all", "strategic", "critical"] = "all"
    muted: bool = False


class LLMConfig(BaseModel):
    """Endpoint compatible con OpenAI (Fase 6); valores provisionales."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    endpoint: str = "http://127.0.0.1:1234/v1"
    model: str = ""
    # SecretStr oculta la clave en repr y en los volcados (también en SQLite).
    api_key: SecretStr | None = None
    temperature: float = Field(default=0.3, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    seed: int | None = None
    max_tokens: int = Field(default=200, gt=0)
    stream: bool = True
    structured_output: Literal["auto", "json_schema", "json_object", "none"] = "auto"
    extra_body: dict[str, Any] = Field(default_factory=dict)
    connect_timeout_s: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    max_in_flight: int = Field(default=1, ge=1)
    health_interval_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("endpoint debe ser una URL http o https")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("endpoint no debe incluir credenciales")
        if not parsed.hostname:
            raise ValueError("endpoint debe incluir un host")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("endpoint tiene un puerto inválido") from exc
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_model(self) -> LLMConfig:
        if self.enabled and not self.model.strip():
            raise ValueError("model es obligatorio cuando el LLM está activado")
        return self

    @property
    def resolved_api_key(self) -> str | None:
        """Clave explícita o, si falta, la de ``F1_LLM_API_KEY``."""
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        return os.environ.get("F1_LLM_API_KEY") or None


class AgentConfig(BaseModel):
    """Comunicación del agente (Fase 6); valores provisionales."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(default="es", min_length=2, max_length=16)
    max_words: int = Field(default=30, gt=0)
    max_chars: int = Field(default=220, gt=0)
    critical_uses_llm: bool = False
    timeout_critical_s: float = Field(default=1.5, gt=0, allow_inf_nan=False)
    timeout_strategic_s: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    timeout_informational_s: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    fallback_on_failure: Literal["template", "drop"] = "template"
    max_alert_age_s: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    recent_messages: int = Field(default=3, ge=0)
    context_token_budget: int = Field(default=900, gt=0)
    offline_mode: Literal["template", "recorded", "live"] = "template"
    record_context: bool = True
    imperative_phrases: tuple[str, ...] = (
        "entra a boxes",
        "boxea ahora",
        "ataca ahora",
        "empuja ahora",
    )

    def timeout_for(self, priority: str) -> float:
        """Tiempo de espera según la prioridad; otras prioridades usan el informativo."""
        if priority == "critical":
            return self.timeout_critical_s
        if priority == "strategic":
            return self.timeout_strategic_s
        return self.timeout_informational_s


_PILOT_INTENT_NAMES = frozenset(
    {
        "general_race_state",
        "ask_strategy",
        "fuel_status",
        "tyre_life",
        "pace_diagnosis",
        "compare_driver",
        "pit_undercut",
        "gap_status",
        "damage_status",
        "weather_forecast",
        "unknown",
    }
)


class PilotConfig(BaseModel):
    """Preguntas del piloto (Fase 7); valores provisionales."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    language: str = Field(default="es", min_length=2, max_length=16)
    min_intent_confidence: float = Field(default=0.6, ge=0, le=1, allow_inf_nan=False)
    dialogue_turns: int = Field(default=3, ge=0)
    dialogue_ttl_s: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    clarification_ttl_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    max_question_chars: int = Field(default=200, gt=0)
    llm_followup: bool = True
    llm_followup_intents: tuple[str, ...] = (
        "ask_strategy",
        "pace_diagnosis",
        "pit_undercut",
    )
    ack_max_ms: int = Field(default=50, gt=0)
    followup_timeout_s: float = Field(default=12.0, gt=0, allow_inf_nan=False)
    followup_max_words: int = Field(default=40, gt=0)
    hedge_words: tuple[str, ...] = (
        "aproximadamente",
        "alrededor de",
        "estimado",
        "estimada",
        "en torno a",
        "posible",
        "probablemente",
    )
    pace_trend_stable_s: float = Field(default=0.05, ge=0, allow_inf_nan=False)
    undercut_horizon_laps: int = Field(default=3, ge=1)
    undercut_min_rival_clean_laps: int = Field(default=3, ge=1)
    history_size: int = Field(default=50, ge=1)
    record_text: bool = True

    @field_validator("llm_followup_intents")
    @classmethod
    def _validate_followup_intents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip().lower() for item in value)
        unknown = set(normalized) - _PILOT_INTENT_NAMES
        if unknown:
            raise ValueError(
                f"Intenciones desconocidas en llm_followup_intents: {', '.join(sorted(unknown))}"
            )
        return normalized


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    pilot: PilotConfig = Field(default_factory=PilotConfig)


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
    # Modo Python: el volcado JSON enmascara la clave de API y la perderíamos.
    data = cfg.model_dump(mode="python")
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
