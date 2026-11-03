from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from f1_strategist.parser import (
    BooleanValue,
    CarDamagePacket,
    CarStatusPacket,
    EnumValue,
    EventPacket,
    LapDataPacket,
    NoEventDetail,
    ParsedPacket,
    ParticipantsPacket,
    SessionPacket,
    TyreSetsPacket,
    parse_packet,
)
from f1_strategist.race_state import RaceState

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
    assert snapshot.freshness_for("tyre_sets").updated_at_monotonic == 2.5

    state.apply(
        _with_header(replace(packet, car_index=1, fitted_index=255)),
        observed_at=3.0,
    )
    assert state.snapshot().cars[1].fitted_tyre is None


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


def test_session_packet_publishes_environment_and_forecast() -> None:
    state = RaceState()
    session = _parsed_fixture("session.bin")
    assert isinstance(session, SessionPacket)

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
    assert len(snapshot.session.forecast) == 3
    first_sample = snapshot.session.forecast[0]
    assert first_sample.offset_minutes == 0
    assert first_sample.weather == "clear"
    assert first_sample.track_temperature_c == 35.0
    assert first_sample.air_temperature_c == 25.0
    assert first_sample.rain_percentage == 6


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
    assert [item.kind for item in snapshot.events] == ["session_changed", "session_started"]


def test_session_uid_change_resets_old_state_and_records_transition() -> None:
    state = RaceState()
    participants = _parsed_fixture("participants.bin")
    assert isinstance(participants, ParticipantsPacket)
    state.apply(_with_header(participants), observed_at=1.0)
    assert state.snapshot().cars[0].name is not None

    session = _parsed_fixture("session.bin")
    assert isinstance(session, SessionPacket)
    state.apply(_with_header(session, session_uid=2), observed_at=2.0)

    snapshot = state.snapshot()
    assert snapshot.session.uid == 2
    assert snapshot.cars[0].name is None
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
        "pit_stop_completed",
        "session_started",
        "session_ended",
    ]
    assert snapshot.session.started is True
    assert snapshot.session.ended is True
