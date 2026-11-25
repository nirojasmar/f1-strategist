"""Pruebas de resolución de pilotos, aclaraciones y diálogo (Q4)."""

from __future__ import annotations

from dataclasses import replace

from f1_strategist.config import PilotConfig
from f1_strategist.pilot.entities import (
    Ambiguous,
    DialogueState,
    DriverDirectory,
    ResolutionContext,
    Resolved,
    Unknown,
    UnknownReason,
    resolve_driver,
)
from f1_strategist.pilot.types import (
    AnswerKind,
    Intent,
    PilotAnswer,
    PilotExchange,
    PilotQuery,
)
from f1_strategist.race_state import CarSnapshot, RaceState

RACE_NUMBERS = {0: 44, 1: 1, 2: 11, 3: 17, 4: 16, 5: 81, 6: 63}


def _directory() -> DriverDirectory:
    cars = (
        CarSnapshot(0, name="Lewis Hamilton", team="mercedes", position=1),
        CarSnapshot(1, name="Max Verstappen", team="red_bull", position=2),
        CarSnapshot(4, name="Charles Leclerc", team="ferrari", position=3),
        CarSnapshot(2, name="Sergio Pérez", team="red_bull", position=4),
        CarSnapshot(3, name="Álex Pérez", team="ferrari", position=5),
        CarSnapshot(5, name="Oscar Piastri", team="mclaren", position=6, result_status="retired"),
        CarSnapshot(6, name="George Russell", team="mercedes", position=7),
    )
    state = replace(RaceState().snapshot(), cars=cars, player_car_index=0)
    return DriverDirectory.from_state(state, race_numbers=RACE_NUMBERS)


def _exchange(query_id: str, received_at: float) -> PilotExchange:
    return PilotExchange(
        query=PilotQuery(
            query_id=query_id,
            text="pregunta",
            language="es",
            received_at=received_at,
        ),
        answer=PilotAnswer(
            query_id=query_id,
            intent=Intent.UNKNOWN,
            kind=AnswerKind.UNKNOWN,
            text="",
            message_key="pilot_unknown",
        ),
    )


def test_repeated_surname_with_accents_is_ambiguous_by_position() -> None:
    result = resolve_driver("Pérez", _directory())
    assert isinstance(result, Ambiguous)
    assert [item.name for item in result.candidates] == ["Sergio Pérez", "Álex Pérez"]
    assert [item.position for item in result.candidates] == [4, 5]


def test_prefix_needs_at_least_three_letters() -> None:
    directory = _directory()
    assert isinstance(resolve_driver("ver", directory), Resolved)
    assert isinstance(resolve_driver("ham", directory), Resolved)
    assert isinstance(resolve_driver("lecl", directory), Resolved)
    result = resolve_driver("pe", directory)
    assert isinstance(result, Unknown)
    assert result.reason == UnknownReason.DRIVER_NOT_FOUND


def test_race_number_reference() -> None:
    directory = _directory()
    result = resolve_driver("el 44", directory)
    assert isinstance(result, Resolved)
    assert result.driver.name == "Lewis Hamilton"
    result = resolve_driver("número 1", directory)
    assert isinstance(result, Resolved)
    assert result.driver.name == "Max Verstappen"


def test_position_references() -> None:
    directory = _directory()
    for raw in ("el P3", "el tercero"):
        result = resolve_driver(raw, directory)
        assert isinstance(result, Resolved), raw
        assert result.driver.name == "Charles Leclerc"


def test_relative_references_respect_extremes() -> None:
    directory = _directory()
    ahead = resolve_driver("el de delante", directory)
    assert isinstance(ahead, Unknown)
    assert ahead.reason == UnknownReason.NO_CAR_AHEAD
    behind = resolve_driver("el de atrás", directory)
    assert isinstance(behind, Resolved)
    assert behind.driver.name == "Max Verstappen"
    leader = resolve_driver("el líder", directory)
    assert isinstance(leader, Resolved)
    assert leader.driver.name == "Lewis Hamilton"
    last = resolve_driver("el último", directory)
    assert isinstance(last, Resolved)
    assert last.driver.name == "George Russell"


def test_relative_from_middle_of_the_field() -> None:
    directory = _directory()
    ahead = resolve_driver("el de delante", directory, context=ResolutionContext(2))
    assert isinstance(ahead, Resolved)
    assert ahead.driver.name == "Charles Leclerc"
    behind = resolve_driver("el de atrás", directory, context=ResolutionContext(2))
    assert isinstance(behind, Resolved)
    assert behind.driver.name == "Álex Pérez"


def test_teammate_and_unknown_player() -> None:
    directory = _directory()
    mate = resolve_driver("mi compañero", directory)
    assert isinstance(mate, Resolved)
    assert mate.driver.name == "George Russell"
    no_player = resolve_driver("mi compañero", replace(directory, player_car_index=None))
    assert isinstance(no_player, Unknown)
    assert no_player.reason == UnknownReason.NO_PLAYER


def test_team_reference_returns_candidates_in_position_order() -> None:
    result = resolve_driver("el de Mercedes", _directory())
    assert isinstance(result, Ambiguous)
    assert [item.name for item in result.candidates] == ["Lewis Hamilton", "George Russell"]


def test_clarification_resolution_by_ordinal_name_and_number() -> None:
    directory = _directory()
    dialogue = DialogueState(PilotConfig())
    ambiguous = resolve_driver("Pérez", directory)
    assert isinstance(ambiguous, Ambiguous)

    clarification = dialogue.start_clarification(ambiguous, now=100.0)
    assert [item.position for item in clarification.options] == [4, 5]
    assert dialogue.pending_clarification(100.0) is clarification
    resolved = dialogue.resolve_clarification("2", now=101.0)
    assert isinstance(resolved, Resolved)
    assert resolved.driver.name == "Álex Pérez"
    assert dialogue.pending_clarification(101.0) is None

    dialogue.start_clarification(ambiguous, now=102.0)
    resolved = dialogue.resolve_clarification("Sergio", now=103.0)
    assert isinstance(resolved, Resolved)
    assert resolved.driver.name == "Sergio Pérez"

    dialogue.start_clarification(ambiguous, now=104.0)
    resolved = dialogue.resolve_clarification("número 11", now=105.0)
    assert isinstance(resolved, Resolved)
    assert resolved.driver.name == "Sergio Pérez"


def test_clarification_options_are_sorted_even_if_input_is_not() -> None:
    directory = _directory()
    ambiguous = resolve_driver("Pérez", directory)
    assert isinstance(ambiguous, Ambiguous)
    dialogue = DialogueState(PilotConfig())
    clarification = dialogue.start_clarification(tuple(reversed(ambiguous.candidates)), now=0.0)
    assert [item.position for item in clarification.options] == [4, 5]


def test_clarification_expiry_uses_the_received_instant() -> None:
    directory = _directory()
    ambiguous = resolve_driver("Pérez", directory)
    assert isinstance(ambiguous, Ambiguous)
    dialogue = DialogueState(PilotConfig(clarification_ttl_s=30.0))
    clarification = dialogue.start_clarification(ambiguous, now=100.0)
    assert dialogue.pending_clarification(129.999) is clarification
    assert dialogue.pending_clarification(130.0) is None
    dialogue.start_clarification(ambiguous, now=200.0)
    assert dialogue.resolve_clarification("1", now=230.0) is None


def test_dialogue_memory_keeps_turns_within_ttl() -> None:
    dialogue = DialogueState(PilotConfig(dialogue_turns=2, dialogue_ttl_s=10.0))
    for received_at in (0.0, 1.0, 2.0):
        dialogue.record(_exchange(str(received_at), received_at), now=received_at)
    assert [item.query.query_id for item in dialogue.exchanges(2.0)] == ["1.0", "2.0"]
    assert [item.query.query_id for item in dialogue.exchanges(11.0)] == ["1.0", "2.0"]
    assert [item.query.query_id for item in dialogue.exchanges(12.0)] == ["2.0"]


def test_retired_driver_is_reported() -> None:
    result = resolve_driver("Piastri", _directory())
    assert isinstance(result, Unknown)
    assert result.reason == UnknownReason.DRIVER_RETIRED
    assert result.candidate is not None
    assert result.candidate.car_index == 5


def test_unknown_and_empty_references() -> None:
    directory = _directory()
    empty = resolve_driver("", directory)
    assert isinstance(empty, Unknown)
    assert empty.reason == UnknownReason.EMPTY_REFERENCE
    unknown = resolve_driver("nadie", directory)
    assert isinstance(unknown, Unknown)
    assert unknown.reason == UnknownReason.DRIVER_NOT_FOUND
