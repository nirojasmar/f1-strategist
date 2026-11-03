"""Cabecera común de 29 bytes de los paquetes UDP de F1 25.

Layout (little-endian, 29 bytes), según el formato de F1 24/F1 25:

    uint16 packet_format, uint8 game_year, uint8 game_major_version,
    uint8 game_minor_version, uint8 packet_version, uint8 packet_id,
    uint64 session_uid, float session_time, uint32 frame_identifier,
    uint32 overall_frame_identifier, uint8 player_car_index,
    uint8 secondary_player_car_index

El parser por tipo y las versiones admitidas están en `parser.py`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER_STRUCT = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE = HEADER_STRUCT.size  # 29
SESSION_UID_OFFSET = struct.calcsize("<HBBBBB")

PACKET_NAMES: dict[int, str] = {
    0: "Motion",
    1: "Session",
    2: "LapData",
    3: "Event",
    4: "Participants",
    5: "CarSetups",
    6: "CarTelemetry",
    7: "CarStatus",
    8: "FinalClassification",
    9: "LobbyInfo",
    10: "CarDamage",
    11: "SessionHistory",
    12: "TyreSets",
    13: "MotionEx",
    14: "TimeTrial",
    15: "LapPositions",
}


@dataclass(frozen=True, slots=True)
class PacketHeader:
    packet_format: int
    game_year: int
    game_major_version: int
    game_minor_version: int
    packet_version: int
    packet_id: int
    session_uid: int
    session_time: float
    frame_identifier: int
    overall_frame_identifier: int
    player_car_index: int
    secondary_player_car_index: int


def parse_header(data: bytes) -> PacketHeader | None:
    """Devuelve la cabecera o None si el datagrama es demasiado corto."""
    if len(data) < HEADER_SIZE:
        return None
    return PacketHeader(*HEADER_STRUCT.unpack_from(data))


def packet_name(packet_id: int) -> str:
    return PACKET_NAMES.get(packet_id, f"id {packet_id}")
