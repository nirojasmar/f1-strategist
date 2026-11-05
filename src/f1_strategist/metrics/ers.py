"""ERS metrics derived from the current car state and completed laps."""

from __future__ import annotations

from ..race_state import PacketFreshness
from .comparability import classify_lap
from .history import MetricInputs
from .types import Metric, Quality, Window, unavailable


class ERSMetrics:
    """Expose current ERS state and lap-level harvest/deploy totals."""

    name = "ers"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        car = inputs.car
        freshness = _freshness(inputs, "car_status")
        store = car.ers_store_j if car is not None else None
        deploy_mode = car.ers_deploy_mode if car is not None else None
        results: list[Metric] = []

        results.append(
            _state_metric(
                "ers.store_j",
                store,
                "J",
                inputs,
                freshness,
                reason="ers_store_unavailable",
            )
        )
        capacity = inputs.config.ers_capacity_joules
        if capacity is None:
            results.append(
                unavailable(
                    "ers.capacity_j",
                    "J",
                    "ers_capacity_not_configured",
                    inputs.now,
                    sources=("metrics_config",),
                )
            )
        else:
            results.append(
                Metric(
                    key="ers.capacity_j",
                    value=capacity,
                    unit="J",
                    window=Window("instant"),
                    quality=Quality.OK,
                    sample_count=1,
                    computed_at_monotonic=inputs.now,
                    sources=("metrics_config",),
                )
            )
        if store is None:
            results.append(
                unavailable(
                    "ers.store_pct",
                    "%",
                    "ers_store_unavailable",
                    inputs.now,
                    sources=("car_status",),
                )
            )
        elif capacity is None:
            results.append(
                unavailable(
                    "ers.store_pct",
                    "%",
                    "ers_capacity_not_configured",
                    inputs.now,
                    sources=("car_status", "metrics_config"),
                )
            )
        else:
            results.append(
                _value_metric(
                    "ers.store_pct",
                    store / capacity * 100,
                    "%",
                    inputs,
                    freshness,
                )
            )
        results.append(
            _state_metric(
                "ers.deploy_mode",
                deploy_mode,
                "mode",
                inputs,
                freshness,
                reason="ers_deploy_mode_unavailable",
            )
        )

        records = tuple(
            record
            for record in inputs.history.records_for(inputs.car_index)
            if record.closed
            and not {"incomplete", "partial_lap", "telemetry_partial", "retired"} & record.flags
            and "invalid" not in classify_lap(record, inputs.config)
        )
        latest = records[-1] if records else None
        harvested = latest.ers_harvested_j if latest is not None else None
        deployed = latest.ers_deployed_j if latest is not None else None
        history_freshness = _freshness(inputs, "car_status")
        history_stale = history_freshness.is_stale(inputs.now)

        results.extend(
            (
                _history_metric(
                    "ers.harvested_last_lap_j",
                    harvested,
                    inputs,
                    history_freshness,
                    "ers_lap_history_unavailable",
                ),
                _history_metric(
                    "ers.deployed_last_lap_j",
                    deployed,
                    inputs,
                    history_freshness,
                    "ers_lap_history_unavailable",
                ),
            )
        )

        if harvested is None or deployed is None:
            results.append(
                unavailable(
                    "ers.net_last_lap_j",
                    "J",
                    "ers_lap_totals_unavailable",
                    inputs.now,
                    window=Window("laps", 1),
                    sample_count=1 if latest is not None else 0,
                    sources=("lap_history",),
                )
            )
        else:
            results.append(
                Metric(
                    key="ers.net_last_lap_j",
                    value=harvested - deployed,
                    unit="J",
                    window=Window("laps", 1),
                    quality=Quality.STALE if history_stale else Quality.OK,
                    sample_count=1,
                    computed_at_monotonic=inputs.now,
                    sources=("lap_history",),
                )
            )

        last_three = records[-3:]
        totals = tuple(
            record.ers_harvested_j - record.ers_deployed_j
            for record in last_three
            if record.ers_harvested_j is not None and record.ers_deployed_j is not None
        )
        if not totals:
            results.append(
                unavailable(
                    "ers.net_mean_3_laps_j",
                    "J",
                    "ers_lap_totals_unavailable",
                    inputs.now,
                    window=Window("laps", 3),
                    sources=("lap_history",),
                )
            )
        else:
            results.append(
                Metric(
                    key="ers.net_mean_3_laps_j",
                    value=sum(totals) / len(totals),
                    unit="J",
                    window=Window("laps", 3),
                    quality=(
                        Quality.STALE
                        if history_stale
                        else Quality.OK
                        if len(totals) == 3
                        else Quality.LOW
                    ),
                    sample_count=len(totals),
                    computed_at_monotonic=inputs.now,
                    sources=("lap_history",),
                    notes=() if len(totals) == 3 else ("fewer_than_3_laps",),
                )
            )
        return tuple(results)


def _freshness(inputs: MetricInputs, group: str) -> PacketFreshness:
    try:
        return inputs.state.freshness_for(group)
    except KeyError:
        return PacketFreshness(group, None, None)


def _state_metric(
    key: str,
    value: float | str | None,
    unit: str,
    inputs: MetricInputs,
    freshness: PacketFreshness,
    *,
    reason: str,
) -> Metric:
    if value is None:
        return unavailable(key, unit, reason, inputs.now, sources=("car_status",))
    return _value_metric(key, value, unit, inputs, freshness)


def _value_metric(
    key: str,
    value: float | str,
    unit: str,
    inputs: MetricInputs,
    freshness: PacketFreshness,
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("instant"),
        quality=(Quality.STALE if freshness.is_stale(inputs.now) else Quality.OK),
        sample_count=1,
        computed_at_monotonic=inputs.now,
        sources=("car_status",),
    )


def _history_metric(
    key: str,
    value: float | None,
    inputs: MetricInputs,
    freshness: PacketFreshness,
    reason: str,
) -> Metric:
    if value is None:
        return unavailable(
            key,
            "J",
            reason,
            inputs.now,
            window=Window("laps", 1),
            sources=("lap_history",),
        )
    return Metric(
        key=key,
        value=value,
        unit="J",
        window=Window("laps", 1),
        quality=Quality.STALE if freshness.is_stale(inputs.now) else Quality.OK,
        sample_count=1,
        computed_at_monotonic=inputs.now,
        sources=("lap_history",),
    )
