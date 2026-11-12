from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.comparability import classify_lap, is_clean
from f1_strategist.metrics.lap_tracker import LapTracker
from f1_strategist.parser import CarDamagePacket, parse_packet
from f1_strategist.race_state import CarDamageSnapshot, CarSnapshot, RaceState, RaceStateSnapshot
from tests.scenarios import ScenarioCar, build_race_timed


def _run_race(
    *,
    cars: tuple[ScenarioCar, ...],
    total_laps: int,
    safety_car_laps: tuple[int, int] | None = None,
    weather_change: tuple[int, str] | None = None,
    gap_ahead_s: Mapping[tuple[int, int], tuple[float, float]] | None = None,
    damage_events: Mapping[tuple[int, int], Mapping[str, int]] | None = None,
    flashback: tuple[int, int] | None = None,
    retirements: dict[int, int] | None = None,
) -> tuple[RaceState, LapTracker]:
    race = RaceState()
    tracker = LapTracker(config=MetricsConfig())
    for packet, now in build_race_timed(
        cars,
        total_laps,
        safety_car_laps=safety_car_laps,
        weather_change=weather_change,
        gap_ahead_s=gap_ahead_s,
        damage_events=damage_events,
        flashback=flashback,
        retirements=retirements,
    ):
        race.apply(packet, now)
        tracker.observe(race.snapshot(), now)
    return race, tracker


def _car(
    *,
    index: int = 0,
    grid_position: int = 1,
    base_lap_time_s: float = 90.0,
    initial_fuel_kg: float = 50.0,
    fuel_per_lap_kg: float = 2.0,
    tyre_wear_per_lap_pct: float = 1.0,
    pit_laps: tuple[int, ...] = (),
    compounds: tuple[str, ...] = ("soft",),
    ers_harvest_per_lap_j: float = 0.0,
    ers_deploy_per_lap_j: float = 0.0,
) -> ScenarioCar:
    return ScenarioCar(
        index=index,
        name="Driver",
        grid_position=grid_position,
        base_lap_time_s=base_lap_time_s,
        initial_fuel_kg=initial_fuel_kg,
        fuel_per_lap_kg=fuel_per_lap_kg,
        tyre_wear_per_lap_pct=tyre_wear_per_lap_pct,
        pit_laps=pit_laps,
        compounds=compounds,
        ers_harvest_per_lap_j=ers_harvest_per_lap_j,
        ers_deploy_per_lap_j=ers_deploy_per_lap_j,
    )


def _snapshot_with_car(
    car: CarSnapshot,
    *,
    uid: int = 42,
    total_laps: int | None = 5,
    ended: bool = False,
    red_flag_periods: int | None = 0,
    track_temperature_c: float | None = None,
    session_type: str | None = None,
    track_length_m: float | None = None,
    freshness_updates: Mapping[str, float] | None = None,
) -> RaceStateSnapshot:
    snapshot = RaceState().snapshot()
    cars = list(snapshot.cars)
    cars[car.car_index] = car
    updates = freshness_updates or {}
    freshness = tuple(
        replace(item, updated_at_monotonic=updates[item.group]) if item.group in updates else item
        for item in snapshot.freshness
    )
    return replace(
        snapshot,
        session=replace(
            snapshot.session,
            uid=uid,
            total_laps=total_laps,
            ended=ended,
            red_flag_periods=red_flag_periods,
            track_temperature_c=track_temperature_c,
            session_type=session_type,
            track_length_m=track_length_m,
        ),
        cars=tuple(cars),
        freshness=freshness,
    )


def _damage_snapshot() -> CarDamageSnapshot:
    packet = parse_packet((Path(__file__).parent / "fixtures" / "car_damage.bin").read_bytes())
    assert isinstance(packet, CarDamagePacket)
    race = RaceState()
    race.apply(packet, 1.0)
    damage = race.snapshot().cars[0].damage
    assert damage is not None
    return damage


def test_closes_completed_laps_once_with_boundary_samples() -> None:
    _, tracker = _run_race(
        cars=(_car(ers_harvest_per_lap_j=150.0, ers_deploy_per_lap_j=60.0),),
        total_laps=3,
    )

    records = tracker.history.snapshot().records_for(0)
    assert [record.lap_number for record in records] == [1, 2, 3]
    assert all(record.closed for record in records)
    assert records[0].flags == frozenset({"first_lap"})
    assert [(record.fuel_start_kg, record.fuel_end_kg) for record in records] == [
        (49.0, 48.0),
        (48.0, 46.0),
        (46.0, 44.0),
    ]
    assert all(record.lap_time_s == 90.0 for record in records)
    assert all(record.lap_time_source == "lap_data" for record in records)
    assert all(record.ers_harvested_j == 150.0 for record in records)
    assert all(record.ers_deployed_j == 60.0 for record in records)
    assert all(record.gap_ahead_start_s is None for record in records)
    assert all(record.gap_ahead_end_s is None for record in records)
    assert all(record.traffic_fraction == 0.0 for record in records)


def test_records_pit_entry_exit_and_pit_times_on_their_laps() -> None:
    _, tracker = _run_race(
        cars=(
            _car(
                pit_laps=(2,),
                compounds=("soft", "medium"),
            ),
        ),
        total_laps=4,
    )

    records = tracker.history.snapshot().records_for(0)
    assert records[1].pit_entry is True
    assert records[1].pit_exit is False
    assert records[1].pit_lane_time_s == 22.0
    assert records[1].pit_stop_time_s == 2.5
    assert records[2].pit_entry is False
    assert records[2].pit_exit is True
    assert records[2].tyre_age_start == 0
    assert records[1].tyre_age_start is not None
    assert records[1].tyre_age_end == records[1].tyre_age_start + 1


@pytest.mark.parametrize("new_tyre_at_boundary", [True, False])
def test_tyre_age_is_correct_when_new_tyre_appears_at_or_after_lap_change(
    new_tyre_at_boundary: bool,
) -> None:
    tracker = LapTracker(config=MetricsConfig())
    freshness = {"lap_data": 1.0, "car_status": 1.0}
    old_tyre = CarSnapshot(
        car_index=0,
        current_lap=2,
        current_lap_time_ms=90_000,
        lap_distance_m=5_000.0,
        fuel_kg=40.0,
        actual_tyre_compound="soft",
        tyre_age_laps=2,
        pit_stops=0,
    )
    tracker.observe(_snapshot_with_car(old_tyre, freshness_updates=freshness), 1.0)

    at_boundary = replace(
        old_tyre,
        current_lap=3,
        current_lap_time_ms=1_000,
        lap_distance_m=50.0,
        fuel_kg=38.5,
        actual_tyre_compound="medium" if new_tyre_at_boundary else "soft",
        tyre_age_laps=0 if new_tyre_at_boundary else 2,
        pit_stops=1,
    )
    freshness["lap_data"] = 2.0
    tracker.observe(_snapshot_with_car(at_boundary, freshness_updates=freshness), 2.0)

    if not new_tyre_at_boundary:
        freshness["car_status"] = 3.0
        tracker.observe(
            _snapshot_with_car(
                replace(
                    at_boundary,
                    actual_tyre_compound="medium",
                    tyre_age_laps=0,
                ),
                freshness_updates=freshness,
            ),
            3.0,
        )

    freshness["lap_data"] = 4.0
    tracker.observe(
        _snapshot_with_car(
            replace(
                at_boundary,
                current_lap=4,
                current_lap_time_ms=1_000,
                lap_distance_m=50.0,
                fuel_kg=37.0,
                actual_tyre_compound="medium",
                tyre_age_laps=0,
            ),
            freshness_updates=freshness,
        ),
        4.0,
    )

    in_lap = tracker.history.get(0, 2)
    out_lap = tracker.history.get(0, 3)
    assert in_lap is not None
    assert out_lap is not None
    assert in_lap.tyre_age_start == 2
    assert in_lap.tyre_age_start is not None
    assert in_lap.tyre_age_end == 3
    assert out_lap.tyre_age_start == 0
    assert out_lap.compound == "medium"


def test_records_safety_car_status_seen_during_a_lap() -> None:
    _, tracker = _run_race(cars=(_car(),), total_laps=4, safety_car_laps=(2, 3))

    records = tracker.history.snapshot().records_for(0)
    assert "full_safety_car" in records[1].track_status_seen
    assert "full_safety_car" in records[2].track_status_seen
    assert "full_safety_car" not in records[3].track_status_seen


def test_records_weather_seen_during_a_lap() -> None:
    _, tracker = _run_race(
        cars=(_car(),),
        total_laps=4,
        weather_change=(3, "light_rain"),
    )

    records = tracker.history.snapshot().records_for(0)
    assert "clear" in records[1].weather_seen
    assert "light_rain" in records[2].weather_seen
    assert "light_rain" in records[3].weather_seen


def test_records_largest_damage_change_during_a_lap() -> None:
    _, tracker = _run_race(
        cars=(_car(),),
        total_laps=3,
        damage_events={(0, 2): {"front_left_wing_damage": 10}},
    )

    records = tracker.history.snapshot().records_for(0)
    assert records[0].damage_max_delta_pct == 0.0
    assert records[1].damage_max_delta_pct == 10.0
    assert records[2].damage_max_delta_pct == 0.0


def test_traffic_fraction_uses_distinct_gap_samples() -> None:
    _, tracker = _run_race(
        cars=(
            _car(),
            _car(index=1, grid_position=2, base_lap_time_s=90.5),
        ),
        total_laps=2,
        gap_ahead_s={(1, 1): (0.25, 1.25)},
    )

    records = tracker.history.snapshot().records_for(1)
    assert records[0].gap_ahead_start_s == 0.75
    assert records[0].gap_ahead_end_s == 1.25
    assert records[0].traffic_fraction == 0.5


def test_flashback_discards_future_laps_and_marks_replayed_lap() -> None:
    _, tracker = _run_race(cars=(_car(),), total_laps=4, flashback=(3, 2))

    records = tracker.history.snapshot().records_for(0)
    assert [record.lap_number for record in records] == [1, 2, 3, 4]
    assert "flashback" in records[1].flags
    assert "flashback" not in records[2].flags


def test_retired_lap_is_closed_only_once() -> None:
    race = RaceState()
    tracker = LapTracker(config=MetricsConfig())
    retired_state: RaceStateSnapshot | None = None
    retired_at: float | None = None
    for packet, now in build_race_timed((_car(),), 4, retirements={0: 2}):
        race.apply(packet, now)
        snapshot = race.snapshot()
        tracker.observe(snapshot, now)
        if snapshot.cars[0].result_status == "retired" and retired_state is None:
            retired_state = snapshot
            retired_at = now

    assert retired_state is not None
    assert retired_at is not None
    record = tracker.history.get(0, 2)
    assert record is not None
    assert record.closed is True
    assert "retired" in record.flags

    tracker.observe(retired_state, retired_at + 10.0)

    assert tracker.history.get(0, 2) == record
    assert [item.lap_number for item in tracker.history.snapshot().records_for(0)] == [1, 2]


def test_session_change_resets_history() -> None:
    race, tracker = _run_race(cars=(_car(),), total_laps=2)
    before = race.snapshot()
    assert before.session.uid is not None
    assert tracker.history.snapshot().records_for(0)

    changed_session = replace(before, session=replace(before.session, uid=before.session.uid + 1))
    tracker.observe(changed_session, 999.0)

    assert tracker.history.session_uid == changed_session.session.uid
    assert tracker.history.snapshot().records_for(0) == ()


def test_connection_mid_lap_marks_partial_lap_only() -> None:
    tracker = LapTracker(config=MetricsConfig())
    freshness = {"lap_data": 1.0}
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=2,
                current_lap_time_ms=4_000,
                lap_distance_m=100.0,
                fuel_kg=40.0,
            ),
            freshness_updates=freshness,
        ),
        1.0,
    )
    freshness["lap_data"] = 2.0
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=3,
                current_lap_time_ms=1_000,
                lap_distance_m=20.0,
                fuel_kg=38.5,
            ),
            freshness_updates=freshness,
        ),
        2.0,
    )
    partial = tracker.history.get(0, 2)
    assert partial is not None
    assert "partial_lap" in partial.flags
    assert not is_clean(classify_lap(partial, tracker.config))

    freshness["lap_data"] = 3.0
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=4,
                current_lap_time_ms=1_000,
                lap_distance_m=20.0,
                fuel_kg=37.0,
            ),
            freshness_updates=freshness,
        ),
        3.0,
    )
    complete = tracker.history.get(0, 3)
    assert complete is not None
    assert "partial_lap" not in complete.flags


@pytest.mark.parametrize(
    ("lap_time_ms", "distance_m", "is_partial"),
    [(3_000, 50.0, False), (3_001, 50.0, True), (1_000, 50.1, True)],
)
def test_partial_lap_thresholds(
    lap_time_ms: int,
    distance_m: float,
    is_partial: bool,
) -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=2,
                current_lap_time_ms=lap_time_ms,
                lap_distance_m=distance_m,
            ),
            freshness_updates={"lap_data": 1.0},
        ),
        1.0,
    )

    open_lap = tracker._open_laps[0]
    assert ("partial_lap" in open_lap.flags) is is_partial


def test_gradual_tyre_and_component_wear_is_not_counted_as_damage() -> None:
    tracker = LapTracker(config=MetricsConfig())
    baseline = _damage_snapshot()
    gradual_wear = replace(
        baseline,
        tyre_blisters=tuple(value + 20 for value in baseline.tyre_blisters),
        brakes_damage=tuple(value + 20 for value in baseline.brakes_damage),
        engine_mguh_wear=min(100, baseline.engine_mguh_wear + 20),
        engine_es_wear=min(100, baseline.engine_es_wear + 20),
        engine_ce_wear=min(100, baseline.engine_ce_wear + 20),
        engine_ice_wear=min(100, baseline.engine_ice_wear + 20),
        engine_mguk_wear=min(100, baseline.engine_mguk_wear + 20),
        engine_tc_wear=min(100, baseline.engine_tc_wear + 20),
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=2, damage=baseline),
            freshness_updates={"lap_data": 1.0, "car_damage": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=2,
                current_lap_time_ms=50_000,
                damage=gradual_wear,
            ),
            freshness_updates={"lap_data": 2.0, "car_damage": 2.0},
        ),
        2.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=3, current_lap_time_ms=1_000, damage=gradual_wear),
            freshness_updates={"lap_data": 3.0, "car_damage": 3.0},
        ),
        3.0,
    )

    record = tracker.history.get(0, 2)
    assert record is not None
    assert record.damage_max_delta_pct == 0.0
    assert "damage_change" not in classify_lap(record, tracker.config)


def test_session_end_marks_laps_incomplete_and_timed_sessions_do_not_close() -> None:
    tracker = LapTracker(config=MetricsConfig())
    car = CarSnapshot(car_index=0, current_lap=4, current_lap_time_ms=30_000, fuel_kg=30.0)
    snapshot = _snapshot_with_car(
        car,
        total_laps=5,
        ended=False,
        freshness_updates={"lap_data": 1.0},
    )
    tracker.observe(snapshot, 1.0)
    ended = replace(snapshot, session=replace(snapshot.session, ended=True))
    tracker.observe(ended, 2.0)

    incomplete = tracker.history.get(0, 4)
    assert incomplete is not None
    assert "incomplete" in classify_lap(incomplete, tracker.config)
    assert not is_clean(classify_lap(incomplete, tracker.config))

    timed_tracker = LapTracker(config=MetricsConfig())
    timed_state = _snapshot_with_car(
        car,
        total_laps=None,
        ended=False,
        freshness_updates={"lap_data": 1.0},
    )
    timed_tracker.observe(timed_state, 1.0)
    timed_tracker.observe(
        replace(timed_state, session=replace(timed_state.session, ended=True)),
        2.0,
    )
    assert timed_tracker.history.snapshot().records_for(0) == ()


def test_in_lap_flashback_marks_lap_and_resets_traffic_damage_and_ers() -> None:
    tracker = LapTracker(config=MetricsConfig())
    damage = _damage_snapshot()
    impact_damage = replace(
        damage,
        front_left_wing_damage=min(100, damage.front_left_wing_damage + 10),
    )
    base_car = CarSnapshot(
        car_index=0,
        position=2,
        current_lap=2,
        current_lap_time_ms=20_000,
        lap_distance_m=1_000.0,
        delta_to_car_in_front_ms=100,
        ers_harvested_mguk_j=800.0,
        ers_deployed_this_lap_j=700.0,
        damage=damage,
    )
    tracker.observe(
        _snapshot_with_car(
            base_car,
            freshness_updates={"lap_data": 1.0, "car_damage": 1.0, "car_status": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            replace(
                base_car,
                current_lap_time_ms=25_000,
                lap_distance_m=1_500.0,
                damage=impact_damage,
            ),
            freshness_updates={"lap_data": 2.0, "car_damage": 2.0, "car_status": 2.0},
        ),
        2.0,
    )
    tracker.observe(
        _snapshot_with_car(
            replace(
                base_car,
                current_lap_time_ms=23_000,
                lap_distance_m=1_400.0,
                delta_to_car_in_front_ms=2_000,
                ers_harvested_mguk_j=100.0,
                ers_deployed_this_lap_j=80.0,
                damage=impact_damage,
            ),
            freshness_updates={"lap_data": 3.0, "car_damage": 3.0, "car_status": 3.0},
        ),
        3.0,
    )
    tracker.observe(
        _snapshot_with_car(
            replace(
                base_car,
                current_lap_time_ms=30_000,
                lap_distance_m=2_000.0,
                delta_to_car_in_front_ms=2_000,
                ers_harvested_mguk_j=200.0,
                ers_deployed_this_lap_j=150.0,
                damage=impact_damage,
            ),
            freshness_updates={"lap_data": 4.0, "car_damage": 4.0, "car_status": 4.0},
        ),
        4.0,
    )
    tracker.observe(
        _snapshot_with_car(
            replace(
                base_car,
                current_lap=3,
                current_lap_time_ms=1_000,
                lap_distance_m=20.0,
                delta_to_car_in_front_ms=2_000,
                ers_harvested_mguk_j=0.0,
                ers_deployed_this_lap_j=0.0,
                damage=impact_damage,
            ),
            freshness_updates={"lap_data": 5.0, "car_damage": 5.0, "car_status": 5.0},
        ),
        5.0,
    )

    record = tracker.history.get(0, 2)
    assert record is not None
    assert "flashback" in record.flags
    assert record.traffic_fraction == 0.0
    assert record.ers_harvested_j == 200.0
    assert record.ers_deployed_j == 150.0
    assert record.damage_max_delta_pct == 0.0


@pytest.mark.parametrize(
    ("session_type", "expected"),
    [
        ("practice", frozenset()),
        ("race", frozenset({"first_lap"})),
    ],
)
def test_first_lap_only_marks_the_start_of_a_race(
    session_type: str,
    expected: frozenset[str],
) -> None:
    tracker = LapTracker(config=MetricsConfig())
    car = CarSnapshot(
        car_index=0,
        current_lap=1,
        current_lap_time_ms=10_000,
        lap_distance_m=1_000.0,
    )
    tracker.observe(
        _snapshot_with_car(
            car,
            session_type=session_type,
            freshness_updates={"lap_data": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            replace(
                car,
                current_lap=2,
                current_lap_time_ms=None,
                last_lap_time_ms=90_000,
                lap_distance_m=10.0,
            ),
            session_type=session_type,
            freshness_updates={"lap_data": 2.0},
        ),
        2.0,
    )

    record = tracker.history.get(0, 1)
    assert record is not None
    assert record.flags == expected


def test_start_line_crossing_keeps_the_out_lap_without_flashback() -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=6,
                current_lap_time_ms=60_000,
                lap_distance_m=5_000.0,
                pit_status="in_pit_area",
                tyre_age_laps=5,
                actual_tyre_compound="soft",
            ),
            session_type="practice",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 1.0, "car_status": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=7,
                lap_distance_m=3.0,
                tyre_age_laps=0,
                actual_tyre_compound="hard",
            ),
            session_type="practice",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 2.0, "car_status": 2.0},
        ),
        2.0,
    )
    out_lap = tracker._open_laps[0]
    assert out_lap.pit_exit is True

    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=7,
                current_lap_time_ms=40_000,
                sector1_time_ms=10_000,
                lap_distance_m=5_809.0,
                pit_lane_time_ms=25_000,
                pit_stop_timer_ms=2_500,
                tyre_age_laps=0,
                actual_tyre_compound="hard",
            ),
            session_type="practice",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 3.0, "car_status": 3.0},
        ),
        3.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=7,
                lap_distance_m=3.0,
                tyre_age_laps=0,
                actual_tyre_compound="hard",
            ),
            session_type="practice",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 4.0, "car_status": 4.0},
        ),
        4.0,
    )
    timed_lap = tracker._open_laps[0]
    assert timed_lap.pit_exit is True
    assert "flashback" not in timed_lap.flags

    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=7,
                current_lap_time_ms=80_000,
                sector1_time_ms=30_000,
                lap_distance_m=4_000.0,
                tyre_age_laps=0,
                actual_tyre_compound="hard",
            ),
            session_type="practice",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 5.0, "car_status": 5.0},
        ),
        5.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=8,
                lap_distance_m=10.0,
                last_lap_time_ms=96_099,
                tyre_age_laps=1,
                actual_tyre_compound="hard",
            ),
            session_type="practice",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 6.0, "car_status": 6.0},
        ),
        6.0,
    )

    record = tracker.history.get(0, 7)
    assert record is not None
    assert record.lap_time_s == 96.099
    assert record.pit_exit is True
    assert record.pit_entry is False
    assert record.pit_lane_time_s == 25.0
    assert record.pit_stop_time_s == 2.5
    assert "flashback" not in record.flags
    assert "first_lap" not in record.flags
    assert "out_lap" in classify_lap(record, MetricsConfig())


def test_normal_out_lap_with_a_new_lap_number_keeps_out_lap() -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=6,
                current_lap_time_ms=60_000,
                lap_distance_m=5_000.0,
                pit_status="in_pit_area",
                tyre_age_laps=5,
                actual_tyre_compound="soft",
            ),
            session_type="practice",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 1.0, "car_status": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=7,
                current_lap_time_ms=10_000,
                lap_distance_m=100.0,
                tyre_age_laps=0,
                actual_tyre_compound="hard",
            ),
            session_type="practice",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 2.0, "car_status": 2.0},
        ),
        2.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=8,
                lap_distance_m=10.0,
                last_lap_time_ms=90_000,
                tyre_age_laps=1,
                actual_tyre_compound="hard",
            ),
            session_type="practice",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 3.0, "car_status": 3.0},
        ),
        3.0,
    )

    record = tracker.history.get(0, 7)
    assert record is not None
    assert record.pit_exit is True
    assert "out_lap" in classify_lap(record, MetricsConfig())


def test_red_flag_restart_lap_is_marked() -> None:
    tracker = LapTracker(config=MetricsConfig())
    car = CarSnapshot(car_index=0, current_lap=2, current_lap_time_ms=20_000, lap_distance_m=1_000)
    tracker.observe(
        _snapshot_with_car(
            car,
            freshness_updates={"lap_data": 1.0, "session": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            car,
            red_flag_periods=1,
            freshness_updates={"lap_data": 1.0, "session": 2.0},
        ),
        2.0,
    )
    tracker.observe(
        _snapshot_with_car(
            replace(car, current_lap=3, current_lap_time_ms=1_000, lap_distance_m=20),
            red_flag_periods=1,
            freshness_updates={"lap_data": 2.0, "session": 2.0},
        ),
        3.0,
    )
    tracker.observe(
        _snapshot_with_car(
            replace(car, current_lap=4, current_lap_time_ms=1_000, lap_distance_m=20),
            red_flag_periods=1,
            freshness_updates={"lap_data": 3.0, "session": 2.0},
        ),
        4.0,
    )

    restart_lap = tracker.history.get(0, 3)
    assert restart_lap is not None
    assert "red_flag" in restart_lap.track_status_seen


def test_observe_skips_car_work_when_freshness_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = LapTracker(config=MetricsConfig())
    state = _snapshot_with_car(
        CarSnapshot(car_index=0, current_lap=2, current_lap_time_ms=10_000),
        freshness_updates={"lap_data": 1.0},
    )
    tracker.observe(state, 1.0)

    def fail_if_processed(*args: object, **kwargs: object) -> None:
        pytest.fail("No debe procesar coches si la frescura no cambió")

    monkeypatch.setattr(tracker, "_update_lap", fail_if_processed)
    tracker.observe(state, 2.0)


def test_missing_samples_remain_none_in_closed_record() -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=1),
            total_laps=2,
            freshness_updates={"lap_data": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=2),
            total_laps=2,
            freshness_updates={"lap_data": 2.0},
        ),
        2.0,
    )

    record = tracker.history.get(0, 1)
    assert record is not None
    assert record.fuel_start_kg is None
    assert record.fuel_end_kg is None
    assert record.tyre_wear_start_pct is None
    assert record.tyre_wear_end_pct is None
    assert record.position_start is None
    assert record.position_end is None
    assert record.gap_ahead_start_s is None
    assert record.gap_ahead_end_s is None
    assert record.ers_harvested_j is None
    assert record.ers_deployed_j is None
    assert record.traffic_fraction is None
    assert record.damage_max_delta_pct is None


def test_closed_lap_records_mean_track_temperature() -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=2, current_lap_time_ms=10_000),
            track_temperature_c=30.0,
            freshness_updates={"lap_data": 1.0, "session": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=2, current_lap_time_ms=20_000),
            track_temperature_c=32.0,
            freshness_updates={"lap_data": 2.0, "session": 2.0},
        ),
        2.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=3, current_lap_time_ms=1_000),
            track_temperature_c=34.0,
            freshness_updates={"lap_data": 3.0, "session": 3.0},
        ),
        3.0,
    )

    record = tracker.history.get(0, 2)

    assert record is not None
    assert record.track_temperature_c == 31.0


def test_track_temperature_is_sampled_once_per_fresh_session_update() -> None:
    tracker = LapTracker(config=MetricsConfig())
    for step in range(10):
        tracker.observe(
            _snapshot_with_car(
                CarSnapshot(
                    car_index=0,
                    current_lap=2 if step < 9 else 3,
                    current_lap_time_ms=10_000 + step * 1000,
                ),
                track_temperature_c=30.0,
                freshness_updates={"lap_data": 1.0 + step, "session": 1.0},
            ),
            1.0 + step,
        )

    record = tracker.history.get(0, 2)

    assert record is not None
    assert record.track_temperature_c == 30.0


def test_stale_session_temperature_is_not_added_to_lap_history() -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=2, current_lap_time_ms=10_000),
            track_temperature_c=30.0,
            freshness_updates={"lap_data": 1.0, "session": 1.0},
        ),
        10.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=3, current_lap_time_ms=1_000),
            track_temperature_c=32.0,
            freshness_updates={"lap_data": 11.0, "session": 1.0},
        ),
        11.0,
    )

    record = tracker.history.get(0, 2)

    assert record is not None
    assert record.track_temperature_c is None


def test_rewind_without_a_pit_exit_is_a_flashback() -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=6,
                current_lap_time_ms=85_000,
                lap_distance_m=5_700.0,
                tyre_age_laps=5,
                actual_tyre_compound="soft",
            ),
            session_type="race",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 1.0, "car_status": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=6,
                current_lap_time_ms=1_000,
                lap_distance_m=20.0,
                tyre_age_laps=5,
                actual_tyre_compound="soft",
            ),
            session_type="race",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 2.0, "car_status": 2.0},
        ),
        2.0,
    )

    open_lap = tracker._open_laps[0]
    assert open_lap.pit_exit is False
    assert "flashback" in open_lap.flags


@pytest.mark.parametrize("race_type", ["race", "race_2", "race_3"])
def test_first_lap_is_marked_for_every_race_type(race_type: str) -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=1, lap_distance_m=10.0, tyre_age_laps=0),
            session_type=race_type,
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 1.0, "car_status": 1.0},
        ),
        1.0,
    )

    assert "first_lap" in tracker._open_laps[0].flags


def test_first_lap_is_reconciled_when_the_session_type_arrives_late() -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=1, lap_distance_m=10.0, tyre_age_laps=0),
            session_type=None,
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 1.0, "car_status": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(car_index=0, current_lap=1, lap_distance_m=800.0, tyre_age_laps=0),
            session_type="race",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 2.0, "car_status": 2.0},
        ),
        2.0,
    )

    assert "first_lap" in tracker._open_laps[0].flags


def test_lap_one_is_marked_before_closing_when_race_type_arrives_on_lap_two() -> None:
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=1,
                lap_distance_m=10.0,
                current_lap_time_ms=10_000,
                tyre_age_laps=0,
            ),
            session_type=None,
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 1.0, "car_status": 1.0},
        ),
        1.0,
    )
    tracker.observe(
        _snapshot_with_car(
            CarSnapshot(
                car_index=0,
                current_lap=2,
                lap_distance_m=10.0,
                last_lap_time_ms=90_000,
                tyre_age_laps=1,
            ),
            session_type="race",
            track_length_m=5_809.0,
            freshness_updates={"lap_data": 2.0, "car_status": 2.0},
        ),
        2.0,
    )

    record = tracker.history.get(0, 1)
    assert record is not None
    assert "first_lap" in record.flags
