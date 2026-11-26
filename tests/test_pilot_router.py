from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import PilotConfig
from f1_strategist.pilot.entities import DriverDirectory
from f1_strategist.pilot.router import (
    SUGGESTED_INTENTS,
    IntentCatalog,
    RouterError,
    load_catalog,
    normalize,
    parse_catalog,
    route,
)
from f1_strategist.pilot.types import Intent, RoutedQuery
from f1_strategist.race_state import CarSnapshot, RaceState


class _Drivers(DriverDirectory):
    """Directorio vacío: no hay pilotos que resolver."""


class _Dialogue:
    def __init__(self, intent: Intent | None) -> None:
        self._intent = intent
        self.calls: list[float] = []

    def last_intent(self, now: float) -> Intent | None:
        self.calls.append(now)
        return self._intent

    def pending_clarification(self, now: float) -> None:
        return None

    def resolve_clarification(self, text: str, now: float) -> None:
        return None

    def cancel_clarification(self) -> None:
        return None


def _route(
    text: str,
    *,
    dialogue: _Dialogue | None = None,
    cfg: PilotConfig | None = None,
    catalog: IntentCatalog | None = None,
    language: str = "es",
    now: float = 0.0,
) -> RoutedQuery:
    return route(
        text,
        language=language,
        drivers=_Drivers(),
        dialogue=dialogue,
        cfg=cfg or PilotConfig(),
        now=now,
        catalog=catalog,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("¿Cambiamos de estrategia?", Intent.ASK_STRATEGY),
        ("¿Cómo voy respecto a Hamilton?", Intent.COMPARE_DRIVER),
        ("¿Cuánto combustible queda?", Intent.FUEL_STATUS),
        ("¿Cuánto aguantan estos neumáticos?", Intent.TYRE_LIFE),
        ("¿Por qué estamos perdiendo tiempo?", Intent.PACE_DIAGNOSIS),
        ("¿Podemos hacer undercut?", Intent.PIT_UNDERCUT),
    ],
)
def test_reference_questions(text: str, expected: Intent) -> None:
    assert _route(text).intent is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("¿Cómo voy?", Intent.GENERAL_RACE_STATE),
        ("En qué posición estoy", Intent.GENERAL_RACE_STATE),
        ("¿Cuándo paro?", Intent.ASK_STRATEGY),
        ("¿Cuánto combustible me queda para el final?", Intent.FUEL_STATUS),
        ("¿Cuánto me duran las ruedas?", Intent.TYRE_LIFE),
        ("¿Dónde estamos perdiendo?", Intent.PACE_DIAGNOSIS),
        ("¿Cómo voy contra Verstappen?", Intent.COMPARE_DRIVER),
        ("¿A cuánto está el de delante?", Intent.GAP_STATUS),
        ("¿Cómo está el coche?", Intent.DAMAGE_STATUS),
        ("¿Cuántos daños tengo?", Intent.DAMAGE_STATUS),
        ("¿Va a llover?", Intent.WEATHER_FORECAST),
        ("¿Cuál es la previsión?", Intent.WEATHER_FORECAST),
    ],
)
def test_intent_variants(text: str, expected: Intent) -> None:
    assert _route(text).intent is expected


def test_normalize_strips_case_accents_and_punctuation() -> None:
    assert normalize("  ¿CUÁNTO   Combustible… queda? ") == "cuanto combustible queda"


def test_normalization_and_minor_typos_are_tolerated() -> None:
    routed = _route("¿CUANTO COMBUSTIBE QUEDA?")
    assert routed.intent is Intent.FUEL_STATUS
    assert routed.normalized_text == "cuanto combustibe queda"
    assert routed.confidence >= PilotConfig().min_intent_confidence


@pytest.mark.parametrize(
    "text",
    [
        "¿Quién ganó el mundial de 1996?",
        "¿Cuál es el color del coche de seguridad?",
        "",
        "   ",
    ],
)
def test_out_of_scope_questions_are_unknown(text: str) -> None:
    routed = _route(text)
    assert routed.intent is Intent.UNKNOWN
    assert routed.confidence < PilotConfig().min_intent_confidence


def test_three_suggestions_are_published_for_unknown() -> None:
    assert SUGGESTED_INTENTS == (Intent.FUEL_STATUS, Intent.TYRE_LIFE, Intent.ASK_STRATEGY)


def test_tie_between_intents_is_unknown() -> None:
    catalog = parse_catalog(
        {
            "intents": {
                "fuel_status": {"phrases": {"prueba": 0.9}},
                "tyre_life": {"phrases": {"prueba": 0.9}},
            }
        },
        language="es",
    )
    routed = _route("prueba", catalog=catalog)
    assert routed.intent is Intent.UNKNOWN
    assert routed.confidence == 0.0


def test_below_threshold_is_unknown() -> None:
    routed = _route("el de delante", cfg=PilotConfig(min_intent_confidence=0.95))
    assert routed.intent is Intent.UNKNOWN


def test_continuation_reuses_previous_intent() -> None:
    dialogue = _Dialogue(Intent.FUEL_STATUS)
    routed = _route("¿y el de atrás?", dialogue=dialogue)
    assert routed.intent is Intent.FUEL_STATUS
    assert routed.confidence >= PilotConfig().min_intent_confidence


def test_continuation_uses_received_instant() -> None:
    dialogue = _Dialogue(Intent.TYRE_LIFE)
    _route("¿y Verstappen?", dialogue=dialogue, now=42.5)
    assert dialogue.calls == [42.5]


def test_explicit_intent_wins_over_continuation() -> None:
    dialogue = _Dialogue(Intent.TYRE_LIFE)
    routed = _route("¿y cuánto combustible queda?", dialogue=dialogue)
    assert routed.intent is Intent.FUEL_STATUS


def test_no_continuation_without_previous_intent() -> None:
    assert _route("¿y el de atrás?").intent is Intent.UNKNOWN
    unknown = _Dialogue(Intent.UNKNOWN)
    assert _route("¿y el de atrás?", dialogue=unknown).intent is Intent.UNKNOWN


def test_routing_is_deterministic_and_pure() -> None:
    first = _route("¿Cuánto combustible queda?", now=3.0)
    second = _route("¿Cuánto combustible queda?", now=3.0)
    assert first == second
    assert first.query.received_at == 3.0
    assert first.query.query_id.startswith("q-")


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"intents": {}},
        {"intents": {"fuel_status": {}}},
        {"intents": {"fuel_status": {"phrases": {}}}},
        {"intents": {"fuel_status": {"keywords": {"combustible": 0.8}}}},
        {"intents": {"fuel_status": {"phrases": {"combustible": 0.0}}}},
        {"intents": {"fuel_status": {"phrases": {"combustible": 1.5}}}},
        {"intents": {"fuel_status": {"phrases": {"combustible": "alto"}}}},
        {"intents": {"fuel_status": {"phrases": {"combustible": True}}}},
        {"intents": {"charla": {"phrases": {"hola": 1.0}}}},
        {"intents": {"unknown": {"phrases": {"hola": 1.0}}}},
        {"intents": {"fuel_status": {"phrases": {"combustible": 0.8}, "extra": 1}}},
        {"intents": {"fuel_status": {"phrases": {"combustible": 0.8}, "negative": "x"}}},
        {"intents": {"fuel_status": {"phrases": {"¿?": 0.8}}}},
        {"extra": {}, "intents": {"fuel_status": {"phrases": {"combustible": 0.8}}}},
    ],
)
def test_invalid_catalog_is_rejected(data: dict[str, object]) -> None:
    with pytest.raises(RouterError):
        parse_catalog(data, language="es")


def test_catalog_loads_all_intents() -> None:
    catalog = load_catalog("es")
    assert {pattern.intent for pattern in catalog.intents} == set(Intent) - {Intent.UNKNOWN}


@pytest.mark.parametrize("language", ["en", "", "../es"])
def test_unsupported_language_is_a_clear_error(language: str) -> None:
    with pytest.raises(RouterError) as error:
        load_catalog(language)
    assert "es" in str(error.value)


def test_route_rejects_unsupported_language() -> None:
    with pytest.raises(RouterError):
        _route("¿Cuánto combustible queda?", language="en")


def _race_drivers() -> DriverDirectory:
    cars = (
        CarSnapshot(0, name="Adrian Vance", position=4),
        CarSnapshot(7, name="Otto Kessler", position=8),
    )
    state = replace(RaceState().snapshot(), cars=cars, player_car_index=0)
    return DriverDirectory.from_state(state)


def test_rival_compound_question_routes_as_a_driver_comparison() -> None:
    routed = route(
        "¿Qué neumáticos lleva Kessler?",
        language="es",
        drivers=_race_drivers(),
        dialogue=None,
        cfg=PilotConfig(),
        now=0.0,
        catalog=None,
    )

    assert routed.intent is Intent.COMPARE_DRIVER
    assert routed.entities[0].car_index == 7


def test_own_tyre_question_stays_player_only_with_a_full_grid() -> None:
    routed = route(
        "¿Cuánto aguantan estos neumáticos?",
        language="es",
        drivers=_race_drivers(),
        dialogue=None,
        cfg=PilotConfig(),
        now=0.0,
        catalog=None,
    )

    assert routed.intent is Intent.TYRE_LIFE
