"""Suite de evaluación de respuestas del LLM (Fase 6, L11).

Repite los mismos contextos contra un modelo o contra respuestas grabadas y
publica cuántos casos pasan cada regla del validador. No fija umbrales de
aceptación ni compara modelos: solo mide y señala la regla y el texto de cada
fallo para poder repetir la medición.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from ..config import AgentConfig, LLMConfig
from ..events.types import EventPriority
from .llm_client import FakeLLMClient, LLMClient
from .prompt_registry import PromptRegistry
from .types import ContextKind, ContextPayload, LLMRequest, LLMResponse, wire_reply_schema
from .validator import ResponseValidator, ValidationIssue

EVAL_SCHEMA_VERSION = 2

# Reglas del informe: agrupan los códigos del validador y los propios de la suite.
_RULE_GROUPS: dict[str, str] = {
    ValidationIssue.INVALID_JSON: "schema",
    ValidationIssue.SCHEMA_MISMATCH: "schema",
    ValidationIssue.LANGUAGE_MISMATCH: "language",
    ValidationIssue.PRIORITY_MISMATCH: "priority",
    ValidationIssue.UNKNOWN_FACT: "used_facts",
    ValidationIssue.UNKNOWN_NUMBER: "numbers",
    ValidationIssue.UNIT_MISMATCH: "numbers",
    ValidationIssue.TOO_LONG: "length",
    ValidationIssue.EMPTY_MESSAGE: "length",
    ValidationIssue.FORMAT_INVALID: "format",
    ValidationIssue.IMPERATIVE_PHRASE: "imperative",
    ValidationIssue.REPEATED_MESSAGE: "repetition",
    "required_fact": "used_facts",
    "forbidden_number": "numbers",
    "response_error": "fallback",
}

_METRIC_RULES = ("schema", "numbers", "length", "priority", "language", "used_facts")


@dataclass(frozen=True, slots=True)
class EvalCase:
    """Un contexto con lo que la respuesta debe cumplir, y su respuesta grabada."""

    case_id: str
    context: ContextPayload
    required_facts: tuple[str, ...] = ()
    forbidden_numbers: tuple[str, ...] = ()
    language: str | None = None
    max_words: int | None = None
    max_chars: int | None = None
    recorded_response: str | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id no puede estar vacío")
        object.__setattr__(self, "required_facts", tuple(self.required_facts))
        object.__setattr__(self, "forbidden_numbers", tuple(self.forbidden_numbers))


def load_cases(directory: Path) -> tuple[EvalCase, ...]:
    """Carga los casos ``*.json`` de un directorio, en orden de nombre."""
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise ValueError(f"No hay casos de evaluación en {directory}.")
    return tuple(_load_case(path) for path in paths)


def _load_case(path: Path) -> EvalCase:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Caso de evaluación inválido {path.name}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError(f"Caso de evaluación inválido {path.name}: se espera un objeto JSON.")
    context = data.get("context")
    if not isinstance(context, Mapping):
        raise ValueError(f"Caso de evaluación inválido {path.name}: falta context.")
    try:
        payload = ContextPayload.from_dict(context)
        return EvalCase(
            case_id=str(data.get("id") or path.stem),
            context=payload,
            required_facts=_str_tuple(data.get("required_facts"), "required_facts", path),
            forbidden_numbers=_str_tuple(data.get("forbidden_numbers"), "forbidden_numbers", path),
            language=_optional_text(data.get("language"), "language", path),
            max_words=_optional_int(data.get("max_words"), "max_words", path),
            max_chars=_optional_int(data.get("max_chars"), "max_chars", path),
            recorded_response=_optional_text(
                data.get("recorded_response"), "recorded_response", path
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Caso de evaluación inválido {path.name}: {exc}") from exc


def _str_tuple(value: object, name: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(
            f"Caso de evaluación inválido {path.name}: {name} debe ser una lista de textos."
        )
    return tuple(value)


def _optional_text(value: object, name: str, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Caso de evaluación inválido {path.name}: {name} debe ser un texto no vacío."
        )
    return value


def _optional_int(value: object, name: str, path: Path) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(
            f"Caso de evaluación inválido {path.name}: {name} debe ser un entero positivo."
        )
    return value


def cases_from_calls(calls: Iterable[object]) -> tuple[EvalCase, ...]:
    """Convierte llamadas guardadas (``agent export``) en casos reproducibles."""
    cases: list[EvalCase] = []
    for call in calls:
        context_json = getattr(call, "context_json", None)
        if not context_json:
            continue
        try:
            context = ContextPayload.from_json(context_json)
        except ValueError:
            continue
        seq = getattr(call, "seq", len(cases) + 1)
        cases.append(
            EvalCase(
                case_id=f"session-{seq}",
                context=context,
                recorded_response=getattr(call, "response_text", None),
            )
        )
    return tuple(cases)


def replay_client(cases: Sequence[EvalCase]) -> FakeLLMClient | None:
    """Cliente simulado que repite las respuestas grabadas; ``None`` si falta alguna."""
    if not cases or any(case.recorded_response is None for case in cases):
        return None
    return FakeLLMClient([LLMResponse(text=case.recorded_response or "") for case in cases])


def run_evaluation(
    client: LLMClient,
    cases: Sequence[EvalCase],
    llm: LLMConfig,
    *,
    agent: AgentConfig | None = None,
    registry: PromptRegistry | None = None,
) -> dict[str, Any]:
    """Ejecuta los casos y devuelve un informe determinista con tasas y latencias."""
    agent_config = agent if agent is not None else AgentConfig()
    prompt_set = registry if registry is not None else PromptRegistry.load(agent_config.language)
    results = [_evaluate_case(client, case, llm, agent_config, prompt_set) for case in cases]
    metrics: dict[str, Any] = {
        f"{rule}_valid_rate": _rate(
            sum(1 for result in results if rule not in result["failed_rules"]), len(results)
        )
        for rule in _METRIC_RULES
    }
    metrics["fallback_rate"] = _rate(
        sum(1 for result in results if result["fallback"]), len(results)
    )
    metrics["latency_s"] = _percentiles(
        [result["latency_s"] for result in results if result["latency_s"] is not None]
    )
    metrics["by_priority"] = {}
    for priority in ("critical", "strategic", "informational"):
        priority_results = [result for result in results if result["priority"] == priority]
        metrics["by_priority"][priority] = {
            "case_count": len(priority_results),
            "valid_rate": _rate(
                sum(1 for result in priority_results if result["valid"]), len(priority_results)
            ),
            "fallback_rate": _rate(
                sum(1 for result in priority_results if result["fallback"]), len(priority_results)
            ),
            "latency_s": _percentiles(
                [
                    result["latency_s"]
                    for result in priority_results
                    if result["latency_s"] is not None
                ]
            ),
        }
    return {
        "schema_version": EVAL_SCHEMA_VERSION,
        "model": llm.model,
        "case_count": len(results),
        "metrics": metrics,
        "cases": results,
    }


def _evaluate_case(
    client: LLMClient,
    case: EvalCase,
    llm: LLMConfig,
    agent: AgentConfig,
    prompt_set: PromptRegistry,
) -> dict[str, Any]:
    priority = _context_priority(case.context)
    rendered = prompt_set.render(ContextKind.ALERT, case.context)
    request = LLMRequest(
        model=llm.model,
        messages=tuple(rendered.messages),
        temperature=llm.temperature,
        top_p=llm.top_p,
        max_tokens=llm.max_tokens,
        seed=llm.seed,
        stream=False,
        response_format=_response_format(llm),
        extra_body=dict(llm.extra_body),
        timeout_s=agent.timeout_for(priority.value),
    )
    try:
        response = client.generate(request)
    except Exception as exc:  # el cliente real puede lanzar; se cuenta como respaldo
        response = LLMResponse(error=f"{type(exc).__name__}: {exc}", total_s=0.0)
    text = response.text
    failures: list[dict[str, str]] = []
    if response.error is not None:
        failures.append(_failure("response_error", text or response.error))
    result = ResponseValidator(_case_agent(agent, case)).validate(text, case.context)
    for code in result.errors:
        failures.append(_failure(str(code), text))
    reply = result.reply
    if reply is not None:
        for fact in case.required_facts:
            if fact not in reply.used_facts:
                failures.append(_failure("required_fact", text, detail=fact))
        for number in case.forbidden_numbers:
            if number in reply.message:
                failures.append(_failure("forbidden_number", text, detail=number))
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for failure in failures:
        unique.setdefault((failure["rule"], failure["text"], failure["detail"]), failure)
    failures = list(unique.values())
    failed_rules = sorted({_RULE_GROUPS.get(item["rule"], item["rule"]) for item in failures})
    return {
        "id": case.case_id,
        "message_key": _message_key(case.context),
        "priority": priority.value,
        "text": text,
        "finish_reason": response.finish_reason,
        "reasoning_content": response.reasoning_content,
        "fallback": response.error is not None or not result.valid,
        "valid": not failures,
        "failed_rules": failed_rules,
        "failures": failures,
        "latency_s": None if response.total_s is None else round(response.total_s, 4),
    }


def _failure(rule: str, text: str, *, detail: str = "") -> dict[str, str]:
    return {"rule": rule, "text": text, "detail": detail}


def _case_agent(agent: AgentConfig, case: EvalCase) -> AgentConfig:
    updates: dict[str, Any] = {}
    if case.language is not None:
        updates["language"] = case.language
    if case.max_words is not None:
        updates["max_words"] = case.max_words
    if case.max_chars is not None:
        updates["max_chars"] = case.max_chars
    return agent if not updates else agent.model_copy(update=updates)


def _response_format(llm: LLMConfig) -> dict[str, Any] | None:
    if llm.structured_output == "none":
        return None
    return {
        "json_schema": {
            "name": "llm_reply",
            "schema": wire_reply_schema(),
            "strict": True,
        }
    }


def _context_priority(context: ContextPayload) -> EventPriority:
    alert = context.alert or {}
    try:
        return EventPriority(str(alert.get("priority")))
    except ValueError:
        return EventPriority.INFORMATIONAL


def _message_key(context: ContextPayload) -> str | None:
    alert = context.alert or {}
    value = alert.get("message_key")
    return None if value is None else str(value)


def _rate(count: int, total: int) -> float | None:
    return None if total == 0 else round(count / total, 4)


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None}
    ordered = sorted(values)
    return {
        "p50": round(median(ordered), 4),
        "p95": round(_percentile(ordered, 0.95), 4),
    }


def _percentile(ordered: list[float], percentile: float) -> float:
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


__all__ = [
    "EVAL_SCHEMA_VERSION",
    "EvalCase",
    "cases_from_calls",
    "load_cases",
    "replay_client",
    "run_evaluation",
]
