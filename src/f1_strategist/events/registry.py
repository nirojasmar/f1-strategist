"""Registro de reglas de detección.

Cada regla de E4 a E7 añade una línea a ``EVENT_RULES``. En E1 el registro
está vacío para que la integración pueda construirse antes que las reglas.
"""

from __future__ import annotations

from .types import EventRule

EVENT_RULES: tuple[EventRule, ...] = ()
