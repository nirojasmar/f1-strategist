"""Preguntas del piloto (Fase 7): contrato común y registro de herramientas."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .tools_undercut import pit_undercut
from .types import Intent, Tool

# Las herramientas se registran en las tareas Q5 a Q9; Q1 deja el registro vacío.
TOOLS: Mapping[Intent, Tool] = MappingProxyType({Intent.PIT_UNDERCUT: pit_undercut})

__all__ = ["TOOLS"]
