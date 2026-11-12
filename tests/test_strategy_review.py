from __future__ import annotations

from f1_strategist.metrics.history import LapRecord
from f1_strategist.store.sqlite_store import StoredStrategyEvaluation
from f1_strategist.strategy.review import review_strategy_evaluations


def _evaluation(
    seq: int,
    lap: int,
    projected: tuple[float, ...] | None,
) -> StoredStrategyEvaluation:
    return StoredStrategyEvaluation(
        seq=seq,
        t_sim_s=float(seq),
        lap=lap,
        action="NO_CHANGE",
        recommended_key="stay_out",
        gain_vs_baseline_s=None,
        gain_low_s=None,
        gain_high_s=None,
        confidence=None,
        data_quality="ok",
        inputs_hash="a" * 64,
        profile_hash=None,
        evaluation_json="{}",
        forecast_car_index=0 if projected is not None else None,
        forecast_compound="soft" if projected is not None else None,
        projected_lap_times_s=projected,
    )


def _lap(
    number: int,
    time_s: float,
    *,
    pit_entry: bool = False,
) -> LapRecord:
    return LapRecord(
        car_index=0,
        lap_number=number,
        lap_time_s=time_s,
        valid=True,
        compound="soft",
        pit_entry=pit_entry,
        closed=True,
    )


def test_review_uses_latest_revision_and_reports_mae_by_future_lap() -> None:
    result = review_strategy_evaluations(
        (
            _evaluation(1, 1, (100.0, 100.0)),
            _evaluation(2, 1, (92.0, 92.0, 92.0)),
            _evaluation(3, 2, (91.0, 91.0)),
        ),
        (
            _lap(2, 91.5),
            _lap(3, 91.25),
            _lap(4, 90.0, pit_entry=True),
        ),
    )

    assert result.evaluations_total == 2
    assert result.evaluations_used == 2
    assert [(item.horizon_laps, item.samples) for item in result.horizons] == [(1, 2), (2, 1)]
    assert result.horizons[0].mean_absolute_error_s == 0.375
    assert result.horizons[1].mean_absolute_error_s == 0.75


def test_review_counts_legacy_evaluations_without_forecasts_as_unused() -> None:
    result = review_strategy_evaluations(
        (_evaluation(1, 1, None),),
        (_lap(2, 91.5),),
    )

    assert result.evaluations_total == 1
    assert result.evaluations_used == 0
    assert result.horizons == ()
