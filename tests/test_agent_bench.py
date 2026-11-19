from __future__ import annotations

import json
from typing import Any

import httpx

from f1_strategist.agent.bench import report_json, run_benchmark, run_check
from f1_strategist.agent.llm_client import FakeLLMClient, OpenAICompatibleClient
from f1_strategist.agent.types import LLMHealth, LLMResponse, LLMStatus
from f1_strategist.config import LLMConfig


def _reply(priority: str) -> str:
    return json.dumps(
        {
            "should_speak": True,
            "priority": priority,
            "language": "es",
            "message": "OK",
            "reason": "",
            "used_facts": [],
        }
    )


def test_check_uses_fake_client_and_reports_capabilities() -> None:
    client = FakeLLMClient(
        [
            "OK",
            LLMResponse(text=_reply("informational"), structured_mode="json_schema"),
            LLMResponse(text="OK", ttft_s=0.1, total_s=0.2),
        ],
        health=LLMHealth(status=LLMStatus.AVAILABLE, model="test-model"),
    )
    config = LLMConfig(enabled=True, model="small-3B-model")

    report = run_check(client, config)

    assert report["ok"] is True
    assert report["model_available"] is True
    assert report["structured_output"] is True
    assert report["streaming"] is True
    assert report["warnings"] == ["small_model"]
    assert [request.stream for request in client.requests] == [False, False, True]


def test_check_reports_endpoint_failure_without_throwing() -> None:
    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    config = LLMConfig(enabled=True, model="test-model")
    client = OpenAICompatibleClient(config, transport=httpx.MockTransport(unavailable))

    report = run_check(client, config)

    assert report["connectivity"] == LLMStatus.UNAVAILABLE.value
    assert report["model_available"] is False
    assert report["ok"] is False


def test_check_works_with_openai_mock_transport_and_structured_streaming() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        payload: dict[str, Any] = json.loads(request.content)
        if payload["stream"]:
            content = (
                'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, content=content, headers={"content-type": "text/event-stream"}
            )
        response_text = _reply("informational") if "response_format" in payload else "OK"
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": response_text}, "finish_reason": "stop"}],
            },
        )

    config = LLMConfig(enabled=True, model="test-model")
    client = OpenAICompatibleClient(config, transport=httpx.MockTransport(respond))

    report = run_check(client, config)

    assert report["ok"] is True
    assert report["structured_output"] is True
    assert report["streaming"] is True


def test_benchmark_reports_percentiles_validity_and_stable_json() -> None:
    client = FakeLLMClient(
        [
            LLMResponse(
                text=_reply("critical"),
                ttft_s=0.2,
                total_s=1.0,
                completion_tokens=10,
            ),
            LLMResponse(
                text=_reply("strategic"),
                ttft_s=0.4,
                total_s=2.0,
                completion_tokens=30,
            ),
        ]
    )

    report = run_benchmark(
        client,
        LLMConfig(enabled=True, model="test-model"),
        runs=1,
        warmup=0,
        note="game running",
    )

    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["valid_response_rate"] == 1.0
    assert metrics["ttft_s"] == {"p50": 0.3, "p95": 0.39}
    assert metrics["total_s"] == {"p50": 1.5, "p95": 1.95}
    assert metrics["tokens_per_second"] == {"p50": 12.5, "p95": 14.75}
    assert json.loads(report_json(report)) == report
    assert report_json(report) == report_json(report)
    assert len(client.requests) == 2
