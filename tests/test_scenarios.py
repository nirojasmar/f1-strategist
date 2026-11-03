from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.parser import (
    CarDamagePacket,
    CarStatusPacket,
    FinalClassificationPacket,
    LapDataPacket,
    ParticipantsPacket,
    SessionHistoryPacket,
    SessionPacket,
    TyreSetsPacket,
)
from f1_strategist.race_state import RaceState

from . import scenarios as scenarios_module
from .scenarios import ScenarioCar, build_race


@pytest.fixture
def race_cars() -> tuple[ScenarioCar, ...]:
    return (
        ScenarioCar(
            index=0,
            name="Driver A",
            grid_position=2,
            base_lap_time_s=91.0,
            initial_fuel_kg=30.0,
            fuel_per_lap_kg=2.0,
            tyre_wear_per_lap_pct=4.0,
            pit_laps=(3,),
            compounds=("soft", "medium"),
        ),
        ScenarioCar(
            index=1,
            name="Driver B",
            grid_position=1,
            base_lap_time_s=90.0,
            initial_fuel_kg=31.0,
            fuel_per_lap_kg=2.1,
            tyre_wear_per_lap_pct=3.0,
        ),
    )


def test_scenario_emits_all_required_packet_types_and_coherent_headers(
    race_cars: tuple[ScenarioCar, ...],
) -> None:
    packets = list(
        build_race(
            race_cars,
            total_laps=5,
            safety_car_laps=(2, 2),
            weather_change=(4, "light_rain"),
            invalid_laps=frozenset({(1, 3)}),
        )
    )

    assert isinstance(packets[0], SessionPacket)
    assert isinstance(packets[1], ParticipantsPacket)
    assert any(isinstance(packet, LapDataPacket) for packet in packets)
    assert any(isinstance(packet, CarStatusPacket) for packet in packets)
    assert any(isinstance(packet, CarDamagePacket) for packet in packets)
    assert any(isinstance(packet, TyreSetsPacket) for packet in packets)
    assert sum(isinstance(packet, SessionHistoryPacket) for packet in packets) == 10
    assert isinstance(packets[-1], FinalClassificationPacket)

    headers = [packet.header for packet in packets]
    assert all(header.session_uid == headers[0].session_uid for header in headers)
    assert [header.session_time for header in headers] == sorted(
        header.session_time for header in headers
    )
    assert [header.overall_frame_identifier for header in headers] == list(
        range(1, len(headers) + 1)
    )
    lap_values = [
        packet.cars[0].current_lap_number for packet in packets if isinstance(packet, LapDataPacket)
    ]
    assert lap_values == sorted(lap_values)
    for car in race_cars:
        fuel_samples = [
            packet.cars[car.index].fuel_in_tank
            for packet in packets
            if isinstance(packet, CarStatusPacket)
        ]
        assert fuel_samples == sorted(fuel_samples, reverse=True)
        pit_stop_samples = [
            packet.cars[car.index].pit_stops
            for packet in packets
            if isinstance(packet, LapDataPacket)
        ]
        assert pit_stop_samples == sorted(pit_stop_samples)

    sc_packets = [
        packet
        for packet in packets
        if isinstance(packet, SessionPacket) and packet.safety_car_status.name == "full_safety_car"
    ]
    assert sc_packets
    assert any(
        isinstance(packet, SessionPacket) and packet.weather.name == "light_rain"
        for packet in packets
    )
    invalid_packet = next(
        packet
        for packet in packets
        if isinstance(packet, LapDataPacket) and packet.cars[1].current_lap_number == 3
    )
    assert invalid_packet.cars[1].current_lap_invalid.value is True


def test_generated_race_updates_state_with_expected_car_data(
    race_cars: tuple[ScenarioCar, ...],
) -> None:
    state = RaceState()
    packets = list(build_race(race_cars, total_laps=5))

    for observed_at, packet in enumerate(packets, start=1):
        state.apply(packet, float(observed_at))

    snapshot = state.snapshot()
    assert snapshot.session.uid == packets[0].header.session_uid
    assert snapshot.session.total_laps == 5
    assert snapshot.session.ended is True
    assert snapshot.current_lap == 5
    assert snapshot.cars[0].name == "Driver A"
    assert snapshot.cars[1].name == "Driver B"
    assert snapshot.cars[0].current_lap == 5
    assert snapshot.cars[0].pit_stops == 1
    assert snapshot.cars[1].pit_stops == 0
    assert snapshot.cars[0].fuel_kg == pytest.approx(20.0)
    assert snapshot.cars[1].fuel_kg == pytest.approx(20.5)
    assert snapshot.cars[0].position is not None
    assert snapshot.cars[1].position is not None


def test_same_scenario_produces_equal_packet_sequences(
    race_cars: tuple[ScenarioCar, ...],
) -> None:
    first = list(build_race(race_cars, total_laps=3, hz=1))
    second = list(build_race(race_cars, total_laps=3, hz=1))

    assert first == second


@pytest.mark.parametrize(
    ("compound", "visual_raw"),
    [("intermediate", 7), ("wet", 8)],
)
def test_scenario_preserves_wet_visual_compound_codes(
    compound: str,
    visual_raw: int,
) -> None:
    car = ScenarioCar(0, "Driver A", 1, 90.0, 20.0, 1.0, 2.0, compounds=(compound,))
    packets = list(build_race((car,), total_laps=1, hz=1))

    status = next(packet for packet in packets if isinstance(packet, CarStatusPacket))
    tyre_sets = next(packet for packet in packets if isinstance(packet, TyreSetsPacket))
    history = next(packet for packet in packets if isinstance(packet, SessionHistoryPacket))
    classification = next(
        packet for packet in packets if isinstance(packet, FinalClassificationPacket)
    )
    classified_car = next(row for row in classification.classification if row is not None)

    assert status.cars[0].visual_tyre_compound.raw == visual_raw
    assert tyre_sets.tyre_sets[0].visual_tyre_compound.raw == visual_raw
    assert history.tyre_stint_history[0].visual_compound.raw == visual_raw
    assert classified_car.tyre_stints_visual[0].raw == visual_raw


def test_scenario_reports_fixture_packet_type_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_fixture = scenarios_module._fixture

    def wrong_session_fixture(name: str) -> scenarios_module.ParsedPacket:
        if name == "session.bin":
            return original_fixture("participants.bin")
        return original_fixture(name)

    monkeypatch.setattr(scenarios_module, "_fixture", wrong_session_fixture)
    car = ScenarioCar(0, "Driver A", 1, 90.0, 20.0, 1.0, 2.0)

    with pytest.raises(
        TypeError,
        match="session.bin: expected SessionPacket, got ParticipantsPacket",
    ):
        list(build_race((car,), total_laps=1))


def test_scenario_car_inputs_are_immutable() -> None:
    original = ScenarioCar(0, "Driver A", 1, 90.0, 20.0, 1.5, 2.0)
    updated = replace(original, pit_laps=(2,), compounds=("soft", "hard"))

    assert original.pit_laps == ()
    assert original.compounds == ("soft",)
    assert updated.pit_laps == (2,)


@pytest.mark.parametrize(
    ("cars", "total_laps", "hz"),
    [
        ((), 5, 2),
        ((ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0),), 0, 2),
        ((ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0),), 5, 0),
        (
            (
                ScenarioCar(
                    0,
                    "A",
                    1,
                    90.0,
                    20.0,
                    1.0,
                    2.0,
                    pit_laps=(6,),
                    compounds=("soft", "hard"),
                ),
            ),
            5,
            2,
        ),
    ],
)
def test_scenario_rejects_invalid_inputs(
    cars: tuple[ScenarioCar, ...],
    total_laps: int,
    hz: int,
) -> None:
    with pytest.raises(ValueError):
        list(build_race(cars, total_laps, hz))


def test_scenario_car_rejects_incoherent_stint_definition() -> None:
    with pytest.raises(ValueError, match="compuesto"):
        ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0, pit_laps=(2,))
