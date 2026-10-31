from __future__ import annotations

import struct

from f1_strategist.packets import HEADER_STRUCT


def make_packet(packet_id: int, session_time: float = 0.0, extra: bytes = b"") -> bytes:
    """Datagrama sintético: cabecera válida (formato 2025, año 25) más un cuerpo arbitrario."""
    header = HEADER_STRUCT.pack(2025, 25, 1, 5, 1, packet_id, 0xABCDEF, session_time, 1, 1, 0, 255)
    return header + extra + struct.pack("<B", packet_id)
