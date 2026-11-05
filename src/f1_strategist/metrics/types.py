"""Modelos inmutables y utilidades comunes para las métricas."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

WindowKind = Literal["instant", "laps", "seconds", "samples", "stint", "session"]
MetricValue = float | int | str | bool | tuple[str, ...] | None


class Quality(StrEnum):
    OK = "ok"
    LOW = "low"
    STALE = "stale"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Window:
    kind: WindowKind
    size: int | float | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"instant", "laps", "seconds", "samples", "stint", "session"}:
            raise ValueError(f"Tipo de ventana no válido: {self.kind!r}")
        if self.kind in {"instant", "session"} and self.size is not None:
            raise ValueError(f"La ventana {self.kind!r} no admite tamaño")
        if self.size is not None and (
            isinstance(self.size, bool) or not math.isfinite(self.size) or self.size <= 0
        ):
            raise ValueError("El tamaño de la ventana debe ser finito y positivo")
        if (
            self.kind in {"laps", "samples"}
            and self.size is not None
            and not isinstance(self.size, int)
        ):
            raise ValueError(f"La ventana {self.kind!r} requiere un tamaño entero")


@dataclass(frozen=True, slots=True)
class Metric:
    key: str
    value: MetricValue
    unit: str
    window: Window
    quality: Quality
    sample_count: int
    computed_at_monotonic: float
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("La clave de métrica no puede estar vacía")
        if self.sample_count < 0:
            raise ValueError("sample_count no puede ser negativo")
        if not math.isfinite(self.computed_at_monotonic):
            raise ValueError("computed_at_monotonic debe ser finito")
        if self.quality in {Quality.INSUFFICIENT, Quality.UNAVAILABLE} and self.value is not None:
            raise ValueError(f"Una métrica {self.quality.value!r} debe tener value=None")
        if self.quality in {Quality.OK, Quality.LOW, Quality.STALE} and self.value is None:
            raise ValueError(f"Una métrica {self.quality.value!r} debe tener un valor")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("El valor de la métrica debe ser finito")
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "notes", tuple(self.notes))


def assess_quality(
    sample_count: int,
    ok_at: int,
    low_at: int,
    stale: bool,
) -> Quality:
    """Evalúa la calidad según el número de muestras y la frescura de los datos."""
    if sample_count < 0:
        raise ValueError("sample_count no puede ser negativo")
    if low_at < 1 or ok_at < low_at:
        raise ValueError("Se requiere 1 <= low_at <= ok_at")
    if sample_count == 0:
        return Quality.INSUFFICIENT
    if stale:
        return Quality.STALE
    if sample_count >= ok_at:
        return Quality.OK
    if sample_count >= low_at:
        return Quality.LOW
    return Quality.INSUFFICIENT


def unavailable(
    key: str,
    unit: str,
    reason_key: str,
    now: float,
    *,
    window: Window | None = None,
    sample_count: int = 0,
    sources: tuple[str, ...] = (),
) -> Metric:
    return Metric(
        key=key,
        value=None,
        unit=unit,
        window=window or Window("instant"),
        quality=Quality.UNAVAILABLE,
        sample_count=sample_count,
        computed_at_monotonic=now,
        sources=sources,
        notes=(reason_key,),
    )


def insufficient(
    key: str,
    unit: str,
    reason_key: str,
    now: float,
    *,
    window: Window | None = None,
    sample_count: int = 0,
    sources: tuple[str, ...] = (),
) -> Metric:
    return Metric(
        key=key,
        value=None,
        unit=unit,
        window=window or Window("instant"),
        quality=Quality.INSUFFICIENT,
        sample_count=sample_count,
        computed_at_monotonic=now,
        sources=sources,
        notes=(reason_key,),
    )
