"""Métricas de progreso de sesión."""

from __future__ import annotations

from ..race_state import PacketFreshness
from .history import MetricInputs
from .types import Metric, Quality, Window, unavailable


class ProgressMetrics:
    """Calcula el progreso de carrera a partir de la telemetría actual."""

    name = "progress"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        car = inputs.car
        session = inputs.state.session
        lap_freshness = inputs.state.freshness_for("lap_data")
        session_freshness = inputs.state.freshness_for("session")

        current_lap = car.current_lap if car is not None else None
        current_lap_metric = (
            _available(
                "progress.current_lap",
                current_lap,
                "lap",
                inputs.now,
                lap_freshness,
                sources=("lap_data",),
            )
            if current_lap is not None
            else unavailable(
                "progress.current_lap",
                "lap",
                "progress_lap_unavailable",
                inputs.now,
                sources=("lap_data",),
            )
        )

        if session.ended is True and session.total_laps is not None:
            laps_completed = session.total_laps
        elif current_lap is not None:
            laps_completed = max(0, current_lap - 1)
            if session.total_laps is not None:
                laps_completed = min(laps_completed, session.total_laps)
        else:
            laps_completed = None

        laps_completed_metric = (
            _available(
                "progress.laps_completed",
                laps_completed,
                "lap",
                inputs.now,
                lap_freshness,
                sources=("lap_data",),
            )
            if laps_completed is not None
            else unavailable(
                "progress.laps_completed",
                "lap",
                "progress_lap_unavailable",
                inputs.now,
                sources=("lap_data",),
            )
        )

        laps_remaining: float | None = None
        if session.total_laps is not None:
            if session.ended is True:
                laps_remaining = 0.0
            elif current_lap is not None:
                distance = car.lap_distance_m if car is not None else None
                if (
                    distance is not None
                    and session.track_length_m is not None
                    and session.track_length_m > 0
                ):
                    lap_fraction = min(max(distance / session.track_length_m, 0.0), 1.0)
                    laps_remaining = max(
                        0.0,
                        session.total_laps - (current_lap - 1) - lap_fraction,
                    )

        laps_remaining_metric = (
            _available(
                "progress.laps_remaining",
                laps_remaining,
                "lap",
                inputs.now,
                _stale(inputs.now, lap_freshness, session_freshness),
                sources=("lap_data", "session"),
            )
            if laps_remaining is not None
            else unavailable(
                "progress.laps_remaining",
                "lap",
                "progress_laps_remaining_unavailable",
                inputs.now,
                sources=("lap_data", "session"),
            )
        )

        time_left = session.session_time_left_s
        session_time_metric = (
            _available(
                "progress.session_time_left_s",
                time_left,
                "s",
                inputs.now,
                session_freshness,
                sources=("session",),
            )
            if time_left is not None
            else unavailable(
                "progress.session_time_left_s",
                "s",
                "progress_session_time_unavailable",
                inputs.now,
                sources=("session",),
            )
        )

        race_progress: float | None = None
        if session.total_laps is not None and session.total_laps > 0:
            if session.ended is True:
                race_progress = 100.0
            elif current_lap is not None:
                distance = car.lap_distance_m if car is not None else None
                if (
                    distance is not None
                    and session.track_length_m is not None
                    and session.track_length_m > 0
                ):
                    lap_fraction = min(max(distance / session.track_length_m, 0.0), 1.0)
                    race_progress = min(
                        100.0,
                        max(0.0, 100.0 * ((current_lap - 1 + lap_fraction) / session.total_laps)),
                    )

        race_progress_metric = (
            _available(
                "progress.race_progress_pct",
                race_progress,
                "%",
                inputs.now,
                _stale(inputs.now, lap_freshness, session_freshness),
                sources=("lap_data", "session"),
            )
            if race_progress is not None
            else unavailable(
                "progress.race_progress_pct",
                "%",
                "progress_total_laps_unavailable",
                inputs.now,
                sources=("lap_data", "session"),
            )
        )
        return (
            current_lap_metric,
            laps_completed_metric,
            laps_remaining_metric,
            session_time_metric,
            race_progress_metric,
        )


def _available(
    key: str,
    value: float | int | None,
    unit: str,
    now: float,
    freshness: PacketFreshness | bool,
    *,
    sources: tuple[str, ...],
) -> Metric:
    stale = freshness if isinstance(freshness, bool) else freshness.is_stale(now)
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("instant"),
        quality=Quality.STALE if stale else Quality.OK,
        sample_count=1,
        computed_at_monotonic=now,
        sources=sources,
    )


def _stale(now: float, *freshness: PacketFreshness) -> bool:
    return any(item.is_stale(now) for item in freshness)
