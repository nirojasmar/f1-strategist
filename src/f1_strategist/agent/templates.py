"""Catálogo de plantillas de respaldo y formato único de cifras (Fase 6).

El respaldo determinista comparte cifras con el validador: los hechos se
formatean solo con ``format_fact`` y las plantillas nunca calculan ni redondean
por su cuenta. Una clave o un hecho ausente produce un texto seguro, nunca una
excepción, para que la captura nunca dependa de la traducción.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..events.types import Fact
from ..strategy.types import StrategyAction

_UNKNOWN = "desconocido"

#: Precisión y unidad de cada hecho numérico: ``nombre -> (decimales, unidad)``.
_FACT_SPECS: dict[str, tuple[int, str]] = {
    "gain_s": (2, "s"),
    # gain_low_s y gain_high_s comparten una sola unidad en el rango de la plantilla.
    "laps_remaining": (2, "vueltas"),
    "laps_to_ideal": (2, "vueltas"),
    "margin_laps": (2, "vueltas"),
    "margin_kg": (2, "kg"),
    "consumption_per_lap": (2, "kg/vuelta"),
    "age_laps": (2, "vueltas"),
    "wear_mean_pct": (2, "%"),
    "laps_left": (2, "vueltas"),
    "wear_rate_pct_per_lap": (2, "%/vuelta"),
    "expected_rate_pct_per_lap": (2, "%/vuelta"),
    "pace_trend_s_per_lap": (2, "s/vuelta"),
    "track_temperature_c": (2, "°C"),
    "air_temperature_c": (2, "°C"),
    "probability_pct": (2, "%"),
    "horizon_min": (2, "min"),
    "age_s": (2, "s"),
    "damage_pct": (2, "%"),
    "delta_pct": (2, "%"),
    "gap_s": (2, "s"),
}
_DEFAULT_DECIMALS = 2

# shortcut: el español usa el punto, como la consola histórica; la Fase 10 revisa
# el separador por locale al añadir idiomas.
_DECIMAL_SEPARATORS = {"es": "."}

_ACTION_LABELS = {
    StrategyAction.NO_CHANGE: "Mantener",
    StrategyAction.PIT_NOW: "Parar ahora",
    StrategyAction.PIT_IN: "Parar más adelante",
    StrategyAction.INSUFFICIENT_DATA: "Datos insuficientes",
}

_WEATHER_LABELS = {
    "clear": "despejado",
    "light_cloud": "poco nuboso",
    "overcast": "cubierto",
    "light_rain": "lluvia ligera",
    "heavy_rain": "lluvia intensa",
    "wet": "lluvia",
    "storm": "tormenta",
}

_EVENT_TEMPLATES = {
    "event.strategy_pit_recommended": (
        "Parada recomendada: {action}, vuelta {pit_lap}, {compound}; "
        "ganancia {gain_s} (rango {gain_low_s}–{gain_high_s} s), "
        "confianza {confidence}, {laps_remaining} restantes."
    ),
    "event.pit_window_open": (
        "Ventana de parada abierta: vuelta ideal {ideal_lap}, última {latest_lap}; "
        "faltan {laps_to_ideal} para la ideal."
    ),
    "event.fuel_margin_low": (
        "Margen de combustible bajo: {margin_laps} / {margin_kg}; "
        "quedan {laps_remaining} (consumo {consumption_per_lap})."
    ),
    "event.fuel_margin_critical": (
        "Margen de combustible crítico: {margin_laps} / {margin_kg}; "
        "quedan {laps_remaining} (consumo {consumption_per_lap})."
    ),
    "event.tyre_life_near_limit": (
        "Neumático {compound} cerca del límite: edad {age_laps}, "
        "desgaste {wear_mean_pct}, vida estimada {laps_left}."
    ),
    "event.tyre_degradation_abnormal": (
        "Degradación anormal: {wear_rate_pct_per_lap} frente a "
        "{expected_rate_pct_per_lap} esperados (x{ratio}); "
        "tendencia de ritmo {pace_trend_s_per_lap}."
    ),
    "event.safety_car_deployed": "Coche de seguridad desplegado en la vuelta {lap}.",
    "event.virtual_safety_car_deployed": "Virtual Safety Car desplegado en la vuelta {lap}.",
    "event.safety_car_cleared": "Coche de seguridad retirado en la vuelta {lap}.",
    "event.red_flag": "Bandera roja en la vuelta {lap}.",
    "event.weather_changed": (
        "Cambio de clima: {previous} → {current}; pista {track_temperature_c}, "
        "aire {air_temperature_c}."
    ),
    "event.rain_expected": "Lluvia prevista: probabilidad {probability_pct} en {horizon_min}.",
    "event.data_stale": "Datos de {group} obsoletos: {age_s}.",
    "event.damage_detected": "Daño en {component}: {damage_pct} (cambio {delta_pct}).",
    "event.rival_pit_stop": (
        "Parada de rival: {name} (coche {car_index}, posición {position}), "
        "hueco {gap_s}, vuelta {lap}."
    ),
}


def strategy_action_label(action: StrategyAction) -> str:
    """Etiqueta localizada de una acción estratégica."""
    return _ACTION_LABELS[action]


def format_fact(name: str, value: float | int | str | None, language: str = "es") -> str:
    """Formatea un hecho con su precisión y unidad; lo desconocido sigue siéndolo."""
    decimals, unit = _FACT_SPECS.get(name, (_DEFAULT_DECIMALS, ""))
    return f"{_fact_value_text(name, value, decimals, language)} {unit}".strip()


def _fact_value_text(
    name: str,
    value: float | int | str | None,
    decimals: int,
    language: str,
) -> str:
    if value is None:
        return _UNKNOWN
    if name == "action":
        try:
            return _ACTION_LABELS[StrategyAction(str(value))]
        except ValueError:
            return str(value)
    if name in {"previous", "current"}:
        return _WEATHER_LABELS.get(str(value), str(value))
    if name == "car_index" and isinstance(value, int) and not isinstance(value, bool):
        return str(value + 1)
    if isinstance(value, float):
        return _format_number(value, decimals, language)
    return str(value)


def _format_number(value: float, decimals: int, language: str) -> str:
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return text.replace(".", _DECIMAL_SEPARATORS.get(language, "."))


def render_template(message_key: str, facts: Iterable[Fact], language: str = "es") -> str:
    """Renderiza la plantilla de respaldo sin lanzar por claves o hechos ausentes."""
    template = _EVENT_TEMPLATES.get(message_key)
    if template is None:
        return _missing_translation(message_key)
    try:
        return template.format_map(_FactValues(facts, language))
    except (ValueError, KeyError):
        return _missing_translation(message_key)


class _FactValues(dict[str, str]):
    def __init__(self, facts: Iterable[Fact], language: str) -> None:
        self._language = language
        super().__init__((name, format_fact(name, value, language)) for name, value in facts)

    def __missing__(self, key: str) -> str:
        return format_fact(key, None, self._language)


def _missing_translation(message_key: str) -> str:
    return f"Falta la traducción al español del mensaje '{message_key}'."
