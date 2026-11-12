"""Motor de estrategia: tipos comunes, modelos y decisión (Fase 4)."""

from __future__ import annotations

from .pace_model import (
    CompoundModel,
    CompoundModels,
    build_compound_models,
    project_lap_times,
    project_tyre_option_lap_times,
)
from .pit_model import (
    PitLossEstimate,
    RejoinEstimate,
    estimate_pit_loss,
    estimate_rejoin,
)
from .types import (
    ASSUMPTION_FUEL_EFFECT_NOT_SEPARATED,
    ASSUMPTION_PROFILE_PRIOR_USED,
    ASSUMPTION_RIVALS_DO_NOT_PIT,
    BASELINE_STAY_OUT,
    RISK_DAMAGE_NOT_MODELLED,
    RISK_TRAFFIC_NOT_PRICED,
    RISK_TYRE_DAMAGE_NOT_MODELLED,
    RISK_TYRE_LIFE_EXCEEDED,
    RISK_WET_CONDITIONS,
    STRATEGY_SCHEMA_VERSION,
    AlternativeResult,
    Perturbation,
    StrategyAction,
    StrategyAlternative,
    StrategyEvaluation,
    StrategyRecommendation,
    StrategySnapshot,
    compute_inputs_hash,
)
from .tyre_options import (
    TyreOption,
    TyreOptions,
    tyre_options,
)

__all__ = [
    "ASSUMPTION_FUEL_EFFECT_NOT_SEPARATED",
    "ASSUMPTION_PROFILE_PRIOR_USED",
    "ASSUMPTION_RIVALS_DO_NOT_PIT",
    "BASELINE_STAY_OUT",
    "RISK_DAMAGE_NOT_MODELLED",
    "RISK_TRAFFIC_NOT_PRICED",
    "RISK_TYRE_DAMAGE_NOT_MODELLED",
    "RISK_TYRE_LIFE_EXCEEDED",
    "RISK_WET_CONDITIONS",
    "STRATEGY_SCHEMA_VERSION",
    "AlternativeResult",
    "Perturbation",
    "StrategyAction",
    "StrategyAlternative",
    "StrategyEvaluation",
    "StrategyRecommendation",
    "StrategySnapshot",
    "compute_inputs_hash",
    "CompoundModel",
    "CompoundModels",
    "build_compound_models",
    "project_lap_times",
    "project_tyre_option_lap_times",
    "PitLossEstimate",
    "RejoinEstimate",
    "estimate_pit_loss",
    "estimate_rejoin",
    "TyreOption",
    "TyreOptions",
    "tyre_options",
]
