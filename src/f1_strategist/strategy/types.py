"""Tipos inmutables del motor de estrategia y su serialización canónica.

Este módulo fija el contrato común de la Fase 4: acciones, alternativas,
evaluación y snapshot. No contiene cálculo: los modelos de ritmo, boxes,
neumáticos y decisión viven en sus propios módulos y reciben estos tipos.

La serialización es estable: las claves se ordenan y los flotantes se
redondean, de modo que dos ejecuciones equivalentes producen el mismo JSON y
el mismo ``inputs_hash``. Los textos visibles son claves de mensaje; el
español se resuelve en la capa de presentación.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from ..metrics.types import Quality

STRATEGY_SCHEMA_VERSION = 1
"""Versión del contrato serializado; se incrementa ante cambios incompatibles."""

BASELINE_STAY_OUT = "stay_out"
"""Clave de la línea base: continuar con el compuesto montado."""

ASSUMPTION_FUEL_EFFECT_NOT_SEPARATED = "fuel_effect_not_separated"
ASSUMPTION_RIVALS_DO_NOT_PIT = "rivals_do_not_pit"
ASSUMPTION_PROFILE_PRIOR_USED = "profile_prior_used"

RISK_DAMAGE_NOT_MODELLED = "damage_not_modelled"
RISK_TYRE_DAMAGE_NOT_MODELLED = "tyre_damage_not_modelled"
RISK_TRAFFIC_NOT_PRICED = "traffic_not_priced"
RISK_TYRE_LIFE_EXCEEDED = "tyre_life_exceeded"
RISK_WET_CONDITIONS = "wet_conditions"

_FLOAT_DIGITS = 9


class StrategyAction(StrEnum):
    """Acción recomendada por el motor."""

    NO_CHANGE = "NO_CHANGE"
    PIT_NOW = "PIT_NOW"
    PIT_IN = "PIT_IN"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True, slots=True)
class Perturbation:
    """Variación aplicada a las entradas para medir sensibilidad."""

    slope_scale: float = 1.0
    pit_loss_delta_s: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.slope_scale) or self.slope_scale <= 0:
            raise ValueError("slope_scale debe ser finito y positivo")
        if not math.isfinite(self.pit_loss_delta_s):
            raise ValueError("pit_loss_delta_s debe ser finito")


@dataclass(frozen=True, slots=True)
class AlternativeResult:
    """Resultado de evaluar una alternativa concreta (salida de S5)."""

    key: str
    action: StrategyAction
    remaining_time_s: float
    gain_vs_baseline_s: float
    quality: Quality
    pit_lap: int | None = None
    compound: str | None = None
    set_index: int | None = None
    projected_laps_s: float | None = None
    pit_loss_s: float | None = None
    traffic_cost_s: float | None = None
    rejoin_position: int | None = None
    traffic_risk: bool = False
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("La clave de la alternativa no puede estar vacía")
        _check_finite("remaining_time_s", self.remaining_time_s)
        _check_finite("gain_vs_baseline_s", self.gain_vs_baseline_s)
        if self.pit_lap is not None and self.pit_lap < 1:
            raise ValueError("pit_lap debe ser positivo")
        if self.set_index is not None and self.set_index < 0:
            raise ValueError("set_index no puede ser negativo")
        if self.rejoin_position is not None and self.rejoin_position < 1:
            raise ValueError("rejoin_position debe ser positivo")
        for name in ("projected_laps_s", "pit_loss_s", "traffic_cost_s"):
            value = getattr(self, name)
            if value is not None:
                _check_finite(name, value)
        object.__setattr__(self, "notes", tuple(self.notes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "action": self.action.value,
            "pit_lap": self.pit_lap,
            "compound": self.compound,
            "set_index": self.set_index,
            "remaining_time_s": _round(self.remaining_time_s),
            "gain_vs_baseline_s": _round(self.gain_vs_baseline_s),
            "projected_laps_s": _round(self.projected_laps_s),
            "pit_loss_s": _round(self.pit_loss_s),
            "traffic_cost_s": _round(self.traffic_cost_s),
            "rejoin_position": self.rejoin_position,
            "traffic_risk": self.traffic_risk,
            "quality": self.quality.value,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class StrategyRecommendation:
    """Recomendación publicada dentro de una evaluación."""

    action: StrategyAction
    key: str
    pit_lap: int | None = None
    compound: str | None = None
    gain_vs_baseline_s: float | None = None
    gain_interval_s: tuple[float, float] | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("La clave de la recomendación no puede estar vacía")
        if self.pit_lap is not None and self.pit_lap < 1:
            raise ValueError("pit_lap debe ser positivo")
        if self.gain_vs_baseline_s is not None:
            _check_finite("gain_vs_baseline_s", self.gain_vs_baseline_s)
        if self.gain_interval_s is not None:
            low, high = self.gain_interval_s
            _check_finite("gain_interval_s", low)
            _check_finite("gain_interval_s", high)
            if low > high:
                raise ValueError("gain_interval_s requiere low <= high")
            object.__setattr__(self, "gain_interval_s", (low, high))
        if self.confidence is not None:
            _check_finite("confidence", self.confidence)
            if not 0 <= self.confidence <= 1:
                raise ValueError("confidence debe estar entre 0 y 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "key": self.key,
            "pit_lap": self.pit_lap,
            "compound": self.compound,
            "gain_vs_baseline_s": _round(self.gain_vs_baseline_s),
            "gain_interval_s": (
                None
                if self.gain_interval_s is None
                else [_round(self.gain_interval_s[0]), _round(self.gain_interval_s[1])]
            ),
            "confidence": _round(self.confidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StrategyRecommendation:
        """Reconstruye la recomendación desde su representación JSON validada."""
        interval = data.get("gain_interval_s")
        return cls(
            action=StrategyAction(data["action"]),
            key=str(data["key"]),
            pit_lap=data.get("pit_lap"),
            compound=data.get("compound"),
            gain_vs_baseline_s=data.get("gain_vs_baseline_s"),
            gain_interval_s=(
                None if interval is None else (float(interval[0]), float(interval[1]))
            ),
            confidence=data.get("confidence"),
        )


@dataclass(frozen=True, slots=True)
class StrategyAlternative:
    """Alternativa tal como se publica en la evaluación."""

    key: str
    remaining_time_s: float
    gain_vs_baseline_s: float
    rejoin_position: int | None = None
    traffic_risk: bool | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("La clave de la alternativa no puede estar vacía")
        _check_finite("remaining_time_s", self.remaining_time_s)
        _check_finite("gain_vs_baseline_s", self.gain_vs_baseline_s)
        if self.rejoin_position is not None and self.rejoin_position < 1:
            raise ValueError("rejoin_position debe ser positivo")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "remaining_time_s": _round(self.remaining_time_s),
            "gain_vs_baseline_s": _round(self.gain_vs_baseline_s),
            "rejoin_position": self.rejoin_position,
            "traffic_risk": self.traffic_risk,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StrategyAlternative:
        """Reconstruye la alternativa desde su representación JSON validada."""
        return cls(
            key=str(data["key"]),
            remaining_time_s=float(data["remaining_time_s"]),
            gain_vs_baseline_s=float(data["gain_vs_baseline_s"]),
            rejoin_position=data.get("rejoin_position"),
            traffic_risk=data.get("traffic_risk"),
        )


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    """Evaluación completa, inmutable y serializable (sección 5.8 del plan)."""

    revision: int
    session_uid: str
    lap: int
    laps_remaining: float | None
    baseline: str
    recommendation: StrategyRecommendation
    alternatives: tuple[StrategyAlternative, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    sensitive_to: tuple[str, ...]
    data_quality: Quality
    inputs_hash: str
    profile_hash: str | None = None
    schema_version: int = STRATEGY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version no soportada: {self.schema_version}; "
                f"esperada {STRATEGY_SCHEMA_VERSION}"
            )
        if self.revision < 0:
            raise ValueError("revision no puede ser negativa")
        if not self.session_uid:
            raise ValueError("session_uid no puede estar vacío")
        if self.lap < 1:
            raise ValueError("lap debe ser positivo")
        if self.laps_remaining is not None:
            _check_finite("laps_remaining", self.laps_remaining)
            if self.laps_remaining < 0:
                raise ValueError("laps_remaining no puede ser negativo")
        if not self.baseline:
            raise ValueError("baseline no puede estar vacía")
        if not self.inputs_hash:
            raise ValueError("inputs_hash no puede estar vacío")
        alternatives = tuple(self.alternatives)
        keys = [item.key for item in alternatives]
        if len(set(keys)) != len(keys):
            raise ValueError("Hay alternativas duplicadas en la evaluación")
        # Las alternativas se publican ordenadas por clave: así dos evaluaciones
        # equivalentes se serializan igual. Los supuestos, riesgos y
        # sensibilidades son conjuntos y se canonizan alfabéticamente.
        object.__setattr__(
            self,
            "alternatives",
            tuple(sorted(alternatives, key=lambda item: item.key)),
        )
        object.__setattr__(self, "assumptions", tuple(sorted(set(self.assumptions))))
        object.__setattr__(self, "risks", tuple(sorted(set(self.risks))))
        object.__setattr__(self, "sensitive_to", tuple(sorted(set(self.sensitive_to))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "session_uid": self.session_uid,
            "lap": self.lap,
            "laps_remaining": _round(self.laps_remaining),
            "baseline": self.baseline,
            "recommendation": self.recommendation.to_dict(),
            "alternatives": [item.to_dict() for item in self.alternatives],
            "assumptions": list(self.assumptions),
            "risks": list(self.risks),
            "sensitive_to": list(self.sensitive_to),
            "data_quality": self.data_quality.value,
            "inputs_hash": self.inputs_hash,
            "profile_hash": self.profile_hash,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StrategyEvaluation:
        """Reconstruye la evaluación desde su representación JSON validada."""
        return cls(
            revision=int(data["revision"]),
            session_uid=str(data["session_uid"]),
            lap=int(data["lap"]),
            laps_remaining=data.get("laps_remaining"),
            baseline=str(data["baseline"]),
            recommendation=StrategyRecommendation.from_dict(data["recommendation"]),
            alternatives=tuple(
                StrategyAlternative.from_dict(item) for item in data["alternatives"]
            ),
            assumptions=tuple(data["assumptions"]),
            risks=tuple(data["risks"]),
            sensitive_to=tuple(data["sensitive_to"]),
            data_quality=Quality(data["data_quality"]),
            inputs_hash=str(data["inputs_hash"]),
            profile_hash=data.get("profile_hash"),
            schema_version=int(data.get("schema_version", STRATEGY_SCHEMA_VERSION)),
        )

    @classmethod
    def from_json(cls, text: str) -> StrategyEvaluation:
        """Reconstruye la evaluación desde su JSON canónico."""
        payload = json.loads(text)
        if not isinstance(payload, Mapping):
            raise ValueError("La evaluación debe ser un objeto JSON")
        return cls.from_dict(payload)


@dataclass(frozen=True, slots=True)
class StrategySnapshot:
    """Última evaluación publicada por el motor; vacío hasta que S7 exista."""

    evaluation: StrategyEvaluation | None = None
    updated_at_monotonic: float = 0.0
    error: str | None = None

    def __post_init__(self) -> None:
        _check_finite("updated_at_monotonic", self.updated_at_monotonic)
        if self.updated_at_monotonic < 0:
            raise ValueError("updated_at_monotonic no puede ser negativo")

    @property
    def available(self) -> bool:
        return self.evaluation is not None


def compute_inputs_hash(payload: object) -> str:
    """SHA-256 del JSON canónico de las entradas, independiente del orden."""
    return hashlib.sha256(_canonical_json(_normalize(payload)).encode("utf-8")).hexdigest()


def _check_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} debe ser finito")


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    rounded = round(value, _FLOAT_DIGITS)
    return 0.0 if rounded == 0 else rounded


def _normalize(value: object) -> Any:
    if isinstance(value, Enum):
        return _normalize(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _normalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        # ``Mapping`` cubre también los ``MappingProxyType`` del histórico; sin
        # esto caerían al ``str(value)`` y el orden de inserción alteraría el hash.
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted((_normalize(item) for item in value), key=_canonical_json)
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("El hash de entradas no admite valores no finitos")
        return _round(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
