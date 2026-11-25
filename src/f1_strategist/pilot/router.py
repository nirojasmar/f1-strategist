"""Enrutador determinista de preguntas del piloto (Fase 7, Q3).

Normaliza el texto, puntúa las intenciones del catálogo del idioma y devuelve
la mejor por encima de ``min_intent_confidence``. Resuelve las referencias a
pilotos con el directorio de Q4 (aclaración incluida) y no compone respuestas
(Q10) ni lee el reloj: el instante llega como parámetro.

El catálogo vive en ``pilot/intents/<idioma>.toml``: por intención, frases y
palabras clave con peso en (0, 1] y patrones negativos que la descartan.
"""

from __future__ import annotations

import hashlib
import math
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from importlib.resources import files
from typing import Any, Protocol

from ..config import PilotConfig
from .entities import (
    _ORDINALS,
    AMBIGUOUS_DRIVER_MESSAGE_KEY,
    Ambiguous,
    DriverDirectory,
    Resolved,
    normalize_text,
    resolve_driver,
)
from .types import (
    Clarification,
    EntityRef,
    Intent,
    PilotQuery,
    RoutedQuery,
)

SUGGESTED_INTENTS: tuple[Intent, ...] = (
    Intent.FUEL_STATUS,
    Intent.TYRE_LIFE,
    Intent.ASK_STRATEGY,
)

# Las métricas del piloto solo existen para el coche propio.
_PLAYER_ONLY_INTENTS = frozenset({Intent.FUEL_STATUS, Intent.TYRE_LIFE, Intent.DAMAGE_STATUS})
_COMPARABLE_STATE_INTENTS = frozenset({Intent.GENERAL_RACE_STATE, Intent.PACE_DIAGNOSIS})

_FUZZY_MIN_LENGTH = 5
_FUZZY_RATIO = 0.85
_FUZZY_PENALTY = 0.85
_VARIANT_MIN_LENGTH = 4
_TIE_TOLERANCE = 1e-9

# Preguntas de definición o de ficha técnica del coche, no del estado de la sesión.
_OUT_OF_SCOPE = re.compile(
    r"(?<![a-z0-9])(?:que significa|que significan|que quiere decir|que es un|que es una|"
    r"significado de|cuanto pesa|cuanto mide)(?![a-z0-9])"
)

_INTENTS_PACKAGE = "f1_strategist.pilot"

_RELATIVE_REFERENCE = re.compile(r"\b(?:de|del)\s+(delante|detras|atras)\b")
_EXTREME_REFERENCE = re.compile(r"\b(?:el|la|al|del)\s+(lider|ultimo)\b")
_RELATIVE_TO_ME = re.compile(r"(?<![a-z0-9])(delante|detras|atras) de mi(?![a-z0-9])")
_TEAMMATE_REFERENCE = re.compile(r"\b(?:mi|el|al|del|su)\s+companero(?: de equipo)?\b")
_NUMBER_REFERENCE = re.compile(r"\b(?:el|al|del|numero|num)\s+(\d{1,2})\b")
_POSITION_REFERENCE = re.compile(r"\b(?:el|al|del)\s+(p\d{1,2})\b")
_UNKNOWN_REFERENCE = re.compile(
    r"(?<![a-z0-9])(?:respecto a|frente a|contra|comparado con|comparame con|comparativa con|"
    r"a cuanto esta|a cuantos segundos esta|undercut a|undercut al|undercut contra|con)"
    r"\s+([a-z][a-z0-9]{2,})(?![a-z0-9])"
)
_UNKNOWN_REFERENCE_STOPWORDS = frozenset(
    {
        "coche",
        "piloto",
        "rival",
        "undercut",
        "delante",
        "detras",
        "atras",
        "lider",
        "ultimo",
        "este",
        "esa",
        "ese",
        "vuelta",
        "boxes",
        "ritmo",
        "gap",
    }
)
_DRIVER_INTENTS = frozenset({Intent.COMPARE_DRIVER, Intent.GAP_STATUS, Intent.PIT_UNDERCUT})
_ORDINAL_REFERENCE = re.compile(r"\b(?:el|al|del)\s+([a-z]+)\b")


class RouterError(ValueError):
    """Idioma no soportado o catálogo de intenciones inválido."""


class DialogueState(Protocol):
    """Contrato de memoria de diálogo que usa el enrutador (Q4)."""

    def last_intent(self, now: float) -> Intent | None: ...

    def pending_clarification(self, now: float) -> Clarification | None: ...

    def resolve_clarification(self, text: str, now: float) -> object: ...

    def cancel_clarification(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Pattern:
    pattern: re.Pattern[str]
    word: str
    weight: float


@dataclass(frozen=True, slots=True)
class IntentPattern:
    """Frases, palabras clave y negativos ya normalizados de una intención."""

    intent: Intent
    phrases: tuple[_Pattern, ...]
    keywords: tuple[_Pattern, ...]
    negatives: tuple[re.Pattern[str], ...]

    def score(self, text: str, tokens: tuple[str, ...]) -> float:
        if any(pattern.search(text) for pattern in self.negatives):
            return 0.0
        total = sum(item.weight for item in self.phrases if item.pattern.search(text))
        words = frozenset(keyword.word for keyword in self.keywords)
        for keyword in self.keywords:
            if keyword.pattern.search(text):
                total += keyword.weight
            elif " " not in keyword.word:
                if _variant_match(tokens, keyword.word, words):
                    total += keyword.weight
                elif _fuzzy_match(tokens, keyword.word):
                    total += keyword.weight * _FUZZY_PENALTY
        return min(total, 1.0)


@dataclass(frozen=True, slots=True)
class IntentCatalog:
    """Patrones de un idioma, en orden estable de carga."""

    language: str
    intents: tuple[IntentPattern, ...]


def normalize(text: str) -> str:
    """Minúsculas, sin tildes ni signos, espacios colapsados."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", stripped))


def parse_catalog(data: Mapping[str, Any], *, language: str) -> IntentCatalog:
    """Valida un catálogo ya leído; falla con ``RouterError`` si es inválido."""
    unexpected = set(data) - {"intents"}
    if unexpected:
        raise RouterError(f"Claves no admitidas en el catálogo: {sorted(unexpected)}")
    raw = data.get("intents")
    if not isinstance(raw, Mapping) or not raw:
        raise RouterError("El catálogo no tiene intenciones en la sección [intents]")

    patterns: list[IntentPattern] = []
    for name in sorted(raw, key=str):
        if not isinstance(name, str):
            raise RouterError("Los nombres de intención deben ser texto")
        try:
            intent = Intent(name)
        except ValueError as exc:
            raise RouterError(f"Intención desconocida en el catálogo: {name!r}") from exc
        if intent is Intent.UNKNOWN:
            raise RouterError("La intención 'unknown' no se declara en el catálogo")
        table = raw[name]
        if not isinstance(table, Mapping):
            raise RouterError(f"La intención {name!r} debe ser una tabla")
        extra = set(table) - {"phrases", "keywords", "negative"}
        if extra:
            raise RouterError(f"Claves no admitidas en {name!r}: {sorted(extra)}")
        if table.get("phrases") is None:
            raise RouterError(f"La intención {name!r} no tiene frases")
        phrases = _compile(table.get("phrases"), intent=name, kind="frases")
        keywords = _compile(table.get("keywords"), intent=name, kind="palabras clave")
        negatives = _negatives(table.get("negative"), intent=name)
        patterns.append(IntentPattern(intent, phrases, keywords, negatives))
    return IntentCatalog(language, tuple(patterns))


@lru_cache(maxsize=8)
def load_catalog(language: str) -> IntentCatalog:
    """Carga y valida ``pilot/intents/<idioma>.toml``."""
    root = files(_INTENTS_PACKAGE).joinpath("intents")
    available = {
        entry.name[: -len(".toml")]: entry
        for entry in root.iterdir()
        if entry.name.endswith(".toml")
    }
    resource = available.get(language)
    if resource is None:
        names = ", ".join(sorted(available)) or "ninguno"
        raise RouterError(f"Idioma no soportado: {language!r}; disponibles: {names}")
    try:
        data = tomllib.loads(resource.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RouterError(f"Catálogo TOML inválido para {language!r}: {exc}") from exc
    return parse_catalog(data, language=language)


def route(
    text: str,
    *,
    language: str,
    drivers: DriverDirectory,
    dialogue: DialogueState | None,
    cfg: PilotConfig,
    now: float = 0.0,
    catalog: IntentCatalog | None = None,
) -> RoutedQuery:
    """Clasifica una pregunta y resuelve sus referencias a pilotos."""
    catalog = load_catalog(language) if catalog is None else catalog
    query = PilotQuery(
        query_id=_query_id(language, text, now),
        text=text,
        language=language,
        received_at=now,
    )
    normalized = normalize(text)
    tokens = tuple(normalized.split())

    resolved = _resolve_pending(text, dialogue, now)
    if resolved is not None:
        intent = _last_intent(dialogue, now) or Intent.UNKNOWN
        return _routed(
            query,
            normalized,
            intent,
            1.0,
            entities=(EntityRef(resolved.raw, resolved.driver.car_index),),
        )

    if _OUT_OF_SCOPE.search(normalized) is not None:
        return _routed(query, normalized, Intent.UNKNOWN, 0.0)

    scored = sorted(
        ((pattern.intent, pattern.score(normalized, tokens)) for pattern in catalog.intents),
        key=lambda item: -item[1],
    )
    accepted = [item for item in scored if item[1] >= cfg.min_intent_confidence]
    best_score = scored[0][1] if scored else 0.0
    forced = _forced_intent(normalized, scored, cfg)
    if forced is not None:
        intent, confidence = forced
    elif len(accepted) >= 2 and math.isclose(
        accepted[0][1], accepted[1][1], rel_tol=0.0, abs_tol=_TIE_TOLERANCE
    ):
        return _routed(query, normalized, Intent.UNKNOWN, 0.0)
    elif accepted:
        intent, confidence = accepted[0]
    else:
        last = _last_intent(dialogue, now)
        if last is None or not normalized.startswith("y "):
            return _routed(query, normalized, Intent.UNKNOWN, best_score)
        intent = last
        confidence = max(best_score, cfg.min_intent_confidence)

    entities, clarification = _references(normalized, drivers, cfg, now, intent)
    if intent in _COMPARABLE_STATE_INTENTS and (entities or clarification is not None):
        # Preguntar por el estado propio mencionando a un piloto es compararse con él.
        intent = Intent.COMPARE_DRIVER
    if clarification is None and intent in _PLAYER_ONLY_INTENTS and entities:
        player = drivers.player_car_index if isinstance(drivers, DriverDirectory) else None
        if player is not None and entities[0].car_index != player:
            return _routed(query, normalized, Intent.UNKNOWN, 0.0)
    if clarification is not None:
        start = getattr(dialogue, "start_clarification", None)
        if callable(start):
            start(
                clarification.options,
                now,
                message_key=clarification.message_key,
            )
    return _routed(
        query,
        normalized,
        intent,
        confidence,
        entities=entities,
        clarification=clarification,
    )


def _routed(
    query: PilotQuery,
    normalized: str,
    intent: Intent,
    confidence: float,
    *,
    entities: tuple[EntityRef, ...] = (),
    clarification: Clarification | None = None,
) -> RoutedQuery:
    return RoutedQuery(
        query=query,
        intent=intent,
        confidence=confidence,
        entities=entities,
        clarification=clarification,
        normalized_text=normalized,
    )


_COMPARE_CUE = re.compile(
    r"(?<![a-z0-9])(?:por delante|por detras|mejor que|peor que|mas rapido)"
    r"(?![a-z0-9])"
)


def _forced_intent(
    normalized: str, scored: list[tuple[Intent, float]], cfg: PilotConfig
) -> tuple[Intent, float] | None:
    """Una comparación explícita manda sobre el vocabulario de intervalos."""
    if _COMPARE_CUE.search(normalized) is None:
        return None
    compare = next((score for intent, score in scored if intent is Intent.COMPARE_DRIVER), 0.0)
    if compare <= 0:
        return None
    return Intent.COMPARE_DRIVER, max(compare, cfg.min_intent_confidence)


def _resolve_pending(text: str, dialogue: DialogueState | None, now: float) -> Resolved | None:
    """Contesta a una aclaración pendiente; otro texto la descarta."""
    if dialogue is None:
        return None
    pending = getattr(dialogue, "pending_clarification", None)
    resolver = getattr(dialogue, "resolve_clarification", None)
    if not callable(pending) or not callable(resolver) or pending(now) is None:
        return None
    outcome = resolver(text, now)
    if isinstance(outcome, Resolved):
        return outcome
    cancel = getattr(dialogue, "cancel_clarification", None)
    if callable(cancel):
        cancel()
    return None


def _references(
    normalized: str,
    drivers: DriverDirectory,
    cfg: PilotConfig,
    now: float,
    intent: Intent,
) -> tuple[tuple[EntityRef, ...], Clarification | None]:
    """Extrae y resuelve la primera referencia del texto."""
    if not isinstance(drivers, DriverDirectory):
        return (), None
    raw = _first_reference(normalized, drivers)
    if raw is None:
        unknown = _unknown_reference(normalized, intent)
        if unknown is None:
            return (), None
        return (EntityRef(unknown, reason="driver_not_found"),), None
    outcome = resolve_driver(raw, drivers)
    if isinstance(outcome, Resolved):
        return (EntityRef(raw, outcome.driver.car_index),), None
    if isinstance(outcome, Ambiguous):
        clarification = Clarification(
            message_key=AMBIGUOUS_DRIVER_MESSAGE_KEY,
            options=outcome.candidates,
            expires_at=now + cfg.clarification_ttl_s,
        )
        return (), clarification
    # La referencia fallida se conserva para que la herramienta conteste que no
    # hay datos de ese piloto en vez de elegir un rival sustituto.
    return (EntityRef(raw, reason=outcome.reason.value),), None


def _first_reference(normalized: str, drivers: DriverDirectory) -> str | None:
    refs: list[tuple[int, int, str]] = []

    def add(match: re.Match[str], raw: str) -> None:
        refs.append((match.start(), -match.end(), raw))

    match = _RELATIVE_REFERENCE.search(normalized)
    if match is not None:
        add(match, f"el de {match.group(1)}")
    match = _EXTREME_REFERENCE.search(normalized)
    if match is not None:
        add(match, f"el {match.group(1)}")
    match = _RELATIVE_TO_ME.search(normalized)
    if match is not None:
        word = "detras" if match.group(1) in {"detras", "atras"} else "delante"
        add(match, f"el de {word}")
    match = _TEAMMATE_REFERENCE.search(normalized)
    if match is not None:
        add(match, "mi companero")
    for team in _teams(drivers):
        match = re.search(rf"\b(?:el|al|del|la)\s+de\s+{re.escape(team)}\b", normalized)
        if match is not None:
            add(match, f"el de {team}")
    match = _NUMBER_REFERENCE.search(normalized)
    if match is not None:
        add(match, f"el {match.group(1)}")
    match = _POSITION_REFERENCE.search(normalized)
    if match is not None:
        add(match, f"el {match.group(1)}")
    match = _ORDINAL_REFERENCE.search(normalized)
    if match is not None and match.group(1) in _ORDINALS:
        add(match, f"el {match.group(1)}")
    for candidate in drivers.candidates:
        for alias in _aliases(candidate.name):
            match = re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized)
            if match is not None:
                add(match, alias)
    for token in dict.fromkeys(normalized.split()):
        if len(token) < 3 or not _is_name_prefix(token, drivers):
            continue
        match = re.search(rf"(?<!\w){re.escape(token)}(?!\w)", normalized)
        if match is not None:
            add(match, token)
    if not refs:
        return None
    refs.sort()
    return refs[0][2]


def _unknown_reference(normalized: str, intent: Intent) -> str | None:
    """Reconoce un nombre no listado tras una preposición de comparación."""
    if intent not in _DRIVER_INTENTS:
        return None
    match = _UNKNOWN_REFERENCE.search(normalized)
    if match is None or match.group(1) in _UNKNOWN_REFERENCE_STOPWORDS:
        return None
    return match.group(1)


def _teams(drivers: DriverDirectory) -> tuple[str, ...]:
    teams = {
        normalize_text(item.team)
        for item in drivers.candidates
        if item.team is not None and normalize_text(item.team)
    }
    return tuple(sorted(teams))


def _aliases(name: str) -> tuple[str, ...]:
    full = normalize_text(name)
    tokens = tuple(token for token in full.split() if len(token) >= 3)
    return tuple(dict.fromkeys((full, *tokens)))


def _is_name_prefix(token: str, drivers: DriverDirectory) -> bool:
    return any(
        word.startswith(token)
        for candidate in drivers.candidates
        for word in normalize_text(candidate.name).split()
        if len(word) > len(token)
    )


def _last_intent(dialogue: DialogueState | None, now: float) -> Intent | None:
    if dialogue is None:
        return None
    last = dialogue.last_intent(now)
    if last is None or Intent(last) is Intent.UNKNOWN:
        return None
    return Intent(last)


def _query_id(language: str, text: str, now: float) -> str:
    payload = f"{language}\x1f{text}\x1f{now!r}".encode()
    return f"q-{hashlib.blake2b(payload, digest_size=8).hexdigest()}"


def _variant_match(tokens: tuple[str, ...], word: str, words: frozenset[str]) -> bool:
    """Plural o conjugación: ``falta/faltan``, ``pieza/piezas``.

    El token debe alargar la palabra y no ser ya otra palabra del catálogo, para
    no contar dos veces singular y plural.
    """
    if len(word) < _VARIANT_MIN_LENGTH:
        return False
    for token in tokens:
        if token in words or len(token) < _VARIANT_MIN_LENGTH:
            continue
        if 1 <= len(token) - len(word) <= 2 and token.startswith(word):
            return True
    return False


def _fuzzy_match(tokens: tuple[str, ...], word: str) -> bool:
    if len(word) < _FUZZY_MIN_LENGTH:
        return False
    for token in tokens:
        if len(token) < _FUZZY_MIN_LENGTH or abs(len(token) - len(word)) > 2:
            continue
        if SequenceMatcher(None, token, word).ratio() >= _FUZZY_RATIO:
            return True
    return False


def _compile(value: object, *, intent: str, kind: str) -> tuple[_Pattern, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping) or not value:
        raise RouterError(f"La intención {intent!r} no tiene {kind}")
    compiled: list[_Pattern] = []
    for phrase, weight in value.items():
        if not isinstance(phrase, str):
            raise RouterError(f"{kind.capitalize()} de {intent!r} deben ser texto")
        word = normalize(phrase)
        if not word:
            raise RouterError(f"{kind.capitalize()} vacía en {intent!r}: {phrase!r}")
        compiled.append(_Pattern(_phrase_regex(word), word, _weight(weight, intent, phrase)))
    return tuple(compiled)


def _negatives(value: object, *, intent: str) -> tuple[re.Pattern[str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RouterError(f"'negative' de {intent!r} debe ser una lista")
    patterns: list[re.Pattern[str]] = []
    for item in value:
        if not isinstance(item, str):
            raise RouterError(f"'negative' de {intent!r} debe ser una lista de textos")
        word = normalize(item)
        if not word:
            raise RouterError(f"Patrón negativo vacío en {intent!r}: {item!r}")
        patterns.append(_phrase_regex(word))
    return tuple(patterns)


def _weight(value: object, intent: str, phrase: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 < value <= 1.0
    ):
        raise RouterError(f"Peso inválido en {intent!r}/{phrase!r}: {value!r} (0 < peso <= 1)")
    return float(value)


def _phrase_regex(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(word)}(?!\w)")


__all__ = [
    "SUGGESTED_INTENTS",
    "DriverDirectory",
    "DialogueState",
    "IntentCatalog",
    "IntentPattern",
    "RouterError",
    "load_catalog",
    "normalize",
    "parse_catalog",
    "route",
]
