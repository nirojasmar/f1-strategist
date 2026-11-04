from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.history import (
    LapHistory,
    LapHistorySnapshot,
    LapRecord,
    MetricInputs,
    MetricModule,
    TyreStint,
)
from f1_strategist.metrics.registry import METRIC_MODULES
from f1_strategist.metrics.types import Metric
from f1_strategist.race_state import RaceState


def _record(
    lap_number: int,
    lap_time_s: float = 90.0,
    *,
    car_index: int = 0,
) -> LapRecord:
    return LapRecord(
        car_index=car_index,
        lap_number=lap_number,
        lap_time_s=lap_time_s,
        lap_time_source="session_history",
    )


def test_lap_record_is_frozen_and_normalizes_collections() -> None:
    record = LapRecord(
        car_index=0,
        lap_number=3,
        lap_time_s=90.5,
        lap_time_source="lap_data",
        track_status_seen=frozenset({"green", "yellow"}),
        weather_seen=frozenset({"clear"}),
        tyre_wear_start_pct=(10.0, 11.0, 12.0, 13.0),
        tyre_wear_end_pct=(20.0, 21.0, 22.0, 23.0),
        flags=frozenset({"traffic"}),
    )

    assert record.track_status_seen == frozenset({"green", "yellow"})
    assert record.weather_seen == frozenset({"clear"})
    assert record.tyre_wear_start_pct == (10.0, 11.0, 12.0, 13.0)
    assert record.tyre_wear_end_pct == (20.0, 21.0, 22.0, 23.0)
    assert record.flags == frozenset({"traffic"})

    with pytest.raises(FrozenInstanceError):
        record.lap_number = 4  # type: ignore[misc]


def test_lap_record_rejects_invalid_identifiers() -> None:
    with pytest.raises(ValueError):
        LapRecord(car_index=-1, lap_number=1)
    with pytest.raises(ValueError):
        LapRecord(car_index=0, lap_number=0)


def test_upsert_replaces_the_record_for_the_same_lap() -> None:
    history = LapHistory(history_max_laps=5)
    original = _record(2, 90.0)
    replacement = _record(2, 88.5)

    history.upsert(original)
    history.upsert(replacement)

    assert history.get(0, 2) == replacement
    assert len(history.snapshot().records_for(0)) == 1


def test_limit_discards_the_oldest_laps_per_car() -> None:
    history = LapHistory(history_max_laps=2)
    history.upsert(_record(3))
    history.upsert(_record(1))
    history.upsert(_record(2))

    assert [record.lap_number for record in history.snapshot().records_for(0)] == [2, 3]
    assert history.get(0, 1) is None

    history.upsert(_record(4))

    assert [record.lap_number for record in history.snapshot().records_for(0)] == [3, 4]


def test_limit_is_applied_independently_per_car() -> None:
    history = LapHistory(history_max_laps=1)
    history.upsert(_record(1, car_index=0))
    history.upsert(_record(1, car_index=1))
    history.upsert(_record(2, car_index=0))

    snapshot = history.snapshot()
    assert [record.lap_number for record in snapshot.records_for(0)] == [2]
    assert [record.lap_number for record in snapshot.records_for(1)] == [1]


def test_history_accepts_a_metrics_config_for_its_limit() -> None:
    history = LapHistory(MetricsConfig(history_max_laps=1))
    history.upsert(_record(1))
    history.upsert(_record(2))

    assert history.history_max_laps == 1
    assert [record.lap_number for record in history.snapshot().records_for(0)] == [2]

    with pytest.raises(ValueError):
        LapHistory(0)


def test_snapshot_does_not_share_mutable_state_with_history() -> None:
    history = LapHistory(history_max_laps=5)
    history.upsert(_record(1))
    history.replace_tyre_stints(0, (TyreStint("soft", 1, 1),))

    snapshot = history.snapshot()
    history.upsert(_record(2))
    history.replace_tyre_stints(0, (TyreStint("medium", 2, 2),))

    assert [record.lap_number for record in snapshot.records_for(0)] == [1]
    assert snapshot.stints_for(0) == (TyreStint("soft", 1, 1),)

    with pytest.raises(TypeError):
        snapshot.records[0] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.tyre_stints[0] = ()  # type: ignore[index]


def test_reset_clears_records_stints_and_updates_the_session() -> None:
    history = LapHistory(history_max_laps=5)
    history.upsert(_record(1))
    history.replace_tyre_stints(0, (TyreStint("soft", 1, 1),))

    history.reset(42)

    snapshot = history.snapshot()
    assert history.session_uid == 42
    assert snapshot.session_uid == 42
    assert dict(snapshot.records) == {}
    assert dict(snapshot.tyre_stints) == {}
    assert history.get(0, 1) is None


def test_discard_after_removes_later_laps_for_one_car() -> None:
    history = LapHistory(history_max_laps=5)
    history.upsert(_record(1, car_index=0))
    history.upsert(_record(2, car_index=0))
    history.upsert(_record(3, car_index=0))
    history.upsert(_record(3, car_index=1))

    history.discard_after(0, 1)

    snapshot = history.snapshot()
    assert [record.lap_number for record in snapshot.records_for(0)] == [1]
    assert [record.lap_number for record in snapshot.records_for(1)] == [3]


def test_tyre_stints_are_replaced_and_copied_per_car() -> None:
    history = LapHistory(history_max_laps=5)
    stints = (TyreStint("soft", 1, 5), TyreStint("medium", 6))

    history.replace_tyre_stints(3, stints)
    snapshot = history.snapshot()

    assert snapshot.stints_for(3) == stints
    assert snapshot.stints_for(2) == ()
    assert snapshot.tyre_stints == {3: stints}


def test_metric_inputs_expose_the_requested_car() -> None:
    state_snapshot = RaceState().snapshot()
    inputs = MetricInputs(
        state=state_snapshot,
        history=LapHistorySnapshot(),
        config=MetricsConfig(),
        now=1.5,
        car_index=0,
    )

    assert inputs.car == state_snapshot.cars[0]
    assert (
        MetricInputs(
            state=state_snapshot,
            history=inputs.history,
            config=inputs.config,
            now=2.0,
            car_index=99,
        ).car
        is None
    )

    with pytest.raises(FrozenInstanceError):
        inputs.now = 2.0  # type: ignore[misc]


def test_metric_module_is_a_runtime_checkable_protocol() -> None:
    class DummyModule:
        name = "dummy"

        def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
            return ()

    assert isinstance(DummyModule(), MetricModule)
    assert tuple(module.name for module in METRIC_MODULES) == (
        "progress",
        "fuel",
        "tyres",
        "pace",
        "gap",
        "pit",
        "ers",
        "damage",
        "environment",
    )


def test_lap_record_fields_can_be_updated_with_replace() -> None:
    record = _record(1)
    updated = replace(record, lap_time_s=89.0, closed=True)

    assert updated.lap_time_s == 89.0
    assert updated.closed is True
    assert record.lap_time_s == 90.0
