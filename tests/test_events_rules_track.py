"""Pruebas de las reglas de pista, clima y datos obsoletos (E6)."""

from __future__ import annotations

from f1_strategist.config import DataEventsConfig, EventsConfig, WeatherEventsConfig
from f1_strategist.events.registry import EVENT_RULES
from f1_strategist.events.rules_track import (
    DataStaleRule,
    RainExpectedRule,
    RedFlagRule,
    SafetyCarClearedRule,
    SafetyCarDeployedRule,
    VirtualSafetyCarDeployedRule,
    WeatherChangedRule,
)
from f1_strategist.events.tracker import ConditionTracker, ConditionTransition
from f1_strategist.events.types import EventInputs, EventPhase, EventPriority, EventRule
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.types import Metric, Quality, Window
from f1_strategist.race_state import (
    PacketFreshness,
    RaceEvent,
    RaceSessionSnapshot,
    RaceStateSnapshot,
)
from f1_strategist.strategy.types import StrategySnapshot

_RAIN_KEY = "env.rain_probability_next_10_min_pct"


def _session(
    *,
    weather: str | None = "clear",
    safety_car_status: str | None = "no_safety_car",
    started: bool | None = True,
    ended: bool | None = False,
    game_paused: bool | None = False,
    track_temperature_c: float | None = 30.0,
    air_temperature_c: float | None = 22.0,
) -> RaceSessionSnapshot:
    return RaceSessionSnapshot(
        uid=1,
        session_type="race",
        track="test",
        total_laps=57,
        started=started,
        ended=ended,
        weather=weather,
        safety_car_status=safety_car_status,
        track_temperature_c=track_temperature_c,
        air_temperature_c=air_temperature_c,
        track_length_m=5000.0,
        game_paused=game_paused,
    )


def _state(
    *,
    session: RaceSessionSnapshot | None = None,
    freshness: tuple[PacketFreshness, ...] = (),
) -> RaceStateSnapshot:
    return RaceStateSnapshot(
        session=session or _session(),
        current_lap=12,
        player_car_index=0,
        cars=(),
        freshness=freshness,
        events=(),
    )


def _metric(
    key: str,
    value: float | int | str | None,
    *,
    quality: Quality = Quality.OK,
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit="pct",
        window=Window("instant"),
        quality=quality,
        sample_count=1,
        computed_at_monotonic=10.0,
    )


def _inputs(
    *,
    state: RaceStateSnapshot | None = None,
    metrics: MetricsSnapshot | None = None,
    new_state_events: tuple[RaceEvent, ...] = (),
    now: float = 10.0,
    lap: int = 12,
    config: EventsConfig | None = None,
) -> EventInputs:
    return EventInputs(
        state=state or _state(),
        metrics=metrics or MetricsSnapshot(),
        strategy=StrategySnapshot(),
        config=config or EventsConfig(),
        now=now,
        lap=lap,
        new_state_events=new_state_events,
    )


def _update(
    tracker: ConditionTracker,
    rule: EventRule,
    inputs: EventInputs,
    *,
    now: float,
) -> list[ConditionTransition]:
    transitions: list[ConditionTransition] = []
    for observation in rule.observe(inputs):
        transitions.extend(tracker.update(observation, now=now, lap=inputs.lap))
    return transitions


def _safety_car_event(value: str, previous: str) -> RaceEvent:
    return RaceEvent(
        kind="safety_car_changed",
        occurred_at_monotonic=10.0,
        value=value,
        previous_value=previous,
    )


def _weather_event(value: str, previous: str) -> RaceEvent:
    return RaceEvent(
        kind="weather_changed",
        occurred_at_monotonic=10.0,
        value=value,
        previous_value=previous,
    )


def _red_flag_event() -> RaceEvent:
    return RaceEvent(kind="red_flag", occurred_at_monotonic=10.0)


def test_event_rules_are_registered() -> None:
    expected = (
        SafetyCarDeployedRule,
        VirtualSafetyCarDeployedRule,
        SafetyCarClearedRule,
        RedFlagRule,
        WeatherChangedRule,
        RainExpectedRule,
        DataStaleRule,
    )

    for rule_type in expected:
        assert any(isinstance(rule, rule_type) for rule in EVENT_RULES)


def test_policies_match_event_matrix() -> None:
    deployed = SafetyCarDeployedRule().policy
    assert deployed.rule == "safety_car_deployed"
    assert deployed.category == "track"
    assert deployed.priority is EventPriority.CRITICAL
    assert deployed.message_key == "event.safety_car_deployed"

    virtual = VirtualSafetyCarDeployedRule().policy
    assert virtual.rule == "virtual_safety_car_deployed"
    assert virtual.priority is EventPriority.CRITICAL

    cleared = SafetyCarClearedRule().policy
    assert cleared.rule == "safety_car_cleared"
    assert cleared.priority is EventPriority.STRATEGIC
    assert cleared.one_shot is True

    red_flag = RedFlagRule().policy
    assert red_flag.rule == "red_flag"
    assert red_flag.priority is EventPriority.CRITICAL
    assert red_flag.one_shot is True

    weather = WeatherChangedRule().policy
    assert weather.rule == "weather_changed"
    assert weather.category == "weather"
    assert weather.one_shot is True

    rain = RainExpectedRule().policy
    assert rain.priority is EventPriority.STRATEGIC
    assert rain.persistence_observations == 2
    assert rain.enter == 50.0
    assert rain.exit == 30.0
    assert rain.direction == "above"

    stale = DataStaleRule().policy
    assert stale.rule == "data_stale"
    assert stale.priority is EventPriority.INFORMATIONAL
    assert stale.persistence_s == 3.0


def test_safety_car_alerts_once_when_deployed_and_once_when_cleared() -> None:
    deployed_rule = SafetyCarDeployedRule()
    cleared_rule = SafetyCarClearedRule()
    deployed_tracker = ConditionTracker(deployed_rule.policy)
    cleared_tracker = ConditionTracker(cleared_rule.policy)

    under_sc = _state(session=_session(safety_car_status="full_safety_car"))

    # Al desplegarse solo sale un aviso, aunque se evalúe varias veces.
    opened = _update(
        deployed_tracker,
        deployed_rule,
        _inputs(state=under_sc),
        now=10.0,
    )
    assert len(opened) == 1
    assert opened[0].signal.phase is EventPhase.OPENED
    assert opened[0].signal.priority is EventPriority.CRITICAL
    assert opened[0].signal.event_id == "safety_car_deployed:safety_car_deployed:1"
    assert dict(opened[0].signal.facts) == {"lap": 12}

    repeated = _update(
        deployed_tracker,
        deployed_rule,
        _inputs(state=under_sc, lap=13),
        now=11.0,
    )
    assert repeated == []

    green = _state(session=_session(safety_car_status="no_safety_car"))
    closing = _update(
        deployed_tracker,
        deployed_rule,
        _inputs(state=green, lap=14),
        now=12.0,
    )
    # El cierre del despliegue es silencioso; solo informa safety_car_cleared.
    assert closing == []

    cleared = _update(
        cleared_tracker,
        cleared_rule,
        _inputs(
            state=green,
            lap=14,
            new_state_events=(_safety_car_event("no_safety_car", "full_safety_car"),),
        ),
        now=12.0,
    )
    assert len(cleared) == 1
    assert cleared[0].signal.phase is EventPhase.OPENED
    assert cleared[0].signal.priority is EventPriority.STRATEGIC
    assert cleared[0].signal.event_id == "safety_car_cleared:safety_car_cleared:1"
    assert dict(cleared[0].signal.facts) == {"lap": 14}

    # La retirada también es un único aviso.
    after = _update(
        cleared_tracker,
        cleared_rule,
        _inputs(state=green, lap=15),
        now=13.0,
    )
    assert after == []
    assert len(opened) + len(cleared) == 2, (
        "un safety car debe producir exactamente un aviso al salir y otro al retirarse"
    )


def test_virtual_safety_car_uses_its_own_rule_and_clear_event() -> None:
    rule = VirtualSafetyCarDeployedRule()
    tracker = ConditionTracker(rule.policy)

    under_vsc = _state(session=_session(safety_car_status="virtual_safety_car"))
    opened = _update(tracker, rule, _inputs(state=under_vsc), now=10.0)
    assert len(opened) == 1
    assert opened[0].signal.event_id == "virtual_safety_car_deployed:virtual_safety_car_deployed:1"

    # El safety car completo no dispara la regla de VSC.
    assert (
        _update(
            tracker,
            rule,
            _inputs(
                state=_state(session=_session(safety_car_status="full_safety_car")),
                lap=13,
            ),
            now=11.0,
        )
        == []
    )

    cleared_rule = SafetyCarClearedRule()
    cleared = _update(
        ConditionTracker(cleared_rule.policy),
        cleared_rule,
        _inputs(
            state=_state(session=_session(safety_car_status="no_safety_car")),
            new_state_events=(_safety_car_event("no_safety_car", "virtual_safety_car"),),
        ),
        now=12.0,
    )
    assert len(cleared) == 1
    assert cleared[0].signal.priority is EventPriority.STRATEGIC


def test_safety_car_rules_are_suppressed_without_a_running_session() -> None:
    rule = SafetyCarDeployedRule()
    for session in (
        _session(started=False),
        _session(ended=True),
        _session(game_paused=True),
    ):
        observation = rule.observe(_inputs(state=_state(session=session)))
        assert observation[0].active is None


def test_red_flag_alerts_once_per_period_event() -> None:
    rule = RedFlagRule()
    tracker = ConditionTracker(rule.policy)

    first = _update(
        tracker,
        rule,
        _inputs(new_state_events=(_red_flag_event(),)),
        now=10.0,
    )
    assert len(first) == 1
    assert first[0].signal.priority is EventPriority.CRITICAL
    assert first[0].signal.event_id == "red_flag:red_flag:1"
    assert dict(first[0].signal.facts) == {"lap": 12}

    # Sin un evento nuevo no se repite el aviso.
    assert _update(tracker, rule, _inputs(lap=13), now=11.0) == []

    second = _update(
        tracker,
        rule,
        _inputs(lap=20, new_state_events=(_red_flag_event(),)),
        now=20.0,
    )
    assert len(second) == 1
    assert second[0].signal.event_id == "red_flag:red_flag:2"


def test_weather_change_only_alerts_on_transition_and_prioritises_rain() -> None:
    rule = WeatherChangedRule()
    tracker = ConditionTracker(rule.policy)

    dry_to_wet = _update(
        tracker,
        rule,
        _inputs(new_state_events=(_weather_event("light_rain", "clear"),)),
        now=10.0,
    )
    assert len(dry_to_wet) == 1
    assert dry_to_wet[0].signal.priority is EventPriority.STRATEGIC
    assert dict(dry_to_wet[0].signal.facts) == {
        "air_temperature_c": 22.0,
        "current": "light_rain",
        "previous": "clear",
        "track_temperature_c": 30.0,
    }

    # Una evaluación sin cambio no genera aviso.
    assert _update(tracker, rule, _inputs(lap=13), now=11.0) == []

    wet_to_dry = _update(
        tracker,
        rule,
        _inputs(lap=14, new_state_events=(_weather_event("overcast", "heavy_rain"),)),
        now=12.0,
    )
    assert len(wet_to_dry) == 1
    assert wet_to_dry[0].signal.priority is EventPriority.STRATEGIC


def test_weather_change_within_the_same_regime_is_informational() -> None:
    rule = WeatherChangedRule()
    tracker = ConditionTracker(rule.policy)

    signals = _update(
        tracker,
        rule,
        _inputs(new_state_events=(_weather_event("overcast", "clear"),)),
        now=10.0,
    )
    assert len(signals) == 1
    assert signals[0].signal.priority is EventPriority.INFORMATIONAL


def test_rain_expected_needs_two_observations_and_uses_hysteresis() -> None:
    config = EventsConfig(
        weather=WeatherEventsConfig(
            rain_enter_pct=50.0,
            rain_exit_pct=30.0,
            update_delta_pct=10.0,
        )
    )
    rule = RainExpectedRule(config)
    tracker = ConditionTracker(rule.policy)

    def inputs(probability: float, *, lap: int) -> EventInputs:
        metrics = MetricsSnapshot({_RAIN_KEY: _metric(_RAIN_KEY, probability)})
        return _inputs(metrics=metrics, lap=lap, config=config)

    first = _update(tracker, rule, inputs(60.0, lap=12), now=10.0)
    assert first == []

    second = _update(tracker, rule, inputs(60.0, lap=13), now=11.0)
    assert len(second) == 1
    assert second[0].signal.priority is EventPriority.STRATEGIC
    assert second[0].signal.event_id == "rain_expected:rain_expected:1"
    assert dict(second[0].signal.facts) == {"horizon_min": 10, "probability_pct": 60.0}

    # Una variación menor que update_delta_pct no actualiza.
    assert _update(tracker, rule, inputs(65.0, lap=14), now=12.0) == []

    updated = _update(tracker, rule, inputs(75.0, lap=15), now=13.0)
    assert len(updated) == 1
    assert updated[0].signal.phase is EventPhase.UPDATED
    assert dict(updated[0].signal.facts)["probability_pct"] == 75.0

    # Por encima de exit pero por debajo de enter no reabre ni cierra.
    assert _update(tracker, rule, inputs(70.0, lap=16), now=14.0) == []

    closed = _update(tracker, rule, inputs(20.0, lap=17), now=15.0)
    assert closed == []


def test_rain_expected_ignores_unusable_or_unknown_probability() -> None:
    rule = RainExpectedRule()

    for metric in (
        _metric(_RAIN_KEY, 80.0, quality=Quality.STALE),
        _metric(_RAIN_KEY, None, quality=Quality.UNAVAILABLE),
        _metric(_RAIN_KEY, None, quality=Quality.INSUFFICIENT),
    ):
        observation = rule.observe(_inputs(metrics=MetricsSnapshot({_RAIN_KEY: metric})))
        assert observation[0].active is None


def test_data_stale_requires_persistence_and_reports_group_and_age() -> None:
    config = EventsConfig(data=DataEventsConfig(stale_persistence_s=3.0))
    rule = DataStaleRule(config)
    tracker = ConditionTracker(rule.policy)
    freshness = PacketFreshness(
        group="session",
        updated_at_monotonic=0.0,
        stale_after_seconds=2.0,
    )
    state = _state(freshness=(freshness,))

    first = _update(
        tracker,
        rule,
        _inputs(state=state, lap=12, config=config, now=10.0),
        now=10.0,
    )
    assert first == []

    almost = _update(
        tracker,
        rule,
        _inputs(state=state, lap=13, config=config, now=12.9),
        now=12.9,
    )
    assert almost == []

    opened = _update(
        tracker,
        rule,
        _inputs(state=state, lap=14, config=config, now=13.0),
        now=13.0,
    )
    assert len(opened) == 1
    assert opened[0].signal.priority is EventPriority.INFORMATIONAL
    assert opened[0].signal.event_id == "data_stale:data_stale_session:1"
    assert dict(opened[0].signal.facts) == {"age_s": 13.0, "group": "session"}

    # Los datos frescos cierran la condición sin aviso.
    fresh = PacketFreshness(
        group="session",
        updated_at_monotonic=20.0,
        stale_after_seconds=2.0,
    )
    assert (
        _update(
            tracker,
            rule,
            _inputs(state=_state(freshness=(fresh,)), lap=15, config=config, now=21.0),
            now=21.0,
        )
        == []
    )


def test_data_stale_is_suppressed_while_paused_or_outside_the_session() -> None:
    rule = DataStaleRule()
    freshness = PacketFreshness(
        group="session",
        updated_at_monotonic=0.0,
        stale_after_seconds=2.0,
    )

    for session in (
        _session(game_paused=True),
        _session(ended=True),
        _session(started=False),
    ):
        state = _state(session=session, freshness=(freshness,))
        observation = rule.observe(_inputs(state=state, now=100.0))
        assert observation[0].active is None


def test_data_stale_ignores_non_critical_groups_and_groups_without_updates() -> None:
    rule = DataStaleRule()
    non_critical = PacketFreshness(
        group="tyre_sets",
        updated_at_monotonic=0.0,
        stale_after_seconds=2.0,
    )
    never_updated = PacketFreshness(
        group="session",
        updated_at_monotonic=None,
        stale_after_seconds=2.0,
    )

    assert rule.observe(_inputs(state=_state(freshness=(non_critical,)), now=100.0)) == ()

    observations = rule.observe(_inputs(state=_state(freshness=(never_updated,)), now=100.0))
    assert len(observations) == 1
    assert observations[0].active is None


def test_data_stale_emits_one_observation_per_critical_group() -> None:
    rule = DataStaleRule()
    freshness = (
        PacketFreshness("lap_data", 0.0, 1.5),
        PacketFreshness("session", 0.0, 2.5),
        PacketFreshness("participants", 0.0, 12.0),
    )

    observations = rule.observe(_inputs(state=_state(freshness=freshness), now=100.0))
    assert {observation.key for observation in observations} == {
        "data_stale_lap_data",
        "data_stale_participants",
        "data_stale_session",
    }
