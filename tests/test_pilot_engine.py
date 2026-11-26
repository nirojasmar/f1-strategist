from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import pytest

from f1_strategist.config import (
    AppConfig,
    CaptureConfig,
    PilotConfig,
    TelemetryConfig,
)
from f1_strategist.events.types import EventsSnapshot
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.history import LapHistorySnapshot
from f1_strategist.metrics.types import Quality
from f1_strategist.offline import OfflineEventPipeline
from f1_strategist.pilot.engine import PilotEngine, PilotInputs
from f1_strategist.pilot.evaluation import (
    build_race_state_from_grid,
    load_pilot_dataset,
)
from f1_strategist.pilot.types import (
    AnswerKind,
    Clarification,
    DriverCandidate,
    FactKind,
    Intent,
    PilotAnswer,
    PilotExchange,
    PilotQuery,
    RoutedQuery,
    ToolFact,
    ToolInputs,
    ToolResult,
)
from f1_strategist.race_state import (
    PacketFreshness,
    RaceSessionSnapshot,
    RaceStateSnapshot,
)
from f1_strategist.strategy.types import StrategySnapshot


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _state(*, freshness: tuple[PacketFreshness, ...] | None = None) -> RaceStateSnapshot:
    if freshness is None:
        freshness = (PacketFreshness("lap_data", 9.8, 1.5),)
    return RaceStateSnapshot(
        session=RaceSessionSnapshot(
            uid=1,
            session_type="race",
            track="monza",
            total_laps=10,
            started=True,
            ended=False,
        ),
        current_lap=5,
        player_car_index=None,
        cars=(),
        freshness=freshness,
        events=(),
    )


def _inputs(*, state: RaceStateSnapshot | None = None, events: EventsSnapshot | None = None):
    return PilotInputs(
        state=state or _state(),
        metrics=MetricsSnapshot(),
        strategy=StrategySnapshot(),
        history=LapHistorySnapshot(),
        events=events or EventsSnapshot(),
    )


def _routed(query: PilotQuery, intent: Intent) -> RoutedQuery:
    return RoutedQuery(query=query, intent=intent, confidence=0.9)


def _result(intent: Intent = Intent.FUEL_STATUS) -> ToolResult:
    return ToolResult(
        intent=intent,
        quality=Quality.OK,
        facts=(
            ToolFact(
                name="fuel_kg",
                value=18.5,
                unit="kg",
                kind=FactKind.MEASURED,
                quality=Quality.OK,
            ),
        ),
    )


def _answer(routed: RoutedQuery, result: ToolResult | None, *, language, cfg) -> PilotAnswer:
    return PilotAnswer(
        query_id=routed.query.query_id,
        intent=routed.intent,
        kind=AnswerKind.ANSWER,
        text="Quedan 18.5 kg.",
        message_key="pilot_answer",
        tool_result=result,
    )


def test_ask_publishes_ack_before_slow_answer_and_records_exchange() -> None:
    clock = _Clock()
    published: list[tuple[PilotAnswer, float]] = []
    tool_calls: list[ToolInputs] = []

    def tool(routed: RoutedQuery, inputs: ToolInputs) -> ToolResult:
        tool_calls.append(inputs)
        return _result()

    def compose(
        routed: RoutedQuery, result: ToolResult | None, *, language: str, cfg
    ) -> PilotAnswer:
        clock.now += 5.0  # el núcleo está ocupado / el modelo tarda
        return _answer(routed, result, language=language, cfg=cfg)

    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(),
        route=lambda text, **kwargs: _routed(
            PilotQuery(query_id="stale", text=text, language="es", received_at=0.0),
            Intent.FUEL_STATUS,
        ),
        directory=lambda state: object(),
        tools={Intent.FUEL_STATUS: tool},
        compose=compose,
        publish=lambda answer: published.append((answer, clock.now)),
        clock=clock,
    )

    answer = engine.ask("¿Cuánto combustible queda?", now=10.0)

    # El ACK sale antes de enrutar, con el reloj aún sin avanzar (< 50 ms).
    assert len(published) == 1
    assert published[0][0].kind is AnswerKind.ACK
    assert published[0][0].message_key == "pilot_ack"
    assert published[0][1] == 0.0
    assert answer.kind is AnswerKind.ANSWER
    assert answer.tool_result == _result()
    assert answer.latency_s == pytest.approx(5.0)
    assert tool_calls[0].now == 10.0
    snapshot = engine.snapshot()
    assert len(snapshot.recent) == 1
    assert snapshot.recent[0].answer.query_id == answer.query_id
    assert snapshot.counters == {"answer": 1}


def test_failing_tool_is_logged_and_returns_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def tool(routed: RoutedQuery, inputs: ToolInputs) -> ToolResult:
        raise RuntimeError("boom")

    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(),
        route=lambda text, **kwargs: _routed(
            PilotQuery(query_id="q", text=text, language="es", received_at=0.0),
            Intent.FUEL_STATUS,
        ),
        directory=lambda state: object(),
        tools={Intent.FUEL_STATUS: tool},
        compose=_answer,
    )

    with caplog.at_level(logging.ERROR):
        answer = engine.ask("¿Cuánto combustible queda?", now=10.0)

    assert answer.kind is AnswerKind.UNAVAILABLE
    assert answer.message_key == "pilot_unavailable"
    assert "pilot_tool_failed" in caplog.text


def test_clarification_is_pending_until_resolution() -> None:
    clarification = Clarification(
        message_key="pilot_clarify_driver",
        options=(DriverCandidate(car_index=1, name="Hamilton"),),
        expires_at=40.0,
    )
    routed: list[RoutedQuery] = []

    def route(text: str, **kwargs) -> RoutedQuery:
        query = PilotQuery(query_id="q", text=text, language="es", received_at=0.0)
        result = RoutedQuery(
            query=query,
            intent=Intent.COMPARE_DRIVER,
            confidence=0.5,
            clarification=clarification if not routed else None,
        )
        routed.append(result)
        return result

    def compose(routed_query: RoutedQuery, result: ToolResult | None, *, language, cfg):
        kind = AnswerKind.CLARIFICATION if routed_query.clarification else AnswerKind.ANSWER
        return PilotAnswer(
            query_id=routed_query.query.query_id,
            intent=routed_query.intent,
            kind=kind,
            text="¿Qué piloto?",
            message_key="pilot_clarify_driver",
        )

    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(),
        route=route,
        directory=lambda state: object(),
        tools={Intent.COMPARE_DRIVER: lambda routed_query, inputs: _result()},
        compose=compose,
    )

    first = engine.ask("¿Cómo va Hamilton?", now=10.0)
    assert first.kind is AnswerKind.CLARIFICATION
    assert engine.snapshot().pending_clarification == clarification

    second = engine.ask("Hamilton", now=11.0)
    assert second.kind is AnswerKind.ANSWER
    assert engine.snapshot().pending_clarification is None
    assert engine.snapshot().counters == {"answer": 1, "clarification": 1}


class _Dialogue:
    def __init__(self) -> None:
        self.exchanges: list[PilotExchange] = []
        self.pending: Clarification | None = None

    def record(self, exchange: PilotExchange, now: float) -> None:
        self.exchanges.append(exchange)

    def pending_clarification(self, now: float) -> Clarification | None:
        return self.pending


def test_dialogue_pending_clarification_wins_over_router() -> None:
    pending = Clarification(
        message_key="pilot_clarify_driver",
        options=(DriverCandidate(car_index=1, name="Hamilton"),),
        expires_at=40.0,
    )
    dialogue = _Dialogue()
    dialogue.pending = pending
    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(),
        route=lambda text, **kwargs: _routed(
            PilotQuery(query_id="q", text=text, language="es", received_at=0.0),
            Intent.COMPARE_DRIVER,
        ),
        directory=lambda state: object(),
        dialogue=dialogue,
        tools={Intent.COMPARE_DRIVER: lambda routed_query, inputs: _result()},
        compose=_answer,
    )

    engine.ask("¿Y él?", now=10.0)

    assert len(dialogue.exchanges) == 1
    assert engine.snapshot().pending_clarification == pending


def test_question_during_alert_storm_with_muted_gate_is_answered() -> None:
    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(
            events=EventsSnapshot(muted=True, counters={"alerts_suppressed": 50})
        ),
        route=lambda text, **kwargs: _routed(
            PilotQuery(query_id="q", text=text, language="es", received_at=0.0),
            Intent.FUEL_STATUS,
        ),
        directory=lambda state: object(),
        tools={Intent.FUEL_STATUS: lambda routed_query, inputs: _result()},
        compose=_answer,
    )

    answer = engine.ask("¿Cuánto combustible queda?", now=10.0)

    assert answer.kind is AnswerKind.ANSWER
    assert answer.tool_result is not None


def test_gate_failure_does_not_prevent_answer(caplog: pytest.LogCaptureFixture) -> None:
    def note_interaction(now: float) -> None:
        raise RuntimeError("gate caído")

    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(),
        route=lambda text, **kwargs: _routed(
            PilotQuery(query_id="q", text=text, language="es", received_at=0.0),
            Intent.FUEL_STATUS,
        ),
        directory=lambda state: object(),
        tools={Intent.FUEL_STATUS: lambda routed_query, inputs: _result()},
        compose=_answer,
        note_interaction=note_interaction,
    )

    with caplog.at_level(logging.ERROR):
        answer = engine.ask("¿Cuánto combustible queda?", now=10.0)

    assert answer.kind is AnswerKind.ANSWER
    assert "pilot_gate_failed" in caplog.text


def test_stale_telemetry_is_declared_by_composer_without_tool() -> None:
    calls: list[ToolResult | None] = []
    tools_called: list[bool] = []

    def compose(routed: RoutedQuery, result: ToolResult | None, *, language, cfg):
        calls.append(result)
        return PilotAnswer(
            query_id=routed.query.query_id,
            intent=routed.intent,
            kind=AnswerKind.ANSWER,
            text="No hay datos recientes.",
            message_key="pilot_no_recent_data",
            tool_result=result,
        )

    def tool(routed: RoutedQuery, inputs: ToolInputs) -> ToolResult:
        tools_called.append(True)
        return _result()

    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(state=_state(freshness=(PacketFreshness("lap_data", 1.0, 1.5),))),
        route=lambda text, **kwargs: _routed(
            PilotQuery(query_id="q", text=text, language="es", received_at=0.0),
            Intent.FUEL_STATUS,
        ),
        directory=lambda state: object(),
        tools={Intent.FUEL_STATUS: tool},
        compose=compose,
    )

    answer = engine.ask("¿Cuánto combustible queda?", now=10.0)

    assert calls == [None]
    assert tools_called == []
    assert answer.message_key == "pilot_no_recent_data"


def test_followup_is_scheduled_and_its_failure_is_isolated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduled: list[PilotAnswer] = []

    def followup(answer: PilotAnswer, sources) -> None:
        scheduled.append(answer)
        assert sources.observed_at == 10.0
        raise RuntimeError("comunicador no disponible")

    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(),
        route=lambda text, **kwargs: _routed(
            PilotQuery(query_id="q", text=text, language="es", received_at=0.0),
            Intent.ASK_STRATEGY,
        ),
        directory=lambda state: object(),
        tools={Intent.ASK_STRATEGY: lambda routed_query, inputs: _result(Intent.ASK_STRATEGY)},
        compose=_answer,
        followup=followup,
    )

    with caplog.at_level(logging.ERROR):
        answer = engine.ask("¿Qué estrategia sigo?", now=10.0)

    assert answer.kind is AnswerKind.ANSWER
    assert answer.followup_pending is True
    assert len(scheduled) == 1
    assert "pilot_followup_failed" in caplog.text


def test_default_pipeline_answers_and_reset_clear_state() -> None:
    # Sin colaboradores explícitos el motor resuelve router, directorio, diálogo,
    # herramientas y compositor reales.
    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(),
        note_interaction=lambda now: None,
    )

    answer = engine.ask("¿Cómo voy?", now=10.0)

    assert answer.kind is AnswerKind.ANSWER
    assert answer.text
    assert len(engine.snapshot().recent) == 1
    engine.reset()
    assert engine.snapshot().recent == ()
    assert engine.snapshot().counters == {}
    assert engine.snapshot().pending_clarification is None


def test_real_pipeline_resolves_clarification_end_to_end() -> None:
    dataset = load_pilot_dataset(Path(__file__).parent / "fixtures" / "pilot_questions")
    state = replace(
        build_race_state_from_grid(dataset.default_grid),
        freshness=(PacketFreshness("lap_data", 9.8, 1.5),),
    )
    seen: list[RoutedQuery] = []

    def tool(routed: RoutedQuery, inputs: ToolInputs) -> ToolResult:
        seen.append(routed)
        return ToolResult(
            intent=Intent.COMPARE_DRIVER,
            quality=Quality.OK,
            facts=(
                ToolFact(
                    name="gap_s",
                    value=1.5,
                    unit="s",
                    kind=FactKind.MEASURED,
                    quality=Quality.OK,
                ),
            ),
        )

    engine = PilotEngine(
        PilotConfig(),
        snapshots=lambda: _inputs(state=state),
        tools={Intent.COMPARE_DRIVER: tool},
    )

    first = engine.ask("¿Cómo voy respecto a Halden?", now=10.0)

    assert first.kind is AnswerKind.CLARIFICATION
    pending = engine.snapshot().pending_clarification
    assert pending is not None
    assert tuple(option.car_index for option in pending.options) == (12, 13)

    second = engine.ask("Timo", now=11.0)

    assert second.kind is AnswerKind.ANSWER
    assert second.intent is Intent.COMPARE_DRIVER
    assert second.text
    assert seen and seen[0].entities[0].car_index == 12
    assert engine.snapshot().pending_clarification is None
    assert engine.snapshot().counters == {"answer": 1, "clarification": 1}


def test_offline_pipeline_answers_without_packets() -> None:
    pipeline = OfflineEventPipeline(
        AppConfig(
            telemetry=TelemetryConfig(listen_port=0),
            capture=CaptureConfig(enabled=False),
        )
    )
    try:
        answer = pipeline.ask_pilot("¿Cómo voy?")
        assert answer.kind is AnswerKind.UNAVAILABLE
        snapshot = pipeline.pilot_snapshot()
        assert len(snapshot.recent) == 1
        assert snapshot.counters == {"unavailable": 1}
    finally:
        pipeline.stop()


def _grid_state():
    dataset = load_pilot_dataset(Path(__file__).parent / "fixtures" / "pilot_questions")
    return replace(
        build_race_state_from_grid(dataset.default_grid),
        freshness=(PacketFreshness("lap_data", 9.8, 1.5),),
    )


def _engine_with_real_tools(state: RaceStateSnapshot) -> PilotEngine:
    return PilotEngine(PilotConfig(), snapshots=lambda: _inputs(state=state))


def test_real_flow_retired_and_unknown_rivals_do_not_use_a_substitute() -> None:
    engine = _engine_with_real_tools(_grid_state())

    retired = engine.ask("¿Cómo voy respecto a Shaw?", now=10.0)
    unknown = engine.ask("¿Cómo voy respecto a Senna?", now=10.0)

    assert retired.intent is Intent.COMPARE_DRIVER
    assert retired.text == "El piloto que pides está retirado y no tengo datos suyos."
    assert unknown.intent is Intent.COMPARE_DRIVER
    assert unknown.text == "No sé a qué piloto te refieres; no tengo datos de esa referencia."


def test_real_flow_undercut_retired_rival_is_not_replaced() -> None:
    engine = _engine_with_real_tools(_grid_state())

    answer = engine.ask("¿Podemos hacer undercut a Shaw?", now=10.0)

    assert answer.intent is Intent.PIT_UNDERCUT
    assert answer.text == "El piloto que pides está retirado y no tengo datos suyos."


def test_real_flow_extreme_relatives_answer_without_numbers() -> None:
    state = _grid_state()
    leader = replace(
        state,
        cars=tuple(
            replace(car, position=1) if car.car_index == state.player_car_index else car
            for car in state.cars
        ),
    )
    ahead = _engine_with_real_tools(leader).ask("¿A cuánto está el de delante?", now=10.0)

    last = replace(
        state,
        cars=tuple(
            replace(car, position=20) if car.car_index == state.player_car_index else car
            for car in state.cars
        ),
    )
    behind = _engine_with_real_tools(last).ask("¿A cuánto está el de atrás?", now=10.0)

    assert ahead.text == "No hay nadie delante de ti."
    assert behind.text == "No hay nadie detrás de ti."
