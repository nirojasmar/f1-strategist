"""Enrutador determinista de preguntas del piloto (Fase 7, Q3).

Normaliza el texto, puntúa las intenciones del catálogo del idioma y devuelve
la mejor por encima de ``min_intent_confidence``. No resuelve entidades (Q4),
no compone respuestas (Q10) y no lee el reloj: el instante llega como parámetro.

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
from .types import Intent, PilotQuery, RoutedQuery

SUGGESTED_INTENTS: tuple[Intent, ...] = (
    Intent.FUEL_STATUS,
    Intent.TYRE_LIFE,
    Intent.ASK_STRATEGY,
)

_FUZZY_MIN_LENGTH = 5
_FUZZY_RATIO = 0.85
_FUZZY_PENALTY = 0.85
_TIE_TOLERANCE = 1e-9

_INTENTS_PACKAGE = "f1_strategist.pilot"


class RouterError(ValueError):
    """Idioma no soportado o catálogo de intenciones inválido."""


class DriverDirectory(Protocol):
    """Marcador del directorio de Q4; el enrutador no resuelve entidades."""


class DialogueState(Protocol):
    """Contrato de Q4 que usa el enrutador: intención del turno anterior."""

    def last_intent(self, now: float) -> Intent | None: ...


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
        for keyword in self.keywords:
            if keyword.pattern.search(text):
                total += keyword.weight
            elif " " not in keyword.word and _fuzzy_match(tokens, keyword.word):
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
    """Clasifica una pregunta; sin intención suficiente devuelve ``UNKNOWN``."""
    del drivers  # Q4 resuelve entidades; el enrutador solo clasifica.
    catalog = load_catalog(language) if catalog is None else catalog
    query = PilotQuery(
        query_id=_query_id(language, text, now),
        text=text,
        language=language,
        received_at=now,
    )
    normalized = normalize(text)
    tokens = tuple(normalized.split())
    scored = sorted(
        ((pattern.intent, pattern.score(normalized, tokens)) for pattern in catalog.intents),
        key=lambda item: -item[1],
    )
    accepted = [item for item in scored if item[1] >= cfg.min_intent_confidence]
    if len(accepted) >= 2 and math.isclose(
        accepted[0][1], accepted[1][1], rel_tol=0.0, abs_tol=_TIE_TOLERANCE
    ):
        return _routed(query, normalized, Intent.UNKNOWN, 0.0)
    if accepted:
        return _routed(query, normalized, accepted[0][0], accepted[0][1])

    best_score = scored[0][1] if scored else 0.0
    last = _last_intent(dialogue, now)
    if last is not None and normalized.startswith("y "):
        confidence = max(best_score, cfg.min_intent_confidence)
        return _routed(query, normalized, last, confidence)
    return _routed(query, normalized, Intent.UNKNOWN, best_score)


def _routed(query: PilotQuery, normalized: str, intent: Intent, confidence: float) -> RoutedQuery:
    return RoutedQuery(
        query=query,
        intent=intent,
        confidence=confidence,
        normalized_text=normalized,
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
