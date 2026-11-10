"""Generación determinista de carreras sintéticas con modelos del parser."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from types import MappingProxyType

from f1_strategist.parser import (
    MAX_CARS,
    BooleanValue,
    CarDamagePacket,
    CarStatusPacket,
    EnumValue,
    EventPacket,
    FinalClassification,
    FinalClassificationPacket,
    FlashbackDetail,
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
_VISUAL_COMPOUND_RAW = {
    "soft": 16,
    "medium": 17,
    "hard": 18,
    "intermediate": 7,
    "wet": 8,
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
    pace_slope_s_per_lap: float = 0.0
    lap_time_overrides: Mapping[int, float] = field(default_factory=dict)
    ers_harvest_per_lap_j: float = 0.0
    ers_deploy_per_lap_j: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "pit_laps", tuple(self.pit_laps))
        object.__setattr__(self, "compounds", tuple(self.compounds))
        object.__setattr__(
            self, "lap_time_overrides", MappingProxyType(dict(self.lap_time_overrides))
        )
        if (
            not isinstance(self.index, int)
            or isinstance(self.index, bool)
            or not 0 <= self.index < MAX_CARS
        ):
            raise ValueError(f"El índice del coche debe estar entre 0 y {MAX_CARS - 1}")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("El nombre del coche no puede estar vacío")
        if (
            not isinstance(self.grid_position, int)
            or isinstance(self.grid_position, bool)
            or not 1 <= self.grid_position <= MAX_CARS
        ):
            raise ValueError(f"La parrilla debe estar entre 1 y {MAX_CARS}")
        if (
            not isinstance(self.base_lap_time_s, (int, float))
            or isinstance(self.base_lap_time_s, bool)
            or not math.isfinite(self.base_lap_time_s)
            or self.base_lap_time_s <= 0
        ):
            raise ValueError("base_lap_time_s debe ser positivo")
        if (
            not isinstance(self.initial_fuel_kg, (int, float))
            or isinstance(self.initial_fuel_kg, bool)
            or not math.isfinite(self.initial_fuel_kg)
            or self.initial_fuel_kg < 0
        ):
            raise ValueError("initial_fuel_kg no puede ser negativo")
        if (
            not isinstance(self.fuel_per_lap_kg, (int, float))
            or isinstance(self.fuel_per_lap_kg, bool)
            or not math.isfinite(self.fuel_per_lap_kg)
            or self.fuel_per_lap_kg < 0
        ):
            raise ValueError("fuel_per_lap_kg no puede ser negativo")
        if (
            not isinstance(self.tyre_wear_per_lap_pct, (int, float))
            or isinstance(self.tyre_wear_per_lap_pct, bool)
            or not math.isfinite(self.tyre_wear_per_lap_pct)
            or not 0 <= self.tyre_wear_per_lap_pct <= 100
        ):
            raise ValueError("tyre_wear_per_lap_pct debe estar entre 0 y 100")
        if (
            not isinstance(self.pace_slope_s_per_lap, (int, float))
            or isinstance(self.pace_slope_s_per_lap, bool)
            or not math.isfinite(self.pace_slope_s_per_lap)
        ):
            raise ValueError("pace_slope_s_per_lap debe ser finito")
        if (
            not isinstance(self.ers_harvest_per_lap_j, (int, float))
            or isinstance(self.ers_harvest_per_lap_j, bool)
            or not math.isfinite(self.ers_harvest_per_lap_j)
            or self.ers_harvest_per_lap_j < 0
        ):
            raise ValueError("ers_harvest_per_lap_j no puede ser negativo")
        if (
            not isinstance(self.ers_deploy_per_lap_j, (int, float))
            or isinstance(self.ers_deploy_per_lap_j, bool)
            or not math.isfinite(self.ers_deploy_per_lap_j)
            or self.ers_deploy_per_lap_j < 0
        ):
            raise ValueError("ers_deploy_per_lap_j no puede ser negativo")
        for lap, lap_time in self.lap_time_overrides.items():
            if (
                not isinstance(lap, int)
                or isinstance(lap, bool)
                or not isinstance(lap_time, (int, float))
                or isinstance(lap_time, bool)
                or not math.isfinite(lap_time)
                or lap < 1
                or lap_time <= 0
            ):
                raise ValueError("lap_time_overrides requiere vueltas y tiempos positivos")
        if any(not isinstance(lap, int) or isinstance(lap, bool) for lap in self.pit_laps):
            raise ValueError("pit_laps debe contener números de vuelta enteros")
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


def _expect_fixture[PacketT: ParsedPacket](name: str, packet_type: type[PacketT]) -> PacketT:
    packet = _fixture(name)
    if not isinstance(packet, packet_type):
        raise TypeError(f"{name}: expected {packet_type.__name__}, got {type(packet).__name__}")
    return packet


def _enum(raw: int, name: str) -> EnumValue:
    return EnumValue(raw, name)


def _validate_race(
    cars: tuple[ScenarioCar, ...],
    total_laps: int,
    samples_per_lap: int,
    sample_interval_s: float | None,
    safety_car_laps: tuple[int, int] | None,
    weather_change: tuple[int, str] | None,
    invalid_laps: frozenset[tuple[int, int]],
    pit_loss_s: float,
    pit_lane_time_s: float,
    pit_stationary_s: float,
    gap_ahead_s: Mapping[tuple[int, int], tuple[float, float]],
    damage_events: Mapping[tuple[int, int], Mapping[str, int]],
    retirements: Mapping[int, int],
    flashback: tuple[int, int] | None,
) -> None:
    if not cars:
        raise ValueError("La carrera debe incluir al menos un coche")
    if len(cars) > MAX_CARS:
        raise ValueError(f"La carrera admite como máximo {MAX_CARS} coches")
    if len({car.index for car in cars}) != len(cars):
        raise ValueError("Los índices de coche deben ser únicos")
    if len({car.grid_position for car in cars}) != len(cars):
        raise ValueError("Las posiciones de parrilla deben ser únicas")
    if not isinstance(total_laps, int) or isinstance(total_laps, bool) or total_laps < 1:
        raise ValueError("total_laps debe ser positivo")
    if total_laps > 255:
        raise ValueError("total_laps no puede superar 255")
    if (
        not isinstance(samples_per_lap, int)
        or isinstance(samples_per_lap, bool)
        or samples_per_lap < 1
    ):
        raise ValueError("samples_per_lap debe ser positivo")
    if sample_interval_s is not None and (
        not isinstance(sample_interval_s, (int, float))
        or isinstance(sample_interval_s, bool)
        or not math.isfinite(sample_interval_s)
        or sample_interval_s <= 0
    ):
        raise ValueError("sample_interval_s debe ser positivo y finito")
    for name, value in (
        ("pit_loss_s", pit_loss_s),
        ("pit_lane_time_s", pit_lane_time_s),
        ("pit_stationary_s", pit_stationary_s),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} debe ser no negativo y finito")
    if pit_stationary_s > pit_lane_time_s:
        raise ValueError("pit_stationary_s no puede superar pit_lane_time_s")
    car_indices = {car.index for car in cars}
    for car in cars:
        if any(not 1 <= lap <= total_laps for lap in car.pit_laps):
            raise ValueError(f"Vueltas de parada fuera de la carrera para {car.name}")
        if any(lap > total_laps for lap in car.lap_time_overrides):
            raise ValueError(f"Override de tiempo fuera de la carrera para {car.name}")
    if safety_car_laps is not None:
        if len(safety_car_laps) != 2:
            raise ValueError("safety_car_laps debe tener dos límites")
        start, end = safety_car_laps
        if not 1 <= start <= end <= total_laps:
            raise ValueError("safety_car_laps debe ser un rango válido dentro de la carrera")
    if weather_change is not None:
        if len(weather_change) != 2:
            raise ValueError("weather_change debe contener vuelta y clima")
        lap, weather = weather_change
        if not 1 <= lap <= total_laps:
            raise ValueError("La vuelta de cambio de clima debe estar dentro de la carrera")
        if weather not in _WEATHER:
            raise ValueError(f"Clima no soportado: {weather!r}")
    for car_index, lap in invalid_laps:
        if car_index not in car_indices or not 1 <= lap <= total_laps:
            raise ValueError(
                f"Vuelta inválida fuera del escenario: coche {car_index}, vuelta {lap}"
            )
    for (car_index, lap), values in gap_ahead_s.items():
        if car_index not in car_indices or not 1 <= lap <= total_laps:
            raise ValueError(
                f"Override de intervalo fuera del escenario: coche {car_index}, vuelta {lap}"
            )
        if len(values) != 2 or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in values
        ):
            raise ValueError("gap_ahead_s debe contener pares de tiempos finitos")
    damage_fields = {
        "front_left_wing_damage",
        "front_right_wing_damage",
        "rear_wing_damage",
        "floor_damage",
        "diffuser_damage",
        "sidepod_damage",
        "drs_fault",
        "ers_fault",
        "gearbox_damage",
        "engine_damage",
        "engine_mguh_wear",
        "engine_es_wear",
        "engine_ce_wear",
        "engine_ice_wear",
        "engine_mguk_wear",
        "engine_tc_wear",
        "engine_blown",
        "engine_seized",
    }
    for (car_index, lap), changes in damage_events.items():
        if car_index not in car_indices or not 1 <= lap <= total_laps:
            raise ValueError(f"Evento de daño fuera del escenario: coche {car_index}, vuelta {lap}")
        for field_name, value in changes.items():
            if field_name not in damage_fields:
                raise ValueError(f"Campo de daño no soportado: {field_name!r}")
            is_fault = field_name in {"drs_fault", "ers_fault", "engine_blown", "engine_seized"}
            upper_bound = 1 if is_fault else 100
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= upper_bound
            ):
                raise ValueError(f"El daño {field_name} debe ser un entero entre 0 y 100")
    for car_index, lap in retirements.items():
        if (
            not isinstance(car_index, int)
            or isinstance(car_index, bool)
            or not isinstance(lap, int)
            or isinstance(lap, bool)
            or car_index not in car_indices
            or not 1 <= lap <= total_laps
        ):
            raise ValueError(f"Retirada fuera del escenario: coche {car_index}, vuelta {lap}")
        if any(
            lap in (pit_lap, pit_lap + 1)
            for pit_lap in next(car.pit_laps for car in cars if car.index == car_index)
        ):
            raise ValueError("Una retirada no puede coincidir con una parada")
    if flashback is not None:
        if len(flashback) != 2:
            raise ValueError("flashback debe contener vuelta de origen y vuelta de destino")
        rewind_from, rewind_to = flashback
        if not 1 <= rewind_to < rewind_from <= total_laps:
            raise ValueError(
                "flashback debe cumplir 1 <= vuelta_hasta < vuelta_desde <= total_laps"
            )


def build_race(
    cars: tuple[ScenarioCar, ...] | list[ScenarioCar],
    total_laps: int,
    samples_per_lap: int = 2,
    *,
    sample_interval_s: float | None = None,
    safety_car_laps: tuple[int, int] | None = None,
    weather_change: tuple[int, str] | None = None,
    invalid_laps: frozenset[tuple[int, int]] = frozenset(),
    pit_loss_s: float = 20.0,
    pit_lane_time_s: float = 22.0,
    pit_stationary_s: float = 2.5,
    gap_ahead_s: Mapping[tuple[int, int], tuple[float, float]] | None = None,
    damage_events: Mapping[tuple[int, int], Mapping[str, int]] | None = None,
    retirements: Mapping[int, int] | None = None,
    flashback: tuple[int, int] | None = None,
) -> Iterator[ParsedPacket]:
    """Genera paquetes reproducibles; pit_laps señala la vuelta de entrada.

    `safety_car_laps` usa límites inclusivos. `weather_change` indica la vuelta
    en la que empieza a regir el clima nuevo; `invalid_laps`, `gap_ahead_s` y
    `damage_events` se indexan por coche y vuelta. La mitad de `pit_loss_s` se
    añade a la vuelta de entrada y a la de salida, para que ambas juntas tengan
    exactamente esa pérdida extra frente a dos vueltas de referencia.
    """
    scenario_cars = tuple(cars)
    gaps = dict(gap_ahead_s or {})
    damages = {key: dict(value) for key, value in (damage_events or {}).items()}
    retired = dict(retirements or {})
    _validate_race(
        scenario_cars,
        total_laps,
        samples_per_lap,
        sample_interval_s,
        safety_car_laps,
        weather_change,
        invalid_laps,
        pit_loss_s,
        pit_lane_time_s,
        pit_stationary_s,
        gaps,
        damages,
        retired,
        flashback,
    )
    return _generate_race(
        scenario_cars,
        total_laps,
        samples_per_lap,
        sample_interval_s,
        safety_car_laps,
        weather_change,
        invalid_laps,
        pit_loss_s,
        pit_lane_time_s,
        pit_stationary_s,
        gaps,
        damages,
        retired,
        flashback,
    )


def build_race_timed(
    cars: tuple[ScenarioCar, ...] | list[ScenarioCar],
    total_laps: int,
    samples_per_lap: int = 2,
    *,
    sample_interval_s: float | None = None,
    safety_car_laps: tuple[int, int] | None = None,
    weather_change: tuple[int, str] | None = None,
    invalid_laps: frozenset[tuple[int, int]] = frozenset(),
    pit_loss_s: float = 20.0,
    pit_lane_time_s: float = 22.0,
    pit_stationary_s: float = 2.5,
    gap_ahead_s: Mapping[tuple[int, int], tuple[float, float]] | None = None,
    damage_events: Mapping[tuple[int, int], Mapping[str, int]] | None = None,
    retirements: Mapping[int, int] | None = None,
    flashback: tuple[int, int] | None = None,
) -> Iterator[tuple[ParsedPacket, float]]:
    """Genera paquetes con un reloj observado estrictamente monótono."""
    observed_at: float | None = None
    for packet in build_race(
        cars,
        total_laps,
        samples_per_lap,
        sample_interval_s=sample_interval_s,
        safety_car_laps=safety_car_laps,
        weather_change=weather_change,
        invalid_laps=invalid_laps,
        pit_loss_s=pit_loss_s,
        pit_lane_time_s=pit_lane_time_s,
        pit_stationary_s=pit_stationary_s,
        gap_ahead_s=gap_ahead_s,
        damage_events=damage_events,
        retirements=retirements,
        flashback=flashback,
    ):
        observed_at = (
            packet.header.session_time
            if observed_at is None
            else max(packet.header.session_time, observed_at + 0.001)
        )
        yield packet, observed_at


def build_weekend_timed(
    cars: tuple[ScenarioCar, ...] | list[ScenarioCar],
    total_laps: int,
    samples_per_lap: int = 2,
) -> Iterator[tuple[ParsedPacket, float]]:
    """Genera práctica, clasificación y carrera del mismo fin de semana.

    Cada sesión conserva el mismo circuito, temporada y fin de semana, y usa
    un ``session_link_identifier`` distinto. Los UID y el reloj observado son
    distintos entre sesiones para ejercitar los límites del pipeline.
    """
    session_specs = (
        (0xF12501, "practice_1", 1),
        (0xF12502, "qualifying_1", 5),
        (0xF12503, "race", 15),
    )
    weekend_structure = (
        EnumValue(1, "practice_1"),
        EnumValue(5, "qualifying_1"),
        EnumValue(15, "race"),
    )
    observed_offset = 0.0
    for session_uid, session_name, session_type in session_specs:
        first_observed_at: float | None = None
        last_relative_time = 0.0
        for packet, observed_at in build_race_timed(cars, total_laps, samples_per_lap):
            if first_observed_at is None:
                first_observed_at = observed_at
            relative_time = observed_at - first_observed_at
            packet = replace(
                packet,
                header=replace(packet.header, session_uid=session_uid),
            )
            if isinstance(packet, SessionPacket):
                packet = replace(
                    packet,
                    session_type=EnumValue(session_type, session_name),
                    season_link_identifier=2026,
                    weekend_link_identifier=1,
                    session_link_identifier=session_type,
                    weekend_structure=weekend_structure,
                )
            yield packet, relative_time + observed_offset
            last_relative_time = relative_time
        observed_offset += last_relative_time + 0.001


def _generate_race(
    cars: tuple[ScenarioCar, ...],
    total_laps: int,
    samples_per_lap: int,
    sample_interval_s: float | None,
    safety_car_laps: tuple[int, int] | None,
    weather_change: tuple[int, str] | None,
    invalid_laps: frozenset[tuple[int, int]],
    pit_loss_s: float,
    pit_lane_time_s: float,
    pit_stationary_s: float,
    gap_ahead_s: Mapping[tuple[int, int], tuple[float, float]],
    damage_events: Mapping[tuple[int, int], Mapping[str, int]],
    retirements: Mapping[int, int],
    flashback: tuple[int, int] | None,
) -> Iterator[ParsedPacket]:
    session_template = _expect_fixture("session.bin", SessionPacket)
    participants_template = _expect_fixture("participants.bin", ParticipantsPacket)
    lap_template = _expect_fixture("lap_data.bin", LapDataPacket)
    status_template = _expect_fixture("car_status.bin", CarStatusPacket)
    damage_template = _expect_fixture("car_damage.bin", CarDamagePacket)
    tyres_template = _expect_fixture("tyre_sets.bin", TyreSetsPacket)
    history_template = _expect_fixture("session_history.bin", SessionHistoryPacket)
    classification_template = _expect_fixture("final_classification.bin", FinalClassificationPacket)
    event_template = _expect_fixture("event.bin", EventPacket)

    player_index = cars[0].index
    frame_id = 0
    overall_frame_id = 0
    session_time = -0.001

    def stamped(
        packet: ParsedPacket,
        target_time: float,
        *,
        allow_rewind: bool = False,
        frame_override: int | None = None,
    ) -> ParsedPacket:
        nonlocal frame_id, overall_frame_id, session_time
        overall_frame_id += 1
        if frame_override is None:
            frame_id += 1
        else:
            frame_id = frame_override
        session_time = target_time if allow_rewind else max(target_time, session_time + 0.001)
        header = replace(
            packet.header,
            session_uid=0xF125,
            session_time=session_time,
            frame_identifier=frame_id,
            overall_frame_identifier=overall_frame_id,
            player_car_index=player_index,
            secondary_player_car_index=255,
        )
        return replace(packet, header=header)

    def lap_time(car: ScenarioCar, lap: int) -> float:
        stint_index = sum(stop_lap < lap for stop_lap in car.pit_laps)
        stint_start = car.pit_laps[stint_index - 1] + 1 if stint_index else 1
        tyre_age = lap - stint_start
        clean_time = car.lap_time_overrides.get(
            lap, car.base_lap_time_s + car.pace_slope_s_per_lap * tyre_age
        )
        under_sc = safety_car_laps is not None and safety_car_laps[0] <= lap <= safety_car_laps[1]
        pit_extra = pit_loss_s / 2 if lap in car.pit_laps or lap - 1 in car.pit_laps else 0.0
        return clean_time * (1.3 if under_sc else 1.0) + pit_extra

    def samples_for(lap: int) -> tuple[float, ...]:
        average = sum(lap_time(car, lap) for car in cars) / len(cars)
        count = (
            max(1, math.ceil(average / sample_interval_s))
            if sample_interval_s is not None
            else samples_per_lap
        )
        return tuple((sample + 1) / count for sample in range(count))

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

    lap_durations = [
        sum(lap_time(car, lap) for car in cars) / len(cars) for lap in range(1, total_laps + 1)
    ]
    race_duration = sum(lap_durations)
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
    lap_start_times: dict[int, float] = {}
    lap_start_frames: dict[int, int] = {}
    lap_start_history: dict[int, dict[int, tuple[LapHistory, ...]]] = {}
    elapsed = 0.001
    last_lap_rows = list(lap_template.cars)
    last_status_rows = list(status_template.cars)
    last_damage_rows = list(damage_template.cars)

    def elapsed_at(car: ScenarioCar, lap: int, fraction: float) -> float:
        actual_lap = min(lap, retirements.get(car.index, total_laps + 1))
        return sum(lap_time(car, previous) for previous in range(1, actual_lap)) + (
            lap_time(car, actual_lap) * (fraction if actual_lap == lap else 1.0)
        )

    def emit_lap(lap: int, fractions: tuple[float, ...], close_lap: bool) -> Iterator[ParsedPacket]:
        nonlocal elapsed, last_lap_rows, last_status_rows, last_damage_rows
        lap_start_times.setdefault(lap, elapsed)
        lap_start_frames.setdefault(lap, frame_id + 1)
        lap_start_history.setdefault(
            lap, {index: tuple(history) for index, history in closed_laps.items()}
        )
        average_lap_s = lap_durations[lap - 1]
        weather = (
            weather_change[1]
            if weather_change is not None and lap >= weather_change[0]
            else "clear"
        )
        weather_value = _enum(_WEATHER[weather], weather)
        for sample_index, fraction in enumerate(fractions):
            sample_time = elapsed + average_lap_s * fraction
            session_packet = replace(
                session_state,
                weather=weather_value,
                safety_car_status=_enum(
                    int(
                        safety_car_laps is not None
                        and safety_car_laps[0] <= lap <= safety_car_laps[1]
                    ),
                    "full_safety_car"
                    if safety_car_laps is not None
                    and safety_car_laps[0] <= lap <= safety_car_laps[1]
                    else "no_safety_car",
                ),
                session_time_left=max(0, round(race_duration - sample_time)),
            )
            yield stamped(session_packet, sample_time)

            order = sorted(
                cars,
                key=lambda car: (elapsed_at(car, lap, fraction), car.grid_position),
            )
            positions = {car.index: position for position, car in enumerate(order, start=1)}
            lap_cars = list(last_lap_rows)
            status_cars = list(last_status_rows)
            damage_cars = list(last_damage_rows)
            order_times = {car.index: elapsed_at(car, lap, fraction) for car in order}

            for car in cars:
                retirement_lap = retirements.get(car.index)
                if retirement_lap is not None and lap > retirement_lap:
                    continue
                stop_count = sum(stop_lap < lap for stop_lap in car.pit_laps)
                stint_index = stop_count
                stint_start_lap = car.pit_laps[stint_index - 1] + 1 if stint_index else 1
                tyre_age = lap - stint_start_lap
                compound_name = car.compounds[stint_index]
                actual_raw, visual_name = _COMPOUNDS[compound_name]
                visual_raw = _VISUAL_COMPOUND_RAW[visual_name]
                current_lap_s = lap_time(car, lap)
                previous_lap_ms = round(lap_time(car, lap - 1) * 1000) if lap > 1 else 0
                fuel = max(
                    0.0,
                    car.initial_fuel_kg - car.fuel_per_lap_kg * ((lap - 1) + fraction),
                )
                entering_lane = lap in car.pit_laps and sample_index == len(fractions) - 1
                exiting_lane = lap - 1 in car.pit_laps and sample_index == 0
                in_lane = entering_lane or exiting_lane
                is_retired = retirement_lap is not None and lap >= retirement_lap
                invalid = (car.index, lap) in invalid_laps
                ahead_index = (
                    order[positions[car.index] - 2].index if positions[car.index] > 1 else None
                )
                ahead_gap_s = (
                    order_times[car.index] - order_times[ahead_index]
                    if ahead_index is not None
                    else 0.0
                )
                override = gap_ahead_s.get((car.index, lap))
                if override is not None:
                    ahead_gap_s = override[0] + (override[1] - override[0]) * fraction
                leader_gap_s = order_times[car.index] - order_times[order[0].index]

                lap_cars[car.index] = replace(
                    lap_cars[car.index],
                    last_lap_time_ms=previous_lap_ms,
                    current_lap_time_ms=round(current_lap_s * fraction * 1000),
                    sector1_time_ms=round(current_lap_s * fraction * 300),
                    sector2_time_ms=round(current_lap_s * fraction * 650),
                    delta_to_car_in_front_ms=round(ahead_gap_s * 1000),
                    delta_to_race_leader_ms=round(leader_gap_s * 1000),
                    lap_distance=round(track_length * fraction, 2),
                    total_distance=round(((lap - 1) + fraction) * track_length, 2),
                    car_position=positions[car.index],
                    current_lap_number=lap,
                    pit_status=_enum(1 if in_lane else 0, "pitting" if in_lane else "none"),
                    pit_stops=stop_count,
                    current_lap_invalid=_boolean_value(invalid),
                    grid_position_zero_based=car.grid_position - 1,
                    driver_status=_enum(2 if in_lane else 4, "in_lap" if in_lane else "on_track"),
                    result_status=_enum(7, "retired") if is_retired else _enum(2, "active"),
                    pit_lane_timer_active=_boolean_value(in_lane),
                    pit_lane_time_ms=round(pit_lane_time_s * 1000) if in_lane else 0,
                    pit_stop_timer_ms=round(pit_stationary_s * 1000) if in_lane else 0,
                )

                wear = min(100.0, car.tyre_wear_per_lap_pct * (tyre_age + fraction))
                status_car = status_cars[car.index]
                prior_net = sum(
                    car.ers_harvest_per_lap_j - car.ers_deploy_per_lap_j
                    for completed_lap in range(1, lap)
                )
                capacity = 4_000_000.0
                initial_charge = status_template.cars[car.index].ers_store_energy
                ers_store = min(
                    capacity,
                    max(
                        0.0,
                        initial_charge
                        + prior_net
                        + (car.ers_harvest_per_lap_j - car.ers_deploy_per_lap_j) * fraction,
                    ),
                )
                status_cars[car.index] = replace(
                    status_car,
                    fuel_in_tank=fuel,
                    fuel_capacity=max(car.initial_fuel_kg, 1.0),
                    fuel_remaining_laps=(
                        fuel / car.fuel_per_lap_kg if car.fuel_per_lap_kg else 0.0
                    ),
                    actual_tyre_compound=_enum(actual_raw, compound_name),
                    visual_tyre_compound=_enum(visual_raw, visual_name),
                    tyres_age_laps=tyre_age,
                    ers_store_energy=ers_store,
                    ers_harvested_this_lap_mguk=car.ers_harvest_per_lap_j * fraction,
                    ers_deployed_this_lap=car.ers_deploy_per_lap_j * fraction,
                )

                updates: dict[str, int | EnumValue] = {}
                for (event_car, event_lap), changes in damage_events.items():
                    if event_car != car.index or event_lap > lap:
                        continue
                    for field_name, value in changes.items():
                        updates[field_name] = (
                            _enum(value, "fault" if value else "none")
                            if field_name
                            in {"drs_fault", "ers_fault", "engine_blown", "engine_seized"}
                            else value
                        )
                damage_car = damage_cars[car.index]
                damage_cars[car.index] = replace(
                    damage_car,
                    tyres_wear=(wear, wear, wear, wear),
                    **updates,
                )

            last_lap_rows = lap_cars
            last_status_rows = status_cars
            last_damage_rows = damage_cars
            yield stamped(replace(lap_template, cars=tuple(lap_cars)), sample_time)
            yield stamped(replace(status_template, cars=tuple(status_cars)), sample_time)
            yield stamped(replace(damage_template, cars=tuple(damage_cars)), sample_time)

            for car in cars:
                retirement_lap = retirements.get(car.index)
                if retirement_lap is not None and lap > retirement_lap:
                    continue
                stint_index = sum(stop_lap < lap for stop_lap in car.pit_laps)
                compound = car.compounds[stint_index]
                actual_raw, visual_name = _COMPOUNDS[compound]
                visual_raw = _VISUAL_COMPOUND_RAW[visual_name]
                stint_start_lap = car.pit_laps[stint_index - 1] + 1 if stint_index else 1
                tyre_age = lap - stint_start_lap
                wear = min(100.0, car.tyre_wear_per_lap_pct * (tyre_age + fraction))
                fitted_set = replace(
                    tyres_template.tyre_sets[0],
                    actual_tyre_compound=_enum(actual_raw, compound),
                    visual_tyre_compound=_enum(visual_raw, visual_name),
                    wear=round(wear),
                )
                tyre_sets = (fitted_set, *tyres_template.tyre_sets[1:])
                yield stamped(
                    replace(
                        tyres_template,
                        car_index=car.index,
                        tyre_sets=tyre_sets,
                        fitted_index=0,
                    ),
                    sample_time,
                )

            if close_lap and sample_index == len(fractions) - 1:
                for car in cars:
                    retirement_lap = retirements.get(car.index)
                    if retirement_lap is not None and lap >= retirement_lap:
                        continue
                    lap_ms = round(lap_time(car, lap) * 1000)
                    sector1 = round(lap_ms * 0.3)
                    sector2 = round(lap_ms * 0.35)
                    closed_laps[car.index].append(
                        LapHistory(
                            lap_ms,
                            sector1,
                            sector2,
                            lap_ms - sector1 - sector2,
                            0 if (car.index, lap) in invalid_laps else 15,
                        )
                    )
                    stints = _stint_history(car)
                    history_times = [item.lap_time_ms for item in closed_laps[car.index]]
                    best_lap_no = history_times.index(min(history_times)) + 1
                    history = replace(
                        history_template,
                        car_index=car.index,
                        num_laps=min(len(history_times), 100),
                        num_tyre_stints=len(stints),
                        best_lap_time_lap_number=best_lap_no,
                        best_sector1_lap_number=best_lap_no,
                        best_sector2_lap_number=best_lap_no,
                        best_sector3_lap_number=best_lap_no,
                        lap_history=tuple(closed_laps[car.index][-100:]),
                        tyre_stint_history=stints,
                    )
                    yield stamped(history, sample_time)
        if close_lap:
            elapsed += average_lap_s

    for lap in range(1, total_laps + 1):
        fractions = samples_for(lap)
        if flashback is not None and lap == flashback[0]:
            halfway = next(index + 1 for index, fraction in enumerate(fractions) if fraction >= 0.5)
            partial_fractions = list(fractions[:halfway])
            if partial_fractions[-1] > 0.5:
                partial_fractions[-1] = 0.5
            yield from emit_lap(lap, tuple(partial_fractions), close_lap=False)
            target_lap = flashback[1]
            target_time = lap_start_times[target_lap]
            target_frame = lap_start_frames[target_lap]
            flashback_event = replace(
                event_template,
                event_code=EnumValue("FLBK", "flashback"),
                detail=FlashbackDetail(target_frame, target_time),
            )
            yield stamped(
                flashback_event,
                target_time,
                allow_rewind=True,
                frame_override=target_frame,
            )
            for car_index, history in lap_start_history[target_lap].items():
                closed_laps[car_index] = list(history)
            elapsed = target_time
            for replay_lap in range(target_lap, total_laps + 1):
                yield from emit_lap(replay_lap, samples_for(replay_lap), close_lap=True)
            break
        yield from emit_lap(lap, fractions, close_lap=True)

    def completed_race_time(car: ScenarioCar) -> float:
        retirement_lap = retirements.get(car.index)
        laps_completed = retirement_lap - 1 if retirement_lap is not None else total_laps
        return sum(lap_time(car, lap) for lap in range(1, laps_completed + 1))

    final_order = sorted(cars, key=lambda car: (completed_race_time(car), car.grid_position))
    classification: list[FinalClassification | None] = [None] * MAX_CARS
    for position, car in enumerate(final_order, start=1):
        stints = _stint_history(car)
        retirement_lap = retirements.get(car.index)
        laps_completed = retirement_lap - 1 if retirement_lap is not None else total_laps
        result_status = _enum(7, "retired") if retirement_lap is not None else _enum(3, "finished")
        result_reason = _enum(1, "retired") if retirement_lap is not None else _enum(2, "finished")
        history = closed_laps[car.index]
        best_lap = min(
            (item.lap_time_ms for item in history), default=round(car.base_lap_time_s * 1000)
        )
        row_template = classification_template.classification[0]
        assert row_template is not None
        row = replace(
            row_template,
            position=position,
            laps=laps_completed,
            grid_position=car.grid_position,
            pit_stops=sum(stop_lap < laps_completed for stop_lap in car.pit_laps),
            result_status=result_status,
            result_reason=result_reason,
            best_lap_time_ms=best_lap,
            total_race_time=completed_race_time(car),
            num_tyre_stints=len(stints),
            tyre_stints_actual=tuple(item.actual_compound for item in stints),
            tyre_stints_visual=tuple(item.visual_compound for item in stints),
            tyre_stints_end_laps=tuple(item.end_lap for item in stints),
        )
        classification[position - 1] = row
    yield stamped(
        replace(
            classification_template,
            num_cars=len(cars),
            classification=tuple(classification),
        ),
        elapsed,
    )


def _boolean_value(value: bool) -> BooleanValue:
    return BooleanValue(int(value), value)


def _stint_history(car: ScenarioCar) -> tuple[TyreStintHistory, ...]:
    stints = []
    for index, compound in enumerate(car.compounds):
        raw, visual = _COMPOUNDS[compound]
        visual_raw = _VISUAL_COMPOUND_RAW[visual]
        end_lap = car.pit_laps[index] if index < len(car.pit_laps) else None
        stints.append(
            TyreStintHistory(
                _enum(raw, compound),
                _enum(visual_raw, visual),
                end_lap,
            )
        )
    return tuple(stints)
