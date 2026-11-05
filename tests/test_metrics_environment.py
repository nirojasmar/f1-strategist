from __future__ import annotations

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.environment import EnvironmentMetrics
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import (
    CarSnapshot,
    PacketFreshness,
    RaceSessionSnapshot,
    RaceStateSnapshot,
    WeatherForecastSnapshot,
)


def _inputs(
    *,
    forecast: tuple[WeatherForecastSnapshot, ...] = (),
    accuracy: str | None = "perfect",
    stale: bool = False,
    records: tuple[LapRecord, ...] = (),
) -> MetricInputs:
    now = 100.0
    session = RaceSessionSnapshot(
        uid=1,
        session_type="race",
        track="track",
        total_laps=10,
        started=True,
        ended=False,
        weather="light_rain",
        track_temperature_c=31.5,
        air_temperature_c=21.0,
        safety_car_status="virtual_safety_car",
        forecast=forecast,
        forecast_accuracy=accuracy,
    )
    state = RaceStateSnapshot(
        session=session,
        current_lap=3,
        player_car_index=0,
        cars=(CarSnapshot(car_index=0, fia_flag="yellow"),),
        freshness=(
            PacketFreshness("session", now - (10.0 if stale else 0.1), 2.0),
            PacketFreshness("car_status", now - (10.0 if stale else 0.1), 2.0),
        ),
        events=(),
    )
    return MetricInputs(
        state=state,
        history=LapHistorySnapshot(records={0: records} if records else {}),
        config=MetricsConfig(),
        now=now,
        car_index=0,
    )


def _metric(inputs: MetricInputs, key: str):
    return {metric.key: metric for metric in EnvironmentMetrics().compute(inputs)}[key]


def test_environment_and_irregular_forecast_offsets() -> None:
    forecast = (
        WeatherForecastSnapshot(0, "clear", 31.0, 21.0, 0, "race"),
        WeatherForecastSnapshot(15, "light_rain", 30.0, 20.0, 30, "race"),
        WeatherForecastSnapshot(45, "rain", 28.0, 19.0, 90, "race"),
        WeatherForecastSnapshot(15, "rain", 30.0, 20.0, 100, "qualifying"),
    )
    metrics = {
        metric.key: metric for metric in EnvironmentMetrics().compute(_inputs(forecast=forecast))
    }

    assert metrics["env.weather"].value == "light_rain"
    assert metrics["env.track_temperature_c"].value == 31.5
    assert metrics["env.air_temperature_c"].value == 21.0
    assert metrics["env.rain_probability_next_10_min_pct"].value == 20.0
    assert metrics["env.rain_probability_next_30_min_pct"].value == 60.0
    assert metrics["env.rain_probability_next_10_min_pct"].quality is Quality.OK
    assert metrics["track.safety_car_status"].value == "virtual_safety_car"
    assert metrics["track.player_flag"].value == "yellow"


def test_approximate_forecast_is_annotated_and_temperature_trend_requires_history() -> None:
    forecast = (
        WeatherForecastSnapshot(0, "clear", 31.0, 21.0, 0, "race"),
        WeatherForecastSnapshot(30, "rain", 28.0, 19.0, 60, "race"),
    )
    inputs = _inputs(forecast=forecast, accuracy="approximate")

    probability = _metric(inputs, "env.rain_probability_next_10_min_pct")
    trend = _metric(inputs, "env.track_temperature_trend_c_per_lap")

    assert probability.value == 20.0
    assert probability.quality is Quality.LOW
    assert probability.notes == ("forecast_approximate",)
    assert trend.value is None
    assert trend.quality is Quality.INSUFFICIENT
    assert trend.notes == ("track_temperature_history_insufficient",)


def test_track_temperature_trend_uses_lap_history_regression() -> None:
    inputs = _inputs(
        records=(
            LapRecord(car_index=0, lap_number=2, track_temperature_c=30.0, closed=True),
            LapRecord(car_index=0, lap_number=3, track_temperature_c=31.5, closed=True),
            LapRecord(car_index=0, lap_number=4, track_temperature_c=33.0, closed=True),
            LapRecord(car_index=0, lap_number=5, track_temperature_c=90.0, closed=False),
        )
    )

    trend = _metric(inputs, "env.track_temperature_trend_c_per_lap")

    assert trend.value == 1.5
    assert trend.unit == "°C/lap"
    assert trend.quality is Quality.OK
    assert trend.sample_count == 3


def test_missing_forecast_and_stale_conditions() -> None:
    missing = _metric(_inputs(), "env.rain_probability_next_10_min_pct")
    assert missing.value is None
    assert missing.quality is Quality.UNAVAILABLE

    stale = _metric(
        _inputs(
            forecast=(
                WeatherForecastSnapshot(0, "clear", 31.0, 21.0, 0, "race"),
                WeatherForecastSnapshot(30, "rain", 28.0, 19.0, 60, "race"),
            ),
            stale=True,
        ),
        "env.rain_probability_next_10_min_pct",
    )
    assert stale.quality is Quality.STALE
