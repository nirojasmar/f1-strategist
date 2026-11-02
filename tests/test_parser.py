from __future__ import annotations

import struct
from dataclasses import FrozenInstanceError

import pytest

from f1_strategist.packets import HEADER_SIZE, HEADER_STRUCT
from f1_strategist.parser import (
    PACKET_DECODERS,
    SUPPORTED_PACKET_SIZES,
    CarDamagePacket,
    CarStatusPacket,
    EventPacket,
    FinalClassificationPacket,
    LapDataPacket,
    PacketSizeError,
    ParticipantsPacket,
    SessionHistoryPacket,
    SessionPacket,
    TyreSetsPacket,
    UnknownEventDetail,
    UnsupportedFormatError,
    UnsupportedPacketTypeError,
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
    assert isinstance(found[7], CarStatusPacket)
    assert len(found[7].cars) == 22
    assert isinstance(found[8], FinalClassificationPacket)
    assert found[8].num_cars == 0
    assert isinstance(found[10], CarDamagePacket)
    assert len(found[10].cars) == 22
    assert isinstance(found[11], SessionHistoryPacket)
    assert len(found[11].lap_history) == 100
    assert isinstance(found[12], TyreSetsPacket)
    assert len(found[12].tyre_sets) == 20


def test_packet_size_must_match_exactly() -> None:
    with pytest.raises(PacketSizeError, match="se esperaban 1239"):
        parse_packet(_header(7) + bytes(1209))
    with pytest.raises(PacketSizeError, match="se esperaban 1239"):
        parse_packet(_header(7) + bytes(1211))


@pytest.mark.parametrize(
    ("header", "message"),
    [
        (_header(7, packet_format=2024), "formato 2024"),
        (_header(7, game_year=24), "año 24"),
        (_header(7, minor=27), "juego 1.27"),
        (_header(7, packet_version=2), "paquete v2"),
    ],
)
def test_unknown_format_or_version_is_rejected(header: bytes, message: str) -> None:
    with pytest.raises(UnsupportedFormatError, match=message):
        parse_packet(header + bytes(1210))


def test_unimplemented_packet_type_is_rejected_without_decoding() -> None:
    with pytest.raises(UnsupportedPacketTypeError, match="Motion.*sin decodificador"):
        parse_packet(_header(0) + bytes(1320))


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


def test_decoded_models_are_immutable() -> None:
    parsed = parse_packet(_header(7) + bytes(1210))
    assert isinstance(parsed, CarStatusPacket)
    with pytest.raises(FrozenInstanceError):
        parsed.cars[0].fuel_in_tank = 3.0  # type: ignore[misc]
