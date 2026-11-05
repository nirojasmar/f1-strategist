"""Weather, forecast, and track-status metrics."""

from __future__ import annotations

from statistics import mean

from ..race_state import PacketFreshness, WeatherForecastSnapshot
from .history import MetricInputs
from .types import Metric, Quality, Window, insufficient, unavailable


class EnvironmentMetrics:
    """Publish current session conditions and horizon-aware rain estimates."""

    name = "environment"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        session = inputs.state.session
        session_freshness = _freshness(inputs, "session")
        stale = session_freshness.is_stale(inputs.now)
        metrics = [
            _session_metric("env.weather", session.weather, "weather", inputs, session_freshness),
            _session_metric(
                "env.track_temperature_c",
                session.track_temperature_c,
                "°C",
                inputs,
                session_freshness,
            ),
            _session_metric(
                "env.air_temperature_c",
                session.air_temperature_c,
                "°C",
                inputs,
                session_freshness,
            ),
            _track_temperature_trend(inputs, session_freshness),
        ]

        forecast = tuple(
            sample
            for sample in session.forecast
            if sample.session_type in {None, session.session_type}
        )
        forecast_accuracy = session.forecast_accuracy
        for horizon in (10, 30):
            key = f"env.rain_probability_next_{horizon}_min_pct"
            probability = _rain_probability(forecast, horizon)
            if probability is None:
                metrics.append(
                    unavailable(
                        key,
                        "%",
                        "forecast_unavailable_for_horizon",
                        inputs.now,
                        window=Window("seconds", horizon * 60),
                        sources=("session_forecast",),
                    )
                )
                continue
            notes: tuple[str, ...]
            quality: Quality
            if forecast_accuracy == "approximate":
                quality, notes = Quality.LOW, ("forecast_approximate",)
            elif forecast_accuracy == "perfect":
                quality, notes = Quality.OK, ()
            else:
                quality, notes = Quality.LOW, ("forecast_accuracy_unknown",)
            if stale:
                quality = Quality.STALE
            metrics.append(
                Metric(
                    key=key,
                    value=probability,
                    unit="%",
                    window=Window("seconds", horizon * 60),
                    quality=quality,
                    sample_count=len(forecast),
                    computed_at_monotonic=inputs.now,
                    sources=("session_forecast",),
                    notes=notes,
                )
            )

        metrics.extend(
            (
                _session_metric(
                    "track.safety_car_status",
                    session.safety_car_status,
                    "status",
                    inputs,
                    session_freshness,
                ),
                _session_metric(
                    "track.player_flag",
                    inputs.car.fia_flag if inputs.car is not None else None,
                    "flag",
                    inputs,
                    _freshness(inputs, "car_status"),
                ),
            )
        )
        return tuple(metrics)


def _freshness(inputs: MetricInputs, group: str) -> PacketFreshness:
    try:
        return inputs.state.freshness_for(group)
    except KeyError:
        return PacketFreshness(group, None, None)


def _session_metric(
    key: str,
    value: float | str | None,
    unit: str,
    inputs: MetricInputs,
    freshness: PacketFreshness,
) -> Metric:
    if value is None:
        return unavailable(key, unit, f"{key.replace('.', '_')}_unavailable", inputs.now)
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("instant"),
        quality=Quality.STALE if freshness.is_stale(inputs.now) else Quality.OK,
        sample_count=1,
        computed_at_monotonic=inputs.now,
        sources=(freshness.group,),
    )


def _rain_probability(
    samples: tuple[WeatherForecastSnapshot, ...], horizon_minutes: int
) -> float | None:
    ordered = sorted(
        (sample for sample in samples if sample.offset_minutes >= 0),
        key=lambda sample: sample.offset_minutes,
    )
    if not ordered:
        return None
    before = [sample for sample in ordered if sample.offset_minutes <= horizon_minutes]
    after = [sample for sample in ordered if sample.offset_minutes >= horizon_minutes]
    if not before or not after:
        return None
    left = before[-1]
    right = after[0]
    if left.offset_minutes == right.offset_minutes:
        return float(left.rain_percentage)
    fraction = (horizon_minutes - left.offset_minutes) / (
        right.offset_minutes - left.offset_minutes
    )
    return left.rain_percentage + (right.rain_percentage - left.rain_percentage) * fraction


def _track_temperature_trend(inputs: MetricInputs, freshness: PacketFreshness) -> Metric:
    records = tuple(
        (float(record.lap_number), record.track_temperature_c)
        for record in inputs.history.records_for(inputs.car_index)
        if record.closed and record.track_temperature_c is not None
    )
    if len(records) < 2:
        return insufficient(
            "env.track_temperature_trend_c_per_lap",
            "°C/lap",
            "track_temperature_history_insufficient",
            inputs.now,
            window=Window("session"),
            sample_count=len(records),
            sources=("lap_history",),
        )
    x_mean = mean(lap for lap, _ in records)
    y_mean = mean(temperature for _, temperature in records if temperature is not None)
    denominator = sum((lap - x_mean) ** 2 for lap, _ in records)
    if denominator == 0:
        return insufficient(
            "env.track_temperature_trend_c_per_lap",
            "°C/lap",
            "track_temperature_history_insufficient",
            inputs.now,
            window=Window("session"),
            sample_count=len(records),
            sources=("lap_history",),
        )
    slope = (
        sum(
            (lap - x_mean) * (temperature - y_mean)
            for lap, temperature in records
            if temperature is not None
        )
        / denominator
    )
    return Metric(
        key="env.track_temperature_trend_c_per_lap",
        value=slope,
        unit="°C/lap",
        window=Window("session"),
        quality=Quality.STALE if freshness.is_stale(inputs.now) else Quality.OK,
        sample_count=len(records),
        computed_at_monotonic=inputs.now,
        sources=("lap_history", "session"),
    )
