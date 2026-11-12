"""Inventario y reglas de neumáticos (S4).

El módulo es puro: recibe el estado, el histórico y la configuración, y
devuelve las opciones elegibles para una parada. No abre el almacén ni usa
reloj. Cuando el juego no publica el inventario (telemetría restringida) la
respuesta es «sin opciones» con una nota explícita, nunca una lista vacía
silenciosa.

Solo se modelan compuestos secos: intermedios y mojado se detectan como
condiciones de lluvia y quedan fuera del alcance de la evaluación.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from statistics import median

from ..config import StrategyConfig
from ..metrics.comparability import classify_lap, is_clean
from ..metrics.history import MetricInputs
from ..metrics.types import Quality
from ..race_state import TyreSetSnapshot
from .pace_model import canonical_compound

#: Compuestos fuera del alcance: solo se avisa de su presencia.
WET_COMPOUNDS: frozenset[str] = frozenset({"intermediate", "wet"})
MANDATORY_DRY_COMPOUNDS: frozenset[str] = frozenset({"hard", "medium"})

#: Estados de clima que ya implican pista mojada.
WET_WEATHER: frozenset[str] = frozenset({"light_rain", "heavy_rain", "wet", "storm"})

#: Probabilidad de lluvia (porcentaje) declarada por el juego para el intervalo.
RAIN_PROBABILITY_THRESHOLD = 50

#: Ventana del pronóstico que se considera relevante cuando no puede deducirse
#: del estado de la sesión, en minutos.
FORECAST_HORIZON_MINUTES = 30

#: Tiempo de vuelta supuesto cuando el histórico no permite estimarlo, en segundos.
DEFAULT_LAP_TIME_S = 100.0

NOTE_NO_INVENTORY = "inventory_unavailable"
NOTE_AVAILABILITY_UNKNOWN = "inventory_availability_unknown"
NOTE_WET = "wet_conditions"
NOTE_FORECAST_RAIN = "forecast_rain"
NOTE_FORECAST_HORIZON_ASSUMED = "forecast_horizon_assumed"
NOTE_FORECAST_LAP_TIME_APPROXIMATE = "forecast_lap_time_approximate"
NOTE_RULE_PENDING = "two_compounds_pending"
NOTE_RULE_DISABLED = "two_compounds_disabled"
NOTE_RULE_SATISFIED = "two_compounds_satisfied"
NOTE_RULE_SPRINT = "two_compounds_not_applicable_sprint"
NOTE_RULE_SESSION_UNKNOWN = "race_type_unknown_rule_not_applied"
NOTE_RULE_WET_WAIVED = "two_compounds_waived_wet"
NOTE_MANDATORY_COMPOUND_PENDING = "medium_or_hard_pending"
NOTE_LIFE_RISK = "tyre_life_risk"
NOTE_LIFE_UNKNOWN = "tyre_life_unknown"
NOTE_LIFE_SCALED = "useful_life_scaled_by_wear"
NOTE_NO_OPTIONS = "no_tyre_options"
NOTE_WET_SETS_SKIPPED = "wet_compounds_out_of_scope"


def _finite(value: float, name: str, *, minimum: float | None = None) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} debe ser finito")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} no puede ser menor que {minimum}")
    return value


def _is_dry(compound: str | None) -> bool:
    return compound is not None and compound not in WET_COMPOUNDS


@dataclass(frozen=True, slots=True)
class TyreOption:
    """Un juego concreto que puede montarse en la próxima parada."""

    compound: str
    visual_compound: str | None = None
    set_index: int | None = None
    initial_wear_pct: float = 0.0
    useful_life_laps: float | None = None
    life_risk: bool = False
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.compound:
            raise ValueError("El compuesto de la opción no puede estar vacío")
        if self.set_index is not None and self.set_index < 0:
            raise ValueError("El índice del juego no puede ser negativo")
        _finite(self.initial_wear_pct, "initial_wear_pct", minimum=0.0)
        if self.initial_wear_pct > 100.0:
            raise ValueError("initial_wear_pct no puede superar 100")
        if self.useful_life_laps is not None:
            _finite(self.useful_life_laps, "useful_life_laps", minimum=0.0)
        object.__setattr__(self, "notes", tuple(self.notes))

    def to_dict(self) -> dict[str, object]:
        return {
            "compound": self.compound,
            "visual_compound": self.visual_compound,
            "set_index": self.set_index,
            "initial_wear_pct": self.initial_wear_pct,
            "useful_life_laps": self.useful_life_laps,
            "life_risk": self.life_risk,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class TyreOptions:
    """Conjunto acotado de paradas posibles y reglas de neumáticos vigentes."""

    options: tuple[TyreOption, ...] = ()
    used_dry_compounds: tuple[str, ...] = ()
    current_compound: str | None = None
    requires_stop: bool = False
    stay_out_allowed: bool = True
    wet_conditions: bool = False
    quality: Quality = Quality.UNAVAILABLE
    notes: tuple[str, ...] = ()
    inventory_available: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", tuple(self.options))
        object.__setattr__(self, "used_dry_compounds", tuple(self.used_dry_compounds))
        object.__setattr__(self, "notes", tuple(self.notes))
        if self.requires_stop and self.stay_out_allowed:
            raise ValueError("Si la parada es obligatoria, STAY_OUT no puede ser válida")

    def __iter__(self) -> Iterator[TyreOption]:
        return iter(self.options)

    def __len__(self) -> int:
        return len(self.options)

    @property
    def compounds(self) -> tuple[str, ...]:
        return tuple(option.compound for option in self.options)

    def get(self, compound: str) -> TyreOption | None:
        """Devuelve la primera opción del compuesto indicado, si existe."""
        normalized = canonical_compound(compound)
        return next(
            (option for option in self.options if option.compound == normalized),
            None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "options": [option.to_dict() for option in self.options],
            "used_dry_compounds": list(self.used_dry_compounds),
            "current_compound": self.current_compound,
            "requires_stop": self.requires_stop,
            "stay_out_allowed": self.stay_out_allowed,
            "wet_conditions": self.wet_conditions,
            "quality": self.quality.value,
            "notes": list(self.notes),
            "inventory_available": self.inventory_available,
        }


def _current_compound(inputs: MetricInputs) -> str | None:
    car = inputs.car
    if car is None:
        return None
    fitted = car.fitted_tyre.actual_tyre_compound if car.fitted_tyre is not None else None
    return canonical_compound(car.actual_tyre_compound or fitted)


def _is_mandatory_dry_compound(compound: str | None, visual: str | None) -> bool:
    actual = canonical_compound(compound)
    shown = canonical_compound(visual)
    return _is_dry(actual) and (
        actual in MANDATORY_DRY_COMPOUNDS or shown in MANDATORY_DRY_COMPOUNDS
    )


def _has_mandatory_dry_compound(inputs: MetricInputs) -> bool:
    if any(
        _is_mandatory_dry_compound(stint.compound, stint.visual_compound)
        for stint in inputs.history.stints_for(inputs.car_index)
    ):
        return True
    car = inputs.car
    if car is None:
        return False
    fitted = car.fitted_tyre
    actual = car.actual_tyre_compound or (
        fitted.actual_tyre_compound if fitted is not None else None
    )
    visual = car.visual_tyre_compound or (
        fitted.visual_tyre_compound if fitted is not None else None
    )
    return _is_mandatory_dry_compound(actual, visual)


def _is_sprint(inputs: MetricInputs) -> bool | None:
    session = inputs.state.session
    if session.session_type != "race":
        return False
    if not session.weekend_structure:
        return None
    return any("sprint_shootout" in item for item in session.weekend_structure)


def _used_dry_compounds(inputs: MetricInputs, current: str | None) -> tuple[str, ...]:
    used: set[str] = set()
    for stint in inputs.history.stints_for(inputs.car_index):
        compound = canonical_compound(stint.compound)
        if _is_dry(compound):
            used.add(compound)  # type: ignore[arg-type]
    if _is_dry(current):
        used.add(current)  # type: ignore[arg-type]
    return tuple(sorted(used))


def _used_wet_compounds(inputs: MetricInputs, current: str | None) -> bool:
    if current in WET_COMPOUNDS:
        return True
    return any(
        canonical_compound(stint.compound) in WET_COMPOUNDS
        for stint in inputs.history.stints_for(inputs.car_index)
    )


def _stale(inputs: MetricInputs) -> bool:
    try:
        freshness = inputs.state.freshness_for("tyre_sets")
    except KeyError:
        return False
    return freshness.is_stale(inputs.now)


def _window_laps(inputs: MetricInputs, cfg: StrategyConfig) -> int | None:
    """Vueltas que abarca la ventana de parada relevante para el pronóstico."""
    session = inputs.state.session
    car = inputs.car
    current_lap = car.current_lap if car is not None else None
    if session.pit_window_latest_lap is not None and current_lap is not None:
        return max(0, session.pit_window_latest_lap - current_lap)
    if cfg.max_pit_in_laps > 0:
        return cfg.max_pit_in_laps
    return None


def _lap_time_s(inputs: MetricInputs) -> tuple[float | None, bool]:
    """Mediana del tiempo de vuelta del jugador y si hubo que relajar el filtro."""
    records = inputs.history.records_for(inputs.car_index)
    clean = [
        record.lap_time_s
        for record in records
        if record.closed
        and record.lap_time_s is not None
        and is_clean(classify_lap(record, inputs.config))
    ]
    if clean:
        return median(clean), False
    any_closed = [
        record.lap_time_s for record in records if record.closed and record.lap_time_s is not None
    ]
    if any_closed:
        return median(any_closed), True
    return None, True


def _forecast_horizon_minutes(
    inputs: MetricInputs,
    cfg: StrategyConfig,
) -> tuple[float, tuple[str, ...]]:
    """Horizonte del pronóstico en minutos, derivado de la ventana de parada.

    La ventana se mide en vueltas y se convierte con el ritmo del jugador; si
    falta la ventana o el ritmo se usa el valor provisional y se declara.
    """
    laps = _window_laps(inputs, cfg)
    lap_time, approximate = _lap_time_s(inputs)
    if laps is None or lap_time is None:
        return float(FORECAST_HORIZON_MINUTES), (NOTE_FORECAST_HORIZON_ASSUMED,)
    notes: list[str] = []
    if approximate:
        notes.append(NOTE_FORECAST_LAP_TIME_APPROXIMATE)
    return laps * lap_time / 60.0, tuple(notes)


def _rain_expected(inputs: MetricInputs, horizon_minutes: float) -> bool:
    """True si el pronóstico relevante anuncia lluvia probable en el horizonte."""
    session = inputs.state.session
    current_type = session.session_type
    for sample in session.forecast:
        if current_type is not None and sample.session_type not in {None, current_type}:
            continue
        if sample.offset_minutes > horizon_minutes:
            continue
        if sample.rain_percentage >= RAIN_PROBABILITY_THRESHOLD:
            return True
    return False


def _remaining_laps(inputs: MetricInputs) -> int | None:
    session = inputs.state.session
    car = inputs.car
    current_lap = car.current_lap if car is not None else None
    if session.total_laps is None or current_lap is None:
        return None
    remaining = session.total_laps - current_lap
    return max(0, remaining)


def _option(
    item: TyreSetSnapshot,
    compound: str,
    remaining_laps: int | None,
) -> TyreOption:
    notes: list[str] = []
    wear = max(0.0, min(100.0, item.wear_pct))
    useful: float | None = None
    if item.usable_life_laps > 0:
        useful = float(item.usable_life_laps)
        if wear > 0.0:
            useful *= max(0.0, 1.0 - wear / 100.0)
            notes.append(NOTE_LIFE_SCALED)
        if item.lifespan_laps > 0:
            # Wear scales the estimate; the game's lifespan is a separate cap.
            useful = min(useful, float(item.lifespan_laps))
    else:
        notes.append(NOTE_LIFE_UNKNOWN)
    life_risk = remaining_laps is not None and useful is not None and useful < remaining_laps
    if life_risk:
        notes.append(NOTE_LIFE_RISK)
    return TyreOption(
        compound=compound,
        visual_compound=canonical_compound(item.visual_tyre_compound),
        set_index=item.index,
        initial_wear_pct=wear,
        useful_life_laps=useful,
        life_risk=life_risk,
        notes=tuple(notes),
    )


def _quality(
    inputs: MetricInputs,
    *,
    inventory_available: bool,
    options: tuple[TyreOption, ...],
) -> Quality:
    if not inventory_available:
        return Quality.UNAVAILABLE
    if not options:
        return Quality.INSUFFICIENT
    base = Quality.LOW if all(option.life_risk for option in options) else Quality.OK
    return Quality.STALE if _stale(inputs) else base


def tyre_options(inputs: MetricInputs, cfg: StrategyConfig) -> TyreOptions:
    """Acota qué paradas son posibles y válidas en la vuelta actual.

    En una carrera normal sin lluvia se exigen dos compuestos secos distintos,
    uno medio o duro. En fines de semana Sprint se identifica el Sprint por el
    calendario que incluye una sesión de Sprint Shootout; la carrera principal
    sigue sujeta a la regla. Las condiciones de lluvia se declaran pero no se
    modelan (fuera de alcance).
    """
    session = inputs.state.session
    current = _current_compound(inputs)
    used = _used_dry_compounds(inputs, current)
    wet_tyres_used = _used_wet_compounds(inputs, current)

    notes: list[str] = []
    wet = session.weather in WET_WEATHER
    if wet:
        notes.append(NOTE_WET)
    else:
        horizon, horizon_notes = _forecast_horizon_minutes(inputs, cfg)
        notes.extend(horizon_notes)
        if _rain_expected(inputs, horizon):
            wet = True
            notes.extend((NOTE_FORECAST_RAIN, NOTE_WET))

    sprint = _is_sprint(inputs)
    race_session = session.session_type in {
        "race",
        "race_2",
        "race_3",
    }
    rule_enabled = cfg.require_two_dry_compounds and race_session and sprint is False
    rule_waived = wet or wet_tyres_used
    mandatory_compound_used = _has_mandatory_dry_compound(inputs)
    requires_mandatory_compound = rule_enabled and not rule_waived and not mandatory_compound_used
    requires_stop = (
        rule_enabled and not rule_waived and (len(used) < 2 or requires_mandatory_compound)
    )
    if not rule_enabled:
        if sprint is True:
            notes.append(NOTE_RULE_SPRINT)
        elif cfg.require_two_dry_compounds and session.session_type == "race" and sprint is None:
            notes.append(NOTE_RULE_SESSION_UNKNOWN)
        else:
            notes.append(NOTE_RULE_DISABLED)
    elif rule_waived:
        notes.append(NOTE_RULE_WET_WAIVED)
    elif requires_stop:
        notes.append(NOTE_RULE_PENDING)
        if requires_mandatory_compound:
            notes.append(NOTE_MANDATORY_COMPOUND_PENDING)
    else:
        notes.append(NOTE_RULE_SATISFIED)

    inventory = inputs.state.tyre_inventory
    remaining = _remaining_laps(inputs)
    options: list[TyreOption] = []
    skipped_wet = False
    unknown_availability = False
    for item in inventory:
        # Solo se ofrece un juego con disponibilidad confirmada; un ``None``
        # significa que el juego no lo ha publicado y no se puede suponer.
        if item.available is not True:
            unknown_availability = unknown_availability or item.available is None
            continue
        compound = canonical_compound(item.actual_tyre_compound)
        if compound is None:
            continue
        if not _is_dry(compound):
            skipped_wet = True
            continue
        option = _option(item, compound, remaining)
        if requires_stop:
            if compound == current:
                continue
            if requires_mandatory_compound and not _is_mandatory_dry_compound(
                option.compound, option.visual_compound
            ):
                continue
        options.append(option)
    if skipped_wet:
        notes.append(NOTE_WET_SETS_SKIPPED)
    if unknown_availability:
        notes.append(NOTE_AVAILABILITY_UNKNOWN)

    options.sort(key=lambda option: (option.compound, option.set_index or -1))
    ordered = tuple(options)

    inventory_available = bool(inventory)
    if not inventory_available:
        notes.append(NOTE_NO_INVENTORY)
    if not ordered:
        notes.append(NOTE_NO_OPTIONS)

    return TyreOptions(
        options=ordered,
        used_dry_compounds=used,
        current_compound=current,
        requires_stop=requires_stop,
        stay_out_allowed=not requires_stop,
        wet_conditions=wet,
        quality=_quality(inputs, inventory_available=inventory_available, options=ordered),
        notes=tuple(notes),
        inventory_available=inventory_available,
    )


__all__ = [
    "DEFAULT_LAP_TIME_S",
    "FORECAST_HORIZON_MINUTES",
    "NOTE_AVAILABILITY_UNKNOWN",
    "NOTE_FORECAST_HORIZON_ASSUMED",
    "NOTE_FORECAST_LAP_TIME_APPROXIMATE",
    "NOTE_FORECAST_RAIN",
    "NOTE_LIFE_RISK",
    "NOTE_LIFE_SCALED",
    "NOTE_LIFE_UNKNOWN",
    "NOTE_MANDATORY_COMPOUND_PENDING",
    "NOTE_NO_INVENTORY",
    "NOTE_NO_OPTIONS",
    "NOTE_RULE_DISABLED",
    "NOTE_RULE_PENDING",
    "NOTE_RULE_SATISFIED",
    "NOTE_RULE_SESSION_UNKNOWN",
    "NOTE_RULE_SPRINT",
    "NOTE_RULE_WET_WAIVED",
    "NOTE_WET",
    "NOTE_WET_SETS_SKIPPED",
    "RAIN_PROBABILITY_THRESHOLD",
    "WET_COMPOUNDS",
    "WET_WEATHER",
    "MANDATORY_DRY_COMPOUNDS",
    "TyreOption",
    "TyreOptions",
    "tyre_options",
]
