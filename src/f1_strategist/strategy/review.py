"""Compara proyecciones guardadas con vueltas reales posteriores."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from ..metrics.history import LapRecord
from ..store.sqlite_store import StoredStrategyEvaluation


@dataclass(frozen=True, slots=True)
class HorizonError:
    horizon_laps: int
    samples: int
    mean_absolute_error_s: float


@dataclass(frozen=True, slots=True)
class StrategyReview:
    evaluations_total: int
    evaluations_used: int
    horizons: tuple[HorizonError, ...]


def _comparable_lap(
    lap: LapRecord | None,
    forecast_compound: str,
) -> bool:
    return (
        lap is not None
        and lap.valid is True
        and lap.closed
        and not lap.pit_entry
        and not lap.pit_exit
        and lap.lap_time_s is not None
        and math.isfinite(lap.lap_time_s)
        and lap.lap_time_s > 0.0
        and lap.compound is not None
        and lap.compound.strip().lower() == forecast_compound.strip().lower()
    )


def review_strategy_evaluations(
    evaluations: Iterable[StoredStrategyEvaluation],
    laps: Iterable[LapRecord],
) -> StrategyReview:
    """Calcula el error absoluto medio para cada vuelta de horizonte.

    Se usa la última revisión de cada vuelta y solo se comparan vueltas reales
    válidas, cerradas, sin parada y con el mismo compuesto proyectado.
    """
    latest_by_lap: dict[int, StoredStrategyEvaluation] = {}
    for evaluation in evaluations:
        previous = latest_by_lap.get(evaluation.lap)
        if previous is None or evaluation.seq > previous.seq:
            latest_by_lap[evaluation.lap] = evaluation

    actual_by_key = {(lap.car_index, lap.lap_number): lap for lap in laps}
    errors_by_horizon: dict[int, list[float]] = defaultdict(list)
    used_laps: set[int] = set()

    for evaluation in latest_by_lap.values():
        forecast = evaluation.projected_lap_times_s
        car_index = evaluation.forecast_car_index
        compound = evaluation.forecast_compound
        if not forecast or car_index is None or compound is None:
            continue
        for horizon, projected_time in enumerate(forecast, start=1):
            actual_lap = actual_by_key.get((car_index, evaluation.lap + horizon))
            if actual_lap is None:
                continue
            if (
                (
                    actual_lap.compound is not None
                    and actual_lap.compound.strip().lower() != compound.strip().lower()
                )
                or actual_lap.pit_entry
                or actual_lap.pit_exit
            ):
                break
            if not _comparable_lap(actual_lap, compound):
                continue
            assert actual_lap is not None and actual_lap.lap_time_s is not None
            errors_by_horizon[horizon].append(abs(projected_time - actual_lap.lap_time_s))
            used_laps.add(evaluation.lap)

    horizons = tuple(
        HorizonError(
            horizon_laps=horizon,
            samples=len(errors),
            mean_absolute_error_s=math.fsum(errors) / len(errors),
        )
        for horizon, errors in sorted(errors_by_horizon.items())
    )
    return StrategyReview(
        evaluations_total=len(latest_by_lap),
        evaluations_used=len(used_laps),
        horizons=horizons,
    )


__all__ = ["HorizonError", "StrategyReview", "review_strategy_evaluations"]
