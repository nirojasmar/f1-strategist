"""Registro central de los módulos de métricas disponibles."""

from __future__ import annotations

from .history import MetricModule

METRIC_MODULES: tuple[MetricModule, ...] = ()

__all__ = ["METRIC_MODULES"]
