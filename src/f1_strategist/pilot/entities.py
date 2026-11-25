"""Resolución de pilotos, aclaraciones y memoria de diálogo (Fase 7)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..config import PilotConfig
from ..race_state import CarSnapshot, RaceStateSnapshot
from .types import Clarification, DriverCandidate, PilotExchange

AMBIGUOUS_DRIVER_MESSAGE_KEY = "pilot_ambiguous_driver"

_RETIRED_STATUSES = frozenset({"retired", "did_not_finish", "disqualified", "not_classified"})
_AHEAD_WORDS = frozenset({"delante", "ahead"})
_BEHIND_WORDS = frozenset({"atras", "detras", "behind"})
_LEADER_WORDS = frozenset({"lider", "leader"})
_LAST_WORDS = frozenset({"ultimo", "last"})
_TEAMMATE_WORDS = frozenset({"companero", "teammate"})
_PREFIXES = (
    "el de ",
    "la de ",
    "los de ",
    "las de ",
    "el equipo ",
    "al ",
    "del ",
    "el ",
    "la ",
    "los ",
    "las ",
    "de ",
    "a ",
    "mi ",
)
_ORDINALS = {
    "primero": 1,
    "primera": 1,
    "primer": 1,
    "segundo": 2,
    "segunda": 2,
    "tercero": 3,
    "tercera": 3,
    "cuarto": 4,
    "cuarta": 4,
    "quinto": 5,
    "quinta": 5,
    "sexto": 6,
    "sexta": 6,
    "septimo": 7,
    "septima": 7,
    "octavo": 8,
    "octava": 8,
    "noveno": 9,
    "novena": 9,
    "decimo": 10,
    "decima": 10,
    "undecimo": 11,
    "duodecimo": 12,
    "decimotercero": 13,
    "decimocuarto": 14,
    "decimoquinto": 15,
    "decimosexto": 16,
    "decimoseptimo": 17,
    "decimoctavo": 18,
    "decimonoveno": 19,
    "vigesimo": 20,
}
_NUMBER_REFERENCE = re.compile(r"(?:numero|num|n)\s+(\d{1,2})|(\d{1,2})")
_POSITION_REFERENCE = re.compile(r"(?:p|pos|posicion)\s*(\d{1,2})")


class UnknownReason(StrEnum):
    """Motivo por el que una referencia no se pudo resolver."""

    EMPTY_REFERENCE = "empty_reference"
    DRIVER_NOT_FOUND = "driver_not_found"
    DRIVER_RETIRED = "driver_retired"
    NO_PLAYER = "no_player"
    NO_CAR_AHEAD = "no_car_ahead"
    NO_CAR_BEHIND = "no_car_behind"
    NO_LEADER = "no_leader"
    NO_LAST = "no_last"
    NO_TEAMMATE = "no_teammate"
    CLARIFICATION_NO_MATCH = "clarification_no_match"
    CLARIFICATION_AMBIGUOUS = "clarification_ambiguous"


@dataclass(frozen=True, slots=True)
class Resolved:
    """Referencia resuelta a un único piloto."""

    raw: str
    driver: DriverCandidate


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """Referencia que admite varios pilotos, ordenados por posición."""

    raw: str
    candidates: tuple[DriverCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))


@dataclass(frozen=True, slots=True)
class Unknown:
    """Referencia que no se pudo resolver, con su motivo."""

    raw: str
    reason: UnknownReason
    candidate: DriverCandidate | None = None


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    """Contexto de la pregunta; por defecto usa el coche del jugador."""

    player_car_index: int | None = None


def normalize_text(text: str) -> str:
    """Minúsculas, sin tildes y con separadores simples."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    cleaned = "".join(char if char.isalnum() else " " for char in plain)
    return " ".join(cleaned.split())


@dataclass(frozen=True, slots=True)
class DriverDirectory:
    """Pilotos conocidos, ordenados por posición y con estado de retirada."""

    candidates: tuple[DriverCandidate, ...] = ()
    retired_car_indices: frozenset[int] = frozenset()
    player_car_index: int | None = None

    def __post_init__(self) -> None:
        ordered = sorted(self.candidates, key=_position_key)
        object.__setattr__(self, "candidates", tuple(ordered))
        object.__setattr__(self, "retired_car_indices", frozenset(self.retired_car_indices))

    @classmethod
    def from_state(
        cls,
        state: RaceStateSnapshot,
        *,
        race_numbers: Mapping[int, int] | None = None,
    ) -> DriverDirectory:
        """Construye el directorio desde una instantánea; sin nombre no hay piloto."""
        numbers = race_numbers or {}
        candidates: list[DriverCandidate] = []
        retired: set[int] = set()
        for car in state.cars:
            if not car.name:
                continue
            if _is_retired(car):
                retired.add(car.car_index)
            number = numbers.get(car.car_index)
            if number is None:
                number = getattr(car, "race_number", None)
            candidates.append(
                DriverCandidate(
                    car_index=car.car_index,
                    name=car.name,
                    race_number=number,
                    position=car.position,
                    team=car.team,
                )
            )
        return cls(
            candidates=tuple(candidates),
            retired_car_indices=frozenset(retired),
            player_car_index=state.player_car_index,
        )

    def get(self, car_index: int) -> DriverCandidate | None:
        return next(
            (item for item in self.candidates if item.car_index == car_index),
            None,
        )

    def is_retired(self, car_index: int) -> bool:
        return car_index in self.retired_car_indices

    def active(self) -> tuple[DriverCandidate, ...]:
        return tuple(
            item for item in self.candidates if item.car_index not in self.retired_car_indices
        )

    def leader(self) -> DriverCandidate | None:
        return next(iter(self.active()), None)

    def last(self) -> DriverCandidate | None:
        return next(reversed(self.active()), None)

    def ahead_of(self, car_index: int) -> DriverCandidate | None:
        driver = self.get(car_index)
        if driver is None or driver.position is None:
            return None
        ahead = [
            item
            for item in self.active()
            if item.position is not None and item.position < driver.position
        ]
        return max(ahead, key=_position_key, default=None)

    def behind_of(self, car_index: int) -> DriverCandidate | None:
        driver = self.get(car_index)
        if driver is None or driver.position is None:
            return None
        behind = [
            item
            for item in self.active()
            if item.position is not None and item.position > driver.position
        ]
        return min(behind, key=_position_key, default=None)

    def team_mates(self, car_index: int) -> tuple[DriverCandidate, ...]:
        driver = self.get(car_index)
        team = normalize_text(driver.team) if driver and driver.team else ""
        if not team:
            return ()
        return tuple(
            item
            for item in self.active()
            if item.car_index != car_index
            and item.team is not None
            and normalize_text(item.team) == team
        )


def resolve_driver(
    raw: str,
    directory: DriverDirectory,
    *,
    context: ResolutionContext | None = None,
) -> Resolved | Ambiguous | Unknown:
    """Resuelve una referencia del piloto contra el directorio."""
    text = _strip_reference(normalize_text(raw))
    if not text:
        return Unknown(raw, UnknownReason.EMPTY_REFERENCE)
    player = _player_index(context, directory)

    relative = _resolve_relative(raw, text, directory, player)
    if relative is not None:
        return relative

    number = _parse_number(text)
    if number is not None:
        matches = [item for item in directory.candidates if item.race_number == number]
        return _select(raw, matches, directory)

    position = _parse_position(text)
    if position is not None:
        matches = [item for item in directory.candidates if item.position == position]
        return _select(raw, matches, directory)

    team_matches = [
        item
        for item in directory.candidates
        if item.team is not None and normalize_text(item.team) == text
    ]
    if team_matches:
        return _select(raw, team_matches, directory)

    exact = [item for item in directory.candidates if _name_matches(item, text, prefix=False)]
    if exact:
        return _select(raw, exact, directory)
    prefixed = [item for item in directory.candidates if _name_matches(item, text, prefix=True)]
    return _select(raw, prefixed, directory)


class DialogueState:
    """Memoria acotada de intercambios y una única aclaración pendiente."""

    __slots__ = ("_config", "_exchanges", "_pending")

    def __init__(self, config: PilotConfig | None = None) -> None:
        self._config = config or PilotConfig()
        self._exchanges: list[PilotExchange] = []
        self._pending: Clarification | None = None

    def record(self, exchange: PilotExchange, now: float) -> None:
        self._exchanges.append(exchange)
        self._exchanges = list(self.exchanges(now))

    def exchanges(self, now: float) -> tuple[PilotExchange, ...]:
        ttl = self._config.dialogue_ttl_s
        kept = [item for item in self._exchanges if now - item.query.received_at <= ttl]
        count = self._config.dialogue_turns
        return tuple(kept[-count:]) if count else ()

    def start_clarification(
        self,
        options: Sequence[DriverCandidate] | Ambiguous,
        now: float,
        *,
        message_key: str = AMBIGUOUS_DRIVER_MESSAGE_KEY,
    ) -> Clarification:
        """Registra la aclaración pendiente, sustituyendo la anterior."""
        candidates = options.candidates if isinstance(options, Ambiguous) else tuple(options)
        self._pending = Clarification(
            message_key=message_key,
            options=tuple(sorted(candidates, key=_position_key)),
            expires_at=now + self._config.clarification_ttl_s,
        )
        return self._pending

    def pending_clarification(self, now: float) -> Clarification | None:
        pending = self._pending
        if pending is None:
            return None
        if now >= pending.expires_at:
            self._pending = None
            return None
        return pending

    def resolve_clarification(self, text: str, now: float) -> Resolved | Unknown | None:
        """Resuelve la pendiente con nombre, número u ordinal; None si no hay o caducó."""
        pending = self.pending_clarification(now)
        if pending is None:
            return None
        normalized = _strip_reference(normalize_text(text))
        options = pending.options

        ordinal = _ORDINALS.get(normalized)
        if ordinal is not None:
            if 1 <= ordinal <= len(options):
                return self._confirm(text, options[ordinal - 1])
            return Unknown(text, UnknownReason.CLARIFICATION_NO_MATCH)

        number, forced = _parse_clarification_number(normalized)
        if number is not None:
            if not forced and 1 <= number <= len(options):
                return self._confirm(text, options[number - 1])
            matches = [item for item in options if item.race_number == number]
            return self._confirm_match(text, matches)

        matches = [item for item in options if _name_matches(item, normalized, prefix=False)]
        if not matches:
            matches = [item for item in options if _name_matches(item, normalized, prefix=True)]
        return self._confirm_match(text, matches)

    def _confirm_match(self, text: str, matches: Sequence[DriverCandidate]) -> Resolved | Unknown:
        if not matches:
            return Unknown(text, UnknownReason.CLARIFICATION_NO_MATCH)
        if len(matches) > 1:
            return Unknown(text, UnknownReason.CLARIFICATION_AMBIGUOUS)
        return self._confirm(text, matches[0])

    def _confirm(self, text: str, driver: DriverCandidate) -> Resolved:
        self._pending = None
        return Resolved(text, driver)


def _player_index(
    context: ResolutionContext | None,
    directory: DriverDirectory,
) -> int | None:
    if context is not None and context.player_car_index is not None:
        return context.player_car_index
    return directory.player_car_index


def _resolve_relative(
    raw: str,
    text: str,
    directory: DriverDirectory,
    player: int | None,
) -> Resolved | Ambiguous | Unknown | None:
    if text in _AHEAD_WORDS or text in _BEHIND_WORDS:
        if player is None or directory.get(player) is None:
            return Unknown(raw, UnknownReason.NO_PLAYER)
        if text in _AHEAD_WORDS:
            driver = directory.ahead_of(player)
            if driver is None:
                return Unknown(raw, UnknownReason.NO_CAR_AHEAD)
            return Resolved(raw, driver)
        driver = directory.behind_of(player)
        if driver is None:
            return Unknown(raw, UnknownReason.NO_CAR_BEHIND)
        return Resolved(raw, driver)

    if text in _LEADER_WORDS:
        driver = directory.leader()
        return Resolved(raw, driver) if driver else Unknown(raw, UnknownReason.NO_LEADER)
    if text in _LAST_WORDS:
        driver = directory.last()
        return Resolved(raw, driver) if driver else Unknown(raw, UnknownReason.NO_LAST)

    if text in _TEAMMATE_WORDS or text.startswith("companero de equipo"):
        if player is None or directory.get(player) is None:
            return Unknown(raw, UnknownReason.NO_PLAYER)
        mates = directory.team_mates(player)
        if not mates:
            return Unknown(raw, UnknownReason.NO_TEAMMATE)
        if len(mates) == 1:
            return Resolved(raw, mates[0])
        return Ambiguous(raw, mates)
    return None


def _select(
    raw: str,
    matches: Sequence[DriverCandidate],
    directory: DriverDirectory,
) -> Resolved | Ambiguous | Unknown:
    active = [item for item in matches if not directory.is_retired(item.car_index)]
    if not active:
        if matches:
            return Unknown(raw, UnknownReason.DRIVER_RETIRED, matches[0])
        return Unknown(raw, UnknownReason.DRIVER_NOT_FOUND)
    ordered = sorted(active, key=_position_key)
    if len(ordered) == 1:
        return Resolved(raw, ordered[0])
    return Ambiguous(raw, tuple(ordered))


def _name_matches(candidate: DriverCandidate, text: str, *, prefix: bool) -> bool:
    tokens = normalize_text(candidate.name).split()
    query = text.split()
    if not tokens or not query:
        return False
    if all(any(word == token for token in tokens) for word in query):
        return True
    if not prefix:
        return False
    return all(any(len(word) >= 3 and token.startswith(word) for token in tokens) for word in query)


def _parse_number(text: str) -> int | None:
    match = _NUMBER_REFERENCE.fullmatch(text)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def _parse_clarification_number(text: str) -> tuple[int | None, bool]:
    match = _NUMBER_REFERENCE.fullmatch(text)
    if match is None:
        return None, False
    if match.group(1) is not None:
        return int(match.group(1)), True
    return int(match.group(2)), False


def _parse_position(text: str) -> int | None:
    match = _POSITION_REFERENCE.fullmatch(text)
    if match is not None:
        return int(match.group(1))
    return _ORDINALS.get(text)


def _strip_reference(text: str) -> str:
    result = text
    while True:
        for prefix in _PREFIXES:
            if result.startswith(prefix) and len(result) > len(prefix):
                result = result[len(prefix) :].strip()
                break
        else:
            return result


def _position_key(candidate: DriverCandidate) -> tuple[int, int, int]:
    return (
        candidate.position is None,
        candidate.position or 0,
        candidate.car_index,
    )


def _is_retired(car: CarSnapshot) -> bool:
    return car.result_status in _RETIRED_STATUSES or car.driver_status in _RETIRED_STATUSES


__all__ = [
    "AMBIGUOUS_DRIVER_MESSAGE_KEY",
    "Ambiguous",
    "DialogueState",
    "DriverDirectory",
    "ResolutionContext",
    "Resolved",
    "Unknown",
    "UnknownReason",
    "normalize_text",
    "resolve_driver",
]
