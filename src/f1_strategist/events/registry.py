"""Registro de reglas de detección.

Cada regla de E4 a E7 añade una línea a ``EVENT_RULES``. En E1 el registro
está vacío para que la integración pueda construirse antes que las reglas.
"""

from __future__ import annotations

from .rules_resources import (
    FuelMarginCriticalRule,
    FuelMarginLowRule,
    TyreDegradationAbnormalRule,
    TyreLifeNearLimitRule,
)
from .rules_strategy import PitWindowOpenRule, StrategyPitRecommendedRule
from .rules_track import (
    DataStaleRule,
    RainExpectedRule,
    RedFlagRule,
    SafetyCarClearedRule,
    SafetyCarDeployedRule,
    VirtualSafetyCarDeployedRule,
    WeatherChangedRule,
)
from .types import EventRule

EVENT_RULES: tuple[EventRule, ...] = (
    StrategyPitRecommendedRule(),
    PitWindowOpenRule(),
    FuelMarginLowRule(),
    FuelMarginCriticalRule(),
    TyreLifeNearLimitRule(),
    TyreDegradationAbnormalRule(),
    SafetyCarDeployedRule(),
    VirtualSafetyCarDeployedRule(),
    SafetyCarClearedRule(),
    RedFlagRule(),
    WeatherChangedRule(),
    RainExpectedRule(),
    DataStaleRule(),
)
