"""Inventario y reglas de neumáticos (S4)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, StrategyConfig
from f1_strategist.metrics.history import (
    LapHistorySnapshot,
    LapRecord,
    MetricInputs,
    TyreStint,
)
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import (
    CarSnapshot,
    RaceState,
    TyreSetSnapshot,
    WeatherForecastSnapshot,
)
from f1_strategist.strategy.tyre_options import (
    NOTE_AVAILABILITY_UNKNOWN,
    NOTE_LIFE_RISK,
    NOTE_NO_INVENTORY,
    NOTE_NO_OPTIONS,
    NOTE_RULE_DISABLED,
    NOTE_RULE_PENDING,
    NOTE_RULE_SATISFIED,
    NOTE_WET,
    NOTE_WET_SETS_SKIPPED,
    TyreOption,
    TyreOptions,
    tyre_options,
)


def _tyre_set(
    index: int,
    compound: str,
    *,
    visual: str | None = None,
    wear: float = 0.0,
    available: bool | None = True,
    usable_life_laps: int = 30,
    lap_delta_time_ms: int = 300,
) -> TyreSetSnapshot:
    return TyreSetSnapshot(
        index=index,
        actual_tyre_compound=compound,
        visual_tyre_compound=visual,
        wear_pct=wear,
        available=available,
        recommended_session="race",
        lifespan_laps=40,
        usable_life_laps=usable_life_laps,
        lap_delta_time_ms=lap_delta_time_ms,
        fitted=False,
    )


def _car(index: int = 0, **fields: object) -> CarSnapshot:
    return CarSnapshot(car_index=index, **fields)  # type: ignore[arg-type]


def _inputs(
    *,
    inventory: tuple[TyreSetSnapshot, ...] = (),
    stints: tuple[TyreStint, ...] = (),
    records: tuple[LapRecord, ...] = (),
    car: CarSnapshot | None = None,
    config: MetricsConfig | None = None,
    session_fields: dict[str, object] | None = None,
) -> MetricInputs:
    state = RaceState().snapshot()
    cars = list(state.cars)
    if car is not None:
        cars[car.car_index] = car
    session = replace(state.session, **(session_fields or {}))
    state = replace(state, cars=tuple(cars), session=session, tyre_inventory=inventory)
    history = LapHistorySnapshot(
        records={0: records} if records else {},
        tyre_stints={0: stints},
    )
    return MetricInputs(state, history, config or MetricsConfig(), 10.0, 0)


def _forecast(
    offset_minutes: int,
    rain_percentage: int,
    *,
    session_type: str | None = "race",
) -> WeatherForecastSnapshot:
    return WeatherForecastSnapshot(
        offset_minutes=offset_minutes,
        weather="light_rain" if rain_percentage >= 50 else "clear",
        track_temperature_c=30.0,
        air_temperature_c=22.0,
        rain_percentage=rain_percentage,
        session_type=session_type,
    )


def test_available_sets_become_options_with_real_and_visual_compound() -> None:
    inventory = (
        _tyre_set(2, "hard", visual="c1"),
        _tyre_set(1, "medium", visual="c2", wear=10.0),
    )
    car = _car(current_lap=10, actual_tyre_compound="soft")
    options = tyre_options(_inputs(inventory=inventory, car=car), StrategyConfig())

    assert options.compounds == ("hard", "medium")
    hard = options.get("hard")
    assert hard is not None
    assert hard.visual_compound == "c1"
    assert hard.set_index == 2
    assert hard.initial_wear_pct == 0.0
    assert not hard.life_risk
    assert options.inventory_available
    assert options.quality is Quality.OK


def test_sets_without_confirmed_availability_are_excluded() -> None:
    inventory = (
        _tyre_set(1, "soft", available=False),
        _tyre_set(2, "hard", available=None),
    )
    options = tyre_options(
        _inputs(inventory=inventory, car=_car(current_lap=5, actual_tyre_compound="medium")),
        StrategyConfig(),
    )

    assert options.compounds == ()
    assert NOTE_AVAILABILITY_UNKNOWN in options.notes
    assert options.quality is Quality.INSUFFICIENT


def test_only_confirmed_availability_is_offered() -> None:
    inventory = (
        _tyre_set(1, "soft", available=True),
        _tyre_set(2, "hard", available=None),
        _tyre_set(3, "medium", available=False),
    )
    options = tyre_options(
        _inputs(inventory=inventory, car=_car(current_lap=5, actual_tyre_compound="hard")),
        StrategyConfig(),
    )

    assert options.compounds == ("soft",)
    assert NOTE_AVAILABILITY_UNKNOWN in options.notes


def test_rule_pending_makes_the_stop_mandatory_and_excludes_the_current_compound() -> None:
    inventory = (
        _tyre_set(1, "medium"),
        _tyre_set(2, "hard"),
    )
    car = _car(current_lap=10, actual_tyre_compound="medium")
    stints = (TyreStint(compound="medium", start_lap=1),)

    options = tyre_options(
        _inputs(inventory=inventory, stints=stints, car=car),
        StrategyConfig(),
    )

    assert options.used_dry_compounds == ("medium",)
    assert options.requires_stop
    assert not options.stay_out_allowed
    assert NOTE_RULE_PENDING in options.notes
    assert options.compounds == ("hard",)


def test_rule_satisfied_allows_staying_out() -> None:
    inventory = (_tyre_set(1, "hard"),)
    car = _car(current_lap=20, actual_tyre_compound="hard")
    stints = (
        TyreStint(compound="soft", start_lap=1, end_lap=10),
        TyreStint(compound="hard", start_lap=11),
    )

    options = tyre_options(
        _inputs(inventory=inventory, stints=stints, car=car),
        StrategyConfig(),
    )

    assert options.used_dry_compounds == ("hard", "soft")
    assert not options.requires_stop
    assert options.stay_out_allowed
    assert NOTE_RULE_SATISFIED in options.notes
    assert options.compounds == ("hard",)


def test_rule_can_be_disabled_by_configuration() -> None:
    inventory = (_tyre_set(1, "medium"),)
    car = _car(current_lap=5, actual_tyre_compound="medium")

    options = tyre_options(
        _inputs(inventory=inventory, car=car),
        StrategyConfig(require_two_dry_compounds=False),
    )

    assert not options.requires_stop
    assert options.stay_out_allowed
    assert NOTE_RULE_DISABLED in options.notes
    assert options.compounds == ("medium",)


def test_missing_inventory_returns_no_options_with_a_note() -> None:
    car = _car(current_lap=5, actual_tyre_compound="soft")

    options = tyre_options(_inputs(car=car), StrategyConfig())

    assert options.options == ()
    assert not options.inventory_available
    assert options.quality is Quality.UNAVAILABLE
    assert NOTE_NO_INVENTORY in options.notes
    assert NOTE_NO_OPTIONS in options.notes


def test_wet_weather_sets_the_flag() -> None:
    inventory = (_tyre_set(1, "hard"),)
    car = _car(current_lap=5, actual_tyre_compound="medium")

    options = tyre_options(
        _inputs(inventory=inventory, car=car, session_fields={"weather": "heavy_rain"}),
        StrategyConfig(),
    )

    assert options.wet_conditions
    assert NOTE_WET in options.notes


def test_forecast_rain_within_the_window_sets_the_flag() -> None:
    inventory = (_tyre_set(1, "hard"),)
    car = _car(current_lap=5, actual_tyre_compound="medium")
    session = {"weather": "clear", "forecast": (_forecast(20, 70),)}

    options = tyre_options(
        _inputs(inventory=inventory, car=car, session_fields=session),
        StrategyConfig(),
    )

    assert options.wet_conditions
    assert NOTE_WET in options.notes


def test_forecast_rain_outside_the_window_is_ignored() -> None:
    inventory = (_tyre_set(1, "hard"),)
    car = _car(current_lap=5, actual_tyre_compound="medium")
    session = {"weather": "clear", "forecast": (_forecast(90, 90),)}

    options = tyre_options(
        _inputs(inventory=inventory, car=car, session_fields=session),
        StrategyConfig(),
    )

    assert not options.wet_conditions


def test_forecast_for_another_session_type_is_ignored() -> None:
    inventory = (_tyre_set(1, "hard"),)
    car = _car(current_lap=5, actual_tyre_compound="medium")
    session = {
        "weather": "clear",
        "session_type": "race",
        "forecast": (_forecast(10, 90, session_type="qualifying"),),
    }

    options = tyre_options(
        _inputs(inventory=inventory, car=car, session_fields=session),
        StrategyConfig(),
    )

    assert not options.wet_conditions


def test_wet_compounds_are_skipped_and_reported() -> None:
    inventory = (
        _tyre_set(1, "intermediate"),
        _tyre_set(2, "hard"),
    )
    car = _car(current_lap=5, actual_tyre_compound="medium")

    options = tyre_options(_inputs(inventory=inventory, car=car), StrategyConfig())

    assert options.compounds == ("hard",)
    assert NOTE_WET_SETS_SKIPPED in options.notes


def test_life_risk_is_flagged_when_the_set_cannot_reach_the_end() -> None:
    inventory = (_tyre_set(1, "hard", usable_life_laps=5),)
    car = _car(current_lap=10, actual_tyre_compound="medium")
    session: dict[str, object] = {"total_laps": 30}

    options = tyre_options(
        _inputs(inventory=inventory, car=car, session_fields=session),
        StrategyConfig(),
    )

    hard = options.get("hard")
    assert hard is not None
    assert hard.life_risk
    assert NOTE_LIFE_RISK in hard.notes
    assert options.quality is Quality.LOW


def test_useful_life_is_scaled_by_existing_wear() -> None:
    inventory = (_tyre_set(1, "hard", wear=50.0, usable_life_laps=20),)
    car = _car(current_lap=5, actual_tyre_compound="medium")

    hard = tyre_options(_inputs(inventory=inventory, car=car), StrategyConfig()).get("hard")

    assert hard is not None
    assert hard.initial_wear_pct == 50.0
    assert hard.useful_life_laps == pytest.approx(10.0)


def test_unknown_life_is_reported_without_risk() -> None:
    inventory = (_tyre_set(1, "hard", usable_life_laps=0),)
    car = _car(current_lap=5, actual_tyre_compound="medium")
    session: dict[str, object] = {"total_laps": 50}

    hard = tyre_options(
        _inputs(inventory=inventory, car=car, session_fields=session),
        StrategyConfig(),
    ).get("hard")

    assert hard is not None
    assert hard.useful_life_laps is None
    assert not hard.life_risk


def test_no_valid_option_reports_insufficient_quality() -> None:
    inventory = (_tyre_set(1, "medium"),)
    car = _car(current_lap=5, actual_tyre_compound="medium")

    options = tyre_options(_inputs(inventory=inventory, car=car), StrategyConfig())

    assert options.requires_stop
    assert options.options == ()
    assert options.quality is Quality.INSUFFICIENT
    assert NOTE_NO_OPTIONS in options.notes


def test_options_are_sorted_and_deduplicated_by_set() -> None:
    inventory = (
        _tyre_set(3, "soft"),
        _tyre_set(1, "hard"),
        _tyre_set(2, "hard"),
    )
    car = _car(current_lap=5, actual_tyre_compound="medium")

    options = tyre_options(_inputs(inventory=inventory, car=car), StrategyConfig())

    assert [(option.compound, option.set_index) for option in options] == [
        ("hard", 1),
        ("hard", 2),
        ("soft", 3),
    ]


def test_to_dict_round_trips_the_essentials() -> None:
    inventory = (_tyre_set(1, "hard", visual="c1"),)
    car = _car(current_lap=5, actual_tyre_compound="medium")
    options = tyre_options(_inputs(inventory=inventory, car=car), StrategyConfig())

    payload = options.to_dict()

    assert payload["current_compound"] == "medium"
    assert payload["quality"] == Quality.OK.value
    assert payload["inventory_available"] is True
    first = payload["options"]
    assert isinstance(first, list)
    assert first[0]["compound"] == "hard"
    assert first[0]["visual_compound"] == "c1"


def test_options_are_deterministic() -> None:
    inventory = (
        _tyre_set(2, "hard"),
        _tyre_set(1, "soft"),
    )
    car = _car(current_lap=8, actual_tyre_compound="medium")
    inputs = _inputs(inventory=inventory, car=car)

    assert tyre_options(inputs, StrategyConfig()) == tyre_options(inputs, StrategyConfig())


def test_stay_out_cannot_be_allowed_while_the_stop_is_mandatory() -> None:
    with pytest.raises(ValueError, match="STAY_OUT"):
        TyreOptions(requires_stop=True, stay_out_allowed=True)


def test_tyre_option_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="no puede estar vacío"):
        TyreOption(compound="")
    with pytest.raises(ValueError, match="índice"):
        TyreOption(compound="soft", set_index=-1)
    with pytest.raises(ValueError, match="100"):
        TyreOption(compound="soft", initial_wear_pct=120.0)
    with pytest.raises(ValueError, match="useful_life_laps"):
        TyreOption(compound="soft", useful_life_laps=-1.0)


def _closed_lap(lap: int, lap_time_s: float = 100.0) -> LapRecord:
    return LapRecord(
        car_index=0,
        lap_number=lap,
        lap_time_s=lap_time_s,
        lap_time_source="lap_data",
        closed=True,
    )


def test_forecast_horizon_follows_the_stop_window() -> None:
    # 30 vueltas de 100 s son 50 minutos: el aviso a los 31 minutos está dentro.
    records = tuple(_closed_lap(lap) for lap in range(1, 4))
    options = tyre_options(
        _inputs(
            records=records,
            car=_car(current_lap=1, actual_tyre_compound="medium"),
            session_fields={"forecast": (_forecast(31, 80),), "total_laps": 60},
        ),
        StrategyConfig(max_pit_in_laps=30),
    )

    assert options.wet_conditions
    assert NOTE_WET in options.notes


def test_forecast_outside_the_stop_window_is_ignored() -> None:
    records = tuple(_closed_lap(lap) for lap in range(1, 4))
    options = tyre_options(
        _inputs(
            records=records,
            car=_car(current_lap=1, actual_tyre_compound="medium"),
            session_fields={"forecast": (_forecast(60, 80),), "total_laps": 60},
        ),
        StrategyConfig(max_pit_in_laps=30),
    )

    assert not options.wet_conditions
