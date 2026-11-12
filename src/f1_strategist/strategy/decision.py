"""Sensibilidad, regla de decisión y confianza (S6).

El módulo es puro y determinista: recibe la función que evalúa alternativas
(S5), el resumen de calidad de las entradas y la configuración, y devuelve una
decisión inmutable. No usa reloj, no hace E/S y no evalúa alternativas por su
cuenta: solo las repite con perturbaciones de banda.

La evaluación se repite con la pendiente de desgaste escalada
``1 ± wear_slope_band_pct`` y con la pérdida en boxes desplazada
``± pit_loss_band_s``. El mínimo y el máximo de la ganancia de cada alternativa
forman su intervalo; una alternativa que desaparece en alguna perturbación es
inestable y no se recomienda.

La regla es deliberadamente conservadora: solo se recomienda parar cuando el
límite **inferior** del intervalo de ganancia supera ``min_gain_s``. En
cualquier otro caso la respuesta es ``NO_CHANGE``, que es una respuesta válida
y no un fallo.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

from ..config import StrategyConfig
from ..metrics.types import Quality
from .types import (
    BASELINE_STAY_OUT,
    AlternativeResult,
    DataQuality,
    Perturbation,
    StrategyAction,
    StrategyDecision,
)

NOTE_NO_ALTERNATIVES = "decision_without_alternatives"
NOTE_DEFAULT_NO_CHANGE = "decision_default_no_change"
NOTE_NOT_A_RACE = "session_is_not_a_race"
NOTE_NO_LAPS_REMAINING = "laps_remaining_unavailable"
NOTE_PACE_INSUFFICIENT = "pace_quality_insufficient"
NOTE_PIT_LOSS_INSUFFICIENT = "pit_loss_unavailable"
NOTE_STALE_DATA = "data_stale"
NOTE_ALTERNATIVE_UNSTABLE = "alternative_unstable"

#: Claves de ``sensitive_to``: supuesto que, dentro de su banda, cambia la decisión.
SENSITIVE_WEAR_SLOPE = "wear_slope"
SENSITIVE_PIT_LOSS = "pit_loss_s"

#: Confianza asociada a cada calidad de entrada.
_QUALITY_BASE: dict[Quality, float] = {
    Quality.OK: 1.0,
    Quality.LOW: 0.7,
    Quality.STALE: 0.4,
    Quality.INSUFFICIENT: 0.1,
    Quality.UNAVAILABLE: 0.0,
}

#: Muestras del modelo de ritmo a partir de las cuales la confianza no sube más.
_FULL_SAMPLES = 10

#: Calidades que impiden decidir con rigor.
_BLOCKING_QUALITIES = frozenset({Quality.INSUFFICIENT, Quality.UNAVAILABLE})

#: Escala mínima de pendiente admitida al perturbar (``slope_scale`` debe ser > 0).
_MIN_SLOPE_SCALE = 1e-3

Evaluator = Callable[[Perturbation], tuple[AlternativeResult, ...]]


def _scenarios(cfg: StrategyConfig) -> dict[str, Perturbation]:
    """Perturbaciones de banda: neutral, cada factor por separado y esquinas."""
    band = cfg.wear_slope_band_pct / 100.0
    up = max(_MIN_SLOPE_SCALE, 1.0 + band)
    down = max(_MIN_SLOPE_SCALE, 1.0 - band)
    delta = cfg.pit_loss_band_s
    return {
        "neutral": Perturbation(),
        "slope_up": Perturbation(slope_scale=up),
        "slope_down": Perturbation(slope_scale=down),
        "pit_up": Perturbation(pit_loss_delta_s=delta),
        "pit_down": Perturbation(pit_loss_delta_s=-delta),
        "corner_up_up": Perturbation(slope_scale=up, pit_loss_delta_s=delta),
        "corner_up_down": Perturbation(slope_scale=up, pit_loss_delta_s=-delta),
        "corner_down_up": Perturbation(slope_scale=down, pit_loss_delta_s=delta),
        "corner_down_down": Perturbation(slope_scale=down, pit_loss_delta_s=-delta),
    }


def _bounds(
    runs: Mapping[str, tuple[AlternativeResult, ...]],
) -> tuple[dict[str, float], dict[str, float], frozenset[str]]:
    """Intervalo de ganancia por alternativa a partir de todas las pasadas."""
    lower: dict[str, float] = {}
    upper: dict[str, float] = {}
    unstable: set[str] = set()
    keys = {item.key for item in runs["neutral"]}
    for key in keys:
        values: list[float] = []
        for name, results in runs.items():
            found = next((item for item in results if item.key == key), None)
            if found is None:
                # La alternativa deja de existir bajo esta perturbación: no se
                # puede prometer su ganancia y se marca como inestable.
                if name != "neutral":
                    unstable.add(key)
                continue
            values.append(found.gain_vs_baseline_s)
        if values:
            lower[key] = min(values)
            upper[key] = max(values)
    return lower, upper, frozenset(unstable)


def _baseline_key(results: tuple[AlternativeResult, ...]) -> str | None:
    """Clave de la línea base: ``stay_out`` si existe, si no la primera."""
    for item in results:
        if item.key == BASELINE_STAY_OUT:
            return item.key
    return results[0].key if results else None


def _best_stop(
    results: tuple[AlternativeResult, ...],
    lower: Mapping[str, float],
    unstable: frozenset[str],
    cfg: StrategyConfig,
) -> AlternativeResult | None:
    """Alternativa de parada con el límite inferior por encima del mínimo."""
    eligible = [
        item
        for item in results
        if item.action in {StrategyAction.PIT_NOW, StrategyAction.PIT_IN}
        and item.key not in unstable
        and lower.get(item.key, -math.inf) > cfg.min_gain_s
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: (lower[item.key], item.gain_vs_baseline_s, item.key),
    )


def _signature(
    results: tuple[AlternativeResult, ...],
    lower: Mapping[str, float],
    cfg: StrategyConfig,
) -> tuple[StrategyAction, str | None]:
    """Acción y clave que produciría la regla con estas alternativas."""
    chosen = _best_stop(results, lower, frozenset(), cfg)
    if chosen is not None:
        return chosen.action, chosen.key
    return StrategyAction.NO_CHANGE, _baseline_key(results)


def _self_lower(results: tuple[AlternativeResult, ...]) -> dict[str, float]:
    """Límite inferior de una pasada aislada: su propia ganancia."""
    return {item.key: item.gain_vs_baseline_s for item in results}


def _sensitive_to(
    runs: Mapping[str, tuple[AlternativeResult, ...]],
    cfg: StrategyConfig,
) -> tuple[str, ...]:
    """Supuestos cuya variación dentro de banda cambia la decisión."""
    base = _signature(runs["neutral"], _self_lower(runs["neutral"]), cfg)
    keys: list[str] = []
    if any(
        _signature(runs[name], _self_lower(runs[name]), cfg) != base
        for name in ("slope_up", "slope_down")
    ):
        keys.append(SENSITIVE_WEAR_SLOPE)
    if any(
        _signature(runs[name], _self_lower(runs[name]), cfg) != base
        for name in ("pit_up", "pit_down")
    ):
        keys.append(SENSITIVE_PIT_LOSS)
    return tuple(keys)


def _confidence(quality: DataQuality, width: float, cfg: StrategyConfig) -> float:
    """Confianza 0-1: calidad, muestras, peso del prior y amplitud del intervalo."""
    base = min(_QUALITY_BASE[quality.pace], _QUALITY_BASE[quality.pit_loss])
    samples = min(1.0, quality.pace_samples / _FULL_SAMPLES)
    data_factor = 0.6 + 0.4 * samples
    prior_factor = 1.0 - 0.5 * quality.prior_weight
    scale = max(cfg.min_gain_s, 1.0)
    width_factor = scale / (scale + max(0.0, width))
    value = base * data_factor * prior_factor * width_factor
    if quality.stale:
        value *= 0.5
    return min(1.0, max(0.0, value))


def _insufficient_notes(quality: DataQuality, cfg: StrategyConfig) -> tuple[str, ...]:
    """Motivos por los que no se puede decidir con las entradas actuales."""
    del cfg  # la configuración no cambia los motivos de insuficiencia
    notes: list[str] = []
    if not quality.session_is_race:
        notes.append(NOTE_NOT_A_RACE)
    if quality.laps_remaining is None or quality.laps_remaining <= 0.0:
        notes.append(NOTE_NO_LAPS_REMAINING)
    if quality.pace in _BLOCKING_QUALITIES:
        notes.append(NOTE_PACE_INSUFFICIENT)
    if quality.pit_loss in _BLOCKING_QUALITIES:
        notes.append(NOTE_PIT_LOSS_INSUFFICIENT)
    if quality.stale:
        notes.append(NOTE_STALE_DATA)
    return tuple(notes)


def decide(
    evaluate: Evaluator,
    quality: DataQuality,
    cfg: StrategyConfig,
) -> StrategyDecision:
    """Convierte las alternativas en una decisión con intervalo y confianza.

    Se evalúan la pasada neutral y las perturbaciones de banda; el intervalo de
    ganancia de cada alternativa es el mínimo y el máximo observados. Se
    recomienda parar solo si el límite inferior supera ``min_gain_s``; si
    faltan entradas críticas la acción es ``INSUFFICIENT_DATA`` y, en cualquier
    otro caso, ``NO_CHANGE``.
    """
    scenarios = _scenarios(cfg)
    runs = {name: evaluate(perturbation) for name, perturbation in scenarios.items()}
    neutral = runs["neutral"]
    if not neutral:
        return StrategyDecision(
            action=StrategyAction.INSUFFICIENT_DATA,
            confidence=0.0,
            notes=(NOTE_NO_ALTERNATIVES,),
        )

    insufficient = _insufficient_notes(quality, cfg)
    if insufficient:
        return StrategyDecision(
            action=StrategyAction.INSUFFICIENT_DATA,
            confidence=0.0,
            notes=insufficient,
        )

    lower, upper, unstable = _bounds(runs)
    sensitive_to = _sensitive_to(runs, cfg)
    chosen = _best_stop(neutral, lower, unstable, cfg)

    if chosen is None:
        key = _baseline_key(neutral)
        # La incertidumbre de un «no cambiar» es la de la alternativa que casi
        # ganó: si su intervalo es ancho, la confianza debe bajar.
        width = max((upper[item] - lower[item] for item in upper), default=0.0)
        notes = [NOTE_DEFAULT_NO_CHANGE]
        if BASELINE_STAY_OUT not in {item.key for item in neutral}:
            notes.append(NOTE_ALTERNATIVE_UNSTABLE)
        return StrategyDecision(
            action=StrategyAction.NO_CHANGE,
            key=key,
            confidence=_confidence(quality, width, cfg),
            sensitive_to=sensitive_to,
            notes=tuple(notes),
        )

    wide = upper.get(chosen.key, chosen.gain_vs_baseline_s) - lower.get(
        chosen.key, chosen.gain_vs_baseline_s
    )
    return StrategyDecision(
        action=chosen.action,
        key=chosen.key,
        pit_lap=chosen.pit_lap,
        compound=chosen.compound,
        gain_vs_baseline_s=chosen.gain_vs_baseline_s,
        gain_interval_s=(
            lower.get(chosen.key, chosen.gain_vs_baseline_s),
            upper.get(chosen.key, chosen.gain_vs_baseline_s),
        ),
        confidence=_confidence(quality, wide, cfg),
        sensitive_to=sensitive_to,
    )


__all__ = [
    "NOTE_ALTERNATIVE_UNSTABLE",
    "NOTE_DEFAULT_NO_CHANGE",
    "NOTE_NO_ALTERNATIVES",
    "NOTE_NO_LAPS_REMAINING",
    "NOTE_NOT_A_RACE",
    "NOTE_PACE_INSUFFICIENT",
    "NOTE_PIT_LOSS_INSUFFICIENT",
    "NOTE_STALE_DATA",
    "SENSITIVE_PIT_LOSS",
    "SENSITIVE_WEAR_SLOPE",
    "decide",
]
