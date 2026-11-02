from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from f1_strategist.parser import (
    CarStatusPacket,
    EnumValue,
    EventPacket,
    LapDataPacket,
    NoEventDetail,
    ParsedPacket,
    ParticipantsPacket,
    SessionPacket,
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
    first_participant = replace(participants.participants[0], name="Test Driver")
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
