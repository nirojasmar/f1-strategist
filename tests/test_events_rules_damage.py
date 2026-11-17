"""Pruebas de las reglas de daños y paradas de rivales (E7)."""

from __future__ import annotations

from f1_strategist.config import DamageEventsConfig, EventsConfig
from f1_strategist.events.registry import EVENT_RULES
from f1_strategist.events.rules_damage import DamageDetectedRule, RivalPitStopRule
from f1_strategist.events.tracker import ConditionTracker, ConditionTransition
from f1_strategist.events.types import (
    EventInputs,
    EventPhase,
    EventPriority,
    Observation,
)
from f1_strategist.metrics.engine import MetricsSnapshot
from f1_strategist.metrics.types import Metric, MetricValue, Quality, Window
from f1_strategist.race_state import (
    CarDamageSnapshot,
    CarSnapshot,
    PacketFreshness,
    RaceEvent,
    RaceSessionSnapshot,
    RaceStateSnapshot,
)
from f1_strategist.strategy.types import StrategySnapshot


def _session(
    *,
    uid: int | None = 1,
    started: bool | None = True,
    ended: bool | None = False,
    game_paused: bool | None = False,
) -> RaceSessionSnapshot:
    return RaceSessionSnapshot(
        uid=uid,
        session_type="race",
        track="test",
        total_laps=57,
        started=started,
        ended=ended,
        track_length_m=5000.0,
        game_paused=game_paused,
    )


def _damage(
    *,
    wing: int = 0,
    floor: int = 0,
    gearbox: int = 0,
    tyres: tuple[int, ...] = (0, 0, 0, 0),
    drs: str | None = None,
    ers: str | None = None,
    engine_blown: str | None = None,
    engine_seized: str | None = None,
) -> CarDamageSnapshot:
    return CarDamageSnapshot(
        tyres_wear=(0.0, 0.0, 0.0, 0.0),
        tyres_damage=tyres,
        brakes_damage=(0, 0, 0, 0),
        tyre_blisters=(0, 0, 0, 0),
        front_left_wing_damage=wing,
        front_right_wing_damage=0,
        rear_wing_damage=0,
        floor_damage=floor,
        diffuser_damage=0,
        sidepod_damage=0,
        drs_fault=drs,
        ers_fault=ers,
        gearbox_damage=gearbox,
        engine_damage=0,
        engine_mguh_wear=0,
        engine_es_wear=0,
        engine_ce_wear=0,
        engine_ice_wear=0,
        engine_mguk_wear=0,
        engine_tc_wear=0,
        engine_blown=engine_blown,
        engine_seized=engine_seized,
    )


def _car(
    index: int = 0,
    *,
    name: str | None = None,
    position: int | None = None,
    delta_ms: int = 0,
    result_status: str | None = None,
    driver_status: str | None = None,
    damage: CarDamageSnapshot | None = None,
) -> CarSnapshot:
    return CarSnapshot(
        car_index=index,
        name=name,
        position=position,
        delta_to_leader_ms=delta_ms,
        result_status=result_status,
        driver_status=driver_status,
        damage=damage,
    )


def _state(
    *,
    cars: tuple[CarSnapshot, ...] | None = None,
    session: RaceSessionSnapshot | None = None,
    freshness: tuple[PacketFreshness, ...] = (),
) -> RaceStateSnapshot:
    return RaceStateSnapshot(
        session=session or _session(),
        current_lap=12,
        player_car_index=0,
        cars=cars if cars is not None else (_car(),),
        freshness=freshness,
        events=(),
    )


def _metric(
    key: str,
    value: MetricValue,
    *,
    quality: Quality = Quality.OK,
    unit: str = "%",
) -> Metric:
    return Metric(
        key=key,
        value=value,
        unit=unit,
        window=Window("instant"),
        quality=quality,
        sample_count=1,
        computed_at_monotonic=10.0,
    )


def _damage_metrics(
    *,
    wing: float = 20.0,
    floor: float = 5.0,
    gearbox: float = 0.0,
    faults: tuple[str, ...] | None = (),
    quality: Quality = Quality.OK,
    faults_quality: Quality | None = None,
) -> MetricsSnapshot:
    metrics = {
        "damage.aero_max_pct": _metric("damage.aero_max_pct", wing, quality=quality),
        "damage.floor_pct": _metric("damage.floor_pct", floor, quality=quality),
        "damage.gearbox_pct": _metric("damage.gearbox_pct", gearbox, quality=quality),
    }
    fault_quality = faults_quality if faults_quality is not None else quality
    if faults is None:
        metrics["damage.faults"] = _metric(
            "damage.faults",
            None,
            quality=Quality.UNAVAILABLE,
            unit="faults",
        )
    else:
        metrics["damage.faults"] = _metric(
            "damage.faults",
            faults,
            quality=fault_quality,
            unit="faults",
        )
    return MetricsSnapshot(metrics=metrics)


def _inputs(
    *,
    metrics: MetricsSnapshot | None = None,
    state: RaceStateSnapshot | None = None,
    config: EventsConfig | None = None,
    now: float = 10.0,
    lap: int = 12,
    new_state_events: tuple[RaceEvent, ...] = (),
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


def _observations(
    rule: DamageDetectedRule | RivalPitStopRule,
    inputs: EventInputs,
) -> dict[str, Observation]:
    return {observation.key: observation for observation in rule.observe(inputs)}


def _update(
    tracker: ConditionTracker,
    rule: DamageDetectedRule | RivalPitStopRule,
    inputs: EventInputs,
    now: float,
    *,
    include_silent_closures: bool = False,
) -> tuple[ConditionTransition, ...]:
    transitions: list[ConditionTransition] = []
    for observation in rule.observe(inputs):
        transitions.extend(
            tracker.update(
                observation,
                now=now,
                lap=inputs.lap,
                include_silent_closures=include_silent_closures,
            )
        )
    return tuple(transitions)


def _pit_entry(car_index: int = 1, *, lap: int = 20) -> RaceEvent:
    return RaceEvent(
        kind="pit_entry",
        occurred_at_monotonic=10.0,
        car_index=car_index,
        lap_number=lap,
    )


def _rival_state(
    *,
    gap_ms: int = 1500,
    result_status: str | None = None,
    driver_status: str | None = None,
    session: RaceSessionSnapshot | None = None,
    freshness: tuple[PacketFreshness, ...] = (),
) -> RaceStateSnapshot:
    cars = (
        _car(0, name="Player", position=5, delta_ms=0),
        _car(
            1,
            name="Rival",
            position=6,
            delta_ms=gap_ms,
            result_status=result_status,
            driver_status=driver_status,
        ),
    )
    return _state(cars=cars, session=session, freshness=freshness)


def test_damage_and_rival_rules_are_registered() -> None:
    names = {rule.policy.rule for rule in EVENT_RULES}

    assert {"damage_detected", "rival_pit_stop"} <= names


def test_policies_match_the_matrix() -> None:
    damage = DamageDetectedRule().policy
    rival = RivalPitStopRule().policy

    assert (damage.rule, damage.category, damage.priority) == (
        "damage_detected",
        "damage",
        EventPriority.STRATEGIC,
    )
    assert damage.message_key == "event.damage_detected"
    assert damage.persistence_observations == 1
    assert damage.update_min_delta == 10.0
    assert damage.one_shot is False

    assert (rival.rule, rival.category, rival.priority) == (
        "rival_pit_stop",
        "rivals",
        EventPriority.INFORMATIONAL,
    )
    assert rival.message_key == "event.rival_pit_stop"
    assert rival.one_shot is True


def test_damage_opens_per_component_with_facts() -> None:
    rule = DamageDetectedRule()
    tracker = ConditionTracker(rule.policy)

    transitions = _update(
        tracker,
        rule,
        _inputs(metrics=_damage_metrics(wing=20.0, floor=5.0)),
        10.0,
    )

    assert [item.signal.phase for item in transitions] == [EventPhase.OPENED]
    signal = transitions[0].signal
    assert signal.event_id == "damage_detected:damage_wing:1"
    assert dict(signal.facts) == {
        "component": "wing",
        "damage_pct": 20.0,
        "delta_pct": 0.0,
    }


def test_announced_damage_only_updates_when_it_worsens_enough() -> None:
    rule = DamageDetectedRule()
    tracker = ConditionTracker(rule.policy)

    opened = _update(tracker, rule, _inputs(metrics=_damage_metrics(wing=20.0)), 10.0)
    assert [item.signal.phase for item in opened] == [EventPhase.OPENED]

    # Cinco puntos más no alcanzan el umbral de actualización.
    assert _update(tracker, rule, _inputs(metrics=_damage_metrics(wing=25.0)), 11.0) == ()

    updated = _update(tracker, rule, _inputs(metrics=_damage_metrics(wing=30.0)), 12.0)
    assert [item.signal.phase for item in updated] == [EventPhase.UPDATED]
    assert dict(updated[0].signal.facts) == {
        "component": "wing",
        "damage_pct": 30.0,
        "delta_pct": 10.0,
    }

    assert _update(tracker, rule, _inputs(metrics=_damage_metrics(wing=35.0)), 13.0) == ()
    updated = _update(tracker, rule, _inputs(metrics=_damage_metrics(wing=40.0)), 14.0)
    assert dict(updated[0].signal.facts)["damage_pct"] == 40.0
    assert updated[0].signal.event_id == opened[0].signal.event_id


def test_engine_failure_is_critical_and_other_faults_are_strategic() -> None:
    rule = DamageDetectedRule()
    observations = _observations(
        rule,
        _inputs(metrics=_damage_metrics(wing=0.0, floor=0.0, faults=("DRS", "motor"))),
    )

    engine = observations["damage_fault_engine"]
    drs = observations["damage_fault_drs"]
    assert engine.active is True
    assert engine.priority is EventPriority.CRITICAL
    assert dict(engine.facts)["component"] == "engine"
    assert drs.active is True
    assert drs.priority is EventPriority.STRATEGIC

    tracker = ConditionTracker(rule.policy)
    opened = tracker.update(engine, now=10.0, lap=12)
    assert [item.signal.phase for item in opened] == [EventPhase.OPENED]
    assert opened[0].signal.priority is EventPriority.CRITICAL


def test_tyre_damage_uses_the_player_car_and_its_maximum() -> None:
    rule = DamageDetectedRule()
    car = _car(damage=_damage(tyres=(10, 22, 5, 5)))

    observations = _observations(rule, _inputs(state=_state(cars=(car,))))
    tyres = observations["damage_tyres"]

    assert tyres.active is True
    assert tyres.value == 22.0
    assert dict(tyres.facts) == {
        "component": "tyres",
        "damage_pct": 22.0,
        "delta_pct": 0.0,
    }


def test_damage_does_not_alert_with_stale_data_or_outside_the_session() -> None:
    rule = DamageDetectedRule()

    stale = _observations(rule, _inputs(metrics=_damage_metrics(quality=Quality.STALE)))
    assert stale["damage_wing"].active is None

    missing = _observations(rule, _inputs(metrics=MetricsSnapshot()))
    assert missing["damage_wing"].active is None

    paused = _observations(
        rule,
        _inputs(state=_state(session=_session(game_paused=True))),
    )
    assert all(observation.active is None for observation in paused.values())

    car = _car(damage=_damage(tyres=(30, 0, 0, 0)))
    freshness = (PacketFreshness("car_damage", 0.0, 1.0),)
    tyres = _observations(
        rule,
        _inputs(state=_state(cars=(car,), freshness=freshness)),
    )
    assert tyres["damage_tyres"].active is None


def test_damage_at_exact_warning_threshold_is_inactive() -> None:
    config = EventsConfig(damage=DamageEventsConfig(warn_pct=15.0))
    observations = _observations(
        DamageDetectedRule(config),
        _inputs(metrics=_damage_metrics(wing=15.0), config=config),
    )

    assert observations["damage_wing"].active is False


def test_damage_memory_resets_with_a_new_session() -> None:
    rule = DamageDetectedRule()

    first = _observations(rule, _inputs(metrics=_damage_metrics(wing=40.0)))
    assert first["damage_wing"].value == 40.0

    second = _observations(
        rule,
        _inputs(
            metrics=_damage_metrics(wing=20.0),
            state=_state(session=_session(uid=2)),
        ),
    )
    assert second["damage_wing"].value == 20.0
    assert dict(second["damage_wing"].facts)["damage_pct"] == 20.0


def test_rival_pit_stop_near_player_opens_with_name_and_position() -> None:
    rule = RivalPitStopRule()
    tracker = ConditionTracker(rule.policy)

    transitions = _update(
        tracker,
        rule,
        _inputs(
            state=_rival_state(gap_ms=1500),
            new_state_events=(_pit_entry(1),),
        ),
        10.0,
    )

    assert [item.signal.phase for item in transitions] == [EventPhase.OPENED]
    signal = transitions[0].signal
    assert signal.event_id == "rival_pit_stop:rival_pit_stop_1:1"
    assert dict(signal.facts) == {
        "car_index": 1,
        "name": "Rival",
        "position": 6,
        "gap_s": 1.5,
        "lap": 20,
    }

    # La condición one-shot se cierra en la siguiente evaluación.
    assert (
        _update(
            tracker,
            rule,
            _inputs(state=_rival_state(gap_ms=1500)),
            11.0,
        )
        == ()
    )


def test_rival_pit_stop_far_from_player_generates_no_alert() -> None:
    rule = RivalPitStopRule()
    inputs = _inputs(
        state=_rival_state(gap_ms=5000),
        new_state_events=(_pit_entry(1),),
    )

    assert rule.observe(inputs) == ()
    assert _update(ConditionTracker(rule.policy), rule, inputs, 10.0) == ()


def test_rival_pit_stop_adjacency_boundary_is_inclusive() -> None:
    near = RivalPitStopRule().observe(
        _inputs(state=_rival_state(gap_ms=2000), new_state_events=(_pit_entry(1),))
    )
    far = RivalPitStopRule().observe(
        _inputs(state=_rival_state(gap_ms=2001), new_state_events=(_pit_entry(1),))
    )

    assert len(near) == 1
    assert far == ()


def test_retired_rival_does_not_trigger_an_alert() -> None:
    retired = _inputs(
        state=_rival_state(result_status="retired"),
        new_state_events=(_pit_entry(1),),
    )
    inactive = _inputs(
        state=_rival_state(driver_status="inactive"),
        new_state_events=(_pit_entry(1),),
    )

    assert RivalPitStopRule().observe(retired) == ()
    assert RivalPitStopRule().observe(inactive) == ()


def test_player_pit_entry_is_not_a_rival_alert() -> None:
    inputs = _inputs(state=_rival_state(), new_state_events=(_pit_entry(0),))

    assert RivalPitStopRule().observe(inputs) == ()


def test_rival_pit_stop_is_suppressed_when_paused_or_stale() -> None:
    paused = _inputs(
        state=_rival_state(session=_session(game_paused=True)),
        new_state_events=(_pit_entry(1),),
    )
    stale = _inputs(
        state=_rival_state(freshness=(PacketFreshness("lap_data", 0.0, 1.0),)),
        new_state_events=(_pit_entry(1),),
    )

    assert RivalPitStopRule().observe(paused) == ()
    assert RivalPitStopRule().observe(stale) == ()


def test_rival_pit_stop_pending_key_survives_stale_data_and_rearms() -> None:
    rule = RivalPitStopRule()
    tracker = ConditionTracker(rule.policy)
    opening = _update(
        tracker,
        rule,
        _inputs(state=_rival_state(), new_state_events=(_pit_entry(1),)),
        10.0,
    )
    assert opening[0].signal.event_id == "rival_pit_stop:rival_pit_stop_1:1"

    stale = _inputs(
        state=_rival_state(freshness=(PacketFreshness("lap_data", 0.0, 1.0),)),
        now=11.0,
    )
    assert _update(tracker, rule, stale, 11.0) == ()

    closed = _update(
        tracker,
        rule,
        _inputs(state=_rival_state()),
        12.0,
        include_silent_closures=True,
    )
    assert len(closed) == 1
    assert closed[0].signal.phase is EventPhase.CLOSED

    reopened = _update(
        tracker,
        rule,
        _inputs(state=_rival_state(), new_state_events=(_pit_entry(1, lap=22),)),
        13.0,
    )
    assert reopened[0].signal.event_id == "rival_pit_stop:rival_pit_stop_1:2"


def test_rival_pit_stop_session_change_drops_pending_key() -> None:
    rule = RivalPitStopRule()
    tracker = ConditionTracker(rule.policy)
    _update(
        tracker,
        rule,
        _inputs(state=_rival_state(), new_state_events=(_pit_entry(1),)),
        10.0,
    )

    changed_session = _inputs(
        state=_rival_state(session=_session(uid=2)),
        now=11.0,
    )
    assert rule.observe(changed_session) == ()
    assert _update(tracker, rule, changed_session, 11.0) == ()
