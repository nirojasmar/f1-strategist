"""Diagnóstico y medición de clientes LLM (Fase 6)."""

from __future__ import annotations

import ipaddress
import json
import math
import platform
import re
from statistics import median
from urllib.parse import urlparse

from ..config import LLMConfig
from .llm_client import LLMClient
from .types import LLMMessage, LLMReply, LLMRequest, LLMResponse, LLMStatus

_RESPONSE_FORMAT: dict[str, object] = {
    "json_schema": {
        "name": "llm_check",
        "schema": LLMReply.model_json_schema(),
        "strict": True,
    }
}

_EXAMPLES = (
    (
        "short_alert",
        "Alerta: margen de combustible bajo. Vuelta 18, margen calculado 0.4 vueltas.",
        "critical",
    ),
    (
        "strategic_alert",
        (
            "Alerta: ventana de parada abierta. Vuelta 24, quedan 12 vueltas. "
            "Estrategia: parar en la vuelta 25, neumático medio, ganancia prevista 2.4 s, "
            "confianza media."
        ),
        "strategic",
    ),
)


def diagnostic_config(
    config: LLMConfig, *, endpoint: str | None = None, model: str | None = None
) -> LLMConfig:
    """Activa el cliente para diagnóstico sin exponer ni sustituir su clave."""
    selected_model = model if model is not None else config.model
    if not selected_model.strip():
        raise ValueError("Configura llm.model o indica --model para diagnosticar el endpoint.")
    values = config.model_dump()
    values.update(
        enabled=True,
        endpoint=endpoint if endpoint is not None else config.endpoint,
        model=selected_model,
    )
    return LLMConfig.model_validate(values)


def run_check(client: LLMClient, config: LLMConfig) -> dict[str, object]:
    """Prueba salud, generación simple, salida estructurada y streaming."""
    health = client.health()
    simple = client.generate(_request(config, "Escribe exactamente: OK.", stream=False))
    structured = client.generate(
        _request(
            config,
            "Devuelve una respuesta JSON válida con priority=informational y language=es.",
            stream=False,
            response_format=_RESPONSE_FORMAT,
        )
    )
    streaming = client.generate(_request(config, "Escribe exactamente: OK.", stream=True))

    simple_ok = _response_ok(simple)
    structured_ok = _response_ok(structured) and structured.structured_mode in {
        "json_schema",
        "json_object",
    }
    streaming_ok = _response_ok(streaming) and streaming.ttft_s is not None
    warnings: list[str] = []
    if _small_model(config.model):
        warnings.append("small_model")
    if not structured_ok:
        warnings.append("structured_output_unsupported")
    if not streaming_ok:
        warnings.append("streaming_unsupported")
    return {
        "model": config.model,
        "connectivity": health.status.value,
        "model_available": health.status is LLMStatus.AVAILABLE,
        "health_latency_s": health.latency_s,
        "simple_generation": simple_ok,
        "structured_output": structured_ok,
        "structured_mode": structured.structured_mode,
        "streaming": streaming_ok,
        "ttft_s": streaming.ttft_s,
        "total_s": streaming.total_s,
        "warnings": warnings,
        "ok": health.status is LLMStatus.AVAILABLE and simple_ok,
    }


def run_benchmark(
    client: LLMClient,
    config: LLMConfig,
    *,
    runs: int,
    warmup: int,
    note: str | None = None,
) -> dict[str, object]:
    """Mide dos contextos sintéticos y devuelve un informe JSON serializable."""
    samples: list[LLMResponse] = []
    valid_count = 0
    for _, context, priority in _EXAMPLES:
        response_format = _RESPONSE_FORMAT if config.structured_output != "none" else None
        request = _request(config, context, stream=True, response_format=response_format)
        for _ in range(warmup):
            client.generate(request)
        for _ in range(runs):
            response = client.generate(request)
            samples.append(response)
            valid_count += _valid_reply(response, priority)

    ttft = [response.ttft_s for response in samples if response.ttft_s is not None]
    totals = [response.total_s for response in samples if response.total_s is not None]
    rates = [
        response.completion_tokens / response.total_s
        for response in samples
        if response.completion_tokens is not None
        and response.total_s is not None
        and response.total_s > 0
    ]
    valid_rate = valid_count / len(samples) if samples else 0.0
    host, scope = _endpoint_host(config.endpoint)
    return {
        "schema_version": 1,
        "model": config.model,
        "runs_per_context": runs,
        "warmup_per_context": warmup,
        "sample_count": len(samples),
        "metrics": {
            "ttft_s": _percentiles(ttft),
            "total_s": _percentiles(totals),
            "tokens_per_second": _percentiles(rates),
            "valid_response_rate": round(valid_rate, 4),
            "valid_responses": valid_count,
        },
        "environment": {
            "system": platform.system(),
            "release": platform.release(),
            "endpoint_host": host,
            "endpoint_scope": scope,
        },
        "note": note,
    }


def report_json(report: dict[str, object]) -> str:
    """Serializa informes con orden de claves y formato estable."""
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _request(
    config: LLMConfig,
    context: str,
    *,
    stream: bool,
    response_format: dict[str, object] | None = None,
) -> LLMRequest:
    return LLMRequest(
        model=config.model,
        messages=(
            LLMMessage(
                role="system",
                content=(
                    "Eres ingeniero de carrera. Usa solo los datos proporcionados y responde "
                    "brevemente en español."
                ),
            ),
            LLMMessage(role="user", content=context),
        ),
        temperature=config.temperature,
        top_p=config.top_p,
        max_tokens=config.max_tokens,
        seed=config.seed,
        stream=stream,
        response_format=response_format,
        extra_body=config.extra_body,
        timeout_s=max(config.connect_timeout_s, 6.0),
    )


def _response_ok(response: LLMResponse) -> bool:
    return response.error is None and bool(response.text.strip())


def _valid_reply(response: LLMResponse, priority: str) -> int:
    if not _response_ok(response):
        return 0
    try:
        reply = LLMReply.model_validate_json(response.text)
    except ValueError:
        return 0
    return int(reply.language == "es" and reply.priority.value == priority)


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


def _endpoint_host(endpoint: str) -> tuple[str | None, str]:
    hostname = urlparse(endpoint).hostname
    if hostname is None:
        return None, "unknown"
    try:
        local = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        local = hostname.casefold() == "localhost"
    return hostname, "local" if local else "remote"


def _small_model(model: str) -> bool:
    if re.search(r"\b(?:tiny|small|mini|nano)\b", model, re.IGNORECASE):
        return True
    sizes = re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*b(?:$|[^\w])", model, re.IGNORECASE)
    return bool(sizes and float(sizes[-1]) < 7)


__all__ = ["diagnostic_config", "report_json", "run_benchmark", "run_check"]
