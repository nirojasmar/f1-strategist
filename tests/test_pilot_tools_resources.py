"""Pruebas de las herramientas de recursos del piloto (Q5)."""

from __future__ import annotations

import pytest

from f1_strategist.config import PilotConfig
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.pilot.tools_resources import (
    TOOLS,
    damage_status,
    estimate_fuel_margin,
    estimate_tyre_life,
    get_race_state,
    get_recent_pace_trend,
    weather_forecast,
)
from f1_strategist.pilot.types import Intent, PilotQuery, RoutedQuery, ToolInputs
from f1_strategist.race_state import (
    CarSnapshot,
    FittedTyreSnapshot,
    PacketFreshness,
    RaceSessionSnapshot,
    RaceStateSnapshot,
    WeatherForecastSnapshot,
)
from f1_strategist.strategy.types import StrategySnapshot

NOW = 10.0


def _metric(
    key: str,
    value: float | int | str | bool | tuple[str, ...] | None,
    *,
    unit: str = "",
    quality: Quality = Quality.OK,
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("instant"),
        quality=quality,
        sample_count=1,
        computed_at_monotonic=NOW,
    )


def _state(
    *,
    lap: int | None = 20,
    total_laps: int | None = 57,
    session_type: str | None = "race",
    track: str | None = "test",
    weather: str | None = "clear",
    safety_car_status: str | None = "none",
    car: CarSnapshot | None = None,
    freshness: tuple[PacketFreshness, ...] = (),
    forecast: tuple[WeatherForecastSnapshot, ...] = (),
) -> RaceStateSnapshot:
    session = RaceSessionSnapshot(
        uid=1,
        session_type=session_type,
        track=track,
        total_laps=total_laps,
        started=True,
        ended=False,
        weather=weather,
        safety_car_status=safety_car_status,
        forecast=forecast,
    )
    cars = () if car is None else (car,)
    return RaceStateSnapshot(
        session=session,
        current_lap=lap,
        player_car_index=None if car is None else 0,
        cars=cars,
        freshness=freshness,
        events=(),
    )


def _car(
    *,
    telemetry_restricted: bool | None = None,
    fuel_kg: float | None = None,
    compound: str | None = "medium",
    age: int | None = 8,
    lifespan: int = 20,
) -> CarSnapshot:
    return CarSnapshot(
        car_index=0,
        position=3,
        current_lap=20,
        actual_tyre_compound=compound,
        tyre_age_laps=age,
        fuel_kg=fuel_kg,
        telemetry_restricted=telemetry_restricted,
        fitted_tyre=FittedTyreSnapshot(
            actual_tyre_compound=compound,
            visual_tyre_compound=compound,
            wear_pct=20.0,
            lifespan_laps=lifespan,
            usable_life_laps=lifespan,
        ),
    )


def _inputs(
    *,
    state: RaceStateSnapshot | None = None,
    metrics: MetricsSnapshot | None = None,
    config: PilotConfig | None = None,
) -> ToolInputs:
    return ToolInputs(
        state=state if state is not None else _state(),
        metrics=metrics if metrics is not None else MetricsSnapshot(),
        strategy=StrategySnapshot(),
        history=LapHistorySnapshot(),
        config=config if config is not None else PilotConfig(),
        now=NOW,
    )


def _query() -> RoutedQuery:
    return RoutedQuery(
        query=PilotQuery(query_id="q1", text="?", language="es", received_at=NOW),
        intent=Intent.GENERAL_RACE_STATE,
        confidence=0.9,
    )


def _facts(result) -> dict[str, object]:
    return {fact.name: fact.value for fact in result.facts}


def _fact(result, name):
    return next(fact for fact in result.facts if fact.name == name)


def test_tools_registered_for_q5_intents() -> None:
    assert TOOLS[Intent.GENERAL_RACE_STATE] is get_race_state
    assert TOOLS[Intent.FUEL_STATUS] is estimate_fuel_margin
    assert TOOLS[Intent.TYRE_LIFE] is estimate_tyre_life
    assert TOOLS[Intent.PACE_DIAGNOSIS] is get_recent_pace_trend
    assert TOOLS[Intent.DAMAGE_STATUS] is damage_status
    assert TOOLS[Intent.WEATHER_FORECAST] is weather_forecast


def test_get_race_state_complete() -> None:
    result = get_race_state(_query(), _inputs(state=_state(car=_car())))
    facts = _facts(result)
    assert result.quality is Quality.OK
    assert result.missing == ()
    assert facts["session_type"] == "race"
    assert facts["track"] == "test"
    assert facts["current_lap"] == 20
    assert facts["total_laps"] == 57
    assert facts["position"] == 3
    assert facts["tyre_compound"] == "medium"
    assert facts["tyre_age_laps"] == 8
    assert facts["weather"] == "clear"


def test_get_race_state_without_data() -> None:
    empty = _state(
        lap=None,
        total_laps=None,
        session_type=None,
        track=None,
        weather=None,
        safety_car_status=None,
    )
    result = get_race_state(_query(), _inputs(state=empty))
    assert result.quality is Quality.INSUFFICIENT
    assert result.facts == ()
    assert "position" in result.missing
    assert "current_lap" in result.missing


def test_get_race_state_stale() -> None:
    state = _state(
        car=_car(),
        freshness=(PacketFreshness("session", updated_at_monotonic=0.0, stale_after_seconds=1.0),),
    )
    result = get_race_state(_query(), _inputs(state=state))
    assert result.stale is True
    assert result.quality is Quality.STALE
    assert all(fact.quality is Quality.STALE for fact in result.facts)


def test_estimate_fuel_margin_complete() -> None:
    metrics = MetricsSnapshot(
        metrics={
            "fuel.in_tank_kg": _metric("fuel.in_tank_kg", 42.5, unit="kg"),
            "fuel.consumption_per_lap": _metric("fuel.consumption_per_lap", 2.1, unit="kg/lap"),
            "fuel.laps_of_fuel": _metric("fuel.laps_of_fuel", 20.2, unit="lap"),
            "fuel.margin_laps_at_finish": _metric("fuel.margin_laps_at_finish", 1.7, unit="lap"),
            "fuel.margin_kg_at_finish": _metric("fuel.margin_kg_at_finish", 3.6, unit="kg"),
        }
    )
    result = estimate_fuel_margin(_query(), _inputs(metrics=metrics))
    facts = _facts(result)
    assert result.quality is Quality.OK
    assert facts["fuel_in_tank_kg"] == 42.5
    assert _fact(result, "fuel_in_tank_kg").kind.value == "measured"
    assert _fact(result, "fuel_margin_laps").kind.value == "estimated"


def test_estimate_fuel_margin_without_data() -> None:
    result = estimate_fuel_margin(_query(), _inputs(state=_state(car=_car())))
    assert result.quality is Quality.INSUFFICIENT
    assert result.facts == ()
    assert "fuel_in_tank_kg" in result.missing


def test_estimate_fuel_margin_restricted_telemetry_is_absent() -> None:
    metrics = MetricsSnapshot(metrics={"fuel.in_tank_kg": _metric("fuel.in_tank_kg", 0.0)})
    result = estimate_fuel_margin(
        _query(),
        _inputs(state=_state(car=_car(telemetry_restricted=True, fuel_kg=0.0)), metrics=metrics),
    )
    assert "fuel_in_tank_kg" in result.missing
    assert "fuel_in_tank_kg" not in _facts(result)
    assert result.quality is Quality.INSUFFICIENT


@pytest.mark.parametrize(
    ("metric_key", "metric_value", "expected_source", "expected_kind", "expected_life"),
    [
        ("tyres.game_lifespan_laps", 12.0, "game", "measured", 12.0),
        ("tyres.laps_to_wear_limit", 9.0, "wear_limit", "estimated", 9.0),
    ],
)
def test_estimate_tyre_life_source_from_metric(
    metric_key: str,
    metric_value: float,
    expected_source: str,
    expected_kind: str,
    expected_life: float,
) -> None:
    metrics = MetricsSnapshot(metrics={metric_key: _metric(metric_key, metric_value, unit="lap")})
    result = estimate_tyre_life(_query(), _inputs(metrics=metrics))
    facts = _facts(result)
    assert facts["tyre_life_laps"] == expected_life
    assert facts["tyre_life_source"] == expected_source
    assert _fact(result, "tyre_life_laps").kind.value == expected_kind


def test_estimate_tyre_life_falls_back_to_model() -> None:
    metrics = MetricsSnapshot(
        metrics={
            "tyres.wear_mean_pct": _metric("tyres.wear_mean_pct", 40.0, unit="%"),
            "tyres.wear_rate_pct_per_lap": _metric(
                "tyres.wear_rate_pct_per_lap", 0.5, unit="%/lap"
            ),
        }
    )
    result = estimate_tyre_life(_query(), _inputs(metrics=metrics))
    facts = _facts(result)
    assert facts["tyre_life_source"] == "model"
    assert facts["tyre_life_laps"] == pytest.approx(120.0)
    assert _fact(result, "tyre_life_laps").kind.value == "estimated"


def test_estimate_tyre_life_without_data() -> None:
    result = estimate_tyre_life(_query(), _inputs())
    assert result.quality is Quality.INSUFFICIENT
    assert "tyre_life_laps" in result.missing
    assert "tyre_life_source" not in _facts(result)


@pytest.mark.parametrize(
    ("trend", "label"),
    [
        (0.05, "stable"),
        (-0.05, "stable"),
        (0.06, "worsening"),
        (-0.06, "improving"),
    ],
)
def test_get_recent_pace_trend_labels(trend: float, label: str) -> None:
    metrics = MetricsSnapshot(
        metrics={
            "pace.trend_s_per_lap": _metric("pace.trend_s_per_lap", trend, unit="s/lap"),
            "pace.last_lap_s": _metric("pace.last_lap_s", 91.2, unit="s"),
            "pace.best_lap_s": _metric("pace.best_lap_s", 90.8, unit="s"),
            "pace.clean_laps_used": _metric("pace.clean_laps_used", 4, unit="lap"),
        }
    )
    result = get_recent_pace_trend(_query(), _inputs(metrics=metrics))
    assert result.quality is Quality.OK
    assert _facts(result)["pace_trend_label"] == label


def test_get_recent_pace_trend_without_data() -> None:
    result = get_recent_pace_trend(_query(), _inputs())
    assert result.quality is Quality.INSUFFICIENT
    assert "pace_trend_s_per_lap" in result.missing


def test_damage_status_complete() -> None:
    metrics = MetricsSnapshot(
        metrics={
            "damage.aero_max_pct": _metric("damage.aero_max_pct", 12.0, unit="%"),
            "damage.floor_pct": _metric("damage.floor_pct", 4.0, unit="%"),
            "damage.engine_max_pct": _metric("damage.engine_max_pct", 8.0, unit="%"),
            "damage.gearbox_pct": _metric("damage.gearbox_pct", 2.0, unit="%"),
            "damage.faults": _metric("damage.faults", ("drs", "engine"), unit="faults"),
        }
    )
    result = damage_status(_query(), _inputs(metrics=metrics))
    facts = _facts(result)
    assert result.quality is Quality.OK
    assert facts["damage_aero_max_pct"] == 12.0
    assert facts["damage_fault_count"] == 2
    assert facts["damage_faults"] == "drs,engine"


def test_damage_status_without_data() -> None:
    result = damage_status(_query(), _inputs())
    assert result.quality is Quality.INSUFFICIENT
    assert result.facts == ()
    assert "damage_aero_max_pct" in result.missing


def test_empty_fault_list_is_a_measured_zero() -> None:
    metrics = MetricsSnapshot(
        metrics={"damage.faults": _metric("damage.faults", (), unit="faults")}
    )

    result = damage_status(_query(), _inputs(metrics=metrics))

    assert _facts(result)["damage_fault_count"] == 0
    assert "damage_faults" not in result.missing


def test_stale_metric_quality_is_preserved_when_converting_to_facts() -> None:
    metrics = MetricsSnapshot(
        metrics={
            "fuel.in_tank_kg": _metric("fuel.in_tank_kg", 42.5, unit="kg", quality=Quality.STALE),
            "tyres.game_lifespan_laps": _metric(
                "tyres.game_lifespan_laps", 12.0, unit="lap", quality=Quality.STALE
            ),
            "pace.trend_s_per_lap": _metric(
                "pace.trend_s_per_lap", 0.1, unit="s/lap", quality=Quality.STALE
            ),
        }
    )
    inputs = _inputs(metrics=metrics)

    fuel = estimate_fuel_margin(_query(), inputs)
    tyre = estimate_tyre_life(_query(), inputs)
    pace = get_recent_pace_trend(_query(), inputs)

    assert _fact(fuel, "fuel_in_tank_kg").quality is Quality.STALE
    assert _fact(tyre, "tyre_life_laps").quality is Quality.STALE
    assert _fact(pace, "pace_trend_s_per_lap").quality is Quality.STALE


def test_weather_forecast_complete() -> None:
    forecast = (
        WeatherForecastSnapshot(
            offset_minutes=10,
            weather="light_rain",
            track_temperature_c=30.0,
            air_temperature_c=22.0,
            rain_percentage=60,
        ),
        WeatherForecastSnapshot(
            offset_minutes=30,
            weather="clear",
            track_temperature_c=31.0,
            air_temperature_c=23.0,
            rain_percentage=10,
        ),
    )
    metrics = MetricsSnapshot(
        metrics={
            "env.track_temperature_c": _metric("env.track_temperature_c", 30.0, unit="C"),
            "env.air_temperature_c": _metric("env.air_temperature_c", 22.0, unit="C"),
            "env.rain_probability_next_10_min_pct": _metric(
                "env.rain_probability_next_10_min_pct", 60, unit="%"
            ),
            "env.rain_probability_next_30_min_pct": _metric(
                "env.rain_probability_next_30_min_pct", 20, unit="%"
            ),
            "env.track_temperature_trend_c_per_lap": _metric(
                "env.track_temperature_trend_c_per_lap", -0.1, unit="C/lap"
            ),
        }
    )
    result = weather_forecast(_query(), _inputs(state=_state(forecast=forecast), metrics=metrics))
    facts = _facts(result)
    assert result.quality is Quality.OK
    assert facts["weather"] == "clear"
    assert facts["track_temperature_c"] == 30.0
    assert facts["forecast_plus_10_min_weather"] == "light_rain"
    assert facts["forecast_plus_10_min_rain_pct"] == 60
    assert facts["forecast_plus_30_min_rain_pct"] == 10


def test_weather_forecast_without_data() -> None:
    result = weather_forecast(_query(), _inputs(state=_state(weather=None, forecast=())))
    assert result.quality is Quality.INSUFFICIENT
    assert "forecast" in result.missing
    assert "track_temperature_c" in result.missing
