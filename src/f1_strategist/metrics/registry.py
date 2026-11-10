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
from .profile import ProfileMetrics
from .progress import ProgressMetrics
from .tyres import TyreMetrics


def create_metric_modules() -> tuple[MetricModule, ...]:
    """Crea módulos nuevos para que cada motor tenga su propio estado."""
    return (
        ProgressMetrics(),
        FuelMetrics(),
        TyreMetrics(),
        PaceMetrics(),
        GapMetrics(),
        PitMetrics(),
        ERSMetrics(),
        DamageMetrics(),
        EnvironmentMetrics(),
        ProfileMetrics(),
    )


METRIC_MODULES: tuple[MetricModule, ...] = create_metric_modules()

__all__ = ["METRIC_MODULES", "create_metric_modules"]
