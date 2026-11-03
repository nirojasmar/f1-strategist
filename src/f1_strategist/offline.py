"""Reproduce capturas localmente a través de consumidores del pipeline."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .capture import CaptureReader
from .packets import parse_header
from .parser import (
    PACKET_DECODERS,
    PacketParseError,
    ParsedPacket,
    ParseIssue,
    _record_parser_rejection,
    parse_packet,
)

_SUPPORTED_HEADER_VERSION = (2025, 25, 1, 1)


class PacketConsumer(Protocol):
    def apply(self, packet: ParsedPacket, observed_at: float) -> None: ...


@dataclass(frozen=True, slots=True)
class OfflineResult:
    packets_read: int
    packets_parsed: int
    parser_rejections: Mapping[ParseIssue, int]
    ignored_packets_by_id: Mapping[int, int]
    truncated: bool


def run_capture_offline(
    path: Path,
    consumers: Iterable[PacketConsumer],
    *,
    speed: float | None = None,
) -> OfflineResult:
    """Procesa una captura sin red, hilos ni esperas.

    `speed` escala el reloj simulado como en `replay`: 2.0 acelera el tiempo
    a la mitad y 0.0 asigna el mismo instante a todos los paquetes.
    """
    playback_speed = 1.0 if speed is None else speed
    if not math.isfinite(playback_speed) or playback_speed < 0:
        raise ValueError("speed debe ser un número finito no negativo")

    reader = CaptureReader(path)
    packet_consumers = tuple(consumers)
    rejections: Counter[ParseIssue] = Counter()
    ignored: Counter[int] = Counter()
    packets_read = 0
    packets_parsed = 0
    first_recv_time_ns: int | None = None
    last_observed_at = 0.0

    for captured in reader:
        packets_read += 1
        if first_recv_time_ns is None:
            first_recv_time_ns = captured.recv_time_ns

        elapsed = (captured.recv_time_ns - first_recv_time_ns) / 1_000_000_000
        observed_at = 0.0 if playback_speed == 0 else elapsed / playback_speed
        observed_at = max(last_observed_at, observed_at)
        last_observed_at = observed_at

        header = parse_header(captured.payload)
        if (
            header is not None
            and 0 <= header.packet_id <= 15
            and header.packet_id not in PACKET_DECODERS
            and (
                header.packet_format,
                header.game_year,
                header.game_major_version,
                header.packet_version,
            )
            == _SUPPORTED_HEADER_VERSION
        ):
            ignored[header.packet_id] += 1
            continue

        try:
            packet = parse_packet(captured.payload)
        except PacketParseError as exc:
            _record_parser_rejection(rejections, exc.issue)
            continue

        packets_parsed += 1
        for consumer in packet_consumers:
            consumer.apply(packet, observed_at)

    return OfflineResult(
        packets_read=packets_read,
        packets_parsed=packets_parsed,
        parser_rejections=MappingProxyType(dict(rejections)),
        ignored_packets_by_id=MappingProxyType(dict(ignored)),
        truncated=reader.truncated,
    )
