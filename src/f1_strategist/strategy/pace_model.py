"""Modelo de ritmo y desgaste por compuesto (S2).

El módulo es puro y determinista: recibe el estado, el histórico, el perfil del
fin de semana y la configuración, y devuelve modelos inmutables. No usa reloj,
no hace E/S y no inventa valores: cuando falta la base o la pendiente, el
modelo lo indica y la alternativa correspondiente no se evalúa.

La pendiente en bruto incluye el efecto de quemar combustible; es coherente
para proyectar hacia delante y se declara como supuesto. La corrección solo se
aplica si ``fuel_effect_s_per_kg`` está configurado.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from statistics import linear_regression, mean
from typing import Literal

from ..config import StrategyConfig
from ..metrics.comparability import classify_lap, is_clean
from ..metrics.history import LapRecord, MetricInputs, current_stint_bounds
from ..metrics.types import Quality, assess_quality
from ..race_state import TyreSetSnapshot
from ..weekend_profile import CompoundProfile, WeekendProfile, blend_with_prior

#: Origen de la base y la pendiente de un modelo.
CompoundSource = Literal["race", "blend", "profile", "game_delta", "none"]

SOURCES: frozenset[str] = frozenset({"race", "blend", "profile", "game_delta", "none"})

#: Compuestos fuera del alcance de la primera versión.
WET_COMPOUNDS: frozenset[str] = frozenset({"intermediate", "wet"})

NOTE_SLOPE_SCALED = "slope_scaled_from_profile_wear"
NOTE_SLOPE_FROM_REFERENCE = "slope_from_reference_compound"
NOTE_SLOPE_UNAVAILABLE = "slope_unavailable"
NOTE_INVENTORY_UNAVAILABLE = "inventory_unavailable"
NOTE_NO_COMPOUNDS = "no_compounds"
NOTE_USED_SET = "used_set_initial_age_from_wear"

_MIN_RACE_LAPS = 3

_QUALITY_ORDER = {
    Quality.OK: 0,
    Quality.LOW: 1,
    Quality.STALE: 2,
    Quality.INSUFFICIENT: 3,
    Quality.UNAVAILABLE: 4,
}


def canonical_compound(value: str | None) -> str | None:
    """Normaliza el nombre del compuesto para comparar perfil, juego e histórico."""
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _worst_quality(left: Quality, right: Quality) -> Quality:
    return left if _QUALITY_ORDER[left] >= _QUALITY_ORDER[right] else right


def _finite(value: float, name: str, *, minimum: float | None = None) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} debe ser finito")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} no puede ser menor que {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class CompoundModel:
    """Modelo ``t(edad) = base + pendiente · edad`` de un compuesto.

    La edad se cuenta en vueltas del neumático. ``base_lap_s`` y
    ``slope_s_per_lap`` pueden ser ``None`` cuando solo hay datos parciales:
    en ese caso el modelo existe, pero ``available`` es ``False`` y no se
    puede proyectar con él.
    """

    compound: str
    base_lap_s: float | None
    slope_s_per_lap: float | None
    wear_rate_pct_per_lap: float | None
    n_race_laps: int
    source: CompoundSource
    quality: Quality
    visual_compound: str | None = None
    initial_age_laps: float | None = None
    usable_life_laps: float | None = None
    cliff_penalty_s_per_lap: float | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.compound:
            raise ValueError("El compuesto no puede estar vacío")
        if self.source not in SOURCES:
            raise ValueError(f"Origen de modelo desconocido: {self.source}")
        if self.n_race_laps < 0:
            raise ValueError("n_race_laps no puede ser negativo")
        if self.base_lap_s is not None:
            _finite(self.base_lap_s, "base_lap_s", minimum=0.0)
        if self.slope_s_per_lap is not None:
            _finite(self.slope_s_per_lap, "slope_s_per_lap")
        if self.wear_rate_pct_per_lap is not None:
            _finite(self.wear_rate_pct_per_lap, "wear_rate_pct_per_lap", minimum=0.0)
        if self.initial_age_laps is not None:
            _finite(self.initial_age_laps, "initial_age_laps", minimum=0.0)
        if self.usable_life_laps is not None:
            _finite(self.usable_life_laps, "usable_life_laps", minimum=0.0)
        if self.cliff_penalty_s_per_lap is not None:
            _finite(self.cliff_penalty_s_per_lap, "cliff_penalty_s_per_lap", minimum=0.0)
        object.__setattr__(self, "notes", tuple(self.notes))

    @property
    def available(self) -> bool:
        """True si hay base y pendiente para proyectar tiempos de vuelta."""
        return self.base_lap_s is not None and self.slope_s_per_lap is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "compound": self.compound,
            "visual_compound": self.visual_compound,
            "base_lap_s": self.base_lap_s,
            "slope_s_per_lap": self.slope_s_per_lap,
            "wear_rate_pct_per_lap": self.wear_rate_pct_per_lap,
            "n_race_laps": self.n_race_laps,
            "source": self.source,
            "quality": self.quality.value,
            "initial_age_laps": self.initial_age_laps,
            "usable_life_laps": self.usable_life_laps,
            "cliff_penalty_s_per_lap": self.cliff_penalty_s_per_lap,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class CompoundModels:
    """Colección determinista de modelos, uno por compuesto elegible."""

    models: tuple[CompoundModel, ...] = ()
    reference_compound: str | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", tuple(self.models))
        object.__setattr__(self, "notes", tuple(self.notes))
        compounds = [model.compound for model in self.models]
        if len(set(compounds)) != len(compounds):
            raise ValueError("Hay compuestos duplicados en los modelos")

    def __iter__(self) -> Iterator[CompoundModel]:
        return iter(self.models)

    def __len__(self) -> int:
        return len(self.models)

    @property
    def compounds(self) -> tuple[str, ...]:
        return tuple(model.compound for model in self.models)

    @property
    def available(self) -> tuple[CompoundModel, ...]:
        return tuple(model for model in self.models if model.available)

    def get(self, compound: str) -> CompoundModel | None:
        normalized = canonical_compound(compound)
        return next(
            (model for model in self.models if model.compound == normalized),
            None,
        )


@dataclass(frozen=True, slots=True)
class _Fit:
    base_lap_s: float
    slope_s_per_lap: float
    n_laps: int


def _freshness_is_stale(inputs: MetricInputs) -> bool:
    for group in ("lap_data", "session_history"):
        try:
            freshness = inputs.state.freshness_for(group)
        except KeyError:
            continue
        if freshness.is_stale(inputs.now):
            return True
    return False


def _clean_laps(
    inputs: MetricInputs,
    compound: str,
    bounds: tuple[int, int | None] | None,
) -> tuple[LapRecord, ...]:
    """Vueltas cerradas, limpias y del compuesto indicado, sin mirar al futuro."""
    car = inputs.car
    current_lap = car.current_lap if car is not None and car.current_lap is not None else None
    records: list[LapRecord] = []
    for record in inputs.history.records_for(inputs.car_index):
        if not record.closed or record.lap_time_s is None:
            continue
        if canonical_compound(record.compound) != compound:
            continue
        if record.tyre_age_start is None:
            continue
        if current_lap is not None and record.lap_number > current_lap:
            continue
        if bounds is not None:
            start, end = bounds
            if record.lap_number < start:
                continue
            if end is not None and record.lap_number > end:
                continue
        if record.valid is False or "telemetry_partial" in record.flags:
            continue
        if not is_clean(classify_lap(record, inputs.config)):
            continue
        records.append(record)
    records.sort(key=lambda record: record.lap_number)
    return tuple(records)


def _fit_compound(
    records: tuple[LapRecord, ...],
    fuel_effect_s_per_kg: float | None,
) -> _Fit | None:
    """Regresión de tiempo de vuelta sobre la edad del neumático al empezar la vuelta."""
    points: list[tuple[float, float]] = []
    for record in records:
        if record.lap_time_s is None or record.tyre_age_start is None:
            continue
        value = record.lap_time_s
        if fuel_effect_s_per_kg is not None:
            if record.fuel_start_kg is None:
                continue
            value -= record.fuel_start_kg * fuel_effect_s_per_kg
        points.append((float(record.tyre_age_start), value))
    if len(points) < _MIN_RACE_LAPS:
        return None
    ages = [age for age, _ in points]
    if len(set(ages)) < 2:
        return None
    regression = linear_regression(ages, [value for _, value in points])
    if not math.isfinite(regression.intercept) or not math.isfinite(regression.slope):
        return None
    return _Fit(regression.intercept, regression.slope, len(points))


def _wear_rate_from_laps(records: tuple[LapRecord, ...]) -> float | None:
    points: list[tuple[float, float]] = []
    for record in records:
        if record.tyre_age_end is None or not record.tyre_wear_end_pct:
            continue
        points.append((float(record.tyre_age_end), mean(record.tyre_wear_end_pct)))
    if len(points) < _MIN_RACE_LAPS or len({age for age, _ in points}) < 2:
        return None
    slope = linear_regression(
        [age for age, _ in points],
        [wear for _, wear in points],
    ).slope
    if not math.isfinite(slope) or slope <= 0:
        return None
    return slope


def _profile_compound(profile: WeekendProfile | None, compound: str) -> CompoundProfile | None:
    if profile is None:
        return None
    return profile.compound(compound)


def _scaled_slope(
    profile: WeekendProfile | None,
    compound: str,
    reference: str | None,
    reference_slope: float | None,
) -> tuple[float | None, str | None]:
    """Pendiente del perfil escalada por la tasa de desgaste relativa."""
    if profile is None or reference_slope is None or reference is None:
        return None, None
    target = profile.compound(compound)
    anchor = profile.compound(reference)
    if target is None or anchor is None:
        return None, None
    target_wear = target.wear_rate_pct_per_lap.value
    anchor_wear = anchor.wear_rate_pct_per_lap.value
    if target_wear is None or anchor_wear is None or anchor_wear <= 0:
        return None, None
    return reference_slope * target_wear / anchor_wear, NOTE_SLOPE_SCALED


def _inventory_set(inputs: MetricInputs, compound: str) -> TyreSetSnapshot | None:
    candidates = [
        item
        for item in inputs.state.tyre_inventory
        if canonical_compound(item.actual_tyre_compound) == compound and item.available is not False
    ]
    candidates.sort(key=lambda item: item.index)
    return candidates[0] if candidates else None


def _visual_compound(
    inputs: MetricInputs,
    compound: str,
    profile_entry: CompoundProfile | None,
    inventory: TyreSetSnapshot | None,
) -> str | None:
    car = inputs.car
    if car is not None and canonical_compound(car.actual_tyre_compound) == compound:
        visual = canonical_compound(car.visual_tyre_compound)
        if visual is not None:
            return visual
    if profile_entry is not None and profile_entry.visual_compounds:
        return canonical_compound(profile_entry.visual_compounds[0])
    if inventory is not None:
        return canonical_compound(inventory.visual_tyre_compound)
    return None


def _relative_delta(
    profile: WeekendProfile | None,
    compound: str,
    reference: str | None,
) -> float | None:
    """Diferencia de ritmo del compuesto frente al de referencia, en segundos."""
    if profile is None or reference is None:
        return None
    target = profile.compound(compound)
    if target is None:
        return None
    target_delta = target.pace_delta_vs_reference_s.value
    if target_delta is None:
        return None
    if reference == canonical_compound(profile.reference_compound):
        return target_delta
    anchor = profile.compound(reference)
    if anchor is None:
        return None
    anchor_delta = anchor.pace_delta_vs_reference_s.value
    if anchor_delta is None:
        return None
    return target_delta - anchor_delta


def _initial_age(
    inputs: MetricInputs,
    compound: str,
    fitted: str | None,
    wear_rate: float | None,
    inventory: TyreSetSnapshot | None,
) -> tuple[float | None, tuple[str, ...]]:
    car = inputs.car
    if compound == fitted and car is not None and car.tyre_age_laps is not None:
        return float(car.tyre_age_laps), ()
    if inventory is None:
        return None, ()
    if inventory.wear_pct <= 0:
        return 0.0, ()
    if wear_rate is None or wear_rate <= 0:
        return None, ()
    return inventory.wear_pct / wear_rate, (NOTE_USED_SET,)


def _usable_life(
    inputs: MetricInputs,
    compound: str,
    fitted: str | None,
    inventory: TyreSetSnapshot | None,
    wear_rate: float | None,
    initial_age: float | None,
) -> float | None:
    candidates: list[float] = []
    car = inputs.car
    if (
        compound == fitted
        and car is not None
        and car.fitted_tyre is not None
        and car.fitted_tyre.usable_life_laps > 0
    ):
        candidates.append(float(car.fitted_tyre.usable_life_laps))
    if inventory is not None and inventory.usable_life_laps > 0:
        candidates.append(float(inventory.usable_life_laps))
    limit = inputs.config.tyre_wear_limit_pct
    if limit is not None and wear_rate is not None and wear_rate > 0:
        wear_now = inventory.wear_pct if inventory is not None else 0.0
        remaining_wear = max(0.0, limit - wear_now)
        candidates.append((initial_age or 0.0) + remaining_wear / wear_rate)
    return min(candidates) if candidates else None


def _target_compounds(inputs: MetricInputs, fitted: str | None) -> tuple[str, ...]:
    compounds: set[str] = set()
    if fitted is not None:
        compounds.add(fitted)
    for record in inputs.history.records_for(inputs.car_index):
        compound = canonical_compound(record.compound)
        if compound is not None:
            compounds.add(compound)
    for item in inputs.state.tyre_inventory:
        if item.available is False:
            continue
        compound = canonical_compound(item.actual_tyre_compound)
        if compound is not None:
            compounds.add(compound)
    profile = inputs.profile
    if profile is not None:
        for entry in profile.compounds:
            compound = canonical_compound(entry.compound)
            if compound is not None:
                compounds.add(compound)
    return tuple(sorted(compounds - WET_COMPOUNDS))


def _mounted_compound(inputs: MetricInputs) -> str | None:
    car = inputs.car
    if car is None:
        return None
    fitted_tyre = car.fitted_tyre.actual_tyre_compound if car.fitted_tyre is not None else None
    return canonical_compound(car.actual_tyre_compound or fitted_tyre)


def _build_model(
    inputs: MetricInputs,
    cfg: StrategyConfig,
    compound: str,
    fitted: str | None,
    fits: dict[str, _Fit],
    reference: str | None,
    inventory: TyreSetSnapshot | None,
) -> CompoundModel | None:
    profile = inputs.profile
    profile_entry = _profile_compound(profile, compound)
    prior_base = profile_entry.pace_mean_s.value if profile_entry is not None else None
    prior_quality = profile_entry.quality if profile_entry is not None else Quality.UNAVAILABLE
    fit = fits.get(compound)
    reference_fit = fits.get(reference) if reference is not None else None
    reference_slope = reference_fit.slope_s_per_lap if reference_fit is not None else None
    reference_base = reference_fit.base_lap_s if reference_fit is not None else None
    notes: list[str] = []
    source: CompoundSource
    base: float | None
    slope: float | None
    stale = _freshness_is_stale(inputs)

    if fit is not None:
        prior_slope, slope_note = _scaled_slope(profile, compound, reference, reference_slope)
        base = blend_with_prior(prior_base, fit.base_lap_s, fit.n_laps, cfg.prior_strength_k)
        slope = blend_with_prior(prior_slope, fit.slope_s_per_lap, fit.n_laps, cfg.prior_strength_k)
        source = "blend" if prior_base is not None else "race"
        low, ok = inputs.config.thresholds("pace")
        quality = assess_quality(fit.n_laps, ok, low, stale)
        if source == "blend":
            quality = _worst_quality(quality, prior_quality)
        n_race_laps = fit.n_laps
    else:
        scaled_slope, slope_note = _scaled_slope(profile, compound, reference, reference_slope)
        slope = scaled_slope
        if slope is None:
            slope = reference_slope
            slope_note = NOTE_SLOPE_FROM_REFERENCE if slope is not None else None
        n_race_laps = 0
        delta = _relative_delta(profile, compound, reference)
        if prior_base is not None:
            base = prior_base
            source = "profile"
        elif delta is not None and reference_base is not None:
            base = reference_base + delta
            source = "profile"
        elif inventory is not None and reference_base is not None and compound != reference:
            base = reference_base + inventory.lap_delta_time_ms / 1000.0
            source = "game_delta"
        elif compound == reference and reference_base is not None:
            base = reference_base
            source = "profile"
        else:
            base = None
            source = "profile"
        if source == "game_delta":
            quality = _worst_quality(
                prior_quality if prior_quality is not Quality.UNAVAILABLE else Quality.LOW,
                Quality.LOW,
            )
        else:
            quality = prior_quality
        if stale:
            quality = Quality.STALE

    if base is None:
        return None
    if slope is None:
        notes.append(NOTE_SLOPE_UNAVAILABLE)
    elif slope_note is not None:
        notes.append(slope_note)

    wear_rate = _wear_rate_from_laps(
        _clean_laps(
            inputs,
            compound,
            current_stint_bounds(inputs, inputs.car_index) if compound == fitted else None,
        )
    )
    if wear_rate is None and profile_entry is not None:
        wear_rate = profile_entry.wear_rate_pct_per_lap.value

    initial_age, age_notes = _initial_age(inputs, compound, fitted, wear_rate, inventory)
    notes.extend(age_notes)
    if not inputs.state.tyre_inventory:
        notes.append(NOTE_INVENTORY_UNAVAILABLE)
    usable_life = _usable_life(
        inputs,
        compound,
        fitted,
        inventory,
        wear_rate,
        initial_age,
    )
    visual = _visual_compound(inputs, compound, profile_entry, inventory)
    return CompoundModel(
        compound=compound,
        base_lap_s=base,
        slope_s_per_lap=slope,
        wear_rate_pct_per_lap=wear_rate,
        n_race_laps=n_race_laps,
        source=source,
        quality=quality,
        visual_compound=visual,
        initial_age_laps=initial_age,
        usable_life_laps=usable_life,
        cliff_penalty_s_per_lap=cfg.cliff_penalty_s_per_lap,
        notes=tuple(notes),
    )


def _select_reference(fits: dict[str, _Fit], fitted: str | None) -> str | None:
    """Compuesto de referencia: el montado si tiene ajuste; si no, el de más vueltas."""
    if fitted is not None and fitted in fits:
        return fitted
    if not fits:
        return None
    return min(fits, key=lambda compound: (-fits[compound].n_laps, compound))


def build_compound_models(inputs: MetricInputs, cfg: StrategyConfig) -> CompoundModels:
    """Construye un modelo por compuesto elegible a partir de las entradas actuales."""
    fitted = _mounted_compound(inputs)
    targets = _target_compounds(inputs, fitted)
    if not targets:
        return CompoundModels(notes=(NOTE_NO_COMPOUNDS,))

    bounds = current_stint_bounds(inputs, inputs.car_index)
    fuel_effect = inputs.config.fuel_effect_s_per_kg
    fits: dict[str, _Fit] = {}
    for compound in targets:
        records = _clean_laps(inputs, compound, bounds if compound == fitted else None)
        fit = _fit_compound(records, fuel_effect)
        if fit is not None:
            fits[compound] = fit

    reference = _select_reference(fits, fitted)
    models: list[CompoundModel] = []
    for compound in targets:
        model = _build_model(
            inputs,
            cfg,
            compound,
            fitted,
            fits,
            reference,
            _inventory_set(inputs, compound),
        )
        if model is not None:
            models.append(model)
    if not models:
        return CompoundModels(notes=(NOTE_NO_COMPOUNDS,))
    return CompoundModels(models=tuple(models), reference_compound=reference)


def project_lap_times(
    model: CompoundModel,
    start_age: float,
    laps: float,
) -> tuple[float, ...]:
    """Proyecta el tiempo de cada vuelta restante a partir de la edad actual.

    Las edades usadas son ``start_age, start_age + 1, ...``; ``laps`` puede ser
    fraccionario y se devuelven ``ceil(laps)`` tiempos. Si el modelo no tiene
    base o pendiente se lanza ``ValueError``: el llamador debe comprobar
    ``model.available`` antes de proyectar. La penalización de fin de vida se
    aplica linealmente a partir de ``usable_life_laps``.
    """
    if not model.available:
        raise ValueError(f"El modelo de {model.compound!r} no tiene base y pendiente")
    assert model.base_lap_s is not None
    assert model.slope_s_per_lap is not None
    _finite(start_age, "start_age", minimum=0.0)
    if not math.isfinite(laps):
        raise ValueError("laps debe ser finito")
    if laps < 0:
        raise ValueError("laps no puede ser negativo")
    count = math.ceil(laps)
    times: list[float] = []
    for index in range(count):
        age = start_age + index
        value = model.base_lap_s + model.slope_s_per_lap * age
        life = model.usable_life_laps
        penalty = model.cliff_penalty_s_per_lap
        if life is not None and penalty is not None and age > life:
            value += penalty * (age - life)
        times.append(value)
    return tuple(times)


__all__ = [
    "NOTE_INVENTORY_UNAVAILABLE",
    "NOTE_NO_COMPOUNDS",
    "NOTE_SLOPE_FROM_REFERENCE",
    "NOTE_SLOPE_SCALED",
    "NOTE_SLOPE_UNAVAILABLE",
    "NOTE_USED_SET",
    "SOURCES",
    "WET_COMPOUNDS",
    "CompoundModel",
    "CompoundModels",
    "build_compound_models",
    "canonical_compound",
    "project_lap_times",
]
