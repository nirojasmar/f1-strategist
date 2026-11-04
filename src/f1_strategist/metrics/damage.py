"""Damage-state metrics; performance impact is intentionally not estimated."""

from __future__ import annotations

from ..race_state import CarDamageSnapshot, PacketFreshness
from .history import MetricInputs
from .types import Metric, Quality, Window, insufficient, unavailable


class DamageMetrics:
    """Summarize available damage, faults, and historical damage changes."""

    name = "damage"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        car = inputs.car
        damage = car.damage if car is not None else None
        freshness = _freshness(inputs)
        stale = freshness.is_stale(inputs.now)
        metrics: list[Metric] = []

        if damage is None:
            for key, unit in (
                ("damage.aero_max_pct", "%"),
                ("damage.floor_pct", "%"),
                ("damage.engine_max_pct", "%"),
                ("damage.gearbox_pct", "%"),
                ("damage.faults", "faults"),
            ):
                metrics.append(
                    unavailable(
                        key,
                        unit,
                        "car_damage_unavailable",
                        inputs.now,
                        sources=("car_damage",),
                    )
                )
        else:
            metrics.extend(
                (
                    _damage_value(
                        "damage.aero_max_pct",
                        max(
                            damage.front_left_wing_damage,
                            damage.front_right_wing_damage,
                            damage.rear_wing_damage,
                        ),
                        inputs,
                        freshness,
                    ),
                    _damage_value("damage.floor_pct", damage.floor_damage, inputs, freshness),
                    _damage_value(
                        "damage.engine_max_pct",
                        max(
                            damage.engine_damage,
                            damage.engine_mguh_wear,
                            damage.engine_es_wear,
                            damage.engine_ce_wear,
                            damage.engine_ice_wear,
                            damage.engine_mguk_wear,
                            damage.engine_tc_wear,
                        ),
                        inputs,
                        freshness,
                    ),
                    _damage_value(
                        "damage.gearbox_pct",
                        damage.gearbox_damage,
                        inputs,
                        freshness,
                    ),
                    Metric(
                        key="damage.faults",
                        value=_faults(damage),
                        unit="faults",
                        window=Window("instant"),
                        quality=Quality.STALE if stale else Quality.OK,
                        sample_count=1,
                        computed_at_monotonic=inputs.now,
                        sources=("car_damage",),
                    ),
                )
            )

        records = inputs.history.records.get(inputs.car_index, ())
        observed_changes = tuple(
            record for record in records if record.damage_max_delta_pct is not None
        )
        if not observed_changes:
            metrics.append(
                insufficient(
                    "damage.changed_laps",
                    "laps",
                    "damage_history_insufficient",
                    inputs.now,
                    window=Window("session"),
                    sample_count=len(records),
                    sources=("lap_history",),
                )
            )
        else:
            changed_count = sum(
                1
                for record in observed_changes
                if record.damage_max_delta_pct is not None
                and record.damage_max_delta_pct > 0
                and record.damage_max_delta_pct >= inputs.config.damage_change_threshold_pct
            )
            metrics.append(
                Metric(
                    key="damage.changed_laps",
                    value=changed_count,
                    unit="laps",
                    window=Window("session"),
                    quality=(Quality.STALE if _history_stale(inputs) else Quality.OK),
                    sample_count=len(observed_changes),
                    computed_at_monotonic=inputs.now,
                    sources=("lap_history", "metrics_config"),
                )
            )

        metrics.append(
            unavailable(
                "damage.performance_impact",
                "s",
                "performance_impact_not_estimated_in_phase_2",
                inputs.now,
                sources=("not_estimated",),
            )
        )
        return tuple(metrics)


def _freshness(inputs: MetricInputs) -> PacketFreshness:
    try:
        return inputs.state.freshness_for("car_damage")
    except KeyError:
        return PacketFreshness("car_damage", None, None)


def _history_stale(inputs: MetricInputs) -> bool:
    try:
        return inputs.state.freshness_for("car_damage").is_stale(inputs.now)
    except KeyError:
        return False


def _damage_value(
    key: str,
    value: int,
    inputs: MetricInputs,
    freshness: PacketFreshness,
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit="%",
        window=Window("instant"),
        quality=Quality.STALE if freshness.is_stale(inputs.now) else Quality.OK,
        sample_count=1,
        computed_at_monotonic=inputs.now,
        sources=("car_damage",),
    )


def _faults(damage: CarDamageSnapshot) -> tuple[str, ...]:
    faults: list[str] = []
    if damage.drs_fault == "fault":
        faults.append("DRS")
    if damage.ers_fault == "fault":
        faults.append("ERS")
    if damage.engine_blown == "fault" or damage.engine_seized == "fault":
        faults.append("motor")
    return tuple(faults)
