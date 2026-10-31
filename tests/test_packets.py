from __future__ import annotations

from f1_strategist.packets import HEADER_SIZE, packet_name, parse_header

from .conftest import make_packet


def test_header_size_is_29_bytes() -> None:
    assert HEADER_SIZE == 29


def test_parse_header_roundtrip() -> None:
    header = parse_header(make_packet(6, session_time=12.5))
    assert header is not None
    assert header.packet_format == 2025
    assert header.game_year == 25
    assert header.packet_id == 6
    assert header.session_time == 12.5
    assert header.secondary_player_car_index == 255


def test_short_datagram_has_no_header() -> None:
    assert parse_header(b"\x00" * (HEADER_SIZE - 1)) is None


def test_packet_names() -> None:
    assert packet_name(2) == "LapData"
    assert packet_name(99) == "id 99"
