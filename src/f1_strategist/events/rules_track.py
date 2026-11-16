"""Reglas de pista, clima y datos obsoletos (E6)."""

from __future__ import annotations

from ..config import EventsConfig
from ..metrics.types import Metric, Quality
from ..race_state import RaceEvent, RaceSessionSnapshot, RaceStateSnapshot
from .types import ConditionPolicy, EventInputs, EventPriority, Observation

_SAFETY_CAR_KEY = "safety_car_deployed"
_VIRTUAL_SAFETY_CAR_KEY = "virtual_safety_car_deployed"
_SAFETY_CAR_CLEARED_KEY = "safety_car_cleared"
_RED_FLAG_KEY = "red_flag"
_WEATHER_CHANGED_KEY = "weather_changed"
_RAIN_EXPECTED_KEY = "rain_expected"

_NO_SAFETY_CAR = "no_safety_car"
_FULL_SAFETY_CAR = "full_safety_car"
_VIRTUAL_SAFETY_CAR = "virtual_safety_car"
_FORMATION_LAP = "formation_lap"
_SAFETY_CAR_STATES = frozenset(
    {_NO_SAFETY_CAR, _FULL_SAFETY_CAR, _VIRTUAL_SAFETY_CAR, _FORMATION_LAP}
)

_DRY_WEATHER = frozenset({"clear", "light_cloud", "overcast"})
_WET_WEATHER = frozenset({"light_rain", "heavy_rain", "wet", "storm"})

_RAIN_PROBABILITY_KEY = "env.rain_probability_next_10_min_pct"
_RAIN_HORIZON_MIN = 10

# Grupos cuyo retraso impide calcular con garantías el estado de la carrera.
_CRITICAL_GROUPS = frozenset({"car_damage", "car_status", "lap_data", "participants", "session"})

_USABLE_QUALITIES = frozenset({Quality.OK, Quality.LOW})


class SafetyCarDeployedRule:
    """Avisa de que el safety car ha salido a pista."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="safety_car_deployed",
            category="track",
            priority=EventPriority.CRITICAL,
            message_key="event.safety_car_deployed",
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        return (_safety_car_observation(inputs, key=_SAFETY_CAR_KEY, status=_FULL_SAFETY_CAR),)


class VirtualSafetyCarDeployedRule:
    """Avisa de que el coche de seguridad virtual ha salido a pista."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="virtual_safety_car_deployed",
            category="track",
            priority=EventPriority.CRITICAL,
            message_key="event.virtual_safety_car_deployed",
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        return (
            _safety_car_observation(
                inputs,
                key=_VIRTUAL_SAFETY_CAR_KEY,
                status=_VIRTUAL_SAFETY_CAR,
            ),
        )


class SafetyCarClearedRule:
    """Avisa de que la pista vuelve a estar verde tras safety car o VSC."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="safety_car_cleared",
            category="track",
            priority=EventPriority.STRATEGIC,
            message_key="event.safety_car_cleared",
            one_shot=True,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        facts = (("lap", inputs.lap),)
        if not _session_allows_alerts(inputs.state):
            return (Observation(key=_SAFETY_CAR_CLEARED_KEY, active=None, facts=facts),)

        event = _latest_state_event(inputs, "safety_car_changed")
        cleared = (
            event is not None
            and event.value == _NO_SAFETY_CAR
            and event.previous_value in {_FULL_SAFETY_CAR, _VIRTUAL_SAFETY_CAR}
        )
        return (Observation(key=_SAFETY_CAR_CLEARED_KEY, active=cleared, facts=facts),)


class RedFlagRule:
    """Avisa de una nueva bandera roja contada como periodo de sesión."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="red_flag",
            category="track",
            priority=EventPriority.CRITICAL,
            message_key="event.red_flag",
            one_shot=True,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        facts = (("lap", inputs.lap),)
        if not _session_allows_alerts(inputs.state):
            return (Observation(key=_RED_FLAG_KEY, active=None, facts=facts),)
        event = _latest_state_event(inputs, "red_flag")
        return (Observation(key=_RED_FLAG_KEY, active=event is not None, facts=facts),)


class WeatherChangedRule:
    """Avisa de un cambio de clima; solo es estratégico al cruzar seco/lluvia."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="weather_changed",
            category="weather",
            priority=EventPriority.STRATEGIC,
            message_key="event.weather_changed",
            one_shot=True,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        if not _session_allows_alerts(inputs.state):
            return (Observation(key=_WEATHER_CHANGED_KEY, active=None),)
        event = _latest_state_event(inputs, "weather_changed")
        if event is None:
            return (Observation(key=_WEATHER_CHANGED_KEY, active=False),)

        session = inputs.state.session
        facts = (
            ("previous", event.previous_value),
            ("current", event.value),
            ("track_temperature_c", session.track_temperature_c),
            ("air_temperature_c", session.air_temperature_c),
        )
        return (
            Observation(
                key=_WEATHER_CHANGED_KEY,
                active=True,
                priority=_weather_priority(event.previous_value, event.value),
                facts=facts,
            ),
        )


class RainExpectedRule:
    """Avisa de que la previsión a diez minutos supera el umbral de lluvia."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()

    @property
    def policy(self) -> ConditionPolicy:
        weather = self._config.weather
        return ConditionPolicy(
            rule="rain_expected",
            category="weather",
            priority=EventPriority.STRATEGIC,
            message_key="event.rain_expected",
            persistence_observations=2,
            enter=weather.rain_enter_pct,
            exit=weather.rain_exit_pct,
            direction="above",
            update_min_delta=weather.update_delta_pct,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        metric = inputs.metrics.get(_RAIN_PROBABILITY_KEY)
        probability = _number(inputs, _RAIN_PROBABILITY_KEY)
        quality = metric.quality if metric is not None else None
        facts = (
            ("probability_pct", probability),
            ("horizon_min", _RAIN_HORIZON_MIN),
        )

        if probability is None or not _session_allows_alerts(inputs.state):
            return (
                Observation(
                    key=_RAIN_EXPECTED_KEY,
                    active=None,
                    facts=facts,
                    quality=quality,
                ),
            )

        return (
            Observation(
                key=_RAIN_EXPECTED_KEY,
                active=probability >= self._config.weather.rain_enter_pct,
                value=probability,
                facts=facts,
                material_key=_RAIN_EXPECTED_KEY,
                quality=quality,
            ),
        )


class DataStaleRule:
    """Avisa de grupos críticos de paquetes que llevan demasiado tiempo sin refrescarse."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="data_stale",
            category="data",
            priority=EventPriority.INFORMATIONAL,
            message_key="event.data_stale",
            persistence_s=self._config.data.stale_persistence_s,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        allows_alerts = _session_allows_alerts(inputs.state)
        observations: list[Observation] = []
        seen: set[str] = set()

        for freshness in sorted(inputs.state.freshness, key=lambda item: item.group):
            group = freshness.group
            if group not in _CRITICAL_GROUPS or group in seen:
                continue
            seen.add(group)
            age = freshness.age_seconds(inputs.now)
            facts = (("group", group), ("age_s", age))
            key = f"data_stale_{group}"
            if age is None or not allows_alerts:
                observations.append(Observation(key=key, active=None, facts=facts))
                continue
            observations.append(
                Observation(
                    key=key,
                    active=freshness.is_stale(inputs.now),
                    value=age,
                    facts=facts,
                ),
            )

        return tuple(observations)


def _safety_car_observation(inputs: EventInputs, *, key: str, status: str) -> Observation:
    session: RaceSessionSnapshot = inputs.state.session
    facts = (("lap", inputs.lap),)
    status_known = session.safety_car_status in _SAFETY_CAR_STATES
    if not _session_allows_alerts(inputs.state) or not status_known:
        return Observation(key=key, active=None, facts=facts)
    return Observation(key=key, active=session.safety_car_status == status, facts=facts)


def _latest_state_event(inputs: EventInputs, kind: str) -> RaceEvent | None:
    for event in reversed(inputs.new_state_events):
        if event.kind == kind:
            return event
    return None


def _weather_priority(previous: str | None, current: str | None) -> EventPriority:
    if previous is None or current is None:
        return EventPriority.INFORMATIONAL
    crossed_into_rain = previous in _DRY_WEATHER and current in _WET_WEATHER
    crossed_into_dry = previous in _WET_WEATHER and current in _DRY_WEATHER
    if crossed_into_rain or crossed_into_dry:
        return EventPriority.STRATEGIC
    return EventPriority.INFORMATIONAL


def _number(inputs: EventInputs, key: str) -> float | None:
    metric: Metric | None = inputs.metrics.get(key)
    if metric is None or metric.quality not in _USABLE_QUALITIES:
        return None
    value = metric.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _session_allows_alerts(state: RaceStateSnapshot) -> bool:
    session = state.session
    return session.started is True and session.ended is not True and session.game_paused is not True


__all__ = [
    "DataStaleRule",
    "RainExpectedRule",
    "RedFlagRule",
    "SafetyCarClearedRule",
    "SafetyCarDeployedRule",
    "VirtualSafetyCarDeployedRule",
    "WeatherChangedRule",
]
