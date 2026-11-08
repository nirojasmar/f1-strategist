from __future__ import annotations

import time
from dataclasses import FrozenInstanceError, replace

import pytest

from f1_strategist.config import MetricsConfig
from f1_strategist.metrics.engine import MetricsEngine, MetricsSnapshot
from f1_strategist.metrics.history import MetricInputs
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.parser import (
    EnumValue,
    ParsedPacket,
    SessionHistoryPacket,
    SessionPacket,
    TyreStintHistory,
)
from f1_strategist.race_state import RaceState, RaceStateSnapshot
from tests.scenarios import ScenarioCar, build_race, build_race_timed


class _CountingModule:
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        self.calls += 1
        return (
            Metric(
                "counting.value",
                self.calls,
                "count",
                Window("instant"),
                Quality.OK,
                1,
                inputs.now,
            ),
        )


class _FailingModule:
    name = "broken"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        raise RuntimeError("boom")


class _ConstantModule:
    name = "constant"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        return (
            Metric(
                "constant.value",
                7,
                "count",
                Window("instant"),
                Quality.OK,
                1,
                inputs.now,
            ),
        )


def _cars() -> tuple[ScenarioCar, ScenarioCar]:
    return (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(1, "Driver B", 2, 90.5, 40.0, 2.0, 2.3, compounds=("medium",)),
    )


def _state_and_packet() -> tuple[RaceStateSnapshot, ParsedPacket]:
    state = RaceState()
    packet = next(iter(build_race(_cars(), total_laps=3)))
    state.apply(packet, 1.0)
    return state.snapshot(), packet


def test_metrics_snapshot_is_immutable_and_lookup_only_by_key() -> None:
    metric = Metric("demo.value", 1.0, "s", Window("instant"), Quality.OK, 1, 2.0)
    snapshot = MetricsSnapshot({"demo.value": metric}, computed_at_monotonic=2.0, session_uid=5)

    assert snapshot.get("demo.value") is metric
    assert snapshot.get("missing") is None
    assert snapshot["demo.value"] is metric
    assert "demo.value" in snapshot
    assert list(snapshot) == ["demo.value"]
    assert snapshot.updated_at_monotonic == 2.0

    with pytest.raises(FrozenInstanceError):
        snapshot.computed_at_monotonic = 3.0  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.metrics["other"] = metric  # type: ignore[index]


def test_publish_policy_respects_interval_lap_and_session_changes() -> None:
    module = _CountingModule()
    engine = MetricsEngine(
        MetricsConfig(publish_interval_s=0.5),
        modules=(module,),
    )
    state, packet = _state_and_packet()

    engine.apply(packet, 10.0, state)
    assert module.calls == 1

    engine.apply(packet, 10.1, state)
    engine.apply(packet, 10.49, state)
    assert module.calls == 1

    engine.apply(packet, 10.5, state)
    assert module.calls == 2

    next_lap = replace(state, current_lap=2)
    engine.apply(packet, 10.6, next_lap)
    assert module.calls == 3

    next_session = replace(state, session=replace(state.session, uid=99))
    engine.apply(packet, 10.7, next_session)
    assert module.calls == 4


def test_module_failure_does_not_prevent_other_modules_or_history() -> None:
    engine = MetricsEngine(modules=(_FailingModule(), _ConstantModule()))
    state, packet = _state_and_packet()

    engine.apply(packet, 1.0, state)
    snapshot = engine.snapshot()

    failure = snapshot.get("broken.error")
    assert failure is not None
    assert failure.quality is Quality.UNAVAILABLE
    assert failure.value is None
    assert failure.notes[0] == "metric_module_failed"
    assert "RuntimeError" in failure.notes
    assert snapshot.get("constant.value").value == 7  # type: ignore[union-attr]


def test_invalid_stint_history_warning_is_deduplicated_and_valid_history_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    packet = next(
        packet
        for packet, _ in build_race_timed(_cars(), total_laps=3)
        if isinstance(packet, SessionHistoryPacket) and packet.num_laps >= 1
    )
    invalid_packet = replace(
        packet,
        tyre_stint_history=(
            TyreStintHistory(
                EnumValue(-1, "unknown"),
                EnumValue(-1, "unknown"),
                0,
            ),
        ),
    )
    state = RaceState().snapshot()
    state = replace(state, session=replace(state.session, uid=packet.header.session_uid))
    engine = MetricsEngine()

    with caplog.at_level("WARNING", logger="f1_strategist.metrics.engine"):
        engine.apply(invalid_packet, 1.0, state)
        engine.apply(invalid_packet, 2.0, state)
        engine.apply(packet, 3.0, state)

    warnings = [
        record
        for record in caplog.records
        if "Ignoring invalid tyre stint history" in record.message
    ]
    assert len(warnings) == 1
    assert engine.history.snapshot().stints_for(0)


def test_session_change_resets_history_and_publication() -> None:
    engine = MetricsEngine()
    state = RaceState()
    first = next(iter(build_race(_cars(), total_laps=2)))
    state.apply(first, 1.0)
    engine.apply(first, 1.0, state.snapshot())
    assert engine.history.snapshot().session_uid == first.header.session_uid

    second_state = RaceState()
    session_packet = next(
        packet for packet in build_race(_cars(), total_laps=2) if isinstance(packet, SessionPacket)
    )
    new_session = replace(
        session_packet,
        header=replace(session_packet.header, session_uid=first.header.session_uid + 1),
    )
    second_state.apply(new_session, 2.0)
    engine.apply(new_session, 2.0, second_state.snapshot())
    snapshot = engine.snapshot()

    assert snapshot.session_uid == first.header.session_uid + 1
    assert snapshot.computed_at_monotonic == 2.0
    assert engine.history.snapshot().records == {}
    assert engine.history.session_uid == first.header.session_uid + 1


def test_complete_scenario_publishes_every_module_family() -> None:
    state = RaceState()
    engine = MetricsEngine()
    last_at = 0.0
    for packet, observed_at in build_race_timed(_cars(), total_laps=8, samples_per_lap=2):
        state.apply(packet, observed_at)
        engine.apply(packet, observed_at, state.snapshot())
        last_at = observed_at

    snapshot = engine.snapshot()
    for key in (
        "progress.laps_completed",
        "fuel.in_tank_kg",
        "tyres.compound",
        "pace.last_lap_s",
        "gap.ahead_s",
        "pit.stops_done",
        "ers.store_j",
        "damage.aero_max_pct",
        "env.weather",
        "track.safety_car_status",
    ):
        assert snapshot.get(key) is not None, key

    assert snapshot.session_uid == state.snapshot().session.uid
    assert snapshot.computed_at_monotonic == last_at
    assert snapshot.get("progress.laps_completed").value == 8  # type: ignore[union-attr]
    records = engine.history.snapshot().records_for(0)
    assert len(records) == 8
    assert all(record.closed for record in records)
    assert all(record.lap_time_source == "session_history" for record in records)


@pytest.mark.benchmark
@pytest.mark.parametrize("budget_ms", [1.0])
def test_apply_cost_stays_below_one_millisecond(budget_ms: float) -> None:
    state = RaceState()
    engine = MetricsEngine()
    elapsed = 0.0
    calls = 0
    for packet, observed_at in build_race_timed(_cars(), total_laps=6, samples_per_lap=4):
        state.apply(packet, observed_at)
        started = time.perf_counter()
        engine.apply(packet, observed_at, state.snapshot())
        elapsed += time.perf_counter() - started
        calls += 1

    mean_ms = 1000.0 * elapsed / calls
    assert mean_ms < budget_ms


class _PlayerModule:
    name = "player"

    def compute(self, inputs: MetricInputs) -> tuple[Metric, ...]:
        return (
            Metric(
                "player.index",
                inputs.car_index,
                "",
                Window("instant"),
                Quality.OK,
                1,
                inputs.now,
            ),
        )


def test_unknown_player_does_not_fall_back_to_car_zero() -> None:
    module = _PlayerModule()
    engine = MetricsEngine(modules=(module,))
    state, packet = _state_and_packet()

    engine.apply(packet, 1.0, replace(state, player_car_index=None))
    assert engine.snapshot().get("player.index").value == -1  # type: ignore[union-attr]

    engine.apply(packet, 1.1, replace(state, player_car_index=1))
    assert engine.snapshot().get("player.index").value == 1  # type: ignore[union-attr]


def test_each_engine_owns_independent_module_instances() -> None:
    first = MetricsEngine()
    second = MetricsEngine()

    assert first.modules is not second.modules
    assert all(left is not right for left, right in zip(first.modules, second.modules, strict=True))


def test_finalize_forces_a_last_publication() -> None:
    module = _CountingModule()
    engine = MetricsEngine(MetricsConfig(publish_interval_s=1000.0), modules=(module,))
    state, packet = _state_and_packet()

    engine.apply(packet, 1.0, state)
    engine.finalize(state, 1.1)

    assert module.calls == 2
    assert engine.snapshot().computed_at_monotonic == 1.1


def test_official_final_best_lap_reaches_pace_after_finalize() -> None:
    car = ScenarioCar(
        0,
        "Driver A",
        1,
        90.0,
        40.0,
        2.0,
        2.5,
        lap_time_overrides={5: 70.0},
    )
    state = RaceState()
    engine = MetricsEngine(MetricsConfig(publish_interval_s=1000.0))
    last_at = 0.0
    for packet, observed_at in build_race_timed((car,), total_laps=5, samples_per_lap=2):
        state.apply(packet, observed_at)
        engine.apply(packet, observed_at, state.snapshot())
        last_at = observed_at
    engine.finalize(state.snapshot(), last_at)

    final_lap = engine.history.get(0, 5)
    assert final_lap is not None
    assert final_lap.lap_time_s == 70.0
    assert "incomplete" not in final_lap.flags
    assert engine.snapshot().get("pace.best_lap_s").value == 70.0  # type: ignore[union-attr]
