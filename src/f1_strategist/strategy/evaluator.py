"""Evaluación de alternativas y tiempo restante (S5).

El módulo es puro y determinista: no usa reloj, no hace E/S y no inventa datos.
Recibe los modelos de ritmo (S2), la pérdida en boxes (S3), las opciones de
neumático (S4) y el estado, y devuelve el tiempo restante de cada alternativa
junto con su ganancia frente a la línea base.

La línea base es continuar con el compuesto montado. Si una regla obliga a
parar o el neumático no llega al final, la referencia es la mejor opción en
la vuelta ideal del juego o, si no hay ventana, en la última vuelta viable.

El tiempo restante de una alternativa es la suma de sus vueltas proyectadas
más la pérdida en boxes. La pérdida ya incluye la in-lap y la out-lap, así que
no se añade ninguna penalización extra por la vuelta de salida. El coste de
tráfico solo se suma cuando hay parámetro configurado y la parada es inmediata:
sin parámetro, el tráfico es un riesgo declarado y nunca un tiempo inventado.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import StrategyConfig
from ..metrics.history import MetricInputs
from ..metrics.types import Quality
from .pace_model import (
    CompoundModel,
    CompoundModels,
    canonical_compound,
    project_lap_times,
)
from .pit_model import PitLossEstimate, RejoinEstimate, estimate_rejoin
from .types import (
    ASSUMPTION_RIVALS_DO_NOT_PIT,
    BASELINE_STAY_OUT,
    RISK_TRAFFIC_NOT_PRICED,
    RISK_TYRE_LIFE_EXCEEDED,
    AlternativeResult,
    Perturbation,
    StrategyAction,
)
from .tyre_options import TyreOption, TyreOptions

NOTE_BASELINE = "baseline_alternative"
NOTE_NO_MODEL = "alternative_without_compound_model"
NOTE_PIT_LOSS_UNAVAILABLE = "alternative_without_pit_loss"
NOTE_STAY_OUT_NOT_VIABLE = "stay_out_not_viable"
NOTE_STAY_OUT_UNAVAILABLE = "stay_out_unavailable"
NOTE_TRAFFIC_COST = "traffic_cost_applied"
NOTE_PERTURBATION = "perturbation_applied"

#: Perturbación neutra; se usa como valor por defecto (evita crear objetos en
#: la firma) y como referencia para detectar si se aplicó alguna perturbación.
_NEUTRAL_PERTURBATION = Perturbation()

#: De mejor a peor: sirve para combinar calidades de componentes distintos.
_QUALITY_RANK = {
    Quality.OK: 0,
    Quality.LOW: 1,
    Quality.STALE: 2,
    Quality.INSUFFICIENT: 3,
    Quality.UNAVAILABLE: 4,
}


@dataclass(frozen=True, slots=True)
class _Candidate:
    """Alternativa ya valorada, antes de compararla con la línea base."""

    key: str
    action: StrategyAction
    remaining_time_s: float
    projected_laps_s: float
    quality: Quality
    pit_lap: int | None = None
    compound: str | None = None
    set_index: int | None = None
    pit_loss_s: float | None = None
    traffic_cost_s: float | None = None
    rejoin_position: int | None = None
    traffic_risk: bool = False
    notes: tuple[str, ...] = ()
    stop_in: int | None = None


def _worst(*qualities: Quality) -> Quality:
    return max(qualities, key=lambda quality: _QUALITY_RANK[quality])


def _merge_notes(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Une notas sin duplicados y conservando el orden de aparición."""
    seen: dict[str, None] = {}
    for group in groups:
        for note in group:
            seen.setdefault(note, None)
    return tuple(seen)


def _mounted_compound(inputs: MetricInputs) -> str | None:
    car = inputs.car
    if car is None:
        return None
    fitted = car.fitted_tyre
    value = fitted.actual_tyre_compound if fitted is not None else None
    return canonical_compound(value or car.actual_tyre_compound)


def _laps_remaining(inputs: MetricInputs) -> float | None:
    """Vueltas restantes incluyendo la fracción recorrida de la vuelta actual."""
    session = inputs.state.session
    if session.total_laps is None:
        return None
    if session.ended is True:
        return 0.0
    if session.safety_car_status == "formation_lap":
        return float(session.total_laps)
    car = inputs.car
    if car is None or car.current_lap is None:
        return None
    distance = car.lap_distance_m
    track_length = session.track_length_m
    if distance is None or track_length is None or track_length <= 0:
        return None
    lap_fraction = min(max(distance / track_length, 0.0), 1.0)
    return max(0.0, session.total_laps - (car.current_lap - 1) - lap_fraction)


def _scaled(model: CompoundModel, perturbation: Perturbation) -> CompoundModel:
    """Copia el modelo con la pendiente escalada por la perturbación."""
    slope = model.slope_s_per_lap
    if slope is None or perturbation.slope_scale == 1.0:
        return model
    return _copy_model(model, slope=slope * perturbation.slope_scale)


def _copy_model(
    model: CompoundModel,
    *,
    slope: float | None = None,
    initial_age: float | None = None,
    usable_life: float | None = None,
    visual_compound: str | None = None,
) -> CompoundModel:
    """Copia el modelo cambiando solo los campos indicados."""
    return CompoundModel(
        compound=model.compound,
        base_lap_s=model.base_lap_s,
        slope_s_per_lap=model.slope_s_per_lap if slope is None else slope,
        wear_rate_pct_per_lap=model.wear_rate_pct_per_lap,
        n_race_laps=model.n_race_laps,
        source=model.source,
        quality=model.quality,
        visual_compound=visual_compound or model.visual_compound,
        initial_age_laps=model.initial_age_laps if initial_age is None else initial_age,
        usable_life_laps=model.usable_life_laps if usable_life is None else usable_life,
        cliff_penalty_s_per_lap=model.cliff_penalty_s_per_lap,
        notes=model.notes,
    )


def _projected_time(
    model: CompoundModel,
    start_age: float,
    laps: float,
    *,
    initial_fraction: float = 0.0,
) -> float:
    """Suma los tiempos proyectados cubriendo exactamente ``laps`` vueltas.

    La fracción de la vuelta actual se proyecta primero a la edad actual; las
    vueltas completas siguientes avanzan la edad antes de cualquier fracción final.
    """
    initial_fraction = min(max(initial_fraction, 0.0), laps)
    total = (
        initial_fraction * project_lap_times(model, start_age, 1)[0]
        if initial_fraction > 0.0
        else 0.0
    )
    after_initial = laps - initial_fraction
    whole = math.floor(after_initial)
    total += math.fsum(project_lap_times(model, start_age + 1.0, whole))
    fractional = after_initial - whole
    if fractional > 0.0:
        total += fractional * project_lap_times(model, start_age + whole + 1.0, 1)[0]
    return total


def _current_lap_fraction_remaining(inputs: MetricInputs) -> float:
    car = inputs.car
    track_length = inputs.state.session.track_length_m
    if car is None or car.lap_distance_m is None or track_length is None or track_length <= 0:
        return 0.0
    fraction = min(max(car.lap_distance_m / track_length, 0.0), 1.0)
    return 1.0 - fraction


def _start_age(model: CompoundModel, option: TyreOption) -> float | None:
    """Edad inicial del juego; ``None`` si el desgaste no permite deducirla."""
    wear = option.initial_wear_pct
    if wear <= 0.0:
        return 0.0
    rate = model.wear_rate_pct_per_lap
    if rate is None or rate <= 0.0:
        return None
    return wear / rate


def _stop_window(inputs: MetricInputs, cfg: StrategyConfig, laps: float) -> int:
    """Última parada con sentido, expresada en vueltas desde ahora."""
    session = inputs.state.session
    car = inputs.car
    current_lap = car.current_lap if car is not None else None
    window = cfg.max_pit_in_laps
    if session.pit_window_latest_lap is not None and current_lap is not None:
        window = session.pit_window_latest_lap - current_lap
    return max(0, min(window, math.floor(laps) - 1))


def _traffic_cost(
    rejoin: RejoinEstimate | None,
    inputs: MetricInputs,
    cfg: StrategyConfig,
) -> float | None:
    """Coste de tráfico configurado; ``None`` sin parámetro o sin riesgo."""
    if rejoin is None or not rejoin.traffic_risk:
        return None
    per_lap = cfg.traffic_cost_s_per_lap
    if per_lap is None:
        return None
    return per_lap * inputs.config.traffic_lap_fraction


def _traffic_notes(
    rejoin: RejoinEstimate | None,
    traffic_cost_s: float | None,
) -> tuple[str, ...]:
    """Notas de tráfico: supuesto de reincorporación y riesgo sin precio."""
    if rejoin is None:
        return ()
    notes: list[str] = []
    if rejoin.available:
        notes.append(ASSUMPTION_RIVALS_DO_NOT_PIT)
    if rejoin.traffic_risk:
        notes.append(NOTE_TRAFFIC_COST if traffic_cost_s is not None else RISK_TRAFFIC_NOT_PRICED)
    return tuple(notes)


def _stay_out(
    model: CompoundModel,
    start_age: float,
    laps: float,
    current_fraction: float,
    notes: tuple[str, ...],
) -> _Candidate | None:
    """Alternativa de no parar; ``None`` si el neumático no llega al final."""
    life = model.usable_life_laps
    if life is not None and start_age + laps > life:
        return None
    projected = _projected_time(
        model,
        start_age,
        laps,
        initial_fraction=current_fraction,
    )
    return _Candidate(
        key=BASELINE_STAY_OUT,
        action=StrategyAction.NO_CHANGE,
        remaining_time_s=projected,
        projected_laps_s=projected,
        quality=model.quality,
        compound=model.compound,
        notes=notes,
    )


def _pit(
    option: TyreOption,
    model: CompoundModel,
    *,
    inputs: MetricInputs,
    inventory_quality: Quality,
    stop_in: int,
    laps: float,
    stay: CompoundModel | None,
    stay_age: float | None,
    pit_loss_s: float,
    quality: Quality,
    stop_lap: int | None,
    rejoin: RejoinEstimate | None,
    traffic_cost_s: float | None,
    notes: tuple[str, ...],
    option_identity: str,
) -> _Candidate | None:
    """Alternativa de parada; ``None`` si no puede proyectarse con rigor."""
    start_age = _start_age(model, option)
    if start_age is None:
        return None
    before_laps = float(stop_in)
    current_fraction = _current_lap_fraction_remaining(inputs)
    new_laps = laps - before_laps
    if new_laps <= 0.0:
        return None
    before = 0.0
    if stop_in > 0:
        if stay is None or stay_age is None:
            return None
        before = _projected_time(
            stay,
            stay_age,
            before_laps,
            initial_fraction=current_fraction,
        )
    # El fin de vida del juego seleccionado manda sobre el del compuesto: es la
    # goma que se monta y la que sufre el posible acantilado.
    life = None if option.useful_life_laps is None else start_age + option.useful_life_laps
    option_model = _copy_model(
        model,
        initial_age=start_age,
        usable_life=life,
        visual_compound=option.visual_compound,
    )
    after = _projected_time(
        option_model,
        start_age,
        new_laps,
        initial_fraction=current_fraction,
    )
    action = StrategyAction.PIT_NOW if stop_in == 0 else StrategyAction.PIT_IN
    suffix = f":{option.compound}:{option_identity}"
    key = f"pit_now{suffix}" if stop_in == 0 else f"pit_in_{stop_in}{suffix}"
    local = _traffic_notes(rejoin, traffic_cost_s)
    if (option.useful_life_laps is not None and new_laps > option.useful_life_laps) or (
        stay is not None
        and stay.usable_life_laps is not None
        and stay_age is not None
        and stay_age + stop_in > stay.usable_life_laps
    ):
        local = (*local, RISK_TYRE_LIFE_EXCEEDED)
    return _Candidate(
        key=key,
        action=action,
        remaining_time_s=before + after + pit_loss_s + (traffic_cost_s or 0.0),
        projected_laps_s=before + after,
        quality=_worst(
            quality,
            model.quality,
            inventory_quality,
            *((stay.quality,) if stop_in > 0 and stay is not None else ()),
        ),
        pit_lap=stop_lap,
        compound=option.compound,
        set_index=option.set_index,
        pit_loss_s=pit_loss_s,
        traffic_cost_s=traffic_cost_s,
        rejoin_position=rejoin.position if rejoin is not None else None,
        traffic_risk=bool(rejoin is not None and rejoin.traffic_risk),
        notes=_merge_notes(local, notes),
        stop_in=stop_in,
    )


def _pit_quality(pit: PitLossEstimate, rejoin: RejoinEstimate | None) -> Quality:
    """Peor calidad de los componentes de una parada que sí se conocen."""
    quality = pit.quality
    if rejoin is not None and rejoin.available:
        quality = _worst(quality, rejoin.quality)
    return quality


def evaluate_alternatives(
    models: CompoundModels,
    pit: PitLossEstimate,
    options: TyreOptions,
    inputs: MetricInputs,
    cfg: StrategyConfig,
    perturbation: Perturbation = _NEUTRAL_PERTURBATION,
    *,
    baseline_key: str | None = None,
) -> tuple[AlternativeResult, ...]:
    """Evalúa las alternativas y las devuelve con la línea base primero.

    Se emiten ``STAY_OUT`` (solo si la regla lo permite y el neumático llega al
    final), ``PIT_NOW`` y ``PIT_IN(n)`` para cada compuesto elegible, con ``n``
    desde 1 hasta el final de la ventana. Un compuesto sin modelo o una pérdida
    en boxes no disponible no generan alternativa: se omiten y se declaran en
    las notas de las que sí quedan.

    El orden es determinista: línea base, ``PIT_NOW`` y ``PIT_IN(n)`` con ``n``
    creciente, y dentro de cada grupo el orden de las opciones recibidas. Si no
    hay ninguna alternativa evaluable devuelve una tupla vacía.
    """
    laps = _laps_remaining(inputs)
    if laps is None or laps <= 0.0 or not models.models:
        return ()

    notes: list[str] = []
    if perturbation != _NEUTRAL_PERTURBATION:
        notes.append(NOTE_PERTURBATION)

    mounted = _mounted_compound(inputs)
    stay_model = models.get(mounted) if mounted is not None else None
    stay: CompoundModel | None = None
    stay_age: float | None = None
    if stay_model is not None and stay_model.available:
        stay = _scaled(stay_model, perturbation)
        stay_age = stay.initial_age_laps

    pit_loss = (
        None if pit.value_s is None else max(0.0, pit.value_s + perturbation.pit_loss_delta_s)
    )
    current_fraction = _current_lap_fraction_remaining(inputs)
    if pit_loss is None:
        notes.append(NOTE_PIT_LOSS_UNAVAILABLE)

    candidates: list[_Candidate] = []
    if options.stay_out_allowed:
        candidate = (
            _stay_out(stay, stay_age, laps, current_fraction, tuple(notes))
            if stay is not None and stay_age is not None
            else None
        )
        if candidate is not None:
            candidates.append(candidate)
        else:
            notes.append(NOTE_STAY_OUT_UNAVAILABLE)
            if stay is not None and stay_age is not None:
                notes.append(NOTE_STAY_OUT_NOT_VIABLE)
    else:
        notes.append(NOTE_STAY_OUT_NOT_VIABLE)

    if pit_loss is not None:
        rejoin = estimate_rejoin(inputs, pit_loss, cfg)
        traffic_cost = _traffic_cost(rejoin, inputs, cfg)
        quality = _pit_quality(pit, rejoin)
        car = inputs.car
        current_lap = car.current_lap if car is not None else None
        missing_models: list[str] = []
        evaluated = 0
        for stop_in in range(0, _stop_window(inputs, cfg, laps) + 1):
            stop_lap = current_lap + stop_in if current_lap is not None else None
            for option_index, option in enumerate(options.options):
                model = models.get(option.compound)
                if model is None or not model.available:
                    if option.compound and option.compound not in missing_models:
                        missing_models.append(option.compound)
                    continue
                candidate = _pit(
                    option,
                    _scaled(model, perturbation),
                    inputs=inputs,
                    inventory_quality=options.quality,
                    stop_in=stop_in,
                    laps=laps,
                    stay=stay,
                    stay_age=stay_age,
                    pit_loss_s=pit_loss,
                    quality=quality,
                    stop_lap=stop_lap,
                    rejoin=rejoin,
                    # El tráfico se mide al salir de boxes ahora; extrapolarlo a
                    # una parada futura sería inventar el estado de la pista.
                    traffic_cost_s=traffic_cost if stop_in == 0 else None,
                    notes=tuple(notes),
                    option_identity=(
                        f"set{option.set_index}"
                        if option.set_index is not None
                        else f"option{option_index}"
                    ),
                )
                if candidate is not None:
                    candidates.append(candidate)
                    evaluated += 1
        if evaluated == 0 and missing_models:
            notes.append(NOTE_NO_MODEL)

    if not candidates:
        return ()

    if baseline_key is not None:
        baseline = next((item for item in candidates if item.key == baseline_key), None)
        if baseline is None:
            return ()
    else:
        baseline = next((item for item in candidates if item.key == BASELINE_STAY_OUT), None)
        if baseline is None:
            session = inputs.state.session
            car = inputs.car
            current_lap = car.current_lap if car is not None else None
            latest = _stop_window(inputs, cfg, laps)
            if session.pit_window_ideal_lap is not None and current_lap is not None:
                target = max(0, min(latest, session.pit_window_ideal_lap - current_lap))
            elif stay is not None and stay_age is not None and stay.usable_life_laps is not None:
                safe_laps = stay.usable_life_laps - stay_age
                target = max(0, min(latest, math.floor(safe_laps)))
            else:
                target = latest
            target_candidates = [item for item in candidates if item.stop_in == target]
            if not target_candidates:
                latest_available = max(
                    (
                        candidate.stop_in
                        for candidate in candidates
                        if candidate.stop_in is not None
                    ),
                    default=0,
                )
                target_candidates = [
                    item for item in candidates if item.stop_in == latest_available
                ]
            baseline = min(
                target_candidates or candidates,
                key=lambda item: (item.remaining_time_s, item.key),
            )
    ordered = [baseline, *(item for item in candidates if item is not baseline)]

    results: list[AlternativeResult] = []
    for candidate in ordered:
        is_baseline = candidate is baseline
        results.append(
            AlternativeResult(
                key=candidate.key,
                action=candidate.action,
                remaining_time_s=candidate.remaining_time_s,
                gain_vs_baseline_s=baseline.remaining_time_s - candidate.remaining_time_s,
                quality=candidate.quality,
                pit_lap=candidate.pit_lap,
                compound=candidate.compound,
                set_index=candidate.set_index,
                projected_laps_s=candidate.projected_laps_s,
                pit_loss_s=candidate.pit_loss_s,
                traffic_cost_s=candidate.traffic_cost_s,
                rejoin_position=candidate.rejoin_position,
                traffic_risk=candidate.traffic_risk,
                # Las notas globales se añaden al final: así lo que se descubre
                # después de crear un candidato (un compuesto sin modelo, una
                # línea base inviable) también llega a todas las alternativas.
                notes=_merge_notes(
                    (NOTE_BASELINE,) if is_baseline else (),
                    candidate.notes,
                    tuple(notes),
                ),
            )
        )
    return tuple(results)


__all__ = [
    "NOTE_BASELINE",
    "NOTE_NO_MODEL",
    "NOTE_PERTURBATION",
    "NOTE_PIT_LOSS_UNAVAILABLE",
    "NOTE_STAY_OUT_NOT_VIABLE",
    "NOTE_STAY_OUT_UNAVAILABLE",
    "NOTE_TRAFFIC_COST",
    "evaluate_alternatives",
]
