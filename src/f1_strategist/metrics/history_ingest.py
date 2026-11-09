"""Ingesta de tiempos oficiales y stints desde paquetes SessionHistory."""

from __future__ import annotations

from ..parser import SessionHistoryPacket
from .history import LapHistory, TyreStint

_LAP_VALID = 0b0001
_SECTOR1_VALID = 0b0010
_SECTOR2_VALID = 0b0100
_SECTOR3_VALID = 0b1000


def ingest_session_history(packet: SessionHistoryPacket, history: LapHistory) -> str | None:
    """Actualiza tiempos oficiales y stints si la secuencia recibida es coherente.

    Devuelve un diagnóstico cuando se conserva la última secuencia válida de
    stints. Los tiempos oficiales se ingieren aunque los stints sean anómalos.
    """
    if history.session_uid is not None and history.session_uid != packet.header.session_uid:
        return None

    stints, anomaly = _validated_stints(packet)
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

    if anomaly is None:
        history.replace_tyre_stints(packet.car_index, stints)
    return anomaly


def _validated_stints(packet: SessionHistoryPacket) -> tuple[list[TyreStint], str | None]:
    stints: list[TyreStint] = []
    start_lap = 1
    has_open_stint = False
    for index, stint in enumerate(packet.tyre_stint_history):
        if has_open_stint:
            return [], "stint_after_open_stint"
        if stint.end_lap is not None and (
            stint.end_lap < start_lap or stint.end_lap < 1 or stint.end_lap > packet.num_laps
        ):
            return [], "invalid_stint_end_lap"
        if stint.end_lap is None and index != len(packet.tyre_stint_history) - 1:
            return [], "open_stint_not_last"
        stints.append(
            TyreStint(
                compound=_known_compound(stint.actual_compound.name),
                visual_compound=_known_compound(stint.visual_compound.name),
                start_lap=start_lap,
                end_lap=stint.end_lap,
            )
        )
        if stint.end_lap is None:
            has_open_stint = True
        else:
            start_lap = stint.end_lap + 1
    return stints, None


def _known_compound(value: str) -> str | None:
    return value if value != "unknown" else None
