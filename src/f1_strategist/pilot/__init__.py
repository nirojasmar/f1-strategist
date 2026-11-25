"""Preguntas del piloto (Fase 7): contrato común y registro de herramientas."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .tools_compare import compare_driver, gap_status
from .tools_diagnosis import diagnose_pace
from .tools_resources import TOOLS as RESOURCE_TOOLS
from .tools_strategy import ask_strategy_or_simulate
from .tools_undercut import pit_undercut
from .types import Intent, Tool

# Las herramientas se registran en las tareas Q5 a Q9.
TOOLS: Mapping[Intent, Tool] = MappingProxyType(
    {
        **RESOURCE_TOOLS,
        Intent.ASK_STRATEGY: ask_strategy_or_simulate,
        Intent.COMPARE_DRIVER: compare_driver,
        Intent.GAP_STATUS: gap_status,
        Intent.PACE_DIAGNOSIS: diagnose_pace,
        Intent.PIT_UNDERCUT: pit_undercut,
    }
)

__all__ = ["TOOLS"]
