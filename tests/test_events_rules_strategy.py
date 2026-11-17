"""Pruebas de las reglas de estrategia y ventana de parada (E4)."""

from __future__ import annotations

from f1_strategist.config import EventsConfig, PitWindowEventsConfig, StrategyEventsConfig
from f1_strategist.events.rules_strategy import (
    PitWindowOpenRule,
    StrategyPitRecommendedRule,
)
from f1_strategist.events.tracker import ConditionTracker
from f1_strategist.events.types import EventInputs, EventPhase, EventPriority
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.types import Quality
from f1_strategist.race_state import RaceSessionSnapshot, RaceStateSnapshot
from f1_strategist.strategy.types import (
    StrategyAction,
    StrategyEvaluation,
    StrategyRecommendation,
    StrategySnapshot,
)


def _session(
    *,
    uid: int | None = 1,
    ideal: int | None = None,
    latest: int | None = None,
    session_type: str | None = "race",
) -> RaceSessionSnapshot:
    return RaceSessionSnapshot(
        uid=uid,
        session_type=session_type,
        track=None,
        total_laps=None,
        started=True,
        ended=False,
        pit_window_ideal_lap=ideal,
        pit_window_latest_lap=latest,
    )


def _state(
    *,
    lap: int | None = 12,
    session: RaceSessionSnapshot | None = None,
) -> RaceStateSnapshot:
    return RaceStateSnapshot(
        session=session or _session(),
        current_lap=lap,
        player_car_index=None,
        cars=(),
        freshness=(),
        events=(),
    )


def _recommendation(
    *,
    action: StrategyAction = StrategyAction.PIT_NOW,
    pit_lap: int | None = 12,
    compound: str | None = "SOFT",
    gain: float | None = 4.0,
    interval: tuple[float, float] | None = (2.5, 5.5),
    confidence: float | None = 0.8,
) -> StrategyRecommendation:
    return StrategyRecommendation(
        action=action,
        key=f"{action.value}:{pit_lap}:{compound}",
        pit_lap=pit_lap,
        compound=compound,
        gain_vs_baseline_s=gain,
        gain_interval_s=interval,
        confidence=confidence,
    )


def _evaluation(
    *,
    revision: int = 1,
    uid: str = "uid-1",
    lap: int = 12,
    recommendation: StrategyRecommendation | None = None,
    quality: Quality = Quality.OK,
    laps_remaining: float | None = 20.0,
) -> StrategyEvaluation:
    return StrategyEvaluation(
        revision=revision,
        session_uid=uid,
        lap=lap,
        laps_remaining=laps_remaining,
        baseline="stay_out",
        recommendation=recommendation or _recommendation(),
        alternatives=(),
        assumptions=(),
        risks=(),
        sensitive_to=(),
        data_quality=quality,
        inputs_hash=f"hash-{revision}",
        schema_version=2,
    )


def _strategy(
    evaluation: StrategyEvaluation | None = None,
    *,
    updated_at: float = 10.0,
) -> StrategySnapshot:
    return StrategySnapshot(evaluation=evaluation, updated_at_monotonic=updated_at)


def _inputs(
    *,
    strategy: StrategySnapshot | None = None,
    state: RaceStateSnapshot | None = None,
    config: EventsConfig | None = None,
    now: float = 10.0,
) -> EventInputs:
    return EventInputs(
        state=state or _state(),
        metrics=MetricsSnapshot(),
        strategy=strategy or _strategy(_evaluation()),
        config=config or EventsConfig(),
        now=now,
        lap=12,
    )


def _facts(observation: object) -> dict[str, object]:
    return dict(observation.facts)  # type: ignore[attr-defined]


def test_strategy_policy_matches_the_matrix() -> None:
    policy = StrategyPitRecommendedRule().policy

    assert policy.rule == "strategy_pit_recommended"
    assert policy.priority is EventPriority.STRATEGIC
    assert policy.message_key == "event.strategy_pit_recommended"
    assert policy.persistence_observations == 2
    assert policy.update_min_delta == 2.0


def test_recommendation_opens_after_two_consecutive_evaluations() -> None:
    rule = StrategyPitRecommendedRule()
    tracker = ConditionTracker(rule.policy)

    first = rule.observe(_inputs(strategy=_strategy(_evaluation(revision=1))))
    second = rule.observe(_inputs(strategy=_strategy(_evaluation(revision=2))))

    assert first[0].active is True
    assert tracker.update(first[0], now=10.0, lap=12) == ()
    opened = tracker.update(second[0], now=11.0, lap=12)
    assert [item.signal.phase for item in opened] == [EventPhase.OPENED]
    assert opened[0].signal.event_id == "strategy_pit_recommended:pit_recommendation:1"
    facts = dict(opened[0].signal.facts)
    assert facts == {
        "action": "PIT_NOW",
        "pit_lap": 12,
        "compound": "SOFT",
        "gain_s": 4.0,
        "gain_low_s": 2.5,
        "gain_high_s": 5.5,
        "confidence": 0.8,
        "laps_remaining": 20.0,
    }


def test_a_stable_recommendation_is_observed_only_once_per_revision() -> None:
    rule = StrategyPitRecommendedRule()
    snapshot = _strategy(_evaluation(revision=7), updated_at=20.0)

    assert rule.observe(_inputs(strategy=snapshot)) != ()
    assert rule.observe(_inputs(strategy=snapshot)) == ()
    assert rule.observe(_inputs(strategy=_strategy(_evaluation(revision=8), updated_at=20.0)))


def test_non_alerting_actions_confidence_and_quality_are_inactive() -> None:
    cases = (
        _evaluation(recommendation=_recommendation(action=StrategyAction.NO_CHANGE)),
        _evaluation(recommendation=_recommendation(action=StrategyAction.INSUFFICIENT_DATA)),
        _evaluation(recommendation=_recommendation(confidence=0.2)),
        _evaluation(quality=Quality.INSUFFICIENT),
        _evaluation(quality=Quality.UNAVAILABLE),
    )

    for revision, evaluation in enumerate(cases, start=1):
        rule = StrategyPitRecommendedRule()
        observation = rule.observe(
            _inputs(
                strategy=_strategy(
                    _evaluation(
                        revision=revision,
                        recommendation=evaluation.recommendation,
                        quality=evaluation.data_quality,
                    )
                )
            )
        )[0]
        assert observation.active is not True


def test_stale_quality_does_not_open_and_is_reported_as_unknown() -> None:
    rule = StrategyPitRecommendedRule()

    observation = rule.observe(_inputs(strategy=_strategy(_evaluation(quality=Quality.STALE))))[0]

    assert observation.active is None
    assert observation.quality is Quality.STALE
    tracker = ConditionTracker(rule.policy)
    assert tracker.update(observation, now=10.0, lap=12) == ()


def test_unavailable_strategy_does_not_alert() -> None:
    rule = StrategyPitRecommendedRule()

    observation = rule.observe(_inputs(strategy=StrategySnapshot(updated_at_monotonic=3.0)))[0]

    assert observation.active is None
    assert _facts(observation)["action"] is None


def test_pit_lap_or_compound_change_updates_the_open_event() -> None:
    rule = StrategyPitRecommendedRule()
    tracker = ConditionTracker(rule.policy)
    for revision in (1, 2):
        tracker.update(
            rule.observe(_inputs(strategy=_strategy(_evaluation(revision=revision))))[0],
            now=10.0 + revision,
            lap=12,
        )

    changed = _strategy(
        _evaluation(revision=3, recommendation=_recommendation(pit_lap=13, compound="MEDIUM"))
    )
    transitions = tracker.update(rule.observe(_inputs(strategy=changed))[0], now=13.0, lap=12)

    assert [item.signal.phase for item in transitions] == [EventPhase.UPDATED]
    assert (
        _facts(
            rule.observe(
                _inputs(
                    strategy=_strategy(
                        _evaluation(
                            revision=4,
                            recommendation=_recommendation(pit_lap=13, compound="MEDIUM"),
                        )
                    )
                )
            )[0]
        )["compound"]
        == "MEDIUM"
    )


def test_gain_change_below_the_configured_delta_does_not_update() -> None:
    rule = StrategyPitRecommendedRule()
    tracker = ConditionTracker(rule.policy)
    for revision in (1, 2):
        tracker.update(
            rule.observe(_inputs(strategy=_strategy(_evaluation(revision=revision))))[0],
            now=10.0 + revision,
            lap=12,
        )

    small = _strategy(_evaluation(revision=3, recommendation=_recommendation(gain=5.0)))
    assert tracker.update(rule.observe(_inputs(strategy=small))[0], now=13.0, lap=12) == ()

    large = _strategy(_evaluation(revision=4, recommendation=_recommendation(gain=6.5)))
    transitions = tracker.update(rule.observe(_inputs(strategy=large))[0], now=14.0, lap=12)
    assert [item.signal.phase for item in transitions] == [EventPhase.UPDATED]


def test_returning_to_no_change_closes_the_condition_silently() -> None:
    rule = StrategyPitRecommendedRule()
    tracker = ConditionTracker(rule.policy)
    for revision in (1, 2):
        tracker.update(
            rule.observe(_inputs(strategy=_strategy(_evaluation(revision=revision))))[0],
            now=10.0 + revision,
            lap=12,
        )

    no_change = _strategy(
        _evaluation(revision=3, recommendation=_recommendation(action=StrategyAction.NO_CHANGE))
    )
    transitions = tracker.update(rule.observe(_inputs(strategy=no_change))[0], now=13.0, lap=12)

    assert transitions == ()
    assert (
        tracker.update(
            rule.observe(_inputs(strategy=_strategy(_evaluation(revision=4))))[0], now=14.0, lap=12
        )
        == ()
    )


def test_session_change_reobserves_the_same_revision() -> None:
    rule = StrategyPitRecommendedRule()

    assert rule.observe(_inputs(strategy=_strategy(_evaluation(revision=1, uid="a")))) != ()
    assert rule.observe(_inputs(strategy=_strategy(_evaluation(revision=1, uid="a")))) == ()
    assert rule.observe(_inputs(strategy=_strategy(_evaluation(revision=1, uid="b")))) != ()


def test_pit_window_policy_matches_the_matrix() -> None:
    policy = PitWindowOpenRule().policy

    assert policy.rule == "pit_window_open"
    assert policy.priority is EventPriority.INFORMATIONAL
    assert policy.message_key == "event.pit_window_open"
    assert policy.persistence_observations == 1
    assert policy.update_min_delta is None


def test_pit_window_near_far_past_and_missing() -> None:
    rule = PitWindowOpenRule()

    near = rule.observe(_inputs(state=_state(lap=14, session=_session(ideal=15, latest=17))))[0]
    far = rule.observe(_inputs(state=_state(lap=10, session=_session(uid=2, ideal=15, latest=17))))[
        0
    ]
    past = rule.observe(
        _inputs(state=_state(lap=18, session=_session(uid=3, ideal=15, latest=17)))
    )[0]
    missing = rule.observe(_inputs(state=_state(lap=12, session=_session(uid=4))))[0]

    assert near.active is True
    assert _facts(near) == {"ideal_lap": 15, "latest_lap": 17, "laps_to_ideal": 1}
    assert far.active is False
    assert past.active is False
    assert missing.active is None


def test_pit_window_lead_laps_is_configurable() -> None:
    rule = PitWindowOpenRule(EventsConfig(pit_window=PitWindowEventsConfig(lead_laps=3)))

    observation = rule.observe(
        _inputs(state=_state(lap=12, session=_session(ideal=15, latest=17)))
    )[0]

    assert observation.active is True


def test_pit_window_does_not_emit_updates_while_it_stays_open() -> None:
    rule = PitWindowOpenRule()
    tracker = ConditionTracker(rule.policy)

    first = rule.observe(_inputs(state=_state(lap=14, session=_session(ideal=15, latest=17))))[0]
    opened = tracker.update(first, now=10.0, lap=14)
    assert [item.signal.phase for item in opened] == [EventPhase.OPENED]

    later = rule.observe(_inputs(state=_state(lap=15, session=_session(ideal=15, latest=17))))[0]
    assert tracker.update(later, now=11.0, lap=15) == ()


def test_strategy_config_min_confidence_is_used() -> None:
    config = EventsConfig(strategy=StrategyEventsConfig(min_confidence=0.9))
    rule = StrategyPitRecommendedRule(config)

    observation = rule.observe(
        _inputs(
            config=config,
            strategy=_strategy(_evaluation(recommendation=_recommendation(confidence=0.85))),
        )
    )[0]

    assert observation.active is False
