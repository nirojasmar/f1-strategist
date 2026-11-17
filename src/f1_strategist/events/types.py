"""Tipos del detector de eventos y de la compuerta de avisos.

Los modelos son inmutables y no contienen lógica de detección ni de decisión:
solo describen observaciones, políticas, señales, avisos y decisiones. Las
señales, los avisos y las decisiones se serializan a JSON canónico para que su
representación sea estable e independiente del orden de los hechos.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Protocol

from ..config import EventsConfig
from ..metrics.engine import MetricsSnapshot
from ..metrics.types import Quality
from ..race_state import RaceEvent, RaceStateSnapshot
from ..strategy.types import StrategySnapshot

EVENTS_SCHEMA_VERSION = 1


class EventPriority(StrEnum):
    """Prioridad de un aviso; ordena el arbitraje de la compuerta."""

    CRITICAL = "critical"
    STRATEGIC = "strategic"
    INFORMATIONAL = "informational"


class EventPhase(StrEnum):
    """Fase del ciclo de vida de una condición."""

    OPENED = "opened"
    UPDATED = "updated"
    CLOSED = "closed"


class GateOutcome(StrEnum):
    """Resultado de procesar una señal en la compuerta."""

    EMIT = "emit"
    UPDATE = "update"
    SUPPRESS = "suppress"
    DEFER = "defer"
    CANCEL = "cancel"


Fact = tuple[str, float | int | str | None]


@dataclass(frozen=True, slots=True)
class Observation:
    """Lectura de una regla en un instante concreto.

    ``active`` desconocido (``None``) no abre ni cierra la condición. ``value``
    es el valor de la condición y ``facts`` los datos que acompañarán al aviso.
    """

    key: str
    active: bool | None
    value: float | None = None
    priority: EventPriority | None = None
    facts: tuple[Fact, ...] = ()
    material_key: str | None = None
    quality: Quality | None = None

    def __post_init__(self) -> None:
        _check_identifier("key", self.key)
        if self.active is not None and not isinstance(self.active, bool):
            raise ValueError("active debe ser un booleano o None")
        if self.value is not None:
            _check_finite("value", self.value)
        if self.priority is not None:
            object.__setattr__(self, "priority", EventPriority(self.priority))
        if self.quality is not None:
            object.__setattr__(self, "quality", Quality(self.quality))
        if self.material_key is not None:
            _check_identifier("material_key", self.material_key)
        object.__setattr__(self, "facts", _canonical_facts(self.facts))


@dataclass(frozen=True, slots=True)
class ConditionPolicy:
    """Reglas comunes de persistencia, histéresis y actualización.

    ``direction`` indica si la condición se abre al quedar por debajo o por
    encima de ``enter``. Con histéresis, ``exit`` debe quedar al otro lado:
    mayor que ``enter`` si la dirección es ``below`` y menor si es ``above``.
    """

    rule: str
    category: str
    priority: EventPriority
    message_key: str
    persistence_s: float = 0.0
    persistence_observations: int = 0
    clear_persistence_s: float = 0.0
    enter: float | None = None
    exit: float | None = None
    direction: Literal["below", "above"] | None = None
    enter_inclusive: bool = False
    update_min_delta: float | None = None
    one_shot: bool = False
    announce_close: bool = False

    def __post_init__(self) -> None:
        _check_identifier("rule", self.rule)
        _check_identifier("category", self.category)
        _check_identifier("message_key", self.message_key)
        object.__setattr__(self, "priority", EventPriority(self.priority))
        _check_non_negative_finite("persistence_s", self.persistence_s)
        if type(self.persistence_observations) is not int or self.persistence_observations < 0:
            raise ValueError("persistence_observations debe ser un entero no negativo")
        _check_non_negative_finite("clear_persistence_s", self.clear_persistence_s)
        if self.enter is not None:
            _check_finite("enter", self.enter)
        if self.exit is not None:
            _check_finite("exit", self.exit)
        if self.update_min_delta is not None:
            _check_non_negative_finite("update_min_delta", self.update_min_delta)
        if self.direction is not None and self.direction not in {"below", "above"}:
            raise ValueError("direction debe ser 'below', 'above' o None")
        if not isinstance(self.enter_inclusive, bool):
            raise ValueError("enter_inclusive debe ser booleano")
        if (self.enter is None) != (self.exit is None):
            raise ValueError("enter y exit deben declararse juntos")
        if self.enter is not None and self.exit is not None:
            if self.direction is None:
                raise ValueError("direction es obligatoria cuando hay enter y exit")
            if self.direction == "below" and self.exit <= self.enter:
                raise ValueError("exit debe ser mayor que enter cuando direction es 'below'")
            if self.direction == "above" and self.exit >= self.enter:
                raise ValueError("exit debe ser menor que enter cuando direction es 'above'")


@dataclass(frozen=True, slots=True)
class EventInputs:
    """Entradas de una evaluación del detector; el instante llega como parámetro."""

    state: RaceStateSnapshot
    metrics: MetricsSnapshot
    strategy: StrategySnapshot
    config: EventsConfig
    now: float
    lap: int
    new_state_events: tuple[RaceEvent, ...] = ()

    def __post_init__(self) -> None:
        _check_non_negative_finite("now", self.now)
        if type(self.lap) is not int or self.lap < 0:
            raise ValueError("lap debe ser un entero no negativo")
        object.__setattr__(self, "new_state_events", tuple(self.new_state_events))


class EventRule(Protocol):
    """Contrato de una regla de detección."""

    @property
    def policy(self) -> ConditionPolicy: ...

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]: ...


@dataclass(frozen=True, slots=True)
class EventSignal:
    """Señal emitida por el detector y consumida por la compuerta."""

    rule: str
    event_id: str
    phase: EventPhase
    priority: EventPriority
    category: str
    message_key: str
    facts: tuple[Fact, ...] = ()
    material_key: str | None = None
    lap: int = 0
    observed_at: float = 0.0

    def __post_init__(self) -> None:
        _check_identifier("rule", self.rule)
        _check_identifier("event_id", self.event_id)
        _check_identifier("category", self.category)
        _check_identifier("message_key", self.message_key)
        object.__setattr__(self, "phase", EventPhase(self.phase))
        object.__setattr__(self, "priority", EventPriority(self.priority))
        object.__setattr__(self, "facts", _canonical_facts(self.facts))
        if self.material_key is not None:
            _check_identifier("material_key", self.material_key)
        _check_lap("lap", self.lap)
        _check_non_negative_finite("observed_at", self.observed_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVENTS_SCHEMA_VERSION,
            "rule": self.rule,
            "event_id": self.event_id,
            "phase": self.phase.value,
            "priority": self.priority.value,
            "category": self.category,
            "message_key": self.message_key,
            "facts": _facts_payload(self.facts),
            "material_key": self.material_key,
            "lap": self.lap,
            "observed_at": self.observed_at,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EventSignal:
        """Reconstruye la señal desde su representación JSON validada."""
        _check_schema_version(data)
        return cls(
            rule=_required_str(data, "rule"),
            event_id=_required_str(data, "event_id"),
            phase=EventPhase(_required_str(data, "phase")),
            priority=EventPriority(_required_str(data, "priority")),
            category=_required_str(data, "category"),
            message_key=_required_str(data, "message_key"),
            facts=_facts_from_payload(data.get("facts", ())),
            material_key=_optional_str(data, "material_key"),
            lap=_required_int(data, "lap"),
            observed_at=_required_float(data, "observed_at"),
        )

    @classmethod
    def from_json(cls, text: str) -> EventSignal:
        """Reconstruye la señal desde su JSON canónico."""
        return cls.from_dict(_json_mapping(text, "La señal"))


@dataclass(frozen=True, slots=True)
class AgentAlert:
    """Aviso inmutable listo para la consola o el contexto del LLM."""

    alert_id: str
    event_id: str
    phase: EventPhase
    priority: EventPriority
    category: str
    message_key: str
    facts: tuple[Fact, ...] = ()
    lap: int = 0
    emitted_at: float = 0.0
    interrupt: bool = False
    supersedes: str | None = None

    def __post_init__(self) -> None:
        _check_identifier("alert_id", self.alert_id)
        _check_identifier("event_id", self.event_id)
        _check_identifier("category", self.category)
        _check_identifier("message_key", self.message_key)
        object.__setattr__(self, "phase", EventPhase(self.phase))
        object.__setattr__(self, "priority", EventPriority(self.priority))
        object.__setattr__(self, "facts", _canonical_facts(self.facts))
        _check_lap("lap", self.lap)
        _check_non_negative_finite("emitted_at", self.emitted_at)
        if not isinstance(self.interrupt, bool):
            raise ValueError("interrupt debe ser booleano")
        if self.supersedes is not None:
            _check_identifier("supersedes", self.supersedes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVENTS_SCHEMA_VERSION,
            "alert_id": self.alert_id,
            "event_id": self.event_id,
            "phase": self.phase.value,
            "priority": self.priority.value,
            "category": self.category,
            "message_key": self.message_key,
            "facts": _facts_payload(self.facts),
            "lap": self.lap,
            "emitted_at": self.emitted_at,
            "interrupt": self.interrupt,
            "supersedes": self.supersedes,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentAlert:
        """Reconstruye el aviso desde su representación JSON validada."""
        _check_schema_version(data)
        return cls(
            alert_id=_required_str(data, "alert_id"),
            event_id=_required_str(data, "event_id"),
            phase=EventPhase(_required_str(data, "phase")),
            priority=EventPriority(_required_str(data, "priority")),
            category=_required_str(data, "category"),
            message_key=_required_str(data, "message_key"),
            facts=_facts_from_payload(data.get("facts", ())),
            lap=_required_int(data, "lap"),
            emitted_at=_required_float(data, "emitted_at"),
            interrupt=_required_bool(data, "interrupt"),
            supersedes=_optional_str(data, "supersedes"),
        )

    @classmethod
    def from_json(cls, text: str) -> AgentAlert:
        """Reconstruye el aviso desde su JSON canónico."""
        return cls.from_dict(_json_mapping(text, "El aviso"))


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Resultado explicado de la compuerta para una señal."""

    event_id: str
    phase: EventPhase
    outcome: GateOutcome
    reason: str
    alert: AgentAlert | None = None
    observed_at: float = 0.0

    def __post_init__(self) -> None:
        _check_identifier("event_id", self.event_id)
        _check_identifier("reason", self.reason)
        object.__setattr__(self, "phase", EventPhase(self.phase))
        object.__setattr__(self, "outcome", GateOutcome(self.outcome))
        _check_non_negative_finite("observed_at", self.observed_at)
        if self.outcome in {GateOutcome.EMIT, GateOutcome.UPDATE}:
            if self.alert is None:
                raise ValueError("EMIT y UPDATE requieren un aviso")
        elif self.alert is not None:
            raise ValueError(f"{self.outcome.value} no puede llevar un aviso")
        if self.alert is not None:
            if self.alert.event_id != self.event_id:
                raise ValueError("El aviso debe pertenecer al mismo event_id")
            if self.alert.phase is not self.phase:
                raise ValueError("La fase del aviso debe coincidir con la de la decisión")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVENTS_SCHEMA_VERSION,
            "event_id": self.event_id,
            "phase": self.phase.value,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "alert": None if self.alert is None else self.alert.to_dict(),
            "observed_at": self.observed_at,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GateDecision:
        """Reconstruye la decisión desde su representación JSON validada."""
        _check_schema_version(data)
        alert_payload = data.get("alert")
        if alert_payload is not None and not isinstance(alert_payload, Mapping):
            raise ValueError("alert debe ser un objeto JSON o None")
        return cls(
            event_id=_required_str(data, "event_id"),
            phase=EventPhase(_required_str(data, "phase")),
            outcome=GateOutcome(_required_str(data, "outcome")),
            reason=_required_str(data, "reason"),
            alert=None if alert_payload is None else AgentAlert.from_dict(alert_payload),
            observed_at=_required_float(data, "observed_at"),
        )

    @classmethod
    def from_json(cls, text: str) -> GateDecision:
        """Reconstruye la decisión desde su JSON canónico."""
        return cls.from_dict(_json_mapping(text, "La decisión"))


@dataclass(frozen=True, slots=True)
class EventsSnapshot:
    """Vista inmutable del estado del detector y de la compuerta."""

    alerts: tuple[AgentAlert, ...] = ()
    active_events: tuple[EventSignal, ...] = ()
    counters: Mapping[str, int] = field(default_factory=dict)
    muted: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        alerts = tuple(sorted(self.alerts, key=lambda alert: (alert.emitted_at, alert.alert_id)))
        active = tuple(sorted(self.active_events, key=lambda signal: signal.event_id))
        counters: dict[str, int] = {}
        for key, value in self.counters.items():
            _check_identifier("counters", key)
            if type(value) is not int or value < 0:
                raise ValueError("Los contadores deben ser enteros no negativos")
            counters[key] = value
        object.__setattr__(self, "alerts", alerts)
        object.__setattr__(self, "active_events", active)
        object.__setattr__(self, "counters", MappingProxyType(dict(sorted(counters.items()))))
        if not isinstance(self.muted, bool):
            raise ValueError("muted debe ser booleano")
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("error debe ser una cadena o None")


def _check_schema_version(data: Mapping[str, Any]) -> None:
    version = data.get("schema_version")
    if type(version) is not int:
        raise ValueError("schema_version debe estar presente y ser un entero")
    if version != EVENTS_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version no soportada: {version}; esperada {EVENTS_SCHEMA_VERSION}"
        )


def _json_mapping(text: str, description: str) -> Mapping[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{description} debe ser un objeto JSON")
    return payload


def _required_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} debe ser una cadena")
    return value


def _optional_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} debe ser una cadena o None")
    return value


def _required_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise ValueError(f"{key} debe ser un entero")
    return value


def _required_float(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} debe ser un número")
    result = float(value)
    _check_finite(key, result)
    return result


def _required_bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} debe ser booleano")
    return value


def _check_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} no puede estar vacío")


def _check_lap(name: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} debe ser un entero no negativo")


def _check_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} debe ser finito")


def _check_non_negative_finite(name: str, value: float) -> None:
    _check_finite(name, value)
    if value < 0:
        raise ValueError(f"{name} no puede ser negativo")


def _canonical_facts(facts: Sequence[Fact]) -> tuple[Fact, ...]:
    normalized: dict[str, float | int | str | None] = {}
    for name, value in facts:
        _check_identifier("El nombre de un hecho", name)
        if name in normalized:
            raise ValueError(f"Hecho duplicado: {name!r}")
        if isinstance(value, bool):
            raise ValueError(f"El hecho {name!r} no admite booleanos")
        if isinstance(value, float):
            _check_finite(f"facts.{name}", value)
            normalized[name] = value
        elif value is None or isinstance(value, (int, str)):
            normalized[name] = value
        else:
            raise ValueError(f"El hecho {name!r} debe ser número, cadena o None")
    return tuple((name, normalized[name]) for name in sorted(normalized))


def _facts_payload(facts: tuple[Fact, ...]) -> list[list[object]]:
    return [[name, value] for name, value in facts]


def _facts_from_payload(payload: object) -> tuple[Fact, ...]:
    if not isinstance(payload, (list, tuple)):
        raise ValueError("facts debe ser una lista")
    facts: list[Fact] = []
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("Cada hecho debe ser una pareja [nombre, valor]")
        name, value = item
        if not isinstance(name, str):
            raise ValueError("El nombre de un hecho debe ser una cadena")
        if isinstance(value, bool) or (
            value is not None and not isinstance(value, (int, float, str))
        ):
            raise ValueError(f"El hecho {name!r} debe ser número, cadena o None")
        facts.append((name, value))
    return _canonical_facts(facts)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
