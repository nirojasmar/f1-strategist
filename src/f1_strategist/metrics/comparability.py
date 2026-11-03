"""Clasificación determinista de vueltas según su comparabilidad."""

from __future__ import annotations

from ..config import MetricsConfig
from .history import LapRecord

_SAFETY_CAR_STATUSES = frozenset({"full_safety_car", "safety_car"})
_VIRTUAL_SAFETY_CAR_STATUSES = frozenset({"virtual_safety_car"})


def classify_lap(record: LapRecord, config: MetricsConfig) -> frozenset[str]:
    """Clasifica los factores que hacen una vuelta menos comparable."""
    flags = set(record.flags)

    if record.lap_number == 1 or "first_lap" in record.flags:
        flags.add("first_lap")
    if record.pit_entry:
        flags.add("in_lap")
    if record.pit_exit:
        flags.add("out_lap")
    if record.valid is False:
        flags.add("invalid")
    if _SAFETY_CAR_STATUSES.intersection(record.track_status_seen):
        flags.add("safety_car")
    if _VIRTUAL_SAFETY_CAR_STATUSES.intersection(record.track_status_seen):
        flags.add("virtual_safety_car")
    if "red_flag" in record.track_status_seen:
        flags.add("red_flag")
    if "flashback" in record.flags:
        flags.add("flashback")
    if len(record.weather_seen) > 1:
        flags.add("weather_change")
    if (
        record.damage_max_delta_pct is not None
        and record.damage_max_delta_pct > 0
        and record.damage_max_delta_pct >= config.damage_change_threshold_pct
    ):
        flags.add("damage_change")
    if (
        record.traffic_fraction is not None
        and record.traffic_fraction > 0
        and record.traffic_fraction >= config.traffic_lap_fraction
    ):
        flags.add("traffic")

    return frozenset(flags)


def is_clean(flags: frozenset[str] | set[str], *, allow_traffic: bool = False) -> bool:
    """Indica si una vuelta carece de banderas no permitidas."""
    if allow_traffic:
        return flags <= {"traffic"}
    return not flags
