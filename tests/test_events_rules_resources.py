"""Pruebas de las reglas de combustible y neumáticos (E5)."""

from __future__ import annotations

import pytest

from f1_strategist.config import EventsConfig, FuelEventsConfig, TyreEventsConfig
from f1_strategist.events.registry import EVENT_RULES
from f1_strategist.events.rules_resources import (
    FuelMarginCriticalRule,
    FuelMarginLowRule,
    TyreDegradationAbnormalRule,
    TyreLifeNearLimitRule,
)
from f1_strategist.events.tracker import ConditionTracker
from f1_strategist.events.types import EventInputs, EventPhase, EventPriority
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.race_state import (
    CarSnapshot,
    FittedTyreSnapshot,
    RaceSessionSnapshot,
    RaceStateSnapshot,
)
from f1_strategist.strategy.types import StrategySnapshot


def _session(
    *,
    uid: int | None = 1,
    total_laps: int | None = 57,
    started: bool | None = True,
    ended: bool | None = False,
    game_paused: bool | None = False,
    track_length_m: float | None = 5000.0,
) -> RaceSessionSnapshot:
    return RaceSessionSnapshot(
        uid=uid,
        session_type="race",
        track="test",
        total_laps=total_laps,
        started=started,
        ended=ended,
        track_length_m=track_length_m,
        game_paused=game_paused,
    )


def _state(
    *,
    lap: int | None = 12,
    age: int = 10,
    compound: str | None = "SOFT",
    lifespan: int = 20,
    session: RaceSessionSnapshot | None = None,
) -> RaceStateSnapshot:
    car = CarSnapshot(
        car_index=0,
        current_lap=lap,
        actual_tyre_compound=compound,
        tyre_age_laps=age,
        lap_distance_m=0.0,
        fitted_tyre=FittedTyreSnapshot(
            actual_tyre_compound=compound,
            visual_tyre_compound=compound,
            wear_pct=10.0,
            lifespan_laps=lifespan,
            usable_life_laps=lifespan,
        ),
    )
    return RaceStateSnapshot(
        session=session or _session(),
        current_lap=lap,
        player_car_index=0,
        cars=(car,),
        freshness=(),
        events=(),
    )


def _metric(
    key: str,
    value: float | int | str | None,
    *,
    quality: Quality = Quality.OK,
    unit: str = "",
    window: str = "instant",
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window(window),  # type: ignore[arg-type]
        quality=quality,
        sample_count=1,
        computed_at_monotonic=10.0,
    )


def _fuel_metrics(
    *,
    margin: float | None = -0.2,
    margin_quality: Quality = Quality.OK,
    margin_kg: float | None = 2.0,
    consumption: float | None = 2.5,
) -> MetricsSnapshot:
    return MetricsSnapshot(
        metrics={
            "fuel.margin_laps_at_finish": _metric(
                "fuel.margin_laps_at_finish",
                margin,
                quality=margin_quality,
                unit="lap",
            ),
            "fuel.margin_kg_at_finish": _metric(
                "fuel.margin_kg_at_finish",
                margin_kg,
                unit="kg",
            ),
            "fuel.consumption_per_lap": _metric(
                "fuel.consumption_per_lap",
                consumption,
                unit="kg/lap",
                window="laps",
            ),
        }
    )


def _tyre_metrics(
    *,
    age: int = 10,
    compound: str = "SOFT",
    wear_mean: float = 20.0,
    laps_to_limit: float | None = 2.0,
    lifespan: int = 20,
    rate: float | None = 0.075,
    rate_quality: Quality = Quality.OK,
    expected: float | None = 0.05,
) -> MetricsSnapshot:
    metrics: dict[str, Metric] = {
        "tyres.age_laps": _metric("tyres.age_laps", age, unit="lap"),
        "tyres.compound": _metric("tyres.compound", compound, unit="compound"),
        "tyres.wear_mean_pct": _metric("tyres.wear_mean_pct", wear_mean, unit="%"),
        "tyres.game_lifespan_laps": _metric("tyres.game_lifespan_laps", lifespan, unit="lap"),
    }
    if laps_to_limit is None:
        metrics["tyres.laps_to_wear_limit"] = _metric(
            "tyres.laps_to_wear_limit",
            None,
            quality=Quality.UNAVAILABLE,
            unit="lap",
            window="stint",
        )
    else:
        metrics["tyres.laps_to_wear_limit"] = _metric(
            "tyres.laps_to_wear_limit", laps_to_limit, unit="lap", window="stint"
        )
    if rate is None:
        metrics["tyres.wear_rate_pct_per_lap"] = _metric(
            "tyres.wear_rate_pct_per_lap",
            None,
            quality=Quality.INSUFFICIENT,
            unit="%/lap",
            window="stint",
        )
    else:
        metrics["tyres.wear_rate_pct_per_lap"] = _metric(
            "tyres.wear_rate_pct_per_lap",
            rate,
            quality=rate_quality,
            unit="%/lap",
            window="stint",
        )
    if expected is not None:
        metrics["profile.wear_rate_pct_per_lap"] = _metric(
            "profile.wear_rate_pct_per_lap", expected, quality=Quality.LOW, unit="%/lap"
        )
    return MetricsSnapshot(metrics=metrics)


def _inputs(
    *,
    metrics: MetricsSnapshot | None = None,
    state: RaceStateSnapshot | None = None,
    config: EventsConfig | None = None,
    now: float = 10.0,
    lap: int = 12,
) -> EventInputs:
    return EventInputs(
        state=state or _state(lap=lap),
        metrics=metrics or MetricsSnapshot(),
        strategy=StrategySnapshot(),
        config=config or EventsConfig(),
        now=now,
        lap=lap,
    )


def _facts(observation: object) -> dict[str, object]:
    return dict(observation.facts)  # type: ignore[attr-defined]


def test_fuel_and_tyre_rules_are_registered() -> None:
    names = {rule.policy.rule for rule in EVENT_RULES}

    assert {
        "fuel_margin_low",
        "fuel_margin_critical",
        "tyre_life_near_limit",
        "tyre_degradation_abnormal",
    } <= names


def test_fuel_policies_match_the_matrix() -> None:
    low = FuelMarginLowRule().policy
    critical = FuelMarginCriticalRule().policy

    assert (low.rule, low.priority, low.message_key) == (
        "fuel_margin_low",
        EventPriority.STRATEGIC,
        "event.fuel_margin_low",
    )
    assert (low.enter, low.exit, low.direction) == (0.5, 0.8, "below")
    assert low.persistence_observations == 2
    assert critical.rule == "fuel_margin_critical"
    assert critical.priority is EventPriority.CRITICAL
    assert (critical.enter, critical.exit, critical.direction) == (0.0, 0.3, "below")


def test_fuel_margin_low_opens_after_two_evaluations_with_facts() -> None:
    rule = FuelMarginLowRule()
    tracker = ConditionTracker(rule.policy)
    inputs = _inputs(metrics=_fuel_metrics())

    first = rule.observe(inputs)[0]
    second = rule.observe(inputs)[0]

    assert first.active is True
    assert tracker.update(first, now=10.0, lap=12) == ()
    opened = tracker.update(second, now=11.0, lap=12)
    assert [item.signal.phase for item in opened] == [EventPhase.OPENED]
    assert opened[0].signal.event_id == "fuel_margin_low:fuel_margin:1"
    assert _facts(opened[0].signal) == {
        "margin_laps": -0.2,
        "margin_kg": 2.0,
        "laps_remaining": 46.0,
        "consumption_per_lap": 2.5,
    }


def test_resource_rules_do_not_activate_at_warning_boundaries_or_close_old_stints() -> None:
    config = EventsConfig(
        fuel=FuelEventsConfig(warn_laps=0.5),
        tyres=TyreEventsConfig(warn_laps=3.0),
    )
    fuel = FuelMarginLowRule(config).observe(
        _inputs(metrics=_fuel_metrics(margin=0.5), config=config)
    )[0]
    tyre_boundary = TyreLifeNearLimitRule(config).observe(
        _inputs(
            metrics=_tyre_metrics(age=10, laps_to_limit=3.0, lifespan=30),
            state=_state(lap=20, age=10, lifespan=30),
            config=config,
            lap=20,
        )
    )[0]
    assert fuel.active is False
    assert tyre_boundary.active is False

    life_rule = TyreLifeNearLimitRule(config)
    old_life_key = life_rule.observe(
        _inputs(
            metrics=_tyre_metrics(age=10, lifespan=30),
            state=_state(lap=20, age=10, lifespan=30),
            config=config,
            lap=20,
        )
    )[0].key
    new_life = life_rule.observe(
        _inputs(
            metrics=_tyre_metrics(age=2, lifespan=30),
            state=_state(lap=30, age=2, lifespan=30, session=_session(uid=2)),
            config=config,
            lap=30,
        )
    )
    assert all(item.key != old_life_key or item.active is not False for item in new_life)

    degradation_rule = TyreDegradationAbnormalRule(config)
    old_degradation_key = degradation_rule.observe(
        _inputs(
            metrics=_tyre_metrics(age=10, lifespan=30),
            state=_state(lap=20, age=10, lifespan=30),
            config=config,
            lap=20,
        )
    )[0].key
    new_degradation = degradation_rule.observe(
        _inputs(
            metrics=_tyre_metrics(age=2, lifespan=30),
            state=_state(lap=30, age=2, lifespan=30, session=_session(uid=2)),
            config=config,
            lap=30,
        )
    )
    assert all(
        item.key != old_degradation_key or item.active is not False for item in new_degradation
    )


def test_fuel_margin_oscillates_with_hysteresis_and_recovers() -> None:
    config = EventsConfig(
        fuel=FuelEventsConfig(
            warn_laps=0.5,
            critical_laps=0.0,
            hysteresis_laps=0.3,
            update_delta_laps=0.2,
        )
    )
    rule = FuelMarginLowRule(config)
    tracker = ConditionTracker(rule.policy)

    tracker.update(
        rule.observe(_inputs(metrics=_fuel_metrics(margin=-0.1), config=config))[0], now=1.0, lap=10
    )
    tracker.update(
        rule.observe(_inputs(metrics=_fuel_metrics(margin=-0.1), config=config))[0], now=2.0, lap=10
    )

    # 0.6 sigue por debajo de la salida (0.8): no cierra y actualiza por delta.
    update = tracker.update(
        rule.observe(_inputs(metrics=_fuel_metrics(margin=0.6), config=config))[0],
        now=3.0,
        lap=11,
    )
    assert [item.signal.phase for item in update] == [EventPhase.UPDATED]

    # 0.9 supera la histéresis y cierra.
    assert (
        tracker.update(
            rule.observe(_inputs(metrics=_fuel_metrics(margin=0.9), config=config))[0],
            now=4.0,
            lap=12,
        )
        == ()
    )
    tracker.update(
        rule.observe(_inputs(metrics=_fuel_metrics(margin=-0.1), config=config))[0], now=5.0, lap=13
    )
    reopened = tracker.update(
        rule.observe(_inputs(metrics=_fuel_metrics(margin=-0.1), config=config))[0],
        now=6.0,
        lap=14,
    )
    assert reopened[0].signal.event_id == "fuel_margin_low:fuel_margin:2"


def test_fuel_rules_do_not_alert_with_insufficient_or_stale_data() -> None:
    for quality, margin in (
        (Quality.INSUFFICIENT, None),
        (Quality.STALE, 1.0),
    ):
        low = FuelMarginLowRule().observe(
            _inputs(metrics=_fuel_metrics(margin=margin, margin_quality=quality))
        )[0]
        critical = FuelMarginCriticalRule().observe(
            _inputs(metrics=_fuel_metrics(margin=margin, margin_quality=quality))
        )[0]
        assert low.active is None
        assert critical.active is None


def test_fuel_does_not_alert_when_session_is_not_running() -> None:
    for session in (
        _session(started=False),
        _session(ended=True),
        _session(game_paused=True),
    ):
        observation = FuelMarginLowRule().observe(
            _inputs(
                metrics=_fuel_metrics(),
                state=_state(session=session),
            )
        )[0]
        assert observation.active is None


def test_tyre_life_policy_matches_the_matrix() -> None:
    policy = TyreLifeNearLimitRule().policy

    assert policy.rule == "tyre_life_near_limit"
    assert policy.priority is EventPriority.STRATEGIC
    assert policy.message_key == "event.tyre_life_near_limit"
    assert policy.persistence_observations == 1
    assert policy.update_min_delta == 1.0


def test_tyre_life_opens_updates_and_does_not_warn_if_it_reaches_the_finish() -> None:
    config = EventsConfig(tyres=TyreEventsConfig(warn_laps=8.0))
    rule = TyreLifeNearLimitRule(config)
    tracker = ConditionTracker(rule.policy)

    first = rule.observe(
        _inputs(
            metrics=_tyre_metrics(age=18, laps_to_limit=2.0, lifespan=20),
            state=_state(lap=20, age=18, lifespan=20),
            lap=20,
        )
    )[0]
    opened = tracker.update(first, now=10.0, lap=20)
    assert [item.signal.phase for item in opened] == [EventPhase.OPENED]
    assert _facts(opened[0].signal) == {
        "compound": "SOFT",
        "age_laps": 18.0,
        "wear_mean_pct": 20.0,
        "laps_left": 2.0,
    }

    second = rule.observe(
        _inputs(
            metrics=_tyre_metrics(age=19, laps_to_limit=1.0, lifespan=20),
            state=_state(lap=21, age=19, lifespan=20),
            lap=21,
        )
    )[0]
    updated = tracker.update(second, now=11.0, lap=21)
    assert [item.signal.phase for item in updated] == [EventPhase.UPDATED]
    assert _facts(updated[0].signal)["laps_left"] == 1.0

    reaches_finish = TyreLifeNearLimitRule(config).observe(
        _inputs(
            metrics=_tyre_metrics(age=48, laps_to_limit=6.0, lifespan=60),
            state=_state(lap=52, age=48, lifespan=60),
            config=config,
            lap=52,
        )
    )[0]
    assert reaches_finish.active is False

    earlier_race = TyreLifeNearLimitRule(config).observe(
        _inputs(
            metrics=_tyre_metrics(age=48, laps_to_limit=6.0, lifespan=60),
            state=_state(lap=48, age=48, lifespan=60),
            config=config,
            lap=48,
        )
    )[0]
    assert earlier_race.active is True


def test_tyre_life_change_emits_a_close_for_the_previous_tyre() -> None:
    rule = TyreLifeNearLimitRule()
    first = rule.observe(
        _inputs(
            metrics=_tyre_metrics(age=18, laps_to_limit=2.0, lifespan=20),
            state=_state(lap=20, age=18, lifespan=20),
            lap=20,
        )
    )[0]
    assert first.key == "tyre_life_2"

    observations = rule.observe(
        _inputs(
            metrics=_tyre_metrics(
                age=1,
                compound="MEDIUM",
                laps_to_limit=15.0,
                lifespan=20,
            ),
            state=_state(lap=21, age=1, compound="MEDIUM", lifespan=20),
            lap=21,
        )
    )

    assert observations[0].key == "tyre_life_2"
    assert observations[0].active is False
    assert observations[1].key == "tyre_life_20"
    assert observations[1].active is False


def test_tyre_life_does_not_alert_early_without_samples() -> None:
    observation = TyreLifeNearLimitRule().observe(
        _inputs(
            metrics=_tyre_metrics(age=1, laps_to_limit=None, lifespan=20),
            state=_state(lap=1, age=1, lifespan=20),
            lap=1,
        )
    )[0]

    # Sin límite de desgaste usable, la vida del juego es el único límite conocido.
    assert observation.active is False


def test_tyre_life_without_data_stays_inactive() -> None:
    observation = TyreLifeNearLimitRule().observe(
        _inputs(
            metrics=MetricsSnapshot(),
            state=_state(lap=1, age=1, lifespan=1),
            lap=1,
        )
    )[0]

    assert observation.active is None


def test_tyre_degradation_policy_matches_the_matrix() -> None:
    policy = TyreDegradationAbnormalRule().policy

    assert policy.rule == "tyre_degradation_abnormal"
    assert policy.priority is EventPriority.INFORMATIONAL
    assert policy.message_key == "event.tyre_degradation_abnormal"
    assert policy.persistence_observations == 2
    assert policy.enter == 1.25
    assert policy.exit == 1.15
    assert policy.direction == "above"


def test_tyre_degradation_opens_after_two_evaluations_with_profile() -> None:
    rule = TyreDegradationAbnormalRule()
    tracker = ConditionTracker(rule.policy)
    inputs = _inputs(
        metrics=_tyre_metrics(age=10, rate=0.075, expected=0.05),
        state=_state(lap=20, age=10),
        lap=20,
    )

    first = rule.observe(inputs)[0]
    second = rule.observe(inputs)[0]
    assert first.active is True
    assert tracker.update(first, now=10.0, lap=20) == ()
    opened = tracker.update(second, now=11.0, lap=20)

    assert [item.signal.phase for item in opened] == [EventPhase.OPENED]
    assert _facts(opened[0].signal) == {
        "wear_rate_pct_per_lap": 0.075,
        "expected_rate_pct_per_lap": 0.05,
        "ratio": pytest.approx(1.5),
        "pace_trend_s_per_lap": None,
    }


def test_tyre_degradation_does_not_alert_without_profile_or_samples() -> None:
    without_profile = TyreDegradationAbnormalRule().observe(
        _inputs(
            metrics=_tyre_metrics(age=10, rate=0.20, expected=None),
            state=_state(lap=20, age=10),
            lap=20,
        )
    )[0]
    few_samples = TyreDegradationAbnormalRule().observe(
        _inputs(
            metrics=_tyre_metrics(age=10, rate=None, expected=0.05),
            state=_state(lap=20, age=10),
            lap=20,
        )
    )[0]
    early_stint = TyreDegradationAbnormalRule().observe(
        _inputs(
            metrics=_tyre_metrics(age=3, rate=0.20, expected=0.05),
            state=_state(lap=3, age=3),
            lap=3,
        )
    )[0]

    assert without_profile.active is None
    assert few_samples.active is None
    assert early_stint.active is None


def test_tyre_degradation_change_closes_the_previous_stint() -> None:
    rule = TyreDegradationAbnormalRule()
    rule.observe(
        _inputs(
            metrics=_tyre_metrics(age=10, rate=0.10, expected=0.05),
            state=_state(lap=20, age=10),
            lap=20,
        )
    )

    observations = rule.observe(
        _inputs(
            metrics=_tyre_metrics(
                age=1,
                compound="MEDIUM",
                rate=0.02,
                expected=0.05,
            ),
            state=_state(lap=21, age=1, compound="MEDIUM"),
            lap=21,
        )
    )

    assert observations[0].key == "tyre_degradation_10"
    assert observations[0].active is False
    assert observations[0].value == 0.0
    assert observations[1].key == "tyre_degradation_20"
    assert observations[1].active is None
