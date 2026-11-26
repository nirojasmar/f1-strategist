from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from f1_strategist.agent.types import ContextKind, ContextPayload, ValidationResult
from f1_strategist.agent.validator import ResponseValidator, ValidationIssue
from f1_strategist.config import AppConfig
from f1_strategist.events.types import AgentAlert, EventPhase, EventPriority


def _alert(**overrides: object) -> AgentAlert:
    values: dict[str, object] = {
        "alert_id": "alert-1",
        "event_id": "event-1",
        "phase": EventPhase.OPENED,
        "priority": EventPriority.STRATEGIC,
        "category": "strategy",
        "message_key": "event.strategy_pit_recommended",
        "facts": (("gain_s", 2.4), ("confidence", 0.7), ("laps_left", 2)),
        "lap": 12,
    }
    values.update(overrides)
    return AgentAlert(**values)  # type: ignore[arg-type]


def _context(**overrides: object) -> ContextPayload:
    values: dict[str, object] = {
        "kind": ContextKind.ALERT,
        "language": "es",
        "alert": {
            "message_key": "event.strategy_pit_recommended",
            "priority": "strategic",
            "lap": 12,
            "facts": [["gain_s", 2.4], ["confidence", 0.7], ["laps_left", 2]],
        },
        "situation": {"lap": 12, "position": 3, "laps_remaining": 18},
        "strategy": {"action": "pit", "lap": 13, "gain_s": 2.4, "confidence": 0.7},
        "metrics": {"fuel_delta_kg": -0.4},
        "uncertainties": (),
        "recent_messages": (),
    }
    values.update(overrides)
    return ContextPayload(**values)  # type: ignore[arg-type]


def _reply(**overrides: object) -> str:
    data: dict[str, object] = {
        "should_speak": True,
        "priority": "strategic",
        "language": "es",
        "message": "Box esta vuelta: ganamos 2,4 s, con confianza 0,7.",
        "reason": "La degradación hace más rápida la parada.",
        "used_facts": ["gain_s", "confidence"],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _validate(
    text: str, context: ContextPayload | None = None, **alert: object
) -> ValidationResult:
    return ResponseValidator(AppConfig()).validate(
        text, context if context is not None else _context(), _alert(**alert)
    )


def test_valid_reply_passes() -> None:
    result = _validate(_reply())
    assert result.valid, result.errors
    assert result.reply is not None
    assert result.reply.used_facts == ("gain_s", "confidence")


def test_real_context_fact_and_metric_shapes_validate() -> None:
    context = _context(
        alert={
            "message_key": "event.strategy_pit_recommended",
            "priority": "strategic",
            "facts": [{"name": "gain_s", "value": 2.4, "unit": "s"}],
        },
        metrics={"wear_mean_pct": {"value": 42.0, "unit": "pct", "quality": "ok"}},
    )
    text = _reply(message="Ganamos 2,4 s; desgaste 42 %.", used_facts=["gain_s"])
    result = ResponseValidator(AppConfig()).validate(text, context, _alert(facts=()))
    assert result.valid, result.errors


def test_car_index_is_validated_one_based_and_minutes_are_units() -> None:
    context = _context(
        alert={
            "message_key": "event.rival_pit_stop",
            "priority": "strategic",
            "facts": [
                {"name": "car_index", "value": 1, "unit": ""},
                {"name": "horizon_min", "value": 5.0, "unit": "min"},
            ],
        }
    )
    text = _reply(
        message="El coche 1 para en 5 min.",
        used_facts=["car_index", "horizon_min"],
    )
    result = ResponseValidator(AppConfig()).validate(text, context, _alert(facts=()))
    assert result.valid, result.errors


def test_same_name_observations_from_different_sources_are_all_kept() -> None:
    assert _validate(_reply(message="Vuelta 12.", used_facts=[])).valid
    assert _validate(_reply(message="Vuelta 13.", used_facts=[])).valid


def test_compound_cardinal_is_checked_against_remaining_laps() -> None:
    assert _validate(_reply(message="Quedan dos vueltas.", used_facts=[])).valid
    rejected = _validate(_reply(message="Quedan veinticinco vueltas.", used_facts=[]))
    assert ValidationIssue.UNKNOWN_NUMBER in rejected.errors


def test_per_lap_percentage_is_not_an_absolute_percentage() -> None:
    context = _context(
        alert={"facts": []},
        situation={},
        strategy={"wear_rate_pct_per_lap": 0.85},
        metrics={},
    )
    assert _validate(
        _reply(message="Desgaste 0,85 %/vuelta.", used_facts=[]), context, facts=()
    ).valid
    rejected = _validate(_reply(message="Desgaste 0,85 %.", used_facts=[]), context, facts=())
    assert ValidationIssue.UNIT_MISMATCH in rejected.errors


def test_dot_decimal_and_word_number_pass() -> None:
    assert _validate(_reply(message="Ganamos 2.4 s.")).valid
    assert _validate(_reply(message="Quedan dos vueltas.")).valid


def test_invented_number_invalidates() -> None:
    result = _validate(_reply(message="Ganamos 3,9 s."))
    assert not result.valid
    assert ValidationIssue.UNKNOWN_NUMBER in result.errors


def test_invented_word_number_invalidates() -> None:
    result = _validate(_reply(message="Quedan cuatro vueltas."))
    assert not result.valid
    assert ValidationIssue.UNKNOWN_NUMBER in result.errors


def test_changed_unit_invalidates() -> None:
    result = _validate(_reply(message="Ganamos 2,4 kg."))
    assert not result.valid
    assert ValidationIssue.UNIT_MISMATCH in result.errors


def test_rounding_matches_format_fact() -> None:
    context = _context(
        alert={"facts": [["gain_s", 2.45]]},
        situation={},
        strategy={"gain_s": 2.45},
        metrics={},
    )
    assert _validate(_reply(message="Ganamos 2,5 s.", used_facts=[]), context, facts=()).valid
    rejected = _validate(_reply(message="Ganamos 2,4 s.", used_facts=[]), context, facts=())
    assert ValidationIssue.UNKNOWN_NUMBER in rejected.errors


@pytest.mark.parametrize("laps", [0, 10, 20])
def test_integer_facts_keep_trailing_zeros(laps: int) -> None:
    context = _context(
        alert={"facts": [["laps_left", laps]]},
        situation={},
        strategy={},
        metrics={},
    )
    reply = _reply(message=f"Quedan {laps} vueltas.", used_facts=[])
    assert _validate(reply, context, facts=(("laps_left", laps),)).valid


def test_priority_cannot_change() -> None:
    result = _validate(_reply(priority="critical"))
    assert not result.valid
    assert ValidationIssue.PRIORITY_MISMATCH in result.errors


def test_unknown_used_fact_invalidates() -> None:
    result = _validate(_reply(used_facts=["gain_s", "invented"]))
    assert not result.valid
    assert ValidationIssue.UNKNOWN_FACT in result.errors


def test_wrong_language_invalidates() -> None:
    result = _validate(_reply(language="en"))
    assert not result.valid
    assert ValidationIssue.LANGUAGE_MISMATCH in result.errors


def test_too_long_in_words_and_chars() -> None:
    many_words = _reply(message=" ".join(["vueltas"] * 31))
    assert ValidationIssue.TOO_LONG in _validate(many_words).errors
    long_text = _reply(message="vuelta " * 40)
    assert ValidationIssue.TOO_LONG in _validate(long_text).errors


def test_markdown_newlines_and_emoji_are_rejected() -> None:
    assert ValidationIssue.FORMAT_INVALID in _validate(_reply(message="**Box** ahora.")).errors
    assert ValidationIssue.FORMAT_INVALID in _validate(_reply(message="Box\nahora.")).errors
    assert ValidationIssue.FORMAT_INVALID in _validate(_reply(message="Box ahora 🏎️.")).errors


def test_empty_message_invalidates() -> None:
    result = _validate(_reply(message="   "))
    assert not result.valid
    assert ValidationIssue.EMPTY_MESSAGE in result.errors


def test_imperative_phrase_rejected_with_high_uncertainty() -> None:
    context = _context(uncertainties=("stale:tyres",))
    result = _validate(_reply(message="Entra a boxes ahora."), context)
    assert not result.valid
    assert ValidationIssue.IMPERATIVE_PHRASE in result.errors
    assert _validate(_reply(message="Box esta vuelta."), _context()).valid


def test_question_followup_rejects_instructions_without_uncertainty() -> None:
    context = _context(
        kind=ContextKind.QUESTION,
        alert={"facts": []},
        situation={"lap": 3},
        strategy={"action": "stay_out"},
        metrics={},
        uncertainties=(),
    )
    text = _reply(
        message="Entra a boxes aproximadamente en la vuelta 3.",
        used_facts=["action"],
    )

    result = ResponseValidator(AppConfig()).validate(text, context, _alert(facts=()))

    assert ValidationIssue.IMPERATIVE_PHRASE in result.errors
    commentary = _reply(
        message="Puedes mantener el plan aproximadamente 3 vueltas.",
        used_facts=["action"],
    )
    assert ResponseValidator(AppConfig()).validate(commentary, context, _alert(facts=())).valid


def test_estimate_caution_uses_the_supporting_fact() -> None:
    context = _context(
        kind=ContextKind.QUESTION,
        alert={"facts": []},
        situation={},
        strategy={},
        metrics={},
        tool_facts=(
            {
                "name": "tyre_life_laps",
                "value": 8,
                "unit": "lap",
                "kind": "estimated",
                "quality": "ok",
            },
        ),
    )
    unhedged = _reply(message="Quedan 8 vueltas.", used_facts=[])

    result = ResponseValidator(AppConfig()).validate(unhedged, context, _alert(facts=()))

    assert ValidationIssue.ESTIMATE_WITHOUT_CAUTION in result.errors
    hedged = _reply(message="Quedan aproximadamente 8 vueltas.", used_facts=[])
    assert ResponseValidator(AppConfig()).validate(hedged, context, _alert(facts=())).valid


def test_repeated_recent_message_rejected() -> None:
    context = _context(recent_messages=("Box esta vuelta: ganamos 2,4 s, con confianza 0,7.",))
    result = _validate(_reply(), context)
    assert not result.valid
    assert ValidationIssue.REPEATED_MESSAGE in result.errors


def test_invalid_json_and_schema_errors() -> None:
    assert ValidationIssue.INVALID_JSON in _validate("no es json").errors
    assert ValidationIssue.SCHEMA_MISMATCH in _validate(json.dumps({"message": "hola"})).errors
    extra = json.loads(_reply())
    extra["invented"] = True
    assert ValidationIssue.SCHEMA_MISMATCH in _validate(json.dumps(extra)).errors


def test_declined_non_critical_reply_is_valid_without_text_checks() -> None:
    result = _validate(_reply(should_speak=False, message=""))
    assert result.valid
    assert result.reply is not None and not result.reply.should_speak


def test_declined_critical_reply_is_ignored() -> None:
    result = _validate(_reply(should_speak=False, message=""), priority="critical")
    assert not result.valid
    assert ValidationIssue.EMPTY_MESSAGE in result.errors
    spoken = _validate(_reply(should_speak=False, priority="critical"), priority="critical")
    assert spoken.valid
    assert spoken.reply is not None and spoken.reply.should_speak


def test_accepts_agent_config_directly() -> None:
    from f1_strategist.config import AgentConfig

    result = ResponseValidator(AgentConfig()).validate(_reply(), _context(), _alert())
    assert result.valid


@given(st.text(max_size=200))
def test_arbitrary_text_never_raises(text: str) -> None:
    result = ResponseValidator().validate(text, _context(), _alert())
    assert isinstance(result, ValidationResult)
    assert isinstance(result.valid, bool)


@pytest.mark.parametrize("message", ["", "2,4 s", "Sin cifras nuevas.", "Quedan 2 vueltas."])
def test_stable_for_short_texts(message: str) -> None:
    first = _validate(_reply(message=message))
    second = _validate(_reply(message=message))
    assert first == second
