"""Herramienta de undercut (Fase 7, Q9).

Estima si merece la pena parar ahora para adelantar al coche de delante (o a un
rival indicado) mientras este sigue en pista ``k`` vueltas. Reutiliza los
modelos de ritmo, la pérdida en boxes y la reincorporación de la Fase 4.

El cálculo es puro y determinista. Los supuestos viajan en el resultado y ningún
dato ausente se sustituye por cero: si falta un modelo, la edad del rival, sus
vueltas limpias o la pérdida en boxes, el resultado queda insuficiente.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from ..config import MetricsConfig, PilotConfig, StrategyConfig
from ..metrics.comparability import classify_lap, is_clean
from ..metrics.history import MetricInputs, current_stint_bounds
from ..metrics.types import Quality
from ..race_state import CarSnapshot, RaceStateSnapshot
from ..strategy.pace_model import (
    CompoundModel,
    CompoundModels,
    build_compound_models,
    canonical_compound,
    project_lap_times,
)
from ..strategy.pit_model import RejoinEstimate, estimate_pit_loss, estimate_rejoin
from ..strategy.types import ASSUMPTION_RIVALS_DO_NOT_PIT, StrategySnapshot
from .types import FactKind, Intent, RoutedQuery, ToolFact, ToolInputs, ToolResult

MISSING_PILOT_LEADS = "pilot_leads"
MISSING_PLAYER_UNAVAILABLE = "player_unavailable"
MISSING_PLAYER_POSITION = "player_position"
MISSING_RIVAL_UNAVAILABLE = "rival_unavailable"
MISSING_RIVAL_CLEAN_LAPS = "rival_clean_laps"
MISSING_PLAYER_MODEL = "player_model"
MISSING_RIVAL_MODEL = "rival_model"
MISSING_RIVAL_TYRE_AGE = "rival_tyre_age"
MISSING_GAP = "gap_unavailable"
MISSING_PIT_LOSS = "pit_loss"
MISSING_RIVAL_NOT_AHEAD = "rival_not_ahead"

NOTE_PILOT_LEADS = "pilot.leader"

ASSUMPTION_PLAYER_PITS_NOW = "player_pits_now"
ASSUMPTION_RIVAL_PITS_AFTER_K = "rival_pits_after_k_laps"
ASSUMPTION_SAME_PIT_LOSS = "same_pit_loss"
ASSUMPTION_ENGINE_CHOOSES_COMPOUND = "engine_chooses_compound"

_QUALITY_RANK: dict[Quality, int] = {
    Quality.OK: 0,
    Quality.LOW: 1,
    Quality.STALE: 2,
    Quality.INSUFFICIENT: 3,
    Quality.UNAVAILABLE: 4,
}


@dataclass(frozen=True, slots=True)
class UndercutEstimate:
    """Resultado del modelo de undercut, con sus supuestos declarados."""

    rival_car_index: int | None = None
    gap_s: float | None = None
    pit_loss_s: float | None = None
    gains_s: tuple[float, ...] = ()
    best_k: int | None = None
    best_gain_s: float | None = None
    viable: bool | None = None
    rejoin: RejoinEstimate | None = None
    quality: Quality = Quality.UNAVAILABLE
    missing: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "gains_s", tuple(self.gains_s))
        object.__setattr__(self, "missing", tuple(self.missing))
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        object.__setattr__(self, "notes", tuple(self.notes))
        for name in ("gap_s", "pit_loss_s", "best_gain_s"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} debe ser finito")
        for gain in self.gains_s:
            if not math.isfinite(gain):
                raise ValueError("gains_s debe contener valores finitos")
        if self.best_k is not None and self.best_k < 1:
            raise ValueError("best_k debe ser positivo")
        if self.rival_car_index is not None and self.rival_car_index < 0:
            raise ValueError("rival_car_index no puede ser negativo")


def _leader_gap(car: CarSnapshot) -> float | None:
    if car.position == 1:
        return 0.0
    if car.delta_to_leader_ms is None:
        return None
    return car.delta_to_leader_ms / 1000.0


def _worst_quality(*qualities: Quality) -> Quality:
    return max(qualities, key=_QUALITY_RANK.__getitem__)


def evaluate_undercut(
    inputs: MetricInputs,
    rival_index: int,
    *,
    player_model: CompoundModel | None,
    rival_model: CompoundModel | None,
    rival_age_laps: float | None,
    rival_clean_laps: int,
    pit_loss_s: float | None,
    cfg: StrategyConfig,
    pilot: PilotConfig,
) -> UndercutEstimate:
    """Evalúa el undercut contra ``rival_index`` con modelos explícitos.

    El piloto para ahora con el compuesto que elija el motor (modelo recibido);
    el rival continúa ``k`` vueltas con su ritmo y desgaste y después para con la
    misma pérdida. El undercut sale si la ganancia en esas ``k`` vueltas supera
    el intervalo actual.
    """
    missing: list[str] = []
    player = inputs.car
    rival = next(
        (car for car in inputs.state.cars if car.car_index == rival_index),
        None,
    )
    if player is None:
        missing.append(MISSING_PLAYER_UNAVAILABLE)
    elif player.position is None:
        missing.append(MISSING_PLAYER_POSITION)
    elif player.position == 1:
        return UndercutEstimate(
            rival_car_index=rival_index if rival is not None else None,
            quality=Quality.INSUFFICIENT,
            missing=(MISSING_PILOT_LEADS,),
            notes=(NOTE_PILOT_LEADS,),
        )
    if rival is None:
        missing.append(MISSING_RIVAL_UNAVAILABLE)
    if rival_clean_laps < pilot.undercut_min_rival_clean_laps:
        missing.append(MISSING_RIVAL_CLEAN_LAPS)
    if player_model is None or not player_model.available:
        missing.append(MISSING_PLAYER_MODEL)
    if rival_model is None or not rival_model.available:
        missing.append(MISSING_RIVAL_MODEL)
    if rival_age_laps is None:
        missing.append(MISSING_RIVAL_TYRE_AGE)
    if pit_loss_s is None:
        missing.append(MISSING_PIT_LOSS)

    gap = None
    if player is not None and rival is not None:
        player_gap = _leader_gap(player)
        rival_gap = _leader_gap(rival)
        if player_gap is not None and rival_gap is not None:
            gap = player_gap - rival_gap
    if gap is None:
        missing.append(MISSING_GAP)
    elif gap <= 0:
        missing.append(MISSING_RIVAL_NOT_AHEAD)

    if missing:
        return UndercutEstimate(
            rival_car_index=rival_index if rival is not None else None,
            quality=Quality.INSUFFICIENT,
            missing=tuple(missing),
        )

    assert player_model is not None and rival_model is not None
    assert rival_age_laps is not None and pit_loss_s is not None and gap is not None

    gains: list[float] = []
    for k in range(1, pilot.undercut_horizon_laps + 1):
        rival_times = project_lap_times(rival_model, rival_age_laps, k)
        player_times = project_lap_times(player_model, 0.0, k)
        gains.append(math.fsum(rival_times) - math.fsum(player_times))

    best_index = max(range(len(gains)), key=lambda index: (gains[index], -index))
    best_gain = gains[best_index]
    rejoin = estimate_rejoin(
        inputs,
        pit_loss_s,
        cfg,
        pit_loss_band_s=cfg.pit_loss_band_s,
    )
    quality = _worst_quality(player_model.quality, rival_model.quality, rejoin.quality)
    return UndercutEstimate(
        rival_car_index=rival_index,
        gap_s=gap,
        pit_loss_s=pit_loss_s,
        gains_s=tuple(gains),
        best_k=best_index + 1,
        best_gain_s=best_gain,
        viable=best_gain > gap,
        rejoin=rejoin,
        quality=quality,
        assumptions=(
            ASSUMPTION_PLAYER_PITS_NOW,
            ASSUMPTION_RIVAL_PITS_AFTER_K,
            ASSUMPTION_SAME_PIT_LOSS,
            ASSUMPTION_ENGINE_CHOOSES_COMPOUND,
            ASSUMPTION_RIVALS_DO_NOT_PIT,
        ),
    )


def _indicated_rival(routed: RoutedQuery) -> int | None:
    for entity in routed.entities:
        if entity.car_index is not None:
            return entity.car_index
    return None


def _car_ahead(state: RaceStateSnapshot, player: CarSnapshot) -> int | None:
    if player.position is None or player.position <= 1:
        return None
    for car in state.cars:
        if car.position == player.position - 1 and car.car_index != player.car_index:
            return car.car_index
    return None


def _clean_stint_laps(
    inputs: MetricInputs,
    car_index: int,
    cfg: MetricsConfig,
) -> int:
    bounds = current_stint_bounds(inputs, car_index)
    if bounds is None:
        return 0
    start, end = bounds
    count = 0
    for record in inputs.history.records_for(car_index):
        if not record.closed or record.valid is False or record.lap_time_s is None:
            continue
        if "telemetry_partial" in record.flags:
            continue
        if record.lap_number < start or (end is not None and record.lap_number > end):
            continue
        if is_clean(classify_lap(record, cfg)):
            count += 1
    return count


def _mounted_compound(car: CarSnapshot) -> str | None:
    compound = canonical_compound(car.actual_tyre_compound)
    if compound is not None:
        return compound
    if car.fitted_tyre is None:
        return None
    return canonical_compound(car.fitted_tyre.actual_tyre_compound)


def _engine_choice(
    models: CompoundModels,
    horizon_laps: int,
) -> CompoundModel | None:
    available = models.available
    if not available:
        return None
    return min(
        available,
        key=lambda model: (math.fsum(project_lap_times(model, 0.0, horizon_laps)), model.compound),
    )


def pit_undercut(routed: RoutedQuery, inputs: ToolInputs) -> ToolResult:
    """Herramienta de la intención ``PIT_UNDERCUT``."""
    state = inputs.state
    player = state.player_car
    if player is None:
        return ToolResult(
            intent=Intent.PIT_UNDERCUT,
            quality=Quality.UNAVAILABLE,
            missing=(MISSING_PLAYER_UNAVAILABLE,),
        )
    if player.position is None:
        return ToolResult(
            intent=Intent.PIT_UNDERCUT,
            quality=Quality.INSUFFICIENT,
            missing=(MISSING_PLAYER_POSITION,),
            data_lap=state.current_lap,
        )
    if player.position == 1:
        return ToolResult(
            intent=Intent.PIT_UNDERCUT,
            quality=Quality.INSUFFICIENT,
            missing=(MISSING_PILOT_LEADS,),
            notes=(NOTE_PILOT_LEADS,),
            data_lap=state.current_lap,
        )

    rival_index = _indicated_rival(routed)
    if rival_index is None:
        rival_index = _car_ahead(state, player)
    if rival_index is None:
        return ToolResult(
            intent=Intent.PIT_UNDERCUT,
            quality=Quality.INSUFFICIENT,
            missing=(MISSING_RIVAL_UNAVAILABLE,),
            data_lap=state.current_lap,
        )

    metrics_cfg = MetricsConfig()
    strategy_cfg = StrategyConfig()
    player_inputs = MetricInputs(
        state,
        inputs.history,
        metrics_cfg,
        inputs.now,
        state.player_car_index if state.player_car_index is not None else player.car_index,
        inputs.profile,
    )
    rival_inputs = replace(player_inputs, car_index=rival_index)
    rival = next((car for car in state.cars if car.car_index == rival_index), None)

    rival_model = None
    rival_age_laps: float | None = None
    if rival is not None:
        compound = _mounted_compound(rival)
        if compound is not None:
            rival_model = build_compound_models(rival_inputs, strategy_cfg).get(compound)
        rival_age_laps = rival.tyre_age_laps
        if rival_age_laps is None and rival_model is not None:
            rival_age_laps = rival_model.initial_age_laps

    pit_loss_s = _pit_loss(inputs.strategy, player_inputs, strategy_cfg)
    estimate = evaluate_undercut(
        player_inputs,
        rival_index,
        player_model=_engine_choice(
            build_compound_models(player_inputs, strategy_cfg),
            inputs.config.undercut_horizon_laps,
        ),
        rival_model=rival_model,
        rival_age_laps=rival_age_laps,
        rival_clean_laps=_clean_stint_laps(player_inputs, rival_index, metrics_cfg),
        pit_loss_s=pit_loss_s,
        cfg=strategy_cfg,
        pilot=inputs.config,
    )
    if estimate.gap_s is None:
        return ToolResult(
            intent=Intent.PIT_UNDERCUT,
            quality=estimate.quality,
            missing=estimate.missing,
            data_lap=state.current_lap,
        )

    facts = [
        ToolFact("rival_car_index", rival_index, None, FactKind.MEASURED, Quality.OK),
        ToolFact("gap_s", estimate.gap_s, "s", FactKind.MEASURED, Quality.OK),
        ToolFact("pit_loss_s", estimate.pit_loss_s, "s", FactKind.ESTIMATED, estimate.quality),
        ToolFact("best_k", estimate.best_k, "laps", FactKind.ESTIMATED, estimate.quality),
        ToolFact("best_gain_s", estimate.best_gain_s, "s", FactKind.ESTIMATED, estimate.quality),
        ToolFact("viable", estimate.viable, None, FactKind.ESTIMATED, estimate.quality),
    ]
    rejoin = estimate.rejoin
    if rejoin is not None and rejoin.position is not None:
        facts.append(
            ToolFact(
                "rejoin_position",
                rejoin.position,
                "position",
                FactKind.ESTIMATED,
                rejoin.quality,
            )
        )
        facts.append(
            ToolFact(
                "traffic_risk",
                rejoin.traffic_risk,
                None,
                FactKind.ESTIMATED,
                rejoin.quality,
            )
        )
    return ToolResult(
        intent=Intent.PIT_UNDERCUT,
        quality=estimate.quality,
        facts=tuple(facts),
        missing=estimate.missing,
        data_lap=state.current_lap,
        notes=estimate.assumptions,
    )


def _pit_loss(
    strategy: StrategySnapshot,
    inputs: MetricInputs,
    cfg: StrategyConfig,
) -> float | None:
    evaluation = strategy.evaluation
    if evaluation is not None and evaluation.pit_loss_s is not None:
        return evaluation.pit_loss_s
    return estimate_pit_loss(inputs, cfg).value_s


__all__ = [
    "ASSUMPTION_ENGINE_CHOOSES_COMPOUND",
    "ASSUMPTION_PLAYER_PITS_NOW",
    "ASSUMPTION_RIVAL_PITS_AFTER_K",
    "ASSUMPTION_SAME_PIT_LOSS",
    "NOTE_PILOT_LEADS",
    "UndercutEstimate",
    "evaluate_undercut",
    "pit_undercut",
]
