from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from f1_strategist.parser import (
    CarDamagePacket,
    CarStatusPacket,
    EventPacket,
    FinalClassificationPacket,
    LapDataPacket,
    ParticipantsPacket,
    SessionHistoryPacket,
    SessionPacket,
    TyreSetsPacket,
)
from f1_strategist.race_state import RaceState

from . import scenarios as scenarios_module
from .scenarios import ScenarioCar, build_race, build_race_timed


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
    first_lap_end = next(
        packet
        for packet in reversed(packets)
        if isinstance(packet, LapDataPacket) and packet.cars[0].current_lap_number == 1
    )
    second_lap_start = next(
        packet
        for packet in packets
        if isinstance(packet, LapDataPacket) and packet.cars[0].current_lap_number == 2
    )
    assert first_lap_end.cars[0].lap_distance == next(
        packet.track_length for packet in packets if isinstance(packet, SessionPacket)
    )
    assert second_lap_start.cars[0].lap_distance < first_lap_end.cars[0].lap_distance
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
    first = list(build_race(race_cars, total_laps=3, samples_per_lap=1))
    second = list(build_race(race_cars, total_laps=3, samples_per_lap=1))

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
    packets = list(build_race((car,), total_laps=1, samples_per_lap=1))

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
    ("cars", "total_laps", "samples_per_lap"),
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
    samples_per_lap: int,
) -> None:
    with pytest.raises(ValueError):
        list(build_race(cars, total_laps, samples_per_lap))


def test_scenario_car_rejects_incoherent_stint_definition() -> None:
    with pytest.raises(ValueError, match="compuesto"):
        ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0, pit_laps=(2,))


def test_last_lap_time_uses_the_previous_completed_lap() -> None:
    car = ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0)
    laps = [
        packet.cars[0]
        for packet in build_race((car,), 5, samples_per_lap=1, safety_car_laps=(3, 4))
        if isinstance(packet, LapDataPacket)
    ]

    assert [sample.last_lap_time_ms for sample in laps] == [
        0,
        90_000,
        90_000,
        117_000,
        117_000,
    ]


def test_sample_interval_replaces_samples_per_lap() -> None:
    car = ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0)
    packets = list(build_race((car,), 1, sample_interval_s=10.0))
    samples = [packet for packet in packets if isinstance(packet, LapDataPacket)]
    times = [packet.header.session_time for packet in samples]

    assert len(samples) == 9
    assert [sample.cars[0].current_lap_time_ms for sample in samples] == [
        10_000 * step for step in range(1, 10)
    ]
    assert all(9.9 <= right - left <= 10.1 for left, right in zip(times, times[1:], strict=False))


def test_pit_entry_exit_loss_timers_and_tyre_progression() -> None:
    car = ScenarioCar(
        0,
        "A",
        1,
        90.0,
        30.0,
        1.0,
        4.0,
        pit_laps=(2,),
        compounds=("soft", "hard"),
    )
    packets = list(build_race((car,), 4, samples_per_lap=2))
    laps = [packet.cars[0] for packet in packets if isinstance(packet, LapDataPacket)]
    statuses = [packet.cars[0] for packet in packets if isinstance(packet, CarStatusPacket)]
    damages = [packet.cars[0] for packet in packets if isinstance(packet, CarDamagePacket)]
    entry, exit_ = laps[3], laps[4]

    assert statuses[0].tyres_age_laps == 0
    assert damages[1].tyres_wear == (4.0, 4.0, 4.0, 4.0)
    assert entry.current_lap_number == 2
    assert entry.pit_status.name == "pitting"
    assert entry.pit_stops == 0
    assert entry.pit_lane_time_ms == 22_000
    assert entry.pit_stop_timer_ms == 2_500
    assert statuses[3].visual_tyre_compound.name == "soft"
    assert statuses[3].tyres_age_laps == 1
    assert damages[3].tyres_wear == (8.0, 8.0, 8.0, 8.0)
    assert exit_.current_lap_number == 3
    assert exit_.pit_status.name == "pitting"
    assert exit_.pit_stops == 1
    assert statuses[4].visual_tyre_compound.name == "hard"
    assert statuses[4].tyres_age_laps == 0
    assert damages[4].tyres_wear == (2.0, 2.0, 2.0, 2.0)
    history = [
        packet
        for packet in packets
        if isinstance(packet, SessionHistoryPacket) and packet.car_index == 0
    ]
    entry_time = history[1].lap_history[-1].lap_time_ms
    exit_time = history[2].lap_history[-1].lap_time_ms
    assert entry_time + exit_time - 2 * 90_000 == 20_000


def test_pace_slope_and_lap_time_override_have_calculated_times() -> None:
    car = ScenarioCar(
        0,
        "A",
        1,
        90.0,
        20.0,
        1.0,
        2.0,
        pace_slope_s_per_lap=0.75,
    )
    histories = [
        packet.lap_history[-1].lap_time_ms
        for packet in build_race((car,), 4, samples_per_lap=1)
        if isinstance(packet, SessionHistoryPacket)
    ]
    times_s = [time_ms / 1000 for time_ms in histories]
    ages = [0.0, 1.0, 2.0, 3.0]
    mean_age = sum(ages) / len(ages)
    mean_time = sum(times_s) / len(times_s)
    slope = sum(
        (age - mean_age) * (time - mean_time) for age, time in zip(ages, times_s, strict=True)
    ) / sum((age - mean_age) ** 2 for age in ages)

    assert histories == [90_000, 90_750, 91_500, 92_250]
    assert slope == pytest.approx((92.25 - 90.0) / 3)

    override_car = replace(car, lap_time_overrides={3: 88.0})
    override_history = [
        packet.lap_history[-1].lap_time_ms
        for packet in build_race((override_car,), 3, samples_per_lap=1)
        if isinstance(packet, SessionHistoryPacket)
    ]
    assert override_history == [90_000, 90_750, 88_000]
    sc_override_history = [
        packet.lap_history[-1].lap_time_ms
        for packet in build_race((override_car,), 3, samples_per_lap=1, safety_car_laps=(3, 3))
        if isinstance(packet, SessionHistoryPacket)
    ]
    assert sc_override_history[-1] == 114_400


def test_gaps_are_derived_and_can_be_overridden_across_a_lap() -> None:
    cars = (
        ScenarioCar(0, "Leader", 1, 90.0, 30.0, 1.0, 2.0),
        ScenarioCar(1, "Chaser", 2, 91.0, 30.0, 1.0, 2.0),
    )
    packets = list(
        build_race(
            cars,
            3,
            samples_per_lap=2,
            gap_ahead_s={(1, 2): (4.0, 6.0)},
        )
    )
    samples = [
        packet
        for packet in packets
        if isinstance(packet, LapDataPacket) and packet.cars[1].current_lap_number == 2
    ]

    assert [packet.cars[1].delta_to_car_in_front_ms for packet in samples] == [
        5_000,
        6_000,
    ]
    lap_one = [
        packet
        for packet in packets
        if isinstance(packet, LapDataPacket) and packet.cars[1].current_lap_number == 1
    ][-1]
    assert lap_one.cars[1].delta_to_car_in_front_ms == 1_000
    assert lap_one.cars[0].delta_to_race_leader_ms == 0


def test_ers_harvest_deploy_and_store_are_calculated() -> None:
    car = ScenarioCar(
        0,
        "A",
        1,
        90.0,
        20.0,
        1.0,
        2.0,
        ers_harvest_per_lap_j=500_000.0,
        ers_deploy_per_lap_j=1_000_000.0,
    )
    status = [
        packet.cars[0]
        for packet in build_race((car,), 2, samples_per_lap=2)
        if isinstance(packet, CarStatusPacket)
    ]

    assert status[0].ers_harvested_this_lap_mguk == 250_000
    assert status[0].ers_deployed_this_lap == 500_000
    assert status[0].ers_store_energy == 3_750_000
    assert status[1].ers_harvested_this_lap_mguk == 500_000
    assert status[1].ers_deployed_this_lap == 1_000_000
    assert status[1].ers_store_energy == 3_500_000
    assert status[2].ers_harvested_this_lap_mguk == 250_000
    assert status[2].ers_deployed_this_lap == 500_000
    assert status[2].ers_store_energy == 3_250_000
    assert status[3].ers_store_energy == 3_000_000


def test_damage_events_persist_until_a_later_event_changes_them() -> None:
    car = ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0)
    damage = [
        packet.cars[0]
        for packet in build_race(
            (car,),
            4,
            samples_per_lap=1,
            damage_events={
                (0, 2): {"floor_damage": 10, "drs_fault": 1},
                (0, 4): {"floor_damage": 20},
            },
        )
        if isinstance(packet, CarDamagePacket)
    ]

    assert [item.floor_damage for item in damage] == [0, 10, 10, 20]
    assert damage[1].drs_fault.raw == 1
    assert damage[2].drs_fault.raw == 1


def test_retirement_stops_lap_progress_history_and_final_status() -> None:
    car = ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0)
    packets = list(build_race((car,), 4, samples_per_lap=1, retirements={0: 2}))
    lap_samples = [packet.cars[0] for packet in packets if isinstance(packet, LapDataPacket)]
    history = [
        packet
        for packet in packets
        if isinstance(packet, SessionHistoryPacket) and packet.car_index == 0
    ]
    final = next(packet for packet in packets if isinstance(packet, FinalClassificationPacket))
    row = next(item for item in final.classification if item is not None)

    assert [sample.current_lap_number for sample in lap_samples] == [1, 2, 2, 2]
    assert all(sample.result_status.name == "retired" for sample in lap_samples[1:])
    assert len(history) == 1
    assert row.result_status.name == "retired"
    assert row.laps == 1
    assert row.total_race_time == 90.0


def test_flashback_rewinds_lap_and_session_clock_but_not_overall_frame() -> None:
    car = ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0)
    packets = list(build_race((car,), 4, samples_per_lap=2, flashback=(3, 1)))
    event_index = next(
        index
        for index, packet in enumerate(packets)
        if isinstance(packet, EventPacket) and packet.event_code.name == "flashback"
    )
    before = next(
        packet for packet in reversed(packets[:event_index]) if isinstance(packet, LapDataPacket)
    )
    after = next(
        packet for packet in packets[event_index + 1 :] if isinstance(packet, LapDataPacket)
    )
    session_times = [packet.header.session_time for packet in packets]
    overall_frames = [packet.header.overall_frame_identifier for packet in packets]
    observed = [observed_at for _, observed_at in build_race_timed((car,), 4, flashback=(3, 1))]
    without_rewind = list(build_race_timed((car,), 1))

    assert before.cars[0].current_lap_number == 3
    assert after.cars[0].current_lap_number == 1
    assert (
        packets[event_index].header.frame_identifier
        < packets[event_index - 1].header.frame_identifier
    )
    assert session_times[event_index] < session_times[event_index - 1]
    assert overall_frames == list(range(1, len(packets) + 1))
    assert all(right > left for left, right in zip(observed, observed[1:], strict=False))
    assert all(packet.header.session_time == observed_at for packet, observed_at in without_rewind)


def test_all_scenario_options_are_deterministic_together() -> None:
    cars = (
        ScenarioCar(
            0,
            "A",
            1,
            90.0,
            30.0,
            1.0,
            2.0,
            pit_laps=(2,),
            compounds=("soft", "hard"),
            pace_slope_s_per_lap=0.2,
            lap_time_overrides={3: 91.0},
            ers_harvest_per_lap_j=100_000.0,
            ers_deploy_per_lap_j=200_000.0,
        ),
        ScenarioCar(1, "B", 2, 91.0, 30.0, 1.0, 2.0),
    )
    options = {
        "samples_per_lap": 2,
        "sample_interval_s": 45.0,
        "safety_car_laps": (3, 3),
        "weather_change": (2, "light_rain"),
        "invalid_laps": frozenset({(0, 2)}),
        "pit_loss_s": 18.0,
        "pit_lane_time_s": 20.0,
        "pit_stationary_s": 2.0,
        "gap_ahead_s": {(1, 2): (2.0, 3.0)},
        "damage_events": {(0, 2): {"floor_damage": 5}},
        "retirements": {1: 4},
        "flashback": (3, 1),
    }

    assert list(build_race(cars, 4, **options)) == list(build_race(cars, 4, **options))
    assert list(build_race_timed(cars, 4, **options)) == list(build_race_timed(cars, 4, **options))


@pytest.mark.parametrize(
    "options",
    [
        {"sample_interval_s": 0.0},
        {"pit_loss_s": -1.0},
        {"pit_lane_time_s": float("inf")},
        {"pit_stationary_s": 23.0},
        {"gap_ahead_s": {(3, 1): (0.0, 1.0)}},
        {"damage_events": {(0, 1): {"not_damage": 10}}},
        {"retirements": {3: 2}},
        {"flashback": (2, 2)},
        {"flashback": (1, 1)},
    ],
)
def test_scenario_validates_all_new_options(options: Any) -> None:
    car = ScenarioCar(0, "A", 1, 90.0, 20.0, 1.0, 2.0)
    with pytest.raises(ValueError):
        list(build_race((car,), 4, **options))


def test_scenario_rejects_retirement_during_a_pit_stop() -> None:
    car = ScenarioCar(
        0,
        "A",
        1,
        90.0,
        20.0,
        1.0,
        2.0,
        pit_laps=(2,),
        compounds=("soft", "hard"),
    )

    with pytest.raises(ValueError, match="coincidir con una parada"):
        list(build_race((car,), 4, retirements={0: 3}))
