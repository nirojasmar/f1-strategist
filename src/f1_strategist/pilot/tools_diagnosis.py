"""Diagnóstico determinista del ritmo: pérdida y posibles causas (Q7)."""

from __future__ import annotations

from statistics import fmean

from ..config import MetricsConfig
from ..metrics.comparability import classify_lap
from ..metrics.history import LapRecord
from ..metrics.types import Metric, Quality
from .types import FactKind, Intent, RoutedQuery, ToolFact, ToolInputs, ToolResult

# Solo estas vueltas quedan fuera de la comparación; tráfico, daño y clima son causas.
_EXCLUDED_FLAGS = frozenset(
    {
        "first_lap",
        "in_lap",
        "out_lap",
        "invalid",
        "safety_car",
        "virtual_safety_car",
        "red_flag",
        "flashback",
    }
)
_SECTORS = (
    ("sector1", "sector1_s"),
    ("sector2", "sector2_s"),
    ("sector3", "sector3_s"),
)
_DEFAULT_WINDOW = 5


def diagnose_pace(routed: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Descompone la pérdida de ritmo en posibles causas con su evidencia."""
    del routed  # La consulta ya está enrutada; el diagnóstico depende solo de las instantáneas.
    config = inputs.metrics_config or MetricsConfig()
    car_index = inputs.state.player_car_index
    if car_index is None:
        return _insufficient("player_car")

    records = tuple(
        record
        for record in inputs.history.records_for(car_index)
        if record.closed
        and record.lap_time_s is not None
        and not (_flags(record, config) & _EXCLUDED_FLAGS)
    )
    data_lap = records[-1].lap_number if records else None
    if not records:
        return _insufficient("clean_laps", data_lap=data_lap, low_sample=True)

    best_metric = inputs.metrics.get("pace.best_lap_s")
    clean_metric = inputs.metrics.get("pace.clean_mean_s")
    if clean_metric is None or clean_metric.quality in {
        Quality.INSUFFICIENT,
        Quality.UNAVAILABLE,
    }:
        return _insufficient("clean_laps", data_lap=data_lap, low_sample=True)
    if best_metric is None or best_metric.quality in {
        Quality.INSUFFICIENT,
        Quality.UNAVAILABLE,
    }:
        return _insufficient("reference_lap", data_lap=data_lap)
    reference = _metric_number(best_metric)
    if reference is None:
        return _insufficient("reference_lap", data_lap=data_lap)

    window = _window_size(clean_metric)
    recent = records[-window:]
    times = [record.lap_time_s for record in recent if record.lap_time_s is not None]
    if not times:
        return _insufficient("clean_laps", data_lap=data_lap, low_sample=True)

    quality = _worst_quality(clean_metric.quality, best_metric.quality)
    threshold = max(0.0, inputs.config.pace_trend_stable_s)
    current = fmean(times)
    loss = max(0.0, current - reference)
    stable = loss <= threshold

    facts = [
        ToolFact("pace.reference_s", reference, "s", FactKind.MEASURED, quality),
        ToolFact("pace.reference_kind", "best_lap", None, FactKind.ESTIMATED, quality),
        ToolFact("pace.current_s", current, "s", FactKind.ESTIMATED, quality),
        ToolFact("pace.loss_s", loss, "s", FactKind.ESTIMATED, quality),
        ToolFact("pace.stable", stable, None, FactKind.ESTIMATED, quality),
        ToolFact("pace.clean_laps", clean_metric.sample_count, "laps", FactKind.MEASURED, quality),
    ]

    notes: list[str] = []
    if stable:
        notes.append("pace_stable")
    if quality is Quality.LOW:
        notes.append("pace_few_clean_laps")
    if not stable:
        causes = _causes(recent, records, config, quality, threshold)
        if causes:
            notes.append("pace_possible_causes")
            for index, (code, evidence) in enumerate(causes, start=1):
                facts.append(
                    ToolFact(f"pace.cause.{index}", code, None, FactKind.ESTIMATED, quality)
                )
                facts.extend(evidence)

    return ToolResult(
        intent=Intent.PACE_DIAGNOSIS,
        quality=quality,
        facts=tuple(facts),
        data_lap=data_lap,
        stale=quality is Quality.STALE,
        notes=tuple(notes),
    )


def _flags(record: LapRecord, config: MetricsConfig) -> frozenset[str]:
    return classify_lap(record, config)


def _window_size(metric: Metric) -> int:
    size = metric.window.size
    if metric.window.kind == "laps" and isinstance(size, int) and not isinstance(size, bool):
        return max(1, size)
    return _DEFAULT_WINDOW


def _metric_number(metric: Metric | None) -> float | None:
    if metric is None or isinstance(metric.value, bool):
        return None
    if isinstance(metric.value, int | float):
        return float(metric.value)
    return None


def _worst_quality(*qualities: Quality) -> Quality:
    return max(qualities, key=lambda quality: tuple(Quality).index(quality))


def _cause_fact(
    name: str, value: float | int | str, unit: str | None, quality: Quality
) -> ToolFact:
    return ToolFact(name, value, unit, FactKind.MEASURED, quality)


def _causes(
    recent: tuple[LapRecord, ...],
    comparable: tuple[LapRecord, ...],
    config: MetricsConfig,
    quality: Quality,
    threshold: float,
) -> list[tuple[str, list[ToolFact]]]:
    causes: list[tuple[str, list[ToolFact]]] = []
    tyre = _tyre_evidence(recent, comparable, quality)
    if tyre is not None:
        causes.append(("tyre_degradation", [tyre]))
    traffic = _flag_count(recent, "traffic", config, quality)
    if traffic is not None:
        causes.append(("traffic", [traffic]))
    damage = _damage_evidence(recent, config, quality)
    if damage is not None:
        causes.append(("damage", damage))
    weather = _flag_count(recent, "weather_change", config, quality)
    if weather is not None:
        causes.append(("weather_change", [weather]))
    sector = _sector_evidence(recent, comparable, quality, threshold)
    if sector is not None:
        causes.append(("slow_sector", sector))
    return causes


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _tyre_evidence(
    recent: tuple[LapRecord, ...],
    comparable: tuple[LapRecord, ...],
    quality: Quality,
) -> ToolFact | None:
    earlier = comparable[: len(comparable) - len(recent)]
    recent_wear = [fmean(record.tyre_wear_end_pct) for record in recent if record.tyre_wear_end_pct]
    earlier_wear = [
        fmean(record.tyre_wear_end_pct) for record in earlier if record.tyre_wear_end_pct
    ]
    wear_now, wear_before = _mean(recent_wear), _mean(earlier_wear)
    if wear_now is not None and wear_before is not None and wear_now > wear_before:
        return _cause_fact(
            "pace.evidence.tyre_wear_delta_pct",
            wear_now - wear_before,
            "pct",
            quality,
        )

    ages_now = [float(record.tyre_age_end) for record in recent if record.tyre_age_end]
    ages_before = [float(record.tyre_age_end) for record in earlier if record.tyre_age_end]
    age_now, age_before = _mean(ages_now), _mean(ages_before)
    if age_now is not None and age_before is not None and age_now - age_before >= 1.0:
        return _cause_fact(
            "pace.evidence.tyre_age_delta_laps", age_now - age_before, "laps", quality
        )
    return None


def _flag_count(
    recent: tuple[LapRecord, ...], flag: str, config: MetricsConfig, quality: Quality
) -> ToolFact | None:
    count = sum(1 for record in recent if flag in _flags(record, config))
    if count == 0:
        return None
    return _cause_fact(f"pace.evidence.{flag}_laps", count, "laps", quality)


def _damage_evidence(
    recent: tuple[LapRecord, ...], config: MetricsConfig, quality: Quality
) -> list[ToolFact] | None:
    damaged = [record for record in recent if "damage_change" in _flags(record, config)]
    if not damaged:
        return None
    deltas = [
        record.damage_max_delta_pct for record in damaged if record.damage_max_delta_pct is not None
    ]
    evidence = [_cause_fact("pace.evidence.damage_laps", len(damaged), "laps", quality)]
    if deltas:
        evidence.append(_cause_fact("pace.evidence.damage_delta_pct", max(deltas), "pct", quality))
    return evidence


def _sector_evidence(
    recent: tuple[LapRecord, ...],
    comparable: tuple[LapRecord, ...],
    quality: Quality,
    threshold: float,
) -> list[ToolFact] | None:
    deltas: dict[str, float] = {}
    for name, attribute in _SECTORS:
        best = [
            float(value)
            for record in comparable
            if (value := getattr(record, attribute)) is not None and value > 0
        ]
        values = [
            float(value)
            for record in recent
            if (value := getattr(record, attribute)) is not None and value > 0
        ]
        if not best or not values:
            continue
        delta = fmean(values) - min(best)
        if delta > threshold:
            deltas[name] = delta
    if not deltas:
        return None
    name = max(deltas, key=lambda key: deltas[key])
    return [
        _cause_fact("pace.evidence.sector", name, None, quality),
        _cause_fact("pace.evidence.sector_loss_s", deltas[name], "s", quality),
    ]


def _insufficient(
    missing: str, *, data_lap: int | None = None, low_sample: bool = False
) -> ToolResult:
    return ToolResult(
        intent=Intent.PACE_DIAGNOSIS,
        quality=Quality.INSUFFICIENT,
        missing=(missing,),
        data_lap=data_lap,
        notes=("pace_few_clean_laps",) if low_sample else (),
    )
