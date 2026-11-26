"""Pruebas del compositor determinista de respuestas (Q10)."""

from __future__ import annotations

import re

import pytest

from f1_strategist.agent.templates import fact_unit, format_fact
from f1_strategist.config import PilotConfig
from f1_strategist.console_messages import pilot_fact_label
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.pilot.answers import compose_answer
from f1_strategist.pilot.tools_resources import estimate_fuel_margin
from f1_strategist.pilot.types import (
    AnswerKind,
    Clarification,
    DriverCandidate,
    FactKind,
    Intent,
    PilotQuery,
    RoutedQuery,
    ToolFact,
    ToolInputs,
    ToolResult,
)
from f1_strategist.race_state import RaceState
from f1_strategist.strategy.types import StrategySnapshot

ANSWER_INTENTS = tuple(intent for intent in Intent if intent is not Intent.UNKNOWN)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _routed(
    intent: Intent = Intent.FUEL_STATUS,
    *,
    clarification: Clarification | None = None,
) -> RoutedQuery:
    return RoutedQuery(
        query=PilotQuery(
            query_id="q-1",
            text="¿Cuánto combustible queda?",
            language="es",
            received_at=10.0,
        ),
        intent=intent,
        confidence=0.9,
        clarification=clarification,
    )


def _facts() -> tuple[ToolFact, ...]:
    return (
        ToolFact("gap_s", 1.8, "s", FactKind.MEASURED, Quality.OK),
        ToolFact("margin_laps", 0.75, "vueltas", FactKind.ESTIMATED, Quality.OK),
    )


def _result(
    intent: Intent = Intent.FUEL_STATUS,
    *,
    facts: tuple[ToolFact, ...] | None = None,
    missing: tuple[str, ...] = (),
    quality: Quality = Quality.OK,
    stale: bool = False,
    notes: tuple[str, ...] = (),
) -> ToolResult:
    return ToolResult(
        intent=intent,
        quality=quality,
        facts=_facts() if facts is None else facts,
        missing=missing,
        stale=stale,
        notes=notes,
    )


@pytest.mark.parametrize("intent", ANSWER_INTENTS)
def test_one_answer_per_intent(intent: Intent) -> None:
    answer = compose_answer(_routed(intent), _result(intent), language="es", cfg=PilotConfig())

    assert answer.kind is AnswerKind.ANSWER
    assert answer.intent is intent
    assert answer.message_key == f"pilot.answer.{intent.value}"
    assert "1.8 s (medido)" in answer.text
    assert "0.75 vueltas (estimado)" in answer.text


def test_partial_answer_names_missing_data() -> None:
    result = _result(missing=("fuel_kg", "consumption_per_lap"))

    answer = compose_answer(_routed(), result, language="es", cfg=PilotConfig())

    assert answer.kind is AnswerKind.ANSWER
    assert answer.message_key == "pilot.answer.fuel_status.partial"
    assert "No tengo combustible restante, consumo por vuelta." in answer.text


def test_none_valued_fact_counts_as_missing() -> None:
    facts = (ToolFact("fuel_kg", None, "kg", FactKind.MEASURED, Quality.OK),)
    result = _result(facts=facts)

    answer = compose_answer(_routed(), result, language="es", cfg=PilotConfig())

    assert answer.kind is AnswerKind.UNAVAILABLE
    assert "No tengo datos de combustible" in answer.text
    assert "combustible restante" in answer.text


@pytest.mark.parametrize(
    ("result", "cfg"),
    [
        (None, PilotConfig()),
        (_result(facts=()), PilotConfig()),
        (_result(quality=Quality.INSUFFICIENT), PilotConfig()),
        (_result(quality=Quality.UNAVAILABLE), PilotConfig()),
    ],
)
def test_no_data_is_declared_without_inventing_numbers(
    result: ToolResult | None, cfg: PilotConfig
) -> None:
    answer = compose_answer(_routed(), result, language="es", cfg=cfg)

    assert answer.kind is AnswerKind.UNAVAILABLE
    assert answer.text == "No tengo datos de combustible ahora mismo."
    assert not _NUMBER.search(answer.text)


@pytest.mark.parametrize(("stale", "quality"), [(True, Quality.OK), (False, Quality.STALE)])
def test_stale_data_is_declared(stale: bool, quality: Quality) -> None:
    result = _result(stale=stale, quality=quality)

    answer = compose_answer(_routed(), result, language="es", cfg=PilotConfig())

    assert answer.kind is AnswerKind.ANSWER
    assert answer.text.endswith("Los datos están desactualizados.")


def test_unknown_intent_suggests_topics() -> None:
    answer = compose_answer(_routed(Intent.UNKNOWN), None, language="es", cfg=PilotConfig())

    assert answer.kind is AnswerKind.UNKNOWN
    assert answer.message_key == "pilot.unknown"
    assert "combustible" in answer.text and "estrategia" in answer.text


def test_clarification_lists_options_by_position() -> None:
    clarification = Clarification(
        message_key="pilot.clarification",
        options=(
            DriverCandidate(2, "Hamilton", 44, 3, "Ferrari"),
            DriverCandidate(7, "Hülkenberg", 27, 8, "Sauber"),
        ),
        expires_at=40.0,
    )

    answer = compose_answer(
        _routed(Intent.COMPARE_DRIVER, clarification=clarification),
        None,
        language="es",
        cfg=PilotConfig(),
    )

    assert answer.kind is AnswerKind.CLARIFICATION
    assert answer.message_key == "pilot.clarification"
    assert "Hamilton (P3)" in answer.text
    assert "Hülkenberg (P8)" in answer.text


def test_unknown_clarification_key_falls_back_safely() -> None:
    clarification = Clarification(
        message_key="pilot.not_a_template",
        options=(DriverCandidate(2, "Hamilton", 44, 3, "Ferrari"),),
        expires_at=40.0,
    )

    answer = compose_answer(
        _routed(Intent.COMPARE_DRIVER, clarification=clarification),
        None,
        language="es",
        cfg=PilotConfig(),
    )

    assert answer.kind is AnswerKind.CLARIFICATION
    assert answer.message_key == "pilot.clarification"
    assert "Hamilton (P3)" in answer.text


def test_tool_note_template_wins_over_missing_data() -> None:
    result = _result(
        Intent.PIT_UNDERCUT, facts=(), quality=Quality.INSUFFICIENT, notes=("pilot.leader",)
    )

    answer = compose_answer(_routed(Intent.PIT_UNDERCUT), result, language="es", cfg=PilotConfig())

    assert answer.kind is AnswerKind.ANSWER
    assert answer.message_key == "pilot.leader"
    assert answer.text == "Vas primero: no hay nadie delante."


def test_kind_vocabulary_is_distinct_per_fact() -> None:
    facts = (
        ToolFact("gap_s", 1.0, "s", FactKind.MEASURED, Quality.OK),
        ToolFact("margin_laps", 2.0, "vueltas", FactKind.ESTIMATED, Quality.OK),
        ToolFact("action", "PIT_NOW", None, FactKind.RECOMMENDED, Quality.OK),
    )

    answer = compose_answer(
        _routed(Intent.ASK_STRATEGY), _result(facts=facts), language="es", cfg=PilotConfig()
    )

    assert "(medido)" in answer.text
    assert "(estimado)" in answer.text
    assert "(recomendado)" in answer.text
    assert "Parar ahora" in answer.text


@pytest.mark.parametrize("intent", ANSWER_INTENTS)
def test_every_number_in_text_comes_from_a_fact(intent: Intent) -> None:
    facts = (
        ToolFact("fuel_kg", 12.444, "kg", FactKind.MEASURED, Quality.OK),
        ToolFact("position", 3, None, FactKind.MEASURED, Quality.OK),
        ToolFact("undercut_gain_s", 2.006, "s", FactKind.ESTIMATED, Quality.OK),
        ToolFact("car_index", 2, None, FactKind.MEASURED, Quality.OK),
    )

    answer = compose_answer(
        _routed(intent), _result(intent, facts=facts), language="es", cfg=PilotConfig()
    )

    rendered = " ".join(
        format_fact(fact.name, fact.value)
        + (f" {fact.unit}" if fact.unit and fact_unit(fact.name) is None else "")
        for fact in facts
    )
    for number in _NUMBER.findall(answer.text):
        assert number in rendered


def test_number_format_uses_shared_format_fact() -> None:
    facts = (
        ToolFact("gap_s", 2.0, "s", FactKind.MEASURED, Quality.OK),
        ToolFact("undercut_gain_s", 2.006, "s", FactKind.ESTIMATED, Quality.OK),
    )

    answer = compose_answer(
        _routed(Intent.PIT_UNDERCUT), _result(facts=facts), language="es", cfg=PilotConfig()
    )

    assert "2 s (medido)" in answer.text
    assert "2.01 s (estimado)" in answer.text


def test_unsupported_language_falls_back_to_spanish() -> None:
    answer = compose_answer(_routed(), _result(), language="en", cfg=PilotConfig())

    assert answer.text.startswith("Combustible: ")


def test_compose_is_deterministic() -> None:
    routed = _routed()
    result = _result()

    first = compose_answer(routed, result, language="es", cfg=PilotConfig())
    second = compose_answer(routed, result, language="es", cfg=PilotConfig())

    assert first.to_dict() == second.to_dict()


def test_followup_pending_only_for_explanatory_intents() -> None:
    strategy = compose_answer(
        _routed(Intent.ASK_STRATEGY), _result(), language="es", cfg=PilotConfig()
    )
    fuel = compose_answer(_routed(), _result(), language="es", cfg=PilotConfig())
    disabled = compose_answer(
        _routed(Intent.ASK_STRATEGY),
        _result(),
        language="es",
        cfg=PilotConfig(llm_followup=False),
    )

    assert strategy.followup_pending is True
    assert fuel.followup_pending is False
    assert disabled.followup_pending is False


def test_stale_note_answer_keeps_the_stale_warning() -> None:
    result = _result(
        Intent.PIT_UNDERCUT,
        facts=(),
        quality=Quality.STALE,
        stale=True,
        notes=("pilot.leader",),
    )

    answer = compose_answer(_routed(Intent.PIT_UNDERCUT), result, language="es", cfg=PilotConfig())

    assert answer.message_key == "pilot.leader"
    assert answer.text == "Vas primero: no hay nadie delante. Los datos están desactualizados."


def test_real_fuel_tool_result_uses_labels_not_raw_fact_names() -> None:
    metric = Metric(
        key="fuel.in_tank_kg",
        value=42.5,
        unit="kg",
        window=Window("instant"),
        quality=Quality.OK,
        sample_count=1,
        computed_at_monotonic=10.0,
    )
    inputs = ToolInputs(
        state=RaceState().snapshot(),
        metrics=MetricsSnapshot(metrics={"fuel.in_tank_kg": metric}),
        strategy=StrategySnapshot(),
        history=LapHistorySnapshot(),
        config=PilotConfig(),
        now=10.0,
    )
    result = estimate_fuel_margin(_routed(), inputs)

    answer = compose_answer(_routed(), result, language="es", cfg=PilotConfig())

    assert "combustible restante" in answer.text
    assert "fuel_in_tank_kg" not in answer.text


@pytest.mark.parametrize(
    "name",
    [
        "compare.rival.name",
        "compare.interval_s",
        "compare.pace_delta_s",
        "pace.reference_s",
        "pace.loss_s",
        "pace.evidence.sector_loss_s",
        "strategy_action",
        "strategy_pit_lap",
        "strategy_gain_s",
        "fuel_in_tank_kg",
    ],
)
def test_published_fact_schemas_have_spanish_labels(name: str) -> None:
    assert pilot_fact_label(name) != name
