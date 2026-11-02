"""Decodificadores tipados para los paquetes UDP F1 25 (formato 2025)."""

from __future__ import annotations

import math
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType

from .packets import HEADER_SIZE, PacketHeader, packet_name, parse_header

MAX_CARS = 22


class PacketParseError(ValueError):
    """Un datagrama no se puede interpretar con los layouts soportados."""

    def __init__(self, code: str, **parameters: str | int | float) -> None:
        self.code = code
        self.parameters = tuple(sorted(parameters.items()))
        super().__init__(code)

    @property
    def issue(self) -> ParseIssue:
        return ParseIssue(self.code, self.parameters)


class UnsupportedFormatError(PacketParseError):
    """El formato o la versión del juego no está soportado."""


class UnsupportedPacketTypeError(PacketParseError):
    """El tipo de paquete no tiene decodificador en esta versión."""


class PacketSizeError(PacketParseError):
    """El tamaño del datagrama no coincide con su layout."""


@dataclass(frozen=True, slots=True)
class ParseIssue:
    code: str
    parameters: tuple[tuple[str, str | int | float], ...] = ()


@dataclass(frozen=True, slots=True)
class EnumValue:
    raw: int | str
    name: str


@dataclass(frozen=True, slots=True)
class BooleanValue:
    raw: int
    value: bool | None


def _enum(category: str, raw: int | str) -> EnumValue:
    if category not in _ENUMS:
        raise ValueError(f"Enum category is not registered: {category}")
    name = _ENUMS[category].get(raw, "unknown")
    return EnumValue(raw, name)


def _boolean(raw: int) -> BooleanValue:
    return BooleanValue(raw, bool(raw) if raw in (0, 1) else None)


_ENUMS: dict[str, dict[int | str, str]] = {
    "weather": {
        0: "clear",
        1: "light_cloud",
        2: "overcast",
        3: "light_rain",
        4: "heavy_rain",
        5: "storm",
    },
    "formula": {
        0: "f1_modern",
        1: "f1_classic",
        2: "f2",
        3: "f1_generic",
        4: "beta",
        6: "esports",
        8: "f1_world",
        9: "f1_elimination",
    },
    "session_type": {
        0: "unknown",
        1: "practice_1",
        2: "practice_2",
        3: "practice_3",
        4: "short_practice",
        5: "qualifying_1",
        6: "qualifying_2",
        7: "qualifying_3",
        8: "short_qualifying",
        9: "one_shot_qualifying",
        10: "sprint_shootout_1",
        11: "sprint_shootout_2",
        12: "sprint_shootout_3",
        13: "short_sprint_shootout",
        14: "one_shot_sprint_shootout",
        15: "race",
        16: "race_2",
        17: "race_3",
        18: "time_trial",
    },
    "track_id": {
        -1: "unknown",
        0: "melbourne",
        2: "shanghai",
        3: "sakhir",
        4: "catalunya",
        5: "monaco",
        6: "montreal",
        7: "silverstone",
        9: "hungaroring",
        10: "spa",
        11: "monza",
        12: "singapore",
        13: "suzuka",
        14: "abu_dhabi",
        15: "texas",
        16: "brazil",
        17: "austria",
        19: "mexico",
        20: "baku",
        26: "zandvoort",
        27: "imola",
        29: "jeddah",
        30: "miami",
        31: "las_vegas",
        32: "losail",
        39: "silverstone_reverse",
        40: "austria_reverse",
        41: "zandvoort_reverse",
    },
    "safety_car": {
        0: "no_safety_car",
        1: "full_safety_car",
        2: "virtual_safety_car",
        3: "formation_lap",
    },
    "marshal_flag": {-1: "unknown", 0: "none", 1: "green", 2: "blue", 3: "yellow"},
    "temperature_change": {0: "up", 1: "down", 2: "no_change"},
    "forecast_accuracy": {0: "perfect", 1: "approximate"},
    "steering_assist": {0: "off", 1: "on"},
    "braking_assist": {0: "off", 1: "low", 2: "medium", 3: "high"},
    "gearbox_assist": {1: "manual", 2: "manual_suggested_gear", 3: "automatic"},
    "pit_stop_experience": {0: "automatic", 1: "broadcast", 2: "immersive"},
    "dynamic_racing_line": {0: "off", 1: "corners_only", 2: "full"},
    "dynamic_racing_line_type": {0: "2d", 1: "3d"},
    "game_mode": {
        4: "grand_prix",
        5: "time_trial",
        6: "splitscreen",
        7: "online_custom",
        15: "online_weekly_event",
        17: "story_mode",
        27: "my_team_career",
        28: "driver_career",
        29: "career_online",
        30: "challenge_career",
        75: "story_mode_apxgp",
        127: "benchmark",
    },
    "ruleset": {0: "practice_qualifying", 1: "race", 2: "time_trial", 12: "elimination"},
    "session_length": {
        0: "none",
        2: "very_short",
        3: "short",
        4: "medium",
        5: "medium_long",
        6: "long",
        7: "full",
    },
    "speed_units": {0: "mph", 1: "kph"},
    "temperature_units": {0: "celsius", 1: "fahrenheit"},
    "equal_performance": {0: "off", 1: "on"},
    "recovery_mode": {0: "none", 1: "flashbacks", 2: "auto_recovery"},
    "flashback_limit": {0: "low", 1: "medium", 2: "high", 3: "unlimited"},
    "surface_type": {
        0: "tarmac",
        1: "rumble_strip",
        2: "concrete",
        3: "rock",
        4: "gravel",
        5: "mud",
        6: "sand",
        7: "grass",
        8: "water",
        9: "cobblestone",
        10: "metal",
        11: "ridged",
    },
    "low_fuel_mode": {0: "easy", 1: "hard"},
    "race_starts": {0: "manual", 1: "assisted"},
    "tyre_temperature": {0: "surface_only", 1: "surface_and_carcass"},
    "pit_lane_tyre_sim": {0: "on", 1: "off"},
    "car_damage": {0: "off", 1: "reduced", 2: "standard", 3: "simulation"},
    "car_damage_rate": {0: "reduced", 1: "standard", 2: "simulation"},
    "collisions": {0: "off", 1: "player_collision_off", 2: "on"},
    "corner_cutting": {0: "regular", 1: "strict"},
    "safety_car_setting": {0: "off", 1: "reduced", 2: "standard", 3: "increased"},
    "safety_car_experience": {0: "broadcast", 1: "immersive"},
    "formation_lap_experience": {0: "broadcast", 1: "immersive"},
    "red_flags": {0: "off", 1: "reduced", 2: "standard", 3: "increased"},
    "drs_allowed": {0: "not_allowed", 1: "allowed"},
    "network_paused": {0: "not_paused", 1: "paused"},
    "telemetry_setting": {0: "restricted", 1: "public"},
    "network_game": {0: "offline", 1: "online"},
    "fault": {0: "ok", 1: "fault"},
    "traction_control": {0: "off", 1: "medium", 2: "full"},
    "fuel_mix": {0: "lean", 1: "standard", 2: "rich", 3: "max"},
    "ers_deploy_mode": {0: "none", 1: "medium", 2: "hotlap", 3: "overtake"},
    "drs_disabled_reason": {
        0: "wet_track",
        1: "safety_car",
        2: "red_flag",
        3: "minimum_lap_not_reached",
    },
    "retirement_reason": {
        0: "invalid",
        1: "retired",
        2: "finished",
        3: "terminal_damage",
        4: "inactive",
        5: "not_enough_laps",
        6: "black_flagged",
        7: "red_flagged",
        8: "mechanical_failure",
        9: "session_skipped",
        10: "session_simulated",
    },
    "penalty_type": {
        0: "drive_through",
        1: "stop_go",
        2: "grid_penalty",
        3: "penalty_reminder",
        4: "time_penalty",
        5: "warning",
        6: "disqualified",
        7: "removed_from_formation_lap",
        8: "parked_too_long",
        9: "tyre_regulations",
        10: "lap_invalidated",
        11: "lap_and_next_invalidated",
        12: "lap_invalidated_without_reason",
        13: "lap_and_next_invalidated_without_reason",
        14: "lap_and_previous_invalidated",
        15: "lap_and_previous_invalidated_without_reason",
        16: "retired",
        17: "black_flag_timer",
    },
    "infringement_type": {
        value: name
        for value, name in enumerate(
            (
                "blocking_slow_driving",
                "wrong_way_driving",
                "reversing_off_start",
                "big_collision",
                "small_collision",
                "collision_failed_to_return_position",
                "multiple_collisions_failed_to_return_position",
                "corner_cutting_gained_time",
                "corner_cutting_overtake",
                "multiple_corner_cutting_overtakes",
                "crossed_pit_exit_lane",
                "ignoring_blue_flags",
                "ignoring_yellow_flags",
                "ignoring_drive_through",
                "too_many_drive_throughs",
                "drive_through_reminder",
                "drive_through_reminder_this_lap",
                "pit_lane_speeding",
                "parked_too_long",
                "ignoring_tyre_regulations",
                "too_many_penalties",
                "multiple_warnings",
                "approaching_disqualification",
                "tyre_regulations_select_single",
                "tyre_regulations_select_multiple",
                "lap_invalidated_corner_cutting",
                "lap_invalidated_running_wide",
                "wide_gained_time_minor",
                "wide_gained_time_significant",
                "wide_gained_time_extreme",
                "lap_invalidated_wall_riding",
                "lap_invalidated_flashback",
                "lap_invalidated_reset_to_track",
                "blocking_pitlane",
                "jump_start",
                "safety_car_collision",
                "safety_car_illegal_overtake",
                "safety_car_exceeding_pace",
                "virtual_safety_car_exceeding_pace",
                "formation_lap_below_speed",
                "formation_lap_parking",
                "retired_mechanical_failure",
                "retired_terminal_damage",
                "safety_car_too_far_back",
                "black_flag_timer",
                "unserved_stop_go",
                "unserved_drive_through",
                "engine_component_change",
                "gearbox_change",
                "parc_ferme_change",
                "league_grid_penalty",
                "retry_penalty",
                "illegal_time_gain",
                "mandatory_pitstop",
                "attribute_assigned",
            )
        )
    },
    "safety_car_event": {0: "deployed", 1: "returning", 2: "returned", 3: "resume_race"},
    "mp_unsafe_pit_release": {0: "on", 1: "off"},
    "griefing_setting": {0: "disabled", 1: "enabled"},
    "pit_status": {0: "none", 1: "pitting", 2: "in_pit_area"},
    "sector": {0: "sector_1", 1: "sector_2", 2: "sector_3"},
    "driver_status": {
        0: "in_garage",
        1: "flying_lap",
        2: "in_lap",
        3: "out_lap",
        4: "on_track",
    },
    "result_status": {
        0: "invalid",
        1: "inactive",
        2: "active",
        3: "finished",
        4: "did_not_finish",
        5: "disqualified",
        6: "not_classified",
        7: "retired",
    },
    "result_reason": {
        0: "invalid",
        1: "retired",
        2: "finished",
        3: "terminal_damage",
        4: "inactive",
        5: "not_enough_laps",
        6: "black_flagged",
        7: "red_flagged",
        8: "mechanical_failure",
        9: "session_skipped",
        10: "session_simulated",
    },
    "fia_flag": {-1: "unknown", 0: "none", 1: "green", 2: "blue", 3: "yellow"},
    "team_id": {
        0: "mercedes",
        1: "ferrari",
        2: "red_bull",
        3: "williams",
        4: "aston_martin",
        5: "alpine",
        6: "rb",
        7: "haas",
        8: "mclaren",
        9: "sauber",
        41: "f1_generic",
        255: "unknown",
    },
    "driver_id": {
        0: "carlos_sainz",
        2: "daniel_ricciardo",
        3: "fernando_alonso",
        4: "felipe_massa",
        7: "lewis_hamilton",
        9: "max_verstappen",
        10: "nico_hulkenberg",
        11: "kevin_magnussen",
        14: "sergio_perez",
        15: "valtteri_bottas",
        17: "esteban_ocon",
        19: "lance_stroll",
        20: "arron_barnes",
        21: "martin_giles",
        22: "alex_murray",
        23: "lucas_roth",
        24: "igor_correia",
        25: "sophie_levasseur",
        26: "jonas_schiffer",
        27: "alain_forest",
        28: "jay_letourneau",
        29: "esto_saari",
        30: "yasar_atiyeh",
        31: "callisto_calabresi",
        32: "naota_izumi",
        33: "howard_clarke",
        34: "lars_kaufmann",
        35: "marie_laursen",
        36: "flavio_nieves",
        38: "klimek_michalski",
        39: "santiago_moreno",
        40: "benjamin_coppens",
        41: "noah_visser",
        50: "george_russell",
        54: "lando_norris",
        58: "charles_leclerc",
        59: "pierre_gasly",
        62: "alexander_albon",
        70: "rashid_nair",
        71: "jack_tremblay",
        77: "ayrton_senna",
        80: "guanyu_zhou",
        83: "juan_manuel_correa",
        90: "michael_schumacher",
        94: "yuki_tsunoda",
        102: "aidan_jackson",
        109: "jenson_button",
        110: "david_coulthard",
        112: "oscar_piastri",
        113: "liam_lawson",
        116: "richard_verschoor",
        123: "enzo_fittipaldi",
        125: "mark_webber",
        126: "jacques_villeneuve",
        127: "callie_mayer",
        132: "logan_sargeant",
        136: "jack_doohan",
        137: "amaury_cordeel",
        138: "dennis_hauger",
        145: "zane_maloney",
        146: "victor_martins",
        147: "oliver_bearman",
        148: "jak_crawford",
        149: "isack_hadjar",
        152: "roman_stanek",
        153: "kush_maini",
        156: "brendon_leigh",
        157: "david_tonizza",
        158: "jarno_opmeer",
        159: "lucas_blakeley",
        160: "paul_aron",
        161: "gabriel_bortoleto",
        162: "franco_colapinto",
        163: "taylor_barnard",
        164: "joshua_duerksen",
        165: "andrea_kimi_antonelli",
        166: "ritomo_miyata",
        167: "rafael_villagomez",
        168: "zak_o_sullivan",
        169: "pepe_marti",
        170: "sonny_hayes",
        171: "joshua_pearce",
        172: "callum_voisin",
        173: "matias_zagazeta",
        174: "nikola_tsolov",
        175: "tim_tramnitz",
        185: "luca_cortez",
        255: "unknown",
    },
    "nationality": {
        1: "american",
        2: "argentinean",
        3: "australian",
        4: "austrian",
        5: "azerbaijani",
        6: "bahraini",
        7: "belgian",
        8: "bolivian",
        9: "brazilian",
        10: "british",
        11: "bulgarian",
        12: "cameroonian",
        13: "canadian",
        14: "chilean",
        15: "chinese",
        16: "colombian",
        17: "costa_rican",
        18: "croatian",
        19: "cypriot",
        20: "czech",
        21: "danish",
        22: "dutch",
        23: "ecuadorian",
        24: "english",
        25: "emirian",
        26: "estonian",
        27: "finnish",
        28: "french",
        29: "german",
        30: "ghanaian",
        31: "greek",
        32: "guatemalan",
        33: "honduran",
        34: "hong_konger",
        35: "hungarian",
        36: "icelander",
        37: "indian",
        38: "indonesian",
        39: "irish",
        40: "israeli",
        41: "italian",
        42: "jamaican",
        43: "japanese",
        44: "jordanian",
        45: "kuwaiti",
        46: "latvian",
        47: "lebanese",
        48: "lithuanian",
        49: "luxembourger",
        50: "malaysian",
        51: "maltese",
        52: "mexican",
        53: "monegasque",
        54: "new_zealander",
        55: "nicaraguan",
        56: "northern_irish",
        57: "norwegian",
        58: "omani",
        59: "pakistani",
        60: "panamanian",
        61: "paraguayan",
        62: "peruvian",
        63: "polish",
        64: "portuguese",
        65: "qatari",
        66: "romanian",
        68: "salvadoran",
        69: "saudi",
        70: "scottish",
        71: "serbian",
        72: "singaporean",
        73: "slovakian",
        74: "slovenian",
        75: "south_korean",
        76: "south_african",
        77: "spanish",
        78: "swedish",
        79: "swiss",
        80: "thai",
        81: "turkish",
        82: "uruguayan",
        83: "ukrainian",
        84: "venezuelan",
        85: "barbadian",
        86: "welsh",
        87: "vietnamese",
        88: "algerian",
        89: "bosnian",
        90: "filipino",
        255: "network_player",
    },
    "platform": {1: "steam", 3: "playstation", 4: "xbox", 6: "origin", 255: "unknown"},
    "tyre_compound": {
        7: "intermediate",
        8: "wet",
        16: "c5",
        17: "c4",
        18: "c3",
        19: "c2",
        20: "c1",
        21: "c0",
        22: "c6",
    },
    "visual_tyre_compound": {
        7: "intermediate",
        8: "wet",
        16: "soft",
        17: "medium",
        18: "hard",
    },
    "event_code": {
        code: name
        for code, name in (
            ("SSTA", "session_started"),
            ("SEND", "session_ended"),
            ("FTLP", "fastest_lap"),
            ("RTMT", "retirement"),
            ("DRSE", "drs_enabled"),
            ("DRSD", "drs_disabled"),
            ("TMPT", "teammate_in_pits"),
            ("CHQF", "chequered_flag"),
            ("RCWN", "race_winner"),
            ("PENA", "penalty"),
            ("SPTP", "speed_trap"),
            ("STLG", "start_lights"),
            ("LGOT", "lights_out"),
            ("DTSV", "drive_through_served"),
            ("SGSV", "stop_go_served"),
            ("FLBK", "flashback"),
            ("BUTN", "buttons"),
            ("RDFL", "red_flag"),
            ("OVTK", "overtake"),
            ("SCAR", "safety_car"),
            ("COLL", "collision"),
        )
    },
}


@dataclass(frozen=True, slots=True)
class MarshalZone:
    start: float
    flag: EnumValue


@dataclass(frozen=True, slots=True)
class WeatherForecastSample:
    session_type: EnumValue
    time_offset_minutes: int
    weather: EnumValue
    track_temperature: int
    track_temperature_change: EnumValue
    air_temperature: int
    air_temperature_change: EnumValue
    rain_percentage: int


@dataclass(frozen=True, slots=True)
class SessionAssistsAndRules:
    steering_assist: EnumValue
    braking_assist: EnumValue
    gearbox_assist: EnumValue
    pit_assist: BooleanValue
    pit_release_assist: BooleanValue
    ers_assist: BooleanValue
    drs_assist: BooleanValue
    dynamic_racing_line: EnumValue
    dynamic_racing_line_type: EnumValue
    game_mode: EnumValue
    ruleset: EnumValue


@dataclass(frozen=True, slots=True)
class SessionSettings:
    session_length: EnumValue
    speed_units_lead_player: EnumValue
    temperature_units_lead_player: EnumValue
    speed_units_secondary_player: EnumValue
    temperature_units_secondary_player: EnumValue
    safety_car_periods: int
    virtual_safety_car_periods: int
    red_flag_periods: int
    equal_car_performance: EnumValue
    recovery_mode: EnumValue
    flashback_limit: EnumValue
    surface_type: EnumValue
    low_fuel_mode: EnumValue
    race_starts: EnumValue
    tyre_temperature: EnumValue
    pit_lane_tyre_simulation: EnumValue
    car_damage: EnumValue
    car_damage_rate: EnumValue
    collisions: EnumValue
    collisions_off_first_lap_only: BooleanValue
    multiplayer_unsafe_pit_release: EnumValue
    multiplayer_griefing: EnumValue
    corner_cutting_stringency: EnumValue
    parc_ferme_rules: BooleanValue
    pit_stop_experience: EnumValue
    safety_car: EnumValue
    safety_car_experience: EnumValue
    formation_lap: BooleanValue
    formation_lap_experience: EnumValue
    red_flags: EnumValue
    licence_level_solo: BooleanValue
    licence_level_multiplayer: BooleanValue
    num_sessions_in_weekend: int


@dataclass(frozen=True, slots=True)
class SessionPacket:
    header: PacketHeader
    weather: EnumValue
    track_temperature: int
    air_temperature: int
    total_laps: int
    track_length: int
    session_type: EnumValue
    track_id: EnumValue
    formula: EnumValue
    session_time_left: int
    session_duration: int
    pit_speed_limit: int
    game_paused: BooleanValue
    is_spectating: BooleanValue
    spectator_car_index: int
    sli_pro_native_support: BooleanValue
    marshal_zones: tuple[MarshalZone, ...]
    safety_car_status: EnumValue
    network_game: EnumValue
    weather_forecast: tuple[WeatherForecastSample, ...]
    forecast_accuracy: EnumValue
    ai_difficulty: int
    season_link_identifier: int
    weekend_link_identifier: int
    session_link_identifier: int
    pit_stop_window_ideal_lap: int
    pit_stop_window_latest_lap: int
    pit_stop_rejoin_position: int
    assists_and_rules: SessionAssistsAndRules
    time_of_day: int
    session_settings: SessionSettings
    weekend_structure: tuple[EnumValue, ...]
    sector2_lap_distance_start: float
    sector3_lap_distance_start: float


@dataclass(frozen=True, slots=True)
class LapCarData:
    last_lap_time_ms: int
    current_lap_time_ms: int
    sector1_time_ms: int
    sector2_time_ms: int
    delta_to_car_in_front_ms: int
    delta_to_race_leader_ms: int
    lap_distance: float
    total_distance: float
    safety_car_delta: float
    car_position: int
    current_lap_number: int
    pit_status: EnumValue
    pit_stops: int
    sector: EnumValue
    current_lap_invalid: BooleanValue
    penalties: int
    total_warnings: int
    corner_cutting_warnings: int
    unserved_drive_through_penalties: int
    unserved_stop_go_penalties: int
    grid_position_zero_based: int
    driver_status: EnumValue
    result_status: EnumValue
    pit_lane_timer_active: BooleanValue
    pit_lane_time_ms: int
    pit_stop_timer_ms: int
    pit_stop_should_serve_penalty: BooleanValue
    speed_trap_fastest_speed: float
    speed_trap_fastest_lap: int


@dataclass(frozen=True, slots=True)
class LapDataPacket:
    header: PacketHeader
    cars: tuple[LapCarData, ...]
    time_trial_personal_best_car_index: int
    time_trial_rival_car_index: int


@dataclass(frozen=True, slots=True)
class Participant:
    ai_controlled: BooleanValue
    driver_id: EnumValue
    network_id: int
    team_id: EnumValue
    my_team: BooleanValue
    race_number: int
    nationality: EnumValue
    name: str
    your_telemetry: EnumValue
    show_online_names: BooleanValue
    tech_level: int
    platform: EnumValue
    num_livery_colours: int
    livery_colours: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True, slots=True)
class ParticipantsPacket:
    header: PacketHeader
    active_cars: int
    participants: tuple[Participant | None, ...]


@dataclass(frozen=True, slots=True)
class CarStatus:
    traction_control: EnumValue
    anti_lock_brakes: BooleanValue
    fuel_mix: EnumValue
    front_brake_bias: int
    pit_limiter_status: BooleanValue
    fuel_in_tank: float
    fuel_capacity: float
    fuel_remaining_laps: float
    max_rpm: int
    idle_rpm: int
    max_gears: int
    drs_allowed: EnumValue
    drs_activation_distance: int
    actual_tyre_compound: EnumValue
    visual_tyre_compound: EnumValue
    tyres_age_laps: int
    vehicle_fia_flags: EnumValue
    engine_power_ice: float
    engine_power_mguk: float
    ers_store_energy: float
    ers_deploy_mode: EnumValue
    ers_harvested_this_lap_mguk: float
    ers_harvested_this_lap_mguh: float
    ers_deployed_this_lap: float
    network_paused: EnumValue


@dataclass(frozen=True, slots=True)
class CarStatusPacket:
    header: PacketHeader
    cars: tuple[CarStatus, ...]


@dataclass(frozen=True, slots=True)
class FinalClassification:
    position: int
    laps: int
    grid_position: int
    points: int
    pit_stops: int
    result_status: EnumValue
    result_reason: EnumValue
    best_lap_time_ms: int
    total_race_time: float
    penalties_time: int
    penalties: int
    num_tyre_stints: int
    tyre_stints_actual: tuple[EnumValue, ...]
    tyre_stints_visual: tuple[EnumValue, ...]
    tyre_stints_end_laps: tuple[int | None, ...]


@dataclass(frozen=True, slots=True)
class FinalClassificationPacket:
    header: PacketHeader
    num_cars: int
    classification: tuple[FinalClassification | None, ...]


@dataclass(frozen=True, slots=True)
class CarDamage:
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
    drs_fault: EnumValue
    ers_fault: EnumValue
    gearbox_damage: int
    engine_damage: int
    engine_mguh_wear: int
    engine_es_wear: int
    engine_ce_wear: int
    engine_ice_wear: int
    engine_mguk_wear: int
    engine_tc_wear: int
    engine_blown: EnumValue
    engine_seized: EnumValue


@dataclass(frozen=True, slots=True)
class CarDamagePacket:
    header: PacketHeader
    cars: tuple[CarDamage, ...]


@dataclass(frozen=True, slots=True)
class LapHistory:
    lap_time_ms: int
    sector1_time_ms: int
    sector2_time_ms: int
    sector3_time_ms: int
    valid_bit_flags: int


@dataclass(frozen=True, slots=True)
class TyreStintHistory:
    actual_compound: EnumValue
    visual_compound: EnumValue
    end_lap: int | None


@dataclass(frozen=True, slots=True)
class SessionHistoryPacket:
    header: PacketHeader
    car_index: int
    num_laps: int
    num_tyre_stints: int
    best_lap_time_lap_number: int
    best_sector1_lap_number: int
    best_sector2_lap_number: int
    best_sector3_lap_number: int
    lap_history: tuple[LapHistory, ...]
    tyre_stint_history: tuple[TyreStintHistory, ...]


@dataclass(frozen=True, slots=True)
class TyreSet:
    actual_tyre_compound: EnumValue
    visual_tyre_compound: EnumValue
    wear: int
    available: BooleanValue
    recommended_session: EnumValue
    lifespan: int
    usable_life: int
    lap_delta_time_ms: int
    fitted: BooleanValue


@dataclass(frozen=True, slots=True)
class TyreSetsPacket:
    header: PacketHeader
    car_index: int
    tyre_sets: tuple[TyreSet, ...]
    fitted_index: int


@dataclass(frozen=True, slots=True)
class EventDetail:
    values: tuple[int | float | EnumValue, ...] = ()


@dataclass(frozen=True, slots=True)
class NoEventDetail:
    pass


@dataclass(frozen=True, slots=True)
class PenaltyEventDetail:
    penalty_type: EnumValue
    infringement_type: EnumValue
    vehicle_index: int
    other_vehicle_index: int
    time_seconds: int
    lap_number: int
    places_gained: int


@dataclass(frozen=True, slots=True)
class SpeedTrapDetail:
    vehicle_index: int
    speed: float
    overall_fastest: int
    driver_fastest: int
    fastest_vehicle_index: int
    fastest_speed: float


@dataclass(frozen=True, slots=True)
class FlashbackDetail:
    frame_identifier: int
    session_time: float


@dataclass(frozen=True, slots=True)
class UnknownEventDetail:
    raw: bytes


@dataclass(frozen=True, slots=True)
class EventPacket:
    header: PacketHeader
    event_code: EnumValue
    detail: (
        EventDetail
        | NoEventDetail
        | PenaltyEventDetail
        | SpeedTrapDetail
        | FlashbackDetail
        | UnknownEventDetail
    )


type ParsedPacket = (
    SessionPacket
    | LapDataPacket
    | EventPacket
    | ParticipantsPacket
    | CarStatusPacket
    | FinalClassificationPacket
    | CarDamagePacket
    | SessionHistoryPacket
    | TyreSetsPacket
)


_SESSION_PREFIX = struct.Struct("<BbbBHBbBHHBBBBBB")
_SESSION_MIDDLE = struct.Struct("<BBB")
_MARSHAL_ZONE = struct.Struct("<fb")
_FORECAST = struct.Struct("<BBBbbbbB")
_SESSION_TAIL = struct.Struct("<BBIII14BI33B12Bff")
_LAP_CAR = struct.Struct("<IIHBHBHBHBfff15BHHBfB")
_PARTICIPANT = struct.Struct("<7B32sBBHBB12s")
_LIVERY_COLOUR = struct.Struct("<BBB")
_CAR_STATUS = struct.Struct("<5B3f2H2BH3Bb3fB3fB")
_CLASSIFICATION = struct.Struct("<7BId3B8B8B8B")
_CAR_DAMAGE = struct.Struct("<4f30B")
_SESSION_HISTORY_HEADER = struct.Struct("<7B")
_LAP_HISTORY = struct.Struct("<IHBHBHBB")
_TYRE_STINT_HISTORY = struct.Struct("<3B")
_TYRE_SET = struct.Struct("<7BhB")
_EVENT = struct.Struct("<4s12s")
_TIME_TRIAL_CAR_INDICES = struct.Struct("<BB")


def _require_size(data: bytes, expected: int, header: PacketHeader) -> None:
    if len(data) != expected:
        raise PacketSizeError(
            "packet_size_mismatch",
            packet=packet_name(header.packet_id),
            packet_id=header.packet_id,
            actual=len(data),
            expected=expected,
        )


def _validate_finite_values(value: object) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PacketParseError("non_finite_float")
    elif is_dataclass(value):
        for field_info in fields(value):
            _validate_finite_values(getattr(value, field_info.name))
    elif isinstance(value, tuple):
        for item in value:
            _validate_finite_values(item)


def _validate_percentage(field_name: str, value: int | float) -> None:
    if not 0 <= value <= 100:
        raise PacketParseError(
            "percentage_out_of_range",
            field=field_name,
            value=value,
        )


def _parse_session(header: PacketHeader, data: bytes) -> SessionPacket:
    _require_size(data, SUPPORTED_PACKET_SIZES[1], header)
    offset = HEADER_SIZE
    prefix = _SESSION_PREFIX.unpack_from(data, offset)
    offset += _SESSION_PREFIX.size
    zones_all = tuple(
        MarshalZone(start, _enum("marshal_flag", flag))
        for start, flag in _MARSHAL_ZONE.iter_unpack(data[offset : offset + 21 * 5])
    )
    offset += 21 * _MARSHAL_ZONE.size
    safety_car, network_game, forecast_count = _SESSION_MIDDLE.unpack_from(data, offset)
    if prefix[15] > 21 or forecast_count > 64:
        raise PacketParseError("session_count_exceeded")
    zones = zones_all[: prefix[15]]
    offset += _SESSION_MIDDLE.size
    forecasts_raw = tuple(_FORECAST.iter_unpack(data[offset : offset + 64 * 8]))
    forecasts = tuple(
        WeatherForecastSample(
            _enum("session_type", row[0]),
            row[1],
            _enum("weather", row[2]),
            row[3],
            _enum("temperature_change", row[4]),
            row[5],
            _enum("temperature_change", row[6]),
            row[7],
        )
        for row in forecasts_raw[:forecast_count]
    )
    for row in forecasts_raw[:forecast_count]:
        _validate_percentage("weather_forecast.rain_percentage", int(row[7]))
    offset += 64 * _FORECAST.size
    tail = _SESSION_TAIL.unpack_from(data, offset)
    if offset + _SESSION_TAIL.size != len(data):
        raise PacketSizeError("session_layout_size_mismatch")
    settings_raw = tail[20:53]
    num_sessions = settings_raw[32]
    if num_sessions > 12:
        raise PacketParseError("session_weekend_count_exceeded")
    settings = SessionSettings(
        session_length=_enum("session_length", settings_raw[0]),
        speed_units_lead_player=_enum("speed_units", settings_raw[1]),
        temperature_units_lead_player=_enum("temperature_units", settings_raw[2]),
        speed_units_secondary_player=_enum("speed_units", settings_raw[3]),
        temperature_units_secondary_player=_enum("temperature_units", settings_raw[4]),
        safety_car_periods=settings_raw[5],
        virtual_safety_car_periods=settings_raw[6],
        red_flag_periods=settings_raw[7],
        equal_car_performance=_enum("equal_performance", settings_raw[8]),
        recovery_mode=_enum("recovery_mode", settings_raw[9]),
        flashback_limit=_enum("flashback_limit", settings_raw[10]),
        surface_type=_enum("surface_type", settings_raw[11]),
        low_fuel_mode=_enum("low_fuel_mode", settings_raw[12]),
        race_starts=_enum("race_starts", settings_raw[13]),
        tyre_temperature=_enum("tyre_temperature", settings_raw[14]),
        pit_lane_tyre_simulation=_enum("pit_lane_tyre_sim", settings_raw[15]),
        car_damage=_enum("car_damage", settings_raw[16]),
        car_damage_rate=_enum("car_damage_rate", settings_raw[17]),
        collisions=_enum("collisions", settings_raw[18]),
        collisions_off_first_lap_only=_boolean(settings_raw[19]),
        multiplayer_unsafe_pit_release=_enum("mp_unsafe_pit_release", settings_raw[20]),
        multiplayer_griefing=_enum("griefing_setting", settings_raw[21]),
        corner_cutting_stringency=_enum("corner_cutting", settings_raw[22]),
        parc_ferme_rules=_boolean(settings_raw[23]),
        pit_stop_experience=_enum("pit_stop_experience", settings_raw[24]),
        safety_car=_enum("safety_car_setting", settings_raw[25]),
        safety_car_experience=_enum("safety_car_experience", settings_raw[26]),
        formation_lap=_boolean(settings_raw[27]),
        formation_lap_experience=_enum("formation_lap_experience", settings_raw[28]),
        red_flags=_enum("red_flags", settings_raw[29]),
        licence_level_solo=_boolean(settings_raw[30]),
        licence_level_multiplayer=_boolean(settings_raw[31]),
        num_sessions_in_weekend=num_sessions,
    )

    return SessionPacket(
        header=header,
        weather=_enum("weather", prefix[0]),
        track_temperature=prefix[1],
        air_temperature=prefix[2],
        total_laps=prefix[3],
        track_length=prefix[4],
        session_type=_enum("session_type", prefix[5]),
        track_id=_enum("track_id", prefix[6]),
        formula=_enum("formula", prefix[7]),
        session_time_left=prefix[8],
        session_duration=prefix[9],
        pit_speed_limit=prefix[10],
        game_paused=_boolean(prefix[11]),
        is_spectating=_boolean(prefix[12]),
        spectator_car_index=prefix[13],
        sli_pro_native_support=_boolean(prefix[14]),
        marshal_zones=zones,
        safety_car_status=_enum("safety_car", safety_car),
        network_game=_enum("network_game", network_game),
        weather_forecast=forecasts,
        forecast_accuracy=_enum("forecast_accuracy", tail[0]),
        ai_difficulty=tail[1],
        season_link_identifier=tail[2],
        weekend_link_identifier=tail[3],
        session_link_identifier=tail[4],
        pit_stop_window_ideal_lap=tail[5],
        pit_stop_window_latest_lap=tail[6],
        pit_stop_rejoin_position=tail[7],
        assists_and_rules=SessionAssistsAndRules(
            steering_assist=_enum("steering_assist", tail[8]),
            braking_assist=_enum("braking_assist", tail[9]),
            gearbox_assist=_enum("gearbox_assist", tail[10]),
            pit_assist=_boolean(tail[11]),
            pit_release_assist=_boolean(tail[12]),
            ers_assist=_boolean(tail[13]),
            drs_assist=_boolean(tail[14]),
            dynamic_racing_line=_enum("dynamic_racing_line", tail[15]),
            dynamic_racing_line_type=_enum("dynamic_racing_line_type", tail[16]),
            game_mode=_enum("game_mode", tail[17]),
            ruleset=_enum("ruleset", tail[18]),
        ),
        time_of_day=tail[19],
        session_settings=settings,
        weekend_structure=tuple(
            _enum("session_type", value) for value in tail[53 : 53 + num_sessions]
        ),
        sector2_lap_distance_start=tail[65],
        sector3_lap_distance_start=tail[66],
    )


def _parse_lap_data(header: PacketHeader, data: bytes) -> LapDataPacket:
    _require_size(data, SUPPORTED_PACKET_SIZES[2], header)
    cars = tuple(
        _lap_car(row)
        for row in _LAP_CAR.iter_unpack(data[HEADER_SIZE : HEADER_SIZE + MAX_CARS * _LAP_CAR.size])
    )
    personal_best, rival = _TIME_TRIAL_CAR_INDICES.unpack_from(data, len(data) - 2)
    return LapDataPacket(header, cars, personal_best, rival)


def _lap_car(row: tuple[int | float, ...]) -> LapCarData:
    return LapCarData(
        last_lap_time_ms=int(row[0]),
        current_lap_time_ms=int(row[1]),
        sector1_time_ms=int(row[3]) * 60_000 + int(row[2]),
        sector2_time_ms=int(row[5]) * 60_000 + int(row[4]),
        delta_to_car_in_front_ms=int(row[7]) * 60_000 + int(row[6]),
        delta_to_race_leader_ms=int(row[9]) * 60_000 + int(row[8]),
        lap_distance=float(row[10]),
        total_distance=float(row[11]),
        safety_car_delta=float(row[12]),
        car_position=int(row[13]),
        current_lap_number=int(row[14]),
        pit_status=_enum("pit_status", int(row[15])),
        pit_stops=int(row[16]),
        sector=_enum("sector", int(row[17])),
        current_lap_invalid=_boolean(int(row[18])),
        penalties=int(row[19]),
        total_warnings=int(row[20]),
        corner_cutting_warnings=int(row[21]),
        unserved_drive_through_penalties=int(row[22]),
        unserved_stop_go_penalties=int(row[23]),
        grid_position_zero_based=int(row[24]),
        driver_status=_enum("driver_status", int(row[25])),
        result_status=_enum("result_status", int(row[26])),
        pit_lane_timer_active=_boolean(int(row[27])),
        pit_lane_time_ms=int(row[28]),
        pit_stop_timer_ms=int(row[29]),
        pit_stop_should_serve_penalty=_boolean(int(row[30])),
        speed_trap_fastest_speed=float(row[31]),
        speed_trap_fastest_lap=int(row[32]),
    )


def _decode_name(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def _parse_participants(header: PacketHeader, data: bytes) -> ParticipantsPacket:
    _require_size(data, SUPPORTED_PACKET_SIZES[4], header)
    active_cars = data[HEADER_SIZE]
    if active_cars > MAX_CARS:
        raise PacketParseError("participants_count_exceeded")
    records = _PARTICIPANT.iter_unpack(data[HEADER_SIZE + 1 :])
    participants = tuple(
        Participant(
            ai_controlled=_boolean(row[0]),
            driver_id=_enum("driver_id", row[1]),
            network_id=row[2],
            team_id=_enum("team_id", row[3]),
            my_team=_boolean(row[4]),
            race_number=row[5],
            nationality=_enum("nationality", row[6]),
            name=_decode_name(row[7]),
            your_telemetry=_enum("telemetry_setting", row[8]),
            show_online_names=_boolean(row[9]),
            tech_level=row[10],
            platform=_enum("platform", row[11]),
            num_livery_colours=row[12],
            livery_colours=tuple(
                _LIVERY_COLOUR.unpack_from(row[13], index)
                for index in range(0, 12, _LIVERY_COLOUR.size)
            ),
        )
        if any(any(value) if isinstance(value, bytes) else value != 0 for value in row)
        else None
        for row in records
    )
    return ParticipantsPacket(header, active_cars, participants)


def _parse_car_status(header: PacketHeader, data: bytes) -> CarStatusPacket:
    _require_size(data, SUPPORTED_PACKET_SIZES[7], header)
    return CarStatusPacket(
        header,
        tuple(_car_status(row) for row in _CAR_STATUS.iter_unpack(data[HEADER_SIZE:])),
    )


def _car_status(row: tuple[int | float, ...]) -> CarStatus:
    _validate_percentage("front_brake_bias", int(row[3]))
    return CarStatus(
        traction_control=_enum("traction_control", int(row[0])),
        anti_lock_brakes=_boolean(int(row[1])),
        fuel_mix=_enum("fuel_mix", int(row[2])),
        front_brake_bias=int(row[3]),
        pit_limiter_status=_boolean(int(row[4])),
        fuel_in_tank=float(row[5]),
        fuel_capacity=float(row[6]),
        fuel_remaining_laps=float(row[7]),
        max_rpm=int(row[8]),
        idle_rpm=int(row[9]),
        max_gears=int(row[10]),
        drs_allowed=_enum("drs_allowed", int(row[11])),
        drs_activation_distance=int(row[12]),
        actual_tyre_compound=_enum("tyre_compound", int(row[13])),
        visual_tyre_compound=_enum("visual_tyre_compound", int(row[14])),
        tyres_age_laps=int(row[15]),
        vehicle_fia_flags=_enum("fia_flag", int(row[16])),
        engine_power_ice=float(row[17]),
        engine_power_mguk=float(row[18]),
        ers_store_energy=float(row[19]),
        ers_deploy_mode=_enum("ers_deploy_mode", int(row[20])),
        ers_harvested_this_lap_mguk=float(row[21]),
        ers_harvested_this_lap_mguh=float(row[22]),
        ers_deployed_this_lap=float(row[23]),
        network_paused=_enum("network_paused", int(row[24])),
    )


def _parse_classification(header: PacketHeader, data: bytes) -> FinalClassificationPacket:
    _require_size(data, SUPPORTED_PACKET_SIZES[8], header)
    num_cars = data[HEADER_SIZE]
    if num_cars > MAX_CARS:
        raise PacketParseError("classification_count_exceeded")
    entries = tuple(
        _classification(row) if any(row) else None for row in _CLASSIFICATION.iter_unpack(data[30:])
    )
    return FinalClassificationPacket(header, num_cars, entries)


def _classification(row: tuple[int | float, ...]) -> FinalClassification:
    num_tyre_stints = int(row[11])
    if num_tyre_stints > 8:
        raise PacketParseError("classification_stints_exceeded")
    return FinalClassification(
        position=int(row[0]),
        laps=int(row[1]),
        grid_position=int(row[2]),
        points=int(row[3]),
        pit_stops=int(row[4]),
        result_status=_enum("result_status", int(row[5])),
        result_reason=_enum("result_reason", int(row[6])),
        best_lap_time_ms=int(row[7]),
        total_race_time=float(row[8]),
        penalties_time=int(row[9]),
        penalties=int(row[10]),
        num_tyre_stints=num_tyre_stints,
        tyre_stints_actual=tuple(
            _enum("tyre_compound", int(value)) for value in row[12 : 12 + num_tyre_stints]
        ),
        tyre_stints_visual=tuple(
            _enum("visual_tyre_compound", int(value)) for value in row[20 : 20 + num_tyre_stints]
        ),
        tyre_stints_end_laps=tuple(
            None if int(value) == 255 else int(value) for value in row[28 : 28 + num_tyre_stints]
        ),
    )


def _parse_car_damage(header: PacketHeader, data: bytes) -> CarDamagePacket:
    _require_size(data, SUPPORTED_PACKET_SIZES[10], header)
    return CarDamagePacket(
        header,
        tuple(_car_damage(row) for row in _CAR_DAMAGE.iter_unpack(data[HEADER_SIZE:])),
    )


def _car_damage(row: tuple[int | float, ...]) -> CarDamage:
    values = tuple(int(value) for value in row[4:])
    percentage_values = (
        ("tyres_wear", *(float(value) for value in row[:4])),
        ("tyres_damage", *values[:4]),
        ("brakes_damage", *values[4:8]),
        ("tyre_blisters", *values[8:12]),
        (
            "car_damage",
            *values[12:18],
        ),
        ("gearbox_damage", values[20]),
        ("engine_damage", values[21]),
        ("engine_wear", *values[22:28]),
    )
    for field_name, *percentages in percentage_values:
        for value in percentages:
            _validate_percentage(field_name, value)
    return CarDamage(
        tyres_wear=tuple(float(value) for value in row[:4]),
        tyres_damage=values[:4],
        brakes_damage=values[4:8],
        tyre_blisters=values[8:12],
        front_left_wing_damage=values[12],
        front_right_wing_damage=values[13],
        rear_wing_damage=values[14],
        floor_damage=values[15],
        diffuser_damage=values[16],
        sidepod_damage=values[17],
        drs_fault=_enum("fault", values[18]),
        ers_fault=_enum("fault", values[19]),
        gearbox_damage=values[20],
        engine_damage=values[21],
        engine_mguh_wear=values[22],
        engine_es_wear=values[23],
        engine_ce_wear=values[24],
        engine_ice_wear=values[25],
        engine_mguk_wear=values[26],
        engine_tc_wear=values[27],
        engine_blown=_enum("fault", values[28]),
        engine_seized=_enum("fault", values[29]),
    )


def _parse_session_history(header: PacketHeader, data: bytes) -> SessionHistoryPacket:
    _require_size(data, SUPPORTED_PACKET_SIZES[11], header)
    history_header = _SESSION_HISTORY_HEADER.unpack_from(data, HEADER_SIZE)
    car_index, num_laps, num_tyre_stints, best_lap, best_sector1, best_sector2, best_sector3 = (
        int(value) for value in history_header
    )
    if num_laps > 100 or num_tyre_stints > 8:
        raise PacketParseError("session_history_count_exceeded")
    offset = HEADER_SIZE + _SESSION_HISTORY_HEADER.size
    laps = tuple(
        _lap_history(_LAP_HISTORY.unpack_from(data, offset + index * _LAP_HISTORY.size))
        for index in range(num_laps)
    )
    offset += 100 * _LAP_HISTORY.size
    stints = tuple(
        TyreStintHistory(
            _enum("tyre_compound", int(actual)),
            _enum("visual_tyre_compound", int(visual)),
            None if int(end_lap) == 255 else int(end_lap),
        )
        for end_lap, actual, visual in tuple(_TYRE_STINT_HISTORY.iter_unpack(data[offset:]))[
            :num_tyre_stints
        ]
    )
    return SessionHistoryPacket(
        header=header,
        car_index=car_index,
        num_laps=num_laps,
        num_tyre_stints=num_tyre_stints,
        best_lap_time_lap_number=best_lap,
        best_sector1_lap_number=best_sector1,
        best_sector2_lap_number=best_sector2,
        best_sector3_lap_number=best_sector3,
        lap_history=laps,
        tyre_stint_history=stints,
    )


def _lap_history(row: tuple[int | float, ...]) -> LapHistory:
    return LapHistory(
        int(row[0]),
        int(row[2]) * 60_000 + int(row[1]),
        int(row[4]) * 60_000 + int(row[3]),
        int(row[6]) * 60_000 + int(row[5]),
        int(row[7]),
    )


def _parse_tyre_sets(header: PacketHeader, data: bytes) -> TyreSetsPacket:
    _require_size(data, SUPPORTED_PACKET_SIZES[12], header)
    car_index = data[HEADER_SIZE]
    offset = HEADER_SIZE + 1
    tyre_sets = tuple(
        TyreSet(
            _enum("tyre_compound", int(row[0])),
            _enum("visual_tyre_compound", int(row[1])),
            int(row[2]),
            _boolean(int(row[3])),
            _enum("session_type", int(row[4])),
            int(row[5]),
            int(row[6]),
            int(row[7]),
            _boolean(int(row[8])),
        )
        for row in _TYRE_SET.iter_unpack(data[offset : offset + 20 * _TYRE_SET.size])
    )
    for tyre_set in tyre_sets:
        _validate_percentage("tyre_set.wear", tyre_set.wear)
    fitted_index = data[-1]
    return TyreSetsPacket(header, car_index, tyre_sets, fitted_index)


_EVENT_DETAIL_STRUCTS: dict[str, struct.Struct] = {
    "FTLP": struct.Struct("<Bf"),
    "RTMT": struct.Struct("<BB"),
    "DRSD": struct.Struct("<B"),
    "TMPT": struct.Struct("<B"),
    "RCWN": struct.Struct("<B"),
    "PENA": struct.Struct("<7B"),
    "SPTP": struct.Struct("<BfBBBf"),
    "STLG": struct.Struct("<B"),
    "DTSV": struct.Struct("<B"),
    "SGSV": struct.Struct("<Bf"),
    "FLBK": struct.Struct("<If"),
    "BUTN": struct.Struct("<I"),
    "OVTK": struct.Struct("<BB"),
    "SCAR": struct.Struct("<BB"),
    "COLL": struct.Struct("<BB"),
}


def _parse_event(header: PacketHeader, data: bytes) -> EventPacket:
    _require_size(data, SUPPORTED_PACKET_SIZES[3], header)
    code_raw, details_raw = _EVENT.unpack_from(data, HEADER_SIZE)
    code = code_raw.decode("ascii", errors="replace")
    known_codes = _ENUMS["event_code"]
    event_code = EnumValue(code, known_codes.get(code, "unknown"))
    detail_struct = _EVENT_DETAIL_STRUCTS.get(code)
    no_detail_codes = {"SSTA", "SEND", "DRSE", "CHQF", "LGOT", "RDFL"}
    if code in no_detail_codes:
        detail: (
            EventDetail
            | NoEventDetail
            | PenaltyEventDetail
            | SpeedTrapDetail
            | FlashbackDetail
            | UnknownEventDetail
        ) = NoEventDetail()
    elif detail_struct is None:
        detail = UnknownEventDetail(details_raw)
    else:
        values = detail_struct.unpack_from(details_raw)
        if code == "SPTP":
            detail = SpeedTrapDetail(
                int(values[0]),
                float(values[1]),
                int(values[2]),
                int(values[3]),
                int(values[4]),
                float(values[5]),
            )
        elif code == "FLBK":
            detail = FlashbackDetail(int(values[0]), float(values[1]))
        elif code == "RTMT":
            detail = EventDetail((int(values[0]), _enum("retirement_reason", int(values[1]))))
        elif code == "DRSD":
            detail = EventDetail((_enum("drs_disabled_reason", int(values[0])),))
        elif code == "PENA":
            detail = PenaltyEventDetail(
                penalty_type=_enum("penalty_type", int(values[0])),
                infringement_type=_enum("infringement_type", int(values[1])),
                vehicle_index=int(values[2]),
                other_vehicle_index=int(values[3]),
                time_seconds=int(values[4]),
                lap_number=int(values[5]),
                places_gained=int(values[6]),
            )
        elif code == "SCAR":
            detail = EventDetail(
                (
                    _enum("safety_car", int(values[0])),
                    _enum("safety_car_event", int(values[1])),
                )
            )
        elif code in no_detail_codes:
            detail = NoEventDetail()
        else:
            detail = EventDetail(tuple(values))
    return EventPacket(header, event_code, detail)


@dataclass(frozen=True, slots=True)
class PacketDecoder:
    size: int
    decode: Callable[[PacketHeader, bytes], ParsedPacket]


PACKET_DECODERS: Mapping[int, PacketDecoder] = MappingProxyType(
    {
        1: PacketDecoder(
            HEADER_SIZE
            + _SESSION_PREFIX.size
            + 21 * _MARSHAL_ZONE.size
            + _SESSION_MIDDLE.size
            + 64 * _FORECAST.size
            + _SESSION_TAIL.size,
            _parse_session,
        ),
        2: PacketDecoder(
            HEADER_SIZE + MAX_CARS * _LAP_CAR.size + _TIME_TRIAL_CAR_INDICES.size,
            _parse_lap_data,
        ),
        3: PacketDecoder(HEADER_SIZE + _EVENT.size, _parse_event),
        4: PacketDecoder(HEADER_SIZE + 1 + MAX_CARS * _PARTICIPANT.size, _parse_participants),
        7: PacketDecoder(HEADER_SIZE + MAX_CARS * _CAR_STATUS.size, _parse_car_status),
        8: PacketDecoder(
            HEADER_SIZE + 1 + MAX_CARS * _CLASSIFICATION.size,
            _parse_classification,
        ),
        10: PacketDecoder(HEADER_SIZE + MAX_CARS * _CAR_DAMAGE.size, _parse_car_damage),
        11: PacketDecoder(
            HEADER_SIZE
            + _SESSION_HISTORY_HEADER.size
            + 100 * _LAP_HISTORY.size
            + 8 * _TYRE_STINT_HISTORY.size,
            _parse_session_history,
        ),
        12: PacketDecoder(HEADER_SIZE + 2 + 20 * _TYRE_SET.size, _parse_tyre_sets),
    }
)
SUPPORTED_PACKET_SIZES: Mapping[int, int] = MappingProxyType(
    {packet_id: decoder.size for packet_id, decoder in PACKET_DECODERS.items()}
)


def parse_packet(data: bytes) -> ParsedPacket:
    """Valida versión, tipo y tamaño y devuelve un modelo inmutable tipado."""
    header = parse_header(data)
    if header is None:
        raise PacketSizeError(
            "packet_header_too_short",
            actual=len(data),
            expected=HEADER_SIZE,
        )
    if (
        header.packet_format,
        header.game_year,
        header.game_major_version,
        header.packet_version,
    ) != (
        2025,
        25,
        1,
        1,
    ):
        raise UnsupportedFormatError(
            "unsupported_format",
            packet_format=header.packet_format,
            game_year=header.game_year,
            game_major=header.game_major_version,
            game_minor=header.game_minor_version,
            packet_version=header.packet_version,
        )
    packet_decoder = PACKET_DECODERS.get(header.packet_id)
    if packet_decoder is None:
        raise UnsupportedPacketTypeError(
            "unsupported_packet_type",
            packet=packet_name(header.packet_id),
            packet_id=header.packet_id,
        )
    parsed = packet_decoder.decode(header, data)
    _validate_finite_values(parsed)
    return parsed
