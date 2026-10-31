"""Resumen de una captura (`f1-strategist inspect`)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capture import CaptureReader
from .packets import parse_header


@dataclass(frozen=True, slots=True)
class CaptureSummary:
    path: Path
    metadata: dict[str, Any]
    packets: int
    total_bytes: int
    duration_s: float
    sources: dict[str, int]
    packets_by_id: dict[int, int]
    formats: dict[tuple[int, int, int, int], int]  # (formato, año, mayor, menor) -> paquetes
    invalid: int
    truncated: bool


def summarize(path: Path) -> CaptureSummary:
    reader = CaptureReader(path)
    sources: Counter[str] = Counter()
    by_id: Counter[int] = Counter()
    formats: Counter[tuple[int, int, int, int]] = Counter()
    packets = total_bytes = invalid = 0
    first_ns = last_ns = None
    for packet in reader:
        packets += 1
        total_bytes += len(packet.payload)
        sources[packet.source_ip] += 1
        first_ns = packet.recv_time_ns if first_ns is None else first_ns
        last_ns = packet.recv_time_ns
        header = parse_header(packet.payload)
        if header is None:
            invalid += 1
            continue
        by_id[header.packet_id] += 1
        formats[
            (
                header.packet_format,
                header.game_year,
                header.game_major_version,
                header.game_minor_version,
            )
        ] += 1
    duration = 0.0 if first_ns is None or last_ns is None else (last_ns - first_ns) / 1e9
    return CaptureSummary(
        path=Path(path),
        metadata=reader.metadata,
        packets=packets,
        total_bytes=total_bytes,
        duration_s=duration,
        sources=dict(sources),
        packets_by_id=dict(by_id),
        formats=dict(formats),
        invalid=invalid,
        truncated=reader.truncated,
    )
