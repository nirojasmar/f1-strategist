"""Modelo de ritmo y desgaste por compuesto (S2)."""

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
    FittedTyreSnapshot,
    RaceState,
    TyreSetSnapshot,
)
from f1_strategist.strategy.pace_model import (
    NOTE_INVENTORY_UNAVAILABLE,
    NOTE_SLOPE_FROM_REFERENCE,
    NOTE_SLOPE_SCALED,
    NOTE_WEAR_BLENDED,
    CompoundModel,
    build_compound_models,
    canonical_compound,
    project_lap_times,
    project_tyre_option_lap_times,
)
from f1_strategist.strategy.tyre_options import tyre_options
from f1_strategist.weekend_profile import (
    CompoundProfile,
    ProfileValue,
    WeekendProfile,
)


def _record(
    lap: int,
    *,
    compound: str = "soft",
    age_start: int | None = None,
    lap_time_s: float | None = None,
    wear_end: float | None = None,
    flags: frozenset[str] = frozenset(),
    pit_exit: bool = False,
    closed: bool = True,
) -> LapRecord:
    if age_start is None:
        age_start = lap - 1
    return LapRecord(
        car_index=0,
        lap_number=lap,
        lap_time_s=lap_time_s,
        lap_time_source="lap_data",
        compound=compound,
        tyre_age_start=age_start,
        tyre_age_end=None if age_start is None else age_start + 1,
        tyre_wear_end_pct=(
            () if wear_end is None else (wear_end - 3, wear_end - 1, wear_end + 1, wear_end + 3)
        ),
        pit_exit=pit_exit,
        flags=flags,
        closed=closed,
    )


def _profile_value(value: float | None, quality: Quality = Quality.OK) -> ProfileValue:
    return ProfileValue(value=value, samples=4 if value is not None else 0, quality=quality)


def _compound_profile(
    compound: str,
    *,
    pace: float | None = None,
    delta: float | None = None,
    wear: float | None = None,
    quality: Quality = Quality.OK,
    visual: tuple[str, ...] = (),
) -> CompoundProfile:
    return CompoundProfile(
        compound=compound,
        visual_compounds=visual,
        stints=2,
        representative_stints=2,
        laps=10,
        clean_laps=10,
        wear_rate_pct_per_lap=_profile_value(wear, quality),
        pace_mean_s=_profile_value(pace, quality),
        pace_delta_vs_reference_s=_profile_value(delta, quality),
        fuel_consumption_kg_per_lap=_profile_value(1.5, Quality.LOW),
        pit_loss_s=ProfileValue(value=None),
        track_temperature_c=_profile_value(30.0, Quality.LOW),
        fuel_range_kg=(40.0, 60.0),
        weather=("clear",),
        sessions=(1,),
        quality=quality,
    )


def _weekend_profile(
    compounds: tuple[CompoundProfile, ...],
    reference: str | None = None,
) -> WeekendProfile:
    return WeekendProfile(
        compounds=compounds,
        reference_compound=reference,
        sessions=(),
        quality=Quality.OK,
    )


def _inputs(
    *,
    records: tuple[LapRecord, ...] = (),
    stints: tuple[TyreStint, ...] = (),
    car: CarSnapshot | None = None,
    config: MetricsConfig | None = None,
    profile: WeekendProfile | None = None,
    spare: tuple[TyreSetSnapshot, ...] = (),
    now: float = 10.0,
) -> MetricInputs:
    snapshot = RaceState().snapshot()
    cars = list(snapshot.cars)
    if car is not None:
        cars[car.car_index] = car
    state = replace(snapshot, cars=tuple(cars), tyre_inventory=tuple(spare))
    return MetricInputs(
        state,
        LapHistorySnapshot(records={0: records}, tyre_stints={0: stints}),
        config or MetricsConfig(),
        now,
        0,
        profile,
    )


def _degrading_laps(
    *,
    compound: str = "soft",
    first: int = 1,
    count: int = 5,
    base: float = 90.0,
    slope: float = 0.10,
    wear_base: float = 0.0,
    wear_slope: float = 0.5,
) -> tuple[LapRecord, ...]:
    return tuple(
        _record(
            first + index,
            compound=compound,
            age_start=index,
            lap_time_s=base + slope * index,
            wear_end=wear_base + wear_slope * (index + 1),
        )
        for index in range(count)
    )


def test_fit_recovers_exact_slope_and_base() -> None:
    inputs = _inputs(records=_degrading_laps(count=6))
    models = build_compound_models(inputs, StrategyConfig())

    soft = models.get("soft")
    assert soft is not None
    assert soft.available
    assert soft.source == "race"
    assert soft.n_race_laps == 6
    assert soft.base_lap_s == pytest.approx(90.0)
    assert soft.slope_s_per_lap == pytest.approx(0.10)
    assert soft.wear_rate_pct_per_lap == pytest.approx(0.5)
    assert soft.quality is Quality.OK


def test_race_fit_uses_age_not_lap_number() -> None:
    # Vueltas 11-15 con edad 0-4: el ritmo debe ajustarse por edad, no por vuelta.
    records = tuple(
        _record(
            11 + index,
            age_start=index,
            lap_time_s=88.0 + 0.5 * index,
            wear_end=1.0 * (index + 1),
        )
        for index in range(5)
    )
    car = CarSnapshot(car_index=0, current_lap=15, actual_tyre_compound="soft", tyre_age_laps=4)
    soft = build_compound_models(_inputs(records=records, car=car), StrategyConfig()).get("soft")

    assert soft is not None
    assert soft.base_lap_s == pytest.approx(88.0)
    assert soft.slope_s_per_lap == pytest.approx(0.5)


def test_fewer_than_three_laps_produce_no_fit() -> None:
    inputs = _inputs(records=_degrading_laps(count=2))
    models = build_compound_models(inputs, StrategyConfig())

    assert models.models == ()
    assert models.notes == ("no_compounds",)


def test_blend_uses_the_race_aligned_relative_prior() -> None:
    records = _degrading_laps(count=4, base=90.0, slope=0.10)
    profile = _weekend_profile((_compound_profile("soft", pace=95.0, delta=0.0, wear=0.5),))

    weak = build_compound_models(
        _inputs(records=records, profile=profile),
        StrategyConfig(prior_strength_k=1.0),
    ).get("soft")
    strong = build_compound_models(
        _inputs(records=records, profile=profile),
        StrategyConfig(prior_strength_k=100.0),
    ).get("soft")

    assert weak is not None and strong is not None
    assert weak.source == strong.source == "blend"
    assert weak.base_lap_s == pytest.approx(90.0)
    assert strong.base_lap_s == pytest.approx(90.0)


def test_zero_race_laps_without_race_alignment_produce_no_model() -> None:
    profile = _weekend_profile((_compound_profile("soft", pace=91.25, delta=0.0, wear=0.42),))
    car = CarSnapshot(car_index=0, current_lap=1, actual_tyre_compound="soft", tyre_age_laps=0)

    models = build_compound_models(_inputs(car=car, profile=profile), StrategyConfig())
    soft = models.get("soft")

    assert soft is None


def test_unavailable_compound_without_any_source_is_not_modelled() -> None:
    car = CarSnapshot(car_index=0, current_lap=1, actual_tyre_compound="soft", tyre_age_laps=0)
    models = build_compound_models(_inputs(car=car), StrategyConfig())

    assert models.models == ()
    assert models.notes == ("no_compounds",)
    assert models.get("soft") is None


def test_untried_compound_uses_profile_pace_delta() -> None:
    records = _degrading_laps(count=5, base=90.0, slope=0.10)
    profile = _weekend_profile(
        (
            _compound_profile("soft", pace=90.0, delta=0.0, wear=0.5),
            _compound_profile("hard", pace=90.8, delta=0.8, wear=0.3),
        ),
        reference="soft",
    )
    inventory = (
        TyreSetSnapshot(
            index=1,
            actual_tyre_compound="hard",
            visual_tyre_compound="c1",
            wear_pct=0.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=30,
            lap_delta_time_ms=800,
            fitted=False,
        ),
        TyreSetSnapshot(
            index=3,
            actual_tyre_compound="hard",
            visual_tyre_compound="c1",
            wear_pct=0.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=20,
            lap_delta_time_ms=800,
            fitted=False,
        ),
    )

    models = build_compound_models(
        _inputs(records=records, profile=profile, spare=inventory),
        StrategyConfig(),
    )
    hard = models.get("hard")

    assert hard is not None
    assert hard.source == "profile"
    assert hard.base_lap_s == pytest.approx(90.8)
    assert hard.slope_s_per_lap == pytest.approx(0.06)  # 0.10 · 0.3/0.5
    assert NOTE_SLOPE_SCALED in hard.notes
    assert hard.visual_compound == "c1"


def test_untried_compound_falls_back_to_game_lap_delta() -> None:
    records = _degrading_laps(count=5, base=90.0, slope=0.10)
    inventory = (
        TyreSetSnapshot(
            index=1,
            actual_tyre_compound="hard",
            visual_tyre_compound="c1",
            wear_pct=0.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=30,
            lap_delta_time_ms=800,
            fitted=False,
        ),
    )

    models = build_compound_models(
        _inputs(
            records=records,
            spare=inventory,
            car=CarSnapshot(
                car_index=0,
                current_lap=5,
                actual_tyre_compound="soft",
                tyre_age_laps=4,
            ),
        ),
        StrategyConfig(),
    )
    hard = models.get("hard")

    assert hard is not None
    assert models.reference_compound == "soft"
    assert hard.source == "game_delta"
    assert hard.base_lap_s == pytest.approx(90.8)
    assert hard.slope_s_per_lap == pytest.approx(0.10)
    assert NOTE_SLOPE_FROM_REFERENCE in hard.notes


def test_unmounted_set_does_not_leak_its_age_into_the_compound_model() -> None:
    records = _degrading_laps(count=5, base=90.0, slope=0.10, wear_base=0.0, wear_slope=0.5)
    inventory = (
        TyreSetSnapshot(
            index=1,
            actual_tyre_compound="hard",
            visual_tyre_compound="c1",
            wear_pct=3.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=30,
            lap_delta_time_ms=300,
            fitted=False,
        ),
    )
    profile = _weekend_profile(
        (
            _compound_profile("soft", pace=90.0, delta=0.0, wear=0.5),
            _compound_profile("hard", pace=90.3, delta=0.3, wear=0.5),
        ),
        reference="soft",
    )

    hard = build_compound_models(
        _inputs(records=records, profile=profile, spare=inventory),
        StrategyConfig(),
    ).get("hard")

    assert hard is not None
    assert hard.initial_age_laps is None


def test_unmounted_set_life_is_taken_from_each_tyre_option() -> None:
    records = _degrading_laps(count=5, base=90.0, slope=0.10, wear_slope=0.5)
    inventory = (
        TyreSetSnapshot(
            index=1,
            actual_tyre_compound="hard",
            visual_tyre_compound="c1",
            wear_pct=0.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=30,
            lap_delta_time_ms=300,
            fitted=False,
        ),
    )
    profile = _weekend_profile(
        (
            _compound_profile("soft", pace=90.0, delta=0.0, wear=0.5),
            _compound_profile("hard", pace=90.3, delta=0.3, wear=0.5),
        ),
        reference="soft",
    )
    config = MetricsConfig(tyre_wear_limit_pct=60.0)

    hard = build_compound_models(
        _inputs(records=records, profile=profile, spare=inventory, config=config),
        StrategyConfig(),
    ).get("hard")

    assert hard is not None
    assert hard.usable_life_laps is None


def test_fitted_tyre_life_is_bounded_by_its_reported_lifespan() -> None:
    car = CarSnapshot(
        car_index=0,
        current_lap=5,
        actual_tyre_compound="soft",
        tyre_age_laps=4,
        fitted_tyre=FittedTyreSnapshot(
            actual_tyre_compound="soft",
            visual_tyre_compound="c5",
            wear_pct=50.0,
            lifespan_laps=8,
            usable_life_laps=12,
        ),
    )
    soft = build_compound_models(
        _inputs(records=_degrading_laps(count=5), car=car),
        StrategyConfig(),
    ).get("soft")

    assert soft is not None
    assert soft.usable_life_laps == pytest.approx(8.0)


def test_each_tyre_option_projects_with_its_own_age_and_remaining_life() -> None:
    inventory = (
        TyreSetSnapshot(
            index=1,
            actual_tyre_compound="hard",
            visual_tyre_compound="c1",
            wear_pct=0.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=5,
            lap_delta_time_ms=800,
            fitted=False,
        ),
        TyreSetSnapshot(
            index=2,
            actual_tyre_compound="hard",
            visual_tyre_compound="c1",
            wear_pct=3.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=20,
            lap_delta_time_ms=800,
            fitted=False,
        ),
        TyreSetSnapshot(
            index=3,
            actual_tyre_compound="hard",
            visual_tyre_compound="c1",
            wear_pct=0.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=20,
            lap_delta_time_ms=800,
            fitted=False,
        ),
    )
    records = _degrading_laps(count=5)
    profile = _weekend_profile(
        (
            _compound_profile("soft", pace=90.0, delta=0.0, wear=0.5),
            _compound_profile("hard", pace=90.8, delta=0.8, wear=0.5),
        ),
        reference="soft",
    )
    inputs = _inputs(records=records, profile=profile, spare=inventory)
    model = build_compound_models(
        inputs,
        StrategyConfig(cliff_penalty_s_per_lap=0.5),
    ).get("hard")
    options = tyre_options(inputs, StrategyConfig())

    assert model is not None
    fresh = next(option for option in options.options if option.set_index == 1)
    worn = next(option for option in options.options if option.set_index == 2)
    long_life = next(option for option in options.options if option.set_index == 3)
    fresh_projection = project_tyre_option_lap_times(model, fresh, 7)
    worn_projection = project_tyre_option_lap_times(model, worn, 7)
    long_life_projection = project_tyre_option_lap_times(model, long_life, 7)

    assert fresh_projection[0] == pytest.approx(model.base_lap_s)
    assert model.base_lap_s is not None
    assert worn_projection[0] == pytest.approx(model.base_lap_s + 0.6)
    assert fresh_projection[-1] > long_life_projection[-1]


def test_missing_inventory_is_reported_in_notes() -> None:
    car = CarSnapshot(car_index=0, current_lap=4, actual_tyre_compound="soft", tyre_age_laps=3)
    profile = _weekend_profile((_compound_profile("soft", pace=91.0, delta=0.0, wear=0.5),))

    soft = build_compound_models(
        _inputs(records=_degrading_laps(count=3), car=car, profile=profile),
        StrategyConfig(),
    ).get("soft")

    assert soft is not None
    assert NOTE_INVENTORY_UNAVAILABLE in soft.notes
    assert soft.base_lap_s is not None
    assert soft.base_lap_s == pytest.approx(90.0)


def test_project_lap_times_matches_the_analytic_example() -> None:
    model = CompoundModel(
        compound="soft",
        base_lap_s=88.0,
        slope_s_per_lap=0.10,
        wear_rate_pct_per_lap=0.5,
        n_race_laps=10,
        source="race",
        quality=Quality.OK,
    )

    times = project_lap_times(model, start_age=10.0, laps=3.0)

    assert times == pytest.approx((89.0, 89.1, 89.2))


def test_project_lap_times_applies_cliff_penalty_after_usable_life() -> None:
    model = CompoundModel(
        compound="soft",
        base_lap_s=90.0,
        slope_s_per_lap=0.1,
        wear_rate_pct_per_lap=0.5,
        n_race_laps=4,
        source="race",
        quality=Quality.OK,
        usable_life_laps=2.0,
        cliff_penalty_s_per_lap=0.5,
    )

    times = project_lap_times(model, start_age=1.0, laps=3.0)

    # Edades 1, 2 y 3: solo la última supera la vida útil en 1 vuelta.
    assert times == pytest.approx((90.1, 90.2, 90.3 + 0.5))


def test_project_lap_times_rejects_unavailable_models() -> None:
    model = CompoundModel(
        compound="soft",
        base_lap_s=None,
        slope_s_per_lap=None,
        wear_rate_pct_per_lap=None,
        n_race_laps=0,
        source="none",
        quality=Quality.UNAVAILABLE,
    )

    with pytest.raises(ValueError, match="no tiene base"):
        project_lap_times(model, 0.0, 1.0)


def test_project_lap_times_handles_fractional_laps() -> None:
    model = CompoundModel(
        compound="soft",
        base_lap_s=90.0,
        slope_s_per_lap=0.0,
        wear_rate_pct_per_lap=0.5,
        n_race_laps=3,
        source="race",
        quality=Quality.OK,
    )

    assert project_lap_times(model, 0.0, 2.4) == (90.0, 90.0, 90.0)


def test_dirty_laps_are_excluded_from_the_fit() -> None:
    clean = _degrading_laps(count=5, base=90.0, slope=0.1)
    dirty = (
        _record(6, age_start=5, lap_time_s=120.0, wear_end=3.0, flags=frozenset({"traffic"})),
        _record(7, age_start=6, lap_time_s=None, wear_end=3.5),
    )
    car = CarSnapshot(car_index=0, current_lap=7, actual_tyre_compound="soft", tyre_age_laps=6)

    soft = build_compound_models(
        _inputs(records=clean + dirty, car=car),
        StrategyConfig(),
    ).get("soft")

    assert soft is not None
    assert soft.n_race_laps == 5
    assert soft.base_lap_s == pytest.approx(90.0)
    assert soft.slope_s_per_lap == pytest.approx(0.10)


def test_laps_beyond_current_lap_are_ignored() -> None:
    records = _degrading_laps(count=6, base=90.0, slope=0.1)
    car = CarSnapshot(car_index=0, current_lap=3, actual_tyre_compound="soft", tyre_age_laps=2)

    soft = build_compound_models(
        _inputs(records=records, car=car),
        StrategyConfig(),
    ).get("soft")

    assert soft is not None
    assert soft.n_race_laps == 3


def test_stale_freshness_marks_the_model_stale() -> None:
    records = _degrading_laps(count=5)
    inputs = _inputs(records=records, now=1_000.0)
    freshness = tuple(
        replace(item, stale_after_seconds=1.0, updated_at_monotonic=0.0)
        if item.group == "lap_data"
        else item
        for item in inputs.state.freshness
    )
    stale_inputs = MetricInputs(
        replace(inputs.state, freshness=freshness),
        inputs.history,
        inputs.config,
        inputs.now,
        inputs.car_index,
        None,
    )

    soft = build_compound_models(stale_inputs, StrategyConfig()).get("soft")

    assert soft is not None
    assert soft.quality is Quality.STALE


def test_models_reject_duplicate_compounds() -> None:
    from f1_strategist.strategy.pace_model import CompoundModels

    model = CompoundModel(
        compound="soft",
        base_lap_s=90.0,
        slope_s_per_lap=0.1,
        wear_rate_pct_per_lap=0.5,
        n_race_laps=3,
        source="race",
        quality=Quality.OK,
    )

    with pytest.raises(ValueError, match="duplicados"):
        CompoundModels(models=(model, model))


def test_canonical_compound_normalizes_names() -> None:
    assert canonical_compound("  SOFT ") == "soft"
    assert canonical_compound("") is None
    assert canonical_compound(None) is None


def test_models_are_deterministic() -> None:
    records = _degrading_laps(count=5)
    profile = _weekend_profile((_compound_profile("soft", pace=90.4, delta=0.0, wear=0.5),))
    inputs = _inputs(records=records, profile=profile)

    first = build_compound_models(inputs, StrategyConfig())
    second = build_compound_models(inputs, StrategyConfig())

    assert first == second


def test_profile_prior_is_omitted_when_race_alignment_is_unavailable() -> None:
    car = CarSnapshot(car_index=0, current_lap=1, actual_tyre_compound="soft", tyre_age_laps=0)
    profile = _weekend_profile((_compound_profile("soft", pace=91.0, delta=0.0, wear=0.4),))
    soft = build_compound_models(
        _inputs(car=car, profile=profile),
        StrategyConfig(),
    ).get("soft")

    assert soft is None


def test_tyre_stints_are_not_required() -> None:
    records = _degrading_laps(count=5)
    car = CarSnapshot(car_index=0, current_lap=5, actual_tyre_compound="soft", tyre_age_laps=4)

    soft = build_compound_models(
        _inputs(records=records, stints=(), car=car),
        StrategyConfig(),
    ).get("soft")

    assert soft is not None
    assert soft.available
    assert soft.n_race_laps == 5


def test_untried_compound_is_anchored_to_the_race_base() -> None:
    # El ritmo absoluto de práctica (101 s) no debe sustituir al anclaje en
    # carrera (base 90 s) más el delta del compuesto (+1 s).
    records = _degrading_laps(count=3, base=90.0, slope=0.10)
    profile = _weekend_profile(
        (
            _compound_profile("soft", pace=90.0, delta=0.0, wear=0.5),
            _compound_profile("hard", pace=101.0, delta=1.0, wear=0.5),
        ),
        reference="soft",
    )
    inventory = (
        TyreSetSnapshot(
            index=1,
            actual_tyre_compound="hard",
            visual_tyre_compound="c1",
            wear_pct=0.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=30,
            lap_delta_time_ms=800,
            fitted=False,
        ),
    )

    hard = build_compound_models(
        _inputs(records=records, profile=profile, spare=inventory),
        StrategyConfig(),
    ).get("hard")

    assert hard is not None
    assert hard.source == "profile"
    assert hard.base_lap_s == pytest.approx(91.0)


def test_unaligned_absolute_profile_pace_is_not_blended_into_race_fit() -> None:
    records = _degrading_laps(count=5, base=90.0, slope=0.10)
    profile_entry = replace(
        _compound_profile("soft", pace=101.0, delta=0.0, wear=0.5),
        pace_delta_vs_reference_s=ProfileValue(value=None),
    )
    profile = _weekend_profile(
        (profile_entry,),
        reference="soft",
    )

    soft = build_compound_models(
        _inputs(records=records, profile=profile),
        StrategyConfig(),
    ).get("soft")

    assert soft is not None
    assert soft.source == "race"
    assert soft.base_lap_s == pytest.approx(90.0)


def test_fuel_effect_does_not_change_the_fitting_basis() -> None:
    records = tuple(
        replace(record, fuel_start_kg=100.0 - record.lap_number)
        for record in _degrading_laps(count=5, base=90.0, slope=0.10)
    )
    inputs = _inputs(
        records=records,
        config=MetricsConfig(fuel_effect_s_per_kg=0.03),
    )

    soft = build_compound_models(inputs, StrategyConfig()).get("soft")

    assert soft is not None
    assert soft.base_lap_s == pytest.approx(90.0)
    assert soft.slope_s_per_lap == pytest.approx(0.10)


def test_game_delta_requires_a_fit_for_the_mounted_compound() -> None:
    records = _degrading_laps(count=5)
    inventory = (
        TyreSetSnapshot(
            index=1,
            actual_tyre_compound="medium",
            visual_tyre_compound="c2",
            wear_pct=0.0,
            available=True,
            recommended_session="race",
            lifespan_laps=40,
            usable_life_laps=30,
            lap_delta_time_ms=800,
            fitted=False,
        ),
    )
    car = CarSnapshot(car_index=0, current_lap=5, actual_tyre_compound="hard")

    models = build_compound_models(
        _inputs(records=records, car=car, spare=inventory),
        StrategyConfig(),
    )

    assert models.reference_compound == "soft"
    assert models.get("medium") is None


def test_wear_rate_is_blended_with_the_profile_prior() -> None:
    # Carrera 2 %/vuelta con 5 muestras y prior 10 %/vuelta: (3·10 + 5·2)/8 = 5.
    records = _degrading_laps(count=5, base=90.0, slope=0.10, wear_slope=2.0)
    profile = _weekend_profile((_compound_profile("soft", pace=90.0, delta=0.0, wear=10.0),))

    soft = build_compound_models(
        _inputs(records=records, profile=profile),
        StrategyConfig(prior_strength_k=3.0),
    ).get("soft")

    assert soft is not None
    assert soft.wear_rate_pct_per_lap == pytest.approx(5.0)
    assert NOTE_WEAR_BLENDED in soft.notes


def test_prior_only_model_is_omitted_without_a_race_anchor() -> None:
    car = CarSnapshot(car_index=0, current_lap=1, actual_tyre_compound="soft", tyre_age_laps=0)
    profile = _weekend_profile((_compound_profile("soft", pace=91.25, delta=0.0, wear=0.42),))

    soft = build_compound_models(_inputs(car=car, profile=profile), StrategyConfig()).get("soft")

    assert soft is None
