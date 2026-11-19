"""Cliente HTTP compatible con OpenAI y clientes de prueba (Fase 6).

El cliente no interpreta el contenido: solo transporta la petición, mide tiempos
y clasifica errores con códigos estables de :class:`FallbackReason`. La clave de
API nunca aparece en los errores devueltos.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Protocol

import httpx

from ..config import LLMConfig
from .types import (
    FallbackReason,
    LLMHealth,
    LLMRequest,
    LLMResponse,
    LLMStatus,
)

RECORDED_RESPONSE_MISSING = "recorded_response_missing"

_STRUCTURED_LEVELS: dict[str, tuple[str, ...]] = {
    "auto": ("json_schema", "json_object", "none"),
    "json_schema": ("json_schema", "json_object", "none"),
    "json_object": ("json_object", "none"),
    "none": ("none",),
}

_DEGRADE_STATUSES = frozenset({400, 422})


class LLMClient(Protocol):
    """Contrato del cliente: una generación síncrona y una comprobación de salud."""

    def generate(
        self, request: LLMRequest, *, cancel: threading.Event | None = None
    ) -> LLMResponse: ...

    def health(self) -> LLMHealth: ...


class OpenAICompatibleClient:
    """Cliente para cualquier ``/chat/completions`` compatible con OpenAI.

    ``transport`` permite inyectar ``httpx.MockTransport`` en las pruebas.
    """

    def __init__(self, config: LLMConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self._config = config
        self._transport = transport
        # Nivel negociado: recuerda la degradación para no repetir el 400.
        self._level_index = 0

    def generate(
        self, request: LLMRequest, *, cancel: threading.Event | None = None
    ) -> LLMResponse:
        if cancel is not None and cancel.is_set():
            return _error(FallbackReason.CANCELLED)
        if not self._config.enabled:
            return _error(FallbackReason.LLM_DISABLED)
        if request.response_format is None:
            response, _ = self._call(request, "none", False, cancel)
            return response
        while True:
            mode = _STRUCTURED_LEVELS[self._config.structured_output][self._level_index]
            response, retry = self._call(request, mode, mode == "none", cancel)
            if not retry or self._level_index + 1 >= len(self._levels):
                return response
            self._level_index += 1

    @property
    def _levels(self) -> tuple[str, ...]:
        return _STRUCTURED_LEVELS[self._config.structured_output]

    def _call(
        self,
        request: LLMRequest,
        mode: str,
        plain_text: bool,
        cancel: threading.Event | None,
    ) -> tuple[LLMResponse, bool]:
        """Ejecuta una petición; ``retry`` indica que toca degradar el formato."""
        payload = self._payload(request, mode)
        timeout = httpx.Timeout(request.timeout_s, connect=self._config.connect_timeout_s)
        started = time.perf_counter()
        try:
            with (
                httpx.Client(transport=self._transport, follow_redirects=False) as client,
                _cancel_watcher(cancel, client.close),
                client.stream(
                    "POST",
                    f"{self._config.endpoint}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                    timeout=timeout,
                ) as response,
            ):
                if response.status_code in _DEGRADE_STATUSES and mode != "none":
                    return _error(FallbackReason.HTTP_ERROR), True
                if not 200 <= response.status_code < 300:
                    return _error(FallbackReason.HTTP_ERROR), False
                if request.stream:
                    return self._read_stream(response, started, mode, plain_text, cancel), False
                return self._read_message(response, started, mode, plain_text, cancel), False
        except httpx.TimeoutException:
            return _error(FallbackReason.TIMEOUT), False
        except httpx.HTTPError:
            # Cerrar la conexión desde el watcher también llega aquí.
            code = FallbackReason.CANCELLED if _is_set(cancel) else FallbackReason.UNAVAILABLE
            return _error(code), False
        except Exception:
            # Cualquier fallo del transporte queda clasificado, nunca sin capturar.
            code = FallbackReason.CANCELLED if _is_set(cancel) else FallbackReason.UNAVAILABLE
            return _error(code), False

    def _payload(self, request: LLMRequest, mode: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message.to_dict() for message in request.messages],
            "stream": request.stream,
        }
        for name in ("temperature", "top_p", "max_tokens", "seed"):
            value = getattr(request, name)
            if value is not None:
                payload[name] = value
        if mode != "none" and request.response_format is not None:
            if mode == "json_schema":
                payload["response_format"] = _plain(
                    {**request.response_format, "type": "json_schema"}
                )
            else:
                payload["response_format"] = {"type": "json_object"}
        payload.update(_plain(request.extra_body))
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self._config.resolved_api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _read_stream(
        self,
        response: httpx.Response,
        started: float,
        mode: str,
        plain_text: bool,
        cancel: threading.Event | None,
    ) -> LLMResponse:
        parts: list[str] = []
        ttft_s: float | None = None
        finish_reason: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        model_id: str | None = None
        done = False
        try:
            for line in response.iter_lines():
                if _is_set(cancel):
                    return _error(FallbackReason.CANCELLED)
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    return _error(FallbackReason.INVALID_JSON)
                if not isinstance(chunk, dict):
                    return _error(FallbackReason.INVALID_JSON)
                model_id = _opt_text(chunk.get("model")) or model_id
                usage = chunk.get("usage")
                if isinstance(usage, Mapping):
                    prompt_tokens = _opt_int(usage.get("prompt_tokens")) or prompt_tokens
                    completion_tokens = (
                        _opt_int(usage.get("completion_tokens")) or completion_tokens
                    )
                choices = chunk.get("choices")
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if not isinstance(choice, Mapping):
                        continue
                    delta = choice.get("delta")
                    if isinstance(delta, Mapping):
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            if ttft_s is None:
                                ttft_s = time.perf_counter() - started
                            parts.append(content)
                    finish_reason = _opt_text(choice.get("finish_reason")) or finish_reason
        except httpx.HTTPError:
            if _is_set(cancel):
                return _error(FallbackReason.CANCELLED)
            return _error(FallbackReason.INVALID_JSON)
        total_s = time.perf_counter() - started
        text = "".join(parts)
        if finish_reason is None and not done:
            # El servidor cerró la conexión antes de terminar la respuesta.
            return LLMResponse(
                text=text, ttft_s=ttft_s, total_s=total_s, error=FallbackReason.INVALID_JSON.value
            )
        return LLMResponse(
            text=_final_text(text, mode, plain_text),
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            ttft_s=ttft_s,
            total_s=total_s,
            model_id=model_id,
            structured_mode=mode,
        )

    def _read_message(
        self,
        response: httpx.Response,
        started: float,
        mode: str,
        plain_text: bool,
        cancel: threading.Event | None,
    ) -> LLMResponse:
        if _is_set(cancel):
            return _error(FallbackReason.CANCELLED)
        try:
            data = json.loads(response.read())
        except (ValueError, httpx.HTTPError):
            return _error(FallbackReason.INVALID_JSON)
        total_s = time.perf_counter() - started
        if not isinstance(data, dict):
            return _error(FallbackReason.INVALID_JSON)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            return _error(FallbackReason.INVALID_JSON)
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            return _error(FallbackReason.INVALID_JSON)
        content = message.get("content")
        usage = data.get("usage")
        return LLMResponse(
            text=_final_text(content if isinstance(content, str) else "", mode, plain_text),
            finish_reason=_opt_text(choice.get("finish_reason")),
            prompt_tokens=_opt_int(usage.get("prompt_tokens"))
            if isinstance(usage, Mapping)
            else None,
            completion_tokens=(
                _opt_int(usage.get("completion_tokens")) if isinstance(usage, Mapping) else None
            ),
            total_s=total_s,
            model_id=_opt_text(data.get("model")),
            structured_mode=mode,
        )

    def health(self) -> LLMHealth:
        config = self._config
        model = config.model or None
        if not config.enabled:
            return LLMHealth(status=LLMStatus.DISABLED, model=model)
        checked_at = time.time()
        started = time.perf_counter()
        try:
            with httpx.Client(transport=self._transport, follow_redirects=False) as client:
                response = client.get(
                    f"{config.endpoint}/models",
                    headers=self._headers(),
                    timeout=httpx.Timeout(config.connect_timeout_s),
                )
        except httpx.TimeoutException:
            return _health(LLMStatus.UNAVAILABLE, model, checked_at, None, "timeout")
        except httpx.HTTPError:
            return _health(LLMStatus.UNAVAILABLE, model, checked_at, None, "unavailable")
        latency_s = time.perf_counter() - started
        if response.status_code >= 400:
            return _health(LLMStatus.UNAVAILABLE, model, checked_at, latency_s, "http_error")
        try:
            data = response.json()
        except ValueError:
            return _health(LLMStatus.UNAVAILABLE, model, checked_at, latency_s, "invalid_json")
        ids = _model_ids(data)
        if ids is None:
            return _health(LLMStatus.UNAVAILABLE, model, checked_at, latency_s, "invalid_json")
        status = LLMStatus.AVAILABLE
        error: str | None = None
        if model is not None and model not in ids:
            status, error = LLMStatus.UNAVAILABLE, "model_not_found"
        return _health(status, model, checked_at, latency_s, error)


class FakeLLMClient:
    """Respuestas programadas para pruebas; los retardos se simulan sin dormir."""

    def __init__(
        self,
        responses: Sequence[LLMResponse | str | Exception] = (),
        *,
        delay_s: float = 0.0,
        health: LLMHealth | None = None,
    ) -> None:
        self._responses = [
            LLMResponse(text=item) if isinstance(item, str) else item for item in responses
        ]
        self.delay_s = delay_s
        self._health = health or LLMHealth(status=LLMStatus.AVAILABLE, model="fake")
        self._index = 0
        self.requests: list[LLMRequest] = []

    def generate(
        self, request: LLMRequest, *, cancel: threading.Event | None = None
    ) -> LLMResponse:
        self.requests.append(request)
        if cancel is not None and cancel.is_set():
            return _error(FallbackReason.CANCELLED)
        if not self._responses:
            return _error(FallbackReason.UNAVAILABLE)
        # La última respuesta se repite: los bucles del comunicador son deterministas.
        item = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        if self.delay_s:
            return replace(item, ttft_s=self.delay_s, total_s=self.delay_s)
        return item

    def health(self) -> LLMHealth:
        return self._health


class RecordedLLMClient:
    """Devuelve respuestas guardadas según la huella de la petición."""

    def __init__(self, records: Mapping[str, LLMResponse]) -> None:
        self._records = dict(records)
        # La huella cubre modelo y mensajes, donde viaja el contexto.
        self._health = LLMHealth(status=LLMStatus.AVAILABLE)

    def generate(
        self, request: LLMRequest, *, cancel: threading.Event | None = None
    ) -> LLMResponse:
        if cancel is not None and cancel.is_set():
            return _error(FallbackReason.CANCELLED)
        response = self._records.get(request_fingerprint(request))
        if response is None:
            return LLMResponse(error=RECORDED_RESPONSE_MISSING)
        return response

    def health(self) -> LLMHealth:
        return self._health


def request_fingerprint(request: LLMRequest) -> str:
    """Huella estable de la petición, sin tiempos ni modo de transporte."""
    data = request.to_dict()
    for name in ("timeout_s", "stream"):
        data.pop(name)
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@contextmanager
def _cancel_watcher(cancel: threading.Event | None, close: Callable[[], None]) -> Iterator[None]:
    """Cierra la conexión en cuanto se señala la cancelación."""
    if cancel is None:
        yield
        return
    stopped = threading.Event()

    def watch() -> None:
        # shortcut: sondeo de 50 ms; basta para una radio que se cancela en segundos.
        while not stopped.wait(0.05):
            if cancel.is_set():
                close()
                return

    thread = threading.Thread(target=watch, daemon=True, name="llm-cancel")
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1.0)


def _final_text(text: str, mode: str, plain_text: bool) -> str:
    if not plain_text:
        return text
    # Modo texto: rescatar el objeto JSON si el modelo lo envolvió en prosa.
    return _extract_json(text) or text


def _extract_json(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        loaded = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(loaded, dict):
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


def _plain(value: object) -> Any:
    """Copia mappings y secuencias congelados a tipos JSON nativos."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _model_ids(data: object) -> set[str] | None:
    if not isinstance(data, Mapping):
        return None
    items = data.get("data")
    if not isinstance(items, list):
        return None
    ids = {
        item["id"]
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    return ids


def _opt_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _is_set(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _error(code: FallbackReason) -> LLMResponse:
    return LLMResponse(error=code.value)


def _health(
    status: LLMStatus,
    model: str | None,
    checked_at: float,
    latency_s: float | None,
    error: str | None,
) -> LLMHealth:
    return LLMHealth(
        status=status,
        model=model,
        latency_s=latency_s,
        checked_at=checked_at,
        error=error,
    )


__all__ = [
    "RECORDED_RESPONSE_MISSING",
    "FakeLLMClient",
    "LLMClient",
    "OpenAICompatibleClient",
    "RecordedLLMClient",
    "request_fingerprint",
]
