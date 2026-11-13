"""Pérdida en boxes y reincorporación (S3)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from f1_strategist.config import MetricsConfig, StrategyConfig
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import CarSnapshot, RaceState
from f1_strategist.strategy.pit_model import (
    NOTE_CIRCUIT_GREEN_NOT_OFFICIAL,
    NOTE_CIRCUIT_PRIOR,
    NOTE_CIRCUIT_SC_ESTIMATE,
    NOTE_CIRCUIT_SC_UNCERTAINTY,
    NOTE_CIRCUIT_VSC_ESTIMATE,
    NOTE_FIELD_DISPERSION,
    NOTE_FIXED_BAND,
    NOTE_GAME_REJOIN_MISMATCH,
    NOTE_LANE_TIME_ONLY,
    NOTE_LAPPED_CARS_IGNORED,
    NOTE_LEADER_GAP_UNAVAILABLE,
    NOTE_NO_PIT_LOSS,
    NOTE_OVERRIDE,
    NOTE_PROFILE_PRIOR,
    NOTE_RIVAL_GAP_UNAVAILABLE,
    NOTE_RIVALS_DO_NOT_PIT,
    PitLossEstimate,
    estimate_pit_loss,
    estimate_rejoin,
)
from f1_strategist.weekend_profile import ProfileValue, WeekendProfile


def _car(index: int, **fields: object) -> CarSnapshot:
    return CarSnapshot(car_index=index, **fields)  # type: ignore[arg-type]


def _inputs(
    records: dict[int, tuple[LapRecord, ...]] | None = None,
    *,
    cars: tuple[CarSnapshot, ...] = (),
    config: MetricsConfig | None = None,
    profile: WeekendProfile | None = None,
    session_fields: dict[str, object] | None = None,
    car_index: int = 0,
) -> MetricInputs:
    state = RaceState().snapshot()
    slots = list(state.cars)
    for car in cars:
        slots[car.car_index] = car
    session = replace(state.session, **(session_fields or {}))
    state = replace(state, cars=tuple(slots), session=session)
    return MetricInputs(
        state,
        LapHistorySnapshot(records=records or {}),
        config or MetricsConfig(),
        10.0,
        car_index,
        profile,
    )


def _stop_laps(car_index: int, *, loss_s: float = 20.0) -> tuple[LapRecord, ...]:
    """Tanda con una parada cuyo coste exacto es ``loss_s`` sobre 90 s de referencia."""
    entry_lap = 90.0 + loss_s / 2
    return (
        LapRecord(car_index, 1, lap_time_s=90.0, valid=True, closed=True),
        LapRecord(
            car_index,
            2,
            lap_time_s=entry_lap,
            valid=True,
            pit_entry=True,
            pit_lane_time_s=25.0,
            pit_stop_time_s=2.5,
            closed=True,
        ),
        LapRecord(
            car_index,
            3,
            lap_time_s=entry_lap,
            valid=True,
            pit_exit=True,
            pit_lane_time_s=25.0,
            closed=True,
        ),
        LapRecord(car_index, 4, lap_time_s=90.0, valid=True, closed=True),
        LapRecord(car_index, 5, lap_time_s=90.0, valid=True, closed=True),
    )


def _profile(pit_loss: ProfileValue | None) -> WeekendProfile:
    return WeekendProfile(
        compounds=(),
        reference_compound=None,
        pit_loss_s=pit_loss or ProfileValue(value=None),
        sessions=(),
        quality=Quality.OK,
    )


def test_field_stops_give_the_exact_loss() -> None:
    records = {index: _stop_laps(index) for index in range(3)}
    estimate = estimate_pit_loss(
        _inputs(
            records,
            session_fields={"track": "sakhir", "safety_car_status": "no_safety_car"},
        ),
        StrategyConfig(),
    )

    assert estimate.available
    assert estimate.source == "field"
    assert estimate.value_s == pytest.approx(20.0)
    assert estimate.sample_count == 3
    assert estimate.quality is Quality.LOW
    assert estimate.dispersion_s == pytest.approx(0.0)
    assert NOTE_FIXED_BAND in estimate.notes
    assert estimate.band_s == pytest.approx(2.0)


def test_field_dispersion_becomes_the_band() -> None:
    records = {
        0: _stop_laps(0, loss_s=20.0),
        1: _stop_laps(1, loss_s=22.0),
        2: _stop_laps(2, loss_s=24.0),
    }
    estimate = estimate_pit_loss(_inputs(records), StrategyConfig())

    assert estimate.value_s == pytest.approx(22.0)
    assert estimate.dispersion_s is not None
    assert estimate.dispersion_s > 0
    assert estimate.band_s == pytest.approx(estimate.dispersion_s)
    assert NOTE_FIELD_DISPERSION in estimate.notes


def test_enough_field_stops_are_ok() -> None:
    records = {index: _stop_laps(index) for index in range(5)}
    estimate = estimate_pit_loss(_inputs(records), StrategyConfig())

    assert estimate.sample_count == 5
    assert estimate.quality is Quality.OK


def test_override_wins_over_any_other_source() -> None:
    records = {0: _stop_laps(0)}
    config = StrategyConfig(pit_loss_override_s=18.5)
    estimate = estimate_pit_loss(_inputs(records), config)

    assert estimate.source == "override"
    assert estimate.value_s == pytest.approx(18.5)
    assert estimate.band_s == pytest.approx(2.0)
    assert estimate.sample_count == 0
    assert NOTE_OVERRIDE in estimate.notes
    assert estimate.quality is Quality.OK


def test_profile_prior_is_used_without_field_stops() -> None:
    profile = _profile(ProfileValue(value=22.5, samples=6, quality=Quality.OK))
    estimate = estimate_pit_loss(_inputs(profile=profile), StrategyConfig())

    assert estimate.source == "profile"
    assert estimate.value_s == pytest.approx(22.5)
    assert estimate.sample_count == 6
    assert estimate.quality is Quality.OK
    assert NOTE_PROFILE_PRIOR in estimate.notes


def test_sakhir_circuit_prior_does_not_require_a_calendar_year() -> None:
    inputs = _inputs(session_fields={"track": "sakhir", "safety_car_status": "no_safety_car"})

    estimate = estimate_pit_loss(inputs, StrategyConfig())

    assert estimate.source == "circuit"
    assert estimate.value_s == pytest.approx(22.9)
    assert estimate.band_s == pytest.approx(2.0)
    assert estimate.quality is Quality.LOW
    assert NOTE_CIRCUIT_PRIOR in estimate.notes


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        ("suzuka", 23.75),
        ("Japan, Suzuka", 23.75),
        ("bahrain", 22.9),
        ("Las Vegas", 20.0),
        ("Madring", 31.1),
    ],
)
def test_circuit_aliases_resolve_only_to_their_explicit_row(track: str, expected: float) -> None:
    inputs = _inputs(session_fields={"track": track, "safety_car_status": "no_safety_car"})

    estimate = estimate_pit_loss(inputs, StrategyConfig())

    assert estimate.source == "circuit"
    assert estimate.value_s == pytest.approx(expected)


@pytest.mark.parametrize(
    ("status", "expected", "band", "note"),
    [
        ("no_safety_car", 22.9, 2.0, NOTE_CIRCUIT_PRIOR),
        ("virtual_safety_car", 11.9, 2.0, NOTE_CIRCUIT_VSC_ESTIMATE),
        ("full_safety_car", 10.0, 0.5, NOTE_CIRCUIT_SC_ESTIMATE),
    ],
)
def test_circuit_prior_uses_the_matching_flag_and_preserves_its_band(
    status: str,
    expected: float,
    band: float,
    note: str,
) -> None:
    inputs = _inputs(session_fields={"track": "sakhir", "safety_car_status": status})

    estimate = estimate_pit_loss(inputs, StrategyConfig())

    assert estimate.source == "circuit"
    assert estimate.value_s == pytest.approx(expected)
    assert estimate.band_s == pytest.approx(band)
    assert estimate.quality is Quality.LOW
    assert note in estimate.notes
    if status == "full_safety_car":
        assert NOTE_CIRCUIT_SC_UNCERTAINTY in estimate.notes


def test_circuit_prior_is_below_a_weekend_profile_and_skips_sepang() -> None:
    profile = _profile(ProfileValue(value=24.0, samples=3, quality=Quality.LOW))
    inputs = _inputs(
        profile=profile,
        session_fields={"track": "sakhir", "safety_car_status": "no_safety_car"},
    )
    estimate = estimate_pit_loss(inputs, StrategyConfig())
    unavailable = estimate_pit_loss(
        _inputs(session_fields={"track": "sepang", "safety_car_status": "no_safety_car"}),
        StrategyConfig(),
    )
    unknown_status = estimate_pit_loss(
        _inputs(session_fields={"track": "sakhir", "safety_car_status": "unknown"}),
        StrategyConfig(),
    )

    assert estimate.source == "profile"
    assert estimate.value_s == pytest.approx(24.0)
    assert unavailable.source == "none"
    assert unavailable.value_s is None
    assert unknown_status.source == "none"
    assert unknown_status.value_s is None


def test_madrid_prior_discloses_the_missing_official_green_flag_value() -> None:
    inputs = _inputs(session_fields={"track": "madring", "safety_car_status": "no_safety_car"})

    estimate = estimate_pit_loss(inputs, StrategyConfig())

    assert estimate.value_s == pytest.approx(31.1)
    assert NOTE_CIRCUIT_GREEN_NOT_OFFICIAL in estimate.notes


def test_negative_profile_prior_falls_back_to_lane_time() -> None:
    lane_only = (
        LapRecord(
            0,
            4,
            lap_time_s=115.0,
            valid=True,
            pit_exit=True,
            pit_lane_time_s=24.0,
            closed=True,
        ),
    )
    profile = _profile(ProfileValue(value=-1.0, samples=2, quality=Quality.LOW))
    estimate = estimate_pit_loss(_inputs({0: lane_only}, profile=profile), StrategyConfig())

    assert estimate.source == "lane"
    assert estimate.value_s == pytest.approx(24.0)


def test_lane_time_precedes_the_circuit_prior() -> None:
    lane_only = (
        LapRecord(
            0,
            4,
            lap_time_s=115.0,
            valid=True,
            pit_exit=True,
            pit_lane_time_s=24.0,
            closed=True,
        ),
    )
    estimate = estimate_pit_loss(
        _inputs(
            {0: lane_only},
            session_fields={"track": "sakhir", "safety_car_status": "no_safety_car"},
        ),
        StrategyConfig(),
    )

    assert estimate.source == "lane"
    assert estimate.value_s == pytest.approx(24.0)
    assert estimate.sample_count == 1
    assert estimate.quality is Quality.LOW
    assert NOTE_LANE_TIME_ONLY in estimate.notes


def test_partial_entry_lane_time_is_not_a_full_pit_loss() -> None:
    """Una entrada cerrada con 1,933 s de carril no es la pérdida completa."""
    partial = (
        LapRecord(
            0,
            4,
            lap_time_s=115.0,
            valid=True,
            pit_entry=True,
            pit_lane_time_s=1.933,
            closed=True,
        ),
    )
    estimate = estimate_pit_loss(_inputs({0: partial}), StrategyConfig())

    assert not estimate.available
    assert estimate.value_s is None
    assert estimate.source == "none"
    assert estimate.notes == (NOTE_NO_PIT_LOSS,)


def test_exit_lap_lane_time_completes_the_partial_entry() -> None:
    """La vuelta de salida acumula el tiempo completo y sí es una muestra válida."""
    complete = (
        LapRecord(
            0,
            4,
            lap_time_s=115.0,
            valid=True,
            pit_entry=True,
            pit_lane_time_s=1.933,
            closed=True,
        ),
        LapRecord(
            0,
            5,
            lap_time_s=92.0,
            valid=True,
            pit_exit=True,
            pit_lane_time_s=23.933,
            closed=True,
        ),
    )
    estimate = estimate_pit_loss(_inputs({0: complete}), StrategyConfig())

    assert estimate.available
    assert estimate.source == "lane"
    assert estimate.value_s == pytest.approx(23.933)


def test_no_source_means_unavailable_and_never_zero() -> None:
    estimate = estimate_pit_loss(_inputs(), StrategyConfig())

    assert not estimate.available
    assert estimate.value_s is None
    assert estimate.source == "none"
    assert estimate.quality is Quality.UNAVAILABLE
    assert estimate.notes == (NOTE_NO_PIT_LOSS,)


def test_pit_loss_estimate_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="value_s"):
        PitLossEstimate(
            value_s=-1.0,
            band_s=2.0,
            dispersion_s=None,
            source="field",
            sample_count=1,
            quality=Quality.LOW,
        )


def _rejoin_inputs(
    *,
    session_fields: dict[str, object] | None = None,
    player_fields: dict[str, object] | None = None,
    rivals: tuple[CarSnapshot, ...] = (),
    car_index: int = 0,
) -> MetricInputs:
    values: dict[str, object] = {
        "position": 3,
        "current_lap": 20,
        "delta_to_leader_ms": 10_000,
    }
    values.update(player_fields or {})
    player = CarSnapshot(car_index=0, **values)  # type: ignore[arg-type]
    return _inputs(
        cars=(player, *rivals),
        session_fields=session_fields,
        car_index=car_index,
    )


def _rival(index: int, position: int, gap_s: float, **fields: object) -> CarSnapshot:
    values: dict[str, object] = {
        "position": position,
        "current_lap": 20,
        "delta_to_leader_ms": int(gap_s * 1000),
    }
    values.update(fields)
    return CarSnapshot(car_index=index, **values)  # type: ignore[arg-type]


def test_rejoin_with_known_gaps() -> None:
    inputs = _rejoin_inputs(
        rivals=(
            _car(1, position=1, current_lap=20),
            _rival(2, 2, 5.0),
            _rival(3, 4, 15.0),
            _rival(4, 5, 25.0),
            _rival(5, 6, 35.0),
        )
    )
    estimate = estimate_rejoin(inputs, 20.0, StrategyConfig())

    assert estimate.available
    assert estimate.position == 5
    assert estimate.car_ahead_index == 4
    assert estimate.gap_ahead_s == pytest.approx(5.0)
    assert estimate.car_behind_index == 5
    assert estimate.gap_behind_s == pytest.approx(5.0)
    assert estimate.traffic_risk is False
    assert estimate.quality is Quality.OK
    assert NOTE_RIVALS_DO_NOT_PIT in estimate.notes


def test_rejoin_position_range_reflects_pit_loss_uncertainty() -> None:
    inputs = _rejoin_inputs(
        rivals=(
            _car(1, position=1, current_lap=20),
            _rival(2, 2, 5.0),
            _rival(3, 4, 15.0),
            _rival(4, 5, 29.0),
            _rival(5, 6, 35.0),
        )
    )

    estimate = estimate_rejoin(
        inputs,
        20.0,
        StrategyConfig(),
        pit_loss_band_s=2.0,
    )

    assert estimate.position == 5
    assert estimate.position_range == (4, 5)


def test_rejoin_flags_traffic_risk_when_ahead_is_too_close() -> None:
    inputs = _rejoin_inputs(rivals=(_rival(2, 4, 29.5),))
    estimate = estimate_rejoin(inputs, 20.0, StrategyConfig())

    assert estimate.position == 2
    assert estimate.gap_ahead_s == pytest.approx(0.5)
    assert estimate.traffic_risk is True


def test_rejoin_ignores_retired_cars() -> None:
    inputs = _rejoin_inputs(
        rivals=(
            _rival(2, 2, 12.0, result_status="retired"),
            _rival(3, 3, 15.0),
        )
    )
    estimate = estimate_rejoin(inputs, 20.0, StrategyConfig())

    assert estimate.position == 2
    assert estimate.car_ahead_index == 3


def test_rejoin_ignores_lapped_cars() -> None:
    inputs = _rejoin_inputs(
        rivals=(
            _rival(2, 2, 12.0, current_lap=18),
            _rival(3, 3, 15.0),
        )
    )
    estimate = estimate_rejoin(inputs, 20.0, StrategyConfig())

    assert estimate.position == 2
    assert NOTE_LAPPED_CARS_IGNORED in estimate.notes


def test_rejoin_reports_the_gap_with_the_game_position() -> None:
    inputs = _rejoin_inputs(
        rivals=(_rival(2, 2, 5.0),),
        session_fields={"pit_rejoin_position": 1},
    )
    estimate = estimate_rejoin(inputs, 20.0, StrategyConfig())

    assert estimate.position == 2
    assert estimate.game_rejoin_position == 1
    assert estimate.game_delta_positions == 1
    assert NOTE_GAME_REJOIN_MISMATCH in estimate.notes


def test_rejoin_marks_missing_rival_gaps() -> None:
    inputs = _rejoin_inputs(
        rivals=(
            _rival(2, 2, 5.0),
            _rival(3, 4, 10.0),
            _rival(4, 5, 15.0),
            _rival(5, 6, 20.0),
            _car(6, position=7, current_lap=20),
        )
    )
    estimate = estimate_rejoin(inputs, 20.0, StrategyConfig())

    assert estimate.position == 5
    assert NOTE_RIVAL_GAP_UNAVAILABLE in estimate.notes
    assert estimate.quality is Quality.LOW


def test_rejoin_without_leader_gap_is_insufficient() -> None:
    inputs = _rejoin_inputs(player_fields={"delta_to_leader_ms": None})
    estimate = estimate_rejoin(inputs, 20.0, StrategyConfig())

    assert not estimate.available
    assert estimate.position is None
    assert estimate.quality is Quality.INSUFFICIENT
    assert NOTE_LEADER_GAP_UNAVAILABLE in estimate.notes


def test_rejoin_without_active_rivals_is_insufficient() -> None:
    estimate = estimate_rejoin(_rejoin_inputs(), 20.0, StrategyConfig())

    assert not estimate.available
    assert estimate.quality is Quality.INSUFFICIENT


def test_rejoin_without_player_is_unavailable() -> None:
    estimate = estimate_rejoin(_rejoin_inputs(car_index=99), 20.0, StrategyConfig())

    assert not estimate.available
    assert estimate.quality is Quality.UNAVAILABLE


def test_rejoin_rejects_negative_pit_loss() -> None:
    with pytest.raises(ValueError, match="pit_loss_s"):
        estimate_rejoin(_rejoin_inputs(), -1.0, StrategyConfig())


def test_estimates_are_deterministic() -> None:
    records = {index: _stop_laps(index) for index in range(5)}
    profile = _profile(ProfileValue(value=21.0, samples=4, quality=Quality.LOW))
    inputs = _rejoin_inputs(rivals=(_rival(2, 2, 5.0), _rival(3, 4, 15.0)))

    assert estimate_pit_loss(_inputs(records), StrategyConfig()) == estimate_pit_loss(
        _inputs(records), StrategyConfig()
    )
    loss = estimate_pit_loss(_inputs(records, profile=profile), StrategyConfig())
    assert estimate_rejoin(inputs, 20.0, StrategyConfig()) == estimate_rejoin(
        inputs, 20.0, StrategyConfig()
    )
    assert loss.to_dict()["source"] == "field"


@pytest.mark.parametrize("status", ["did_not_finish", "disqualified", "not_classified"])
def test_rejoin_ignores_terminal_result_statuses(status: str) -> None:
    inputs = _rejoin_inputs(
        rivals=(
            _rival(2, 2, 12.0, result_status=status),
            _rival(3, 3, 15.0),
        )
    )
    estimate = estimate_rejoin(inputs, 20.0, StrategyConfig())

    assert estimate.position == 2
    assert estimate.car_ahead_index == 3


def test_negative_field_samples_fall_back_instead_of_raising() -> None:
    records = {0: _stop_laps(0, loss_s=-40.0)}
    estimate = estimate_pit_loss(_inputs(records), StrategyConfig())

    assert estimate.available
    assert estimate.source == "lane"
    assert estimate.value_s == pytest.approx(25.0)
