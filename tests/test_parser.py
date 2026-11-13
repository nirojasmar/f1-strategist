from __future__ import annotations

import struct
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from f1_strategist.packets import HEADER_SIZE, HEADER_STRUCT, parse_header
from f1_strategist.parser import (
    _ENUMS,
    PACKET_DECODERS,
    SUPPORTED_PACKET_SIZES,
    BooleanValue,
    CarDamagePacket,
    CarStatusPacket,
    EventPacket,
    FinalClassificationPacket,
    LapDataPacket,
    NoEventDetail,
    PacketParseError,
    PacketSizeError,
    ParticipantsPacket,
    PenaltyEventDetail,
    SessionHistoryPacket,
    SessionPacket,
    TyreSetsPacket,
    UnknownEventDetail,
    UnsupportedFormatError,
    UnsupportedPacketTypeError,
    _enum,
    parse_packet,
)

_SUPPORTED_IDS = {1, 2, 3, 4, 7, 8, 10, 11, 12}
_CAPTURE_SIZES = {
    1: 753,
    2: 1285,
    3: 45,
    4: 1284,
    7: 1239,
    8: 1042,
    10: 1041,
    11: 1460,
    12: 231,
}


def _header(
    packet_id: int,
    *,
    packet_format: int = 2025,
    game_year: int = 25,
    major: int = 1,
    minor: int = 26,
    packet_version: int = 1,
) -> bytes:
    return HEADER_STRUCT.pack(
        packet_format,
        game_year,
        major,
        minor,
        packet_version,
        packet_id,
        1,
        1.0,
        1,
        1,
        0,
        255,
    )


def test_capture_sizes_and_registered_decoders_match() -> None:
    assert SUPPORTED_PACKET_SIZES == _CAPTURE_SIZES
    assert PACKET_DECODERS.keys() == _SUPPORTED_IDS
    found = {
        packet_id: parse_packet(
            _header(packet_id) + bytes(SUPPORTED_PACKET_SIZES[packet_id] - HEADER_SIZE)
        )
        for packet_id in _SUPPORTED_IDS
    }

    assert found.keys() == _SUPPORTED_IDS
    assert isinstance(found[1], SessionPacket)
    assert (found[1].total_laps, found[1].track_id.name) == (0, "melbourne")
    assert isinstance(found[2], LapDataPacket)
    assert len(found[2].cars) == 22
    assert isinstance(found[3], EventPacket)
    assert isinstance(found[4], ParticipantsPacket)
    assert len(found[4].participants) == 22
    assert all(participant is None for participant in found[4].participants)
    assert isinstance(found[7], CarStatusPacket)
    assert len(found[7].cars) == 22
    assert isinstance(found[8], FinalClassificationPacket)
    assert found[8].num_cars == 0
    assert len(found[8].classification) == 22
    assert all(entry is None for entry in found[8].classification)
    assert isinstance(found[10], CarDamagePacket)
    assert len(found[10].cars) == 22
    assert isinstance(found[11], SessionHistoryPacket)
    assert found[11].lap_history == ()
    assert isinstance(found[12], TyreSetsPacket)
    assert len(found[12].tyre_sets) == 20


def test_packet_size_must_match_exactly() -> None:
    with pytest.raises(PacketSizeError) as short_packet:
        parse_packet(_header(7) + bytes(1209))
    assert dict(short_packet.value.parameters)["expected"] == 1239
    with pytest.raises(PacketSizeError) as long_packet:
        parse_packet(_header(7) + bytes(1211))
    assert dict(long_packet.value.parameters)["expected"] == 1239


def test_non_finite_measurements_are_rejected() -> None:
    packet = bytearray(_header(7) + bytes(1210))
    struct.pack_into("<f", packet, HEADER_SIZE + 5, float("nan"))

    with pytest.raises(PacketParseError, match="non_finite_float"):
        parse_packet(bytes(packet))


def test_percentages_outside_documented_range_are_rejected() -> None:
    packet = bytearray(_header(10) + bytes(SUPPORTED_PACKET_SIZES[10] - HEADER_SIZE))
    struct.pack_into("<f", packet, HEADER_SIZE, 101.0)

    with pytest.raises(PacketParseError) as exc_info:
        parse_packet(bytes(packet))

    assert exc_info.value.code == "percentage_out_of_range"


@pytest.mark.parametrize(
    "header",
    [
        _header(7, packet_format=2024),
        _header(7, game_year=24),
        _header(7, packet_version=2),
    ],
)
def test_unknown_format_or_version_is_rejected(header: bytes) -> None:
    with pytest.raises(UnsupportedFormatError) as exc_info:
        parse_packet(header + bytes(1210))
    assert exc_info.value.code == "unsupported_format"


def test_unimplemented_packet_type_is_rejected_without_decoding() -> None:
    with pytest.raises(UnsupportedPacketTypeError) as exc_info:
        parse_packet(_header(0) + bytes(1320))
    assert exc_info.value.code == "unsupported_packet_type"


def test_minor_game_patch_is_accepted_when_layout_matches() -> None:
    parsed = parse_packet(_header(7, minor=27) + bytes(1210))
    assert isinstance(parsed, CarStatusPacket)


def test_unknown_enum_and_event_codes_are_preserved_as_unknown() -> None:
    car_status = bytearray(_header(7) + bytes(1210))
    car_status[54] = 254
    decoded_status = parse_packet(bytes(car_status))
    assert isinstance(decoded_status, CarStatusPacket)
    assert decoded_status.cars[0].actual_tyre_compound.raw == 254
    assert decoded_status.cars[0].actual_tyre_compound.name == "unknown"

    event = _header(3) + struct.pack("<4s12s", b"ZZZZ", b"event bytes!")
    decoded_event = parse_packet(event)
    assert isinstance(decoded_event, EventPacket)
    assert decoded_event.event_code.name == "unknown"
    assert isinstance(decoded_event.detail, UnknownEventDetail)
    assert decoded_event.detail.raw == b"event bytes!"


def test_madring_track_id_from_the_2026_dlc_is_decoded() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    session = bytearray((fixtures / "session.bin").read_bytes())
    session[HEADER_SIZE + 7] = 42

    decoded = parse_packet(bytes(session))

    assert isinstance(decoded, SessionPacket)
    assert decoded.track_id.raw == 42
    assert decoded.track_id.name == "madring"


def test_session_history_tyre_stints_follow_spec_order_and_count() -> None:
    body = bytearray(SUPPORTED_PACKET_SIZES[11] - HEADER_SIZE)
    body[:7] = bytes([5, 20, 2, 0, 0, 0, 0])
    stint_offset = 7 + 100 * struct.calcsize("<IHBHBHBB")
    body[stint_offset : stint_offset + 6] = bytes([12, 18, 17, 255, 17, 16])

    parsed = parse_packet(_header(11) + bytes(body))

    assert isinstance(parsed, SessionHistoryPacket)
    assert len(parsed.lap_history) == 20
    assert len(parsed.tyre_stint_history) == 2
    assert [
        (
            stint.end_lap,
            stint.actual_compound.raw,
            stint.visual_compound.raw,
        )
        for stint in parsed.tyre_stint_history
    ] == [(12, 18, 17), (None, 17, 16)]


def test_final_classification_tyre_stints_are_aligned_and_truncated() -> None:
    row = struct.pack(
        "<7BId3B8B8B8B",
        1,
        25,
        3,
        25,
        1,
        3,
        2,
        90000,
        1234.5,
        0,
        0,
        2,
        *[18, 16, 0, 0, 0, 0, 0, 0],
        *[17, 16, 0, 0, 0, 0, 0, 0],
        *[12, 25, 0, 0, 0, 0, 0, 0],
    )
    packet = (
        _header(8)
        + b"\x01"
        + bytes(len(row) * 5)
        + row
        + bytes(SUPPORTED_PACKET_SIZES[8] - HEADER_SIZE - 1 - len(row) * 6)
    )

    parsed = parse_packet(packet)

    assert isinstance(parsed, FinalClassificationPacket)
    assert len(parsed.classification) == 22
    assert parsed.classification[0] is None
    classification = parsed.classification[5]
    assert classification is not None
    assert classification.num_tyre_stints == 2
    assert [value.raw for value in classification.tyre_stints_actual] == [18, 16]
    assert [value.raw for value in classification.tyre_stints_visual] == [17, 16]
    assert classification.tyre_stints_end_laps == (12, 25)


def test_participants_preserve_sparse_vehicle_indices() -> None:
    packet = (Path(__file__).parent / "fixtures" / "participants.bin").read_bytes()
    record_size = (len(packet) - HEADER_SIZE - 1) // 22
    records_start = HEADER_SIZE + 1
    first_record = packet[records_start : records_start + record_size]
    sparse_packet = (
        packet[:HEADER_SIZE]
        + b"\x01"
        + bytes(record_size * 5)
        + first_record
        + bytes(record_size * 16)
    )

    parsed = parse_packet(sparse_packet)

    assert isinstance(parsed, ParticipantsPacket)
    assert len(parsed.participants) == 22
    assert all(parsed.participants[index] is None for index in range(5))
    assert parsed.participants[5] is not None
    assert parsed.participants[5].name == "Driver 01"


def test_no_detail_event_is_not_unknown_and_penalty_has_named_fields() -> None:
    no_detail = parse_packet(_header(3) + struct.pack("<4s12s", b"SSTA", bytes(12)))
    assert isinstance(no_detail, EventPacket)
    assert isinstance(no_detail.detail, NoEventDetail)

    penalty_values = struct.pack("<7B", 4, 9, 1, 2, 5, 6, 3)
    penalty = parse_packet(_header(3) + struct.pack("<4s12s", b"PENA", penalty_values + bytes(5)))
    assert isinstance(penalty, EventPacket)
    assert isinstance(penalty.detail, PenaltyEventDetail)
    assert (
        penalty.detail.vehicle_index,
        penalty.detail.other_vehicle_index,
        penalty.detail.time_seconds,
        penalty.detail.lap_number,
        penalty.detail.places_gained,
    ) == (1, 2, 5, 6, 3)


def test_boolean_values_and_enum_tables_preserve_semantics() -> None:
    lap_data = bytearray(_header(2) + bytes(SUPPORTED_PACKET_SIZES[2] - HEADER_SIZE))
    parsed = parse_packet(bytes(lap_data))
    assert isinstance(parsed, LapDataPacket)
    assert parsed.cars[0].current_lap_invalid == BooleanValue(raw=0, value=False)
    assert BooleanValue(raw=1, value=True).value is True
    assert BooleanValue(raw=2, value=None).raw == 2

    assert _enum("nationality", 82).name == "uruguayan"
    assert _enum("nationality", 83).name == "ukrainian"
    assert _enum("nationality", 52).name == "mexican"
    assert _enum("driver_id", 34).name == "lars_kaufmann"
    assert _enum("nationality", 67).name == "unknown"
    with pytest.raises(ValueError, match="not registered"):
        _enum("not_an_enum", 0)
    assert _ENUMS["team_id"].get(228) is None


def test_packet_decoder_registry_is_immutable() -> None:
    with pytest.raises(TypeError):
        SUPPORTED_PACKET_SIZES[1] = 0  # type: ignore[index]


@given(st.binary(max_size=2000))
def test_arbitrary_datagrams_only_raise_parser_errors(data: bytes) -> None:
    try:
        parse_packet(data)
    except PacketParseError:
        return


@given(st.sampled_from(sorted(_SUPPORTED_IDS)), st.binary(max_size=1500))
def test_random_supported_packet_bodies_only_raise_parser_errors(
    packet_id: int, random_body: bytes
) -> None:
    body_size = SUPPORTED_PACKET_SIZES[packet_id] - HEADER_SIZE
    body = random_body[:body_size].ljust(body_size, b"\0")
    try:
        parse_packet(_header(packet_id) + body)
    except PacketParseError:
        return


@pytest.mark.parametrize(
    "fixture_name",
    [
        "session",
        "lap_data",
        "event",
        "participants",
        "car_status",
        "final_classification",
        "car_damage",
        "session_history",
        "tyre_sets",
    ],
)
def test_real_packet_fixtures_parse(fixture_name: str) -> None:
    packet = (Path(__file__).parent / "fixtures" / f"{fixture_name}.bin").read_bytes()
    header = parse_header(packet)
    assert header is not None
    assert parse_packet(packet)


def test_real_capture_fixtures_preserve_known_values() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    session = parse_packet((fixtures / "session.bin").read_bytes())
    lap_data = parse_packet((fixtures / "lap_data.bin").read_bytes())
    participants = parse_packet((fixtures / "participants.bin").read_bytes())
    status = parse_packet((fixtures / "car_status.bin").read_bytes())
    classification = parse_packet((fixtures / "final_classification.bin").read_bytes())
    history = parse_packet((fixtures / "session_history.bin").read_bytes())

    assert isinstance(session, SessionPacket)
    assert (session.total_laps, session.track_id.name, session.session_type.name) == (
        5,
        "monza",
        "race",
    )
    assert isinstance(lap_data, LapDataPacket)
    assert (
        lap_data.cars[0].car_position,
        lap_data.cars[0].grid_position_zero_based,
    ) == (8, 7)
    assert isinstance(participants, ParticipantsPacket)
    first_participant = participants.participants[0]
    assert first_participant is not None
    assert first_participant.name == "Driver 01"
    assert (
        first_participant.team_id.raw,
        first_participant.team_id.name,
    ) == (
        228,
        "unknown",
    )
    assert isinstance(status, CarStatusPacket)
    assert status.cars[0].actual_tyre_compound.raw == 16
    assert isinstance(classification, FinalClassificationPacket)
    assert classification.num_cars == 22
    first_classification = classification.classification[0]
    assert first_classification is not None
    assert first_classification.num_tyre_stints == 1
    assert [value.raw for value in first_classification.tyre_stints_actual] == [16]
    assert isinstance(history, SessionHistoryPacket)
    assert history.num_laps == 5
    assert history.num_tyre_stints == 1
    assert history.tyre_stint_history[0].end_lap is None


def test_decoded_models_are_immutable() -> None:
    parsed = parse_packet(_header(7) + bytes(1210))
    assert isinstance(parsed, CarStatusPacket)
    with pytest.raises(FrozenInstanceError):
        parsed.cars[0].fuel_in_tank = 3.0  # type: ignore[misc]
