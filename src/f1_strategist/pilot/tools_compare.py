"""Herramientas de comparación con un rival y estado de intervalos (Q6)."""

from __future__ import annotations

from ..config import MetricsConfig
from ..metrics.comparability import classify_lap, is_clean
from ..metrics.gap import CarComparison, compare_car
from ..metrics.history import MetricInputs, current_stint_bounds
from ..metrics.types import Quality
from .types import FactKind, Intent, RoutedQuery, ToolFact, ToolInputs, ToolResult

# Dos vueltas limpias por coche bastan para una media de ritmo representativa.
_MIN_CLEAN_LAPS = 2
_CLEAN_PACE_WINDOW = 3
_GAP_FACTS = (
    ("gap.ahead_s", FactKind.MEASURED),
    ("gap.behind_s", FactKind.MEASURED),
    ("gap.leader_s", FactKind.MEASURED),
    ("gap.ahead_trend_s_per_s", FactKind.ESTIMATED),
    ("gap.behind_trend_s_per_s", FactKind.ESTIMATED),
)


def _metric_inputs(inputs: ToolInputs) -> MetricInputs:
    return MetricInputs(
        inputs.state,
        inputs.history,
        inputs.metrics_config or MetricsConfig(),
        inputs.now,
        inputs.state.player_car_index or 0,
    )


_UNRESOLVED_NOTES = {
    "driver_retired": "pilot.driver_retired",
    "driver_not_found": "pilot.driver_unknown",
    "empty_reference": "pilot.driver_unknown",
    "no_teammate": "pilot.driver_unknown",
    "no_player": "pilot.driver_unknown",
    "clarification_no_match": "pilot.driver_unknown",
    "clarification_ambiguous": "pilot.driver_unknown",
    "no_car_ahead": "pilot.no_car_ahead",
    "no_car_behind": "pilot.no_car_behind",
    "no_leader": "pilot.driver_unknown",
    "no_last": "pilot.driver_unknown",
}


def unresolved_reference_note(routed: RoutedQuery) -> str | None:
    """Traduce una referencia no resuelta a la plantilla que debe responder."""
    for entity in routed.entities:
        if entity.car_index is None and entity.reason is not None:
            return _UNRESOLVED_NOTES.get(entity.reason, "pilot.driver_unknown")
    return None


def _rival_index(routed: RoutedQuery, player_index: int) -> int | None:
    if routed.clarification is not None:
        return None
    for entity in routed.entities:
        if entity.car_index is not None and entity.car_index != player_index:
            return entity.car_index
    return None


def _clean_lap_times(metric_inputs: MetricInputs, car_index: int) -> tuple[float, ...]:
    bounds = current_stint_bounds(metric_inputs, car_index)
    if bounds is None:
        return ()
    start_lap, end_lap = bounds
    car = next(
        (item for item in metric_inputs.state.cars if item.car_index == car_index),
        None,
    )
    current_lap = car.current_lap if car is not None else None
    times: list[float] = []
    for record in metric_inputs.history.records_for(car_index):
        if not record.closed or record.lap_time_s is None:
            continue
        if record.lap_number < start_lap:
            continue
        if end_lap is not None and record.lap_number > end_lap:
            continue
        if current_lap is not None and record.lap_number > current_lap:
            continue
        if record.valid is False or not is_clean(classify_lap(record, metric_inputs.config)):
            continue
        times.append(record.lap_time_s)
    return tuple(times[-_CLEAN_PACE_WINDOW:])


def _sufficient_pace(
    metric_inputs: MetricInputs,
    comparison: CarComparison,
    rival_index: int,
) -> float | None:
    if comparison.pace_delta_s is None:
        return None
    player_laps = _clean_lap_times(metric_inputs, metric_inputs.car_index)
    rival_laps = _clean_lap_times(metric_inputs, rival_index)
    if len(player_laps) < _MIN_CLEAN_LAPS or len(rival_laps) < _MIN_CLEAN_LAPS:
        return None
    return comparison.pace_delta_s


def _fact(
    name: str,
    value: float | int | str | bool,
    unit: str | None,
    kind: FactKind,
) -> ToolFact:
    return ToolFact(name, value, unit, kind, Quality.OK)


def compare_driver(routed: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Compara al piloto con el rival indicado mediante la métrica de Fase 2."""
    metric_inputs = _metric_inputs(inputs)
    data_lap = inputs.state.current_lap
    note = unresolved_reference_note(routed)
    if note is not None:
        return ToolResult(
            Intent.COMPARE_DRIVER,
            Quality.INSUFFICIENT,
            missing=("compare.rival",),
            data_lap=data_lap,
            notes=(note,),
        )
    rival_index = _rival_index(routed, metric_inputs.car_index)
    if rival_index is None:
        return ToolResult(
            Intent.COMPARE_DRIVER,
            Quality.INSUFFICIENT,
            missing=("compare.rival",),
            data_lap=data_lap,
        )

    comparison = compare_car(metric_inputs, rival_index)
    rival = next(
        (item for item in inputs.state.cars if item.car_index == rival_index),
        None,
    )
    facts: list[ToolFact] = []
    missing: list[str] = []

    if rival is not None and rival.name:
        facts.append(_fact("compare.rival.name", rival.name, None, FactKind.MEASURED))
    else:
        missing.append("compare.rival.name")
    if rival is not None and rival.position is not None:
        facts.append(_fact("compare.rival.position", rival.position, None, FactKind.MEASURED))
    else:
        missing.append("compare.rival.position")

    if comparison.interval_s is not None:
        facts.append(
            _fact("compare.interval_s", abs(comparison.interval_s), "s", FactKind.MEASURED)
        )
        if comparison.interval_direction is not None:
            facts.append(
                _fact(
                    "compare.interval_direction",
                    comparison.interval_direction,
                    None,
                    FactKind.MEASURED,
                )
            )
        else:
            missing.append("compare.interval_direction")
    else:
        missing.extend(("compare.interval_s", "compare.interval_direction"))

    pace_delta = _sufficient_pace(metric_inputs, comparison, rival_index)
    if pace_delta is not None:
        facts.append(_fact("compare.pace_delta_s", pace_delta, "s/lap", FactKind.ESTIMATED))
    else:
        missing.append("compare.pace_delta_s")

    for name, value, unit in (
        ("compare.rival.compound", comparison.compound, None),
        ("compare.rival.tyre_age_laps", comparison.tyre_age_laps, "laps"),
        ("compare.rival.pit_stops", comparison.stops_done, "stops"),
    ):
        if value is not None:
            facts.append(_fact(name, value, unit, FactKind.MEASURED))
        else:
            missing.append(name)

    retired = "rival_retired" in comparison.notes
    if retired:
        facts.append(_fact("compare.rival.retired", True, None, FactKind.MEASURED))

    if retired:
        quality = Quality.UNAVAILABLE
    elif comparison.quality is Quality.STALE:
        quality = Quality.STALE
    elif comparison.interval_s is None:
        quality = Quality.INSUFFICIENT
    elif missing:
        quality = Quality.LOW
    else:
        quality = Quality.OK
    return ToolResult(
        Intent.COMPARE_DRIVER,
        quality,
        tuple(facts),
        tuple(missing),
        stale=quality is Quality.STALE,
        data_lap=data_lap,
        notes=comparison.notes,
    )


def gap_status(routed: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Publica los intervalos del piloto a partir de las métricas ya calculadas."""
    note = unresolved_reference_note(routed)
    if note is not None:
        return ToolResult(
            Intent.GAP_STATUS,
            Quality.INSUFFICIENT,
            missing=("gap.rival",),
            data_lap=inputs.state.current_lap,
            notes=(note,),
        )
    facts: list[ToolFact] = []
    missing: list[str] = []
    stale = False
    for key, kind in _GAP_FACTS:
        metric = inputs.metrics.get(key)
        if metric is None or metric.value is None or isinstance(metric.value, tuple):
            missing.append(key)
            continue
        if metric.quality is Quality.STALE:
            stale = True
        facts.append(ToolFact(key, metric.value, metric.unit, kind, metric.quality))
    if not facts:
        quality = Quality.UNAVAILABLE
    elif stale:
        quality = Quality.STALE
    else:
        quality = Quality.OK
    return ToolResult(
        Intent.GAP_STATUS,
        quality,
        tuple(facts),
        tuple(missing),
        stale=stale,
        data_lap=inputs.state.current_lap,
    )
