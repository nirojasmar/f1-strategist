"""Generación determinista de carreras sintéticas con modelos del parser."""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from f1_strategist.parser import (
    MAX_CARS,
    BooleanValue,
    CarDamagePacket,
    CarStatusPacket,
    EnumValue,
    FinalClassification,
    FinalClassificationPacket,
    LapDataPacket,
    LapHistory,
    ParsedPacket,
    ParticipantsPacket,
    SessionHistoryPacket,
    SessionPacket,
    TyreSetsPacket,
    TyreStintHistory,
    parse_packet,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_COMPOUNDS: dict[str, tuple[int, str]] = {
    "soft": (16, "soft"),
    "medium": (17, "medium"),
    "hard": (18, "hard"),
    "intermediate": (7, "intermediate"),
    "wet": (8, "wet"),
    "c5": (16, "soft"),
    "c4": (17, "medium"),
    "c3": (18, "hard"),
    "c2": (19, "hard"),
    "c1": (20, "hard"),
    "c0": (21, "hard"),
    "c6": (22, "soft"),
}
_WEATHER = {
    "clear": 0,
    "light_cloud": 1,
    "overcast": 2,
    "light_rain": 3,
    "heavy_rain": 4,
    "storm": 5,
}


@dataclass(frozen=True, slots=True)
class ScenarioCar:
    index: int
    name: str
    grid_position: int
    base_lap_time_s: float
    initial_fuel_kg: float
    fuel_per_lap_kg: float
    tyre_wear_per_lap_pct: float
    pit_laps: tuple[int, ...] = ()
    compounds: tuple[str, ...] = ("soft",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pit_laps", tuple(self.pit_laps))
        object.__setattr__(self, "compounds", tuple(self.compounds))
        if not 0 <= self.index < MAX_CARS:
            raise ValueError(f"El índice del coche debe estar entre 0 y {MAX_CARS - 1}")
        if not self.name:
            raise ValueError("El nombre del coche no puede estar vacío")
        if not 1 <= self.grid_position <= MAX_CARS:
            raise ValueError(f"La parrilla debe estar entre 1 y {MAX_CARS}")
        if not math.isfinite(self.base_lap_time_s) or self.base_lap_time_s <= 0:
            raise ValueError("base_lap_time_s debe ser positivo")
        if not math.isfinite(self.initial_fuel_kg) or self.initial_fuel_kg < 0:
            raise ValueError("initial_fuel_kg no puede ser negativo")
        if not math.isfinite(self.fuel_per_lap_kg) or self.fuel_per_lap_kg < 0:
            raise ValueError("fuel_per_lap_kg no puede ser negativo")
        if (
            not math.isfinite(self.tyre_wear_per_lap_pct)
            or not 0 <= self.tyre_wear_per_lap_pct <= 100
        ):
            raise ValueError("tyre_wear_per_lap_pct debe estar entre 0 y 100")
        if tuple(sorted(set(self.pit_laps))) != self.pit_laps:
            raise ValueError("pit_laps debe contener vueltas únicas en orden ascendente")
        if len(self.compounds) != len(self.pit_laps) + 1:
            raise ValueError("Debe indicarse un compuesto por cada stint")
        for compound in self.compounds:
            if compound not in _COMPOUNDS:
                raise ValueError(f"Compuesto no soportado: {compound!r}")
        if len(self.compounds) > 8:
            raise ValueError("El formato admite como máximo 8 stints por coche")


@cache
def _fixture(name: str) -> ParsedPacket:
    parsed = parse_packet((_FIXTURES / name).read_bytes())
    return parsed


def _enum(raw: int, name: str) -> EnumValue:
    return EnumValue(raw, name)


def _validate_race(
    cars: tuple[ScenarioCar, ...],
    total_laps: int,
    hz: int,
    safety_car_laps: tuple[int, int] | None,
    weather_change: tuple[int, str] | None,
    invalid_laps: frozenset[tuple[int, int]],
) -> None:
    if not cars:
        raise ValueError("La carrera debe incluir al menos un coche")
    if len(cars) > MAX_CARS:
        raise ValueError(f"La carrera admite como máximo {MAX_CARS} coches")
    if len({car.index for car in cars}) != len(cars):
        raise ValueError("Los índices de coche deben ser únicos")
    if len({car.grid_position for car in cars}) != len(cars):
        raise ValueError("Las posiciones de parrilla deben ser únicas")
    if total_laps < 1:
        raise ValueError("total_laps debe ser positivo")
    if total_laps > 255:
        raise ValueError("total_laps no puede superar 255")
    if hz < 1:
        raise ValueError("hz debe ser positivo")
    for car in cars:
        if any(not 1 <= lap <= total_laps for lap in car.pit_laps):
            raise ValueError(f"Vueltas de parada fuera de la carrera para {car.name}")
    if safety_car_laps is not None:
        start, end = safety_car_laps
        if not 1 <= start <= end <= total_laps:
            raise ValueError("safety_car_laps debe ser un rango válido dentro de la carrera")
    if weather_change is not None:
        lap, weather = weather_change
        if not 1 <= lap <= total_laps:
            raise ValueError("La vuelta de cambio de clima debe estar dentro de la carrera")
        if weather not in _WEATHER:
            raise ValueError(f"Clima no soportado: {weather!r}")
    for car_index, lap in invalid_laps:
        if car_index not in {car.index for car in cars} or not 1 <= lap <= total_laps:
            raise ValueError(
                f"Vuelta inválida fuera del escenario: coche {car_index}, vuelta {lap}"
            )


def build_race(
    cars: tuple[ScenarioCar, ...] | list[ScenarioCar],
    total_laps: int,
    hz: int = 2,
    *,
    safety_car_laps: tuple[int, int] | None = None,
    weather_change: tuple[int, str] | None = None,
    invalid_laps: frozenset[tuple[int, int]] = frozenset(),
) -> Iterator[ParsedPacket]:
    """Genera paquetes de sesión, estado y cierre de una carrera reproducible.

    `safety_car_laps` usa límites inclusivos. `weather_change` indica la vuelta
    en la que empieza a regir el clima nuevo; `invalid_laps` contiene pares
    `(car_index, lap_number)`.
    """
    scenario_cars = tuple(cars)
    _validate_race(
        scenario_cars,
        total_laps,
        hz,
        safety_car_laps,
        weather_change,
        invalid_laps,
    )
    return _generate_race(
        scenario_cars,
        total_laps,
        hz,
        safety_car_laps,
        weather_change,
        invalid_laps,
    )


def _generate_race(
    cars: tuple[ScenarioCar, ...],
    total_laps: int,
    hz: int,
    safety_car_laps: tuple[int, int] | None,
    weather_change: tuple[int, str] | None,
    invalid_laps: frozenset[tuple[int, int]],
) -> Iterator[ParsedPacket]:
    session_template = _fixture("session.bin")
    participants_template = _fixture("participants.bin")
    lap_template = _fixture("lap_data.bin")
    status_template = _fixture("car_status.bin")
    damage_template = _fixture("car_damage.bin")
    tyres_template = _fixture("tyre_sets.bin")
    history_template = _fixture("session_history.bin")
    classification_template = _fixture("final_classification.bin")
    assert isinstance(session_template, SessionPacket)
    assert isinstance(participants_template, ParticipantsPacket)
    assert isinstance(lap_template, LapDataPacket)
    assert isinstance(status_template, CarStatusPacket)
    assert isinstance(damage_template, CarDamagePacket)
    assert isinstance(tyres_template, TyreSetsPacket)
    assert isinstance(history_template, SessionHistoryPacket)
    assert isinstance(classification_template, FinalClassificationPacket)

    player_index = cars[0].index
    frame = 0
    session_time = -0.001

    def stamped(packet: ParsedPacket, target_time: float) -> ParsedPacket:
        nonlocal frame, session_time
        frame += 1
        session_time = max(target_time, session_time + 0.001)
        header = replace(
            packet.header,
            session_uid=0xF125,
            session_time=session_time,
            frame_identifier=frame,
            overall_frame_identifier=frame,
            player_car_index=player_index,
            secondary_player_car_index=255,
        )
        return replace(packet, header=header)

    participants = list(participants_template.participants)
    for car in cars:
        participant = participants[car.index]
        if participant is None:
            participant = next(item for item in participants if item is not None)
        participants[car.index] = replace(participant, name=car.name)
    participants_packet = replace(
        participants_template,
        active_cars=len(cars),
        participants=tuple(participants),
    )

    safety_lap_count = (
        safety_car_laps[1] - safety_car_laps[0] + 1 if safety_car_laps is not None else 0
    )
    average_lap_s = sum(car.base_lap_time_s for car in cars) / len(cars)
    race_duration = average_lap_s * (total_laps + safety_lap_count * 0.3)
    track_length = session_template.track_length
    session_state = replace(
        session_template,
        total_laps=total_laps,
        session_duration=round(race_duration),
        session_time_left=round(race_duration),
        session_type=_enum(15, "race"),
    )
    yield stamped(session_state, 0.0)
    yield stamped(participants_packet, 0.001)

    closed_laps: dict[int, list[LapHistory]] = {car.index: [] for car in cars}
    for lap in range(1, total_laps + 1):
        under_safety_car = (
            safety_car_laps is not None and safety_car_laps[0] <= lap <= safety_car_laps[1]
        )
        weather = (
            weather_change[1]
            if weather_change is not None and lap >= weather_change[0]
            else "clear"
        )
        weather_value = _enum(_WEATHER[weather], weather)
        steps = max(hz, 2 if any(lap in car.pit_laps for car in cars) else 1)
        for sample in range(steps):
            fraction = (sample + 1) / steps
            completed_sc_laps = (
                max(0, min(lap - 1, safety_car_laps[1]) - safety_car_laps[0] + 1)
                if safety_car_laps is not None
                else 0
            )
            elapsed = average_lap_s * (
                lap
                - 1
                + fraction
                + 0.3 * completed_sc_laps
                + (0.3 * fraction if under_safety_car else 0.0)
            )
            session_packet = replace(
                session_state,
                weather=weather_value,
                safety_car_status=_enum(
                    1 if under_safety_car else 0,
                    "full_safety_car" if under_safety_car else "no_safety_car",
                ),
                session_time_left=max(0, round(race_duration - elapsed)),
            )
            yield stamped(session_packet, elapsed)

            order = sorted(
                cars,
                key=lambda car: (
                    _elapsed_race_time(car, lap, fraction, safety_car_laps)
                    + 20.0
                    * (
                        sum(stop_lap < lap for stop_lap in car.pit_laps)
                        + int(lap in car.pit_laps and sample > 0)
                    ),
                    car.grid_position,
                ),
            )
            positions = {car.index: position for position, car in enumerate(order, start=1)}
            lap_cars = list(lap_template.cars)
            status_cars = list(status_template.cars)
            damage_cars = list(damage_template.cars)

            for car in cars:
                stop_count = sum(stop_lap < lap for stop_lap in car.pit_laps)
                stopping = lap in car.pit_laps and sample == 0
                stop_completed = lap in car.pit_laps and sample > 0
                pit_stops = stop_count + int(stop_completed)
                stint_index = sum(stop_lap <= lap for stop_lap in car.pit_laps)
                stint_start_lap = car.pit_laps[stint_index - 1] if stint_index else 0
                tyre_age = max(0, lap - stint_start_lap - int(stopping))
                fuel = max(
                    0.0,
                    car.initial_fuel_kg - car.fuel_per_lap_kg * ((lap - 1) + fraction),
                )
                lap_time_s = car.base_lap_time_s * (1.3 if under_safety_car else 1.0)
                invalid = (car.index, lap) in invalid_laps

                lap_cars[car.index] = replace(
                    lap_cars[car.index],
                    last_lap_time_ms=round(lap_time_s * 1000) if lap > 1 else 0,
                    current_lap_time_ms=round(lap_time_s * fraction * 1000),
                    sector1_time_ms=round(lap_time_s * fraction * 300),
                    sector2_time_ms=round(lap_time_s * fraction * 650),
                    lap_distance=round(track_length * fraction, 2),
                    total_distance=round(((lap - 1) + fraction) * track_length, 2),
                    car_position=positions[car.index],
                    current_lap_number=lap,
                    pit_status=_enum(
                        1 if stopping else 0,
                        "pitting" if stopping else "none",
                    ),
                    pit_stops=pit_stops,
                    current_lap_invalid=_boolean_value(invalid),
                    grid_position_zero_based=car.grid_position - 1,
                    driver_status=_enum(4, "on_track"),
                    result_status=_enum(2, "active"),
                )
                compound_name = car.compounds[stint_index]
                actual_raw, visual_name = _COMPOUNDS[compound_name]
                visual_raw = {
                    "soft": 16,
                    "medium": 17,
                    "hard": 18,
                    "intermediate": 7,
                    "wet": 8,
                }.get(visual_name, 18)
                wear = min(100.0, car.tyre_wear_per_lap_pct * tyre_age)
                status_cars[car.index] = replace(
                    status_cars[car.index],
                    fuel_in_tank=fuel,
                    fuel_capacity=max(car.initial_fuel_kg, 1.0),
                    fuel_remaining_laps=(
                        fuel / car.fuel_per_lap_kg if car.fuel_per_lap_kg else 0.0
                    ),
                    actual_tyre_compound=_enum(actual_raw, compound_name),
                    visual_tyre_compound=_enum(visual_raw, visual_name),
                    tyres_age_laps=tyre_age,
                )
                damage_cars[car.index] = replace(
                    damage_cars[car.index],
                    tyres_wear=(wear, wear, wear, wear),
                )

            yield stamped(
                replace(lap_template, cars=tuple(lap_cars)),
                elapsed,
            )
            yield stamped(
                replace(status_template, cars=tuple(status_cars)),
                elapsed,
            )
            yield stamped(
                replace(damage_template, cars=tuple(damage_cars)),
                elapsed,
            )

            for car in cars:
                stint_index = sum(stop_lap <= lap for stop_lap in car.pit_laps)
                compound = car.compounds[stint_index]
                actual_raw, visual_name = _COMPOUNDS[compound]
                visual_raw = _visual_compound_raw(visual_name)
                stint_start_lap = car.pit_laps[stint_index - 1] if stint_index else 0
                tyre_age = max(0, lap - stint_start_lap - int(lap in car.pit_laps and sample == 0))
                fitted_set = replace(
                    tyres_template.tyre_sets[0],
                    actual_tyre_compound=_enum(actual_raw, compound),
                    visual_tyre_compound=_enum(visual_raw, visual_name),
                    wear=round(min(100.0, car.tyre_wear_per_lap_pct * tyre_age)),
                )
                tyre_sets = (fitted_set, *tyres_template.tyre_sets[1:])
                yield stamped(
                    replace(
                        tyres_template,
                        car_index=car.index,
                        tyre_sets=tyre_sets,
                        fitted_index=0,
                    ),
                    elapsed,
                )

            if sample == steps - 1:
                for car in cars:
                    lap_time = round(
                        car.base_lap_time_s * (1.3 if under_safety_car else 1.0) * 1000
                    )
                    sector1 = round(lap_time * 0.3)
                    sector2 = round(lap_time * 0.35)
                    closed_laps[car.index].append(
                        LapHistory(
                            lap_time,
                            sector1,
                            sector2,
                            lap_time - sector1 - sector2,
                            0 if (car.index, lap) in invalid_laps else 15,
                        )
                    )
                    stints = _stint_history(car)
                    history = replace(
                        history_template,
                        car_index=car.index,
                        num_laps=min(lap, 100),
                        num_tyre_stints=len(stints),
                        best_lap_time_lap_number=1,
                        best_sector1_lap_number=1,
                        best_sector2_lap_number=1,
                        best_sector3_lap_number=1,
                        lap_history=tuple(closed_laps[car.index][-100:]),
                        tyre_stint_history=stints,
                    )
                    yield stamped(history, elapsed)

    final_order = sorted(
        cars,
        key=lambda car: (
            _elapsed_race_time(car, total_laps, 1.0, safety_car_laps) + 20.0 * len(car.pit_laps),
            car.grid_position,
        ),
    )
    classification: list[FinalClassification | None] = [None] * MAX_CARS
    for position, car in enumerate(final_order, start=1):
        stint_history = _stint_history(car)
        best_lap = round(car.base_lap_time_s * 1000)
        row_template = classification_template.classification[0]
        assert row_template is not None
        row = replace(
            row_template,
            position=position,
            laps=total_laps,
            grid_position=car.grid_position,
            pit_stops=len(car.pit_laps),
            result_status=_enum(3, "finished"),
            result_reason=_enum(2, "finished"),
            best_lap_time_ms=best_lap,
            total_race_time=(
                _elapsed_race_time(car, total_laps, 1.0, safety_car_laps) + 20.0 * len(car.pit_laps)
            ),
            num_tyre_stints=len(stint_history),
            tyre_stints_actual=tuple(item.actual_compound for item in stint_history),
            tyre_stints_visual=tuple(item.visual_compound for item in stint_history),
            tyre_stints_end_laps=tuple(item.end_lap for item in stint_history),
        )
        classification[position - 1] = row
    final_packet = replace(
        classification_template,
        num_cars=len(cars),
        classification=tuple(classification),
    )
    yield stamped(final_packet, race_duration)


def _boolean_value(value: bool) -> BooleanValue:
    return BooleanValue(int(value), value)


def _stint_history(car: ScenarioCar) -> tuple[TyreStintHistory, ...]:
    stints = []
    for index, compound in enumerate(car.compounds):
        raw, visual = _COMPOUNDS[compound]
        visual_raw = _visual_compound_raw(visual)
        end_lap = car.pit_laps[index] if index < len(car.pit_laps) else None
        stints.append(
            TyreStintHistory(
                _enum(raw, compound),
                _enum(visual_raw, visual),
                end_lap,
            )
        )
    return tuple(stints)


def _visual_compound_raw(name: str) -> int:
    return {"soft": 16, "medium": 17, "hard": 18}.get(name, 18)


def _elapsed_race_time(
    car: ScenarioCar,
    lap: int,
    fraction: float,
    safety_car_laps: tuple[int, int] | None,
) -> float:
    completed_laps = lap - 1
    elapsed = car.base_lap_time_s * completed_laps
    if safety_car_laps is not None:
        sc_completed = max(
            0,
            min(completed_laps, safety_car_laps[1]) - safety_car_laps[0] + 1,
        )
        elapsed += car.base_lap_time_s * 0.3 * sc_completed
        if safety_car_laps[0] <= lap <= safety_car_laps[1]:
            elapsed += car.base_lap_time_s * 1.3 * fraction
        else:
            elapsed += car.base_lap_time_s * fraction
    else:
        elapsed += car.base_lap_time_s * fraction
    return elapsed
