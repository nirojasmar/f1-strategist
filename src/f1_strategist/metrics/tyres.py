"""Métricas de neumáticos."""

from __future__ import annotations

from statistics import StatisticsError, fmean, linear_regression

from ..race_state import PacketFreshness
from .comparability import classify_lap, is_clean
from .history import LapRecord, MetricInputs, current_stint_bounds
from .types import Metric, Quality, Window, assess_quality, insufficient, unavailable


class TyreMetrics:
    """Calcula desgaste y vida útil de los neumáticos del coche."""

    name = "tyres"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        car = inputs.car
        status_freshness = inputs.state.freshness_for("car_status")
        damage_freshness = inputs.state.freshness_for("car_damage")
        sets_freshness = inputs.state.freshness_for("tyre_sets")
        history_sources = ("car_damage", "car_status", "lap_data")
        history_stale = any(
            item.is_stale(inputs.now)
            for item in (damage_freshness, status_freshness, inputs.state.freshness_for("lap_data"))
        )

        compound = None
        if car is not None:
            compound = car.actual_tyre_compound or (
                car.fitted_tyre.actual_tyre_compound if car.fitted_tyre is not None else None
            )
        compound_metric = (
            _value_metric(
                "tyres.compound",
                compound,
                "compound",
                inputs.now,
                status_freshness,
                sources=("car_status",),
            )
            if compound is not None
            else unavailable(
                "tyres.compound",
                "compound",
                "tyres_compound_unavailable",
                inputs.now,
                sources=("car_status",),
            )
        )
        age = car.tyre_age_laps if car is not None else None
        age_metric = (
            _value_metric(
                "tyres.age_laps",
                age,
                "lap",
                inputs.now,
                status_freshness,
                sources=("car_status",),
            )
            if age is not None
            else unavailable(
                "tyres.age_laps",
                "lap",
                "tyres_age_unavailable",
                inputs.now,
                sources=("car_status",),
            )
        )

        wear = car.damage.tyres_wear if car is not None and car.damage is not None else ()
        wheel_metrics = tuple(
            _value_metric(
                f"tyres.wear_wheel_{index}_pct",
                wear[index - 1],
                "%",
                inputs.now,
                damage_freshness,
                sources=("car_damage",),
            )
            if len(wear) == 4
            else unavailable(
                f"tyres.wear_wheel_{index}_pct",
                "%",
                "tyres_wear_unavailable",
                inputs.now,
                sources=("car_damage",),
            )
            for index in range(1, 5)
        )

        mean_wear = fmean(wear) if len(wear) == 4 else None
        max_wear = max(wear) if len(wear) == 4 else None
        wear_mean_metric = (
            _value_metric(
                "tyres.wear_mean_pct",
                mean_wear,
                "%",
                inputs.now,
                damage_freshness,
                sources=("car_damage",),
            )
            if mean_wear is not None
            else unavailable(
                "tyres.wear_mean_pct",
                "%",
                "tyres_wear_unavailable",
                inputs.now,
                sources=("car_damage",),
            )
        )
        wear_max_metric = (
            _value_metric(
                "tyres.wear_max_pct",
                max_wear,
                "%",
                inputs.now,
                damage_freshness,
                sources=("car_damage",),
            )
            if max_wear is not None
            else unavailable(
                "tyres.wear_max_pct",
                "%",
                "tyres_wear_unavailable",
                inputs.now,
                sources=("car_damage",),
            )
        )

        samples = _current_stint_clean_samples(inputs, compound)
        # Keep each regression observation paired with its age, omitting malformed records.
        regression_samples = [
            (float(record.tyre_age_end), fmean(record.tyre_wear_end_pct))
            for record in samples
            if record.tyre_age_end is not None
            and record.tyre_wear_end_pct is not None
            and len(record.tyre_wear_end_pct) == 4
        ]
        x_values = [age_value for age_value, _ in regression_samples]
        y_values = [wear_value for _, wear_value in regression_samples]
        low_at, ok_at = inputs.config.thresholds("tyres")
        minimum_samples = max(3, low_at)
        rate: float | None = None
        rate_quality: Quality | None = None
        if len(regression_samples) >= minimum_samples:
            try:
                rate = linear_regression(x_values, y_values).slope
                rate_quality = (
                    Quality.STALE
                    if history_stale
                    else assess_quality(len(regression_samples), ok_at, low_at, False)
                )
            except StatisticsError:
                pass
        rate_metric = (
            Metric(
                key="tyres.wear_rate_pct_per_lap",
                value=rate,
                unit="%/lap",
                window=Window("stint"),
                quality=rate_quality or Quality.INSUFFICIENT,
                sample_count=len(regression_samples),
                computed_at_monotonic=inputs.now,
                sources=history_sources,
            )
            if rate is not None
            else insufficient(
                "tyres.wear_rate_pct_per_lap",
                "%/lap",
                "tyres_wear_rate_samples_insufficient",
                inputs.now,
                window=Window("stint"),
                sample_count=len(regression_samples),
                sources=history_sources,
            )
        )

        wear_limit = inputs.config.tyre_wear_limit_pct
        if wear_limit is None:
            laps_to_limit_metric = unavailable(
                "tyres.laps_to_wear_limit",
                "lap",
                "tyres_wear_limit_not_configured",
                inputs.now,
                window=Window("stint"),
                sample_count=len(regression_samples),
                sources=history_sources,
            )
        elif mean_wear is None:
            laps_to_limit_metric = unavailable(
                "tyres.laps_to_wear_limit",
                "lap",
                "tyres_wear_unavailable",
                inputs.now,
                window=Window("stint"),
                sample_count=len(regression_samples),
                sources=("car_damage",),
            )
        elif mean_wear >= wear_limit:
            laps_to_limit_metric = _derived_metric(
                "tyres.laps_to_wear_limit",
                0.0,
                "lap",
                inputs.now,
                damage_freshness.is_stale(inputs.now),
                len(regression_samples),
                ("car_damage",),
                Window("stint"),
            )
        elif rate is None or rate <= 0:
            laps_to_limit_metric = insufficient(
                "tyres.laps_to_wear_limit",
                "lap",
                "tyres_wear_rate_unavailable",
                inputs.now,
                window=Window("stint"),
                sample_count=len(regression_samples),
                sources=history_sources,
            )
        else:
            laps_to_limit_metric = _derived_metric(
                "tyres.laps_to_wear_limit",
                (wear_limit - mean_wear) / rate,
                "lap",
                inputs.now,
                history_stale,
                len(regression_samples),
                history_sources,
                Window("stint"),
                rate_quality or Quality.OK,
            )

        fitted = car.fitted_tyre if car is not None else None
        game_lifespan = fitted.usable_life_laps if fitted is not None else None
        lifespan_metric = (
            _value_metric(
                "tyres.game_lifespan_laps",
                game_lifespan,
                "lap",
                inputs.now,
                sets_freshness,
                sources=("tyre_sets",),
            )
            if game_lifespan is not None
            else unavailable(
                "tyres.game_lifespan_laps",
                "lap",
                "tyres_game_lifespan_unavailable",
                inputs.now,
                sources=("tyre_sets",),
            )
        )
        blisters = car.damage.tyre_blisters if car is not None and car.damage is not None else ()
        damages = car.damage.tyres_damage if car is not None and car.damage is not None else ()
        blister_metric = _max_metric(
            "tyres.max_blister_pct", blisters, inputs.now, damage_freshness
        )
        damage_metric = _max_metric("tyres.max_damage_pct", damages, inputs.now, damage_freshness)
        return (
            compound_metric,
            age_metric,
            *wheel_metrics,
            wear_mean_metric,
            wear_max_metric,
            rate_metric,
            laps_to_limit_metric,
            lifespan_metric,
            blister_metric,
            damage_metric,
        )


def _current_stint_clean_samples(
    inputs: MetricInputs, compound: str | None
) -> tuple[LapRecord, ...]:
    records = sorted(inputs.history.records_for(inputs.car_index), key=lambda item: item.lap_number)
    bounds = current_stint_bounds(inputs, inputs.car_index)
    if bounds is None:
        return ()
    start_lap, end_lap = bounds
    car = inputs.car
    current_lap = car.current_lap if car is not None else None
    if current_lap is None:
        current_lap = max((record.lap_number for record in records), default=0) + 1

    samples: list[LapRecord] = []
    for record in records:
        if not record.closed or (start_lap is not None and record.lap_number < start_lap):
            continue
        if record.lap_number > current_lap or (end_lap is not None and record.lap_number > end_lap):
            continue
        if compound is not None and record.compound != compound:
            continue
        if (
            record.tyre_age_end is None
            or record.tyre_wear_end_pct is None
            or len(record.tyre_wear_end_pct) != 4
        ):
            continue
        flags = classify_lap(record, inputs.config)
        if not is_clean(flags) or {
            "partial_lap",
            "incomplete",
            "telemetry_partial",
            "retired",
        } & record.flags:
            continue
        samples.append(record)
    return tuple(samples)


def _value_metric(
    key: str,
    value: float | int | str,
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


def _derived_metric(
    key: str,
    value: float,
    unit: str,
    now: float,
    stale: bool,
    sample_count: int,
    sources: tuple[str, ...],
    window: Window,
    calculated_quality: Quality = Quality.OK,
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=window,
        quality=Quality.STALE if stale else calculated_quality,
        sample_count=sample_count,
        computed_at_monotonic=now,
        sources=sources,
    )


def _max_metric(
    key: str,
    values: tuple[int, ...],
    now: float,
    freshness: PacketFreshness,
) -> Metric:
    if not values:
        return unavailable(
            key,
            "%",
            "tyres_damage_unavailable",
            now,
            sources=("car_damage",),
        )
    return _value_metric(key, max(values), "%", now, freshness, sources=("car_damage",))
