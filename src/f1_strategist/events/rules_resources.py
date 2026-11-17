"""Reglas de combustible y neumáticos (E5)."""

from __future__ import annotations

from ..config import EventsConfig
from ..metrics.types import Metric, Quality
from ..race_state import RaceSessionSnapshot, RaceStateSnapshot
from .types import ConditionPolicy, EventInputs, EventPriority, Fact, Observation

_FUEL_MARGIN_LOW_KEY = "fuel_margin"
_FUEL_MARGIN_CRITICAL_KEY = "fuel_margin_critical"
_TYRE_LIFE_KEY = "tyre_life"
_TYRE_DEGRADATION_KEY = "tyre_degradation"

_USABLE_QUALITIES = frozenset({Quality.OK, Quality.LOW})


class FuelMarginLowRule:
    """Avisa de que el combustible no llega al final con margen de seguridad."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()

    @property
    def policy(self) -> ConditionPolicy:
        fuel = self._config.fuel
        return ConditionPolicy(
            rule="fuel_margin_low",
            category="fuel",
            priority=EventPriority.STRATEGIC,
            message_key="event.fuel_margin_low",
            persistence_observations=2,
            enter=fuel.warn_laps,
            exit=fuel.warn_laps + fuel.hysteresis_laps,
            direction="below",
            update_min_delta=fuel.update_delta_laps,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        return (
            _fuel_margin_observation(inputs, key=_FUEL_MARGIN_LOW_KEY, enter=self.policy.enter),
        )


class FuelMarginCriticalRule:
    """Avisa de que el combustible se agota antes de terminar."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()

    @property
    def policy(self) -> ConditionPolicy:
        fuel = self._config.fuel
        return ConditionPolicy(
            rule="fuel_margin_critical",
            category="fuel",
            priority=EventPriority.CRITICAL,
            message_key="event.fuel_margin_critical",
            persistence_observations=2,
            enter=fuel.critical_laps,
            exit=fuel.critical_laps + fuel.hysteresis_laps,
            direction="below",
            update_min_delta=fuel.update_delta_laps,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        return (
            _fuel_margin_observation(
                inputs,
                key=_FUEL_MARGIN_CRITICAL_KEY,
                enter=self.policy.enter,
            ),
        )


class TyreLifeNearLimitRule:
    """Avisa de que las vueltas útiles del neumático se acaban antes del final."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()
        self._last_session_uid: int | None = None
        self._last_stint_key: str | None = None

    @property
    def policy(self) -> ConditionPolicy:
        return ConditionPolicy(
            rule="tyre_life_near_limit",
            category="tyres",
            priority=EventPriority.STRATEGIC,
            message_key="event.tyre_life_near_limit",
            persistence_observations=1,
            update_min_delta=1.0,
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        self._reset_for_new_session(inputs.state)
        stint_key = _stint_key(inputs, _TYRE_LIFE_KEY)
        closers: tuple[Observation, ...] = ()
        if stint_key is not None:
            if self._last_stint_key is not None and stint_key != self._last_stint_key:
                closers = (_close_observation(self._last_stint_key),)
            self._last_stint_key = stint_key

        observation = self._observation(inputs, stint_key)
        return (*closers, observation)

    def _reset_for_new_session(self, state: RaceStateSnapshot) -> None:
        uid = state.session.uid
        if uid != self._last_session_uid:
            self._last_session_uid = uid
            self._last_stint_key = None

    def _observation(self, inputs: EventInputs, stint_key: str | None) -> Observation:
        compound = _text(inputs, "tyres.compound")
        age = _number(inputs, "tyres.age_laps")
        wear_mean = _number(inputs, "tyres.wear_mean_pct")
        wear_left = _number(inputs, "tyres.laps_to_wear_limit")
        lifespan = _number(inputs, "tyres.game_lifespan_laps")

        game_left = max(0.0, lifespan - age) if lifespan is not None and age is not None else None
        constraints = tuple(value for value in (wear_left, game_left) if value is not None)
        laps_left = min(constraints) if constraints else None

        facts = (
            ("compound", compound),
            ("age_laps", age),
            ("wear_mean_pct", wear_mean),
            ("laps_left", laps_left),
        )
        if stint_key is None or laps_left is None or not _session_allows_alerts(inputs.state):
            return Observation(
                key=stint_key or _TYRE_LIFE_KEY,
                active=None,
                facts=facts,
            )

        laps_remaining = _laps_remaining(inputs.state)
        if laps_remaining is None:
            return Observation(
                key=stint_key,
                active=None,
                facts=facts,
            )

        active = laps_left < self._config.tyres.warn_laps and laps_left < laps_remaining
        return Observation(
            key=stint_key,
            active=active,
            value=laps_left,
            facts=facts,
            material_key=stint_key,
        )


class TyreDegradationAbnormalRule:
    """Avisa de un desgaste anómalo frente al perfil del fin de semana."""

    def __init__(self, config: EventsConfig | None = None) -> None:
        self._config = config or EventsConfig()
        self._last_session_uid: int | None = None
        self._last_stint_key: str | None = None

    @property
    def policy(self) -> ConditionPolicy:
        tyres = self._config.tyres
        return ConditionPolicy(
            rule="tyre_degradation_abnormal",
            category="tyres",
            priority=EventPriority.INFORMATIONAL,
            message_key="event.tyre_degradation_abnormal",
            persistence_observations=2,
            enter=1.0 + tyres.degradation_ratio,
            exit=1.0 + tyres.degradation_ratio - tyres.hysteresis,
            direction="above",
        )

    def observe(self, inputs: EventInputs) -> tuple[Observation, ...]:
        self._reset_for_new_session(inputs.state)
        stint_key = _stint_key(inputs, _TYRE_DEGRADATION_KEY)
        closers: tuple[Observation, ...] = ()
        if stint_key is not None:
            if self._last_stint_key is not None and stint_key != self._last_stint_key:
                # Con histéresis, el cierre necesita un valor por debajo de ``exit``.
                closers = (_close_observation(self._last_stint_key),)
            self._last_stint_key = stint_key

        observation = self._observation(inputs, stint_key)
        return (*closers, observation)

    def _reset_for_new_session(self, state: RaceStateSnapshot) -> None:
        uid = state.session.uid
        if uid != self._last_session_uid:
            self._last_session_uid = uid
            self._last_stint_key = None

    def _observation(self, inputs: EventInputs, stint_key: str | None) -> Observation:
        age = _number(inputs, "tyres.age_laps")
        rate = _number(inputs, "tyres.wear_rate_pct_per_lap")
        expected = _number(inputs, "profile.wear_rate_pct_per_lap")

        ratio = (
            rate / expected if rate is not None and expected is not None and expected > 0 else None
        )
        facts = (
            ("wear_rate_pct_per_lap", rate),
            ("expected_rate_pct_per_lap", expected),
            ("ratio", ratio),
            ("pace_trend_s_per_lap", None),
        )
        minimum_stint = self._config.tyres.min_stint_laps
        if (
            stint_key is None
            or ratio is None
            or age is None
            or age < minimum_stint
            or not _session_allows_alerts(inputs.state)
        ):
            return Observation(
                key=stint_key or _TYRE_DEGRADATION_KEY,
                active=None,
                facts=facts,
            )

        return Observation(
            key=stint_key,
            active=ratio > 1.0 + self._config.tyres.degradation_ratio,
            value=ratio,
            facts=facts,
        )


def _fuel_margin_observation(
    inputs: EventInputs,
    *,
    key: str,
    enter: float | None,
) -> Observation:
    margin_metric = inputs.metrics.get("fuel.margin_laps_at_finish")
    margin = _number(inputs, "fuel.margin_laps_at_finish")
    facts = _fuel_facts(inputs)
    quality = margin_metric.quality if margin_metric is not None else None

    if margin is None or enter is None or not _session_allows_alerts(inputs.state):
        return Observation(key=key, active=None, facts=facts, quality=quality)

    return Observation(
        key=key,
        active=margin < enter,
        value=margin,
        facts=facts,
        material_key=key,
        quality=quality,
    )


def _fuel_facts(inputs: EventInputs) -> tuple[Fact, ...]:
    return (
        ("margin_laps", _number(inputs, "fuel.margin_laps_at_finish")),
        ("margin_kg", _number(inputs, "fuel.margin_kg_at_finish")),
        ("laps_remaining", _laps_remaining(inputs.state)),
        ("consumption_per_lap", _number(inputs, "fuel.consumption_per_lap")),
    )


def _stint_key(inputs: EventInputs, prefix: str) -> str | None:
    age = _number(inputs, "tyres.age_laps")
    if age is None:
        return None
    current = inputs.state.current_lap if inputs.state.current_lap is not None else inputs.lap
    start_lap = max(0, int(current - age))
    return f"{prefix}_{start_lap}"


def _close_observation(key: str) -> Observation:
    # Un valor suficientemente bajo cierra incluso las reglas con histéresis.
    return Observation(
        key=key,
        active=False,
        value=0.0,
        quality=Quality.OK,
    )


def _number(inputs: EventInputs, key: str) -> float | None:
    metric: Metric | None = inputs.metrics.get(key)
    if metric is None or metric.quality not in _USABLE_QUALITIES:
        return None
    value = metric.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(inputs: EventInputs, key: str) -> str | None:
    metric: Metric | None = inputs.metrics.get(key)
    if metric is None or metric.quality not in _USABLE_QUALITIES:
        return None
    return metric.value if isinstance(metric.value, str) else None


def _session_allows_alerts(state: RaceStateSnapshot) -> bool:
    session: RaceSessionSnapshot = state.session
    return session.started is True and session.ended is not True and session.game_paused is not True


def _laps_remaining(state: RaceStateSnapshot) -> float | None:
    session = state.session
    if session.total_laps is None:
        return None
    if session.ended is True:
        return 0.0
    if session.safety_car_status == "formation_lap":
        return float(session.total_laps)
    car = state.player_car
    if (
        car is None
        or car.current_lap is None
        or car.lap_distance_m is None
        or session.track_length_m is None
        or session.track_length_m <= 0
    ):
        return None
    lap_fraction = min(1.0, max(0.0, car.lap_distance_m / session.track_length_m))
    return max(0.0, session.total_laps - (car.current_lap - 1) - lap_fraction)


__all__ = [
    "FuelMarginCriticalRule",
    "FuelMarginLowRule",
    "TyreDegradationAbnormalRule",
    "TyreLifeNearLimitRule",
]
