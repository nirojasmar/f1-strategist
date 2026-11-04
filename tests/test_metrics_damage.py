from __future__ import annotations

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.damage import DamageMetrics
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import (
    CarDamageSnapshot,
    CarSnapshot,
    PacketFreshness,
    RaceSessionSnapshot,
    RaceStateSnapshot,
)


def _damage(**overrides: object) -> CarDamageSnapshot:
    values: dict[str, object] = {
        "tyres_wear": (1.0, 1.0, 1.0, 1.0),
        "tyres_damage": (0, 0, 0, 0),
        "brakes_damage": (0, 0, 0, 0),
        "tyre_blisters": (0, 0, 0, 0),
        "front_left_wing_damage": 0,
        "front_right_wing_damage": 0,
        "rear_wing_damage": 0,
        "floor_damage": 0,
        "diffuser_damage": 0,
        "sidepod_damage": 0,
        "drs_fault": "ok",
        "ers_fault": "ok",
        "gearbox_damage": 0,
        "engine_damage": 0,
        "engine_mguh_wear": 0,
        "engine_es_wear": 0,
        "engine_ce_wear": 0,
        "engine_ice_wear": 0,
        "engine_mguk_wear": 0,
        "engine_tc_wear": 0,
        "engine_blown": "ok",
        "engine_seized": "ok",
    }
    values.update(overrides)
    return CarDamageSnapshot(**values)  # type: ignore[arg-type]


def _inputs(
    *,
    damage: CarDamageSnapshot | None = None,
    records: tuple[LapRecord, ...] = (),
    stale: bool = False,
    threshold: float = 5.0,
) -> MetricInputs:
    now = 50.0
    state = RaceStateSnapshot(
        session=RaceSessionSnapshot(1, "race", "track", 10, True, False),
        current_lap=3,
        player_car_index=0,
        cars=(CarSnapshot(car_index=0, damage=damage),),
        freshness=(
            PacketFreshness("car_damage", now - (5.0 if stale else 0.1), 2.0),
            PacketFreshness("lap_data", now - (5.0 if stale else 0.1), 2.0),
        ),
        events=(),
    )
    return MetricInputs(
        state=state,
        history=LapHistorySnapshot(records={0: records} if records else {}),
        config=MetricsConfig(damage_change_threshold_pct=threshold),
        now=now,
        car_index=0,
    )


def test_damage_groups_faults_and_changed_laps() -> None:
    damage = _damage(
        front_left_wing_damage=12,
        front_right_wing_damage=8,
        rear_wing_damage=15,
        floor_damage=6,
        diffuser_damage=90,
        sidepod_damage=80,
        engine_damage=3,
        engine_mguh_wear=19,
        gearbox_damage=4,
        drs_fault="fault",
        engine_seized="fault",
    )
    records = (
        LapRecord(car_index=0, lap_number=1, damage_max_delta_pct=4.9),
        LapRecord(car_index=0, lap_number=2, damage_max_delta_pct=5.0),
        LapRecord(car_index=0, lap_number=3, damage_max_delta_pct=10.0),
    )

    metrics = {
        metric.key: metric
        for metric in DamageMetrics().compute(_inputs(damage=damage, records=records))
    }

    assert metrics["damage.aero_max_pct"].value == 15
    assert metrics["damage.floor_pct"].value == 6
    assert metrics["damage.engine_max_pct"].value == 19
    assert metrics["damage.gearbox_pct"].value == 4
    assert metrics["damage.faults"].value == ("DRS", "motor")
    assert metrics["damage.changed_laps"].value == 2
    assert metrics["damage.changed_laps"].sample_count == 3
    assert metrics["damage.performance_impact"].quality is Quality.UNAVAILABLE
    assert metrics["damage.performance_impact"].notes == (
        "performance_impact_not_estimated_in_phase_2",
    )


def test_no_faults_and_no_damage_snapshot_are_reported_without_inventing_values() -> None:
    no_faults = {
        metric.key: metric for metric in DamageMetrics().compute(_inputs(damage=_damage()))
    }
    assert no_faults["damage.faults"].value == ()

    missing = {metric.key: metric for metric in DamageMetrics().compute(_inputs())}
    assert missing["damage.aero_max_pct"].quality is Quality.UNAVAILABLE
    assert missing["damage.changed_laps"].quality is Quality.INSUFFICIENT
    assert missing["damage.performance_impact"].value is None


def test_stale_damage_snapshot_is_marked_stale() -> None:
    metrics = {
        metric.key: metric
        for metric in DamageMetrics().compute(_inputs(damage=_damage(), stale=True))
    }

    assert metrics["damage.aero_max_pct"].quality is Quality.STALE
    assert metrics["damage.faults"].quality is Quality.STALE


def test_zero_damage_is_not_counted_as_changed_when_threshold_is_zero() -> None:
    inputs = _inputs(
        records=(LapRecord(car_index=0, lap_number=1, damage_max_delta_pct=0.0),),
        threshold=0.0,
    )

    metric = {item.key: item for item in DamageMetrics().compute(inputs)}["damage.changed_laps"]

    assert metric.value == 0
