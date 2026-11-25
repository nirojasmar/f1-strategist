"""Compositor determinista de respuestas del piloto (Q10).

La respuesta se compone solo con datos de un ``ToolResult``: cada cifra sale de
un ``ToolFact`` y cada hecho lleva el vocabulario de su tipo (medido, estimado o
recomendado). Los textos son datos por idioma; una clave o un hecho ausente
produce un texto seguro, nunca una excepción.

Contrato con las herramientas (Q5 a Q9): los ``notes`` del resultado pueden
contener una clave de plantilla (por ejemplo ``pilot.leader``) que sustituye a
la respuesta normal; los ``missing`` y los hechos con valor ``None`` se nombran
en el texto como datos ausentes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..agent.templates import fact_unit, format_fact
from ..config import PilotConfig
from ..metrics.types import Quality
from .types import (
    AnswerKind,
    DriverCandidate,
    FactKind,
    Intent,
    PilotAnswer,
    RoutedQuery,
    ToolFact,
    ToolResult,
)

_DEFAULT_LANGUAGE = "es"
_FALLBACK_TEXT = "No tengo una respuesta preparada para eso."


@dataclass(frozen=True, slots=True)
class _Catalog:
    """Textos de un idioma: plantillas, etiquetas de hechos y palabras de tipo."""

    templates: Mapping[str, str]
    fact_labels: Mapping[str, str]
    kind_words: Mapping[FactKind, str]


def _build_templates(intents: Mapping[Intent, tuple[str, str]]) -> dict[str, str]:
    templates: dict[str, str] = {}
    for intent, (lead, topic) in intents.items():
        prefix = f"pilot.answer.{intent.value}"
        templates[prefix] = f"{lead}: {{facts}}."
        templates[f"{prefix}.partial"] = f"{lead}: {{facts}}. No tengo {{missing}}."
        templates[f"{prefix}.no_data"] = f"No tengo datos de {topic} ahora mismo."
    return templates


_ES_INTENTS: Mapping[Intent, tuple[str, str]] = MappingProxyType(
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

_ES_FIXED: Mapping[str, str] = MappingProxyType(
    {
        "pilot.unknown": (
            "No he entendido la pregunta. Puedes preguntarme por combustible, "
            "neumáticos, rivales o estrategia."
        ),
        "pilot.clarification": "¿A cuál te refieres: {options}?",
        "pilot.clarification_no_options": "¿A cuál te refieres?",
        "pilot.leader": "Vas primero: no hay nadie delante.",
        "pilot.last": "Vas último: no hay nadie detrás.",
        "pilot.no_car_ahead": "No hay nadie delante de ti.",
        "pilot.no_car_behind": "No hay nadie detrás de ti.",
        "pilot.driver_retired": ("El piloto que pides está retirado y no tengo datos suyos."),
        "pilot.driver_unknown": (
            "No sé a qué piloto te refieres; no tengo datos de esa referencia."
        ),
        "pilot.stale": " Los datos están desactualizados.",
        "pilot.missing": " No tengo {missing}.",
        "pilot.generic": "Datos: {facts}.",
        "pilot.no_data": "No tengo datos ahora mismo.",
        "pilot.missing_template": _FALLBACK_TEXT,
    }
)

_ES_FACT_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "action": "acción",
        "age_laps": "edad",
        "air_temperature_c": "temperatura del aire",
        "best_k": "mejor k",
        "car_index": "coche",
        "compound": "compuesto",
        "component": "componente",
        "confidence": "confianza",
        "consumption_per_lap": "consumo por vuelta",
        "damage_pct": "daño",
        "fuel_kg": "combustible restante",
        "gain_s": "ganancia",
        "gap_ahead_s": "intervalo con el de delante",
        "gap_behind_s": "intervalo con el de atrás",
        "gap_s": "intervalo",
        "horizon_min": "horizonte",
        "lap": "vuelta",
        "laps_left": "vida estimada",
        "laps_remaining": "vueltas restantes",
        "margin_kg": "margen",
        "margin_laps": "margen",
        "name": "piloto",
        "pace_delta_s_per_lap": "diferencia de ritmo",
        "pace_trend_s_per_lap": "tendencia de ritmo",
        "pit_lap": "vuelta de parada",
        "position": "posición",
        "probability_pct": "probabilidad",
        "rejoin_position": "posición de reincorporación",
        "track_temperature_c": "temperatura de pista",
        "undercut_gain_s": "ganancia del undercut",
        "wear_mean_pct": "desgaste",
    }
)

_CATALOGS: Mapping[str, _Catalog] = MappingProxyType(
    {
        "es": _Catalog(
            templates=MappingProxyType({**_build_templates(_ES_INTENTS), **_ES_FIXED}),
            fact_labels=_ES_FACT_LABELS,
            kind_words=MappingProxyType(
                {
                    FactKind.MEASURED: "medido",
                    FactKind.ESTIMATED: "estimado",
                    FactKind.RECOMMENDED: "recomendado",
                }
            ),
        )
    }
)


def compose_answer(
    routed: RoutedQuery,
    result: ToolResult | None,
    *,
    language: str,
    cfg: PilotConfig,
) -> PilotAnswer:
    """Compone la respuesta determinista de una pregunta ya enrutada."""
    catalog = _catalog(language)
    clarification = routed.clarification
    if clarification is not None:
        key = clarification.message_key or "pilot.clarification"
        if key not in catalog.templates:
            key = "pilot.clarification"
        options = ", ".join(_candidate_text(item) for item in clarification.options)
        if options:
            text = _render(catalog, key, options=options)
        else:
            text = _render(catalog, "pilot.clarification_no_options")
        return _answer(routed, AnswerKind.CLARIFICATION, text, key, result, cfg=cfg)

    if routed.intent is Intent.UNKNOWN:
        key = "pilot.unknown"
        return _answer(routed, AnswerKind.UNKNOWN, _render(catalog, key), key, None, cfg=cfg)

    if result is not None:
        note_key = next((note for note in result.notes if note in catalog.templates), None)
        if note_key is not None:
            return _answer(
                routed,
                AnswerKind.ANSWER,
                _render(catalog, note_key),
                note_key,
                result,
                cfg=cfg,
            )

    facts = () if result is None else tuple(fact for fact in result.facts if fact.value is not None)
    missing = _missing_names(result)
    no_data = (
        result is None
        or not facts
        or result.quality
        in {
            Quality.INSUFFICIENT,
            Quality.UNAVAILABLE,
        }
    )
    if no_data:
        key = f"pilot.answer.{routed.intent.value}.no_data"
        text = _render_or(catalog, key, "pilot.no_data")
        if missing:
            text += _render(catalog, "pilot.missing", missing=_labels_text(missing, catalog))
        return _answer(
            routed,
            AnswerKind.UNAVAILABLE,
            _with_stale(text, result, catalog),
            key,
            result,
            cfg=cfg,
        )

    suffix = ".partial" if missing else ""
    key = f"pilot.answer.{routed.intent.value}{suffix}"
    text = _render_or(
        catalog,
        key,
        "pilot.no_data",
        facts=_facts_text(facts, catalog, language),
        missing=_labels_text(missing, catalog),
    )
    return _answer(
        routed,
        AnswerKind.ANSWER,
        _with_stale(text, result, catalog),
        key,
        result,
        cfg=cfg,
    )


def _catalog(language: str) -> _Catalog:
    catalog = _CATALOGS.get(language.strip().lower())
    return catalog if catalog is not None else _CATALOGS[_DEFAULT_LANGUAGE]


def _answer(
    routed: RoutedQuery,
    kind: AnswerKind,
    text: str,
    message_key: str,
    result: ToolResult | None,
    *,
    cfg: PilotConfig,
) -> PilotAnswer:
    followup_pending = (
        kind is AnswerKind.ANSWER
        and cfg.llm_followup
        and routed.intent.value in cfg.llm_followup_intents
    )
    return PilotAnswer(
        query_id=routed.query.query_id,
        intent=routed.intent,
        kind=kind,
        text=text,
        message_key=message_key,
        tool_result=result,
        followup_pending=followup_pending,
    )


def _render(catalog: _Catalog, key: str, **params: str) -> str:
    template = catalog.templates.get(key)
    if template is None:
        return _FALLBACK_TEXT
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return _FALLBACK_TEXT


def _render_or(catalog: _Catalog, key: str, fallback_key: str, **params: str) -> str:
    if key in catalog.templates:
        return _render(catalog, key, **params)
    return _render(catalog, fallback_key, **params)


def _facts_text(facts: Iterable[ToolFact], catalog: _Catalog, language: str) -> str:
    return ", ".join(_fact_text(fact, catalog, language) for fact in facts)


def _fact_text(fact: ToolFact, catalog: _Catalog, language: str) -> str:
    value = format_fact(fact.name, fact.value, language)
    if fact.unit and fact.value is not None and fact_unit(fact.name) is None:
        value = f"{value} {fact.unit}"
    label = catalog.fact_labels.get(fact.name, fact.name)
    return f"{label} {value} ({catalog.kind_words[fact.kind]})"


def _missing_names(result: ToolResult | None) -> tuple[str, ...]:
    if result is None:
        return ()
    names = (*result.missing, *(fact.name for fact in result.facts if fact.value is None))
    return tuple(dict.fromkeys(names))


def _labels_text(names: Iterable[str], catalog: _Catalog) -> str:
    return ", ".join(catalog.fact_labels.get(name, name) for name in names)


def _with_stale(text: str, result: ToolResult | None, catalog: _Catalog) -> str:
    if result is not None and (result.stale or result.quality is Quality.STALE):
        return text + _render(catalog, "pilot.stale")
    return text


def _candidate_text(candidate: DriverCandidate) -> str:
    if candidate.position is None:
        return candidate.name
    return f"{candidate.name} (P{candidate.position})"


__all__ = ["compose_answer"]
