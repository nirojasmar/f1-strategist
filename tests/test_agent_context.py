from __future__ import annotations

from typing import Any

import pytest

from f1_strategist.agent.context import ContextBuilder
from f1_strategist.agent.types import AgentMessage, ContextKind, MessageSource
from f1_strategist.config import AgentConfig
from f1_strategist.events.types import (
    AgentAlert,
    EventPhase,
    EventPriority,
)
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.race_state import CarSnapshot, RaceSessionSnapshot, RaceStateSnapshot
from f1_strategist.strategy.types import (
    ASSUMPTION_RIVALS_DO_NOT_PIT,
    RISK_TRAFFIC_NOT_PRICED,
    STRATEGY_SCHEMA_VERSION,
    StrategyAction,
    StrategyEvaluation,
    StrategyRecommendation,
    StrategySnapshot,
)


def _alert(**overrides: Any) -> AgentAlert:
    values: dict[str, Any] = {
        "alert_id": "alert-1",
        "event_id": "event-1",
        "phase": EventPhase.OPENED,
        "priority": EventPriority.STRATEGIC,
        "category": "strategy",
        "message_key": "event.strategy_pit_recommended",
        "facts": (("gap_s", 0.8), ("margin_laps", 1.5)),
        "lap": 12,
        "emitted_at": 10.0,
    }
    values.update(overrides)
    return AgentAlert(**values)


def _state(
    *,
    lap: int | None = 12,
    total_laps: int | None = 50,
    car: bool = True,
) -> RaceStateSnapshot:
    session = RaceSessionSnapshot(
        uid=1,
        session_type="race",
        track="Monza",
        total_laps=total_laps,
        started=True,
        ended=False,
    )
    cars = (
        (
            CarSnapshot(
                car_index=0,
                position=3,
                actual_tyre_compound="MEDIUM",
                tyre_age_laps=7,
            ),
        )
        if car
        else ()
    )
    return RaceStateSnapshot(
        session=session,
        current_lap=lap,
        player_car_index=0 if car else None,
        cars=cars,
        freshness=(),
        events=(),
    )


def _metric(
    key: str,
    value: Any = 1.0,
    quality: Quality = Quality.OK,
    unit: str = "s",
    computed_at: float = 0.0,
) -> Metric:
    if quality in (Quality.INSUFFICIENT, Quality.UNAVAILABLE):
        value = None
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("instant"),
        quality=quality,
        sample_count=1,
        computed_at_monotonic=computed_at,
    )


def _metrics(*metrics: Metric, computed_at: float = 0.0) -> MetricsSnapshot:
    return MetricsSnapshot(
        metrics={metric.key: metric for metric in metrics},
        computed_at_monotonic=computed_at,
        session_uid=1,
    )


def _strategy(
    *,
    action: StrategyAction = StrategyAction.PIT_IN,
    quality: Quality = Quality.OK,
    updated_at: float = 0.0,
) -> StrategySnapshot:
    recommendation = StrategyRecommendation(
        action=action,
        key="strategy.pit_in",
        pit_lap=14 if action is StrategyAction.PIT_IN else None,
        compound="HARD" if action is StrategyAction.PIT_IN else None,
        gain_vs_baseline_s=1.2,
        gain_interval_s=(0.4, 2.0),
        confidence=0.7,
        notes=("strategy.note.tyre_life",),
    )
    evaluation = StrategyEvaluation(
        revision=1,
        session_uid="session-1",
        lap=12,
        laps_remaining=38.0,
        baseline="stay_out",
        recommendation=recommendation,
        alternatives=(),
        assumptions=(ASSUMPTION_RIVALS_DO_NOT_PIT,),
        risks=(RISK_TRAFFIC_NOT_PRICED,),
        sensitive_to=(),
        data_quality=quality,
        inputs_hash="inputs-hash",
        schema_version=STRATEGY_SCHEMA_VERSION,
    )
    return StrategySnapshot(evaluation=evaluation, updated_at_monotonic=updated_at)


@pytest.mark.parametrize(
    "category",
    ["damage", "rivals", "fuel", "tyres", "strategy", "track", "weather", "data"],
)
def test_build_alert_covers_every_category(category: str) -> None:
    alert = _alert(category=category, message_key=f"event.{category}")
    context = ContextBuilder(AgentConfig()).build_alert(alert, state=_state())

    assert context.kind is ContextKind.ALERT
    assert context.alert is not None
    assert context.alert["category"] == category
    assert context.alert["message_key"] == alert.message_key
    assert context.alert["lap"] == 12
    assert context.language == "es"


def test_facts_keep_value_and_unit_and_unknown_data_stays_null() -> None:
    alert = _alert(facts=(("gap_s", 0.8), ("margin_laps", 1.5), ("compound", None)))
    context = ContextBuilder(AgentConfig()).build_alert(alert, state=_state())

    assert context.alert is not None
    facts = {fact["name"]: fact for fact in context.alert["facts"]}
    assert facts["gap_s"] == {"name": "gap_s", "value": 0.8, "unit": "s"}
    assert facts["margin_laps"]["unit"] == "laps"
    assert facts["compound"]["value"] is None
    assert facts["compound"]["unit"] == ""


def test_situation_uses_only_published_state() -> None:
    context = ContextBuilder(AgentConfig()).build_alert(_alert(), state=_state())

    assert context.situation["lap"] == 12
    assert context.situation["laps_remaining"] == 38
    assert context.situation["position"] == 3
    assert context.situation["compound"] == "MEDIUM"
    assert context.situation["tyre_age_laps"] == 7


def test_missing_data_is_listed_as_uncertainty_not_invented() -> None:
    context = ContextBuilder(AgentConfig()).build_alert(
        _alert(), state=_state(total_laps=None, car=False)
    )

    assert context.situation["laps_remaining"] is None
    assert context.situation["position"] is None
    assert context.situation["compound"] is None
    assert context.situation["tyre_age_laps"] is None
    assert "missing:laps_remaining" in context.uncertainties
    assert "missing:position" in context.uncertainties
    assert "missing:compound" in context.uncertainties
    assert context.uncertainties == tuple(sorted(context.uncertainties))


def test_strategy_includes_recommendation_and_ranges() -> None:
    context = ContextBuilder(AgentConfig()).build_alert(
        _alert(), state=_state(), strategy=_strategy()
    )

    assert context.strategy is not None
    assert context.strategy["action"] == StrategyAction.PIT_IN.value
    assert context.strategy["pit_lap"] == 14
    assert context.strategy["compound"] == "HARD"
    assert context.strategy["gain_low_s"] == 0.4
    assert context.strategy["gain_high_s"] == 2.0
    assert context.strategy["confidence"] == 0.7
    assert context.strategy["assumptions"] == (ASSUMPTION_RIVALS_DO_NOT_PIT,)
    assert context.strategy["risks"] == (RISK_TRAFFIC_NOT_PRICED,)


def test_strategy_without_recommendation_keeps_pit_fields_null() -> None:
    context = ContextBuilder(AgentConfig()).build_alert(
        _alert(), state=_state(), strategy=_strategy(action=StrategyAction.NO_CHANGE)
    )

    assert context.strategy is not None
    assert context.strategy["action"] == StrategyAction.NO_CHANGE.value
    assert context.strategy["pit_lap"] is None
    assert context.strategy["compound"] is None


def test_unavailable_strategy_is_an_uncertainty() -> None:
    context = ContextBuilder(AgentConfig()).build_alert(
        _alert(), state=_state(), strategy=StrategySnapshot()
    )

    assert context.strategy is None
    assert "strategy_unavailable" in context.uncertainties


def test_stale_data_is_reported_with_quality_key() -> None:
    metrics = _metrics(
        _metric("tyre.wear_mean_pct", 42.0, Quality.STALE),
        _metric("fuel.margin_laps", 1.5, Quality.LOW),
    )
    context = ContextBuilder(AgentConfig()).build_alert(
        _alert(category="tyres"), state=_state(), metrics=metrics
    )

    assert "stale:tyre.wear_mean_pct" in context.uncertainties
    assert "low:fuel.margin_laps" in context.uncertainties
    assert context.metrics["tyre.wear_mean_pct"]["quality"] == Quality.STALE.value


def test_stale_telemetry_alert_is_an_uncertainty() -> None:
    context = ContextBuilder(AgentConfig()).build_alert(
        _alert(category="data", message_key="event.data_stale"), state=_state()
    )

    assert "stale:telemetry" in context.uncertainties


def test_game_texts_are_sanitized_and_bounded() -> None:
    alert = _alert(
        facts=(("name", "Max\nVerstappen\x00 PIT NOW"),),
    )
    context = ContextBuilder(AgentConfig()).build_alert(alert, state=_state(), strategy=_strategy())

    assert context.alert is not None
    value = context.alert["facts"][0]["value"]
    assert value == "Max Verstappen PIT NOW"
    assert "\n" not in value
    assert "\x00" not in value
    assert context.strategy is not None
    assert context.strategy["compound"] == "HARD"


def test_long_game_text_is_truncated() -> None:
    alert = _alert(facts=(("name", "x" * 200),))
    context = ContextBuilder(AgentConfig()).build_alert(alert, state=_state())

    assert context.alert is not None
    assert len(context.alert["facts"][0]["value"]) <= 80


def test_budget_trims_metrics_and_recent_messages_but_never_facts() -> None:
    metrics = _metrics(*[_metric(f"tyre.metric_{index}", float(index)) for index in range(10)])
    recent = ["mensaje " * 25 + str(index) for index in range(5)]
    config = AgentConfig(context_token_budget=200)
    context = ContextBuilder(config).build_alert(
        _alert(category="tyres"), state=_state(), metrics=metrics, recent=recent
    )

    assert context.alert is not None
    assert len(context.alert["facts"]) == 2
    assert len(context.metrics) < 10
    assert len(context.recent_messages) < 5
    assert (len(context.to_json()) + 3) // 4 <= config.context_token_budget


def test_budget_trims_oldest_recent_messages_and_keeps_alert_facts() -> None:
    recent = [f"mensaje {index} " + "x" * 100 for index in range(5)]
    generous = AgentConfig(context_token_budget=5000, recent_messages=5)
    kept = ContextBuilder(generous).build_alert(_alert(), state=_state(), recent=recent)
    assert kept.recent_messages == tuple(recent)

    tight = AgentConfig(context_token_budget=200, recent_messages=5)
    trimmed = ContextBuilder(tight).build_alert(_alert(), state=_state(), recent=recent)
    assert len(trimmed.recent_messages) == 1
    assert trimmed.recent_messages[0] == recent[-1]
    assert trimmed.alert is not None
    assert len(trimmed.alert["facts"]) == 2


def test_recent_messages_keep_only_the_last_configured_ones() -> None:
    messages = tuple(
        AgentMessage(
            message_id=f"message-{index}",
            alert_id=None,
            event_id=None,
            priority=EventPriority.INFORMATIONAL,
            language="es",
            text=f"texto {index}",
            source=MessageSource.TEMPLATE,
        )
        for index in range(4)
    )
    context = ContextBuilder(AgentConfig(recent_messages=2)).build_alert(
        _alert(), state=_state(), recent=messages
    )

    assert context.recent_messages == ("texto 2", "texto 3")


def test_context_hash_is_stable_across_playback_speeds() -> None:
    builder = ContextBuilder(AgentConfig())
    slow = builder.build_alert(
        _alert(emitted_at=10.0),
        state=_state(),
        metrics=_metrics(_metric("tyre.wear_mean_pct", 42.0, computed_at=10.0)),
        strategy=_strategy(updated_at=10.0),
    )
    fast = builder.build_alert(
        _alert(emitted_at=10_000.0),
        state=_state(),
        metrics=_metrics(_metric("tyre.wear_mean_pct", 42.0, computed_at=10_000.0)),
        strategy=_strategy(updated_at=10_000.0),
    )

    assert slow.context_hash == fast.context_hash
    assert slow.to_json() == fast.to_json()


def test_context_hash_changes_when_the_content_changes() -> None:
    builder = ContextBuilder(AgentConfig())
    first = builder.build_alert(_alert(facts=(("gap_s", 0.8),)), state=_state())
    second = builder.build_alert(_alert(facts=(("gap_s", 0.9),)), state=_state())

    assert first.context_hash != second.context_hash


def test_build_question_uses_the_question_contract_without_alert() -> None:
    context = ContextBuilder(AgentConfig()).build_question(
        "¿Puedo\nentrar a boxes?", state=_state(), recent=("anterior",)
    )

    assert context.kind is ContextKind.QUESTION
    assert context.alert is None
    assert context.constraints["question"] == "¿Puedo entrar a boxes?"
    assert context.recent_messages == ("anterior",)
    assert context.context_hash
