"""Reglas de daños y paradas de rivales (E7)."""

from __future__ import annotations

from ..config import EventsConfig
from ..metrics.types import Metric, Quality
from ..race_state import CarSnapshot, RaceStateSnapshot
from .types import ConditionPolicy, EventInputs, EventPriority, Fact, Observation

_WING_KEY = "damage_wing"
_FLOOR_KEY = "damage_floor"
_GEARBOX_KEY = "damage_gearbox"
_TYRES_KEY = "damage_tyres"
_DRS_FAULT_KEY = "damage_fault_drs"
_ERS_FAULT_KEY = "damage_fault_ers"
_ENGINE_FAULT_KEY = "damage_fault_engine"

# Orden estable de las observaciones; también se usa al suprimir la regla.
_KEY_COMPONENTS = (
    ("wing", _WING_KEY),
    ("floor", _FLOOR_KEY),
    ("gearbox", _GEARBOX_KEY),
    ("tyres", _TYRES_KEY),
    ("drs", _DRS_FAULT_KEY),
    ("ers", _ERS_FAULT_KEY),
    ("engine", _ENGINE_FAULT_KEY),
)

_COMPONENTS = (
    ("wing", _WING_KEY, "damage.aero_max_pct"),
    ("floor", _FLOOR_KEY, "damage.floor_pct"),
    ("gearbox", _GEARBOX_KEY, "damage.gearbox_pct"),
)

_FAULTS = (
    ("DRS", "drs", _DRS_FAULT_KEY),
    ("ERS", "ers", _ERS_FAULT_KEY),
    ("motor", "engine", _ENGINE_FAULT_KEY),
)

_RETIRED_STATUSES = frozenset({"retired", "inactive", "invalid"})
_RIVAL_FRESHNESS_GROUPS = ("car_status", "lap_data", "session_history")
_USABLE_QUALITIES = frozenset({Quality.OK, Quality.LOW})


class DamageDetectedRule:
    """Avisa de daños por componente y de averías declaradas en el coche."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()
        self._last_session_uid: int | None = None
        self._announced: dict[str, float] = {}

    @property
    def policy(self) -> ConditionPolicy:
        damage = self._config.damage
        return ConditionPolicy(
            rule="damage_detected",
            category="damage",
            priority=EventPriority.STRATEGIC,
            message_key="event.damage_detected",
            persistence_observations=1,
            update_min_delta=damage.update_delta_pct,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        self._reset_for_new_session(inputs.state)
        if not _session_allows_alerts(inputs.state):
            return tuple(
                Observation(key=key, active=None, facts=_damage_facts(component, None, 0.0))
                for component, key in _KEY_COMPONENTS
            )

        observations = [
            self._numeric_observation(
                inputs,
                component=component,
                key=key,
                metric_key=metric_key,
            )
            for component, key, metric_key in _COMPONENTS
        ]
        observations.append(self._tyres_observation(inputs))
        faults = _fault_names(inputs)
        observations.extend(
            self._fault_observation(
                fault=fault,
                component=component,
                key=key,
                faults=faults,
            )
            for fault, component, key in _FAULTS
        )
        return tuple(observations)

    def _numeric_observation(
        self,
        inputs: EventInputs,
        *,
        component: str,
        key: str,
        metric_key: str,
    ) -> Observation:
        metric: Metric | None = inputs.metrics.get(metric_key)
        quality = metric.quality if metric is not None else None
        value = _usable_number(metric)
        if value is None:
            self._announced.pop(key, None)
            return Observation(
                key=key,
                active=None,
                facts=_damage_facts(component, None, 0.0),
                quality=quality,
            )
        return self._damage_observation(
            component=component,
            key=key,
            value=value,
            quality=quality,
        )

    def _tyres_observation(self, inputs: EventInputs) -> Observation:
        quality = _car_damage_quality(inputs)
        car = inputs.state.player_car
        damage = car.damage if car is not None else None
        if damage is None or not damage.tyres_damage:
            self._announced.pop(_TYRES_KEY, None)
            return Observation(
                key=_TYRES_KEY,
                active=None,
                facts=_damage_facts("tyres", None, 0.0),
                quality=Quality.UNAVAILABLE,
            )
        if quality not in _USABLE_QUALITIES:
            self._announced.pop(_TYRES_KEY, None)
            return Observation(
                key=_TYRES_KEY,
                active=None,
                facts=_damage_facts("tyres", None, 0.0),
                quality=quality,
            )
        return self._damage_observation(
            component="tyres",
            key=_TYRES_KEY,
            value=float(max(damage.tyres_damage)),
            quality=quality,
        )

    def _fault_observation(
        self,
        *,
        fault: str,
        component: str,
        key: str,
        faults: frozenset[str] | None,
    ) -> Observation:
        priority = EventPriority.CRITICAL if component == "engine" else EventPriority.STRATEGIC
        if faults is None:
            return Observation(
                key=key,
                active=None,
                priority=priority,
                facts=_damage_facts(component, None, 0.0),
            )
        return Observation(
            key=key,
            active=fault in faults,
            priority=priority,
            facts=_damage_facts(component, None, 0.0),
        )

    def _damage_observation(
        self,
        *,
        component: str,
        key: str,
        value: float,
        quality: Quality | None,
    ) -> Observation:
        if value <= self._config.damage.warn_pct:
            self._announced.pop(key, None)
            return Observation(
                key=key,
                active=False,
                value=value,
                facts=_damage_facts(component, value, 0.0),
                quality=quality,
            )

        announced = self._announced.get(key)
        if announced is None:
            announced = value
            delta = 0.0
            self._announced[key] = announced
        else:
            delta = value - announced
            if delta >= self._config.damage.update_delta_pct:
                announced = value
                self._announced[key] = announced
            else:
                delta = 0.0

        return Observation(
            key=key,
            active=True,
            value=announced,
            facts=_damage_facts(component, announced, delta),
            quality=quality,
        )

    def _reset_for_new_session(self, state: RaceStateSnapshot) -> None:
        uid = state.session.uid
        if uid != self._last_session_uid:
            self._last_session_uid = uid
            self._announced.clear()


class RivalPitStopRule:
    """Avisa de que un coche adyacente entra a boxes."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()
        self._last_session_uid: int | None = None
        self._pending_keys: set[str] = set()

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="rival_pit_stop",
            category="rivals",
            priority=EventPriority.INFORMATIONAL,
            message_key="event.rival_pit_stop",
            one_shot=True,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        self._reset_for_new_session(inputs.state)
        if not _session_allows_alerts(inputs.state):
            return tuple(Observation(key=key, active=None) for key in sorted(self._pending_keys))
        if _state_data_stale(inputs.state, inputs.now):
            return tuple(Observation(key=key, active=None) for key in sorted(self._pending_keys))

        active = self._active_observations(inputs)
        closers = tuple(
            Observation(key=key, active=False)
            for key in sorted(self._pending_keys.difference(active))
        )
        self._pending_keys = set(active)
        return tuple(active[key] for key in sorted(active)) + closers

    def _active_observations(self, inputs: EventInputs) -> dict[str, Observation]:
        observations: dict[str, Observation] = {}
        state = inputs.state
        player = state.player_car
        if player is None:
            return observations

        for event in inputs.new_state_events:
            if event.kind != "pit_entry" or event.car_index is None:
                continue
            if event.car_index == state.player_car_index:
                continue
            rival = _car_by_index(state, event.car_index)
            if rival is None or _is_retired(rival):
                continue
            gap_s = _gap_to_player_s(player, rival)
            if gap_s is None or gap_s > self._config.rivals.gap_s:
                continue
            lap = event.lap_number if event.lap_number is not None else inputs.lap
            key = f"rival_pit_stop_{event.car_index}"
            observations[key] = Observation(
                key=key,
                active=True,
                facts=(
                    ("car_index", event.car_index),
                    ("name", rival.name),
                    ("position", rival.position),
                    ("gap_s", gap_s),
                    ("lap", lap),
                ),
            )
        return observations

    def _reset_for_new_session(self, state: RaceStateSnapshot) -> None:
        uid = state.session.uid
        if uid != self._last_session_uid:
            self._last_session_uid = uid
            self._pending_keys.clear()


def _damage_facts(
    component: str,
    damage_pct: float | None,
    delta_pct: float,
) -> tuple[Fact, ...]:
    return (
        ("component", component),
        ("damage_pct", damage_pct),
        ("delta_pct", delta_pct),
    )


def _usable_number(metric: Metric | None) -> float | None:
    if metric is None or metric.quality not in _USABLE_QUALITIES:
        return None
    value = metric.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fault_names(inputs: EventInputs) -> frozenset[str] | None:
    metric: Metric | None = inputs.metrics.get("damage.faults")
    if metric is None or metric.quality not in _USABLE_QUALITIES:
        return None
    value = metric.value
    if not isinstance(value, (tuple, list)):
        return None
    return frozenset(item for item in value if isinstance(item, str))


def _car_damage_quality(inputs: EventInputs) -> Quality:
    try:
        freshness = inputs.state.freshness_for("car_damage")
    except KeyError:
        return Quality.OK
    return Quality.STALE if freshness.is_stale(inputs.now) else Quality.OK


def _state_data_stale(state: RaceStateSnapshot, now: float) -> bool:
    for group in _RIVAL_FRESHNESS_GROUPS:
        try:
            freshness = state.freshness_for(group)
        except KeyError:
            continue
        if freshness.is_stale(now):
            return True
    return False


def _car_by_index(state: RaceStateSnapshot, car_index: int) -> CarSnapshot | None:
    for car in state.cars:
        if car.car_index == car_index:
            return car
    return None


def _is_retired(car: CarSnapshot) -> bool:
    statuses = (car.result_status, car.driver_status)
    return any(
        status is not None and status.strip().lower() in _RETIRED_STATUSES for status in statuses
    )


def _gap_to_player_s(player: CarSnapshot, rival: CarSnapshot) -> float | None:
    if player.delta_to_leader_ms is not None and rival.delta_to_leader_ms is not None:
        gap_ms = abs(player.delta_to_leader_ms - rival.delta_to_leader_ms)
        return gap_ms / 1000.0
    if player.position is None or rival.position is None:
        return None
    if rival.position + 1 == player.position and player.delta_to_car_in_front_ms is not None:
        return player.delta_to_car_in_front_ms / 1000.0
    if rival.position - 1 == player.position and rival.delta_to_car_in_front_ms is not None:
        return rival.delta_to_car_in_front_ms / 1000.0
    return None


def _session_allows_alerts(state: RaceStateSnapshot) -> bool:
    session = state.session
    return session.started is True and session.ended is not True and session.game_paused is not True


__all__ = ["DamageDetectedRule", "RivalPitStopRule"]
