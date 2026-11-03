"""Recorte y anonimización de capturas `.f1cap`."""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .capture import CapturedPacket, CaptureError, CaptureReader, CaptureWriter
from .packets import HEADER_SIZE, SESSION_UID_OFFSET, parse_header
from .parser import (
    SESSION_LINK_IDENTIFIER_OFFSETS,
    LapDataPacket,
    PacketParseError,
    parse_packet,
)

ROTATING_PACKET_IDS = frozenset({11, 12})
_PARTICIPANT = struct.Struct("<7B32sBBHBB12s")
_PARTICIPANT_COUNT = 22
_ANONYMIZED_EPOCH_NS = 946_684_800_000_000_000


@dataclass(frozen=True, slots=True)
class TrimResult:
    packets_read: int
    packets_written: int
    truncated: bool
    dropped_for_anonymization: int = 0


def trim_capture(
    source: Path,
    destination: Path,
    *,
    packet_types: frozenset[int] | None = None,
    decimate: dict[int, int] | None = None,
    laps: tuple[int, int] | None = None,
    anonymize: bool = False,
    keep_ai_names: bool = False,
) -> TrimResult:
    """Escribe una captura filtrada, manteniendo sus tiempos originales."""
    source = Path(source)
    destination = Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("La entrada y la salida deben ser archivos distintos")
    suffix = destination.name.lower()
    if not (suffix.endswith(".f1cap") or suffix.endswith(".f1cap.gz")):
        raise ValueError("La salida debe terminar en `.f1cap` o `.f1cap.gz`")
    if packet_types is not None and any(not 0 <= packet_id <= 15 for packet_id in packet_types):
        raise ValueError("Los IDs de paquete deben estar entre 0 y 15")
    if decimate is not None and any(
        not 0 <= packet_id <= 15 or interval < 1 for packet_id, interval in decimate.items()
    ):
        raise ValueError("Decimación requiere IDs entre 0 y 15 e intervalos >= 1")
    if laps is not None and not 1 <= laps[0] <= laps[1] <= 255:
        raise ValueError("El rango de vueltas debe cumplir 1 <= inicio <= fin <= 255")

    reader = CaptureReader(source)
    time_window: tuple[int, int] | None = None
    context_packets: tuple[CapturedPacket, ...] = ()
    if laps is not None:
        time_window, context_packets = _find_lap_window(reader, laps)

    counts: Counter[tuple[int, int]] = Counter()
    session_uids: dict[int, int] = {}
    packets_read = 0
    packets_written = 0
    dropped_for_anonymization = 0
    first_written_recv_time_ns: int | None = None

    with CaptureWriter(
        destination,
        _output_metadata(reader.metadata, anonymize),
        include_created_utc=False,
    ) as writer:
        for packet in context_packets:
            header = parse_header(packet.payload)
            if header is not None and _type_is_selected(header.packet_id, packet_types):
                output_packet = packet
                if anonymize:
                    if first_written_recv_time_ns is None:
                        first_written_recv_time_ns = packet.recv_time_ns
                    output_packet = _anonymize_packet(
                        packet,
                        session_uids,
                        first_written_recv_time_ns,
                        keep_ai_names,
                    )
                writer.write(output_packet)
                packets_written += 1

        for packet in reader:
            packets_read += 1
            header = parse_header(packet.payload)
            if header is None or not _type_is_selected(header.packet_id, packet_types):
                continue
            if time_window is not None and not (
                time_window[0] <= packet.recv_time_ns < time_window[1]
            ):
                continue
            if anonymize and header.packet_id == 9:
                dropped_for_anonymization += 1
                continue

            car_index = (
                packet.payload[HEADER_SIZE]
                if header.packet_id in ROTATING_PACKET_IDS and len(packet.payload) > HEADER_SIZE
                else -1
            )
            key = (header.packet_id, car_index)
            interval = (decimate or {}).get(header.packet_id, 1)
            counts[key] += 1
            if (counts[key] - 1) % interval:
                continue

            output_packet = packet
            if anonymize:
                if first_written_recv_time_ns is None:
                    first_written_recv_time_ns = packet.recv_time_ns
                output_packet = _anonymize_packet(
                    packet,
                    session_uids,
                    first_written_recv_time_ns,
                    keep_ai_names,
                )
            writer.write(output_packet)
            packets_written += 1

    return TrimResult(
        packets_read,
        packets_written,
        reader.truncated,
        dropped_for_anonymization,
    )


def _type_is_selected(packet_id: int, packet_types: frozenset[int] | None) -> bool:
    return packet_types is None or packet_id in packet_types


def _find_lap_window(
    reader: CaptureReader,
    laps: tuple[int, int],
) -> tuple[tuple[int, int], tuple[CapturedPacket, ...]]:
    start_lap, end_lap = laps
    start_ns: int | None = None
    end_ns: int | None = None
    context: dict[int, CapturedPacket] = {}
    for captured in reader:
        header = parse_header(captured.payload)
        if header is None:
            continue
        if start_ns is None and header.packet_id in {1, 4}:
            context[header.packet_id] = captured
        if header.packet_id != 2:
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
    previous_context = tuple(
        sorted(
            (packet for packet in context.values() if packet.recv_time_ns < start_ns),
            key=lambda packet: packet.recv_time_ns,
        )
    )
    return (start_ns, end_ns), previous_context


def _output_metadata(metadata: dict[str, object], anonymize: bool) -> dict[str, object]:
    if anonymize:
        result = {key: metadata[key] for key in ("tool", "tool_version") if key in metadata}
        if "listen_address" in metadata:
            result["listen_address"] = "127.0.0.1"
        if "listen_port" in metadata:
            result["listen_port"] = 0
        if "allowed_sources" in metadata:
            result["allowed_sources"] = []
        result["anonymized"] = True
        return result
    return dict(metadata)


def _anonymize_packet(
    packet: CapturedPacket,
    session_uids: dict[int, int],
    first_recv_time_ns: int,
    keep_ai_names: bool,
) -> CapturedPacket:
    payload = bytearray(packet.payload)
    header = parse_header(packet.payload)
    if header is None:
        return packet
    uid = header.session_uid
    if uid != 0:
        anonymized_uid = session_uids.setdefault(uid, len(session_uids) + 1)
        struct.pack_into("<Q", payload, SESSION_UID_OFFSET, anonymized_uid)

    if header.packet_id == 1 and len(payload) >= (
        max(SESSION_LINK_IDENTIFIER_OFFSETS) + struct.calcsize("<I")
    ):
        for offset in SESSION_LINK_IDENTIFIER_OFFSETS:
            struct.pack_into("<I", payload, offset, 0)
    elif (
        header.packet_id == 4
        and len(payload) == HEADER_SIZE + 1 + _PARTICIPANT_COUNT * _PARTICIPANT.size
    ):
        for index in range(_PARTICIPANT_COUNT):
            record_start = HEADER_SIZE + 1 + index * _PARTICIPANT.size
            record_end = record_start + _PARTICIPANT.size
            if not any(payload[record_start:record_end]):
                continue
            ai_controlled = payload[record_start]
            payload[record_start + 2] = 0
            if keep_ai_names and ai_controlled == 1:
                continue
            name = f"Driver {index + 1:02d}".encode("ascii")
            name_start = record_start + 7
            payload[name_start : name_start + 32] = name.ljust(32, b"\0")

    rebased_time = _ANONYMIZED_EPOCH_NS + packet.recv_time_ns - first_recv_time_ns
    if not 0 <= rebased_time <= 0xFFFFFFFFFFFFFFFF:
        raise CaptureError("El desplazamiento temporal anonimizado queda fuera de rango")
    return CapturedPacket(
        recv_time_ns=rebased_time,
        source_ip="127.0.0.1",
        source_port=0,
        payload=bytes(payload),
    )
