"""Herramientas de estado, combustible, neumáticos, ritmo, daños y clima (Q5).

Cada herramienta lee instantáneas publicadas y devuelve hechos etiquetados.
No recalcula métricas ni estrategia: si un dato no está publicado, se declara
como ausente y la calidad baja a ``insufficient``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType

from ..metrics.types import Metric, Quality
from .types import FactKind, Intent, RoutedQuery, Tool, ToolFact, ToolInputs, ToolResult

_ABSENT_QUALITIES = (Quality.INSUFFICIENT, Quality.UNAVAILABLE)


def _number(metric: Metric | None) -> float | int | None:
    """Valor numérico utilizable de una métrica, o ``None`` si no lo hay."""
    if metric is None or metric.value is None or metric.quality in _ABSENT_QUALITIES:
        return None
    value = metric.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _metric_fact(name: str, metric: Metric | None, kind: FactKind) -> ToolFact | None:
    if metric is None or metric.value is None or metric.quality in _ABSENT_QUALITIES:
        return None
    value = metric.value
    if not isinstance(value, (int, float, str, bool)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return ToolFact(name=name, value=value, unit=metric.unit, kind=kind, quality=metric.quality)


def _add_metric(
    facts: list[ToolFact],
    missing: list[str],
    name: str,
    metric: Metric | None,
    kind: FactKind,
) -> None:
    fact = _metric_fact(name, metric, kind)
    if fact is None:
        missing.append(name)
    else:
        facts.append(fact)


def _add_state(
    facts: list[ToolFact],
    missing: list[str],
    name: str,
    value: float | int | str | bool | None,
    unit: str | None,
    stale: bool,
) -> None:
    if value is None:
        missing.append(name)
        return
    facts.append(
        ToolFact(
            name=name,
            value=value,
            unit=unit,
            kind=FactKind.MEASURED,
            quality=Quality.STALE if stale else Quality.OK,
        )
    )


def _group_stale(inputs: ToolInputs, group: str) -> bool:
    for freshness in inputs.state.freshness:
        if freshness.group == group:
            return freshness.is_stale(inputs.now)
    return False


def _is_stale(inputs: ToolInputs, *groups: str) -> bool:
    return any(_group_stale(inputs, group) for group in groups)


def _result(
    intent: Intent,
    inputs: ToolInputs,
    facts: list[ToolFact],
    missing: list[str],
    stale: bool,
) -> ToolResult:
    if not facts or any(fact.quality in _ABSENT_QUALITIES for fact in facts):
        quality = Quality.INSUFFICIENT
    elif stale or any(fact.quality is Quality.STALE for fact in facts):
        quality = Quality.STALE
    elif missing or any(fact.quality is Quality.LOW for fact in facts):
        quality = Quality.LOW
    else:
        quality = Quality.OK
    return ToolResult(
        intent=intent,
        quality=quality,
        facts=tuple(facts),
        missing=tuple(missing),
        stale=stale,
        data_lap=inputs.state.current_lap,
    )


def get_race_state(query: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Estado general de la sesión y del coche del jugador."""
    session = inputs.state.session
    car = inputs.state.player_car
    stale = _is_stale(inputs, "session", "car_status")
    facts: list[ToolFact] = []
    missing: list[str] = []

    _add_state(facts, missing, "session_type", session.session_type, None, stale)
    _add_state(facts, missing, "track", session.track, None, stale)
    _add_state(facts, missing, "current_lap", inputs.state.current_lap, "lap", stale)
    _add_state(facts, missing, "total_laps", session.total_laps, "lap", stale)
    if car is not None:
        _add_state(facts, missing, "position", car.position, None, stale)
        _add_state(facts, missing, "tyre_compound", car.actual_tyre_compound, None, stale)
        _add_state(facts, missing, "tyre_age_laps", car.tyre_age_laps, "lap", stale)
    else:
        missing.extend(("position", "tyre_compound", "tyre_age_laps"))
    _add_state(facts, missing, "safety_car_status", session.safety_car_status, None, stale)
    _add_state(facts, missing, "weather", session.weather, None, stale)
    return _result(Intent.GENERAL_RACE_STATE, inputs, facts, missing, stale)


def estimate_fuel_margin(query: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Combustible disponible y margen estimado hasta el final."""
    car = inputs.state.player_car
    metrics = inputs.metrics
    restricted = car is not None and car.telemetry_restricted is True
    stale = _is_stale(inputs, "car_status", "lap_data", "session")
    facts: list[ToolFact] = []
    missing: list[str] = []

    fuel_kg = None if restricted else _number(metrics.get("fuel.in_tank_kg"))
    if fuel_kg is None and not restricted and car is not None:
        fuel_kg = car.fuel_kg
    if isinstance(fuel_kg, (int, float)) and fuel_kg <= 0:
        fuel_kg = None
    if fuel_kg is None:
        missing.append("fuel_in_tank_kg")
    else:
        facts.append(
            ToolFact(
                name="fuel_in_tank_kg",
                value=fuel_kg,
                unit="kg",
                kind=FactKind.MEASURED,
                quality=Quality.STALE if stale else Quality.OK,
            )
        )

    _add_metric(
        facts,
        missing,
        "fuel_consumption_per_lap",
        metrics.get("fuel.consumption_per_lap"),
        FactKind.MEASURED,
    )
    _add_metric(
        facts, missing, "fuel_laps_of_fuel", metrics.get("fuel.laps_of_fuel"), FactKind.ESTIMATED
    )
    _add_metric(
        facts,
        missing,
        "fuel_margin_laps",
        metrics.get("fuel.margin_laps_at_finish"),
        FactKind.ESTIMATED,
    )
    _add_metric(
        facts,
        missing,
        "fuel_margin_kg",
        metrics.get("fuel.margin_kg_at_finish"),
        FactKind.ESTIMATED,
    )
    return _result(Intent.FUEL_STATUS, inputs, facts, missing, stale)


def estimate_tyre_life(query: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Vida restante del neumático, indicando la fuente de la estimación."""
    car = inputs.state.player_car
    metrics = inputs.metrics
    stale = _is_stale(inputs, "car_status", "tyre_sets", "lap_data")
    facts: list[ToolFact] = []
    missing: list[str] = []

    if car is not None:
        _add_state(facts, missing, "tyre_compound", car.actual_tyre_compound, None, stale)
        _add_state(facts, missing, "tyre_age_laps", car.tyre_age_laps, "lap", stale)
    else:
        missing.extend(("tyre_compound", "tyre_age_laps"))
    _add_metric(
        facts, missing, "tyre_wear_mean_pct", metrics.get("tyres.wear_mean_pct"), FactKind.MEASURED
    )

    game_laps = _number(metrics.get("tyres.game_lifespan_laps"))
    wear_limit_laps = _number(metrics.get("tyres.laps_to_wear_limit"))
    wear_rate = _number(metrics.get("tyres.wear_rate_pct_per_lap"))
    wear_mean = _number(metrics.get("tyres.wear_mean_pct"))

    if game_laps is not None:
        life_laps, source, kind = game_laps, "game", FactKind.MEASURED
    elif wear_limit_laps is not None:
        life_laps, source, kind = wear_limit_laps, "wear_limit", FactKind.ESTIMATED
    elif wear_rate is not None and wear_rate > 0 and wear_mean is not None:
        life_laps, source, kind = (100.0 - wear_mean) / wear_rate, "model", FactKind.ESTIMATED
    else:
        life_laps, source, kind = None, None, FactKind.ESTIMATED

    if life_laps is None or source is None:
        missing.append("tyre_life_laps")
    else:
        facts.append(
            ToolFact(
                name="tyre_life_laps",
                value=life_laps,
                unit="lap",
                kind=kind,
                quality=Quality.STALE if stale else Quality.OK,
            )
        )
        facts.append(
            ToolFact(
                name="tyre_life_source",
                value=source,
                unit=None,
                kind=kind,
                quality=Quality.STALE if stale else Quality.OK,
            )
        )
    return _result(Intent.TYRE_LIFE, inputs, facts, missing, stale)


def get_recent_pace_trend(query: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Tendencia reciente de ritmo y su clasificación con umbral configurable."""
    metrics = inputs.metrics
    stale = _is_stale(inputs, "lap_data", "session")
    facts: list[ToolFact] = []
    missing: list[str] = []

    trend = _number(metrics.get("pace.trend_s_per_lap"))
    if trend is None:
        missing.append("pace_trend_s_per_lap")
    else:
        if abs(trend) <= inputs.config.pace_trend_stable_s:
            label = "stable"
        elif trend > 0:
            label = "worsening"
        else:
            label = "improving"
        quality = Quality.STALE if stale else Quality.OK
        facts.append(ToolFact("pace_trend_s_per_lap", trend, "s/lap", FactKind.ESTIMATED, quality))
        facts.append(ToolFact("pace_trend_label", label, None, FactKind.ESTIMATED, quality))

    _add_metric(facts, missing, "last_lap_s", metrics.get("pace.last_lap_s"), FactKind.MEASURED)
    _add_metric(facts, missing, "best_lap_s", metrics.get("pace.best_lap_s"), FactKind.MEASURED)
    _add_metric(
        facts, missing, "clean_laps_used", metrics.get("pace.clean_laps_used"), FactKind.MEASURED
    )
    return _result(Intent.PACE_DIAGNOSIS, inputs, facts, missing, stale)


def damage_status(query: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Daños medidos del coche y averías activas."""
    metrics = inputs.metrics
    stale = _is_stale(inputs, "car_damage", "car_status")
    facts: list[ToolFact] = []
    missing: list[str] = []

    for name, key in (
        ("damage_aero_max_pct", "damage.aero_max_pct"),
        ("damage_floor_pct", "damage.floor_pct"),
        ("damage_engine_max_pct", "damage.engine_max_pct"),
        ("damage_gearbox_pct", "damage.gearbox_pct"),
    ):
        _add_metric(facts, missing, name, metrics.get(key), FactKind.MEASURED)

    faults = metrics.get("damage.faults")
    if faults is not None and faults.value and faults.quality not in _ABSENT_QUALITIES:
        fault_names = tuple(str(item) for item in faults.value)  # type: ignore[union-attr]
        quality = Quality.STALE if stale else faults.quality
        facts.append(
            ToolFact("damage_fault_count", len(fault_names), "faults", FactKind.MEASURED, quality)
        )
        if fault_names:
            facts.append(
                ToolFact("damage_faults", ",".join(fault_names), None, FactKind.MEASURED, quality)
            )
    else:
        missing.append("damage_faults")
    return _result(Intent.DAMAGE_STATUS, inputs, facts, missing, stale)


def weather_forecast(query: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Clima actual y previsión publicada por el juego."""
    session = inputs.state.session
    metrics = inputs.metrics
    stale = _is_stale(inputs, "session")
    facts: list[ToolFact] = []
    missing: list[str] = []

    weather = _metric_fact("weather", metrics.get("env.weather"), FactKind.MEASURED)
    if weather is None:
        _add_state(facts, missing, "weather", session.weather, None, stale)
    else:
        facts.append(weather)

    _add_metric(
        facts,
        missing,
        "track_temperature_c",
        metrics.get("env.track_temperature_c"),
        FactKind.MEASURED,
    )
    _add_metric(
        facts,
        missing,
        "air_temperature_c",
        metrics.get("env.air_temperature_c"),
        FactKind.MEASURED,
    )
    _add_metric(
        facts,
        missing,
        "rain_probability_next_10_min_pct",
        metrics.get("env.rain_probability_next_10_min_pct"),
        FactKind.ESTIMATED,
    )
    _add_metric(
        facts,
        missing,
        "rain_probability_next_30_min_pct",
        metrics.get("env.rain_probability_next_30_min_pct"),
        FactKind.ESTIMATED,
    )
    _add_metric(
        facts,
        missing,
        "track_temperature_trend_c_per_lap",
        metrics.get("env.track_temperature_trend_c_per_lap"),
        FactKind.ESTIMATED,
    )

    if not session.forecast:
        missing.append("forecast")
    for point in session.forecast:
        quality = Quality.STALE if stale else Quality.OK
        prefix = f"forecast_plus_{point.offset_minutes}_min"
        if point.weather is not None:
            facts.append(
                ToolFact(f"{prefix}_weather", point.weather, None, FactKind.ESTIMATED, quality)
            )
        facts.append(
            ToolFact(f"{prefix}_rain_pct", point.rain_percentage, "%", FactKind.ESTIMATED, quality)
        )
    return _result(Intent.WEATHER_FORECAST, inputs, facts, missing, stale)


# Q5 registra aquí sus intenciones; Q7 puede sustituir PACE_DIAGNOSIS al integrar.
TOOLS: Mapping[Intent, Tool] = MappingProxyType(
    {
        Intent.GENERAL_RACE_STATE: get_race_state,
        Intent.FUEL_STATUS: estimate_fuel_margin,
        Intent.TYRE_LIFE: estimate_tyre_life,
        Intent.PACE_DIAGNOSIS: get_recent_pace_trend,
        Intent.DAMAGE_STATUS: damage_status,
        Intent.WEATHER_FORECAST: weather_forecast,
    }
)

__all__ = [
    "TOOLS",
    "damage_status",
    "estimate_fuel_margin",
    "estimate_tyre_life",
    "get_race_state",
    "get_recent_pace_trend",
    "weather_forecast",
]
