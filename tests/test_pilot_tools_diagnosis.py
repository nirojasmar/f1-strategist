from __future__ import annotations

from dataclasses import replace

from f1_strategist.config import MetricsConfig, PilotConfig
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot, LapRecord, MetricInputs
from f1_strategist.metrics.pace import PaceMetrics
from f1_strategist.metrics.types import Quality
from f1_strategist.pilot.tools_diagnosis import diagnose_pace
from f1_strategist.pilot.types import Intent, PilotQuery, RoutedQuery, ToolInputs, ToolResult
from f1_strategist.race_state import RaceState
from f1_strategist.strategy.types import StrategySnapshot


def _record(
    lap: int,
    time: float,
    *,
    sector1: float | None = None,
    sector2: float | None = None,
    sector3: float | None = None,
    wear: float | None = None,
    age: int | None = None,
    traffic: float | None = None,
    damage: float | None = None,
    weather: frozenset[str] = frozenset(),
    pit_entry: bool = False,
) -> LapRecord:
    return LapRecord(
        car_index=0,
        lap_number=lap,
        lap_time_s=time,
        lap_time_source="session_history",
        sector1_s=sector1,
        sector2_s=sector2,
        sector3_s=sector3,
        valid=True,
        tyre_age_start=None if age is None else age - 1,
        tyre_age_end=age,
        tyre_wear_end_pct=None if wear is None else (wear,) * 4,
        traffic_fraction=traffic,
        damage_max_delta_pct=damage,
        weather_seen=weather,
        pit_entry=pit_entry,
        flags=frozenset({"first_lap"}) if lap == 1 else frozenset(),
        closed=True,
    )


def _tool_inputs(
    records: tuple[LapRecord, ...],
    *,
    metrics_config: MetricsConfig | None = None,
) -> ToolInputs:
    config = MetricsConfig() if metrics_config is None else metrics_config
    state = replace(RaceState().snapshot(), player_car_index=0)
    history = LapHistorySnapshot(records={0: records})
    metrics = MetricsSnapshot(
        metrics={
            metric.key: metric
            for metric in PaceMetrics().compute(MetricInputs(state, history, config, 10.0, 0))
        },
        computed_at_monotonic=10.0,
    )
    return ToolInputs(
        state,
        metrics,
        StrategySnapshot(),
        history,
        PilotConfig(),
        10.0,
        metrics_config=config,
    )


def _routed() -> RoutedQuery:
    query = PilotQuery(query_id="q1", text="¿cómo va mi ritmo?", language="es", received_at=0.0)
    return RoutedQuery(query=query, intent=Intent.PACE_DIAGNOSIS, confidence=1.0)


def _diagnose(records: tuple[LapRecord, ...]) -> ToolResult:
    return diagnose_pace(_routed(), _tool_inputs(records))


def _facts(result: ToolResult) -> dict[str, float | int | str | bool | None]:
    return {fact.name: fact.value for fact in result.facts}


def _number(result: ToolResult, name: str) -> float:
    value = _facts(result)[name]
    assert isinstance(value, int | float) and not isinstance(value, bool)
    return float(value)


def _cause_codes(result: ToolResult) -> list[str]:
    return [str(value) for name, value in _facts(result).items() if name.startswith("pace.cause.")]


def test_stable_pace_has_no_causes_and_ignores_in_lap() -> None:
    records = tuple(_record(lap, 90.0) for lap in range(1, 7)) + (
        _record(7, 130.0, pit_entry=True),
    )

    result = _diagnose(records)
    facts = _facts(result)

    assert result.quality is Quality.OK
    assert facts["pace.stable"] is True
    assert facts["pace.loss_s"] == 0.0
    assert _cause_codes(result) == []
    assert "pace_stable" in result.notes


def test_few_clean_laps_are_low_quality_and_say_so() -> None:
    result = _diagnose(tuple(_record(lap, 90.0) for lap in range(1, 5)))

    assert result.quality is Quality.LOW
    assert "pace_few_clean_laps" in result.notes


def test_insufficient_data_returns_insufficient() -> None:
    result = _diagnose(tuple(_record(lap, 90.0) for lap in range(1, 3)))

    assert result.quality is Quality.INSUFFICIENT
    assert result.missing == ("clean_laps",)
    assert result.data_lap == 2
    assert _cause_codes(result) == []


def test_tyre_degradation_is_a_single_cause() -> None:
    records = tuple(_record(lap, 90.0, wear=10.0 + lap) for lap in range(1, 4)) + tuple(
        _record(lap, 90.4, wear=10.0 + lap) for lap in range(4, 9)
    )

    result = _diagnose(records)
    facts = _facts(result)

    assert _cause_codes(result) == ["tyre_degradation"]
    assert _number(result, "pace.evidence.tyre_wear_delta_pct") > 0
    assert facts["pace.stable"] is False


def test_traffic_is_a_single_cause() -> None:
    records = tuple(_record(lap, 90.0) for lap in range(1, 6)) + (
        _record(6, 90.8, traffic=0.8),
        _record(7, 90.8, traffic=0.8),
    )

    result = _diagnose(records)
    facts = _facts(result)

    assert _cause_codes(result) == ["traffic"]
    assert facts["pace.evidence.traffic_laps"] == 2
    assert _number(result, "pace.loss_s") > 0


def test_damage_is_a_single_cause() -> None:
    records = tuple(_record(lap, 90.0) for lap in range(1, 6)) + (
        _record(6, 90.8, damage=8.0),
        _record(7, 90.8, damage=8.0),
    )

    result = _diagnose(records)
    facts = _facts(result)

    assert _cause_codes(result) == ["damage"]
    assert facts["pace.evidence.damage_laps"] == 2
    assert facts["pace.evidence.damage_delta_pct"] == 8.0


def test_slow_sector_is_a_single_cause() -> None:
    records = tuple(
        _record(lap, 90.0, sector1=30.0, sector2=30.0, sector3=30.0) for lap in range(1, 4)
    ) + tuple(_record(lap, 90.4, sector1=30.0, sector2=30.4, sector3=30.0) for lap in range(4, 9))

    result = _diagnose(records)
    facts = _facts(result)

    assert _cause_codes(result) == ["slow_sector"]
    assert facts["pace.evidence.sector"] == "sector2"
    assert _number(result, "pace.evidence.sector_loss_s") > 0


def test_causes_follow_plan_priority_when_several_apply() -> None:
    records = (
        tuple(
            _record(lap, 90.0, sector1=30.0, sector2=30.0, sector3=30.0, wear=10.0 + lap)
            for lap in range(1, 5)
        )
        + tuple(
            _record(lap, 91.2, sector1=30.0, sector2=30.0, sector3=30.4, wear=10.0 + lap)
            for lap in range(5, 8)
        )
        + (
            _record(
                8,
                91.0,
                sector1=30.0,
                sector2=30.0,
                sector3=30.4,
                wear=18.0,
                traffic=0.8,
                weather=frozenset({"light_rain", "dry"}),
            ),
        )
        + (
            _record(
                9,
                91.4,
                sector1=30.0,
                sector2=30.0,
                sector3=30.4,
                wear=19.0,
                damage=9.0,
            ),
        )
    )

    result = _diagnose(records)

    assert _cause_codes(result) == [
        "tyre_degradation",
        "traffic",
        "damage",
        "weather_change",
        "slow_sector",
    ]
    assert "pace_possible_causes" in result.notes


def test_loss_without_evidence_does_not_assert_a_cause() -> None:
    records = tuple(_record(lap, 90.0) for lap in range(1, 4)) + tuple(
        _record(lap, 90.6) for lap in range(4, 9)
    )

    result = _diagnose(records)
    facts = _facts(result)

    assert facts["pace.stable"] is False
    assert _cause_codes(result) == []
    assert "pace_possible_causes" not in result.notes


def test_cause_flags_use_the_supplied_metrics_config() -> None:
    records = tuple(_record(lap, 90.0) for lap in range(1, 6)) + tuple(
        _record(lap, 90.8, traffic=0.6) for lap in range(6, 8)
    )

    default = diagnose_pace(_routed(), _tool_inputs(records))
    configured = diagnose_pace(
        _routed(),
        _tool_inputs(records, metrics_config=MetricsConfig(traffic_lap_fraction=0.9)),
    )

    assert "traffic" in _cause_codes(default)
    assert "traffic" not in _cause_codes(configured)


def test_stale_reference_metric_propagates_to_the_diagnosis() -> None:
    records = tuple(_record(lap, 90.0) for lap in range(1, 6)) + tuple(
        _record(lap, 90.6) for lap in range(6, 9)
    )
    inputs = _tool_inputs(records)
    metrics = inputs.metrics
    stale_best = replace(metrics["pace.best_lap_s"], quality=Quality.STALE)
    stale_snapshot = MetricsSnapshot(
        metrics={**metrics.metrics, "pace.best_lap_s": stale_best},
        computed_at_monotonic=10.0,
    )

    result = diagnose_pace(_routed(), replace(inputs, metrics=stale_snapshot))

    assert result.quality is Quality.STALE
    assert result.stale is True
    assert all(fact.quality is Quality.STALE for fact in result.facts)
