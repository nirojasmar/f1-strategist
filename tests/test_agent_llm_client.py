from __future__ import annotations

import json
import threading
from typing import Any

import httpx
import pytest

from f1_strategist.agent.llm_client import (
    RECORDED_RESPONSE_MISSING,
    FakeLLMClient,
    OpenAICompatibleClient,
    RecordedLLMClient,
    request_fingerprint,
)
from f1_strategist.agent.types import LLMHealth, LLMMessage, LLMRequest, LLMResponse, LLMStatus
from f1_strategist.config import LLMConfig

SCHEMA = {"type": "json_schema", "json_schema": {"name": "reply", "schema": {"type": "object"}}}


def _config(**overrides: Any) -> LLMConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "endpoint": "http://llm.test/v1",
        "model": "test-model",
        "stream": False,
    }
    values.update(overrides)
    return LLMConfig(**values)


def _request(**overrides: Any) -> LLMRequest:
    values: dict[str, Any] = {
        "model": "test-model",
        "messages": (LLMMessage(role="system", content="eres un ingeniero"),),
        "temperature": 0.3,
        "max_tokens": 120,
        "timeout_s": 5.0,
        "response_format": SCHEMA,
    }
    values.update(overrides)
    return LLMRequest(**values)


def _completion(text: str = '{"message":"ok"}') -> dict[str, Any]:
    return {
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def _sse(*events: str) -> bytes:
    lines = [f"data: {event}" for event in events]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _client(handler: Any, **overrides: Any) -> tuple[OpenAICompatibleClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return OpenAICompatibleClient(
        _config(**overrides), transport=httpx.MockTransport(transport_handler)
    ), seen


def test_non_streaming_request_and_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion())

    client, seen = _client(handler)
    response = client.generate(_request())

    assert response.text == '{"message":"ok"}'
    assert response.structured_mode == "json_schema"
    assert (response.prompt_tokens, response.completion_tokens) == (11, 7)
    assert response.total_s is not None
    assert response.error is None
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/chat/completions"
    assert body["response_format"]["type"] == "json_schema"
    assert body["temperature"] == 0.3
    assert "authorization" not in seen[0].headers


def test_no_api_key_and_no_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://elsewhere.test/v1"})

    client, seen = _client(handler)
    response = client.generate(_request())

    assert response.error == "http_error"
    assert len(seen) == 1


def test_streaming_measures_ttft_and_collects_text() -> None:
    body = _sse(
        json.dumps({"model": "test-model", "choices": [{"delta": {"content": '{"mess'}}]}),
        json.dumps({"choices": [{"delta": {"content": 'age":"ok"}'}}]}),
        json.dumps(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            }
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client, _ = _client(handler)
    response = client.generate(_request(stream=True))

    assert response.text == '{"message":"ok"}'
    assert response.finish_reason == "stop"
    assert response.ttft_s is not None and response.total_s is not None
    assert response.ttft_s <= response.total_s
    assert (response.prompt_tokens, response.completion_tokens) == (3, 4)


def test_streaming_broken_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse("{no es json"))

    client, _ = _client(handler)
    response = client.generate(_request(stream=True))

    assert response.error == "invalid_json"


def test_streaming_closed_midway() -> None:
    body = _sse(json.dumps({"choices": [{"delta": {"content": "a medias"}}]})).replace(
        b"data: [DONE]", b""
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client, _ = _client(handler)
    response = client.generate(_request(stream=True))

    assert response.error == "invalid_json"


def test_streaming_done_marker_without_finish_reason() -> None:
    body = _sse(json.dumps({"choices": [{"delta": {"content": "hola"}}]}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client, _ = _client(handler)
    response = client.generate(_request(stream=True))

    assert response.error is None
    assert response.text == "hola"


def test_timeout_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client, _ = _client(handler)
    assert client.generate(_request()).error == "timeout"


def test_connection_error_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, _ = _client(handler)
    assert client.generate(_request()).error == "unavailable"


def test_structured_output_degrades_to_json_object() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(400, json={"error": "response_format no soportado"})
        return httpx.Response(200, json=_completion())

    client, _ = _client(handler)
    response = client.generate(_request())

    assert response.error is None
    assert response.structured_mode == "json_object"
    assert payloads[1]["response_format"] == {"type": "json_object"}


def test_structured_output_degrades_to_text_and_extracts_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.content.find(b"response_format") != -1:
            return httpx.Response(400, json={"error": "no"})
        return httpx.Response(200, json=_completion('Claro: {"message":"ok"} espero que sirva'))

    client, _ = _client(handler)
    response = client.generate(_request())

    assert response.error is None
    assert response.structured_mode == "none"
    assert response.text == '{"message":"ok"}'


def test_api_key_sent_but_never_in_errors() -> None:
    key = "sk-secreto-123"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": f"clave inválida: {key}"})

    client, seen = _client(handler, api_key=key)
    response = client.generate(_request())

    assert seen[0].headers["authorization"] == f"Bearer {key}"
    assert response.error == "http_error"
    assert key not in (response.error or "")


def test_cancelled_before_call_skips_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no debe llamarse")

    client, seen = _client(handler)
    cancel = threading.Event()
    cancel.set()

    assert client.generate(_request(), cancel=cancel).error == "cancelled"
    assert seen == []


def test_cancel_watcher_closes_connection() -> None:
    from f1_strategist.agent.llm_client import _cancel_watcher

    cancel = threading.Event()
    closed = threading.Event()
    with _cancel_watcher(cancel, closed.set):
        cancel.set()
        assert closed.wait(1.0)


def test_health_available_and_model_missing() -> None:
    def available(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    client, _ = _client(available)
    health = client.health()
    assert health.status is LLMStatus.AVAILABLE
    assert health.model == "test-model"
    assert health.latency_s is not None and health.checked_at is not None

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "otro"}]})

    client, _ = _client(missing)
    health = client.health()
    assert health.status is LLMStatus.UNAVAILABLE
    assert health.error == "model_not_found"


def test_health_timeout_and_disabled() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("lento", request=request)

    client, _ = _client(slow)
    assert client.health().status is LLMStatus.UNAVAILABLE

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no debe llamarse")

    client, seen = _client(unreachable, enabled=False, model="")
    health = client.health()
    assert health.status is LLMStatus.DISABLED
    assert seen == []


def test_disabled_client_returns_llm_disabled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no debe llamarse")

    client, _ = _client(handler, enabled=False, model="")
    assert client.generate(_request()).error == "llm_disabled"


def test_hidden_api_key_never_leaks_from_config() -> None:
    config = _config(api_key="sk-secreto-123")
    assert "sk-secreto-123" not in repr(config)
    assert "sk-secreto-123" not in config.model_dump_json()


def test_fake_client_scripts_responses_delays_and_failures() -> None:
    client = FakeLLMClient(["uno", LLMResponse(error="timeout")], delay_s=1.25)
    request = _request()

    first = client.generate(request)
    assert first.text == "uno"
    assert first.ttft_s == 1.25 and first.total_s == 1.25
    assert client.generate(request).error == "timeout"
    assert client.generate(request).error == "timeout"
    assert len(client.requests) == 3
    assert client.health().status is LLMStatus.AVAILABLE

    failing = FakeLLMClient([RuntimeError("roto")])
    with pytest.raises(RuntimeError):
        failing.generate(request)

    assert FakeLLMClient().generate(request).error == "unavailable"


def test_recorded_client_uses_request_fingerprint() -> None:
    request = _request()
    stored = LLMResponse(text="grabada", structured_mode="json_schema")
    client = RecordedLLMClient({request_fingerprint(request): stored})

    assert client.generate(request).text == "grabada"
    other = _request(messages=(LLMMessage(role="user", content="otra"),))
    assert client.generate(other).error == RECORDED_RESPONSE_MISSING


def test_recorded_client_ignores_transport_details() -> None:
    stored = LLMResponse(text="grabada")
    request = _request(stream=False, timeout_s=5.0)
    client = RecordedLLMClient({request_fingerprint(request): stored})

    assert client.generate(_request(stream=True, timeout_s=9.0)).text == "grabada"
    assert isinstance(client.health(), LLMHealth)
    assert client.health().status is LLMStatus.AVAILABLE
