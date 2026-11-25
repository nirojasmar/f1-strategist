from __future__ import annotations

import pytest

from f1_strategist.agent.prompt_registry import (
    PromptError,
    PromptRegistry,
    prompt_version,
)
from f1_strategist.agent.types import ContextKind, ContextPayload


def _context(kind: ContextKind = ContextKind.ALERT) -> ContextPayload:
    return ContextPayload(
        kind=kind,
        language="es",
        alert={
            "message_key": "events.tyre_wear",
            "priority": "strategic",
            "lap": 14,
            "facts": [{"name": "tyre_age_laps", "value": 14, "unit": "vueltas"}],
        },
        situation={"lap": 14, "position": 3, "compound": "MEDIUM"},
        strategy={"action": "pit", "lap": 16, "confidence": "media"},
        metrics={"wear_mean_pct": 61.0},
        uncertainties=("stale:wear_mean_pct",),
        recent_messages=("Vuelta 13: mantén el ritmo.",),
        glossary={"undercut": "adelantar parando antes"},
        constraints={"max_words": 30, "max_chars": 220},
    )


def test_load_and_render_alert_prompt() -> None:
    registry = PromptRegistry.load("es")
    rendered = registry.render(ContextKind.ALERT, _context())

    assert [message.role for message in rendered.messages] == ["system", "user"]
    system, user = rendered.messages
    assert "ingeniero de radio" in system.content
    assert "Esquema JSON de la respuesta" in system.content
    assert "Ejemplos ilustrativos" in user.content
    assert "events.tyre_wear" in user.content
    assert _context().context_hash in user.content


def test_prompt_version_is_stable() -> None:
    first = PromptRegistry.load("es").render(ContextKind.ALERT, _context())
    second = PromptRegistry.load("es").render(ContextKind.ALERT, _context())

    assert first.prompt_version == second.prompt_version
    assert first.prompt_version.startswith("es/alert.v1+")


def test_prompt_version_changes_with_content() -> None:
    original = prompt_version("es/alert", "v1", "texto del prompt")
    changed = prompt_version("es/alert", "v1", "texto del prompt corregido")

    assert original != changed
    assert prompt_version("es/alert", "v1", "texto del prompt") == original
    assert "es/alert.v1+" in original


def test_response_schema_matches_strict_wire_contract() -> None:
    rendered = PromptRegistry.load("es").render(ContextKind.ALERT, _context())

    assert rendered.response_schema["required"] == [
        "should_speak",
        "priority",
        "language",
        "message",
        "reason",
        "used_facts",
    ]
    properties = rendered.response_schema["properties"]
    assert set(properties) == {
        "should_speak",
        "priority",
        "language",
        "message",
        "reason",
        "used_facts",
    }


def test_unsupported_language_has_clear_error() -> None:
    with pytest.raises(PromptError, match="no soportado") as info:
        PromptRegistry.load("xx")

    message = str(info.value)
    assert "'xx'" in message
    assert "es" in message


def test_question_prompt_is_available() -> None:
    rendered = PromptRegistry.load("es").render(
        ContextKind.QUESTION, _context(ContextKind.QUESTION)
    )

    assert rendered.prompt_version.startswith("es/question.v2+")
    _system, user = rendered.messages
    assert "aclárala o contextualízala" in user.content
    assert "previous_answer" in user.content


def test_examples_are_marked_as_illustration() -> None:
    _system, user = PromptRegistry.load("es").render(ContextKind.ALERT, _context()).messages
    content = user.content.lower()

    assert "ejemplos ilustrativos" in content
    assert "inventados" in content
    assert "no copies sus cifras" in content


def test_system_prompt_includes_race_glossary() -> None:
    system, _user = PromptRegistry.load("es").render(ContextKind.ALERT, _context()).messages

    assert "Glosario de carrera" in system.content
    assert "undercut" in system.content
