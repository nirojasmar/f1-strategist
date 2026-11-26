"""Spanish rendering of core diagnostic message keys for the console."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .agent.templates import render_template, strategy_action_label
from .events.types import Fact
from .messages import DiagnosticMessage
from .metrics.engine import MetricsSnapshot
from .metrics.types import Metric, Quality
from .packets import packet_name
from .parser import ParseIssue
from .pilot.types import Clarification, FactKind, Intent, PilotAnswer
from .race_state import CarSnapshot, RaceEvent, RaceStateSnapshot
from .receiver import PacketSequenceStats
from .strategy.types import (
    BASELINE_STAY_OUT,
    StrategyAction,
    StrategyAlternative,
    StrategyEvaluation,
)

METRICS_PANEL_TITLE = "Métricas"
STRATEGY_PANEL_TITLE = "Estrategia"
EVENTS_PANEL_TITLE = "Avisos del agente"
AGENT_PANEL_TITLE = "Radio"
AGENT_STATUS_LABEL = "LLM"
AGENT_LATENCY_LABEL = "Última latencia"
AGENT_COUNTERS_LABEL = "Usados / respaldos / descartados"
AGENT_MESSAGES_TITLE = "Mensajes de radio"
AGENT_MESSAGES_COLUMNS = ("Tiempo", "Prioridad", "Vuelta", "Origen", "Texto")
AGENT_EMPTY_MESSAGES = "La sesión {session_id} no tiene mensajes que coincidan."
AGENT_SUMMARY_TITLE = "Resumen del agente · sesión {session_id}"
AGENT_SUMMARY_SOURCES_TITLE = "Mensajes por origen"
AGENT_SUMMARY_FALLBACKS_TITLE = "Respaldos por causa"
AGENT_SUMMARY_VALIDATION_TITLE = "Errores de validación"
AGENT_SUMMARY_LATENCY_TITLE = "Latencia total de llamadas"
AGENT_SUMMARY_SOURCE_COLUMN = "Origen"
AGENT_SUMMARY_CAUSE_COLUMN = "Causa"
AGENT_SUMMARY_CODE_COLUMN = "Código"
AGENT_SUMMARY_PERCENTILE_COLUMN = "Percentil"
AGENT_SUMMARY_SECONDS_COLUMN = "Segundos"
AGENT_SCHEMA_ERROR = (
    "Los comandos agent requieren esquema v{required}; la base actual es v{actual}."
)
AGENT_READER_API_ERROR = "La base de datos no ofrece todavía la API de lectura del agente L9."
AGENT_EXPORT_OVERWRITES_DB = "La salida no puede sobrescribir la base de datos."
AGENT_EXPORT_OVERWRITES_CAPTURE = "La salida no puede sobrescribir la captura asociada."
AGENT_EXPORT_WRITTEN = "Exportación del agente de la sesión {session_id} escrita en {path}"
LLM_EVAL_OVERWRITES_DB = "El informe no puede sobrescribir la base de datos de sesiones."
LLM_EVAL_OVERWRITES_CAPTURE = "El informe no puede sobrescribir la captura de la sesión."
LLM_EVAL_OVERWRITES_CONFIG = "El informe no puede sobrescribir la configuración."
LLM_EVAL_OVERWRITES_DATASET = "El informe no puede escribirse dentro del conjunto de casos."
LLM_EVAL_SKIPPED_CALLS = "Llamadas omitidas por contexto no recuperable: {calls}"
AGENT_UNKNOWN_VALUE = "desconocido"
AGENT_EXPORT_SCHEMA_VERSION = 1

PILOT_PANEL_TITLE = "Preguntas"
PILOT_QUESTION_LABEL = "Tú"
PILOT_ANSWER_LABEL = "Ingeniero"
PILOT_EMPTY = "Sin preguntas todavía."
PILOT_ACK_TEXT = "Recibido: consultando…"
PILOT_UNAVAILABLE_TEXT = "El ingeniero no está disponible."
PILOT_CLARIFICATION_LABEL = "Aclaración pendiente"
PILOT_INPUT_PROMPT = "Pregunta> "
PILOT_INPUT_HELP_RUN = (
    "Escribe tu pregunta · Enter: enviar · Esc: borrar · ↑↓: historial · Ctrl+C: salir"
)
PILOT_PLAY_HELP = "espacio: pausa · +/-: velocidad · /: preguntar · q: salir"
PILOT_PLAY_HELP_DISABLED = "espacio: pausa · +/-: velocidad · q: salir"
PILOT_INPUT_DISABLED = "Entrada de preguntas desactivada."
PILOT_INPUT_NO_TERMINAL = "Sin terminal interactiva: entrada de preguntas desactivada."
PILOT_INPUT_ERROR = "No se pudo enviar la pregunta."
PILOT_INPUT_BUSY = "Hay una pregunta en curso; espera a la respuesta."

PILOT_MISSING_TEMPLATE = "No tengo una respuesta preparada para eso."
PILOT_UNKNOWN_TEXT = (
    "No he entendido la pregunta. Puedes preguntarme por combustible, "
    "neumáticos, rivales o estrategia."
)
PILOT_CLARIFICATION_TEXT = "¿A cuál te refieres: {options}?"
PILOT_CLARIFICATION_NO_OPTIONS_TEXT = "¿A cuál te refieres?"
PILOT_LEADER_TEXT = "Vas primero: no hay nadie delante."
PILOT_LAST_TEXT = "Vas último: no hay nadie detrás."
PILOT_NO_CAR_AHEAD_TEXT = "No hay nadie delante de ti."
PILOT_NO_CAR_BEHIND_TEXT = "No hay nadie detrás de ti."
PILOT_DRIVER_RETIRED_TEXT = "El piloto que pides está retirado y no tengo datos suyos."
PILOT_DRIVER_UNKNOWN_TEXT = "No sé a qué piloto te refieres; no tengo datos de esa referencia."
PILOT_STALE_TEXT = " Los datos están desactualizados."
PILOT_MISSING_TEXT = " No tengo {missing}."
PILOT_GENERIC_TEXT = "Datos: {facts}."
PILOT_NO_DATA_TEXT = "No tengo datos ahora mismo."
PILOT_UNSUPPORTED_OVERCUT_TEXT = (
    "El overcut no está soportado; solo puedo evaluar undercuts ofensivos."
)
PILOT_UNSUPPORTED_DEFENSIVE_UNDERCUT_TEXT = (
    "El undercut defensivo no está soportado; solo puedo evaluar undercuts ofensivos."
)

_PILOT_INTENT_LEADS: Mapping[Intent, tuple[str, str]] = MappingProxyType(
    {
        Intent.GENERAL_RACE_STATE: ("Estado de carrera", "estado de carrera"),
        Intent.FUEL_STATUS: ("Combustible", "combustible"),
        Intent.TYRE_LIFE: ("Neumáticos", "neumáticos"),
        Intent.COMPARE_DRIVER: ("Comparación", "comparación de pilotos"),
        Intent.GAP_STATUS: ("Intervalos", "intervalos"),
        Intent.DAMAGE_STATUS: ("Daños", "daños"),
        Intent.WEATHER_FORECAST: ("Clima", "clima"),
        Intent.ASK_STRATEGY: ("Estrategia", "estrategia"),
        Intent.PACE_DIAGNOSIS: ("Ritmo", "ritmo"),
        Intent.PIT_UNDERCUT: ("Undercut", "undercut"),
    }
)


def _build_pilot_templates() -> dict[str, str]:
    templates = {
        "pilot.unknown": PILOT_UNKNOWN_TEXT,
        "pilot.clarification": PILOT_CLARIFICATION_TEXT,
        "pilot.clarification_no_options": PILOT_CLARIFICATION_NO_OPTIONS_TEXT,
        "pilot.leader": PILOT_LEADER_TEXT,
        "pilot.last": PILOT_LAST_TEXT,
        "pilot.no_car_ahead": PILOT_NO_CAR_AHEAD_TEXT,
        "pilot.no_car_behind": PILOT_NO_CAR_BEHIND_TEXT,
        "pilot.driver_retired": PILOT_DRIVER_RETIRED_TEXT,
        "pilot.driver_unknown": PILOT_DRIVER_UNKNOWN_TEXT,
        "pilot.stale": PILOT_STALE_TEXT,
        "pilot.missing": PILOT_MISSING_TEXT,
        "pilot.generic": PILOT_GENERIC_TEXT,
        "pilot.no_data": PILOT_NO_DATA_TEXT,
        "pilot.unsupported.overcut": PILOT_UNSUPPORTED_OVERCUT_TEXT,
        "pilot.unsupported.defensive_undercut": PILOT_UNSUPPORTED_DEFENSIVE_UNDERCUT_TEXT,
        "pilot.missing_template": PILOT_MISSING_TEMPLATE,
    }
    for intent, (lead, topic) in _PILOT_INTENT_LEADS.items():
        prefix = f"pilot.answer.{intent.value}"
        templates[prefix] = f"{lead}: {{facts}}."
        templates[f"{prefix}.partial"] = f"{lead}: {{facts}}. No tengo {{missing}}."
        templates[f"{prefix}.no_data"] = f"No tengo datos de {topic} ahora mismo."
    return templates


PILOT_ANSWER_TEMPLATES = MappingProxyType(_build_pilot_templates())

PILOT_FACT_LABELS = MappingProxyType(
    {
        "action": "acción",
        "age_laps": "edad",
        "air_temperature_c": "temperatura del aire",
        "best_k": "mejor k",
        "best_gain_s": "mejor ganancia",
        "best_lap_s": "mejor vuelta",
        "car_index": "coche",
        "clean_laps_used": "vueltas limpias usadas",
        "compare.interval_direction": "sentido del intervalo",
        "compare.interval_s": "intervalo",
        "compare.pace_delta_s": "diferencia de ritmo",
        "compare.rival.compound": "compuesto del rival",
        "compare.rival.name": "rival",
        "compare.rival.pit_stops": "paradas del rival",
        "compare.rival.position": "posición del rival",
        "compare.rival.retired": "rival retirado",
        "compare.rival.tyre_age_laps": "edad de neumáticos del rival",
        "compound": "compuesto",
        "component": "componente",
        "confidence": "confianza",
        "consumption_per_lap": "consumo por vuelta",
        "current_lap": "vuelta actual",
        "damage_aero_max_pct": "daño aerodinámico",
        "damage_engine_max_pct": "daño del motor",
        "damage_fault_count": "averías",
        "damage_faults": "averías",
        "damage_floor_pct": "daño del suelo",
        "damage_gearbox_pct": "daño de la caja de cambios",
        "damage_pct": "daño",
        "fuel_consumption_per_lap": "consumo por vuelta",
        "fuel_in_tank_kg": "combustible restante",
        "fuel_kg": "combustible restante",
        "fuel_laps_of_fuel": "vueltas de combustible",
        "fuel_margin_kg": "margen de combustible",
        "fuel_margin_laps": "margen de combustible",
        "gain_s": "ganancia",
        "gap.ahead_s": "intervalo con el de delante",
        "gap.ahead_trend_s_per_s": "tendencia del intervalo delantero",
        "gap.behind_s": "intervalo con el de atrás",
        "gap.behind_trend_s_per_s": "tendencia del intervalo trasero",
        "gap.leader_s": "intervalo con el líder",
        "gap_ahead_s": "intervalo con el de delante",
        "gap_behind_s": "intervalo con el de atrás",
        "gap_s": "intervalo",
        "horizon_min": "horizonte",
        "lap": "vuelta",
        "laps_left": "vida estimada",
        "laps_remaining": "vueltas restantes",
        "last_lap_s": "última vuelta",
        "margin_kg": "margen",
        "margin_laps": "margen",
        "name": "piloto",
        "pace.clean_laps": "vueltas limpias",
        "pace.current_s": "ritmo actual",
        "pace.evidence.damage_delta_pct": "cambio de daño",
        "pace.evidence.damage_laps": "vueltas con daño",
        "pace.evidence.sector": "sector lento",
        "pace.evidence.sector_loss_s": "pérdida en el sector",
        "pace.evidence.traffic_laps": "vueltas con tráfico",
        "pace.evidence.tyre_age_delta_laps": "aumento de la edad del neumático",
        "pace.evidence.tyre_wear_delta_pct": "aumento del desgaste",
        "pace.evidence.weather_change_laps": "vueltas con cambio de clima",
        "pace.loss_s": "pérdida de ritmo",
        "pace.reference_kind": "tipo de referencia",
        "pace.reference_s": "vuelta de referencia",
        "pace.stable": "ritmo estable",
        "pace_delta_s_per_lap": "diferencia de ritmo",
        "pace_trend_s_per_lap": "tendencia de ritmo",
        "pace_trend_label": "tendencia de ritmo",
        "pit_lap": "vuelta de parada",
        "pit_loss_s": "pérdida en boxes",
        "position": "posición",
        "probability_pct": "probabilidad",
        "rain_probability_next_10_min_pct": "probabilidad de lluvia en 10 min",
        "rain_probability_next_30_min_pct": "probabilidad de lluvia en 30 min",
        "rejoin_position": "posición de reincorporación",
        "rival_car_index": "coche rival",
        "safety_car_status": "estado del coche de seguridad",
        "session_type": "tipo de sesión",
        "strategy_action": "acción",
        "strategy_compound": "compuesto",
        "strategy_confidence": "confianza",
        "strategy_gain_high_s": "ganancia máxima",
        "strategy_gain_low_s": "ganancia mínima",
        "strategy_gain_s": "ganancia",
        "strategy_pit_lap": "vuelta de parada",
        "strategy_rejoin_position": "posición de reincorporación",
        "strategy_remaining_time_s": "tiempo restante",
        "strategy_safety_car": "coche de seguridad activo",
        "strategy_set_index": "juego",
        "strategy_simulated": "simulación",
        "strategy_traffic_risk": "riesgo de tráfico",
        "total_laps": "vueltas totales",
        "track": "circuito",
        "track_temperature_c": "temperatura de pista",
        "track_temperature_trend_c_per_lap": "tendencia de la temperatura de pista",
        "tyre_age_laps": "edad de neumáticos",
        "tyre_compound": "compuesto",
        "tyre_life_laps": "vida del neumático",
        "tyre_life_source": "fuente de la vida del neumático",
        "tyre_wear_mean_pct": "desgaste",
        "undercut_gain_s": "ganancia del undercut",
        "viable": "viable",
        "wear_mean_pct": "desgaste",
        "weather": "clima",
    }
)

PILOT_KIND_WORDS = MappingProxyType(
    {
        FactKind.MEASURED: "medido",
        FactKind.ESTIMATED: "estimado",
        FactKind.RECOMMENDED: "recomendado",
    }
)


def pilot_fact_label(name: str) -> str:
    """Etiqueta localizada de un hecho; nunca devuelve un nombre crudo conocido."""
    label = PILOT_FACT_LABELS.get(name)
    if label is not None:
        return label
    if name.startswith("pace.cause."):
        return "causa"
    if name.startswith("forecast_plus_"):
        return "previsión"
    return name


_PILOT_KIND_LABELS = {
    "ack": "Acuse",
    "answer": "Respuesta",
    "clarification": "Aclaración",
    "unknown": "Desconocido",
    "unavailable": "No disponible",
    "followup": "Seguimiento",
}
_PILOT_MESSAGE_TEXTS = {
    "pilot_ack": PILOT_ACK_TEXT,
    "pilot_unavailable": PILOT_UNAVAILABLE_TEXT,
}

#: Marcadores del panel: distinguen «no disponible» de «no aplica a la acción».
_STRATEGY_UNKNOWN = "Desconocido"
_STRATEGY_NOT_APPLICABLE = "No aplica"
_AGENT_STATUS_LABELS = {
    "disabled": "desactivado",
    "unknown": "sin comprobar",
    "available": "disponible",
    "unavailable": "no disponible",
}
_AGENT_SOURCE_LABELS = {"llm": "LLM", "template": "plantilla"}
_AGENT_PRIORITY_LABELS = {
    "critical": "crítica",
    "strategic": "estratégica",
    "informational": "informativa",
}

_METRIC_ROWS = (
    ("progress.laps_remaining", "Vueltas restantes"),
    ("fuel.margin_laps_at_finish", "Margen de combustible"),
    ("tyres.wear_rate_pct_per_lap", "Tasa de desgaste"),
    ("pace.trend_s_per_lap", "Tendencia de ritmo"),
    ("gap.ahead_s", "Intervalo delante"),
    ("gap.behind_s", "Intervalo detrás"),
)

_QUALITY_LABELS = {
    Quality.OK: "ok",
    Quality.LOW: "low (bajo)",
    Quality.STALE: "stale (obsoleto)",
    Quality.INSUFFICIENT: "insufficient (insuficiente)",
    Quality.UNAVAILABLE: "unavailable (no disponible)",
}

_UNIT_LABELS = {
    "lap": "vueltas",
    "kg": "kg",
    "s": "s",
    "s/lap": "s/lap",
    "%/lap": "%/lap",
}

_LIVE_METRIC_KEYS = frozenset(
    {
        "progress.laps_remaining",
        "fuel.margin_laps_at_finish",
        "gap.ahead_s",
        "gap.behind_s",
    }
)

_STRATEGY_KEY_LABELS = {
    "fuel_effect_not_separated": "Efecto del combustible no separado",
    "rivals_do_not_pit": "Se supone que los rivales no paran",
    "rejoin_current_gaps_not_projected": (
        "La reincorporación usa huecos actuales; no proyecta el tráfico hasta la vuelta futura"
    ),
    "rival_gap_unavailable": (
        "Faltan huecos de uno o más rivales; no se publica un rango de reincorporación"
    ),
    "profile_prior_used": "Se usó el perfil del fin de semana como prior",
    "damage_not_modelled": "Daños del coche no modelados",
    "tyre_damage_not_modelled": "Daño de neumáticos no modelado",
    "traffic_not_priced": "Coste del tráfico no cuantificado",
    "tyre_life_exceeded": "La vida útil de un juego puede ser insuficiente",
    "wet_conditions": "Condiciones de lluvia fuera del modelo",
    "safety_car_conditions": "Neutralización fuera del modelo",
    "red_flag_conditions": "Bandera roja fuera del modelo",
    "unmodeled_condition": "Condición fuera del modelo; recomendación suspendida",
    "mandatory_stop": "Parada obligatoria; se muestra el plan de referencia",
    "stay_out_unavailable": "No se pudo evaluar continuar con el juego montado",
    "alternative_quality_insufficient": "Calidad insuficiente en las opciones de parada",
    "pace_quality_insufficient": "Calidad insuficiente en el modelo de ritmo",
    "pit_loss_unavailable": "Pérdida en boxes no disponible",
    "no_pit_loss_source": "Pérdida en boxes no disponible",
    "fixed_band": "Banda de incertidumbre configurada",
    "circuit_pit_loss_prior": "Prior de pérdida por circuito",
    "circuit_pit_loss_not_official": "El valor del CSV no es oficial; se presenta como estimación",
    "circuit_vsc_pit_loss_estimate": "Pérdida en boxes estimada bajo VSC",
    "circuit_sc_pit_loss_estimate": "Pérdida en boxes estimada bajo SC",
    "circuit_sc_uncertainty": "La fuente indica la incertidumbre de SC",
    "data_stale": "Datos obsoletos",
    "wear_slope": "Pendiente de desgaste",
    "pit_loss_s": "Pérdida en boxes",
    "decision_without_alternatives": "No hay alternativas que evaluar",
    "decision_default_no_change": "Ninguna parada superó el mínimo de ganancia",
    "session_is_not_a_race": "La sesión actual no es una carrera",
    "laps_remaining_unavailable": "Vueltas restantes no disponibles o agotadas",
    "alternative_unstable": "Una alternativa de parada no se sostiene dentro de la incertidumbre",
    "baseline_alternative": "Alternativa de referencia (línea base), no una recomendación",
    "two_compounds_satisfied": "Regla de dos compuestos secos ya cumplida",
    "two_compounds_unverified": "No se puede verificar la regla de dos compuestos secos",
    "pace_conditioned_by_traffic": "Ritmo condicionado por tráfico; no es ritmo en aire limpio",
    "laps_excluded_after_damage": "Se excluyeron vueltas posteriores a cambios de daño",
    "prior_excluded_insufficient": (
        "Prior del fin de semana descartado por insuficiente; estimación de carrera"
    ),
    "repair_pit_loss_samples_excluded": "Muestras de boxes con reparación excluidas",
    "repair_duration_unknown": "Duración de la reparación desconocida",
    "damage_signal_missing": "Sin señal de daño; no se pudo separar su efecto",
    "provisional_field_pit_loss": "Pérdida en boxes provisional con muestras de carrera",
}

_PIT_LOSS_SOURCE_LABELS = {
    "override": "anulación de configuración",
    "field": "muestras de carrera",
    "profile": "perfil del fin de semana",
    "lane": "tiempo de carril de boxes",
    "circuit": "prior de circuito (estimación)",
    "none": "sin fuente",
}


def _number(item: DiagnosticMessage, name: str) -> float:
    value = item.get(name)
    if not isinstance(value, (int, float)):
        raise TypeError(f"Diagnostic parameter {name!r} must be numeric.")
    return float(value)


def _render_parse_issue(issue: ParseIssue) -> str:
    params = dict(issue.parameters)
    if issue.code == "packet_size_mismatch":
        return (
            f"Paquete {params['packet']} (ID {params['packet_id']}) de "
            f"{params['actual']} bytes; se esperaban {params['expected']}."
        )
    if issue.code == "packet_header_too_short":
        return (
            f"Datagrama de {params['actual']} bytes: no contiene la cabecera UDP "
            f"completa de {params['expected']} bytes."
        )
    if issue.code == "unsupported_format":
        return (
            f"Formato o versión UDP no soportados: formato {params['packet_format']}, "
            f"año {params['game_year']}, juego {params['game_major']}.{params['game_minor']}, "
            f"paquete v{params['packet_version']}."
        )
    if issue.code == "unsupported_packet_type":
        return f"Paquete {params['packet']} (ID {params['packet_id']}) sin decodificador."
    if issue.code == "percentage_out_of_range":
        return f"El campo {params['field']} tiene un porcentaje fuera de 0-100: {params['value']}."
    messages = {
        "non_finite_float": "El paquete contiene un valor flotante no finito.",
        "session_count_exceeded": "Session declara más zonas o pronósticos que el máximo.",
        "session_layout_size_mismatch": "El layout Session no consume el tamaño completo.",
        "session_weekend_count_exceeded": "Session declara más de 12 sesiones en el fin de semana.",
        "participants_count_exceeded": (
            "Participants declara más coches activos que el máximo de 22."
        ),
        "classification_count_exceeded": (
            "FinalClassification declara más coches que el máximo de 22."
        ),
        "classification_stints_exceeded": (
            "FinalClassification declara más de 8 stints de neumáticos."
        ),
        "session_history_count_exceeded": "SessionHistory excede la capacidad de sus historiales.",
        "other_parser_rejections_limited": (
            "Otros rechazos de parser (mensajes distintos limitados)."
        ),
    }
    return messages.get(issue.code, f"Error de parser no traducido: {issue.code}.")


def _render_sequence_anomalies(
    items: tuple[tuple[int, PacketSequenceStats], ...],
) -> str:
    details = []
    for packet_id, stats in items:
        parts = []
        if stats.frame_gaps:
            parts.append(f"{stats.frame_gaps} huecos de frame (posibles pérdidas)")
        if stats.duplicates:
            parts.append(f"{stats.duplicates} duplicados")
        if stats.out_of_order:
            parts.append(f"{stats.out_of_order} fuera de orden")
        details.append(f"{packet_name(packet_id)}: {', '.join(parts)}")
    return "Anomalías de secuencia UDP: " + "; ".join(details) + "."


def render_metrics_rows(
    snapshot: MetricsSnapshot,
    race: RaceStateSnapshot | None = None,
    now: float | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Devuelve etiqueta, valor y marcador de calidad de las métricas clave."""
    rows: list[tuple[str, str, str]] = []
    stale_live = _live_metrics_are_stale(race, now)
    for key, label in _METRIC_ROWS:
        metric = snapshot.get(key)
        quality = _render_metric_quality(metric)
        if stale_live and key in _LIVE_METRIC_KEYS and metric is not None:
            quality = _QUALITY_LABELS[Quality.STALE]
        rows.append((label, _render_metric_value(metric), quality))
    for key in sorted(snapshot):
        if not key.endswith(".error"):
            continue
        metric = snapshot[key]
        module = key.removesuffix(".error")
        detail = next(
            (note for note in reversed(metric.notes) if note != "metric_module_failed"),
            "error",
        )
        rows.append((f"Módulo {module}", "—", f"error de cálculo ({detail})"))
    return tuple(rows)


def _strategy_reference_stop(
    evaluation: StrategyEvaluation,
) -> StrategyAlternative | None:
    """Mejor parada calculada cuando la acción no recomienda ninguna."""
    return min(
        (item for item in evaluation.alternatives if item.key != BASELINE_STAY_OUT),
        key=lambda item: (item.remaining_time_s, item.key),
        default=None,
    )


def _strategy_reference_text(alternative: StrategyAlternative) -> str:
    """Describe una parada calculada que no es la recomendación."""
    parts = alternative.key.split(":")
    compound = parts[1] if len(parts) > 1 else alternative.key
    detail = f"{compound}, {alternative.gain_vs_baseline_s:+.2f} s"
    return f"{detail} (alternativa, no recomendada)"


def _format_laps(value: float) -> str:
    """Muestra una fracción de vuelta positiva sin redondearla a cero."""
    for precision in (1, 3, 6):
        text = f"{value:.{precision}f}"
        if value == 0.0 or float(text) != 0.0:
            return text
    return f"{value:.6g}"


def render_strategy_rows(
    evaluation: StrategyEvaluation,
    *,
    session_ended: bool = False,
) -> tuple[tuple[str, str], ...]:
    """Devuelve las etiquetas y explicaciones del panel de estrategia.

    Los campos de parada solo se rellenan cuando la acción recomienda una y la
    sesión sigue en curso. Para ``NO_CHANGE``, ``INSUFFICIENT_DATA`` o una
    sesión finalizada se marcan como no aplicables y se muestra aparte la mejor
    parada calculada, identificada como alternativa sin recomendación; así el
    panel distingue «no disponible» de «no aplica». Una evaluación conservada
    tras el final se etiqueta como histórica y no como acción vigente.
    """
    recommendation = evaluation.recommendation
    action = recommendation.action
    stop_action = action in {StrategyAction.PIT_NOW, StrategyAction.PIT_IN}
    current_stop = stop_action and not session_ended
    if session_ended:
        explanation = (
            "Sesión finalizada: la evaluación se conserva como referencia histórica, "
            "sin acción vigente."
        )
    elif action is StrategyAction.NO_CHANGE:
        explanation = "Ninguna parada superó el mínimo de ganancia considerando la incertidumbre."
    elif action is StrategyAction.INSUFFICIENT_DATA:
        explanation = (
            "No se puede recomendar con los datos críticos disponibles."
            if evaluation.alternatives
            else "No se pudieron evaluar alternativas."
        )
    elif action is StrategyAction.PIT_NOW:
        explanation = "La parada recomendada es ahora."
    else:
        explanation = "La parada recomendada es en la vuelta indicada."

    interval = recommendation.gain_interval_s
    alternative = next(
        (item for item in evaluation.alternatives if item.key == recommendation.key),
        None,
    )
    reference = None if current_stop else _strategy_reference_stop(evaluation)
    if current_stop:
        if alternative is not None and alternative.rejoin_position is not None:
            rejoin = _render_rejoin_position(
                alternative.rejoin_position, "recomendación", evaluation
            )
        else:
            rejoin = _STRATEGY_UNKNOWN
    elif reference is None:
        rejoin = _STRATEGY_NOT_APPLICABLE
    elif reference.rejoin_position is None:
        rejoin = f"{_STRATEGY_UNKNOWN} (alternativa, no recomendación)"
    else:
        rejoin = _render_rejoin_position(
            reference.rejoin_position, "alternativa, no recomendación", evaluation
        )

    recommendation_text = strategy_action_label(action)
    if session_ended:
        recommendation_text = f"{recommendation_text} (histórica, sesión finalizada)"
    rows: list[tuple[str, str]] = [
        ("Recomendación", recommendation_text),
        ("Explicación", explanation),
        (
            "Compuesto",
            (recommendation.compound or _STRATEGY_UNKNOWN)
            if current_stop
            else _STRATEGY_NOT_APPLICABLE,
        ),
        (
            "Juego",
            (
                _STRATEGY_UNKNOWN
                if recommendation.set_index is None
                else str(recommendation.set_index)
            )
            if current_stop
            else _STRATEGY_NOT_APPLICABLE,
        ),
        (
            "Vuelta de parada",
            (
                "Ahora"
                if action is StrategyAction.PIT_NOW
                else f"Vuelta {recommendation.pit_lap}"
                if recommendation.pit_lap is not None
                else _STRATEGY_UNKNOWN
            )
            if current_stop
            else _STRATEGY_NOT_APPLICABLE,
        ),
        (
            "Ganancia vs. línea base",
            (
                _STRATEGY_UNKNOWN
                if recommendation.gain_vs_baseline_s is None
                else f"{recommendation.gain_vs_baseline_s:.2f} s"
            )
            if current_stop
            else _STRATEGY_NOT_APPLICABLE,
        ),
        (
            "Intervalo de ganancia",
            (_STRATEGY_UNKNOWN if interval is None else f"{interval[0]:.2f} a {interval[1]:.2f} s")
            if current_stop
            else _STRATEGY_NOT_APPLICABLE,
        ),
        (
            "Confianza",
            _STRATEGY_UNKNOWN
            if recommendation.confidence is None
            else f"{recommendation.confidence:.0%}",
        ),
        ("Reincorporación", rejoin),
        ("Pérdida en boxes", _render_pit_loss_estimate(evaluation)),
    ]
    if reference is not None:
        rows.append(("Mejor parada calculada", _strategy_reference_text(reference)))
    elif not current_stop and not evaluation.alternatives:
        rows.append(("Mejor parada calculada", "Ninguna calculada"))
    rows.extend(
        (
            ("Calidad de datos", _QUALITY_LABELS[evaluation.data_quality]),
            ("Motivo", _render_strategy_keys(recommendation.notes)),
            (
                "Vueltas restantes",
                _STRATEGY_UNKNOWN
                if evaluation.laps_remaining is None
                else _format_laps(evaluation.laps_remaining),
            ),
            ("Supuestos", _render_strategy_keys(evaluation.assumptions)),
            ("Riesgos", _render_strategy_keys(evaluation.risks)),
            ("Sensibilidad", _render_strategy_keys(evaluation.sensitive_to)),
        )
    )
    return tuple(rows)


def _render_rejoin_position(
    position: int,
    role: str,
    evaluation: StrategyEvaluation,
) -> str:
    if evaluation.rejoin_position_range is None and evaluation.rejoin_quality is None:
        return f"Posición {position} ({role})"
    details = ["estimada"]
    if evaluation.rejoin_position_range is not None:
        low, high = evaluation.rejoin_position_range
        details.append(f"rango {low} a {high}")
    if evaluation.rejoin_quality is not None:
        details.append(f"calidad {_QUALITY_LABELS[evaluation.rejoin_quality]}")
    details.append(role)
    return f"Posición {position} ({'; '.join(details)})"


def _render_pit_loss_estimate(evaluation: StrategyEvaluation) -> str:
    source = evaluation.pit_loss_source
    if source is None:
        return _STRATEGY_UNKNOWN
    if evaluation.pit_loss_s is None:
        value = "No disponible" if source == "none" else _STRATEGY_UNKNOWN
    else:
        value = f"{evaluation.pit_loss_s:.2f} s"
        if evaluation.pit_loss_band_s is not None:
            value += f" (±{evaluation.pit_loss_band_s:.2f} s)"
    details = [value, _PIT_LOSS_SOURCE_LABELS.get(source, source.replace("_", " "))]
    if evaluation.pit_loss_quality is not None:
        details.append(f"calidad {_QUALITY_LABELS[evaluation.pit_loss_quality]}")
    notes = _render_strategy_keys(evaluation.pit_loss_notes)
    if notes != "—":
        details.append(notes)
    return "; ".join(details)


def render_strategy_recording_warning() -> str:
    return (
        "La estrategia está activa, pero el registro está desactivado; "
        "las evaluaciones no se guardarán."
    )


def render_event_message(message_key: str, facts: tuple[Fact, ...]) -> str:
    """Renderiza el catálogo del núcleo, conservando los datos ausentes como desconocidos."""
    return render_template(message_key, facts)


def render_agent_status(status: str, host: str | None = None) -> str:
    label = _AGENT_STATUS_LABELS.get(status, status)
    if status == "unavailable" and host:
        return f"{label} ({host})"
    return label


def render_agent_source(source: str) -> str:
    return _AGENT_SOURCE_LABELS.get(source, source)


def render_agent_priority(priority: str) -> str:
    return _AGENT_PRIORITY_LABELS.get(priority, priority)


def render_pilot_kind(kind: str) -> str:
    return _PILOT_KIND_LABELS.get(kind, AGENT_UNKNOWN_VALUE)


def render_pilot_answer(answer: PilotAnswer) -> str:
    if answer.text:
        return answer.text
    return _PILOT_MESSAGE_TEXTS.get(answer.message_key, answer.message_key)


def render_pilot_clarification(clarification: Clarification) -> str:
    options = ", ".join(option.name for option in clarification.options)
    if not options:
        return PILOT_CLARIFICATION_LABEL
    return f"{PILOT_CLARIFICATION_LABEL}: {options}"


def _render_strategy_keys(keys: tuple[str, ...]) -> str:
    if not keys:
        return "—"
    return "; ".join(_STRATEGY_KEY_LABELS.get(key, key.replace("_", " ")) for key in keys)


def _live_metrics_are_stale(race: RaceStateSnapshot | None, now: float | None) -> bool:
    if race is None or now is None:
        return False
    try:
        return race.freshness_for("lap_data").is_stale(now)
    except KeyError:
        return False


def _render_metric_value(metric: Metric | None) -> str:
    if metric is None:
        return "—"
    if metric.value is None:
        required = next(
            (
                int(note.removeprefix("required_samples:"))
                for note in metric.notes
                if note.startswith("required_samples:")
                and note.removeprefix("required_samples:").isdigit()
            ),
            None,
        )
        if metric.quality is Quality.INSUFFICIENT and required is not None:
            return f"— ({metric.sample_count}/{required} muestras válidas)"
        return "—"
    value = metric.value
    if isinstance(value, bool):
        text = "sí" if value else "no"
    elif isinstance(value, float):
        if metric.unit == "lap":
            text = _format_laps(value)
        else:
            precision = 3 if metric.unit == "%/lap" else 2
            text = f"{value:.{precision}f}"
    else:
        text = str(value)
    unit = _UNIT_LABELS.get(metric.unit, metric.unit)
    return f"{text} {unit}".strip()


def _render_metric_quality(metric: Metric | None) -> str:
    if metric is None:
        return "—"
    return _QUALITY_LABELS.get(metric.quality, metric.quality.value)


def render_race_event(event: RaceEvent, cars: tuple[CarSnapshot, ...]) -> str:
    if event.kind == "session_changed":
        return "Cambio de sesión"
    if event.kind == "session_started":
        return "Sesión iniciada"
    if event.kind == "session_ended":
        return "Sesión finalizada"
    if event.kind == "lap_changed":
        return f"Vuelta {event.lap_number}"
    if event.kind in {"pit_entry", "pit_exit", "pit_stop_completed", "retirement"}:
        car = (
            cars[event.car_index]
            if event.car_index is not None and 0 <= event.car_index < len(cars)
            else None
        )
        name = (
            car.name
            if car is not None and car.name
            else (f"Coche {event.car_index + 1}" if event.car_index is not None else "Coche")
        )
        actions = {
            "pit_entry": "entra en boxes",
            "pit_exit": "sale de boxes",
            "pit_stop_completed": "completa parada",
            "retirement": f"retirada comunicada (motivo del juego: {event.value or 'desconocido'})",
        }
        action = actions[event.kind]
        return f"{name}: {action}"
    return event.kind


def render_diagnostic(item: DiagnosticMessage) -> str:
    key = item.key
    if key == "strategy_recording_disabled":
        return render_strategy_recording_warning()
    if key == "rejected_sources":
        ips = item.get("ips")
        if not isinstance(ips, tuple) or not all(isinstance(ip, str) for ip in ips):
            raise TypeError("rejected_sources requires a tuple of source addresses.")
        rendered_ips = ", ".join("otros" if ip == "other" else ip for ip in ips)
        return (
            f"Llegan paquetes de orígenes no permitidos ({rendered_ips}). "
            "Revisa 'allowed_sources' en la configuración."
        )
    if key == "no_packets_heading":
        return "No ha llegado ningún paquete desde el arranque. Causas probables:"
    if key == "telemetry_disabled":
        return "  - La telemetría UDP está desactivada en el juego."
    if key == "destination_mismatch":
        return (
            "  - La IP o el puerto de destino configurados en el juego no coinciden con "
            f"{item.get('address')}."
        )
    if key == "firewall":
        return "  - Un firewall bloquea el puerto UDP de entrada."
    if key == "loopback_only":
        return (
            "  - La aplicación escucha solo en loopback: si el juego corre en otro PC, "
            'usa listen_address = "0.0.0.0".'
        )
    if key == "stalled":
        return (
            f"Sin datos desde hace {_number(item, 'age_seconds'):.0f} s. ¿El juego está en pausa, "
            "en menús o con la telemetría desactivada?"
        )
    if key == "queue_dropped":
        return (
            f"Se descartaron {item.get('count')} paquetes por cola llena "
            "(el consumidor no da abasto)."
        )
    if key == "consumer_error":
        error = item.get("error")
        if not isinstance(error, DiagnosticMessage):
            raise TypeError("consumer_error requires a DiagnosticMessage.")
        return (
            f"El consumidor registró {item.get('count')} errores; último error: "
            f"{render_diagnostic(error)}"
        )
    if key == "recorder_dropped":
        return (
            "Se perdieron lotes de la sesión por falta de espacio en la cola "
            f"({item.get('count')} lotes)."
        )
    if key in {
        "consumer_header_error",
        "consumer_parse_error",
        "race_state_update_error",
        "metrics_update_error",
        "strategy_engine_error",
        "recorder_error",
        "capture_write_error",
        "capture_flush_error",
        "capture_close_error",
        "weekend_profile_error",
    }:
        labels = {
            "consumer_header_error": "Error al registrar una cabecera UDP",
            "consumer_parse_error": "Error inesperado en el parser UDP",
            "race_state_update_error": "Error inesperado al actualizar el estado de carrera",
            "metrics_update_error": "Error inesperado al actualizar las métricas",
            "strategy_engine_error": "Error inesperado al actualizar la estrategia",
            "recorder_error": "Error inesperado al grabar la sesión",
            "capture_write_error": "Error al escribir la captura",
            "capture_flush_error": "Error al vaciar la captura",
            "capture_close_error": "Error al cerrar la captura",
            "weekend_profile_error": "Error al preparar el perfil del fin de semana",
        }
        return f"{labels[key]}: {item.get('error')}"
    if key == "parser_rejection":
        issue = item.get("issue")
        if not isinstance(issue, ParseIssue):
            raise TypeError("parser_rejection requires a ParseIssue.")
        return f"Parser: {_render_parse_issue(issue)} ({item.get('count')} paquetes rechazados)"
    if key == "unverified_game_version":
        return (
            f"Juego {item.get('version')}: se parsea por coincidencia de formato y tamaño; "
            f"esta versión menor no se ha verificado ({item.get('count')} paquetes)."
        )
    if key == "sequence_anomalies":
        items = item.get("items")
        if not isinstance(items, tuple):
            raise TypeError("sequence_anomalies requires a tuple of packet statistics.")
        normalized_items: list[tuple[int, PacketSequenceStats]] = []
        for item_value in items:
            if (
                not isinstance(item_value, tuple)
                or len(item_value) != 2
                or not isinstance(item_value[0], int)
                or not isinstance(item_value[1], PacketSequenceStats)
            ):
                raise TypeError("sequence_anomalies contains an invalid packet statistic.")
            normalized_items.append((item_value[0], item_value[1]))
        return _render_sequence_anomalies(tuple(normalized_items))
    if key == "sequence_gap_caveat":
        return (
            "Los huecos se estiman con overall_frame_identifier y la cadencia "
            "observada por tipo; no confirman datagramas perdidos."
        )
    return f"Falta la traducción al español del mensaje '{key}'."
