"""Registro central de los módulos de métricas disponibles."""

from __future__ import annotations

from .damage import DamageMetrics
from .environment import EnvironmentMetrics
from .ers import ERSMetrics
from .fuel import FuelMetrics
from .gap import GapMetrics
from .history import MetricModule
from .pace import PaceMetrics
from .pit import PitMetrics
from .progress import ProgressMetrics
from .tyres import TyreMetrics

METRIC_MODULES: tuple[MetricModule, ...] = (
    ProgressMetrics(),
    FuelMetrics(),
    TyreMetrics(),
    PaceMetrics(),
    GapMetrics(),
    PitMetrics(),
    ERSMetrics(),
    DamageMetrics(),
    EnvironmentMetrics(),
)

__all__ = ["METRIC_MODULES"]
