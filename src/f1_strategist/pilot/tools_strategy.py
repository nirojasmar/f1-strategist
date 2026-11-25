"""Herramientas de estrategia de las preguntas del piloto (Q8).

``ask_strategy`` traduce la evaluación publicada por el motor y
``simulate_strategy`` responde a una parada hipotética: usa la alternativa
publicada o la recalcula bajo demanda con ``evaluate_once``.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from ..metrics.history import MetricInputs
from ..metrics.types import Quality
from ..strategy import evaluate_once
from ..strategy.decision import NOTE_NO_ALTERNATIVES
from ..strategy.types import (
    StrategyAction,
    StrategyAlternative,
    StrategyEvaluation,
)
from .types import FactKind, Intent, RoutedQuery, ToolFact, ToolInputs, ToolResult

NOTE_EVALUATION_MISSING = "strategy_evaluation_missing"
NOTE_EVALUATION_FAILED = "strategy_evaluation_failed"
NOTE_COMPOUND_RULE = "strategy_compound_rule"
NOTE_COMPOUND_UNAVAILABLE = "strategy_compound_unavailable"
NOTE_LAP_OUT_OF_WINDOW = "strategy_pit_lap_out_of_window"
NOTE_SIMULATED = "strategy_simulated"
NOTE_SIMULATION_FAILED = "strategy_simulation_failed"
NOTE_CONFIG_MISSING = "strategy_configuration_missing"

_SAFETY_CAR_STATUSES = {
    "full_safety_car",
    "safety_car",
    "virtual_safety_car",
    "formation_lap",
}

_LOGGER = logging.getLogger(__name__)

_COMPOUND_PATTERNS = (
    (re.compile(r"\b(?:blandos?|softs?)\b"), "soft"),
    (re.compile(r"\b(?:medios?|mediums?)\b"), "medium"),
    (re.compile(r"\b(?:duros?|hards?)\b"), "hard"),
)
_PIT_IN_PATTERN = re.compile(r"\b(?:en|dentro de)\s+(\d+)\s+vueltas?\b")
_NOW_PATTERN = re.compile(r"\b(?:ahora|now)\b")
_SIMULATION_PATTERN = re.compile(r"\bsi\s+(?:paro|paramos|entro|entramos|entra)\b")


def ask_strategy(query: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Traduce la recomendación publicada sin recalcularla."""
    evaluation = inputs.strategy.evaluation
    if evaluation is None:
        note = NOTE_EVALUATION_FAILED if inputs.strategy.error else NOTE_EVALUATION_MISSING
        return ToolResult(
            intent=Intent.ASK_STRATEGY,
            quality=Quality.UNAVAILABLE,
            missing=("strategy_evaluation",),
            notes=(note,),
        )

    recommendation = evaluation.recommendation
    action = recommendation.action
    quality = evaluation.data_quality
    if action is StrategyAction.INSUFFICIENT_DATA and quality is Quality.OK:
        quality = Quality.INSUFFICIENT

    facts = [
        _fact("strategy_action", action.value, None, FactKind.RECOMMENDED, quality),
    ]
    if action is not StrategyAction.INSUFFICIENT_DATA:
        _append(
            facts,
            "strategy_pit_lap",
            recommendation.pit_lap,
            "lap",
            FactKind.RECOMMENDED,
            quality,
        )
        _append(
            facts,
            "strategy_compound",
            recommendation.compound,
            None,
            FactKind.RECOMMENDED,
            quality,
        )
        _append(
            facts,
            "strategy_set_index",
            recommendation.set_index,
            None,
            FactKind.RECOMMENDED,
            quality,
        )
        _append(
            facts,
            "strategy_gain_s",
            recommendation.gain_vs_baseline_s,
            "s",
            FactKind.ESTIMATED,
            quality,
        )
        if recommendation.gain_interval_s is not None:
            low, high = recommendation.gain_interval_s
            _append(facts, "strategy_gain_low_s", low, "s", FactKind.ESTIMATED, quality)
            _append(facts, "strategy_gain_high_s", high, "s", FactKind.ESTIMATED, quality)
        _append(
            facts,
            "strategy_confidence",
            recommendation.confidence,
            None,
            FactKind.ESTIMATED,
            quality,
        )

    facts.append(
        _fact(
            "strategy_safety_car",
            _safety_car_active(inputs),
            None,
            FactKind.MEASURED,
            Quality.OK,
        )
    )

    return ToolResult(
        intent=Intent.ASK_STRATEGY,
        quality=quality,
        facts=tuple(facts),
        stale=_is_stale(evaluation, inputs),
        data_lap=evaluation.lap,
        notes=_context_notes(evaluation),
    )


def simulate_strategy(query: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Responde «¿y si paro ahora / en N vueltas?» buscando o evaluando la alternativa."""
    evaluation = inputs.strategy.evaluation
    if evaluation is None:
        note = NOTE_EVALUATION_FAILED if inputs.strategy.error else NOTE_EVALUATION_MISSING
        return ToolResult(
            intent=Intent.ASK_STRATEGY,
            quality=Quality.UNAVAILABLE,
            missing=("strategy_evaluation",),
            notes=(note,),
        )

    text = query.normalized_text or query.query.text
    pit_in_laps = _pit_in_laps(text)
    compound = _requested_compound(text)
    alternatives = evaluation.alternatives
    candidates = tuple(item for item in alternatives if _matches(item, pit_in_laps, compound))
    if not candidates and _needs_recompute(alternatives, pit_in_laps):
        # La evaluación publicada no cubre la ventana pedida; se evalúa bajo demanda.
        if inputs.metrics_config is None or inputs.strategy_config is None:
            return ToolResult(
                intent=Intent.ASK_STRATEGY,
                quality=Quality.UNAVAILABLE,
                missing=("strategy_configuration",),
                stale=_is_stale(evaluation, inputs),
                data_lap=evaluation.lap,
                notes=(*_context_notes(evaluation), NOTE_CONFIG_MISSING),
            )
        simulated = _evaluate_on_demand(inputs, pit_in_laps)
        if simulated is not None:
            evaluation = simulated
            alternatives = simulated.alternatives
            candidates = tuple(
                item for item in alternatives if _matches(item, pit_in_laps, compound)
            )
    if not candidates:
        notes = list(_context_notes(evaluation))
        notes.append(_unavailable_reason(evaluation, pit_in_laps, compound, alternatives))
        return ToolResult(
            intent=Intent.ASK_STRATEGY,
            quality=Quality.UNAVAILABLE,
            missing=("strategy_alternative",),
            stale=_is_stale(evaluation, inputs),
            data_lap=evaluation.lap,
            notes=tuple(notes),
        )

    best = max(candidates, key=lambda item: item.gain_vs_baseline_s)
    action = StrategyAction.PIT_NOW if pit_in_laps == 0 else StrategyAction.PIT_IN
    quality = best.quality
    facts = [
        _fact("strategy_simulated", True, None, FactKind.MEASURED, Quality.OK),
        _fact("strategy_action", action.value, None, FactKind.ESTIMATED, quality),
        _fact(
            "strategy_pit_lap",
            evaluation.lap + pit_in_laps,
            "lap",
            FactKind.ESTIMATED,
            quality,
        ),
        _fact("strategy_gain_s", best.gain_vs_baseline_s, "s", FactKind.ESTIMATED, quality),
        _fact(
            "strategy_remaining_time_s",
            best.remaining_time_s,
            "s",
            FactKind.ESTIMATED,
            quality,
        ),
    ]
    _append(
        facts, "strategy_compound", _compound_from_key(best.key), None, FactKind.ESTIMATED, quality
    )
    _append(facts, "strategy_set_index", best.set_index, None, FactKind.ESTIMATED, quality)
    _append(
        facts,
        "strategy_rejoin_position",
        best.rejoin_position,
        None,
        FactKind.ESTIMATED,
        quality,
    )
    _append(facts, "strategy_traffic_risk", best.traffic_risk, None, FactKind.ESTIMATED, quality)

    notes = (*_context_notes(evaluation), NOTE_SIMULATED)
    return ToolResult(
        intent=Intent.ASK_STRATEGY,
        quality=quality,
        facts=tuple(facts),
        stale=_is_stale(evaluation, inputs),
        data_lap=evaluation.lap,
        notes=notes,
    )


def ask_strategy_or_simulate(query: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Despacha la pregunta de estrategia a la simulación hipotética o a la evaluación."""
    text = query.normalized_text or query.query.text
    if _SIMULATION_PATTERN.search(_normalize(text)):
        return simulate_strategy(query, inputs)
    return ask_strategy(query, inputs)


def _evaluate_on_demand(
    inputs: ToolInputs,
    pit_in_laps: int,
) -> StrategyEvaluation | None:
    """Recalcula la estrategia con la configuración real y la ventana pedida."""
    car_index = inputs.state.player_car_index
    metrics_cfg = inputs.metrics_config
    strategy_cfg = inputs.strategy_config
    if car_index is None or metrics_cfg is None or strategy_cfg is None:
        return None
    if pit_in_laps > strategy_cfg.max_pit_in_laps:
        # evaluate_once limita las alternativas a max_pit_in_laps; se amplía para
        # que la parada pedida exista de verdad. El fin de carrera sigue mandando.
        strategy_cfg = strategy_cfg.model_copy(update={"max_pit_in_laps": pit_in_laps})
    metric_inputs = MetricInputs(
        inputs.state,
        inputs.history,
        metrics_cfg,
        inputs.now,
        car_index,
        inputs.profile,
    )
    try:
        return evaluate_once(metric_inputs, strategy_cfg)
    except Exception:  # noqa: BLE001 - una simulación no debe tumbar la respuesta
        _LOGGER.exception("pilot_strategy_simulation_failed")
        return None


def _needs_recompute(alternatives: tuple[StrategyAlternative, ...], pit_in_laps: int) -> bool:
    """Solo recalcula si falta la ventana de parada, no si falta un compuesto concreto."""
    if not alternatives:
        return True
    published = 0
    for item in alternatives:
        if not item.key.startswith("pit_in_"):
            continue
        try:
            published = max(published, int(item.key.split("_", 2)[1].split(":", 1)[0]))
        except (IndexError, ValueError):
            continue
    return pit_in_laps > published


def _matches(item: StrategyAlternative, pit_in_laps: int, compound: str | None) -> bool:
    prefix = "pit_now:" if pit_in_laps == 0 else f"pit_in_{pit_in_laps}:"
    if not item.key.startswith(prefix):
        return False
    return compound is None or item.key.split(":")[1] == compound


def _unavailable_reason(
    evaluation: StrategyEvaluation,
    pit_in_laps: int,
    compound: str | None,
    alternatives: tuple[StrategyAlternative, ...],
) -> str:
    available = {item.key.split(":")[1] for item in alternatives if ":" in item.key}
    if compound is not None and compound in available:
        return NOTE_LAP_OUT_OF_WINDOW
    if compound is not None and compound == _normalized_compound(evaluation.forecast_compound):
        return NOTE_COMPOUND_RULE
    if compound is not None and compound not in available:
        return NOTE_COMPOUND_UNAVAILABLE
    if not alternatives and NOTE_NO_ALTERNATIVES in evaluation.recommendation.notes:
        return NOTE_COMPOUND_UNAVAILABLE
    return NOTE_LAP_OUT_OF_WINDOW


def _context_notes(evaluation: StrategyEvaluation) -> tuple[str, ...]:
    notes = list(evaluation.recommendation.notes)
    notes.extend(evaluation.assumptions)
    notes.extend(evaluation.risks)
    notes.extend(evaluation.sensitive_to)
    return tuple(dict.fromkeys(notes))


def _fact(
    name: str,
    value: float | int | str | bool | None,
    unit: str | None,
    kind: FactKind,
    quality: Quality,
) -> ToolFact:
    return ToolFact(name=name, value=value, unit=unit, kind=kind, quality=quality)


def _append(
    facts: list[ToolFact],
    name: str,
    value: float | int | str | bool | None,
    unit: str | None,
    kind: FactKind,
    quality: Quality,
) -> None:
    if value is not None:
        facts.append(_fact(name, value, unit, kind, quality))


def _pit_in_laps(text: str) -> int:
    normalized = _normalize(text)
    match = _PIT_IN_PATTERN.search(normalized)
    if match is not None:
        return int(match.group(1))
    return 0


def _requested_compound(text: str) -> str | None:
    normalized = _normalize(text)
    for pattern, compound in _COMPOUND_PATTERNS:
        if pattern.search(normalized):
            return compound
    return None


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _normalized_compound(compound: str | None) -> str | None:
    if compound is None:
        return None
    normalized = _normalize(compound).strip()
    return normalized or None


def _compound_from_key(key: str) -> str | None:
    parts = key.split(":")
    return parts[1] if len(parts) > 1 else None


def _is_stale(evaluation: StrategyEvaluation, inputs: ToolInputs) -> bool:
    uid = inputs.state.session.uid
    return evaluation.session_uid != (str(uid) if uid is not None else "unknown")


def _safety_car_active(inputs: ToolInputs) -> bool:
    return inputs.state.session.safety_car_status in _SAFETY_CAR_STATUSES


__all__ = [
    "NOTE_COMPOUND_RULE",
    "NOTE_COMPOUND_UNAVAILABLE",
    "NOTE_CONFIG_MISSING",
    "NOTE_EVALUATION_FAILED",
    "NOTE_EVALUATION_MISSING",
    "NOTE_LAP_OUT_OF_WINDOW",
    "NOTE_SIMULATED",
    "NOTE_SIMULATION_FAILED",
    "ask_strategy",
    "ask_strategy_or_simulate",
    "simulate_strategy",
]
