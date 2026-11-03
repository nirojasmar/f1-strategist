"""Recorte y anonimización de capturas `.f1cap`."""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .capture import (
    CapturedPacket,
    CaptureError,
    CaptureReader,
    CaptureWriter,
)
from .packets import HEADER_SIZE, parse_header
from .parser import LapDataPacket, PacketParseError, parse_packet

_PARTICIPANT_SIZE = struct.Struct("<7B32sBBHBB12s").size
_PARTICIPANT_COUNT = 22
_SESSION_UID_OFFSET = 7


@dataclass(frozen=True, slots=True)
class TrimResult:
    packets_read: int
    packets_written: int
    truncated: bool


def trim_capture(
    source: Path,
    destination: Path,
    *,
    packet_types: frozenset[int] | None = None,
    decimate: dict[int, int] | None = None,
    laps: tuple[int, int] | None = None,
    anonymize: bool = False,
) -> TrimResult:
    """Escribe una captura filtrada, manteniendo sus tiempos originales."""
    source = Path(source)
    destination = Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("La entrada y la salida deben ser archivos distintos")
    if destination.name.lower().endswith(".f1cap.gz"):
        raise ValueError("La salida debe ser `.f1cap` sin comprimir")
    if packet_types is not None and any(not 0 <= packet_id <= 15 for packet_id in packet_types):
        raise ValueError("Los IDs de paquete deben estar entre 0 y 15")
    if decimate is not None and any(
        not 0 <= packet_id <= 15 or interval < 1 for packet_id, interval in decimate.items()
    ):
        raise ValueError("Decimación requiere IDs entre 0 y 15 e intervalos >= 1")
    if laps is not None and not 1 <= laps[0] <= laps[1] <= 255:
        raise ValueError("El rango de vueltas debe cumplir 1 <= inicio <= fin <= 255")

    reader = CaptureReader(source)
    time_window = _find_lap_window(reader, laps) if laps is not None else None
    counts_by_type: Counter[int] = Counter()
    packets_read = 0
    packets_written = 0
    with CaptureWriter(destination, _output_metadata(reader.metadata, anonymize)) as writer:
        for packet in reader:
            packets_read += 1
            header = parse_header(packet.payload)
            if packet_types is not None and (
                header is None or header.packet_id not in packet_types
            ):
                continue
            if time_window is not None and not (
                time_window[0] <= packet.recv_time_ns < time_window[1]
            ):
                continue
            packet_id = header.packet_id if header is not None else -1
            interval = (decimate or {}).get(packet_id, 1)
            counts_by_type[packet_id] += 1
            if (counts_by_type[packet_id] - 1) % interval:
                continue

            output_packet = packet
            if anonymize:
                output_packet = CapturedPacket(
                    recv_time_ns=packet.recv_time_ns,
                    source_ip="127.0.0.1",
                    source_port=0,
                    payload=_anonymize_payload(packet.payload),
                )
            writer.write(output_packet)
            packets_written += 1

    return TrimResult(packets_read, packets_written, reader.truncated)


def _find_lap_window(
    reader: CaptureReader,
    laps: tuple[int, int] | None,
) -> tuple[int, int]:
    assert laps is not None
    start_lap, end_lap = laps
    start_ns: int | None = None
    end_ns: int | None = None
    for captured in reader:
        header = parse_header(captured.payload)
        if header is None or header.packet_id != 2:
            continue
        try:
            packet = parse_packet(captured.payload)
        except PacketParseError:
            continue
        if not isinstance(packet, LapDataPacket):
            continue
        if header.player_car_index >= len(packet.cars):
            continue
        current_lap = packet.cars[header.player_car_index].current_lap_number
        if start_ns is None and current_lap >= start_lap:
            start_ns = captured.recv_time_ns
        if current_lap > end_lap:
            end_ns = captured.recv_time_ns
            break
    if start_ns is None:
        raise CaptureError(f"No se encontró la vuelta inicial {start_lap} en la captura")
    if end_ns is None:
        end_ns = 0xFFFFFFFFFFFFFFFF
    return start_ns, end_ns


def _output_metadata(metadata: dict[str, object], anonymize: bool) -> dict[str, object]:
    result = dict(metadata)
    if anonymize:
        if "listen_address" in result:
            result["listen_address"] = "127.0.0.1"
        if "listen_port" in result:
            result["listen_port"] = 0
        if "allowed_sources" in result:
            result["allowed_sources"] = []
        result["anonymized"] = True
    return result


def _anonymize_payload(payload: bytes) -> bytes:
    result = bytearray(payload)
    header = parse_header(payload)
    if header is None:
        return payload
    struct.pack_into("<Q", result, _SESSION_UID_OFFSET, 1)
    if (
        header.packet_id == 4
        and len(result) == HEADER_SIZE + 1 + _PARTICIPANT_COUNT * _PARTICIPANT_SIZE
    ):
        for index in range(_PARTICIPANT_COUNT):
            record_start = HEADER_SIZE + 1 + index * _PARTICIPANT_SIZE
            if not any(result[record_start : record_start + _PARTICIPANT_SIZE]):
                continue
            result[record_start + 2] = 0
            name = f"Driver {index + 1:02d}".encode("ascii")
            name_start = record_start + 7
            result[name_start : name_start + 32] = name.ljust(32, b"\0")
    return bytes(result)
