"""Tipos inmutables de las preguntas del piloto (Fase 7).

Contrato común de la fase: el enrutador describe la pregunta, las herramientas
devuelven hechos clasificados y el compositor produce la respuesta. Todo lo que
se guarda o se muestra desde la consola se serializa a JSON canónico.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ..config import PilotConfig
from ..metrics.engine import MetricsSnapshot
from ..metrics.history import LapHistorySnapshot
from ..metrics.types import Quality
from ..race_state import RaceStateSnapshot
from ..strategy.types import StrategySnapshot
from ..weekend_profile import WeekendProfile


class Intent(StrEnum):
    """Intención reconocida en una pregunta del piloto."""

    GENERAL_RACE_STATE = "general_race_state"
    ASK_STRATEGY = "ask_strategy"
    FUEL_STATUS = "fuel_status"
    TYRE_LIFE = "tyre_life"
    PACE_DIAGNOSIS = "pace_diagnosis"
    COMPARE_DRIVER = "compare_driver"
    PIT_UNDERCUT = "pit_undercut"
    GAP_STATUS = "gap_status"
    DAMAGE_STATUS = "damage_status"
    WEATHER_FORECAST = "weather_forecast"
    UNKNOWN = "unknown"


class FactKind(StrEnum):
    """Origen de una cifra: juego, cálculo propio o recomendación."""

    MEASURED = "measured"
    ESTIMATED = "estimated"
    RECOMMENDED = "recommended"


class AnswerKind(StrEnum):
    """Tipo de respuesta emitida."""

    ACK = "ack"
    ANSWER = "answer"
    CLARIFICATION = "clarification"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    FOLLOWUP = "followup"


@dataclass(frozen=True, slots=True)
class PilotQuery:
    """Pregunta recibida, tal como llega desde una interfaz."""

    query_id: str
    text: str
    language: str
    received_at: float
    source: str = "console"

    def __post_init__(self) -> None:
        if not self.language.strip():
            raise ValueError("language no puede estar vacío")
        _check_finite("received_at", self.received_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "text": self.text,
            "language": self.language,
            "received_at": self.received_at,
            "source": self.source,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PilotQuery:
        return cls(
            query_id=_str(data, "query_id"),
            text=_str(data, "text"),
            language=_str(data, "language"),
            received_at=_float(data, "received_at", 0.0),
            source=_str(data, "source"),
        )

    @classmethod
    def from_json(cls, text: str) -> PilotQuery:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class DriverCandidate:
    """Piloto que puede satisfacer una referencia de la pregunta."""

    car_index: int
    name: str
    race_number: int | None = None
    position: int | None = None
    team: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "car_index": self.car_index,
            "name": self.name,
            "race_number": self.race_number,
            "position": self.position,
            "team": self.team,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DriverCandidate:
        return cls(
            car_index=_int(data, "car_index", 0),
            name=_str(data, "name"),
            race_number=_opt_int(data, "race_number"),
            position=_opt_int(data, "position"),
            team=_opt_str(data, "team"),
        )

    @classmethod
    def from_json(cls, text: str) -> DriverCandidate:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class EntityRef:
    """Referencia a un piloto: resuelta o con varios candidatos."""

    raw: str
    car_index: int | None = None
    candidates: tuple[DriverCandidate, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "car_index": self.car_index,
            "candidates": [item.to_dict() for item in self.candidates],
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EntityRef:
        return cls(
            raw=_str(data, "raw"),
            car_index=_opt_int(data, "car_index"),
            candidates=tuple(
                DriverCandidate.from_dict(item) for item in _json_list(data.get("candidates", []))
            ),
        )

    @classmethod
    def from_json(cls, text: str) -> EntityRef:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class Clarification:
    """Pregunta pendiente para desambiguar una referencia."""

    message_key: str
    options: tuple[DriverCandidate, ...]
    expires_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", tuple(self.options))
        _check_finite("expires_at", self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_key": self.message_key,
            "options": [item.to_dict() for item in self.options],
            "expires_at": self.expires_at,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Clarification:
        return cls(
            message_key=_str(data, "message_key"),
            options=tuple(
                DriverCandidate.from_dict(item) for item in _json_list(data.get("options", []))
            ),
            expires_at=_float(data, "expires_at", 0.0),
        )

    @classmethod
    def from_json(cls, text: str) -> Clarification:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class RoutedQuery:
    """Pregunta normalizada y clasificada por el enrutador."""

    query: PilotQuery
    intent: Intent
    confidence: float
    entities: tuple[EntityRef, ...] = ()
    clarification: Clarification | None = None
    normalized_text: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent", Intent(self.intent))
        object.__setattr__(self, "entities", tuple(self.entities))
        _check_finite("confidence", self.confidence)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence debe estar entre 0 y 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "intent": self.intent.value,
            "confidence": self.confidence,
            "entities": [item.to_dict() for item in self.entities],
            "clarification": None if self.clarification is None else self.clarification.to_dict(),
            "normalized_text": self.normalized_text,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RoutedQuery:
        clarification = data.get("clarification")
        return cls(
            query=PilotQuery.from_dict(_json_mapping(data.get("query", {}), "query")),
            intent=Intent(_str(data, "intent")),
            confidence=_float(data, "confidence", 0.0),
            entities=tuple(
                EntityRef.from_dict(item) for item in _json_list(data.get("entities", []))
            ),
            clarification=(
                None
                if clarification is None
                else Clarification.from_dict(_json_mapping(clarification, "clarification"))
            ),
            normalized_text=_str(data, "normalized_text"),
        )

    @classmethod
    def from_json(cls, text: str) -> RoutedQuery:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class ToolFact:
    """Cifra devuelta por una herramienta, con su origen y calidad."""

    name: str
    value: float | int | str | bool | None
    unit: str | None
    kind: FactKind
    quality: Quality

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("El nombre del hecho no puede estar vacío")
        if not _is_scalar(self.value):
            raise ValueError("value debe ser un escalar o None")
        if isinstance(self.value, float):
            _check_finite("value", self.value)
        object.__setattr__(self, "kind", FactKind(self.kind))
        object.__setattr__(self, "quality", Quality(self.quality))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "kind": self.kind.value,
            "quality": self.quality.value,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolFact:
        return cls(
            name=_str(data, "name"),
            value=data.get("value"),
            unit=_opt_str(data, "unit"),
            kind=FactKind(_str(data, "kind")),
            quality=Quality(_str(data, "quality")),
        )

    @classmethod
    def from_json(cls, text: str) -> ToolFact:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Salida de una herramienta: hechos, datos ausentes y obsolescencia."""

    intent: Intent
    quality: Quality
    facts: tuple[ToolFact, ...] = ()
    missing: tuple[str, ...] = ()
    stale: bool = False
    data_lap: int | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent", Intent(self.intent))
        object.__setattr__(self, "quality", Quality(self.quality))
        object.__setattr__(self, "facts", tuple(self.facts))
        object.__setattr__(self, "missing", tuple(self.missing))
        object.__setattr__(self, "notes", tuple(self.notes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "quality": self.quality.value,
            "facts": [item.to_dict() for item in self.facts],
            "missing": list(self.missing),
            "stale": self.stale,
            "data_lap": self.data_lap,
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolResult:
        return cls(
            intent=Intent(_str(data, "intent")),
            quality=Quality(_str(data, "quality")),
            facts=tuple(ToolFact.from_dict(item) for item in _json_list(data.get("facts", []))),
            missing=_str_tuple(data.get("missing", []), "missing"),
            stale=_bool(data, "stale", False),
            data_lap=_opt_int(data, "data_lap"),
            notes=_str_tuple(data.get("notes", []), "notes"),
        )

    @classmethod
    def from_json(cls, text: str) -> ToolResult:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class ToolInputs:
    """Instantáneas inmutables que reciben todas las herramientas."""

    state: RaceStateSnapshot
    metrics: MetricsSnapshot
    strategy: StrategySnapshot
    history: LapHistorySnapshot
    config: PilotConfig
    now: float
    profile: WeekendProfile | None = None

    def __post_init__(self) -> None:
        _check_finite("now", self.now)


@dataclass(frozen=True, slots=True)
class PilotAnswer:
    """Respuesta preparada para la interfaz, con su trazabilidad."""

    query_id: str
    intent: Intent
    kind: AnswerKind
    text: str
    message_key: str
    params: Mapping[str, Any] = field(default_factory=dict)
    tool_result: ToolResult | None = None
    latency_s: float | None = None
    followup_pending: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent", Intent(self.intent))
        object.__setattr__(self, "kind", AnswerKind(self.kind))
        object.__setattr__(self, "params", _frozen(self.params))
        if self.latency_s is not None:
            _check_finite("latency_s", self.latency_s)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "intent": self.intent.value,
            "kind": self.kind.value,
            "text": self.text,
            "message_key": self.message_key,
            "params": _plain(self.params),
            "tool_result": None if self.tool_result is None else self.tool_result.to_dict(),
            "latency_s": self.latency_s,
            "followup_pending": self.followup_pending,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PilotAnswer:
        tool_result = data.get("tool_result")
        return cls(
            query_id=_str(data, "query_id"),
            intent=Intent(_str(data, "intent")),
            kind=AnswerKind(_str(data, "kind")),
            text=_str(data, "text"),
            message_key=_str(data, "message_key"),
            params=_json_mapping(data.get("params", {}), "params"),
            tool_result=(
                None
                if tool_result is None
                else ToolResult.from_dict(_json_mapping(tool_result, "tool_result"))
            ),
            latency_s=_opt_float(data, "latency_s"),
            followup_pending=_bool(data, "followup_pending", False),
        )

    @classmethod
    def from_json(cls, text: str) -> PilotAnswer:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class PilotExchange:
    """Intercambio completo: pregunta recibida y respuesta emitida."""

    query: PilotQuery
    answer: PilotAnswer
    # Interpretación completa para el grabador; fuera del JSON público y la huella.
    routed: RoutedQuery | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"query": self.query.to_dict(), "answer": self.answer.to_dict()}

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PilotExchange:
        return cls(
            query=PilotQuery.from_dict(_json_mapping(data.get("query", {}), "query")),
            answer=PilotAnswer.from_dict(_json_mapping(data.get("answer", {}), "answer")),
        )

    @classmethod
    def from_json(cls, text: str) -> PilotExchange:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class PilotSnapshot:
    """Estado publicado de las preguntas: intercambios recientes y contadores."""

    recent: tuple[PilotExchange, ...] = ()
    pending_clarification: Clarification | None = None
    counters: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "recent", tuple(self.recent))
        object.__setattr__(self, "counters", _frozen(dict(self.counters)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "recent": [item.to_dict() for item in self.recent],
            "pending_clarification": (
                None if self.pending_clarification is None else self.pending_clarification.to_dict()
            ),
            "counters": dict(self.counters),
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PilotSnapshot:
        pending = data.get("pending_clarification")
        counters = _json_mapping(data.get("counters", {}), "counters")
        return cls(
            recent=tuple(
                PilotExchange.from_dict(item) for item in _json_list(data.get("recent", []))
            ),
            pending_clarification=(
                None
                if pending is None
                else Clarification.from_dict(_json_mapping(pending, "pending_clarification"))
            ),
            counters={str(key): _int_value(value, "counters") for key, value in counters.items()},
        )

    @classmethod
    def from_json(cls, text: str) -> PilotSnapshot:
        return cls.from_dict(_loads(text))


Tool = Callable[[RoutedQuery, ToolInputs], ToolResult]


def unavailable_answer() -> PilotAnswer:
    """Stub de Q1: todavía no hay motor de preguntas."""
    return PilotAnswer(
        query_id="",
        intent=Intent.UNKNOWN,
        kind=AnswerKind.UNAVAILABLE,
        text="",
        message_key="pilot_unavailable",
    )


def _dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _loads(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido: {exc}") from exc
    return _json_mapping(data, "payload")


def _frozen(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_frozen(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"Valor no serializable: {type(value).__name__}")


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _check_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} debe ser un número finito")


def _json_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} debe ser un objeto JSON")
    return dict(value)


def _json_list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("Se esperaba una lista JSON")
    return value


def _str(data: Mapping[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} debe ser texto")
    return value


def _opt_str(data: Mapping[str, Any], name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} debe ser texto o null")
    return value


def _int(data: Mapping[str, Any], name: str, default: int) -> int:
    return _int_value(data.get(name, default), name)


def _int_value(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} debe ser entero")
    return value


def _opt_int(data: Mapping[str, Any], name: str) -> int | None:
    value = data.get(name)
    return None if value is None else _int_value(value, name)


def _float(data: Mapping[str, Any], name: str, default: float) -> float:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} debe ser numérico")
    return float(value)


def _opt_float(data: Mapping[str, Any], name: str) -> float | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} debe ser numérico o null")
    return float(value)


def _bool(data: Mapping[str, Any], name: str, default: bool) -> bool:
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} debe ser booleano")
    return value


def _str_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} debe ser una lista de textos")
    return tuple(value)


__all__ = [
    "AnswerKind",
    "Clarification",
    "DriverCandidate",
    "EntityRef",
    "FactKind",
    "Intent",
    "PilotAnswer",
    "PilotExchange",
    "PilotQuery",
    "PilotSnapshot",
    "RoutedQuery",
    "Tool",
    "ToolFact",
    "ToolInputs",
    "ToolResult",
    "unavailable_answer",
]
