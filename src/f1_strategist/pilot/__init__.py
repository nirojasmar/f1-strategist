"""Preguntas del piloto (Fase 7): contrato común y registro de herramientas."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .tools_compare import compare_driver, gap_status
from .types import Intent, Tool

# Las herramientas se registran en las tareas Q5 a Q9.
TOOLS: Mapping[Intent, Tool] = MappingProxyType(
    {
        Intent.COMPARE_DRIVER: compare_driver,
        Intent.GAP_STATUS: gap_status,
    }
)

__all__ = ["TOOLS"]
