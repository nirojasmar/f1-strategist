"""Tipos inmutables del agente de radio (Fase 6).

El contrato común de la fase: el código determinista rellena el contexto y los
hechos; el LLM solo redacta. Todo se serializa a JSON canónico para poder
guardarlo, compararlo y medirlo.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..events.types import EventPriority
from ..strategy.types import compute_inputs_hash

AGENT_SCHEMA_VERSION = 1


class MessageSource(StrEnum):
    """Origen del texto emitido."""

    LLM = "llm"
    TEMPLATE = "template"


class CallOutcome(StrEnum):
    """Resultado de una llamada al modelo."""

    USED = "used"
    FALLBACK = "fallback"
    DISCARDED_OBSOLETE = "discarded_obsolete"
    DECLINED = "declined"
    SKIPPED = "skipped"


class FallbackReason(StrEnum):
    """Causa estable de un respaldo o descarte."""

    LLM_DISABLED = "llm_disabled"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    INVALID_JSON = "invalid_json"
    VALIDATION_FAILED = "validation_failed"
    CANCELLED = "cancelled"
    OBSOLETE = "obsolete"


class LLMStatus(StrEnum):
    """Disponibilidad observada del endpoint."""

    DISABLED = "disabled"
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ContextKind(StrEnum):
    """Tipo de petición de contexto."""

    ALERT = "alert"
    QUESTION = "question"


@dataclass(frozen=True, slots=True)
class LLMHealth:
    """Último estado conocido del endpoint."""

    status: LLMStatus = LLMStatus.DISABLED
    model: str | None = None
    latency_s: float | None = None
    checked_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "model": self.model,
            "latency_s": self.latency_s,
            "checked_at": self.checked_at,
            "error": self.error,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LLMHealth:
        return cls(
            status=LLMStatus(_str(data, "status")),
            model=_opt_str(data, "model"),
            latency_s=_opt_float(data, "latency_s"),
            checked_at=_opt_float(data, "checked_at"),
            error=_opt_str(data, "error"),
        )

    @classmethod
    def from_json(cls, text: str) -> LLMHealth:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class ContextPayload:
    """Contexto mínimo enviado al modelo, con huella estable de su contenido."""

    kind: ContextKind
    language: str
    alert: Mapping[str, Any] | None = None
    situation: Mapping[str, Any] = field(default_factory=dict)
    strategy: Mapping[str, Any] | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    uncertainties: tuple[str, ...] = ()
    recent_messages: tuple[str, ...] = ()
    glossary: Mapping[str, str] = field(default_factory=dict)
    constraints: Mapping[str, Any] = field(default_factory=dict)
    context_hash: str = ""

    def __post_init__(self) -> None:
        if not self.language.strip():
            raise ValueError("language no puede estar vacío")
        object.__setattr__(self, "alert", _frozen(self.alert))
        object.__setattr__(self, "situation", _frozen(self.situation))
        object.__setattr__(self, "strategy", _frozen(self.strategy))
        object.__setattr__(self, "metrics", _frozen(self.metrics))
        object.__setattr__(self, "glossary", _frozen(self.glossary))
        object.__setattr__(self, "constraints", _frozen(self.constraints))
        object.__setattr__(self, "uncertainties", tuple(self.uncertainties))
        object.__setattr__(self, "recent_messages", tuple(self.recent_messages))
        if not self.context_hash:
            object.__setattr__(self, "context_hash", self.compute_hash())
            return
        computed = self.compute_hash()
        if self.context_hash != computed:
            raise ValueError("context_hash no coincide con el contenido del contexto")

    def compute_hash(self) -> str:
        """SHA-256 del contenido del contexto, sin incluir la propia huella."""
        payload = {item.name: getattr(self, item.name) for item in fields(self)}
        payload.pop("context_hash")
        return compute_inputs_hash(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": AGENT_SCHEMA_VERSION,
            "kind": self.kind.value,
            "language": self.language,
            "alert": _plain(self.alert),
            "situation": _plain(self.situation),
            "strategy": _plain(self.strategy),
            "metrics": _plain(self.metrics),
            "uncertainties": list(self.uncertainties),
            "recent_messages": list(self.recent_messages),
            "glossary": dict(self.glossary),
            "constraints": _plain(self.constraints),
            "context_hash": self.context_hash,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ContextPayload:
        _check_schema_version(data)
        alert = data.get("alert")
        strategy = data.get("strategy")
        return cls(
            kind=ContextKind(_str(data, "kind")),
            language=_str(data, "language"),
            alert=None if alert is None else _json_mapping(alert, "alert"),
            situation=_json_mapping(data.get("situation", {}), "situation"),
            strategy=None if strategy is None else _json_mapping(strategy, "strategy"),
            metrics=_json_mapping(data.get("metrics", {}), "metrics"),
            uncertainties=_str_tuple(data.get("uncertainties", []), "uncertainties"),
            recent_messages=_str_tuple(data.get("recent_messages", []), "recent_messages"),
            glossary=_str_mapping(data.get("glossary", {}), "glossary"),
            constraints=_json_mapping(data.get("constraints", {}), "constraints"),
            context_hash=_opt_str(data, "context_hash") or "",
        )

    @classmethod
    def from_json(cls, text: str) -> ContextPayload:
        return cls.from_dict(_loads(text))


class LLMReply(BaseModel):
    """Respuesta estructurada del modelo: solo texto y hechos usados."""

    model_config = ConfigDict(extra="forbid")

    should_speak: bool = True
    priority: EventPriority
    language: str
    message: str
    reason: str = ""
    used_facts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LLMReply:
        return cls.model_validate(dict(data))

    @classmethod
    def from_json(cls, text: str) -> LLMReply:
        return cls.model_validate_json(text)


def wire_reply_schema() -> dict[str, Any]:
    """Esquema estricto de la respuesta: todos los campos son obligatorios."""
    schema = LLMReply.model_json_schema()
    schema["required"] = list(LLMReply.model_fields)
    return schema


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """Mensaje con formato OpenAI (rol y contenido)."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LLMMessage:
        return cls(role=_str(data, "role"), content=_str(data, "content"))


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Petición al endpoint compatible con OpenAI."""

    model: str
    messages: tuple[LLMMessage, ...]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stream: bool = False
    response_format: Mapping[str, Any] | None = None
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    timeout_s: float = 6.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "response_format", _frozen(self.response_format))
        object.__setattr__(self, "extra_body", _frozen(self.extra_body))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [item.to_dict() for item in self.messages],
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stream": self.stream,
            "response_format": _plain(self.response_format),
            "extra_body": _plain(self.extra_body),
            "timeout_s": self.timeout_s,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LLMRequest:
        response_format = data.get("response_format")
        return cls(
            model=_str(data, "model"),
            messages=tuple(
                LLMMessage.from_dict(item) for item in _json_list(data.get("messages", []))
            ),
            temperature=_opt_float(data, "temperature"),
            top_p=_opt_float(data, "top_p"),
            max_tokens=_opt_int(data, "max_tokens"),
            seed=_opt_int(data, "seed"),
            stream=_bool(data, "stream", False),
            response_format=(
                None
                if response_format is None
                else _json_mapping(response_format, "response_format")
            ),
            extra_body=_json_mapping(data.get("extra_body", {}), "extra_body"),
            timeout_s=_float(data, "timeout_s", 6.0),
        )

    @classmethod
    def from_json(cls, text: str) -> LLMRequest:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Respuesta cruda del cliente HTTP; ``error`` no vacío si no hubo texto."""

    text: str = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    ttft_s: float | None = None
    total_s: float | None = None
    model_id: str | None = None
    structured_mode: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "ttft_s": self.ttft_s,
            "total_s": self.total_s,
            "model_id": self.model_id,
            "structured_mode": self.structured_mode,
            "error": self.error,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LLMResponse:
        return cls(
            text=_str(data, "text"),
            finish_reason=_opt_str(data, "finish_reason"),
            prompt_tokens=_opt_int(data, "prompt_tokens"),
            completion_tokens=_opt_int(data, "completion_tokens"),
            ttft_s=_opt_float(data, "ttft_s"),
            total_s=_opt_float(data, "total_s"),
            model_id=_opt_str(data, "model_id"),
            structured_mode=_opt_str(data, "structured_mode"),
            error=_opt_str(data, "error"),
        )

    @classmethod
    def from_json(cls, text: str) -> LLMResponse:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """Resultado de una llamada al LLM, tal como llega al grabador.

    El grabador añade ``seq`` y el instante simulado; el resto son datos ya
    interpretados. Nunca incluye la clave de API ni cabeceras HTTP.
    """

    kind: str
    model: str
    outcome: str
    alert_id: str | None = None
    endpoint_host: str | None = None
    prompt_version: str | None = None
    parameters_json: str | None = None
    context_hash: str | None = None
    context_json: str | None = None
    response_text: str | None = None
    reply_json: str | None = None
    validation_valid: bool | None = None
    validation_errors: tuple[str, ...] = ()
    fallback_reason: str | None = None
    error: str | None = None
    ttft_s: float | None = None
    total_s: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Salida del validador; ``errors`` son códigos estables, no texto libre."""

    valid: bool
    errors: tuple[str, ...] = ()
    reply: LLMReply | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "errors", tuple(self.errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "reply": None if self.reply is None else self.reply.to_dict(),
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ValidationResult:
        reply = data.get("reply")
        return cls(
            valid=_bool(data, "valid", False),
            errors=_str_tuple(data.get("errors", []), "errors"),
            reply=None if reply is None else LLMReply.from_dict(_json_mapping(reply, "reply")),
        )

    @classmethod
    def from_json(cls, text: str) -> ValidationResult:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """Mensaje emitido al piloto, con su trazabilidad."""

    message_id: str
    alert_id: str | None
    event_id: str | None
    priority: EventPriority
    language: str
    text: str
    source: MessageSource
    used_facts: tuple[str, ...] = ()
    lap: int = 0
    emitted_at: float = 0.0
    interrupt: bool = False
    latency_s: float | None = None
    llm_call_id: str | None = None

    def __post_init__(self) -> None:
        if not self.message_id.strip():
            raise ValueError("message_id no puede estar vacío")
        object.__setattr__(self, "used_facts", tuple(self.used_facts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "alert_id": self.alert_id,
            "event_id": self.event_id,
            "priority": self.priority.value,
            "language": self.language,
            "text": self.text,
            "source": self.source.value,
            "used_facts": list(self.used_facts),
            "lap": self.lap,
            "emitted_at": self.emitted_at,
            "interrupt": self.interrupt,
            "latency_s": self.latency_s,
            "llm_call_id": self.llm_call_id,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentMessage:
        return cls(
            message_id=_str(data, "message_id"),
            alert_id=_opt_str(data, "alert_id"),
            event_id=_opt_str(data, "event_id"),
            priority=EventPriority(_str(data, "priority")),
            language=_str(data, "language"),
            text=_str(data, "text"),
            source=MessageSource(_str(data, "source")),
            used_facts=_str_tuple(data.get("used_facts", []), "used_facts"),
            lap=_int(data, "lap", 0),
            emitted_at=_float(data, "emitted_at", 0.0),
            interrupt=_bool(data, "interrupt", False),
            latency_s=_opt_float(data, "latency_s"),
            llm_call_id=_opt_str(data, "llm_call_id"),
        )

    @classmethod
    def from_json(cls, text: str) -> AgentMessage:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    """Estado publicado del agente: mensajes recientes, salud y contadores."""

    messages: tuple[AgentMessage, ...] = ()
    llm_status: LLMHealth = field(default_factory=LLMHealth)
    counters: Mapping[str, int] = field(default_factory=dict)
    last_latency_s: float | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "counters", _frozen(self.counters))

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [item.to_dict() for item in self.messages],
            "llm_status": self.llm_status.to_dict(),
            "counters": dict(self.counters),
            "last_latency_s": self.last_latency_s,
            "error": self.error,
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentSnapshot:
        return cls(
            messages=tuple(
                AgentMessage.from_dict(item) for item in _json_list(data.get("messages", []))
            ),
            llm_status=LLMHealth.from_dict(_json_mapping(data.get("llm_status", {}), "llm_status")),
            counters={
                str(key): _int_value(value, "counters")
                for key, value in _json_mapping(data.get("counters", {}), "counters").items()
            },
            last_latency_s=_opt_float(data, "last_latency_s"),
            error=_opt_str(data, "error"),
        )

    @classmethod
    def from_json(cls, text: str) -> AgentSnapshot:
        return cls.from_dict(_loads(text))


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
    """Copia y congela mappings y secuencias anidadas."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_frozen(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"Valor no serializable en el contexto: {type(value).__name__}")


def _check_schema_version(data: Mapping[str, Any]) -> None:
    version = data.get("schema_version", AGENT_SCHEMA_VERSION)
    if version != AGENT_SCHEMA_VERSION:
        raise ValueError(f"Versión de esquema no soportada: {version}")


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
    value = data.get(name, default)
    return _int_value(value, name)


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


def _str_mapping(value: object, name: str) -> dict[str, str]:
    data = _json_mapping(value, name)
    if any(not isinstance(item, str) for item in data.values()):
        raise ValueError(f"{name} debe mapear a textos")
    return {str(key): item for key, item in data.items()}


__all__ = [
    "AGENT_SCHEMA_VERSION",
    "AgentMessage",
    "AgentSnapshot",
    "CallOutcome",
    "ContextKind",
    "ContextPayload",
    "FallbackReason",
    "LLMHealth",
    "LLMMessage",
    "LLMReply",
    "LLMRequest",
    "LLMResponse",
    "LLMStatus",
    "MessageSource",
    "ValidationResult",
]
