from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.lap_tracker import LapTracker
from f1_strategist.race_state import RaceState, RaceStateSnapshot
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


def test_missing_samples_remain_none_in_closed_record() -> None:
    initial = RaceState().snapshot()
    session = replace(initial.session, uid=42, total_laps=2)
    cars = list(initial.cars)
    cars[0] = replace(cars[0], current_lap=1)
    tracker = LapTracker(config=MetricsConfig())
    tracker.observe(replace(initial, session=session, cars=tuple(cars)), 1.0)

    cars[0] = replace(cars[0], current_lap=2)
    tracker.observe(replace(initial, session=session, cars=tuple(cars)), 2.0)

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
