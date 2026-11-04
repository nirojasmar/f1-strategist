"""Ingesta de tiempos oficiales y stints desde paquetes SessionHistory."""

from __future__ import annotations

from ..parser import SessionHistoryPacket
from .history import LapHistory, TyreStint

_LAP_VALID = 0b0001
_SECTOR1_VALID = 0b0010
_SECTOR2_VALID = 0b0100
_SECTOR3_VALID = 0b1000


def ingest_session_history(packet: SessionHistoryPacket, history: LapHistory) -> None:
    """Actualiza vueltas ya conocidas y stints; los tiempos oficiales prevalecen."""
    if history.session_uid is not None and history.session_uid != packet.header.session_uid:
        return

    for lap_number, lap in enumerate(packet.lap_history, start=1):
        if lap.lap_time_ms <= 0:
            continue
        flags = lap.valid_bit_flags
        history.update_lap_timing(
            packet.car_index,
            lap_number,
            lap_time_s=lap.lap_time_ms / 1000.0,
            sector1_s=(
                lap.sector1_time_ms / 1000.0
                if flags & _SECTOR1_VALID and lap.sector1_time_ms > 0
                else None
            ),
            sector2_s=(
                lap.sector2_time_ms / 1000.0
                if flags & _SECTOR2_VALID and lap.sector2_time_ms > 0
                else None
            ),
            sector3_s=(
                lap.sector3_time_ms / 1000.0
                if flags & _SECTOR3_VALID and lap.sector3_time_ms > 0
                else None
            ),
            valid=bool(flags & _LAP_VALID),
        )

    stints: list[TyreStint] = []
    start_lap = 1
    for stint in packet.tyre_stint_history:
        stints.append(
            TyreStint(
                compound=stint.actual_compound.name,
                visual_compound=stint.visual_compound.name,
                start_lap=start_lap,
                end_lap=stint.end_lap,
            )
        )
        if stint.end_lap is not None:
            start_lap = stint.end_lap + 1
    history.replace_tyre_stints(packet.car_index, stints)
