"""Preguntas del piloto (Fase 7): contrato común y registro de herramientas."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .tools_diagnosis import diagnose_pace
from .types import Intent, Tool

# Las herramientas se registran en las tareas Q5 a Q9; Q1 deja el registro vacío.
TOOLS: Mapping[Intent, Tool] = MappingProxyType(
    {
        Intent.PACE_DIAGNOSIS: diagnose_pace,
    }
)

__all__ = ["TOOLS"]
