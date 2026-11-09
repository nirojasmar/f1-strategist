from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from f1_strategist.parser import (
    BooleanValue,
    CarDamagePacket,
    CarStatusPacket,
    EnumValue,
    EventDetail,
    EventPacket,
    FinalClassificationPacket,
    LapDataPacket,
    NoEventDetail,
    ParsedPacket,
    ParticipantsPacket,
    SessionPacket,
    TyreSetsPacket,
    parse_packet,
)
from f1_strategist.race_state import CarDamageSnapshot, CarSnapshot, RaceState

_FIXTURES = Path(__file__).parent / "fixtures"


def _set_attribute(target: object, attribute: str, value: object) -> None:
    setattr(target, attribute, value)


def _parsed_fixture(name: str) -> ParsedPacket:
    return parse_packet((_FIXTURES / name).read_bytes())


def _with_header[PacketT: ParsedPacket](
    packet: PacketT, *, session_uid: int = 1, player_car_index: int = 0
) -> PacketT:
    return replace(
        packet,
        header=replace(
            packet.header,
            session_uid=session_uid,
            player_car_index=player_car_index,
        ),
    )


def _lap_packet(
    *,
    lap: int,
    position: int = 1,
    pit_status: str = "none",
    pit_stops: int = 0,
    session_uid: int = 1,
) -> LapDataPacket:
    packet = _parsed_fixture("lap_data.bin")
    assert isinstance(packet, LapDataPacket)
    first_car = replace(
        packet.cars[0],
        current_lap_number=lap,
        car_position=position,
        pit_status=EnumValue(pit_status, pit_status),
        pit_stops=pit_stops,
    )
    return _with_header(
        replace(packet, cars=(first_car, *packet.cars[1:])),
        session_uid=session_uid,
    )


def test_initial_snapshot_has_22_cars_and_unknown_values_are_none() -> None:
    snapshot = RaceState().snapshot()

    assert len(snapshot.cars) == 22
    assert snapshot.player_car is None
    assert all(car.name is None and car.fuel_kg is None for car in snapshot.cars)
    assert all(item.updated_at_monotonic is None for item in snapshot.freshness)


def test_packets_update_pilot_and_named_car_snapshots_without_filling_missing_data() -> None:
    state = RaceState()
    session = _parsed_fixture("session.bin")
    participants = _parsed_fixture("participants.bin")
    status = _parsed_fixture("car_status.bin")
    lap = _parsed_fixture("lap_data.bin")
    assert isinstance(session, SessionPacket)
    assert isinstance(participants, ParticipantsPacket)
    assert isinstance(status, CarStatusPacket)
    assert isinstance(lap, LapDataPacket)

    session = replace(
        _with_header(session),
        session_type=EnumValue(15, "race"),
        track_id=EnumValue(11, "monza"),
        total_laps=5,
    )
    first_participant = participants.participants[0]
    assert first_participant is not None
    first_participant = replace(first_participant, name="Test Driver")
    participants = _with_header(
        replace(participants, participants=(first_participant, *participants.participants[1:]))
    )
    first_status = replace(
        status.cars[0],
        fuel_in_tank=0.0,
        actual_tyre_compound=EnumValue(18, "c3"),
        visual_tyre_compound=EnumValue(17, "medium"),
        tyres_age_laps=0,
    )
    status = _with_header(replace(status, cars=(first_status, *status.cars[1:])))
    first_lap = replace(
        lap.cars[0],
        current_lap_number=3,
        car_position=2,
        grid_position_zero_based=0,
        pit_stops=0,
    )
    lap = _with_header(replace(lap, cars=(first_lap, *lap.cars[1:])))

    for observed_at, packet in enumerate((session, participants, status, lap), start=1):
        state.apply(packet, observed_at)

    snapshot = state.snapshot()
    assert snapshot.session.session_type == "race"
    assert snapshot.session.track == "monza"
    assert snapshot.session.total_laps == 5
    assert snapshot.player_car is not None
    assert snapshot.player_car.name == "Test Driver"
    assert snapshot.player_car.grid_position == 1
    assert snapshot.player_car.position == 2
    assert snapshot.player_car.current_lap == 3
    assert snapshot.current_lap == 3
    assert snapshot.player_car.fuel_kg == 0.0
    assert snapshot.player_car.actual_tyre_compound == "c3"
    assert snapshot.player_car.visual_tyre_compound == "medium"
    assert snapshot.player_car.tyre_age_laps == 0
    assert isinstance(snapshot.cars, tuple)
    with pytest.raises(FrozenInstanceError):
        _set_attribute(snapshot.cars[0], "name", "Modified")


def test_lap_data_publishes_timing_interval_and_pit_fields() -> None:
    packet = _lap_packet(lap=4, position=2)
    first_car = replace(
        packet.cars[0],
        last_lap_time_ms=91_234,
        current_lap_time_ms=12_345,
        sector1_time_ms=30_111,
        sector2_time_ms=31_222,
        delta_to_car_in_front_ms=1_234,
        delta_to_race_leader_ms=5_678,
        lap_distance=0.42,
        current_lap_invalid=BooleanValue(1, True),
        penalties=3,
        result_status=EnumValue(2, "active"),
        pit_lane_timer_active=BooleanValue(1, True),
        pit_lane_time_ms=2_500,
        pit_stop_timer_ms=1_000,
    )
    packet = replace(packet, cars=(first_car, *packet.cars[1:]))

    state = RaceState()
    state.apply(packet, observed_at=1.0)

    car = state.snapshot().player_car
    assert car is not None
    assert car.last_lap_time_ms == 91_234
    assert car.current_lap_time_ms == 12_345
    assert car.sector1_time_ms == 30_111
    assert car.sector2_time_ms == 31_222
    assert car.delta_to_car_in_front_ms == 1_234
    assert car.delta_to_leader_ms == 5_678
    assert car.lap_distance_m == 0.42
    assert car.current_lap_invalid is True
    assert car.penalties_s == 3.0
    assert car.result_status == "active"
    assert car.pit_lane_timer_active is True
    assert car.pit_lane_time_ms == 2_500
    assert car.pit_stop_timer_ms == 1_000


def test_lap_data_maps_each_published_field_to_the_matching_snapshot_field() -> None:
    packet = _lap_packet(lap=13, position=4)
    first_car = replace(
        packet.cars[0],
        last_lap_time_ms=91_234,
        current_lap_time_ms=12_345,
        sector1_time_ms=23_456,
        sector2_time_ms=34_567,
        delta_to_car_in_front_ms=4_321,
        delta_to_race_leader_ms=5_432,
        lap_distance=0.678,
        car_position=4,
        current_lap_number=13,
        pit_status=EnumValue(1, "pitting"),
        pit_stops=3,
        current_lap_invalid=BooleanValue(1, True),
        penalties=6,
        grid_position_zero_based=7,
        driver_status=EnumValue(2, "in_lap"),
        result_status=EnumValue(2, "active"),
        pit_lane_timer_active=BooleanValue(1, True),
        pit_lane_time_ms=8_765,
        pit_stop_timer_ms=9_876,
    )
    state = RaceState()
    state.apply(replace(packet, cars=(first_car, *packet.cars[1:])), observed_at=1.0)

    assert state.snapshot().cars[0] == CarSnapshot(
        car_index=0,
        grid_position=8,
        position=4,
        current_lap=13,
        pit_status="pitting",
        pit_stops=3,
        driver_status="in_lap",
        last_lap_time_ms=91_234,
        current_lap_time_ms=12_345,
        sector1_time_ms=23_456,
        sector2_time_ms=34_567,
        delta_to_car_in_front_ms=4_321,
        delta_to_leader_ms=5_432,
        lap_distance_m=0.678,
        current_lap_invalid=True,
        penalties_s=6.0,
        result_status="active",
        pit_lane_timer_active=True,
        pit_lane_time_ms=8_765,
        pit_stop_timer_ms=9_876,
    )


def test_lap_data_unknown_values_are_none_for_active_leader() -> None:
    state = RaceState()
    state.apply(_lap_packet(lap=1, position=1), observed_at=1.0)

    car = state.snapshot().player_car
    assert car is not None
    assert car.result_status == "active"
    assert car.last_lap_time_ms is None
    assert car.current_lap_time_ms is None
    assert car.sector1_time_ms is None
    assert car.sector2_time_ms is None
    assert car.delta_to_car_in_front_ms is None
    assert car.delta_to_leader_ms is None
    assert car.current_lap_invalid is False
    assert car.penalties_s == 0.0
    assert car.pit_lane_timer_active is False
    assert car.pit_lane_time_ms is None
    assert car.pit_stop_timer_ms is None


def test_invalid_and_inactive_cars_do_not_publish_lap_values() -> None:
    packet = _lap_packet(lap=2, position=2)
    invalid = replace(
        packet.cars[0],
        last_lap_time_ms=80_000,
        current_lap_time_ms=10_000,
        sector1_time_ms=20_000,
        sector2_time_ms=21_000,
        delta_to_car_in_front_ms=1_000,
        delta_to_race_leader_ms=2_000,
        lap_distance=0.5,
        current_lap_invalid=BooleanValue(1, True),
        penalties=2,
        result_status=EnumValue(0, "invalid"),
        pit_lane_timer_active=BooleanValue(1, True),
        pit_lane_time_ms=1_500,
        pit_stop_timer_ms=900,
    )
    inactive = replace(
        packet.cars[1],
        last_lap_time_ms=81_000,
        result_status=EnumValue(1, "inactive"),
    )
    packet = replace(packet, cars=(invalid, inactive, *packet.cars[2:]))

    state = RaceState()
    state.apply(packet, observed_at=1.0)

    snapshot = state.snapshot()
    invalid_car = snapshot.cars[0]
    assert invalid_car.result_status == "invalid"
    assert invalid_car.last_lap_time_ms is None
    assert invalid_car.current_lap_time_ms is None
    assert invalid_car.sector1_time_ms is None
    assert invalid_car.sector2_time_ms is None
    assert invalid_car.delta_to_car_in_front_ms is None
    assert invalid_car.delta_to_leader_ms is None
    assert invalid_car.lap_distance_m is None
    assert invalid_car.current_lap_invalid is None
    assert invalid_car.penalties_s is None
    assert invalid_car.pit_lane_timer_active is None
    assert invalid_car.pit_lane_time_ms is None
    assert invalid_car.pit_stop_timer_ms is None
    assert snapshot.cars[1].result_status == "inactive"
    assert snapshot.cars[1].last_lap_time_ms is None


def test_freshness_records_age_and_obsolete_data_by_packet_group() -> None:
    state = RaceState()
    packet = _parsed_fixture("car_status.bin")
    assert isinstance(packet, CarStatusPacket)

    state.apply(_with_header(packet), observed_at=10.0)
    freshness = state.snapshot().freshness_for("car_status")

    assert freshness.age_seconds(11.25) == 1.25
    assert not freshness.is_stale(11.25)
    assert freshness.is_stale(12.0)
    assert state.snapshot().freshness_for("participants").age_seconds(12.0) is None


def test_car_status_publishes_fuel_ers_flags_and_drs() -> None:
    state = RaceState()
    packet = _parsed_fixture("car_status.bin")
    assert isinstance(packet, CarStatusPacket)

    state.apply(_with_header(packet), observed_at=1.0)

    car = state.snapshot().player_car
    assert car is not None
    assert car.fuel_kg == pytest.approx(10.426180839538574)
    assert car.fuel_capacity_kg == 110.0
    assert car.fuel_remaining_laps == pytest.approx(2.9225478172302246)
    assert car.ers_store_j == 4_000_000.0
    assert car.ers_deploy_mode == "medium"
    assert car.ers_harvested_mguk_j == 0.0
    assert car.ers_harvested_mguh_j == 0.0
    assert car.ers_deployed_this_lap_j == 0.0
    assert car.fia_flag == "none"
    assert car.drs_allowed is False


def test_car_status_maps_each_published_field_to_the_matching_snapshot_field() -> None:
    packet = _parsed_fixture("car_status.bin")
    assert isinstance(packet, CarStatusPacket)
    first_car = replace(
        packet.cars[0],
        fuel_in_tank=13.25,
        fuel_capacity=101.5,
        fuel_remaining_laps=2.75,
        actual_tyre_compound=EnumValue(18, "hard"),
        visual_tyre_compound=EnumValue(17, "medium"),
        tyres_age_laps=7,
        ers_store_energy=1_234_567.0,
        ers_deploy_mode=EnumValue(3, "high"),
        ers_harvested_this_lap_mguk=111.0,
        ers_harvested_this_lap_mguh=222.0,
        ers_deployed_this_lap=333.0,
        vehicle_fia_flags=EnumValue(2, "yellow"),
        drs_allowed=EnumValue(1, "allowed"),
    )
    state = RaceState()
    state.apply(_with_header(replace(packet, cars=(first_car, *packet.cars[1:]))), 1.0)

    assert state.snapshot().cars[0] == CarSnapshot(
        car_index=0,
        fuel_kg=13.25,
        actual_tyre_compound="hard",
        visual_tyre_compound="medium",
        tyre_age_laps=7,
        fuel_capacity_kg=101.5,
        fuel_remaining_laps=2.75,
        ers_store_j=1_234_567.0,
        ers_deploy_mode="high",
        ers_harvested_mguk_j=111.0,
        ers_harvested_mguh_j=222.0,
        ers_deployed_this_lap_j=333.0,
        fia_flag="yellow",
        drs_allowed=True,
    )


def test_restricted_fuel_and_ers_values_are_none() -> None:
    packet = _parsed_fixture("car_status.bin")
    assert isinstance(packet, CarStatusPacket)
    restricted = replace(
        packet.cars[0],
        fuel_in_tank=0.0,
        fuel_capacity=0.0,
        fuel_remaining_laps=0.0,
        ers_store_energy=0.0,
        ers_deploy_mode=EnumValue(0, "none"),
        ers_harvested_this_lap_mguk=0.0,
        ers_harvested_this_lap_mguh=0.0,
        ers_deployed_this_lap=0.0,
    )
    packet = _with_header(replace(packet, cars=(restricted, *packet.cars[1:])))

    state = RaceState()
    state.apply(packet, observed_at=1.0)

    car = state.snapshot().player_car
    assert car is not None
    assert car.fuel_kg is None
    assert car.fuel_capacity_kg is None
    assert car.fuel_remaining_laps is None
    assert car.ers_store_j is None
    assert car.ers_deploy_mode is None
    assert car.ers_harvested_mguk_j is None
    assert car.ers_harvested_mguh_j is None
    assert car.ers_deployed_this_lap_j is None
    assert car.actual_tyre_compound == "c5"
    assert car.fia_flag == "none"
    assert car.drs_allowed is False


def test_restricted_participant_hides_damage_tyre_sets_fuel_and_ers() -> None:
    participants = _parsed_fixture("participants.bin")
    status = _parsed_fixture("car_status.bin")
    damage = _parsed_fixture("car_damage.bin")
    tyre_sets = _parsed_fixture("tyre_sets.bin")
    assert isinstance(participants, ParticipantsPacket)
    assert isinstance(status, CarStatusPacket)
    assert isinstance(damage, CarDamagePacket)
    assert isinstance(tyre_sets, TyreSetsPacket)
    restricted_participant = participants.participants[1]
    assert restricted_participant is not None
    participants_list = list(participants.participants)
    participants_list[1] = replace(
        restricted_participant,
        your_telemetry=EnumValue(0, "restricted"),
    )
    restricted_status = replace(
        status.cars[1],
        fuel_in_tank=0.0,
        fuel_capacity=0.0,
        fuel_remaining_laps=0.0,
        ers_store_energy=0.0,
        ers_harvested_this_lap_mguk=0.0,
        ers_harvested_this_lap_mguh=0.0,
        ers_deployed_this_lap=0.0,
    )
    state = RaceState()
    state.apply(
        _with_header(replace(participants, participants=tuple(participants_list))),
        observed_at=1.0,
    )
    state.apply(
        _with_header(replace(status, cars=(status.cars[0], restricted_status))),
        observed_at=2.0,
    )
    state.apply(
        _with_header(replace(tyre_sets, car_index=1)),
        observed_at=3.0,
    )
    state.apply(
        _with_header(replace(damage, cars=(damage.cars[0], damage.cars[1]))),
        observed_at=4.0,
    )

    car = state.snapshot().cars[1]
    assert car.telemetry_restricted is True
    assert car.fuel_kg is None
    assert car.fuel_capacity_kg is None
    assert car.fuel_remaining_laps is None
    assert car.ers_store_j is None
    assert car.ers_harvested_mguk_j is None
    assert car.ers_harvested_mguh_j is None
    assert car.ers_deployed_this_lap_j is None
    assert car.damage is None
    assert car.fitted_tyre is None


def test_player_telemetry_is_never_marked_restricted_when_fields_are_zero() -> None:
    participants = _parsed_fixture("participants.bin")
    status = _parsed_fixture("car_status.bin")
    damage = _parsed_fixture("car_damage.bin")
    tyre_sets = _parsed_fixture("tyre_sets.bin")
    assert isinstance(participants, ParticipantsPacket)
    assert isinstance(status, CarStatusPacket)
    assert isinstance(damage, CarDamagePacket)
    assert isinstance(tyre_sets, TyreSetsPacket)
    player = participants.participants[0]
    assert player is not None
    participants_list = list(participants.participants)
    participants_list[0] = replace(player, your_telemetry=EnumValue(0, "restricted"))
    zero_status = replace(
        status.cars[0],
        fuel_in_tank=0.0,
        fuel_capacity=0.0,
        fuel_remaining_laps=0.0,
        ers_store_energy=0.0,
    )
    zero_damage = replace(
        damage.cars[0],
        tyres_wear=(0.0, 0.0, 0.0, 0.0),
        front_left_wing_damage=0,
    )
    state = RaceState()
    state.apply(
        _with_header(replace(participants, participants=tuple(participants_list))),
        observed_at=1.0,
    )
    state.apply(
        _with_header(replace(status, cars=(zero_status, *status.cars[1:]))),
        observed_at=2.0,
    )
    state.apply(
        _with_header(replace(damage, cars=(zero_damage, *damage.cars[1:]))),
        observed_at=3.0,
    )
    state.apply(
        _with_header(replace(tyre_sets, car_index=0)),
        observed_at=4.0,
    )

    car = state.snapshot().cars[0]
    assert car.telemetry_restricted is False
    assert car.fuel_kg == 0.0
    assert car.ers_store_j == 0.0
    assert car.damage is not None
    assert car.damage.tyres_wear == (0.0, 0.0, 0.0, 0.0)
    assert car.fitted_tyre is not None


def test_status_capacity_fallback_suppresses_fuel_before_participants_arrive() -> None:
    status = _parsed_fixture("car_status.bin")
    damage = _parsed_fixture("car_damage.bin")
    tyre_sets = _parsed_fixture("tyre_sets.bin")
    assert isinstance(status, CarStatusPacket)
    assert isinstance(damage, CarDamagePacket)
    assert isinstance(tyre_sets, TyreSetsPacket)
    restricted_status = replace(
        status.cars[1],
        fuel_in_tank=0.0,
        fuel_capacity=0.0,
        fuel_remaining_laps=0.0,
        ers_store_energy=0.0,
    )
    state = RaceState()
    state.apply(
        _with_header(replace(status, cars=(status.cars[0], restricted_status))),
        observed_at=1.0,
    )
    assert state.snapshot().cars[1].telemetry_restricted is None
    assert state.snapshot().cars[1].fuel_kg is None
    assert state.snapshot().cars[1].ers_store_j is None

    state.apply(
        _with_header(replace(damage, cars=(damage.cars[0], damage.cars[1]))),
        observed_at=2.0,
    )
    state.apply(
        _with_header(replace(tyre_sets, car_index=1)),
        observed_at=3.0,
    )

    assert state.snapshot().cars[1].telemetry_restricted is None
    assert state.snapshot().cars[1].damage is not None
    assert state.snapshot().cars[1].fitted_tyre is not None


def test_tyre_sets_packet_populates_the_fitted_tyre_for_its_car() -> None:
    packet = _parsed_fixture("tyre_sets.bin")
    assert isinstance(packet, TyreSetsPacket)

    state = RaceState()
    state.apply(_with_header(replace(packet, car_index=1)), observed_at=2.5)

    snapshot = state.snapshot()
    assert snapshot.cars[0].fitted_tyre is None
    fitted = snapshot.cars[1].fitted_tyre
    assert fitted is not None
    assert fitted.actual_tyre_compound == "c5"
    assert fitted.visual_tyre_compound == "soft"
    assert fitted.wear_pct == 0.0
    assert fitted.lifespan_laps == 18
    assert fitted.usable_life_laps == 18
    assert fitted.observed_at_monotonic == 2.5
    assert snapshot.freshness_for("tyre_sets").updated_at_monotonic == 2.5

    state.apply(
        _with_header(replace(packet, car_index=1, fitted_index=255)),
        observed_at=3.0,
    )
    assert state.snapshot().cars[1].fitted_tyre is None


def test_tyre_sets_fitted_freshness_is_tracked_per_car() -> None:
    packet = _parsed_fixture("tyre_sets.bin")
    assert isinstance(packet, TyreSetsPacket)
    state = RaceState()

    state.apply(_with_header(replace(packet, car_index=0)), observed_at=10.0)
    state.apply(_with_header(replace(packet, car_index=1)), observed_at=100.0)

    fitted_car_0 = state.snapshot().cars[0].fitted_tyre
    fitted_car_1 = state.snapshot().cars[1].fitted_tyre
    assert fitted_car_0 is not None
    assert fitted_car_1 is not None
    assert fitted_car_0.observed_at_monotonic == 10.0
    assert fitted_car_1.observed_at_monotonic == 100.0
    assert state.snapshot().freshness_for("tyre_sets").updated_at_monotonic == 100.0


def test_tyre_sets_maps_each_fitted_field() -> None:
    packet = _parsed_fixture("tyre_sets.bin")
    assert isinstance(packet, TyreSetsPacket)
    fitted_set = replace(
        packet.tyre_sets[0],
        actual_tyre_compound=EnumValue(18, "hard"),
        visual_tyre_compound=EnumValue(17, "medium"),
        wear=37,
        lifespan=12,
        usable_life=9,
    )
    packet = replace(
        packet, car_index=5, fitted_index=0, tyre_sets=(fitted_set, *packet.tyre_sets[1:])
    )
    state = RaceState()
    state.apply(_with_header(packet), observed_at=6.75)

    fitted_tyre = state.snapshot().cars[5].fitted_tyre
    assert fitted_tyre is not None
    assert fitted_tyre.actual_tyre_compound == "hard"
    assert fitted_tyre.visual_tyre_compound == "medium"
    assert fitted_tyre.wear_pct == 37.0
    assert fitted_tyre.lifespan_laps == 12
    assert fitted_tyre.usable_life_laps == 9
    assert fitted_tyre.observed_at_monotonic == 6.75


def test_car_damage_packet_is_preserved_in_immutable_car_snapshot() -> None:
    state = RaceState()
    packet = _parsed_fixture("car_damage.bin")
    assert isinstance(packet, CarDamagePacket)
    first_damage = replace(
        packet.cars[0],
        tyres_wear=(10.0, 20.0, 30.0, 40.0),
        front_left_wing_damage=67,
        engine_damage=41,
        drs_fault=EnumValue(254, "unknown"),
    )
    packet = _with_header(replace(packet, cars=(first_damage, *packet.cars[1:])))

    state.apply(packet, observed_at=1.0)

    snapshot = state.snapshot()
    assert snapshot.cars[0].damage is not None
    assert snapshot.cars[0].damage.tyres_wear == (10.0, 20.0, 30.0, 40.0)
    assert snapshot.cars[0].damage.front_left_wing_damage == 67
    assert snapshot.cars[0].damage.engine_damage == 41
    assert snapshot.cars[0].damage.drs_fault is None
    assert snapshot.cars[1].damage is not None
    assert snapshot.freshness_for("car_damage").updated_at_monotonic == 1.0


def test_car_damage_maps_each_field_to_the_matching_snapshot_field() -> None:
    packet = _parsed_fixture("car_damage.bin")
    assert isinstance(packet, CarDamagePacket)
    first_damage = replace(
        packet.cars[0],
        tyres_wear=(11.1, 22.2, 33.3, 44.4),
        tyres_damage=(1, 2, 3, 4),
        brakes_damage=(5, 6, 7, 8),
        tyre_blisters=(9, 10, 11, 12),
        front_left_wing_damage=13,
        front_right_wing_damage=14,
        rear_wing_damage=15,
        floor_damage=16,
        diffuser_damage=17,
        sidepod_damage=18,
        drs_fault=EnumValue(1, "fault"),
        ers_fault=EnumValue(2, "fault"),
        gearbox_damage=21,
        engine_damage=22,
        engine_mguh_wear=23,
        engine_es_wear=24,
        engine_ce_wear=25,
        engine_ice_wear=26,
        engine_mguk_wear=27,
        engine_tc_wear=28,
        engine_blown=EnumValue(1, "yes"),
        engine_seized=EnumValue(0, "no"),
    )
    state = RaceState()
    state.apply(_with_header(replace(packet, cars=(first_damage, *packet.cars[1:]))), 1.0)

    assert state.snapshot().cars[0].damage == CarDamageSnapshot(
        tyres_wear=(11.1, 22.2, 33.3, 44.4),
        tyres_damage=(1, 2, 3, 4),
        brakes_damage=(5, 6, 7, 8),
        tyre_blisters=(9, 10, 11, 12),
        front_left_wing_damage=13,
        front_right_wing_damage=14,
        rear_wing_damage=15,
        floor_damage=16,
        diffuser_damage=17,
        sidepod_damage=18,
        drs_fault="fault",
        ers_fault="fault",
        gearbox_damage=21,
        engine_damage=22,
        engine_mguh_wear=23,
        engine_es_wear=24,
        engine_ce_wear=25,
        engine_ice_wear=26,
        engine_mguk_wear=27,
        engine_tc_wear=28,
        engine_blown="yes",
        engine_seized="no",
    )


def test_session_packet_publishes_environment_and_forecast() -> None:
    state = RaceState()
    session = _parsed_fixture("session.bin")
    assert isinstance(session, SessionPacket)
    forecast = tuple(
        replace(
            sample,
            session_type=EnumValue(index + 1, ("practice_1", "race", "qualifying_1")[index]),
        )
        for index, sample in enumerate(session.weather_forecast)
    )
    session = replace(
        session,
        forecast_accuracy=EnumValue(1, "approximate"),
        season_link_identifier=7,
        weekend_link_identifier=42,
        session_link_identifier=3,
        weekend_structure=(EnumValue(15, "race"), EnumValue(10, "practice_1")),
        session_settings=replace(
            session.session_settings,
            safety_car_periods=2,
            virtual_safety_car_periods=3,
            red_flag_periods=4,
            num_sessions_in_weekend=5,
        ),
        weather_forecast=forecast,
    )

    state.apply(_with_header(session), observed_at=1.0)

    snapshot = state.snapshot()
    assert snapshot.session.weather == "clear"
    assert snapshot.session.track_temperature_c == 40.0
    assert snapshot.session.air_temperature_c == 26.0
    assert snapshot.session.safety_car_status == "no_safety_car"
    assert snapshot.session.session_time_left_s == 7200.0
    assert snapshot.session.track_length_m == 5798.0
    assert snapshot.session.pit_speed_limit_kph == 80.0
    assert snapshot.session.pit_window_ideal_lap is None
    assert snapshot.session.pit_window_latest_lap is None
    assert snapshot.session.pit_rejoin_position is None
    assert snapshot.session.game_paused is False
    assert snapshot.session.forecast_accuracy == "approximate"
    assert snapshot.session.safety_car_periods == 2
    assert snapshot.session.virtual_safety_car_periods == 3
    assert snapshot.session.red_flag_periods == 4
    assert snapshot.session.season_link_id == 7
    assert snapshot.session.weekend_link_id == 42
    assert snapshot.session.session_link_id == 3
    assert snapshot.session.weekend_structure == ("race", "practice_1")
    assert snapshot.session.num_sessions_in_weekend == 5
    assert len(snapshot.session.forecast) == 3
    assert [sample.session_type for sample in snapshot.session.forecast] == [
        "practice_1",
        "race",
        "qualifying_1",
    ]
    first_sample = snapshot.session.forecast[0]
    assert first_sample.offset_minutes == 0
    assert first_sample.session_type == "practice_1"
    assert first_sample.weather == "clear"
    assert first_sample.track_temperature_c == 35.0
    assert first_sample.air_temperature_c == 25.0
    assert first_sample.rain_percentage == 6


def test_session_link_identifiers_at_zero_mean_no_link() -> None:
    state = RaceState()
    session = _parsed_fixture("session.bin")
    assert isinstance(session, SessionPacket)
    session = replace(
        session,
        season_link_identifier=0,
        weekend_link_identifier=0,
        session_link_identifier=0,
        weekend_structure=(),
        session_settings=replace(session.session_settings, num_sessions_in_weekend=0),
    )

    state.apply(_with_header(session), observed_at=1.0)

    snapshot = state.snapshot()
    assert snapshot.session.season_link_id is None
    assert snapshot.session.weekend_link_id is None
    assert snapshot.session.session_link_id is None
    assert snapshot.session.weekend_structure == ()
    assert snapshot.session.num_sessions_in_weekend is None


def test_session_environment_events_are_emitted_only_on_change() -> None:
    state = RaceState()
    session = _parsed_fixture("session.bin")
    assert isinstance(session, SessionPacket)
    state.apply(_with_header(session), observed_at=1.0)
    assert state.snapshot().events == ()

    state.apply(_with_header(session), observed_at=2.0)
    assert state.snapshot().events == ()

    state.apply(
        _with_header(
            replace(
                session,
                weather=EnumValue(3, "light_rain"),
                safety_car_status=EnumValue(1, "full_safety_car"),
            )
        ),
        observed_at=3.0,
    )
    assert [event.kind for event in state.snapshot().events] == [
        "safety_car_changed",
        "weather_changed",
    ]
    assert [(event.previous_value, event.value) for event in state.snapshot().events] == [
        ("no_safety_car", "full_safety_car"),
        ("clear", "light_rain"),
    ]

    state.apply(
        _with_header(
            replace(
                session,
                weather=EnumValue(4, "heavy_rain"),
                safety_car_status=EnumValue(1, "full_safety_car"),
            )
        ),
        observed_at=4.0,
    )
    state.apply(
        _with_header(
            replace(
                session,
                weather=EnumValue(4, "heavy_rain"),
                safety_car_status=EnumValue(1, "full_safety_car"),
            )
        ),
        observed_at=5.0,
    )

    snapshot = state.snapshot()
    assert snapshot.session.weather == "heavy_rain"
    assert snapshot.session.safety_car_status == "full_safety_car"
    assert [(event.kind, event.occurred_at_monotonic) for event in snapshot.events] == [
        ("safety_car_changed", 3.0),
        ("weather_changed", 3.0),
        ("weather_changed", 4.0),
    ]

    state.apply(
        _with_header(
            replace(
                session,
                weather=EnumValue(0, "unknown"),
                safety_car_status=EnumValue(0, "unknown"),
            )
        ),
        observed_at=6.0,
    )
    snapshot = state.snapshot()
    assert snapshot.session.weather is None
    assert snapshot.session.safety_car_status is None
    assert len(snapshot.events) == 3


def test_session_uid_change_clears_environment_and_forecast() -> None:
    state = RaceState()
    session = _parsed_fixture("session.bin")
    event = _parsed_fixture("event.bin")
    assert isinstance(session, SessionPacket)
    assert isinstance(event, EventPacket)
    state.apply(_with_header(session), observed_at=1.0)
    assert state.snapshot().session.forecast != ()

    state.apply(
        replace(
            event,
            header=replace(event.header, session_uid=2, player_car_index=0),
            event_code=EnumValue("SSTA", "session_started"),
            detail=NoEventDetail(),
        ),
        observed_at=2.0,
    )

    snapshot = state.snapshot()
    assert snapshot.session.uid == 2
    assert snapshot.session.weather is None
    assert snapshot.session.track_temperature_c is None
    assert snapshot.session.air_temperature_c is None
    assert snapshot.session.safety_car_status is None
    assert snapshot.session.session_time_left_s is None
    assert snapshot.session.track_length_m is None
    assert snapshot.session.pit_speed_limit_kph is None
    assert snapshot.session.pit_window_ideal_lap is None
    assert snapshot.session.pit_window_latest_lap is None
    assert snapshot.session.pit_rejoin_position is None
    assert snapshot.session.forecast == ()
    assert snapshot.session.game_paused is None
    assert snapshot.session.forecast_accuracy is None
    assert snapshot.session.safety_car_periods is None
    assert snapshot.session.virtual_safety_car_periods is None
    assert snapshot.session.red_flag_periods is None
    assert snapshot.session.season_link_id is None
    assert snapshot.session.weekend_link_id is None
    assert snapshot.session.session_link_id is None
    assert snapshot.session.weekend_structure == ()
    assert snapshot.session.num_sessions_in_weekend is None
    assert [item.kind for item in snapshot.events] == ["session_changed", "session_started"]


def test_red_flag_event_is_emitted_once_when_period_counter_increases() -> None:
    state = RaceState()
    session = _parsed_fixture("session.bin")
    assert isinstance(session, SessionPacket)
    base_settings = replace(session.session_settings, red_flag_periods=0)
    state.apply(replace(_with_header(session), session_settings=base_settings), 1.0)

    increased = replace(base_settings, red_flag_periods=1)
    state.apply(
        replace(_with_header(session), session_settings=increased),
        2.0,
    )
    state.apply(
        replace(_with_header(session), session_settings=increased),
        3.0,
    )

    red_flag_events = [event for event in state.snapshot().events if event.kind == "red_flag"]
    assert len(red_flag_events) == 1
    assert red_flag_events[0].occurred_at_monotonic == 2.0
    assert state.snapshot().session.red_flag_periods == 1


def test_session_uid_change_resets_old_state_and_records_transition() -> None:
    state = RaceState()
    participants = _parsed_fixture("participants.bin")
    assert isinstance(participants, ParticipantsPacket)
    rival = participants.participants[1]
    assert rival is not None
    participants_list = list(participants.participants)
    participants_list[1] = replace(
        rival,
        your_telemetry=EnumValue(0, "restricted"),
    )
    state.apply(
        _with_header(replace(participants, participants=tuple(participants_list))),
        observed_at=1.0,
    )
    assert state.snapshot().cars[0].name is not None
    assert state.snapshot().cars[1].telemetry_restricted is True

    tyre_sets = _parsed_fixture("tyre_sets.bin")
    assert isinstance(tyre_sets, TyreSetsPacket)
    state.apply(_with_header(replace(tyre_sets, car_index=0)), observed_at=1.5)
    assert state.snapshot().cars[0].fitted_tyre is not None

    session = _parsed_fixture("session.bin")
    assert isinstance(session, SessionPacket)
    state.apply(_with_header(session, session_uid=2), observed_at=2.0)

    snapshot = state.snapshot()
    assert snapshot.session.uid == 2
    assert snapshot.cars[0].name is None
    assert snapshot.cars[1].telemetry_restricted is None
    assert snapshot.cars[0].fitted_tyre is None
    assert snapshot.freshness_for("participants").updated_at_monotonic is None
    assert [event.kind for event in snapshot.events] == ["session_changed"]
    assert snapshot.events[0].session_uid == 2


def test_shorter_participants_packet_preserves_previously_known_cars() -> None:
    state = RaceState()
    participants = _parsed_fixture("participants.bin")
    assert isinstance(participants, ParticipantsPacket)
    state.apply(_with_header(participants), observed_at=1.0)
    previous_names = tuple(car.name for car in state.snapshot().cars)

    state.apply(
        _with_header(
            replace(participants, active_cars=1, participants=participants.participants[:1])
        ),
        observed_at=2.0,
    )

    assert tuple(car.name for car in state.snapshot().cars) == previous_names


def test_sparse_participants_do_not_overwrite_or_shift_car_indices() -> None:
    state = RaceState()
    participants = _parsed_fixture("participants.bin")
    assert isinstance(participants, ParticipantsPacket)
    state.apply(_with_header(participants), observed_at=1.0)
    previous_name = state.snapshot().cars[0].name
    fifth_slot_participant = participants.participants[0]
    assert fifth_slot_participant is not None
    sparse_participants = (None,) * 5 + (fifth_slot_participant,) + (None,) * 16

    state.apply(
        _with_header(
            replace(
                participants,
                active_cars=1,
                participants=sparse_participants,
            )
        ),
        observed_at=2.0,
    )

    snapshot = state.snapshot()
    assert snapshot.cars[0].name == previous_name
    assert snapshot.cars[5].name == "Driver 01"


def test_unrelated_event_with_other_session_uid_does_not_reset_state() -> None:
    state = RaceState()
    participants = _parsed_fixture("participants.bin")
    event = _parsed_fixture("event.bin")
    assert isinstance(participants, ParticipantsPacket)
    assert isinstance(event, EventPacket)
    state.apply(_with_header(participants, session_uid=1), observed_at=1.0)

    unrelated_event = replace(
        event,
        header=replace(event.header, session_uid=0),
        event_code=EnumValue("BUTN", "buttons"),
    )
    state.apply(unrelated_event, observed_at=2.0)

    snapshot = state.snapshot()
    assert snapshot.session.uid == 1
    assert snapshot.cars[0].name is not None
    assert snapshot.freshness_for("event").updated_at_monotonic is None
    assert snapshot.events == ()


def test_lap_pit_and_session_events_are_emitted_once_per_transition() -> None:
    state = RaceState()
    state.apply(_lap_packet(lap=1), observed_at=1.0)
    state.apply(
        _lap_packet(lap=2, pit_status="pitting"),
        observed_at=2.0,
    )
    state.apply(
        _lap_packet(lap=2, pit_status="in_pit_area"),
        observed_at=3.0,
    )
    state.apply(
        _lap_packet(lap=2, pit_status="none", pit_stops=1),
        observed_at=4.0,
    )

    event_fixture = _parsed_fixture("event.bin")
    assert isinstance(event_fixture, EventPacket)
    event_header = event_fixture.header
    state.apply(
        replace(
            event_fixture,
            header=replace(event_header, session_uid=1, player_car_index=0),
            event_code=EnumValue("SSTA", "session_started"),
            detail=NoEventDetail(),
        ),
        observed_at=5.0,
    )
    state.apply(
        replace(
            event_fixture,
            header=replace(event_header, session_uid=1, player_car_index=0),
            event_code=EnumValue("SEND", "session_ended"),
            detail=NoEventDetail(),
        ),
        observed_at=6.0,
    )
    state.apply(
        replace(
            event_fixture,
            header=replace(event_header, session_uid=1, player_car_index=0),
            event_code=EnumValue("SEND", "session_ended"),
            detail=NoEventDetail(),
        ),
        observed_at=7.0,
    )

    snapshot = state.snapshot()
    assert [event.kind for event in snapshot.events] == [
        "lap_changed",
        "pit_entry",
        "pit_exit",
        "pit_stop_completed",
        "session_started",
        "session_ended",
    ]
    assert snapshot.session.started is True
    assert snapshot.session.ended is True


def test_pit_transitions_are_suppressed_after_retirement() -> None:
    state = RaceState()
    event_fixture = _parsed_fixture("event.bin")
    assert isinstance(event_fixture, EventPacket)
    state.apply(_lap_packet(lap=1, pit_status="none"), 1.0)
    state.apply(
        replace(
            event_fixture,
            event_code=EnumValue("RTMT", "retirement"),
            detail=EventDetail((0, EnumValue(0, "invalid"))),
        ),
        2.0,
    )
    state.apply(_lap_packet(lap=1, pit_status="pitting"), 3.0)

    assert not any(event.kind == "pit_entry" for event in state.snapshot().events)
    assert state.snapshot().cars[0].result_status == "retired"


def test_session_end_is_not_cleared_by_late_start_event_with_same_uid() -> None:
    state = RaceState()
    event_fixture = _parsed_fixture("event.bin")
    assert isinstance(event_fixture, EventPacket)
    header = replace(event_fixture.header, session_uid=1, player_car_index=0)

    state.apply(
        replace(event_fixture, header=header, event_code=EnumValue("SEND", "session_ended")),
        observed_at=1.0,
    )
    state.apply(
        replace(event_fixture, header=header, event_code=EnumValue("SSTA", "session_started")),
        observed_at=1.0,
    )

    snapshot = state.snapshot()
    assert snapshot.session.ended is True
    assert snapshot.session.started is None
    assert [event.kind for event in snapshot.events].count("session_ended") == 1
    assert "session_started" not in [event.kind for event in snapshot.events]


def test_retirement_event_updates_only_its_car_and_is_idempotent() -> None:
    state = RaceState()
    participants = _parsed_fixture("participants.bin")
    event_fixture = _parsed_fixture("event.bin")
    assert isinstance(participants, ParticipantsPacket)
    assert isinstance(event_fixture, EventPacket)
    state.apply(_with_header(participants), 1.0)
    retirement = replace(
        event_fixture,
        event_code=EnumValue("RTMT", "retirement"),
        detail=EventDetail((0, EnumValue(0, "invalid"))),
    )

    state.apply(_with_header(retirement), 2.0)
    state.apply(_with_header(retirement), 3.0)
    state.apply(
        _with_header(replace(retirement, detail=EventDetail((1, EnumValue(1, "retired"))))),
        4.0,
    )
    state.apply(
        _with_header(
            replace(
                retirement,
                detail=EventDetail((len(state.snapshot().cars), EnumValue(0, "invalid"))),
            )
        ),
        5.0,
    )

    snapshot = state.snapshot()
    assert snapshot.cars[0].result_status == "retired"
    assert snapshot.cars[1].result_status == "retired"
    assert snapshot.session.ended is not True
    retirements = [event for event in snapshot.events if event.kind == "retirement"]
    assert len(retirements) == 2
    assert retirements[0].value == "invalid"


def test_matching_uid_classification_updates_player_and_ends_session() -> None:
    state = RaceState()
    participants = _parsed_fixture("participants.bin")
    classification_packet = _parsed_fixture("final_classification.bin")
    assert isinstance(participants, ParticipantsPacket)
    assert isinstance(classification_packet, FinalClassificationPacket)
    state.apply(_with_header(participants, session_uid=1), 1.0)
    state.apply(_lap_packet(lap=6, session_uid=1), 2.0)
    player = classification_packet.classification[0]
    assert player is not None
    entries = list(classification_packet.classification)
    entries[0] = replace(player, result_status=EnumValue(4, "did_not_finish"))
    packet = replace(
        classification_packet,
        classification=tuple(entries),
        header=replace(
            classification_packet.header,
            session_uid=1,
            player_car_index=0,
        ),
    )

    state.apply(packet, 3.0)

    assert state.snapshot().cars[0].result_status == "did_not_finish"
    assert state.snapshot().session.ended is True


def test_zero_uid_classification_updates_matching_player_without_ending_session() -> None:
    state = RaceState()
    participants = _parsed_fixture("participants.bin")
    classification_packet = _parsed_fixture("final_classification.bin")
    assert isinstance(participants, ParticipantsPacket)
    assert isinstance(classification_packet, FinalClassificationPacket)
    state.apply(_with_header(participants, session_uid=1), 1.0)
    state.apply(_lap_packet(lap=6, session_uid=1), 2.0)
    player = classification_packet.classification[0]
    assert player is not None
    entries = list(classification_packet.classification)
    entries[0] = replace(player, laps=5, result_status=EnumValue(4, "did_not_finish"))
    packet = replace(
        classification_packet,
        classification=tuple(entries),
        header=replace(
            classification_packet.header,
            session_uid=0,
            player_car_index=0,
        ),
    )

    state.apply(packet, 3.0)

    snapshot = state.snapshot()
    assert snapshot.session.uid == 1
    assert snapshot.session.ended is not True
    assert snapshot.cars[0].result_status == "did_not_finish"
    assert snapshot.freshness_for("final_classification").updated_at_monotonic == 3.0


def test_zero_uid_classification_from_other_player_is_ignored() -> None:
    state = RaceState()
    participants = _parsed_fixture("participants.bin")
    classification_packet = _parsed_fixture("final_classification.bin")
    assert isinstance(participants, ParticipantsPacket)
    assert isinstance(classification_packet, FinalClassificationPacket)
    state.apply(_with_header(participants, session_uid=1), 1.0)
    state.apply(_lap_packet(lap=6, session_uid=1), 2.0)
    packet = replace(
        classification_packet,
        header=replace(classification_packet.header, session_uid=0, player_car_index=1),
    )

    state.apply(packet, 3.0)

    assert state.snapshot().session.uid == 1
    assert state.snapshot().cars[0].result_status == "active"
    assert state.snapshot().freshness_for("final_classification").updated_at_monotonic is None
