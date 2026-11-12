"""Cierre de vueltas y muestreo de los valores en la frontera de cada vuelta."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import MetricsConfig
from ..race_state import CarDamageSnapshot, CarSnapshot, RaceStateSnapshot
from .history import LapHistory, LapRecord

_INVALID_RESULT_STATUSES = frozenset({"invalid", "inactive"})
_NON_TRACKING_RESULT_STATUSES = _INVALID_RESULT_STATUSES | {"retired"}
_TERMINAL_RESULT_STATUSES = frozenset(
    {"finished", "did_not_finish", "disqualified", "not_classified", "retired"}
)
_PIT_STATUSES = frozenset({"pitting", "in_pit_area"})
_RACE_SESSION_TYPES = frozenset({"race", "race_2", "race_3"})
_TRACKED_FRESHNESS_GROUPS = ("lap_data", "car_status", "car_damage", "tyre_sets", "session")
PARTIAL_LAP_MIN_TIME_S = 3.0
PARTIAL_LAP_MIN_DISTANCE_M = 50.0
_FLASHBACK_TIME_DELTA_MS = 1_000
_FLASHBACK_DISTANCE_DELTA_M = 50.0
_DAMAGE_SCALAR_FIELDS = (
    "front_left_wing_damage",
    "front_right_wing_damage",
    "rear_wing_damage",
    "floor_damage",
    "diffuser_damage",
    "sidepod_damage",
    "gearbox_damage",
    "engine_damage",
)


@dataclass(slots=True)
class _OpenLap:
    car_index: int
    lap_number: int
    started_at_monotonic: float
    start_fuel_kg: float | None
    start_tyre_age: int | None
    start_wear: tuple[float, ...] | None
    start_position: int | None
    start_gap_ahead_s: float | None
    start_compound: str | None
    flags: set[str] = field(default_factory=set)
    track_statuses: set[str] = field(default_factory=set)
    weathers: set[str] = field(default_factory=set)
    track_temperature_sum_c: float = 0.0
    track_temperature_sample_count: int = 0
    last_track_temperature_sample_at: float | None = None
    pit_entry: bool = False
    pit_exit: bool = False
    pit_lane_time_s: float | None = None
    pit_stop_time_s: float | None = None
    traffic_samples: int = 0
    traffic_samples_ahead: int = 0
    traffic_sample_seen: bool = False
    last_traffic_sample_at: float | None = None
    damage_baseline: tuple[float, ...] | None = None
    damage_max_delta_pct: float | None = None
    ers_harvested_j: float | None = None
    ers_deployed_j: float | None = None
    last_fuel_kg: float | None = None
    last_tyre_age: int | None = None
    last_wear: tuple[float, ...] | None = None
    last_position: int | None = None
    last_gap_ahead_s: float | None = None
    last_compound: str | None = None
    saw_progress: bool = False


def _gap_ahead_s(car: CarSnapshot) -> float | None:
    if car.position == 1 or car.delta_to_car_in_front_ms is None:
        return None
    return car.delta_to_car_in_front_ms / 1000.0


def _compound(car: CarSnapshot) -> str | None:
    if car.actual_tyre_compound is not None:
        return car.actual_tyre_compound
    if car.fitted_tyre is not None:
        return car.fitted_tyre.actual_tyre_compound
    return None


def _wear(car: CarSnapshot | None) -> tuple[float, ...] | None:
    if car is None or car.damage is None:
        return None
    return tuple(float(value) for value in car.damage.tyres_wear)


def _damage_values(damage: CarDamageSnapshot | None) -> tuple[float, ...] | None:
    """Return impact-related damage, excluding gradual wear and tyre/brake degradation."""
    if damage is None:
        return None
    return tuple(float(getattr(damage, field_name)) for field_name in _DAMAGE_SCALAR_FIELDS)


def _max_damage_delta(
    baseline: tuple[float, ...],
    current: tuple[float, ...],
) -> float:
    return max(
        (
            abs(current_value - baseline_value)
            for baseline_value, current_value in zip(baseline, current, strict=False)
        ),
        default=0.0,
    )


def _pit_time(value_ms: int | None) -> float | None:
    if value_ms is None or value_ms <= 0:
        return None
    return value_ms / 1000.0


class LapTracker:
    """Cierra vueltas a partir de las transiciones de ``CarSnapshot.current_lap``."""

    def __init__(
        self,
        history: LapHistory | MetricsConfig | None = None,
        config: MetricsConfig | None = None,
    ) -> None:
        if isinstance(history, MetricsConfig):
            config = history
            history = None
        self._config = config or MetricsConfig()
        self._history = history or LapHistory(self._config)
        self._session_initialized = False
        self._session_uid: int | None = None
        self._open_laps: dict[int, _OpenLap] = {}
        self._retired_cars: set[int] = set()
        self._last_cars: list[CarSnapshot | None] = []
        self._red_flag_periods: int | None = None
        self._red_flag_pending = False
        self._red_flag_restart_pending: set[int] = set()
        self._pending_flashback_laps: dict[int, int] = {}
        self._last_freshness_signature: tuple[float | None, ...] | None = None

    @property
    def history(self) -> LapHistory:
        return self._history

    @property
    def config(self) -> MetricsConfig:
        return self._config

    def observe(self, state: RaceStateSnapshot, now: float) -> None:
        """Actualiza el histórico con una nueva instantánea del estado de carrera."""
        self._sync_session(state)
        freshness_signature = tuple(
            state.freshness_for(group).updated_at_monotonic for group in _TRACKED_FRESHNESS_GROUPS
        )
        if state.session.ended is True:
            self._close_open_laps(state, now)
            self._remember_cars(state)
            self._last_freshness_signature = freshness_signature
            return

        periods = state.session.red_flag_periods
        self._red_flag_pending = periods is not None and periods > (self._red_flag_periods or 0)
        if self._red_flag_pending:
            self._red_flag_restart_pending = {
                index
                for index, car in enumerate(state.cars)
                if car.result_status not in _NON_TRACKING_RESULT_STATUSES
            }
            for open_lap in self._open_laps.values():
                open_lap.track_statuses.add("red_flag")

        if freshness_signature == self._last_freshness_signature and not self._red_flag_pending:
            return

        for index, car in enumerate(state.cars):
            previous = self._last_cars[index] if index < len(self._last_cars) else None
            open_lap = self._open_laps.get(index)

            if index in self._retired_cars:
                if car.result_status == "retired":
                    continue
                self._retired_cars.discard(index)

            if car.result_status in _INVALID_RESULT_STATUSES:
                self._open_laps.pop(index, None)
                self._red_flag_restart_pending.discard(index)
                continue

            if car.current_lap is None:
                if car.result_status == "retired":
                    if open_lap is not None:
                        self._finish_retired_lap(open_lap, car, now)
                        del self._open_laps[index]
                    self._retired_cars.add(index)
                    self._red_flag_restart_pending.discard(index)
                continue

            if open_lap is None:
                open_lap = self._open_lap(index, car, previous, state, now)
                self._open_laps[index] = open_lap
                if car.result_status == "retired":
                    self._finish_retired_lap(open_lap, car, now)
                    del self._open_laps[index]
                continue

            if car.current_lap < open_lap.lap_number:
                self._handle_flashback(index, car, state, now)
                continue

            if car.current_lap > open_lap.lap_number:
                sequential = car.current_lap == open_lap.lap_number + 1
                lap_time_s = (
                    car.last_lap_time_ms / 1000.0
                    if sequential and car.last_lap_time_ms is not None
                    else None
                )
                if open_lap.lap_number == 1 and state.session.session_type in _RACE_SESSION_TYPES:
                    open_lap.flags.add("first_lap")
                new_open = self._open_lap(index, car, previous, state, now)
                self._finish_lap(
                    open_lap,
                    previous or car,
                    now,
                    lap_time_s=lap_time_s,
                )
                self._open_laps[index] = new_open
                if car.result_status == "retired":
                    self._finish_retired_lap(new_open, car, now)
                    del self._open_laps[index]
                continue

            self._update_lap(open_lap, car, previous, state, now)
            if car.result_status == "retired":
                self._finish_retired_lap(open_lap, car, now)
                del self._open_laps[index]

        self._red_flag_periods = periods
        self._remember_cars(state)
        self._last_freshness_signature = freshness_signature

    def _sync_session(self, state: RaceStateSnapshot) -> None:
        session_uid = state.session.uid
        if self._session_initialized and session_uid == self._session_uid:
            return
        self._session_initialized = True
        self._session_uid = session_uid
        self._history.reset(session_uid)
        self._open_laps.clear()
        self._retired_cars.clear()
        self._last_cars = [None] * len(state.cars)
        self._red_flag_periods = state.session.red_flag_periods
        self._red_flag_pending = False
        self._red_flag_restart_pending.clear()
        self._pending_flashback_laps.clear()
        self._last_freshness_signature = None

    def _remember_cars(self, state: RaceStateSnapshot) -> None:
        self._last_cars = list(state.cars)

    def _open_lap(
        self,
        car_index: int,
        car: CarSnapshot,
        previous: CarSnapshot | None,
        state: RaceStateSnapshot,
        now: float,
    ) -> _OpenLap:
        assert car.current_lap is not None
        previous_wear = _wear(previous)
        if self._tyres_changed(car, previous):
            wear = tuple(0.0 for _ in previous_wear) if previous_wear is not None else _wear(car)
        else:
            wear = previous_wear if previous_wear is not None else _wear(car)
        open_lap = _OpenLap(
            car_index=car_index,
            lap_number=car.current_lap,
            started_at_monotonic=now,
            start_fuel_kg=(
                previous.fuel_kg
                if previous is not None and previous.fuel_kg is not None
                else car.fuel_kg
            ),
            start_tyre_age=self._start_tyre_age(car, previous),
            start_wear=wear,
            start_position=(
                previous.position
                if previous is not None and previous.position is not None
                else car.position
            ),
            start_gap_ahead_s=(
                _gap_ahead_s(previous)
                if previous is not None and _gap_ahead_s(previous) is not None
                else _gap_ahead_s(car)
            ),
            start_compound=_compound(car),
        )
        if state.session.session_type in _RACE_SESSION_TYPES and open_lap.lap_number == 1:
            open_lap.flags.add("first_lap")
        if self._pending_flashback_laps.pop(car_index, None) == open_lap.lap_number:
            open_lap.flags.add("flashback")
        if car_index in self._red_flag_restart_pending:
            open_lap.track_statuses.add("red_flag")
            self._red_flag_restart_pending.discard(car_index)
        if (
            previous is None
            and open_lap.lap_number > 1
            and (
                car.current_lap_time_ms is not None
                and car.current_lap_time_ms / 1000 > PARTIAL_LAP_MIN_TIME_S
                or car.lap_distance_m is not None
                and car.lap_distance_m > PARTIAL_LAP_MIN_DISTANCE_M
            )
        ):
            open_lap.flags.add("partial_lap")
        if previous is not None:
            open_lap.damage_baseline = _damage_values(previous.damage)
        self._add_session_context(open_lap, state, now)
        self._update_pit(open_lap, car, previous)
        self._update_traffic(open_lap, car, state)
        self._update_damage(open_lap, car)
        self._update_ers(open_lap, car)
        self._remember_car_values(open_lap, car)
        self._mark_progress(open_lap, car, previous)
        return open_lap

    def _update_lap(
        self,
        open_lap: _OpenLap,
        car: CarSnapshot,
        previous: CarSnapshot | None,
        state: RaceStateSnapshot,
        now: float,
    ) -> None:
        if self._tyres_changed(car, previous):
            open_lap.start_tyre_age = 0
            wear = _wear(car)
            open_lap.start_wear = tuple(0.0 for _ in wear) if wear is not None else None
            open_lap.start_compound = _compound(car)

        if open_lap.start_fuel_kg is None and car.fuel_kg is not None:
            open_lap.start_fuel_kg = car.fuel_kg
        if open_lap.start_tyre_age is None and car.tyre_age_laps is not None:
            open_lap.start_tyre_age = car.tyre_age_laps
        if open_lap.start_wear is None:
            open_lap.start_wear = _wear(car)
        if open_lap.start_position is None and car.position is not None:
            open_lap.start_position = car.position
        if open_lap.start_gap_ahead_s is None and _gap_ahead_s(car) is not None:
            open_lap.start_gap_ahead_s = _gap_ahead_s(car)
        if open_lap.start_compound is None:
            open_lap.start_compound = _compound(car)

        # El tipo de sesión puede llegar después del primer paquete: la marca
        # de primera vuelta se reconcilia antes de cerrar la vuelta 1.
        if (
            open_lap.lap_number == 1
            and state.session.session_type in _RACE_SESSION_TYPES
            and "first_lap" not in open_lap.flags
        ):
            open_lap.flags.add("first_lap")

        crossing = self._is_start_line_crossing(
            car,
            previous,
            state,
            pit_exit=open_lap.pit_exit,
        )
        if crossing:
            self._reset_timed_lap_after_crossing(open_lap, car, now)
        elif self._is_in_lap_flashback(car, previous):
            self._reset_lap_accumulators(open_lap, car)

        self._add_session_context(open_lap, state, now)
        self._update_pit(open_lap, car, None if crossing else previous)
        self._update_traffic(open_lap, car, state)
        self._update_damage(open_lap, car)
        self._update_ers(open_lap, car)
        self._remember_car_values(open_lap, car)
        self._mark_progress(open_lap, car, previous)

    def _is_in_lap_flashback(
        self,
        car: CarSnapshot,
        previous: CarSnapshot | None,
    ) -> bool:
        if previous is None or car.current_lap != previous.current_lap:
            return False
        if car.result_status == "finished":
            return False
        time_rewound = (
            car.current_lap_time_ms is not None
            and previous.current_lap_time_ms is not None
            and previous.current_lap_time_ms - car.current_lap_time_ms > _FLASHBACK_TIME_DELTA_MS
        )
        distance_rewound = (
            car.lap_distance_m is not None
            and previous.lap_distance_m is not None
            and previous.lap_distance_m - car.lap_distance_m > _FLASHBACK_DISTANCE_DELTA_M
        )
        return time_rewound or distance_rewound

    def _is_start_line_crossing(
        self,
        car: CarSnapshot,
        previous: CarSnapshot | None,
        state: RaceStateSnapshot,
        *,
        pit_exit: bool,
    ) -> bool:
        """Detecta el cruce de meta durante una vuelta de salida de boxes.

        El juego puede conservar el número de vuelta al cruzar la línea: la
        distancia cae desde cerca del final del circuito hasta cerca de cero y
        se reinician el tiempo de vuelta o el primer sector. No es un flashback.
        Solo se considera cruce si la vuelta abierta es de salida de boxes: un
        retroceso de estado sin esa evidencia es un flashback.
        """
        if not pit_exit:
            return False
        track_length = state.session.track_length_m
        if (
            previous is None
            or track_length is None
            or track_length <= 0
            or car.current_lap != previous.current_lap
            or car.result_status == "finished"
        ):
            return False
        previous_distance = previous.lap_distance_m
        distance = car.lap_distance_m
        if previous_distance is None or distance is None:
            return False
        if previous_distance < track_length * 0.90:
            return False
        if distance > max(PARTIAL_LAP_MIN_DISTANCE_M, track_length * 0.01):
            return False
        time_restarted = previous.current_lap_time_ms is not None and (
            car.current_lap_time_ms is None
            or car.current_lap_time_ms <= PARTIAL_LAP_MIN_TIME_S * 1000
        )
        sector_restarted = previous.sector1_time_ms is not None and (
            car.sector1_time_ms is None or car.sector1_time_ms <= PARTIAL_LAP_MIN_TIME_S * 1000
        )
        return time_restarted or sector_restarted

    def _reset_timed_lap_after_crossing(
        self,
        open_lap: _OpenLap,
        car: CarSnapshot,
        now: float,
    ) -> None:
        """Separa el tramo cronometrado del tramo de salida de boxes.

        El tramo de salida se conserva en la propia vuelta cronometrada: la
        marca ``pit_exit`` y la bandera ``out_lap`` no se descartan, de modo que
        el histórico mantiene la evidencia de la parada y la comparabilidad
        sigue tratando la vuelta como salida. Un ``flashback`` confirmado
        tampoco se elimina.
        """
        open_lap.flags.discard("partial_lap")
        open_lap.started_at_monotonic = now
        open_lap.start_fuel_kg = car.fuel_kg
        open_lap.start_wear = _wear(car)
        open_lap.start_position = car.position
        open_lap.start_gap_ahead_s = _gap_ahead_s(car)
        open_lap.start_compound = _compound(car)
        if car.tyre_age_laps is not None:
            open_lap.start_tyre_age = car.tyre_age_laps
        open_lap.track_statuses.clear()
        open_lap.weathers.clear()
        open_lap.saw_progress = True
        self._reset_lap_accumulators(open_lap, car, flashback=False)

    def _reset_lap_accumulators(
        self,
        open_lap: _OpenLap,
        car: CarSnapshot,
        *,
        flashback: bool = True,
    ) -> None:
        if flashback:
            open_lap.flags.add("flashback")
        open_lap.damage_baseline = _damage_values(car.damage)
        open_lap.damage_max_delta_pct = None
        open_lap.track_temperature_sum_c = 0.0
        open_lap.track_temperature_sample_count = 0
        open_lap.traffic_samples = 0
        open_lap.traffic_samples_ahead = 0
        open_lap.traffic_sample_seen = False
        open_lap.last_traffic_sample_at = None
        open_lap.ers_harvested_j = None
        open_lap.ers_deployed_j = None

    def _add_session_context(
        self,
        open_lap: _OpenLap,
        state: RaceStateSnapshot,
        now: float,
    ) -> None:
        if state.session.safety_car_status is not None:
            open_lap.track_statuses.add(state.session.safety_car_status)
        if state.session.weather is not None:
            open_lap.weathers.add(state.session.weather)
        freshness = state.freshness_for("session")
        sample_at = freshness.updated_at_monotonic
        if (
            state.session.track_temperature_c is not None
            and sample_at is not None
            and not freshness.is_stale(now)
            and sample_at != open_lap.last_track_temperature_sample_at
        ):
            open_lap.track_temperature_sum_c += state.session.track_temperature_c
            open_lap.track_temperature_sample_count += 1
            open_lap.last_track_temperature_sample_at = sample_at
        if self._red_flag_pending:
            open_lap.track_statuses.add("red_flag")

    def _start_tyre_age(
        self,
        car: CarSnapshot,
        previous: CarSnapshot | None,
    ) -> int | None:
        if previous is None:
            return car.tyre_age_laps
        if self._tyres_changed(car, previous):
            if previous.tyre_age_laps is not None or car.tyre_age_laps is not None:
                return 0
            return None
        if previous.tyre_age_laps is not None:
            return previous.tyre_age_laps + 1
        return car.tyre_age_laps

    def _tyres_changed(
        self,
        car: CarSnapshot,
        previous: CarSnapshot | None,
    ) -> bool:
        if previous is None:
            return False
        pit_stops_changed = (
            previous.pit_stops is not None
            and car.pit_stops is not None
            and previous.pit_stops != car.pit_stops
        )
        previous_compound = _compound(previous)
        current_compound = _compound(car)
        compounds_changed = (
            previous_compound is not None
            and current_compound is not None
            and previous_compound != current_compound
        )
        return pit_stops_changed or compounds_changed

    def _update_pit(
        self,
        open_lap: _OpenLap,
        car: CarSnapshot,
        previous: CarSnapshot | None,
    ) -> None:
        if car.result_status in _TERMINAL_RESULT_STATUSES:
            return
        in_pits = car.pit_status in _PIT_STATUSES
        if previous is not None:
            if previous.result_status in _TERMINAL_RESULT_STATUSES:
                return
            was_in_pits = previous.pit_status in _PIT_STATUSES
            if in_pits and not was_in_pits:
                open_lap.pit_entry = True
            elif was_in_pits and not in_pits:
                open_lap.pit_exit = True
        lane_time = _pit_time(car.pit_lane_time_ms)
        if lane_time is not None:
            open_lap.pit_lane_time_s = max(open_lap.pit_lane_time_s or 0.0, lane_time)
        stop_time = _pit_time(car.pit_stop_timer_ms)
        if stop_time is not None:
            open_lap.pit_stop_time_s = max(open_lap.pit_stop_time_s or 0.0, stop_time)

    def _update_traffic(
        self,
        open_lap: _OpenLap,
        car: CarSnapshot,
        state: RaceStateSnapshot,
    ) -> None:
        timestamp = state.freshness_for("lap_data").updated_at_monotonic
        if open_lap.traffic_sample_seen and timestamp == open_lap.last_traffic_sample_at:
            return
        open_lap.traffic_sample_seen = True
        open_lap.last_traffic_sample_at = timestamp
        if car.position == 1:
            open_lap.traffic_samples += 1
            return
        gap = _gap_ahead_s(car)
        if gap is None:
            return
        open_lap.traffic_samples += 1
        if gap < self._config.traffic_gap_s:
            open_lap.traffic_samples_ahead += 1

    def _update_damage(self, open_lap: _OpenLap, car: CarSnapshot) -> None:
        current = _damage_values(car.damage)
        if current is None:
            return
        if open_lap.damage_baseline is None:
            open_lap.damage_baseline = current
            return
        delta = _max_damage_delta(open_lap.damage_baseline, current)
        open_lap.damage_max_delta_pct = max(open_lap.damage_max_delta_pct or 0.0, delta)

    def _update_ers(self, open_lap: _OpenLap, car: CarSnapshot) -> None:
        harvested = _ers_harvested(car)
        if harvested is not None:
            open_lap.ers_harvested_j = max(open_lap.ers_harvested_j or 0.0, harvested)
        deployed = car.ers_deployed_this_lap_j
        if deployed is not None:
            open_lap.ers_deployed_j = max(open_lap.ers_deployed_j or 0.0, deployed)

    def _remember_car_values(self, open_lap: _OpenLap, car: CarSnapshot) -> None:
        if car.fuel_kg is not None:
            open_lap.last_fuel_kg = car.fuel_kg
        if car.tyre_age_laps is not None:
            open_lap.last_tyre_age = car.tyre_age_laps
        wear = _wear(car)
        if wear is not None:
            open_lap.last_wear = wear
        if car.position is not None:
            open_lap.last_position = car.position
        gap = _gap_ahead_s(car)
        if gap is not None:
            open_lap.last_gap_ahead_s = gap
        compound = _compound(car)
        if compound is not None:
            open_lap.last_compound = compound

    def _mark_progress(
        self,
        open_lap: _OpenLap,
        car: CarSnapshot,
        previous: CarSnapshot | None,
    ) -> None:
        if open_lap.saw_progress:
            return
        if car.current_lap_time_ms is not None or (
            previous is not None
            and car.lap_distance_m is not None
            and car.lap_distance_m != previous.lap_distance_m
        ):
            open_lap.saw_progress = True

    def _handle_flashback(
        self,
        car_index: int,
        car: CarSnapshot,
        state: RaceStateSnapshot,
        now: float,
    ) -> None:
        self._open_laps.pop(car_index, None)
        assert car.current_lap is not None
        self._history.discard_after(car_index, car.current_lap - 1)
        self._pending_flashback_laps[car_index] = car.current_lap
        self._open_laps[car_index] = self._open_lap(car_index, car, None, state, now)

    def _finish_retired_lap(self, open_lap: _OpenLap, car: CarSnapshot, now: float) -> None:
        self._finish_lap(
            open_lap,
            car,
            now,
            extra_flags=("retired",),
        )
        self._retired_cars.add(open_lap.car_index)

    def _close_open_laps(self, state: RaceStateSnapshot, now: float) -> None:
        total_laps = state.session.total_laps
        if total_laps is None:
            self._open_laps.clear()
            return
        for car_index, open_lap in tuple(self._open_laps.items()):
            if not open_lap.saw_progress:
                continue
            car = state.cars[car_index]
            previous = self._last_cars[car_index] if car_index < len(self._last_cars) else None
            self._update_lap(open_lap, car, previous, state, now)
            lap_time_s = (
                car.current_lap_time_ms / 1000.0 if car.current_lap_time_ms is not None else None
            )
            tyre_age_end = (
                open_lap.start_tyre_age + 1
                if open_lap.start_tyre_age is not None
                else car.tyre_age_laps
            )
            self._finish_lap(
                open_lap,
                car,
                now,
                lap_time_s=lap_time_s,
                tyre_age_end=tyre_age_end,
                extra_flags=("incomplete",),
            )
        self._open_laps.clear()

    def _finish_lap(
        self,
        open_lap: _OpenLap,
        end_car: CarSnapshot,
        now: float,
        *,
        lap_time_s: float | None = None,
        tyre_age_end: int | None = None,
        extra_flags: tuple[str, ...] = (),
    ) -> None:
        flags = set(open_lap.flags)
        flags.update(extra_flags)
        fuel_end_kg = end_car.fuel_kg
        if fuel_end_kg is None:
            fuel_end_kg = open_lap.last_fuel_kg
        wear_end = _wear(end_car) or open_lap.last_wear
        position_end = end_car.position
        if position_end is None:
            position_end = open_lap.last_position
        gap_ahead_end_s = _gap_ahead_s(end_car)
        if gap_ahead_end_s is None:
            gap_ahead_end_s = open_lap.last_gap_ahead_s
        compound = _compound(end_car) or open_lap.last_compound or open_lap.start_compound
        age_end = tyre_age_end
        if age_end is None and open_lap.start_tyre_age is not None:
            age_end = open_lap.start_tyre_age + 1
        if age_end is None:
            age_end = end_car.tyre_age_laps
        if age_end is None:
            age_end = open_lap.last_tyre_age
        traffic_fraction = (
            open_lap.traffic_samples_ahead / open_lap.traffic_samples
            if open_lap.traffic_samples
            else None
        )
        record = LapRecord(
            car_index=open_lap.car_index,
            lap_number=open_lap.lap_number,
            lap_time_s=lap_time_s,
            lap_time_source="lap_data" if lap_time_s is not None else None,
            valid=(
                None if end_car.current_lap_invalid is None else not end_car.current_lap_invalid
            ),
            compound=compound,
            tyre_age_start=open_lap.start_tyre_age,
            tyre_age_end=age_end,
            fuel_start_kg=open_lap.start_fuel_kg,
            fuel_end_kg=fuel_end_kg,
            tyre_wear_start_pct=open_lap.start_wear,
            tyre_wear_end_pct=wear_end,
            position_start=open_lap.start_position,
            position_end=position_end,
            gap_ahead_start_s=open_lap.start_gap_ahead_s,
            gap_ahead_end_s=gap_ahead_end_s,
            ers_harvested_j=open_lap.ers_harvested_j,
            ers_deployed_j=open_lap.ers_deployed_j,
            pit_entry=open_lap.pit_entry,
            pit_exit=open_lap.pit_exit,
            pit_lane_time_s=open_lap.pit_lane_time_s,
            pit_stop_time_s=open_lap.pit_stop_time_s,
            track_status_seen=frozenset(open_lap.track_statuses),
            weather_seen=frozenset(open_lap.weathers),
            track_temperature_c=(
                open_lap.track_temperature_sum_c / open_lap.track_temperature_sample_count
                if open_lap.track_temperature_sample_count
                else None
            ),
            traffic_fraction=traffic_fraction,
            damage_max_delta_pct=open_lap.damage_max_delta_pct,
            flags=frozenset(flags),
            closed=True,
            started_at_monotonic=open_lap.started_at_monotonic,
            ended_at_monotonic=now,
        )
        self._history.upsert(record)


def _ers_harvested(car: CarSnapshot) -> float | None:
    if car.ers_harvested_mguk_j is None and car.ers_harvested_mguh_j is None:
        return None
    return (car.ers_harvested_mguk_j or 0.0) + (car.ers_harvested_mguh_j or 0.0)
