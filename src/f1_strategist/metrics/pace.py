"""Métricas de ritmo calculadas a partir del histórico de vueltas."""

from __future__ import annotations

from collections.abc import Iterable
from statistics import mean

from ..race_state import PacketFreshness
from .comparability import classify_lap, is_clean
from .history import LapRecord, MetricInputs, current_stint_bounds
from .types import Metric, Quality, Window, insufficient

_NEVER_PACE_FLAGS = frozenset({"partial_lap", "incomplete", "retired"})


def _freshness(inputs: MetricInputs) -> tuple[bool, tuple[str, ...]]:
    sources: list[str] = ["lap_history"]
    stale = False
    for group in ("lap_data", "session_history"):
        try:
            freshness: PacketFreshness = inputs.state.freshness_for(group)
        except KeyError:
            continue
        sources.append(group)
        stale = stale or freshness.is_stale(inputs.now)
    return stale, tuple(sources)


def _regression_slope(
    records: Iterable[LapRecord],
    *,
    corrected: bool = False,
    fuel_effect_s_per_kg: float | None = None,
) -> float | None:
    points: list[tuple[float, float]] = []
    for record in records:
        if record.lap_time_s is None:
            continue
        value = record.lap_time_s
        if corrected:
            if fuel_effect_s_per_kg is None or record.fuel_start_kg is None:
                continue
            value -= record.fuel_start_kg * fuel_effect_s_per_kg
        points.append((float(record.lap_number), value))
    if len(points) < 3:
        return None
    x_mean = mean(x for x, _ in points)
    y_mean = mean(y for _, y in points)
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    if denominator == 0:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator


def _time_metric(
    key: str,
    value: float | None,
    *,
    now: float,
    stale: bool,
    count: int,
    unit: str = "s",
    window: Window,
    sources: tuple[str, ...],
    reason: str = "insufficient_lap_data",
    low_at: int = 1,
    ok_at: int = 1,
) -> Metric:
    if value is None or count < low_at:
        return insufficient(
            key, unit, reason, now, window=window, sample_count=count, sources=sources
        )
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=window,
        quality=Quality.STALE if stale else Quality.OK if count >= ok_at else Quality.LOW,
        sample_count=count,
        computed_at_monotonic=now,
        sources=sources,
    )


class PaceMetrics:
    """Calcula ritmo general y limpio del stint actual; combustible corregido opcional."""

    name = "pace"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        all_records = tuple(
            record
            for record in inputs.history.records_for(inputs.car_index)
            if record.closed and record.lap_time_s is not None
        )
        records = tuple(
            record for record in all_records
            if record.lap_time_s is not None
            and not _NEVER_PACE_FLAGS.intersection(classify_lap(record, inputs.config))
        )
        stale, sources = _freshness(inputs)
        bounds = current_stint_bounds(inputs, inputs.car_index)
        stint_start, stint_end = bounds if bounds is not None else (None, None)
        car = inputs.car
        current_lap = (
            car.current_lap
            if car is not None and car.current_lap is not None
            else max((record.lap_number for record in records), default=0) + 1
        )

        current_stint = tuple(
            record
            for record in records
            if record.lap_number <= current_lap
            if (stint_start is None or record.lap_number >= stint_start)
            and (stint_end is None or record.lap_number <= stint_end)
        )
        clean = tuple(
            record
            for record in current_stint
            if record.valid is not False
            and "telemetry_partial" not in record.flags
            and is_clean(classify_lap(record, inputs.config))
        )
        clean_session = tuple(
            record
            for record in records
            if record.valid is not False
            and "telemetry_partial" not in record.flags
            and is_clean(classify_lap(record, inputs.config))
        )
        official_valid = tuple(
            record
            for record in all_records
            if record.lap_time_source == "session_history"
            and record.valid is True
            and "invalid" not in classify_lap(record, inputs.config)
        )
        clean_window = clean[-inputs.config.pace_window_laps :]
        last_record = records[-1] if records else None
        last_lap = last_record.lap_time_s if last_record is not None else None
        best_lap = min(
            (
                record.lap_time_s
                for record in official_valid
                if record.lap_time_s is not None
            ),
            default=None,
        )
        best_record_count = len(official_valid)
        delta_to_best = (
            last_lap - best_lap if last_lap is not None and best_lap is not None else None
        )
        trend = _regression_slope(clean)
        result = [
            _time_metric(
                "pace.last_lap_s",
                last_lap,
                now=inputs.now,
                stale=stale,
                count=1 if last_record is not None else 0,
                window=Window("instant"),
                sources=sources,
            ),
            _time_metric(
                "pace.best_lap_s",
                best_lap,
                now=inputs.now,
                stale=stale,
                count=best_record_count,
                window=Window("session"),
                sources=sources,
            ),
            _time_metric(
                "pace.clean_mean_s",
                mean(record.lap_time_s for record in clean_window if record.lap_time_s is not None)
                if clean_window
                else None,
                now=inputs.now,
                stale=stale,
                count=len(clean_window),
                window=Window("laps", inputs.config.pace_window_laps),
                sources=sources,
                low_at=inputs.config.thresholds("pace")[0],
                ok_at=inputs.config.thresholds("pace")[1],
            ),
            _time_metric(
                "pace.trend_s_per_lap",
                trend,
                now=inputs.now,
                stale=stale,
                count=len(clean),
                unit="s/lap",
                window=Window("stint"),
                sources=sources,
                reason="fewer_than_three_clean_laps",
                low_at=max(3, inputs.config.thresholds("pace")[0]),
                ok_at=max(3, inputs.config.thresholds("pace")[1]),
            ),
            _time_metric(
                "pace.delta_to_best_s",
                delta_to_best,
                now=inputs.now,
                stale=stale,
                count=1 if delta_to_best is not None else 0,
                window=Window("session"),
                sources=sources,
            ),
            Metric(
                key="pace.clean_laps_used",
                value=len(clean_window),
                unit="laps",
                window=Window("laps", inputs.config.pace_window_laps),
                quality=Quality.STALE if stale else Quality.OK,
                sample_count=len(clean_window),
                computed_at_monotonic=inputs.now,
                sources=sources,
            ),
        ]

        fuel_effect = inputs.config.fuel_effect_s_per_kg
        if fuel_effect is not None:
            corrected_last = (
                last_lap - last_record.fuel_start_kg * fuel_effect
                if last_record is not None
                and last_record.fuel_start_kg is not None
                and last_lap is not None
                else None
            )
            corrected_all = tuple(
                record
                for record in clean_session
                if record.fuel_start_kg is not None and record.lap_time_s is not None
            )
            corrected_clean = tuple(
                record
                for record in clean
                if record.fuel_start_kg is not None and record.lap_time_s is not None
            )
            corrected_clean_window = corrected_clean[-inputs.config.pace_window_laps :]
            corrected_times = tuple(
                record.lap_time_s - record.fuel_start_kg * fuel_effect
                for record in corrected_clean_window
                if record.lap_time_s is not None and record.fuel_start_kg is not None
            )
            corrected_best = min(
                (
                    record.lap_time_s - record.fuel_start_kg * fuel_effect
                    for record in corrected_all
                    if record.lap_time_s is not None and record.fuel_start_kg is not None
                ),
                default=None,
            )
            corrected_trend = _regression_slope(
                corrected_clean,
                corrected=True,
                fuel_effect_s_per_kg=fuel_effect,
            )
            corrected_delta = (
                corrected_last - corrected_best
                if corrected_last is not None and corrected_best is not None
                else None
            )
            result.extend(
                (
                    _time_metric(
                        "pace.last_lap_fuel_corrected_s",
                        corrected_last,
                        now=inputs.now,
                        stale=stale,
                        count=1 if corrected_last is not None else 0,
                        window=Window("instant"),
                        sources=sources,
                        reason="missing_fuel_data",
                    ),
                    _time_metric(
                        "pace.best_lap_fuel_corrected_s",
                        corrected_best,
                        now=inputs.now,
                        stale=stale,
                        count=len(corrected_all),
                        window=Window("session"),
                        sources=sources,
                        reason="missing_fuel_data",
                    ),
                    _time_metric(
                        "pace.clean_mean_fuel_corrected_s",
                        mean(corrected_times) if corrected_times else None,
                        now=inputs.now,
                        stale=stale,
                        count=len(corrected_times),
                        window=Window("laps", inputs.config.pace_window_laps),
                        sources=sources,
                        reason="missing_fuel_data",
                        low_at=inputs.config.thresholds("pace")[0],
                        ok_at=inputs.config.thresholds("pace")[1],
                    ),
                    _time_metric(
                        "pace.trend_fuel_corrected_s_per_lap",
                        corrected_trend,
                        now=inputs.now,
                        stale=stale,
                        count=len(corrected_clean),
                        unit="s/lap",
                        window=Window("stint"),
                        sources=sources,
                        reason="fewer_than_three_fuel_samples",
                        low_at=max(3, inputs.config.thresholds("pace")[0]),
                        ok_at=max(3, inputs.config.thresholds("pace")[1]),
                    ),
                    _time_metric(
                        "pace.delta_to_best_fuel_corrected_s",
                        corrected_delta,
                        now=inputs.now,
                        stale=stale,
                        count=1 if corrected_delta is not None else 0,
                        window=Window("session"),
                        sources=sources,
                        reason="missing_fuel_data",
                    ),
                )
            )
        return tuple(result)


PaceModule = PaceMetrics
