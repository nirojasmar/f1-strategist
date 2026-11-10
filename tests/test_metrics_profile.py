from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.engine import MetricsEngine
from f1_strategist.metrics.history import LapHistorySnapshot, MetricInputs
from f1_strategist.metrics.profile import PRIOR_NOTE, ProfileMetrics
from f1_strategist.metrics.types import Metric, Quality
from f1_strategist.race_state import CarSnapshot, RaceState
from f1_strategist.store import TyreInventorySnapshot
from f1_strategist.weekend_profile import (
    CompoundProfile,
    ProfileValue,
    WeekendProfile,
)


def _profile_value(value: float, *, samples: int = 5) -> ProfileValue:
    return ProfileValue(value, samples=samples, quality=Quality.OK)


def _compound(
    compound: str = "c3",
    *,
    visual: tuple[str, ...] = ("soft",),
) -> CompoundProfile:
    return CompoundProfile(
        compound=compound,
        visual_compounds=visual,
        stints=2,
        representative_stints=2,
        laps=20,
        clean_laps=16,
        wear_rate_pct_per_lap=_profile_value(0.12),
        pace_mean_s=_profile_value(90.5),
        pace_delta_vs_reference_s=_profile_value(0.0) if compound == "c3" else _profile_value(0.35),
        fuel_consumption_kg_per_lap=_profile_value(1.9),
        pit_loss_s=_profile_value(21.5),
        track_temperature_c=_profile_value(31.0),
        fuel_range_kg=(38.0, 42.0),
        weather=("clear",),
        sessions=(1, 2),
        quality=Quality.OK,
    )


def _inventory_set(
    set_index: int,
    *,
    available: bool | None = True,
    compound: str = "c3",
) -> TyreInventorySnapshot:
    return TyreInventorySnapshot(
        kind="race_start",
        set_index=set_index,
        actual_tyre_compound=compound,
        visual_tyre_compound="soft",
        wear_pct=0.0,
        available=available,
        lifespan_laps=18,
        usable_life_laps=18,
        lap_delta_time_ms=0,
        recommended_session="qualifying_1",
        fitted=False,
    )


def _profile(
    *,
    reference: str | None = "c3",
    inventory: tuple[TyreInventorySnapshot, ...] = (),
    sessions: tuple[int, ...] = (1, 2),
    quality: Quality = Quality.OK,
) -> WeekendProfile:
    # El perfil de referencia agrupa dos compuestos para poder probar el
    # respaldo cuando el montado no aparece en el fin de semana.
    return WeekendProfile(
        compounds=(_compound("c3"), _compound("c4", visual=("medium",))),
        reference_compound=reference,
        pit_loss_s=_profile_value(21.5, samples=2),
        inventory=inventory,
        sessions=sessions,
        quality=quality,
    )


def _car(*, actual: str | None = "c3", visual: str | None = "soft") -> CarSnapshot:
    return CarSnapshot(
        car_index=0,
        actual_tyre_compound=actual,
        visual_tyre_compound=visual,
    )


def _inputs(
    *,
    profile: WeekendProfile | None = None,
    car: CarSnapshot | None = None,
) -> MetricInputs:
    state = RaceState().snapshot()
    cars = list(state.cars)
    if car is not None:
        cars[car.car_index] = car
    state = replace(state, cars=tuple(cars))
    return MetricInputs(
        state,
        LapHistorySnapshot(records={}),
        MetricsConfig(),
        10.0,
        0,
        profile=profile,
    )


def _metrics(inputs: MetricInputs) -> dict[str, Metric]:
    return {metric.key: metric for metric in ProfileMetrics().compute(inputs)}


def test_without_a_profile_every_metric_is_unavailable() -> None:
    metrics = _metrics(_inputs())

    assert tuple(metrics) == (
        "profile.wear_rate_pct_per_lap",
        "profile.pace_mean_s",
        "profile.pace_delta_vs_reference_s",
        "profile.fuel_consumption_kg_per_lap",
        "profile.pit_loss_s",
        "profile.sets_available",
        "profile.confidence",
    )
    for metric in metrics.values():
        assert metric.quality is Quality.UNAVAILABLE
        assert metric.value is None
        assert metric.notes == ("profile_unavailable",)
        assert metric.sources == ("profile",)


def test_the_profile_metrics_are_a_low_quality_prior() -> None:
    metrics = _metrics(_inputs(profile=_profile(), car=_car()))

    wear = metrics["profile.wear_rate_pct_per_lap"]
    assert wear.value == pytest.approx(0.12)
    assert wear.unit == "%/lap"
    assert wear.quality is Quality.LOW
    assert wear.notes[0] == PRIOR_NOTE
    assert "compound:c3" in wear.notes

    pace = metrics["profile.pace_delta_vs_reference_s"]
    assert pace.value == pytest.approx(0.0)
    assert pace.quality is Quality.LOW

    assert metrics["profile.pit_loss_s"].value == pytest.approx(21.5)
    for metric in metrics.values():
        assert metric.quality is not Quality.OK
        assert metric.quality is not Quality.STALE


def test_the_mounted_compound_is_used_for_the_compound_metrics() -> None:
    metrics = _metrics(_inputs(profile=_profile(), car=_car(actual="c4", visual="medium")))

    delta = metrics["profile.pace_delta_vs_reference_s"]
    assert delta.value == pytest.approx(0.35)
    assert "compound:c4" in delta.notes


def test_a_mounted_compound_missing_from_the_profile_falls_back_to_the_reference() -> None:
    metrics = _metrics(_inputs(profile=_profile(), car=_car(actual="c5", visual="hard")))

    wear = metrics["profile.wear_rate_pct_per_lap"]
    assert wear.value == pytest.approx(0.12)
    assert "compound:c3" in wear.notes


def test_without_a_reference_the_compound_metrics_are_unavailable() -> None:
    metrics = _metrics(
        _inputs(
            profile=_profile(reference=None),
            car=_car(actual="c5", visual="hard"),
        )
    )

    wear = metrics["profile.wear_rate_pct_per_lap"]
    assert wear.quality is Quality.UNAVAILABLE
    assert wear.value is None
    assert wear.notes == ("profile_compound_unavailable",)


def test_an_empty_inventory_reports_no_set_count() -> None:
    metrics = _metrics(_inputs(profile=_profile(), car=_car()))

    sets = metrics["profile.sets_available"]
    assert sets.quality is Quality.UNAVAILABLE
    assert sets.value is None
    assert sets.notes == ("profile_inventory_empty",)


def test_the_inventory_counts_only_the_available_sets() -> None:
    inventory = (
        _inventory_set(0, available=True),
        _inventory_set(1, available=False),
        _inventory_set(2, available=None),
    )
    metrics = _metrics(_inputs(profile=_profile(inventory=inventory), car=_car()))

    sets = metrics["profile.sets_available"]
    assert sets.value == 1
    assert sets.sample_count == 2
    assert sets.quality is Quality.LOW
    assert sets.notes[0] == PRIOR_NOTE


def test_an_inventory_without_availability_is_unavailable() -> None:
    inventory = (_inventory_set(0, available=None),)
    metrics = _metrics(_inputs(profile=_profile(inventory=inventory), car=_car()))

    sets = metrics["profile.sets_available"]
    assert sets.quality is Quality.UNAVAILABLE
    assert sets.value is None
    assert sets.notes == ("profile_inventory_unknown",)


def test_confidence_stays_below_certainty_and_grows_with_sessions() -> None:
    few = _metrics(_inputs(profile=_profile(sessions=(1,)), car=_car()))
    many = _metrics(_inputs(profile=_profile(sessions=(1, 2, 3, 4)), car=_car()))

    low = few["profile.confidence"]
    high = many["profile.confidence"]
    assert isinstance(low.value, float)
    assert isinstance(high.value, float)
    assert 0.0 < low.value < high.value <= 0.9
    assert low.quality is Quality.LOW
    assert high.quality is Quality.LOW
    assert high.notes[0] == PRIOR_NOTE
    assert "quality:ok" in high.notes


def test_a_profile_without_data_reports_no_confidence() -> None:
    metrics = _metrics(_inputs(profile=WeekendProfile(), car=_car(actual=None, visual=None)))

    confidence = metrics["profile.confidence"]
    assert confidence.quality is Quality.UNAVAILABLE
    assert confidence.value is None
    assert confidence.notes == ("profile_confidence_unavailable",)


def test_the_engine_publishes_the_profile_metrics() -> None:
    engine = MetricsEngine(MetricsConfig())
    engine.set_profile(_profile(inventory=(_inventory_set(0),)))
    state = RaceState().snapshot()

    engine.finalize(state, 5.0)

    snapshot = engine.snapshot()
    assert snapshot["profile.wear_rate_pct_per_lap"].quality is Quality.LOW
    assert snapshot["profile.sets_available"].value == 1
    assert snapshot["profile.confidence"].value is not None
    assert snapshot["profile.confidence"].notes[0] == PRIOR_NOTE


def test_the_engine_keeps_publishing_without_a_profile() -> None:
    engine = MetricsEngine(MetricsConfig())
    state = RaceState().snapshot()

    engine.finalize(state, 5.0)

    snapshot = engine.snapshot()
    assert snapshot["profile.wear_rate_pct_per_lap"].quality is Quality.UNAVAILABLE
    assert engine.profile is None


def test_setting_a_profile_replaces_the_previous_one() -> None:
    engine = MetricsEngine(MetricsConfig())
    first = _profile(reference="c3")
    second = _profile(reference="c4")

    engine.set_profile(first)
    assert engine.profile is first

    engine.set_profile(second)
    assert engine.profile is second

    engine.set_profile(None)
    assert engine.profile is None
