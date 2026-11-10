"""Single-writer race state and immutable snapshots for presentation adapters."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

from .parser import (
    MAX_CARS,
    CarDamagePacket,
    CarStatusPacket,
    EnumValue,
    EventDetail,
    EventPacket,
    FinalClassificationPacket,
    LapDataPacket,
    ParsedPacket,
    ParticipantsPacket,
    SessionPacket,
    TyreSetsPacket,
)

_EVENT_HISTORY_LIMIT = 50
_TERMINAL_RESULT_STATUSES = frozenset(
    {"finished", "did_not_finish", "disqualified", "not_classified", "retired"}
)
# Thresholds allow several missed intervals; event-driven groups have no periodic expiry.
_PACKET_GROUPS = {
    1: ("session", 2.5),
    2: ("lap_data", 1.5),
    3: ("event", None),
    4: ("participants", 12.0),
    7: ("car_status", 1.5),
    8: ("final_classification", None),
    10: ("car_damage", 3.0),
    11: ("session_history", 5.0),
    12: ("tyre_sets", 5.0),
}


def _known_enum(value: EnumValue) -> str | None:
    return value.name if value.name != "unknown" else None


def _enum_label(value: EnumValue) -> str:
    return value.name if value.name != "unknown" else f"unknown:{value.raw}"


def _positive_int(value: int) -> int | None:
    return value if value > 0 else None


@dataclass(frozen=True, slots=True)
class CarDamageSnapshot:
    tyres_wear: tuple[float, ...]
    tyres_damage: tuple[int, ...]
    brakes_damage: tuple[int, ...]
    tyre_blisters: tuple[int, ...]
    front_left_wing_damage: int
    front_right_wing_damage: int
    rear_wing_damage: int
    floor_damage: int
    diffuser_damage: int
    sidepod_damage: int
    drs_fault: str | None
    ers_fault: str | None
    gearbox_damage: int
    engine_damage: int
    engine_mguh_wear: int
    engine_es_wear: int
    engine_ce_wear: int
    engine_ice_wear: int
    engine_mguk_wear: int
    engine_tc_wear: int
    engine_blown: str | None
    engine_seized: str | None


@dataclass(frozen=True, slots=True)
class FittedTyreSnapshot:
    actual_tyre_compound: str | None
    visual_tyre_compound: str | None
    wear_pct: float
    lifespan_laps: int
    usable_life_laps: int
    observed_at_monotonic: float = 0.0


@dataclass(frozen=True, slots=True)
class TyreSetSnapshot:
    index: int
    actual_tyre_compound: str | None
    visual_tyre_compound: str | None
    wear_pct: float
    available: bool | None
    recommended_session: str | None
    lifespan_laps: int
    usable_life_laps: int
    lap_delta_time_ms: int
    fitted: bool | None


@dataclass(frozen=True, slots=True)
class CarSnapshot:
    car_index: int
    name: str | None = None
    team: str | None = None
    grid_position: int | None = None
    position: int | None = None
    current_lap: int | None = None
    pit_status: str | None = None
    pit_stops: int | None = None
    driver_status: str | None = None
    fuel_kg: float | None = None
    actual_tyre_compound: str | None = None
    visual_tyre_compound: str | None = None
    tyre_age_laps: int | None = None
    damage: CarDamageSnapshot | None = None
    last_lap_time_ms: int | None = None
    current_lap_time_ms: int | None = None
    sector1_time_ms: int | None = None
    sector2_time_ms: int | None = None
    delta_to_car_in_front_ms: int | None = None
    delta_to_leader_ms: int | None = None
    lap_distance_m: float | None = None
    current_lap_invalid: bool | None = None
    penalties_s: float | None = None
    result_status: str | None = None
    pit_lane_timer_active: bool | None = None
    pit_lane_time_ms: int | None = None
    pit_stop_timer_ms: int | None = None
    fuel_capacity_kg: float | None = None
    fuel_remaining_laps: float | None = None
    ers_store_j: float | None = None
    ers_deploy_mode: str | None = None
    ers_harvested_mguk_j: float | None = None
    ers_harvested_mguh_j: float | None = None
    ers_deployed_this_lap_j: float | None = None
    fia_flag: str | None = None
    drs_allowed: bool | None = None
    fitted_tyre: FittedTyreSnapshot | None = None
    telemetry_restricted: bool | None = None


@dataclass(frozen=True, slots=True)
class PacketFreshness:
    group: str
    updated_at_monotonic: float | None
    stale_after_seconds: float | None

    def age_seconds(self, now: float) -> float | None:
        if self.updated_at_monotonic is None:
            return None
        return max(0.0, now - self.updated_at_monotonic)

    def is_stale(self, now: float) -> bool:
        age = self.age_seconds(now)
        return (
            age is not None
            and self.stale_after_seconds is not None
            and age > self.stale_after_seconds
        )


@dataclass(frozen=True, slots=True)
class RaceEvent:
    kind: str
    occurred_at_monotonic: float
    car_index: int | None = None
    lap_number: int | None = None
    session_uid: int | None = None
    value: str | None = None
    previous_value: str | None = None


@dataclass(frozen=True, slots=True)
class WeatherForecastSnapshot:
    offset_minutes: int
    weather: str | None
    track_temperature_c: float
    air_temperature_c: float
    rain_percentage: int
    session_type: str | None = None


@dataclass(frozen=True, slots=True)
class RaceSessionSnapshot:
    uid: int | None
    session_type: str | None
    track: str | None
    total_laps: int | None
    started: bool | None
    ended: bool | None
    weather: str | None = None
    track_temperature_c: float | None = None
    air_temperature_c: float | None = None
    safety_car_status: str | None = None
    session_time_left_s: float | None = None
    track_length_m: float | None = None
    pit_speed_limit_kph: float | None = None
    pit_window_ideal_lap: int | None = None
    pit_window_latest_lap: int | None = None
    pit_rejoin_position: int | None = None
    forecast: tuple[WeatherForecastSnapshot, ...] = ()
    game_paused: bool | None = None
    forecast_accuracy: str | None = None
    safety_car_periods: int | None = None
    virtual_safety_car_periods: int | None = None
    red_flag_periods: int | None = None
    season_link_id: int | None = None
    weekend_link_id: int | None = None
    session_link_id: int | None = None
    weekend_structure: tuple[str, ...] = ()
    num_sessions_in_weekend: int | None = None


@dataclass(frozen=True, slots=True)
class RaceStateSnapshot:
    session: RaceSessionSnapshot
    current_lap: int | None
    player_car_index: int | None
    cars: tuple[CarSnapshot, ...]
    freshness: tuple[PacketFreshness, ...]
    events: tuple[RaceEvent, ...]
    tyre_inventory: tuple[TyreSetSnapshot, ...] = ()
    tyre_inventory_updated_at_monotonic: float | None = None

    @property
    def player_car(self) -> CarSnapshot | None:
        if self.player_car_index is None:
            return None
        return self.cars[self.player_car_index]

    def freshness_for(self, group: str) -> PacketFreshness:
        for item in self.freshness:
            if item.group == group:
                return item
        raise KeyError(group)


class RaceState:
    """Applies parsed packets from one consumer thread and publishes snapshots."""

    def __init__(self, event_sink: Callable[[RaceEvent], None] | None = None) -> None:
        self._event_sink = event_sink
        self._session_uid: int | None = None
        self._session_type: str | None = None
        self._track: str | None = None
        self._total_laps: int | None = None
        self._weather: str | None = None
        self._track_temperature_c: float | None = None
        self._air_temperature_c: float | None = None
        self._safety_car_status: str | None = None
        self._session_time_left_s: float | None = None
        self._track_length_m: float | None = None
        self._pit_speed_limit_kph: float | None = None
        self._pit_window_ideal_lap: int | None = None
        self._pit_window_latest_lap: int | None = None
        self._pit_rejoin_position: int | None = None
        self._forecast: tuple[WeatherForecastSnapshot, ...] = ()
        self._forecast_accuracy: str | None = None
        self._safety_car_periods: int | None = None
        self._virtual_safety_car_periods: int | None = None
        self._red_flag_periods: int | None = None
        self._season_link_id: int | None = None
        self._weekend_link_id: int | None = None
        self._session_link_id: int | None = None
        self._weekend_structure: tuple[str, ...] = ()
        self._num_sessions_in_weekend: int | None = None
        self._game_paused: bool | None = None
        self._current_lap: int | None = None
        self._player_car_index: int | None = None
        self._session_started: bool | None = None
        self._session_ended: bool | None = None
        self._cars = [CarSnapshot(car_index=index) for index in range(MAX_CARS)]
        self._tyre_inventory: tuple[TyreSetSnapshot, ...] = ()
        self._tyre_inventory_updated_at_monotonic: float | None = None
        self._updated_at: dict[str, float | None] = {
            group: None for group, _ in _PACKET_GROUPS.values()
        }
        self._events: deque[RaceEvent] = deque(maxlen=_EVENT_HISTORY_LIMIT)
        self._retirement_events: set[int] = set()
        self._snapshot = self._build_snapshot()

    def snapshot(self) -> RaceStateSnapshot:
        return self._snapshot

    def apply(self, packet: ParsedPacket, observed_at: float) -> None:
        header = packet.header
        # Some non-lifecycle Event packets use a different UID and must not reset the session.
        is_session_event = isinstance(packet, EventPacket) and packet.event_code.name in {
            "session_started",
            "session_ended",
        }
        is_authoritative_session_packet = isinstance(packet, SessionPacket) or is_session_event
        if isinstance(packet, EventPacket) and not is_session_event:
            if self._session_uid is None or header.session_uid != self._session_uid:
                return
        elif (
            isinstance(packet, FinalClassificationPacket)
            and header.session_uid == 0
            and self._session_uid not in (None, 0)
        ):
            player_index = header.player_car_index
            if (
                player_index >= MAX_CARS
                or player_index != self._player_car_index
                or self._cars[player_index].current_lap is None
                or player_index >= len(packet.classification)
                or packet.classification[player_index] is None
            ):
                return
            classification = packet.classification[player_index]
            assert classification is not None
            if classification.laps > (self._cars[player_index].current_lap or 0):
                return
            self._updated_at["final_classification"] = observed_at
            self._apply_classification(packet, player_index)
            self._snapshot = self._build_snapshot()
            return
        elif (
            self._session_uid is not None
            and header.session_uid != self._session_uid
            and not is_authoritative_session_packet
        ):
            return
        self._ensure_session(header.session_uid, observed_at)
        player_car_index = header.player_car_index if header.player_car_index < MAX_CARS else None
        if self._player_car_index != player_car_index:
            self._current_lap = None
        self._player_car_index = player_car_index
        if player_car_index is not None:
            player_car = self._cars[player_car_index]
            if player_car.telemetry_restricted is True:
                self._cars[player_car_index] = replace(
                    player_car,
                    telemetry_restricted=False,
                )
        group, _ = _PACKET_GROUPS[header.packet_id]
        self._updated_at[group] = observed_at

        if isinstance(packet, SessionPacket):
            self._apply_session(packet, observed_at)
        elif isinstance(packet, ParticipantsPacket):
            self._apply_participants(packet)
        elif isinstance(packet, LapDataPacket):
            self._apply_lap_data(packet, observed_at)
        elif isinstance(packet, CarStatusPacket):
            self._apply_car_status(packet)
        elif isinstance(packet, TyreSetsPacket):
            self._apply_tyre_sets(packet)
        elif isinstance(packet, CarDamagePacket):
            self._apply_car_damage(packet)
        elif isinstance(packet, EventPacket):
            self._apply_event(packet, observed_at)
        elif isinstance(packet, FinalClassificationPacket):
            self._apply_classification(packet, self._player_car_index)
            self._mark_session_ended(observed_at)

        self._snapshot = self._build_snapshot()

    def _ensure_session(self, session_uid: int, observed_at: float) -> None:
        if self._session_uid is None:
            self._session_uid = session_uid
            return
        if self._session_uid == session_uid:
            return

        self._session_uid = session_uid
        self._session_type = None
        self._track = None
        self._total_laps = None
        self._weather = None
        self._track_temperature_c = None
        self._air_temperature_c = None
        self._safety_car_status = None
        self._session_time_left_s = None
        self._track_length_m = None
        self._pit_speed_limit_kph = None
        self._pit_window_ideal_lap = None
        self._pit_window_latest_lap = None
        self._pit_rejoin_position = None
        self._forecast = ()
        self._forecast_accuracy = None
        self._safety_car_periods = None
        self._virtual_safety_car_periods = None
        self._red_flag_periods = None
        self._season_link_id = None
        self._weekend_link_id = None
        self._session_link_id = None
        self._weekend_structure = ()
        self._num_sessions_in_weekend = None
        self._game_paused = None
        self._current_lap = None
        self._player_car_index = None
        self._session_started = None
        self._session_ended = None
        self._cars = [CarSnapshot(car_index=index) for index in range(MAX_CARS)]
        self._tyre_inventory = ()
        self._tyre_inventory_updated_at_monotonic = None
        for group in self._updated_at:
            self._updated_at[group] = None
        self._events.clear()
        self._retirement_events.clear()
        self._append_event("session_changed", observed_at, session_uid=session_uid)

    def _apply_session(self, packet: SessionPacket, observed_at: float) -> None:
        """Only changes between known weather/SC states emit transition events."""
        weather = _known_enum(packet.weather)
        safety_car_status = _known_enum(packet.safety_car_status)
        previous_safety_car_status = self._safety_car_status
        if (
            previous_safety_car_status is not None
            and safety_car_status is not None
            and safety_car_status != previous_safety_car_status
        ):
            self._append_event(
                "safety_car_changed",
                observed_at,
                value=safety_car_status,
                previous_value=previous_safety_car_status,
            )
        previous_weather = self._weather
        if previous_weather is not None and weather is not None and weather != previous_weather:
            self._append_event(
                "weather_changed",
                observed_at,
                value=weather,
                previous_value=previous_weather,
            )

        red_flag_periods = packet.session_settings.red_flag_periods
        if self._red_flag_periods is not None and red_flag_periods > self._red_flag_periods:
            self._append_event("red_flag", observed_at)

        self._session_type = _known_enum(packet.session_type)
        self._track = _known_enum(packet.track_id)
        self._total_laps = packet.total_laps or None
        self._weather = weather
        self._track_temperature_c = float(packet.track_temperature)
        self._air_temperature_c = float(packet.air_temperature)
        self._safety_car_status = safety_car_status
        self._session_time_left_s = float(packet.session_time_left)
        self._track_length_m = float(packet.track_length) if packet.track_length > 0 else None
        self._pit_speed_limit_kph = (
            float(packet.pit_speed_limit) if packet.pit_speed_limit > 0 else None
        )
        self._pit_window_ideal_lap = _positive_int(packet.pit_stop_window_ideal_lap)
        self._pit_window_latest_lap = _positive_int(packet.pit_stop_window_latest_lap)
        self._pit_rejoin_position = _positive_int(packet.pit_stop_rejoin_position)
        self._forecast = tuple(
            WeatherForecastSnapshot(
                offset_minutes=sample.time_offset_minutes,
                weather=_known_enum(sample.weather),
                track_temperature_c=float(sample.track_temperature),
                air_temperature_c=float(sample.air_temperature),
                rain_percentage=sample.rain_percentage,
                session_type=_known_enum(sample.session_type),
            )
            for sample in packet.weather_forecast
        )
        self._forecast_accuracy = _known_enum(packet.forecast_accuracy)
        self._safety_car_periods = packet.session_settings.safety_car_periods
        self._virtual_safety_car_periods = packet.session_settings.virtual_safety_car_periods
        self._red_flag_periods = red_flag_periods
        self._season_link_id = _positive_int(packet.season_link_identifier)
        self._weekend_link_id = _positive_int(packet.weekend_link_identifier)
        self._session_link_id = _positive_int(packet.session_link_identifier)
        self._weekend_structure = tuple(
            _enum_label(session_type) for session_type in packet.weekend_structure
        )
        self._num_sessions_in_weekend = _positive_int(
            packet.session_settings.num_sessions_in_weekend
        )
        self._game_paused = packet.game_paused.value

    def _apply_participants(self, packet: ParticipantsPacket) -> None:
        for index, participant in enumerate(packet.participants):
            if participant is None:
                continue
            car = self._cars[index]
            telemetry_restricted = (
                False
                if index == self._player_car_index
                else {"restricted": True, "public": False}.get(participant.your_telemetry.name)
            )
            self._cars[index] = replace(
                car,
                name=participant.name or None,
                team=_known_enum(participant.team_id),
                telemetry_restricted=telemetry_restricted,
                fuel_kg=None if telemetry_restricted else car.fuel_kg,
                fuel_capacity_kg=None if telemetry_restricted else car.fuel_capacity_kg,
                fuel_remaining_laps=None if telemetry_restricted else car.fuel_remaining_laps,
                ers_store_j=None if telemetry_restricted else car.ers_store_j,
                ers_deploy_mode=None if telemetry_restricted else car.ers_deploy_mode,
                ers_harvested_mguk_j=(None if telemetry_restricted else car.ers_harvested_mguk_j),
                ers_harvested_mguh_j=(None if telemetry_restricted else car.ers_harvested_mguh_j),
                ers_deployed_this_lap_j=(
                    None if telemetry_restricted else car.ers_deployed_this_lap_j
                ),
                damage=None if telemetry_restricted else car.damage,
                fitted_tyre=None if telemetry_restricted else car.fitted_tyre,
            )

    def _apply_lap_data(self, packet: LapDataPacket, observed_at: float) -> None:
        player_index = self._player_car_index
        for index, lap_data in enumerate(packet.cars):
            previous = self._cars[index]
            pit_status = _known_enum(lap_data.pit_status)
            current_lap = lap_data.current_lap_number or None
            position = lap_data.car_position or None
            reported_result_status = _known_enum(lap_data.result_status)
            result_status = (
                previous.result_status
                if previous.result_status in _TERMINAL_RESULT_STATUSES
                and reported_result_status not in _TERMINAL_RESULT_STATUSES
                else reported_result_status
            )
            publishes_values = result_status not in {"invalid", "inactive"}
            current = replace(
                previous,
                grid_position=(
                    lap_data.grid_position_zero_based + 1
                    if 0 <= lap_data.grid_position_zero_based < MAX_CARS
                    else None
                ),
                position=position,
                current_lap=current_lap,
                pit_status=pit_status,
                pit_stops=lap_data.pit_stops,
                driver_status=_known_enum(lap_data.driver_status),
                last_lap_time_ms=(
                    _positive_int(lap_data.last_lap_time_ms) if publishes_values else None
                ),
                current_lap_time_ms=(
                    _positive_int(lap_data.current_lap_time_ms) if publishes_values else None
                ),
                sector1_time_ms=(
                    _positive_int(lap_data.sector1_time_ms) if publishes_values else None
                ),
                sector2_time_ms=(
                    _positive_int(lap_data.sector2_time_ms) if publishes_values else None
                ),
                delta_to_car_in_front_ms=(
                    _positive_int(lap_data.delta_to_car_in_front_ms) if publishes_values else None
                ),
                delta_to_leader_ms=(
                    _positive_int(lap_data.delta_to_race_leader_ms)
                    if publishes_values and position != 1
                    else None
                ),
                lap_distance_m=lap_data.lap_distance if publishes_values else None,
                current_lap_invalid=(
                    lap_data.current_lap_invalid.value if publishes_values else None
                ),
                penalties_s=float(lap_data.penalties) if publishes_values else None,
                result_status=result_status,
                pit_lane_timer_active=(
                    lap_data.pit_lane_timer_active.value if publishes_values else None
                ),
                pit_lane_time_ms=(
                    _positive_int(lap_data.pit_lane_time_ms) if publishes_values else None
                ),
                pit_stop_timer_ms=(
                    _positive_int(lap_data.pit_stop_timer_ms) if publishes_values else None
                ),
            )
            self._cars[index] = current

            if index == player_index:
                if (
                    previous.current_lap is not None
                    and current_lap is not None
                    and previous.current_lap != current_lap
                ):
                    self._current_lap = current_lap
                    self._append_event(
                        "lap_changed",
                        observed_at,
                        car_index=index,
                        lap_number=current_lap,
                    )
                elif current_lap is not None:
                    self._current_lap = current_lap

            was_in_pits = previous.pit_status in {"pitting", "in_pit_area"}
            is_in_pits = pit_status in {"pitting", "in_pit_area"}
            terminal = result_status in _TERMINAL_RESULT_STATUSES
            if previous.pit_status is not None and is_in_pits and not was_in_pits and not terminal:
                self._append_event("pit_entry", observed_at, car_index=index)
            elif was_in_pits and pit_status == "none" and not terminal:
                self._append_event("pit_exit", observed_at, car_index=index)
            if (
                previous.pit_stops is not None
                and lap_data.pit_stops > previous.pit_stops
                and not terminal
            ):
                self._append_event("pit_stop_completed", observed_at, car_index=index)

    def _apply_car_status(self, packet: CarStatusPacket) -> None:
        for index, status in enumerate(packet.cars):
            car = self._cars[index]
            restricted = car.telemetry_restricted is True or (
                car.telemetry_restricted is None and status.fuel_capacity == 0
            )
            drs_allowed = {"allowed": True, "not_allowed": False}.get(status.drs_allowed.name)
            self._cars[index] = replace(
                car,
                fuel_kg=None if restricted else status.fuel_in_tank,
                actual_tyre_compound=_known_enum(status.actual_tyre_compound),
                visual_tyre_compound=_known_enum(status.visual_tyre_compound),
                tyre_age_laps=status.tyres_age_laps,
                fuel_capacity_kg=None if restricted else status.fuel_capacity,
                fuel_remaining_laps=None if restricted else status.fuel_remaining_laps,
                ers_store_j=None if restricted else status.ers_store_energy,
                ers_deploy_mode=(None if restricted else _known_enum(status.ers_deploy_mode)),
                ers_harvested_mguk_j=(None if restricted else status.ers_harvested_this_lap_mguk),
                ers_harvested_mguh_j=(None if restricted else status.ers_harvested_this_lap_mguh),
                ers_deployed_this_lap_j=None if restricted else status.ers_deployed_this_lap,
                fia_flag=_known_enum(status.vehicle_fia_flags),
                drs_allowed=drs_allowed,
            )

    def _apply_tyre_sets(self, packet: TyreSetsPacket) -> None:
        if not 0 <= packet.car_index < MAX_CARS:
            return
        restricted = self._is_telemetry_restricted(packet.car_index)
        is_player_car = packet.car_index == self._player_car_index
        updated_at = self._updated_at["tyre_sets"]
        assert updated_at is not None
        fitted_tyre: FittedTyreSnapshot | None = None
        if not restricted and 0 <= packet.fitted_index < len(packet.tyre_sets):
            tyre_set = packet.tyre_sets[packet.fitted_index]
            fitted_tyre = FittedTyreSnapshot(
                actual_tyre_compound=_known_enum(tyre_set.actual_tyre_compound),
                visual_tyre_compound=_known_enum(tyre_set.visual_tyre_compound),
                wear_pct=float(tyre_set.wear),
                lifespan_laps=tyre_set.lifespan,
                usable_life_laps=tyre_set.usable_life,
                observed_at_monotonic=updated_at,
            )
        car = self._cars[packet.car_index]
        self._cars[packet.car_index] = replace(car, fitted_tyre=fitted_tyre)
        if is_player_car:
            self._tyre_inventory = (
                ()
                if restricted
                else tuple(
                    TyreSetSnapshot(
                        index=index,
                        actual_tyre_compound=_known_enum(tyre_set.actual_tyre_compound),
                        visual_tyre_compound=_known_enum(tyre_set.visual_tyre_compound),
                        wear_pct=float(tyre_set.wear),
                        available=tyre_set.available.value,
                        recommended_session=_known_enum(tyre_set.recommended_session),
                        lifespan_laps=tyre_set.lifespan,
                        usable_life_laps=tyre_set.usable_life,
                        lap_delta_time_ms=tyre_set.lap_delta_time_ms,
                        fitted=tyre_set.fitted.value,
                    )
                    for index, tyre_set in enumerate(packet.tyre_sets)
                )
            )
            self._tyre_inventory_updated_at_monotonic = updated_at

    def _apply_car_damage(self, packet: CarDamagePacket) -> None:
        for index, damage in enumerate(packet.cars):
            if self._is_telemetry_restricted(index):
                self._cars[index] = replace(self._cars[index], damage=None)
                continue
            self._cars[index] = replace(
                self._cars[index],
                damage=CarDamageSnapshot(
                    tyres_wear=damage.tyres_wear,
                    tyres_damage=damage.tyres_damage,
                    brakes_damage=damage.brakes_damage,
                    tyre_blisters=damage.tyre_blisters,
                    front_left_wing_damage=damage.front_left_wing_damage,
                    front_right_wing_damage=damage.front_right_wing_damage,
                    rear_wing_damage=damage.rear_wing_damage,
                    floor_damage=damage.floor_damage,
                    diffuser_damage=damage.diffuser_damage,
                    sidepod_damage=damage.sidepod_damage,
                    drs_fault=_known_enum(damage.drs_fault),
                    ers_fault=_known_enum(damage.ers_fault),
                    gearbox_damage=damage.gearbox_damage,
                    engine_damage=damage.engine_damage,
                    engine_mguh_wear=damage.engine_mguh_wear,
                    engine_es_wear=damage.engine_es_wear,
                    engine_ce_wear=damage.engine_ce_wear,
                    engine_ice_wear=damage.engine_ice_wear,
                    engine_mguk_wear=damage.engine_mguk_wear,
                    engine_tc_wear=damage.engine_tc_wear,
                    engine_blown=_known_enum(damage.engine_blown),
                    engine_seized=_known_enum(damage.engine_seized),
                ),
            )

    def _is_telemetry_restricted(self, car_index: int) -> bool:
        if car_index == self._player_car_index:
            return False
        return (
            car_index != self._player_car_index
            and self._cars[car_index].telemetry_restricted is True
        )

    def _apply_event(self, packet: EventPacket, observed_at: float) -> None:
        event_name = packet.event_code.name
        if event_name == "session_started":
            if self._session_ended is True:
                return
            if self._session_started is not True:
                self._append_event("session_started", observed_at)
            self._session_started = True
        elif event_name == "session_ended":
            self._mark_session_ended(observed_at)
        elif event_name == "retirement" and isinstance(packet.detail, EventDetail):
            values = packet.detail.values
            if len(values) >= 2 and isinstance(values[0], int) and isinstance(values[1], EnumValue):
                car_index = values[0]
                if 0 <= car_index < len(self._cars) and car_index not in self._retirement_events:
                    car = self._cars[car_index]
                    reason = _enum_label(values[1])
                    self._append_event(
                        "retirement",
                        observed_at,
                        car_index=car_index,
                        value=reason,
                        previous_value=car.result_status,
                    )
                    self._retirement_events.add(car_index)
                    if car.result_status not in _TERMINAL_RESULT_STATUSES:
                        self._cars[car_index] = replace(car, result_status="retired")

    def _apply_classification(
        self,
        packet: FinalClassificationPacket,
        player_index: int | None,
    ) -> None:
        if player_index is None or not 0 <= player_index < len(packet.classification):
            return
        result = packet.classification[player_index]
        if result is None:
            return
        car = self._cars[player_index]
        self._cars[player_index] = replace(
            car,
            result_status=_enum_label(result.result_status),
        )

    def _mark_session_ended(self, observed_at: float) -> None:
        if self._session_ended is not True:
            self._append_event("session_ended", observed_at)
        self._session_ended = True

    def _append_event(
        self,
        kind: str,
        observed_at: float,
        *,
        car_index: int | None = None,
        lap_number: int | None = None,
        session_uid: int | None = None,
        value: str | None = None,
        previous_value: str | None = None,
    ) -> None:
        event = RaceEvent(
            kind,
            observed_at,
            car_index,
            lap_number,
            session_uid,
            value,
            previous_value,
        )
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)

    def _build_snapshot(self) -> RaceStateSnapshot:
        return RaceStateSnapshot(
            session=RaceSessionSnapshot(
                uid=self._session_uid,
                session_type=self._session_type,
                track=self._track,
                total_laps=self._total_laps,
                started=self._session_started,
                ended=self._session_ended,
                weather=self._weather,
                track_temperature_c=self._track_temperature_c,
                air_temperature_c=self._air_temperature_c,
                safety_car_status=self._safety_car_status,
                session_time_left_s=self._session_time_left_s,
                track_length_m=self._track_length_m,
                pit_speed_limit_kph=self._pit_speed_limit_kph,
                pit_window_ideal_lap=self._pit_window_ideal_lap,
                pit_window_latest_lap=self._pit_window_latest_lap,
                pit_rejoin_position=self._pit_rejoin_position,
                forecast=self._forecast,
                game_paused=self._game_paused,
                forecast_accuracy=self._forecast_accuracy,
                safety_car_periods=self._safety_car_periods,
                virtual_safety_car_periods=self._virtual_safety_car_periods,
                red_flag_periods=self._red_flag_periods,
                season_link_id=self._season_link_id,
                weekend_link_id=self._weekend_link_id,
                session_link_id=self._session_link_id,
                weekend_structure=self._weekend_structure,
                num_sessions_in_weekend=self._num_sessions_in_weekend,
            ),
            current_lap=self._current_lap,
            player_car_index=self._player_car_index,
            cars=tuple(self._cars),
            freshness=tuple(
                PacketFreshness(group, self._updated_at[group], stale_after)
                for group, stale_after in _PACKET_GROUPS.values()
            ),
            events=tuple(self._events),
            tyre_inventory=self._tyre_inventory,
            tyre_inventory_updated_at_monotonic=self._tyre_inventory_updated_at_monotonic,
        )
