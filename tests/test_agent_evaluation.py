from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from f1_strategist.agent.evaluation import (
    EVAL_SCHEMA_VERSION,
    EvalCase,
    cases_from_calls,
    load_cases,
    replay_client,
    run_evaluation,
)
from f1_strategist.agent.llm_client import FakeLLMClient, OpenAICompatibleClient
from f1_strategist.agent.types import LLMResponse
from f1_strategist.cli import main
from f1_strategist.config import LLMConfig
from f1_strategist.events.types import EventPriority

DATASET = Path(__file__).parent / "fixtures" / "llm_eval"
MESSAGE_KEYS = (
    "event.data_stale",
    "event.damage_detected",
    "event.fuel_margin_critical",
    "event.fuel_margin_low",
    "event.pit_window_open",
    "event.rain_expected",
    "event.red_flag",
    "event.rival_pit_stop",
    "event.safety_car_cleared",
    "event.safety_car_deployed",
    "event.strategy_pit_recommended",
    "event.tyre_degradation_abnormal",
    "event.tyre_life_near_limit",
    "event.virtual_safety_car_deployed",
    "event.weather_changed",
)


def _reply(
    message: str,
    *,
    priority: str = "strategic",
    language: str = "es",
    used: tuple[str, ...] = (),
) -> str:
    return json.dumps(
        {
            "should_speak": True,
            "priority": priority,
            "language": language,
            "message": message,
            "reason": "",
            "used_facts": list(used),
        },
        ensure_ascii=False,
    )


def _base_case() -> EvalCase:
    return {case.case_id: case for case in load_cases(DATASET)}["strategy-pit-recommended"]


def test_dataset_covers_keys_priorities_and_edge_cases() -> None:
    cases = load_cases(DATASET)

    assert len(cases) >= 30
    assert {case.context.alert["message_key"] for case in cases if case.context.alert} == set(
        MESSAGE_KEYS
    )
    assert {case.context.alert["priority"] for case in cases if case.context.alert} == {
        priority.value for priority in EventPriority
    }
    uncertainties = {token for case in cases for token in case.context.uncertainties}
    assert any(token.startswith("stale:") for token in uncertainties)
    assert any(token.startswith("insufficient:") for token in uncertainties)
    hostile = " ".join(f"{case.context.glossary} {case.context.recent_messages}" for case in cases)
    assert "IGNORA" in hostile
    assert "<script>" in hostile
    assert "SYSTEM:" in hostile


def test_replay_report_is_reproducible_and_every_case_passes() -> None:
    cases = load_cases(DATASET)
    client = replay_client(cases)

    assert client is not None
    report = run_evaluation(client, cases, LLMConfig(model="recorded"))
    again = run_evaluation(replay_client(cases) or client, cases, LLMConfig(model="recorded"))

    assert json.dumps(report, ensure_ascii=False) == json.dumps(again, ensure_ascii=False)
    assert report["schema_version"] == EVAL_SCHEMA_VERSION
    assert report["case_count"] == len(cases)
    for rule in ("schema", "numbers", "length", "priority", "language", "used_facts"):
        assert report["metrics"][f"{rule}_valid_rate"] == 1.0
    assert report["metrics"]["fallback_rate"] == 0.0
    assert all(case["valid"] for case in report["cases"])


def test_replay_client_needs_every_recorded_response() -> None:
    base = _base_case()

    assert replay_client(()) is None
    assert replay_client([replace(base, recorded_response=None)]) is None
    assert replay_client([base]) is not None


@pytest.mark.parametrize(
    ("case", "text", "rule"),
    [
        (_base_case, _reply("Box esta vuelta.", language="en"), "language"),
        (_base_case, _reply("Box esta vuelta.", priority="critical"), "priority"),
        (_base_case, _reply("Calculamos ganar 9,9 segundos."), "numbers"),
        (_base_case, _reply(" ".join(["palabra"] * 40)), "length"),
        (_base_case, "esto no es json", "schema"),
        (_base_case, _reply("Box esta vuelta."), "used_facts"),
        (
            lambda: replace(_base_case(), forbidden_numbers=("2,4",)),
            _reply("Ganamos 2,4 segundos.", used=("gain_s", "pit_lap")),
            "numbers",
        ),
    ],
)
def test_bad_replies_report_rule_and_text(case: Any, text: str, rule: str) -> None:
    cases = (case(),)

    report = run_evaluation(FakeLLMClient([text]), cases, LLMConfig(model="fake"))
    result = report["cases"][0]

    assert result["valid"] is False
    assert rule in result["failed_rules"]
    assert all(failure["text"] == text for failure in result["failures"])


def test_client_error_is_reported_as_fallback() -> None:
    report = run_evaluation(
        FakeLLMClient([RuntimeError("boom")]), [_base_case()], LLMConfig(model="fake")
    )

    result = report["cases"][0]
    assert "fallback" in result["failed_rules"]
    assert any("boom" in failure["text"] for failure in result["failures"])


def test_latency_is_reported_when_available() -> None:
    response = LLMResponse(text=_reply("Box esta vuelta."), total_s=0.25)

    report = run_evaluation(
        FakeLLMClient([response, response]), [_base_case(), _base_case()], LLMConfig(model="fake")
    )

    assert report["metrics"]["latency_s"] == {"p50": 0.25, "p95": 0.25}
    assert report["cases"][0]["latency_s"] == 0.25


def test_priority_breakdown_and_response_metadata_are_reported() -> None:
    response = LLMResponse(
        text=_reply("Box esta vuelta."),
        finish_reason="length",
        reasoning_content="He pensado brevemente.",
        total_s=0.25,
    )

    report = run_evaluation(
        FakeLLMClient([response]),
        [_base_case()],
        LLMConfig(model="fake"),
    )
    priority = report["cases"][0]["priority"]

    assert report["schema_version"] == 2
    assert report["cases"][0]["finish_reason"] == "length"
    assert report["cases"][0]["reasoning_content"] == "He pensado brevemente."
    assert report["metrics"]["by_priority"][priority]["case_count"] == 1


def test_load_cases_rejects_missing_or_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No hay casos"):
        load_cases(tmp_path)

    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.json"):
        load_cases(tmp_path)


def test_cases_from_calls_skips_unusable_contexts() -> None:
    context = _base_case().context
    calls = [
        SimpleNamespace(seq=1, context_json=context.to_json(), response_text=_reply("OK")),
        SimpleNamespace(seq=2, context_json=None, response_text=_reply("OK")),
        SimpleNamespace(seq=3, context_json="{}", response_text=_reply("OK")),
    ]

    cases = cases_from_calls(calls)

    assert [case.case_id for case in cases] == ["session-1"]
    assert cases[0].recorded_response == _reply("OK")
    assert cases[0].context.context_hash == context.context_hash


def test_cli_eval_replays_dataset_and_writes_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "eval.json"

    assert main(["llm", "eval", "--dataset", str(DATASET), "--out", str(output_path)]) == 0

    printed = capsys.readouterr().out
    assert output_path.read_text(encoding="utf-8") == printed
    report = json.loads(printed)
    assert report["case_count"] >= 30
    assert report["metrics"]["schema_valid_rate"] == 1.0


def test_cli_eval_from_session_requires_an_existing_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    assert main(["llm", "eval", "--from-session", "1", "--db", str(missing)]) == 2


@pytest.mark.llm_eval
def test_real_model_evaluation_is_optional() -> None:
    endpoint = os.environ.get("F1_LLM_ENDPOINT")
    model = os.environ.get("F1_LLM_MODEL")
    if not endpoint or not model:
        pytest.skip("F1_LLM_ENDPOINT y F1_LLM_MODEL no están configurados")

    config = LLMConfig(enabled=True, endpoint=endpoint, model=model)
    cases = load_cases(DATASET)

    report = run_evaluation(OpenAICompatibleClient(config), cases, config)

    assert report["case_count"] == len(cases)
