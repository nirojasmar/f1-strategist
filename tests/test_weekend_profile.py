"""Perfil del fin de semana: agregación, mezcla con prior y hash estable."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.stint_summary import StintSummary
from f1_strategist.metrics.types import Quality
from f1_strategist.store import TyreInventorySnapshot
from f1_strategist.weekend_profile import (
    DEFAULT_PRIOR_STRENGTH,
    PROFILE_SCHEMA_VERSION,
    WeekendProfile,
    WeekendSession,
    blend_with_prior,
    build_weekend_profile,
    profile_from_json,
    profile_hash,
    profile_to_dict,
    profile_to_json,
)


def _summary(
    *,
    compound: str | None = "c3",
    visual_compound: str | None = "soft",
    stint_index: int = 0,
    start_lap: int = 1,
    end_lap: int | None = None,
    laps: int = 10,
    clean_laps: int = 6,
    pace_mean_s: float | None = 90.0,
    pace_mean_fuel_corrected_s: float | None = None,
    wear_rate_pct_per_lap: float | None = 0.10,
    fuel_start_kg: float | None = 40.0,
    fuel_end_kg: float | None = 38.0,
    track_temperature_mean_c: float | None = 30.0,
    weather: tuple[str, ...] = ("clear",),
    representative: bool = True,
    quality: Quality = Quality.OK,
) -> StintSummary:
    return StintSummary(
        car_index=0,
        stint_index=stint_index,
        start_lap=start_lap,
        end_lap=end_lap,
        laps=laps,
        compound=compound,
        visual_compound=visual_compound,
        clean_laps=clean_laps,
        tyre_age_start=0,
        tyre_age_end=laps,
        fuel_start_kg=fuel_start_kg,
        fuel_end_kg=fuel_end_kg,
        pace_mean_s=pace_mean_s,
        pace_mean_fuel_corrected_s=pace_mean_fuel_corrected_s,
        pace_trend_s_per_lap=None,
        pace_trend_fuel_corrected_s_per_lap=None,
        wear_start_pct=0.0,
        wear_end_pct=1.0,
        wear_rate_pct_per_lap=wear_rate_pct_per_lap,
        track_temperature_mean_c=track_temperature_mean_c,
        weather=weather,
        representative=representative,
        quality=quality,
    )


def _inventory_set(
    *,
    kind: str,
    set_index: int,
    compound: str = "c3",
    fitted: bool = False,
) -> TyreInventorySnapshot:
    return TyreInventorySnapshot(
        kind=kind,  # type: ignore[arg-type]
        set_index=set_index,
        actual_tyre_compound=compound,
        visual_tyre_compound="soft",
        wear_pct=0.0,
        available=True,
        lifespan_laps=18,
        usable_life_laps=18,
        lap_delta_time_ms=0,
        recommended_session="qualifying_1",
        fitted=fitted,
    )


def _weekend() -> tuple[WeekendSession, ...]:
    return (
        WeekendSession(
            session_id=1,
            session_type="practice_1",
            stint_summaries=(
                _summary(clean_laps=2, wear_rate_pct_per_lap=0.10, pace_mean_s=90.0),
                _summary(
                    stint_index=1,
                    start_lap=11,
                    compound="c4",
                    visual_compound="medium",
                    clean_laps=2,
                    wear_rate_pct_per_lap=0.20,
                    pace_mean_s=89.5,
                ),
            ),
            pit_losses_s=(20.0, 22.0),
        ),
        WeekendSession(
            session_id=2,
            session_type="qualifying_1",
            stint_summaries=(
                _summary(
                    clean_laps=2,
                    wear_rate_pct_per_lap=0.30,
                    pace_mean_s=91.0,
                    track_temperature_mean_c=34.0,
                    weather=("cloudy",),
                    fuel_start_kg=39.0,
                    fuel_end_kg=37.0,
                ),
            ),
            pit_losses_s=(21.0,),
        ),
    )


def test_weighted_median_aggregation_with_known_values() -> None:
    profile = build_weekend_profile(_weekend(), MetricsConfig())

    assert profile.quality is Quality.OK
    assert profile.sessions == (1, 2)
    assert profile.reference_compound == "c4"

    soft = profile.compound("c3")
    assert soft is not None
    assert soft.stints == 2
    assert soft.representative_stints == 2
    assert soft.laps == 20
    assert soft.clean_laps == 4
    assert soft.wear_rate_pct_per_lap.value == pytest.approx(0.20)
    assert soft.pace_mean_s.value == pytest.approx(90.5)
    assert soft.fuel_consumption_kg_per_lap.value == pytest.approx(0.2)
    assert soft.track_temperature_c.value == pytest.approx(32.0)
    assert soft.fuel_range_kg == (37.0, 40.0)
    assert soft.weather == ("clear", "cloudy")
    assert soft.pace_delta_vs_reference_s.value == pytest.approx(1.0)
    assert soft.pace_delta_vs_reference_s.samples == 2
    assert soft.sessions == (1, 2)
    assert soft.quality is Quality.OK

    medium = profile.compound("c4")
    assert medium is not None
    assert medium.pace_mean_s.value == pytest.approx(89.5)
    assert medium.pace_delta_vs_reference_s.value == 0.0
    assert medium.sessions == (1,)

    assert profile.pit_loss_s.value == pytest.approx(21.0)
    assert profile.pit_loss_s.samples == 3
    assert profile.pit_loss_s.quality is Quality.OK
    assert profile.pit_loss_s.sessions == (1, 2)


@pytest.mark.parametrize(
    ("fuel_start", "fuel_end", "expected"),
    [
        (40.0, 38.0, (38.0, 40.0)),
        (None, 38.0, (38.0, 38.0)),
        (40.0, None, (40.0, 40.0)),
    ],
)
def test_fuel_range_uses_all_available_stint_endpoints(
    fuel_start: float | None,
    fuel_end: float | None,
    expected: tuple[float, float],
) -> None:
    profile = build_weekend_profile(
        (
            WeekendSession(
                session_id=1,
                stint_summaries=(_summary(fuel_start_kg=fuel_start, fuel_end_kg=fuel_end),),
            ),
        ),
        MetricsConfig(),
    )

    compound = profile.compound("c3")
    assert compound is not None
    assert compound.fuel_range_kg == expected


def test_non_representative_stints_are_counted_but_not_used_for_values() -> None:
    sessions = (
        WeekendSession(
            session_id=1,
            stint_summaries=(
                _summary(clean_laps=2, wear_rate_pct_per_lap=0.10, pace_mean_s=90.0),
                _summary(
                    stint_index=1,
                    laps=4,
                    clean_laps=0,
                    wear_rate_pct_per_lap=9.0,
                    pace_mean_s=200.0,
                    representative=False,
                    quality=Quality.INSUFFICIENT,
                ),
            ),
        ),
        WeekendSession(
            session_id=2,
            stint_summaries=(_summary(clean_laps=2, wear_rate_pct_per_lap=0.30, pace_mean_s=91.0),),
        ),
    )

    profile = build_weekend_profile(sessions, MetricsConfig())
    soft = profile.compound("c3")

    assert soft is not None
    assert soft.stints == 3
    assert soft.representative_stints == 2
    assert soft.laps == 24
    assert soft.clean_laps == 4
    assert soft.wear_rate_pct_per_lap.value == pytest.approx(0.20)
    assert soft.pace_mean_s.value == pytest.approx(90.5)


def test_partial_profile_without_representative_stints_never_invents_values() -> None:
    sessions = (
        WeekendSession(
            session_id=1,
            stint_summaries=(
                _summary(
                    laps=3,
                    clean_laps=1,
                    representative=False,
                    quality=Quality.INSUFFICIENT,
                ),
            ),
        ),
    )

    profile = build_weekend_profile(sessions, MetricsConfig())
    soft = profile.compound("c3")

    assert profile.quality is Quality.INSUFFICIENT
    assert soft is not None
    assert soft.representative_stints == 0
    assert soft.quality is Quality.INSUFFICIENT
    assert soft.wear_rate_pct_per_lap.value == pytest.approx(0.10)
    assert soft.wear_rate_pct_per_lap.quality is Quality.INSUFFICIENT
    assert soft.wear_rate_pct_per_lap.samples == 1
    assert profile.reference_compound == "c3"


def test_empty_profile_is_valid_and_marks_every_value_unavailable() -> None:
    empty = build_weekend_profile([], MetricsConfig())
    no_stints = build_weekend_profile(
        (WeekendSession(session_id=1, session_type="practice_1"),),
        MetricsConfig(),
    )

    for profile in (empty, no_stints):
        assert profile.compounds == ()
        assert profile.reference_compound is None
        assert profile.inventory == ()
        assert profile.quality is Quality.UNAVAILABLE
        assert profile.pit_loss_s.quality is Quality.UNAVAILABLE
        assert profile.pit_loss_s.value is None
        assert profile.schema_version == PROFILE_SCHEMA_VERSION
        assert profile_to_dict(profile)["quality"] == "unavailable"

    assert empty.sessions == ()
    assert no_stints.sessions == (1,)
    assert empty.hash != no_stints.hash


def test_hash_is_stable_and_independent_of_input_order() -> None:
    first = build_weekend_profile(_weekend(), MetricsConfig())

    reversed_sessions = tuple(reversed(_weekend()))
    shuffled = tuple(
        WeekendSession(
            session_id=session.session_id,
            session_type=session.session_type,
            stint_summaries=tuple(reversed(session.stint_summaries)),
            tyre_inventory=session.tyre_inventory,
            pit_losses_s=session.pit_losses_s,
        )
        for session in reversed_sessions
    )
    second = build_weekend_profile(shuffled, MetricsConfig())

    assert first.to_dict() == second.to_dict()
    assert first.hash == second.hash
    assert first.hash == profile_hash(first)
    assert first.hash == hashlib.sha256(first.to_json().encode("utf-8")).hexdigest()
    assert len(first.hash) == 64


def test_profile_json_round_trips_without_changing_its_hash() -> None:
    profile = build_weekend_profile(_weekend(), MetricsConfig())

    restored = profile_from_json(profile.to_json())

    assert restored == profile
    assert restored.hash == profile.hash


def test_profile_json_rejects_an_unsupported_schema_version() -> None:
    with pytest.raises(ValueError, match="Versión de perfil no compatible"):
        profile_from_json('{"schema_version":999}')


def test_reference_compound_follows_the_softest_dry_compound_and_can_be_forced() -> None:
    softest = build_weekend_profile(_weekend(), MetricsConfig())
    assert softest.reference_compound == "c4"

    forced = build_weekend_profile(_weekend(), MetricsConfig(), reference_compound="c3")
    assert forced.reference_compound == "c3"
    medium = forced.compound("c4")
    soft = forced.compound("c3")
    assert medium is not None and soft is not None
    assert soft.pace_delta_vs_reference_s.value == 0.0
    assert medium.pace_delta_vs_reference_s.value == pytest.approx(-1.0)

    unknown = build_weekend_profile(_weekend(), MetricsConfig(), reference_compound="c9")
    assert unknown.reference_compound == "c4"


def test_wet_only_weekend_has_no_dry_reference() -> None:
    sessions = (
        WeekendSession(
            session_id=1,
            stint_summaries=(_summary(compound="intermediate", visual_compound="intermediate"),),
        ),
    )

    profile = build_weekend_profile(sessions, MetricsConfig())

    assert profile.compound("intermediate") is not None
    assert profile.reference_compound is None


def test_inventory_prefers_the_race_start_of_the_latest_session() -> None:
    sessions = (
        WeekendSession(
            session_id=1,
            tyre_inventory=(
                _inventory_set(kind="race_start", set_index=1),
                _inventory_set(kind="race_start", set_index=0, fitted=True),
                _inventory_set(kind="session_end", set_index=0),
            ),
        ),
        WeekendSession(
            session_id=2,
            tyre_inventory=(_inventory_set(kind="race_start", set_index=9, compound="c4"),),
        ),
    )

    profile = build_weekend_profile(sessions, MetricsConfig())

    assert [snapshot.set_index for snapshot in profile.inventory] == [9]
    assert all(snapshot.kind == "race_start" for snapshot in profile.inventory)


def test_inventory_falls_back_to_the_last_session_end() -> None:
    sessions = (
        WeekendSession(
            session_id=1,
            tyre_inventory=(
                _inventory_set(kind="session_end", set_index=1),
                _inventory_set(kind="session_end", set_index=0),
            ),
        ),
        WeekendSession(session_id=2),
    )

    profile = build_weekend_profile(sessions, MetricsConfig())

    assert [snapshot.set_index for snapshot in profile.inventory] == [0, 1]
    assert {snapshot.kind for snapshot in profile.inventory} == {"session_end"}


def test_blend_with_prior_handles_no_race_laps_and_large_samples() -> None:
    assert DEFAULT_PRIOR_STRENGTH == 3.0
    assert blend_with_prior(90.0, 92.0, 0) == 90.0
    assert blend_with_prior(90.0, 92.0, 3) == pytest.approx(91.0)
    assert blend_with_prior(90.0, 92.0, 9) == pytest.approx(91.5)
    assert blend_with_prior(90.0, 92.0, 1000) == pytest.approx(91.99401794616152)
    assert blend_with_prior(None, 92.0, 5) == 92.0
    assert blend_with_prior(90.0, None, 5) == 90.0
    assert blend_with_prior(90.0, 92.0, 5, k=0) == 92.0

    with pytest.raises(ValueError):
        blend_with_prior(90.0, 92.0, -1)
    with pytest.raises(ValueError):
        blend_with_prior(90.0, 92.0, 5, k=-1.0)


def test_profile_is_immutable_and_serializes_with_schema_version() -> None:
    profile = build_weekend_profile(_weekend(), MetricsConfig())
    payload = profile_to_dict(profile)

    assert payload["schema_version"] == PROFILE_SCHEMA_VERSION
    assert payload["quality"] == "ok"
    assert payload["reference_compound"] == "c4"
    assert isinstance(payload["compounds"], list)
    assert json.loads(profile_to_json(profile)) == payload
    assert profile.to_dict() == payload

    with pytest.raises(FrozenInstanceError):
        profile.compounds = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        profile.quality = Quality.LOW  # type: ignore[misc]

    assert isinstance(profile, WeekendProfile)
