"""Validador de respuestas del modelo (Fase 6).

Comprueba que el texto del modelo solo use cifras presentes en el contexto,
respete la prioridad de la alerta y las restricciones de longitud y formato.
Función pura: el instante, la red y el cliente quedan fuera; solo decide.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from ..config import AgentConfig, AppConfig
from ..events.types import AgentAlert, EventPriority
from .templates import fact_unit
from .types import ContextPayload, LLMReply, ValidationResult


class ValidationIssue(StrEnum):
    """Códigos estables de los motivos por los que una respuesta no vale."""

    INVALID_JSON = "invalid_json"
    SCHEMA_MISMATCH = "schema_mismatch"
    LANGUAGE_MISMATCH = "language_mismatch"
    PRIORITY_MISMATCH = "priority_mismatch"
    UNKNOWN_FACT = "unknown_fact"
    UNKNOWN_NUMBER = "unknown_number"
    UNIT_MISMATCH = "unit_mismatch"
    TOO_LONG = "too_long"
    EMPTY_MESSAGE = "empty_message"
    FORMAT_INVALID = "format_invalid"
    IMPERATIVE_PHRASE = "imperative_phrase"
    REPEATED_MESSAGE = "repeated_message"


_NUMBER_RE = re.compile(r"(?<![.,])[-+−]?\d+(?:[.,]\d+)?")
_WORD_NUMBERS = {
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "veinte": 20,
    "veintiuno": 21,
    "veintidós": 22,
    "veintitrés": 23,
    "veinticuatro": 24,
    "veinticinco": 25,
    "veintiséis": 26,
    "veintisiete": 27,
    "veintiocho": 28,
    "veintinueve": 29,
    "treinta": 30,
}
_WORD_NUMBER_RE = re.compile(
    r"\b(" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Unidades reconocidas en el texto, de la más larga a la más corta.
_UNIT_ALIASES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            ("%/vuelta", "%/lap"),
            ("%/lap", "%/lap"),
            ("s/vuelta", "s/lap"),
            ("s/lap", "s/lap"),
            ("segundos", "s"),
            ("segundo", "s"),
            ("seg", "s"),
            ("kilos", "kg"),
            ("kilo", "kg"),
            ("kilogramos", "kg"),
            ("kilogramo", "kg"),
            ("kg", "kg"),
            ("vueltas", "lap"),
            ("vuelta", "lap"),
            ("laps", "lap"),
            ("lap", "lap"),
            ("grados", "c"),
            ("°c", "c"),
            ("ºc", "c"),
            ("minutos", "min"),
            ("minuto", "min"),
            ("min", "min"),
            ("por ciento", "%"),
            ("porciento", "%"),
            ("s", "s"),
            ("%", "%"),
            ("m", "m"),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)
_UNIT_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(alias) for alias, _ in _UNIT_ALIASES) + r")(?![\w])",
    re.IGNORECASE,
)
_UNITS_BY_ALIAS = dict(_UNIT_ALIASES)

_MARKDOWN_RE = re.compile(
    r"(\*\*|__|`|^\s{0,3}[#>\-*+]\s|\[[^\]]*\]\(|https?://|www\.)",
    re.MULTILINE,
)
_EMOJI_RE = re.compile(r"[\U0001f300-\U0001faff\u2600-\u27bf\ufe0f]")

_LAP_FACTS = frozenset(
    {
        "lap",
        "position",
        "age_laps",
        "laps_left",
        "laps_remaining",
        "margin_laps",
        "ideal_lap",
        "latest_lap",
        "pit_lap",
        "laps_to_ideal",
    }
)


class ResponseValidator:
    """Valida el texto crudo del modelo contra el contexto y la alerta."""

    def __init__(self, config: AppConfig | AgentConfig | None = None) -> None:
        if isinstance(config, AppConfig):
            config = config.agent
        self._config = config if config is not None else AgentConfig()

    def validate(
        self,
        text: str,
        context: ContextPayload,
        alert: AgentAlert | None = None,
    ) -> ValidationResult:
        """Devuelve el resultado con todos los códigos de error aplicables."""
        errors: list[str] = []
        reply = self._parse(text, errors)
        if reply is None:
            return ValidationResult(valid=False, errors=tuple(errors))

        priority = alert.priority if alert is not None else _context_priority(context)
        if reply.language.strip().casefold() != context.language.strip().casefold():
            errors.append(ValidationIssue.LANGUAGE_MISMATCH)
        if priority is not None and reply.priority != priority:
            errors.append(ValidationIssue.PRIORITY_MISMATCH)
        if priority == EventPriority.CRITICAL and not reply.should_speak:
            # En una alerta crítica el rechazo no se respeta: se habla igualmente.
            reply = reply.model_copy(update={"should_speak": True})

        fact_names = _fact_names(context, alert)
        if any(name not in fact_names for name in reply.used_facts):
            errors.append(ValidationIssue.UNKNOWN_FACT)

        if not reply.should_speak and priority != EventPriority.CRITICAL:
            return ValidationResult(valid=not errors, errors=tuple(errors), reply=reply)

        message = reply.message
        if not message.strip():
            errors.append(ValidationIssue.EMPTY_MESSAGE)
            return ValidationResult(valid=False, errors=tuple(errors), reply=reply)

        if len(message.split()) > self._config.max_words or len(message) > self._config.max_chars:
            errors.append(ValidationIssue.TOO_LONG)
        if "\n" in message or "\r" in message or _MARKDOWN_RE.search(message):
            errors.append(ValidationIssue.FORMAT_INVALID)
        if _EMOJI_RE.search(message):
            errors.append(ValidationIssue.FORMAT_INVALID)

        for code in _number_errors(message, _numbers(context, alert)):
            errors.append(code)

        if _has_high_uncertainty(context.uncertainties):
            folded = _collapse(message)
            if any(_collapse(phrase) in folded for phrase in self._config.imperative_phrases):
                errors.append(ValidationIssue.IMPERATIVE_PHRASE)

        normalized = _normalize(message)
        if any(_normalize(recent) == normalized for recent in context.recent_messages):
            errors.append(ValidationIssue.REPEATED_MESSAGE)

        unique = tuple(dict.fromkeys(errors))
        return ValidationResult(valid=not unique, errors=unique, reply=reply)

    def _parse(self, text: str, errors: list[str]) -> LLMReply | None:
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            errors.append(ValidationIssue.INVALID_JSON)
            return None
        if not isinstance(data, dict):
            errors.append(ValidationIssue.SCHEMA_MISMATCH)
            return None
        try:
            return LLMReply.model_validate(data)
        except ValidationError:
            errors.append(ValidationIssue.SCHEMA_MISMATCH)
            return None


def _context_priority(context: ContextPayload) -> EventPriority | None:
    alert = context.alert
    if not isinstance(alert, Mapping):
        return None
    try:
        return EventPriority(str(alert.get("priority")))
    except ValueError:
        return None


def _fact_names(context: ContextPayload, alert: AgentAlert | None) -> set[str]:
    """Nombres de los hechos entregados al modelo, de la alerta y del contexto."""
    names: set[str] = set()
    if alert is not None:
        names.update(name for name, _ in alert.facts)
    for block in (context.alert, context.situation, context.strategy, context.metrics):
        if isinstance(block, Mapping):
            _collect_names(block, names)
    return names


def _collect_names(block: Mapping[Any, Any], names: set[str], depth: int = 0) -> None:
    if depth > 3:
        return
    for raw_name, raw_value in block.items():
        name = str(raw_name)
        if name == "facts":
            if isinstance(raw_value, Mapping):
                names.update(str(item) for item in raw_value)
            elif isinstance(raw_value, (list, tuple)):
                for item in raw_value:
                    if isinstance(item, Mapping) and item.get("name") is not None:
                        names.add(str(item["name"]))
                    elif isinstance(item, (list, tuple)) and len(item) == 2:
                        names.add(str(item[0]))
            continue
        names.add(name)
        if isinstance(raw_value, Mapping) and "value" not in raw_value:
            _collect_names(raw_value, names, depth + 1)


_Observation = tuple[str, float, str | None]


def _numbers(context: ContextPayload, alert: AgentAlert | None) -> list[_Observation]:
    """Observaciones numéricas usadas para comprobar las cifras.

    Se conservan las de un mismo nombre con valores distintos; solo se deduplica
    la observación idéntica para no repetir comprobaciones.
    """
    observations: list[_Observation] = []
    seen: set[_Observation] = set()
    if alert is not None:
        for name, value in alert.facts:
            number = _as_number(value)
            if number is not None:
                # Los hechos crudos son 0-based; el contexto y las plantillas ya muestran 1-based.
                _store_number(
                    observations,
                    seen,
                    name,
                    number + 1 if name == "car_index" else number,
                )
    for block in (context.alert, context.situation, context.strategy, context.metrics):
        if isinstance(block, Mapping):
            _collect_numbers(block, observations, seen)
    return observations


def _collect_numbers(
    block: Mapping[Any, Any],
    observations: list[_Observation],
    seen: set[_Observation],
    depth: int = 0,
) -> None:
    if depth > 3:
        return
    for raw_name, raw_value in block.items():
        name = str(raw_name)
        if name == "facts":
            if isinstance(raw_value, Mapping):
                _collect_numbers(raw_value, observations, seen, depth + 1)
            elif isinstance(raw_value, (list, tuple)):
                for item in raw_value:
                    if isinstance(item, Mapping):
                        number = _as_number(item.get("value"))
                        if number is not None and item.get("name") is not None:
                            unit = item.get("unit") if isinstance(item.get("unit"), str) else None
                            _store_number(
                                observations,
                                seen,
                                str(item["name"]),
                                number,
                                _canonical_unit(unit),
                            )
                    elif isinstance(item, (list, tuple)) and len(item) == 2:
                        number = _as_number(item[1])
                        if number is not None:
                            _store_number(observations, seen, str(item[0]), number)
            continue
        if isinstance(raw_value, Mapping):
            # Las métricas llegan como ``{clave: {"value", "unit", "quality"}}``.
            if "value" in raw_value:
                number = _as_number(raw_value.get("value"))
                if number is not None:
                    unit = raw_value.get("unit") if isinstance(raw_value.get("unit"), str) else None
                    _store_number(observations, seen, name, number, _canonical_unit(unit))
            else:
                _collect_numbers(raw_value, observations, seen, depth + 1)
            continue
        number = _as_number(raw_value)
        if number is not None:
            _store_number(observations, seen, name, number)


def _store_number(
    observations: list[_Observation],
    seen: set[_Observation],
    name: str,
    number: float,
    unit: str | None = None,
) -> None:
    # ``format_fact`` y el contexto muestran car_index 1-based.
    item = (name, number, unit)
    if item not in seen:
        seen.add(item)
        observations.append(item)


def _as_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _number_errors(message: str, numbers: list[_Observation]) -> list[str]:
    """Cada cifra del texto debe corresponder a un hecho, con su unidad."""
    errors: list[str] = []
    for token, decimals, end in _numeric_tokens(message):
        unit = _unit_after(message, end)
        matches = [
            (name, supplied)
            for name, value, supplied in numbers
            if _rounds_to(token, value, decimals)
        ]
        if not matches:
            errors.append(ValidationIssue.UNKNOWN_NUMBER)
            continue
        if unit is not None and not any(
            (supplied or _fact_unit(name)) == unit for name, supplied in matches
        ):
            errors.append(ValidationIssue.UNIT_MISMATCH)
    return errors


def _numeric_tokens(message: str) -> list[tuple[Decimal, int, int]]:
    """Cifras del texto con sus decimales y la posición donde terminan."""
    tokens: list[tuple[Decimal, int, int]] = []
    for match in _NUMBER_RE.finditer(message):
        raw = match.group().replace("−", "-").replace(",", ".")
        try:
            tokens.append((Decimal(raw), _decimals(raw), match.end()))
        except InvalidOperation:
            continue
    for match in _WORD_NUMBER_RE.finditer(message):
        tokens.append((Decimal(_WORD_NUMBERS[match.group().casefold()]), 0, match.end()))
    tokens.sort(key=lambda item: item[2])
    return tokens


def _decimals(raw: str) -> int:
    number = raw.lstrip("+-")
    return len(number.split(".", 1)[1]) if "." in number else 0


def _rounds_to(token: Decimal, value: float, decimals: int) -> bool:
    """Mismo redondeo que ``format_fact``: formato fijo y ceros finales fuera."""
    try:
        text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
        return token == Decimal(text)
    except InvalidOperation:
        return False


def _unit_after(message: str, end: int) -> str | None:
    match = _UNIT_RE.match(message[end : end + 24])
    if match is None:
        return None
    return _UNITS_BY_ALIAS.get(match.group(1).casefold())


def _canonical_unit(unit: str | None) -> str | None:
    """Traduce las unidades del contexto a las que reconoce el texto."""
    if unit is None:
        return None
    codes = {
        "laps": "lap",
        "pct": "%",
        "pct/lap": "%/lap",
        "celsius": "c",
        "kg/lap": "kg",
    }
    return codes.get(unit.casefold(), unit.casefold())


def _fact_unit(name: str) -> str | None:
    """Unidad esperada: primero la tabla de ``format_fact`` y luego el sufijo."""
    if name in _LAP_FACTS:
        return "lap"
    unit = fact_unit(name)
    if unit is not None:
        return unit
    if name.endswith("_pct_per_lap"):
        return "%/lap"
    if name.endswith("_s_per_lap"):
        return "s/lap"
    if name.endswith("_pct"):
        return "%"
    if name.endswith("_kg"):
        return "kg"
    if name.endswith("_laps"):
        return "lap"
    if name.endswith("_s"):
        return "s"
    if name.endswith("_c"):
        return "c"
    if name.endswith("_m"):
        return "m"
    return None


def _has_high_uncertainty(uncertainties: tuple[str, ...]) -> bool:
    for value in uncertainties:
        tokens = set(re.split(r"[^a-z]+", value.casefold()))
        if tokens & {"stale", "insufficient"}:
            return True
    return False


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().strip(".!?¿¡").casefold()


__all__ = ["ResponseValidator", "ValidationIssue"]
