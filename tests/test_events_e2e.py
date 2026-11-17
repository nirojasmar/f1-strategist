from __future__ import annotations

import struct
from collections import Counter
from pathlib import Path
from time import perf_counter_ns

import pytest

from f1_strategist.capture import CapturedPacket, CaptureWriter
from f1_strategist.config import (
    AppConfig,
    EventsConfig,
    GateConfig,
    GateCooldownConfig,
    WeatherEventsConfig,
)
from f1_strategist.datasets import load_manifest
from f1_strategist.events.engine import EventEngine, EventEvaluation, evaluate_once
from f1_strategist.events.gate import GateReason
from f1_strategist.events.rules_track import RainExpectedRule
from f1_strategist.events.types import (
    ConditionPolicy,
    EventInputs,
    EventPriority,
    EventRule,
    GateOutcome,
    Observation,
)
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.offline import OfflineEventPipeline, run_capture_offline
from f1_strategist.packets import HEADER_SIZE
from f1_strategist.parser import ParsedPacket
from f1_strategist.race_state import RaceSessionSnapshot, RaceStateSnapshot
from f1_strategist.strategy.types import StrategySnapshot
from tests.conftest import make_packet
from tests.scenarios import ScenarioCar, build_race_timed

_RAIN_PROBABILITY_KEY = "env.rain_probability_next_10_min_pct"
_REAL_MANIFEST = load_manifest()


class _LapRule:
    def __init__(
        self,
        rule: str,
        priority: EventPriority,
        active_laps: frozenset[int] | None = None,
    ) -> None:
        self.policy = ConditionPolicy(
            rule=rule,
            category="track" if priority is EventPriority.CRITICAL else "strategy",
            priority=priority,
            message_key=f"event.{rule}",
        )
        self._active_laps = active_laps

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        active = self._active_laps is None or inputs.lap in self._active_laps
        return (
            Observation(
                key=self.policy.rule,
                active=active,
                facts=(("lap", inputs.lap),),
            ),
        )


def _state(lap: int) -> RaceStateSnapshot:
    return RaceStateSnapshot(
        session=RaceSessionSnapshot(
            uid=1,
            session_type="race",
            track="synthetic",
            total_laps=20,
            started=True,
            ended=False,
        ),
        current_lap=lap,
        player_car_index=None,
        cars=(),
        freshness=(),
        events=(),
    )


def _metrics(lap: int, probability_pct: float | None = None) -> MetricsSnapshot:
    computed_at = float(lap)
    if probability_pct is None:
        return MetricsSnapshot(computed_at_monotonic=computed_at, session_uid=1)
    metric = Metric(
        key=_RAIN_PROBABILITY_KEY,
        value=probability_pct,
        unit="pct",
        window=Window("instant"),
        quality=Quality.OK,
        sample_count=1,
        computed_at_monotonic=computed_at,
    )
    return MetricsSnapshot(
        {_RAIN_PROBABILITY_KEY: metric},
        computed_at_monotonic=computed_at,
        session_uid=1,
    )


def _gate(**changes: object) -> GateConfig:
    values: dict[str, object] = {
        "global_min_gap_s": 0.0,
        "cooldown_s": GateCooldownConfig(strategic=0.0, informational=0.0),
        "equivalence_window_s": 0.0,
        "update_min_interval_s": 0.0,
        "defer_max_s": 60.0,
        "quiet_first_laps": 0,
        "informational_space_s": 0.0,
        "pilot_quiet_s": 0.0,
    }
    values.update(changes)
    return GateConfig(**values)  # type: ignore[arg-type]


def _evaluate(
    engine: EventEngine,
    laps: range,
    *,
    probabilities: tuple[float, ...] | None = None,
) -> tuple[EventEvaluation, ...]:
    evaluations: list[EventEvaluation] = []
    for lap in laps:
        probability = None if probabilities is None else probabilities[lap - 1]
        engine.apply(
            _state(lap),
            _metrics(lap, probability),
            StrategySnapshot(),
            float(lap),
        )
        if engine.last_evaluation is not None:
            evaluations.append(engine.last_evaluation)
    return tuple(evaluations)


def _report(evaluations: tuple[EventEvaluation, ...]) -> str:
    alerts_by_lap: Counter[int] = Counter()
    alerts_by_rule: Counter[str] = Counter()
    suppressed_by_reason: Counter[str] = Counter()
    signals = sorted(
        (signal.lap, signal.rule, signal.phase.value)
        for evaluation in evaluations
        for signal in evaluation.signals
    )
    rules_by_event = {
        signal.event_id: signal.rule for evaluation in evaluations for signal in evaluation.signals
    }
    for evaluation in evaluations:
        for decision in evaluation.decisions:
            if decision.alert is not None:
                alerts_by_lap[decision.alert.lap] += 1
                alerts_by_rule[rules_by_event[decision.event_id]] += 1
            elif decision.outcome is GateOutcome.SUPPRESS:
                suppressed_by_reason[decision.reason] += 1
    return (
        f"senales={signals}; "
        f"avisos_por_vuelta={dict(sorted(alerts_by_lap.items()))}; "
        f"avisos_por_regla={dict(sorted(alerts_by_rule.items()))}; "
        f"suprimidos_por_motivo={dict(sorted(suppressed_by_reason.items()))}"
    )


def test_normal_synthetic_race_remains_silent() -> None:
    engine = EventEngine(AppConfig())
    evaluations = _evaluate(engine, range(1, 21))

    assert engine.snapshot().error is None
    assert engine.snapshot().alerts == (), _report(evaluations)
    assert all(not evaluation.decisions for evaluation in evaluations), _report(evaluations)


def test_condition_held_for_twenty_laps_emits_once() -> None:
    engine = EventEngine(
        EventsConfig(),
        gate=_gate(),
        evaluation_min_interval_s=0.1,
        rules=[_LapRule("fuel_margin_low", EventPriority.STRATEGIC)],
    )
    evaluations = _evaluate(engine, range(1, 21))
    emitted = [
        decision.alert
        for evaluation in evaluations
        for decision in evaluation.decisions
        if decision.alert is not None
    ]

    assert len(emitted) == 1, (
        f"20 vueltas con la condición activa: esperado 1 aviso, obtenido {len(emitted)}; "
        f"{_report(evaluations)}"
    )
    assert emitted[0] is not None
    assert len({alert.event_id for alert in emitted if alert is not None}) == 1


def test_rain_threshold_oscillation_is_stable_with_hysteresis() -> None:
    config = EventsConfig(weather=WeatherEventsConfig(update_delta_pct=10.0))
    engine = EventEngine(
        config,
        gate=_gate(),
        evaluation_min_interval_s=0.1,
        rules=[RainExpectedRule(config)],
    )
    probabilities = (49.0, 50.0, 49.0, 51.0, 50.0, 49.0, 51.0, 50.0)
    evaluations = _evaluate(engine, range(1, 9), probabilities=probabilities)
    opened = [
        signal
        for evaluation in evaluations
        for signal in evaluation.signals
        if signal.phase.value == "opened"
    ]
    closed = [
        signal
        for evaluation in evaluations
        for signal in evaluation.signals
        if signal.phase.value == "closed"
    ]

    assert len(opened) == 1, (
        f"oscilación alrededor de 50 %: esperado 1 apertura, obtenido {len(opened)}; "
        f"{_report(evaluations)}"
    )
    assert not closed, _report(evaluations)


def test_critical_event_interrupts_during_cooldown() -> None:
    engine = EventEngine(
        EventsConfig(),
        gate=_gate(
            global_min_gap_s=999.0,
            cooldown_s=GateCooldownConfig(strategic=999.0, informational=999.0),
        ),
        evaluation_min_interval_s=0.1,
        rules=[
            _LapRule("strategy_update", EventPriority.STRATEGIC, frozenset({1, 2})),
            _LapRule("safety_car_deployed", EventPriority.CRITICAL, frozenset({2})),
        ],
    )
    evaluations = _evaluate(engine, range(1, 3))
    emitted = [
        decision.alert
        for evaluation in evaluations
        for decision in evaluation.decisions
        if decision.alert is not None
    ]
    critical = [alert for alert in emitted if alert is not None and alert.interrupt]

    assert len(critical) == 1, (
        f"cooldown activo: esperado 1 crítico interruptivo, obtenido {len(critical)}; "
        f"{_report(evaluations)}"
    )
    assert critical[0].message_key == "event.safety_car_deployed"
    assert len(emitted) == 2, _report(evaluations)


def test_signal_burst_emits_critical_and_reports_deferred_signals() -> None:
    engine = EventEngine(
        EventsConfig(),
        gate=_gate(),
        evaluation_min_interval_s=0.1,
        rules=[
            _LapRule("weather_change", EventPriority.INFORMATIONAL, frozenset({2})),
            _LapRule("strategy_update", EventPriority.STRATEGIC, frozenset({2})),
            _LapRule("safety_car_deployed", EventPriority.CRITICAL, frozenset({2})),
        ],
    )
    evaluations = _evaluate(engine, range(2, 3))
    decisions = evaluations[0].decisions

    assert sum(decision.outcome is GateOutcome.EMIT for decision in decisions) == 1, _report(
        evaluations
    )
    assert (
        sum(decision.reason == GateReason.LOWER_PRIORITY_ACTIVE.value for decision in decisions)
        == 2
    ), _report(evaluations)
    assert sum(alert.interrupt for alert in engine.snapshot().alerts) == 1


def test_muted_and_interruption_level_suppress_as_configured() -> None:
    rules: tuple[EventRule, ...] = (
        _LapRule("strategy_update", EventPriority.STRATEGIC, frozenset({2})),
        _LapRule("safety_car_deployed", EventPriority.CRITICAL, frozenset({2})),
    )
    muted = EventEngine(
        EventsConfig(),
        gate=_gate(muted=True),
        evaluation_min_interval_s=0.1,
        rules=rules,
    )
    muted_evaluations = _evaluate(muted, range(2, 3))
    assert muted.snapshot().alerts == (), _report(muted_evaluations)
    assert len(muted_evaluations[0].signals) == 2
    assert {decision.reason for decision in muted_evaluations[0].decisions} == {
        GateReason.MUTED.value
    }

    critical_only = EventEngine(
        EventsConfig(),
        gate=_gate(interruption_level="critical"),
        evaluation_min_interval_s=0.1,
        rules=rules,
    )
    level_evaluations = _evaluate(critical_only, range(2, 3))
    decisions = level_evaluations[0].decisions
    assert any(
        decision.reason == GateReason.BELOW_INTERRUPTION_LEVEL.value for decision in decisions
    )
    assert len(critical_only.snapshot().alerts) == 1, _report(level_evaluations)
    assert critical_only.snapshot().alerts[0].interrupt


class _ClockSensitiveConsumer:
    def __init__(self, packet_count: int) -> None:
        self._packet_count = packet_count
        self._index = 0
        self.evaluations: list[EventEvaluation] = []
        self.engine = EventEngine(
            EventsConfig(),
            gate=_gate(
                global_min_gap_s=5.0,
                cooldown_s=GateCooldownConfig(strategic=5.0, informational=0.0),
                defer_max_s=0.0,
            ),
            evaluation_min_interval_s=0.1,
            rules=[
                _LapRule("first_signal", EventPriority.STRATEGIC, frozenset({1})),
                _LapRule("second_signal", EventPriority.STRATEGIC, frozenset({2})),
            ],
        )

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        del packet
        lap = 1 if self._index < self._packet_count // 2 else 2
        self._index += 1
        self.engine.apply(
            _state(lap),
            MetricsSnapshot(computed_at_monotonic=observed_at, session_uid=1),
            StrategySnapshot(),
            observed_at,
        )
        if self.engine.last_evaluation is not None:
            self.evaluations.append(self.engine.last_evaluation)


def _clock_scale_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, int]:
    cars = (
        ScenarioCar(0, "Driver A", 1, 90.0, 40.0, 2.0, 2.5),
        ScenarioCar(1, "Driver B", 2, 90.5, 40.0, 2.0, 2.3),
    )
    packets = list(build_race_timed(cars, total_laps=3, samples_per_lap=1))
    path = tmp_path / "clock-scale.f1cap"
    with CaptureWriter(path) as writer:
        for index, _ in enumerate(packets):
            writer.write(
                CapturedPacket(
                    recv_time_ns=index * 1_000_000_000,
                    source_ip="127.0.0.1",
                    source_port=4000,
                    payload=make_packet(1, extra=struct.pack("<I", index)),
                )
            )

    parsed = [packet for packet, _ in packets]

    def fake_parse(payload: bytes) -> ParsedPacket:
        index = struct.unpack_from("<I", payload, HEADER_SIZE)[0]
        return parsed[index]

    monkeypatch.setattr("f1_strategist.offline.parse_packet", fake_parse)
    return path, len(parsed)


def test_offline_clock_scale_can_change_gate_alerts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, packet_count = _clock_scale_capture(tmp_path, monkeypatch)
    evaluations_by_scale: dict[float, tuple[EventEvaluation, ...]] = {}
    alerts_by_scale: dict[float, tuple[str, ...]] = {}
    for scale in (1.0, 4.0):
        consumer = _ClockSensitiveConsumer(packet_count)
        run_capture_offline(path, [consumer], clock_scale=scale)
        evaluations_by_scale[scale] = tuple(consumer.evaluations)
        alerts_by_scale[scale] = tuple(
            decision.event_id
            for evaluation in consumer.evaluations
            for decision in evaluation.decisions
            if decision.alert is not None
        )

    assert len(alerts_by_scale[1.0]) == 2, _report(evaluations_by_scale[1.0])
    assert len(alerts_by_scale[4.0]) == 1, _report(evaluations_by_scale[4.0])


@pytest.mark.real_capture
@pytest.mark.parametrize(
    ("scenario_id", "deployment_key"),
    (
        ("sakhir-20-laps-high-wear", "event.safety_car_deployed"),
        ("baku-13-laps-vsc-retirement", "event.virtual_safety_car_deployed"),
    ),
)
def test_real_safety_car_scenarios_emit_one_deployment_and_withdrawal(
    scenario_id: str,
    deployment_key: str,
) -> None:
    scenario = _REAL_MANIFEST.get(scenario_id)
    if not scenario.available:
        pytest.skip(f"captura no disponible: {scenario.file.name}")

    pipeline = OfflineEventPipeline(AppConfig())
    result = run_capture_offline(scenario.file, [pipeline])
    snapshot = pipeline.events_snapshot()
    report = (
        f"{scenario_id}: avisos_por_vuelta="
        f"{dict(Counter(alert.lap for alert in snapshot.alerts))}; "
        f"avisos_por_regla="
        f"{dict(Counter(alert.message_key for alert in snapshot.alerts))}; "
        f"suprimidos_por_motivo="
        f"{
            dict(
                (key, value)
                for key, value in snapshot.counters.items()
                if key in {reason.value for reason in GateReason}
            )
        }"
    )

    assert result.integrity_problems() == (), f"{scenario_id}: {result.integrity_problems()}"
    assert snapshot.error is None, f"{scenario_id}: {snapshot.error}; {report}"
    deployments = [alert for alert in snapshot.alerts if alert.message_key == deployment_key]
    withdrawals = [
        alert for alert in snapshot.alerts if alert.message_key == "event.safety_car_cleared"
    ]
    assert len(deployments) == 1, f"{scenario_id}: {report}"
    assert deployments[0].interrupt, f"{scenario_id}: {report}"
    assert len(withdrawals) == 1, f"{scenario_id}: {report}"


@pytest.mark.real_capture
def test_normal_real_race_has_no_critical_alert_without_cause() -> None:
    scenario = _REAL_MANIFEST.get("monza-13-laps")
    if not scenario.available:
        pytest.skip(f"captura no disponible: {scenario.file.name}")

    pipeline = OfflineEventPipeline(AppConfig())
    result = run_capture_offline(scenario.file, [pipeline])
    critical = [
        alert
        for alert in pipeline.events_snapshot().alerts
        if alert.priority is EventPriority.CRITICAL
    ]

    assert result.integrity_problems() == (), result.integrity_problems()
    assert not critical, (
        f"{scenario.id}: críticos sin causa={[(a.message_key, a.lap) for a in critical]}"
    )


@pytest.mark.benchmark
def test_detection_and_gate_average_under_one_millisecond() -> None:
    inputs = EventInputs(
        state=_state(10),
        metrics=MetricsSnapshot(),
        strategy=StrategySnapshot(),
        config=EventsConfig(),
        now=10.0,
        lap=10,
    )
    iterations = 2_000
    started = perf_counter_ns()
    for _ in range(iterations):
        evaluate_once(inputs)
    average_ns = (perf_counter_ns() - started) / iterations

    assert average_ns < 1_000_000, f"media {average_ns:.0f} ns por evaluación (límite: 1 ms)"
