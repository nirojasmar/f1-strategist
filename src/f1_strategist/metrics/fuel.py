"""Métricas de combustible."""

from __future__ import annotations

from statistics import fmean, pstdev

from ..race_state import PacketFreshness
from .comparability import classify_lap, is_clean
from .history import LapRecord, MetricInputs
from .types import Metric, Quality, Window, assess_quality, insufficient, unavailable

_EXCLUDED_FLAGS = frozenset({"partial_lap", "incomplete", "retired"})


def _latest_stint_records(records: list[LapRecord]) -> list[LapRecord]:
    """Selecciona el último stint y compuesto de la ventana de combustible."""
    if not records:
        return []

    start = 0
    for index, record in enumerate(records):
        if record.pit_exit or record.tyre_age_start == 0:
            start = index

    stint = records[start:]
    compound = stint[-1].compound
    if compound is None:
        return stint
    return [record for record in stint if record.compound in (None, compound)]


class FuelMetrics:
    """Calcula consumo observado y estimaciones de autonomía."""

    name = "fuel"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        car = inputs.car
        tank_freshness = inputs.state.freshness_for("car_status")
        lap_freshness = inputs.state.freshness_for("lap_data")
        session_freshness = inputs.state.freshness_for("session")
        sources = ("car_status", "lap_data")
        stale = tank_freshness.is_stale(inputs.now) or lap_freshness.is_stale(inputs.now)
        margin_stale = stale or session_freshness.is_stale(inputs.now)

        tank = car.fuel_kg if car is not None else None
        tank_metric = (
            _value_metric(
                "fuel.in_tank_kg",
                tank,
                "kg",
                inputs.now,
                tank_freshness,
                sources=("car_status",),
            )
            if tank is not None
            else unavailable(
                "fuel.in_tank_kg",
                "kg",
                "fuel_tank_unavailable",
                inputs.now,
                sources=("car_status",),
            )
        )

        records = sorted(
            inputs.history.records_for(inputs.car_index), key=lambda item: item.lap_number
        )
        eligible = [
            record
            for record in records
            if record.closed
            and record.fuel_start_kg is not None
            and record.fuel_end_kg is not None
            and not (_EXCLUDED_FLAGS & record.flags)
            and record.fuel_start_kg >= record.fuel_end_kg
        ]
        low_at, ok_at = inputs.config.thresholds("fuel")
        window_size = inputs.config.fuel_window_laps
        selected = eligible[-window_size:]
        stint_records = _latest_stint_records(selected)
        consumptions = [
            record.fuel_start_kg - record.fuel_end_kg
            for record in stint_records
            if record.fuel_start_kg is not None and record.fuel_end_kg is not None
        ]
        comparable_count = sum(is_clean(classify_lap(record, inputs.config)) for record in selected)
        window = Window("laps", window_size)

        comparable_metric = Metric(
            key="fuel.comparable_laps_count",
            value=comparable_count,
            unit="lap",
            window=window,
            quality=Quality.STALE if stale else Quality.OK,
            sample_count=len(selected),
            computed_at_monotonic=inputs.now,
            sources=sources,
        )

        if len(consumptions) < max(2, low_at):
            consumption_metric = insufficient(
                "fuel.consumption_per_lap",
                "kg/lap",
                "fuel_consumption_samples_insufficient",
                inputs.now,
                window=window,
                sample_count=len(consumptions),
                sources=sources,
            )
            stddev_metric = insufficient(
                "fuel.consumption_stddev",
                "kg/lap",
                "fuel_consumption_samples_insufficient",
                inputs.now,
                window=window,
                sample_count=len(consumptions),
                sources=sources,
            )
            consumption = None
        else:
            consumption = fmean(consumptions)
            quality = (
                Quality.STALE
                if stale
                else assess_quality(
                    len(consumptions),
                    ok_at=ok_at,
                    low_at=low_at,
                    stale=False,
                )
            )
            consumption_metric = _calculated(
                "fuel.consumption_per_lap",
                consumption,
                "kg/lap",
                inputs.now,
                window,
                quality,
                len(consumptions),
                sources,
            )
            stddev_metric = _calculated(
                "fuel.consumption_stddev",
                pstdev(consumptions),
                "kg/lap",
                inputs.now,
                window,
                quality,
                len(consumptions),
                sources,
            )

        laps_of_fuel = tank / consumption if tank is not None and consumption else None
        estimate_quality = Quality.OK if len(consumptions) >= ok_at else Quality.LOW
        laps_of_fuel_metric = (
            _derived_metric(
                "fuel.laps_of_fuel",
                laps_of_fuel,
                "lap",
                inputs.now,
                stale,
                len(consumptions),
                sources,
                estimate_quality,
            )
            if laps_of_fuel is not None
            else insufficient(
                "fuel.laps_of_fuel",
                "lap",
                "fuel_laps_of_fuel_unavailable",
                inputs.now,
                sample_count=len(consumptions),
                sources=sources,
            )
        )

        laps_remaining = _laps_remaining(inputs)
        if laps_remaining is None:
            margin_laps_metric = unavailable(
                "fuel.margin_laps_at_finish",
                "lap",
                "fuel_laps_remaining_unavailable",
                inputs.now,
                sources=("lap_data", "session", "car_status"),
            )
            margin_kg_metric = unavailable(
                "fuel.margin_kg_at_finish",
                "kg",
                "fuel_laps_remaining_unavailable",
                inputs.now,
                sources=("lap_data", "session", "car_status"),
            )
        elif laps_of_fuel is None or tank is None or consumption is None:
            margin_laps_metric = insufficient(
                "fuel.margin_laps_at_finish",
                "lap",
                "fuel_estimate_unavailable",
                inputs.now,
                sources=sources,
            )
            margin_kg_metric = insufficient(
                "fuel.margin_kg_at_finish",
                "kg",
                "fuel_estimate_unavailable",
                inputs.now,
                sources=sources,
            )
        else:
            margin_laps_metric = _derived_metric(
                "fuel.margin_laps_at_finish",
                laps_of_fuel - laps_remaining,
                "lap",
                inputs.now,
                margin_stale,
                len(consumptions),
                ("car_status", "lap_data", "session"),
                estimate_quality,
            )
            margin_kg_metric = _derived_metric(
                "fuel.margin_kg_at_finish",
                tank - consumption * laps_remaining,
                "kg",
                inputs.now,
                margin_stale,
                len(consumptions),
                ("car_status", "lap_data", "session"),
                estimate_quality,
            )

        game_laps = car.fuel_remaining_laps if car is not None else None
        game_laps_metric = (
            _value_metric(
                "fuel.game_remaining_laps",
                game_laps,
                "lap",
                inputs.now,
                tank_freshness,
                sources=("car_status",),
            )
            if game_laps is not None
            else unavailable(
                "fuel.game_remaining_laps",
                "lap",
                "fuel_game_laps_unavailable",
                inputs.now,
                sources=("car_status",),
            )
        )
        game_deviation = (
            laps_of_fuel - game_laps if laps_of_fuel is not None and game_laps is not None else None
        )
        game_deviation_metric = (
            _derived_metric(
                "fuel.game_deviation_laps",
                game_deviation,
                "lap",
                inputs.now,
                stale,
                len(consumptions),
                sources,
                estimate_quality,
            )
            if game_deviation is not None
            else unavailable(
                "fuel.game_deviation_laps",
                "lap",
                "fuel_game_deviation_unavailable",
                inputs.now,
                sources=sources,
            )
        )
        return (
            tank_metric,
            consumption_metric,
            stddev_metric,
            laps_of_fuel_metric,
            margin_laps_metric,
            margin_kg_metric,
            game_laps_metric,
            game_deviation_metric,
            comparable_metric,
        )


def _laps_remaining(inputs: MetricInputs) -> float | None:
    session = inputs.state.session
    if session.total_laps is None:
        return None
    if session.ended is True:
        return 0.0
    if session.safety_car_status == "formation_lap":
        return float(session.total_laps)
    car = inputs.car
    if (
        car is None
        or car.current_lap is None
        or car.lap_distance_m is None
        or session.track_length_m is None
        or session.track_length_m <= 0
    ):
        return None
    lap_fraction = min(max(car.lap_distance_m / session.track_length_m, 0.0), 1.0)
    return max(0.0, session.total_laps - (car.current_lap - 1) - lap_fraction)


def _value_metric(
    key: str,
    value: float,
    unit: str,
    now: float,
    freshness: PacketFreshness,
    *,
    sources: tuple[str, ...],
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("instant"),
        quality=Quality.STALE if freshness.is_stale(now) else Quality.OK,
        sample_count=1,
        computed_at_monotonic=now,
        sources=sources,
    )


def _calculated(
    key: str,
    value: float,
    unit: str,
    now: float,
    window: Window,
    quality: Quality,
    sample_count: int,
    sources: tuple[str, ...],
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=window,
        quality=quality,
        sample_count=sample_count,
        computed_at_monotonic=now,
        sources=sources,
    )


def _derived_metric(
    key: str,
    value: float,
    unit: str,
    now: float,
    stale: bool,
    sample_count: int,
    sources: tuple[str, ...],
    calculated_quality: Quality = Quality.OK,
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("instant"),
        quality=Quality.STALE if stale else calculated_quality,
        sample_count=sample_count,
        computed_at_monotonic=now,
        sources=sources,
    )
